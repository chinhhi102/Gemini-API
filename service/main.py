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
from .config import config
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
            "Requires media=base64 or stream (the bytes must be downloaded); "
            "ignored for media=url. Does not affect invisible SynthID."
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
    try:
        yield
    finally:
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


async def _proxy_media(entries, session, mode, request, dewatermark=False) -> list[MediaItem]:
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
            items.append(_to_item(kind, title, url, path, mode, request))
        return items
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _to_item(kind, title, url, path, mode, request) -> MediaItem:
    mime, size = guess_mime(path), Path(path).stat().st_size
    base = MediaItem(kind=kind, title=title, source_url=url, mime_type=mime, size=size)
    if mode == "base64":
        base.data_base64 = base64.b64encode(Path(path).read_bytes()).decode()
    else:  # stream
        media_id = request.app.state.cache.add(path, mime, Path(path).name)
        base.stream_url = f"{str(request.base_url).rstrip('/')}/media/{media_id}"
    return base


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


# --------------------------------------------------------------------------- #
# Generation (with failover across accounts)
# --------------------------------------------------------------------------- #
@app.post("/generate", response_model=GenerateResponse, dependencies=[Auth])
async def generate(req: GenerateRequest, request: Request) -> GenerateResponse:
    manager = request.app.state.manager
    model = req.model or Model.UNSPECIFIED
    try:
        output, account = await manager.generate(req.prompt, model)
    except NoHealthyAccount as exc:
        raise HTTPException(status_code=502, detail={"message": "All accounts failed",
                                                     "attempts": exc.attempts})

    entries = _media_entries(output)
    if req.media == "url":
        media = [MediaItem(kind=k, title=t, source_url=u) for k, t, u, _ in entries]
    else:
        session = manager.session_for(account["id"])
        media = await _proxy_media(
            entries, session, req.media, request, req.remove_watermark
        )

    return GenerateResponse(
        account=account["label"],
        text=output.text,
        thoughts=output.thoughts,
        media=media,
        metadata=output.metadata,
    )


@app.get("/media/{media_id}", dependencies=[Auth])
async def get_media(media_id: str, request: Request) -> FileResponse:
    """Stream a previously downloaded media file (mode=stream). Behind the API key."""

    entry = request.app.state.cache.get(media_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Media not found or expired")
    return FileResponse(entry["path"], media_type=entry["mime"], filename=entry["filename"])
