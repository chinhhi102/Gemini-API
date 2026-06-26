"""FastAPI service exposing Google Gemini text / image / video / voice generation.

Supports multiple Gemini accounts with automatic failover (primary → backup),
managed at runtime through a small admin UI rather than environment variables.

Media handling is selectable per request via `media`:
  - "url"    : pass-through — Gemini's original URLs (need the session to fetch).
  - "base64" : the service downloads the bytes (with the account's session) and
               returns them inline as base64.
  - "stream" : the service downloads the bytes and returns a short-lived
               `stream_url` (GET /media/{id}, behind the API key) for binary
               streaming — ideal for piping into object storage (e.g. MinIO).
"""

import asyncio
import base64
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from gemini_webapi import set_log_level
from gemini_webapi.constants import Model
from gemini_webapi.types import ModelOutput

from .accounts import AccountManager, AccountStore, NoHealthyAccount
from .settings import SettingsStore
from .config import config
from .jobs import JobQueue, JobStatus, build_job_store
from .media import MediaCache, download_one, guess_mime
from .watermark import dewatermark_file

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# Request / response schemas (drive the OpenAPI docs at /docs)
# --------------------------------------------------------------------------- #
class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User text prompt")
    model: str | None = Field(None, description="Model name, e.g. 'gemini-3-pro'")
    media: Literal["url", "base64", "stream"] = Field(
        "url", description="How media is returned: url | base64 | stream"
    )
    remove_watermark: bool = Field(
        False,
        description=(
            "Strip Gemini's visible corner watermark from generated images. "
            "The bytes must be downloaded to be cleaned: media=url is upgraded "
            "to stream when removal applies. Does not affect invisible SynthID. "
            "Note: the server may force removal on for all requests via "
            "FORCE_REMOVE_WATERMARK, in which case this flag can only add to it."
        ),
    )


class MediaItem(BaseModel):
    kind: str = Field(..., description="image | video | audio")
    title: str = ""
    source_url: str = Field("", description="Gemini's original URL")
    mime_type: str | None = Field(None, description="Set when downloaded")
    size: int | None = Field(None, description="Bytes, set when downloaded")
    data_base64: str | None = Field(None, description="Set when media=base64")
    stream_url: str | None = Field(None, description="GET it (with API key) when media=stream")


class GenerateResponse(BaseModel):
    account: str = Field(..., description="Label of the account that served the request")
    text: str = ""
    thoughts: str | None = None
    media: list[MediaItem] = []
    metadata: list[str] = Field(default=[], description="[chat_id, reply_id, ...]")


class JobRequest(GenerateRequest):
    """A /generate request submitted for asynchronous processing."""

    callback_url: str | None = Field(
        None,
        description=(
            "Optional webhook. When set, the finished job (id, status, result, "
            "error) is POSTed here on completion. Polling GET /jobs/{id} remains "
            "available regardless."
        ),
    )


class JobOut(BaseModel):
    id: str = Field(..., description="Job id — poll GET /jobs/{id} with this")
    status: JobStatus = Field(..., description="queued | processing | completed | failed")
    result: GenerateResponse | None = Field(None, description="Set when status=completed")
    error: str | None = Field(None, description="Set when status=failed")
    created_at: str = ""
    updated_at: str = ""


class JobList(BaseModel):
    jobs: list[JobOut] = []


class AccountOut(BaseModel):
    id: str
    label: str
    enabled: bool
    priority: int
    status: str = Field(..., description="untested | ok | error")
    status_detail: str = ""
    updated_at: str = ""
    secure_1psid: str = Field("", description="Masked — last 4 chars only")
    secure_1psidts: str = Field("", description="Masked — last 4 chars only")


class AccountList(BaseModel):
    accounts: list[AccountOut] = []


class AccountIn(BaseModel):
    label: str = Field(..., min_length=1)
    secure_1psid: str = Field(..., min_length=1)
    secure_1psidts: str = ""


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    secure_1psid: str | None = None  # empty/None keeps the existing value
    secure_1psidts: str | None = None


class CookieTest(BaseModel):
    secure_1psid: str = Field(..., min_length=1)
    secure_1psidts: str = ""


class TestResult(BaseModel):
    ok: bool
    detail: str


class DeleteResult(BaseModel):
    deleted: str


class SettingsOut(BaseModel):
    default_model: str = Field(..., description="Model used when a request omits one")
    available_models: list[str] = Field(default=[], description="Selectable model names")


class SettingsIn(BaseModel):
    default_model: str = Field(..., min_length=1)


# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #
def _seed_from_env(store: AccountStore) -> None:
    if not store.list() and config.seed_1psid:
        store.add("primary (from env)", config.seed_1psid, config.seed_1psidts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    set_log_level("INFO")
    store = AccountStore(config.accounts_path)
    _seed_from_env(store)
    app.state.manager = AccountManager(store, config.proxy, config.request_timeout)
    app.state.cache = MediaCache(config.media_cache_dir, config.media_cache_ttl)
    app.state.settings = SettingsStore(config.settings_path)

    job_store = await build_job_store(config.redis_url, config.jobs_path, config.job_ttl)

    async def _handle_job(stored: dict) -> dict:
        # The stored request carries the base_url captured when it was submitted,
        # so stream URLs stay valid even though the worker has no live request.
        base_url = stored.get("base_url", "")
        gen = GenerateRequest.model_validate(stored)
        result = await perform_generation(
            app.state.manager, app.state.cache, base_url, gen, app.state.settings.default_model
        )
        return result.model_dump()

    app.state.jobs = JobQueue(
        job_store,
        _handle_job,
        config.job_workers,
        config.job_callback_timeout,
        config.job_callback_retries,
    )
    await app.state.jobs.start()
    try:
        yield
    finally:
        await app.state.jobs.close()
        await app.state.manager.close_all()


app = FastAPI(
    title="Gemini Web Service",
    version="3.0.0",
    summary="Multi-account Gemini proxy with selectable media delivery (url/base64/stream).",
    lifespan=lifespan,
)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != config.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


Auth = Depends(require_api_key)


def _store(request: Request) -> AccountStore:
    return request.app.state.manager.store


def _media_entries(output: ModelOutput) -> list[tuple]:
    """Flatten the output into (kind, title, source_url, media_object) tuples."""

    entries = [("image", i.title, i.url, i) for i in output.images]
    entries += [("video", v.title, v.url, v) for v in output.videos]
    entries += [("audio", m.title, m.mp3_url or m.url, m) for m in output.media]
    return entries


async def _proxy_media(entries, session, mode, cache, base_url, dewatermark=False) -> list[MediaItem]:
    """Download each media item via the account session; inline or cache for stream."""

    items: list[MediaItem] = []
    work_dir = Path(tempfile.mkdtemp(prefix="gemgen_"))
    try:
        for kind, title, url, obj in entries:
            path = await download_one(kind, obj, session, str(work_dir))
            if not path:
                items.append(MediaItem(kind=kind, title=title, source_url=url))
                continue
            if dewatermark and kind == "image":
                await asyncio.to_thread(dewatermark_file, path)  # CPU-bound; off-loop
            items.append(_to_item(kind, title, url, path, mode, cache, base_url))
        return items
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _to_item(kind, title, url, path, mode, cache, base_url) -> MediaItem:
    mime, size = guess_mime(path), Path(path).stat().st_size
    base = MediaItem(kind=kind, title=title, source_url=url, mime_type=mime, size=size)
    if mode == "base64":
        base.data_base64 = base64.b64encode(Path(path).read_bytes()).decode()
    else:  # stream
        media_id = cache.add(path, mime, Path(path).name)
        base.stream_url = f"{base_url.rstrip('/')}/media/{media_id}"
    return base


async def perform_generation(
    manager: AccountManager,
    cache: MediaCache,
    base_url: str,
    req: GenerateRequest,
    default_model: str,
) -> GenerateResponse:
    """Run a generation with failover and assemble the response (media included).

    Shared by the synchronous ``/generate`` endpoint and the async job worker, so
    both paths behave identically. ``base_url`` is used to build stream URLs.

    ``default_model`` (configured in the admin UI) is used when the request omits
    a model: the UNSPECIFIED bucket shares the strict default image-generation
    quota and exhausts quickly, returning a "limit resets" text with no media,
    whereas a concrete model has its own quota and reliably returns images.
    """

    model = req.model or default_model
    output, account = await manager.generate(req.prompt, model)

    entries = _media_entries(output)

    # Watermark stripping needs the bytes, so url mode can't honour it. When
    # removal is in force and the output carries an image, upgrade url -> stream
    # so the server downloads, cleans, and re-serves it.
    dewatermark = req.remove_watermark or config.force_remove_watermark
    media_mode = req.media
    if dewatermark and media_mode == "url" and any(k == "image" for k, *_ in entries):
        media_mode = "stream"

    if media_mode == "url":
        media = [MediaItem(kind=k, title=t, source_url=u) for k, t, u, _ in entries]
    else:
        session = manager.session_for(account["id"])
        media = await _proxy_media(
            entries, session, media_mode, cache, base_url, dewatermark
        )

    return GenerateResponse(
        account=account["label"],
        text=output.text,
        thoughts=output.thoughts,
        media=media,
        metadata=output.metadata,
    )


# --------------------------------------------------------------------------- #
# Public pages / metadata
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def admin_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/models")
async def models() -> dict:
    return {"models": [m.model_name for m in Model]}


# --------------------------------------------------------------------------- #
# Account management (all gated by the API key)
# --------------------------------------------------------------------------- #
@app.get("/api/accounts", response_model=AccountList, dependencies=[Auth])
async def list_accounts(request: Request) -> AccountList:
    store = _store(request)
    return AccountList(accounts=[store.public_view(a) for a in store.list()])


@app.post("/api/accounts", response_model=AccountOut, dependencies=[Auth])
async def add_account(body: AccountIn, request: Request) -> dict:
    return _store(request).public_view(
        _store(request).add(body.label, body.secure_1psid, body.secure_1psidts)
    )


@app.patch("/api/accounts/{account_id}", response_model=AccountOut, dependencies=[Auth])
async def patch_account(account_id: str, body: AccountPatch, request: Request) -> dict:
    manager = request.app.state.manager
    updated = manager.store.update(account_id, **body.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Account not found")
    await manager.invalidate(account_id)  # force re-init with new settings
    return AccountStore.public_view(updated)


@app.delete("/api/accounts/{account_id}", response_model=DeleteResult, dependencies=[Auth])
async def delete_account(account_id: str, request: Request) -> dict:
    manager = request.app.state.manager
    await manager.invalidate(account_id)
    if not manager.store.delete(account_id):
        raise HTTPException(status_code=404, detail="Account not found")
    return {"deleted": account_id}


@app.post("/api/accounts/{account_id}/test", response_model=TestResult, dependencies=[Auth])
async def test_account(account_id: str, request: Request) -> dict:
    manager = request.app.state.manager
    account = manager.store.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    ok, detail = await manager.test(account["secure_1psid"], account["secure_1psidts"])
    manager.store.set_status(account_id, "ok" if ok else "error", detail)
    return {"ok": ok, "detail": detail}


@app.post("/api/test", response_model=TestResult, dependencies=[Auth])
async def test_cookies(body: CookieTest, request: Request) -> dict:
    ok, detail = await request.app.state.manager.test(body.secure_1psid, body.secure_1psidts)
    return {"ok": ok, "detail": detail}


@app.get("/api/settings", response_model=SettingsOut, dependencies=[Auth])
async def get_settings(request: Request) -> dict:
    return request.app.state.settings.public_view()


@app.put("/api/settings", response_model=SettingsOut, dependencies=[Auth])
async def update_settings(body: SettingsIn, request: Request) -> dict:
    settings = request.app.state.settings
    try:
        settings.set_default_model(body.default_model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return settings.public_view()


# --------------------------------------------------------------------------- #
# Generation (with failover across accounts)
# --------------------------------------------------------------------------- #
@app.post("/generate", response_model=GenerateResponse, dependencies=[Auth])
async def generate(req: GenerateRequest, request: Request) -> GenerateResponse:
    try:
        return await perform_generation(
            request.app.state.manager,
            request.app.state.cache,
            str(request.base_url),
            req,
            request.app.state.settings.default_model,
        )
    except NoHealthyAccount as exc:
        raise HTTPException(status_code=502, detail={"message": "All accounts failed",
                                                     "attempts": exc.attempts})


# --------------------------------------------------------------------------- #
# Asynchronous generation jobs (submit → poll → retrieve)
# --------------------------------------------------------------------------- #
def _job_view(job: dict) -> JobOut:
    return JobOut(
        id=job["id"],
        status=job["status"],
        result=job["result"],
        error=job["error"],
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )


@app.post("/jobs", response_model=JobOut, status_code=202, dependencies=[Auth])
async def submit_job(req: JobRequest, request: Request) -> JobOut:
    """Accept a generation request, enqueue it, and return immediately.

    The response carries a job id with status "queued" — the request has been
    received and will be processed by a worker. Poll GET /jobs/{id} for progress
    and, once status is "completed", the result.
    """

    stored = req.model_dump(exclude={"callback_url"})
    # Capture the caller's base URL now so streamed media links resolve later.
    stored["base_url"] = str(request.base_url)
    job = await request.app.state.jobs.submit(stored, req.callback_url)
    return _job_view(job)


@app.get("/jobs", response_model=JobList, dependencies=[Auth])
async def list_jobs(request: Request) -> JobList:
    jobs = await request.app.state.jobs.store.list()
    return JobList(jobs=[_job_view(j) for j in jobs])


@app.get("/jobs/{job_id}", response_model=JobOut, dependencies=[Auth])
async def get_job(job_id: str, request: Request) -> JobOut:
    job = await request.app.state.jobs.store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return _job_view(job)


@app.delete("/jobs/{job_id}", response_model=DeleteResult, dependencies=[Auth])
async def delete_job(job_id: str, request: Request) -> dict:
    if not await request.app.state.jobs.store.delete(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"deleted": job_id}


@app.get("/media/{media_id}", dependencies=[Auth])
async def get_media(media_id: str, request: Request) -> FileResponse:
    """Stream a previously downloaded media file (mode=stream). Behind the API key."""

    entry = request.app.state.cache.get(media_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Media not found or expired")
    return FileResponse(entry["path"], media_type=entry["mime"], filename=entry["filename"])
