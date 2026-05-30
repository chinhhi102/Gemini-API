"""FastAPI service exposing Google Gemini text / image / video / voice generation.

Supports multiple Gemini accounts with automatic failover (primary → backup),
managed at runtime through a small admin UI rather than environment variables.

This service is a pass-through transporter: it returns Gemini's response exactly
as received (text and the original media URLs), without downloading or storing
any media itself.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from gemini_webapi import set_log_level
from gemini_webapi.constants import Model
from gemini_webapi.types import ModelOutput

from .accounts import AccountManager, AccountStore, NoHealthyAccount
from .config import config

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# Request / response schemas (drive the OpenAPI docs at /docs)
# --------------------------------------------------------------------------- #
class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User text prompt")
    model: str | None = Field(None, description="Model name, e.g. 'gemini-3-pro'")


class ImageOut(BaseModel):
    url: str = Field(..., description="Original Gemini image URL")
    title: str = ""
    alt: str = ""


class VideoOut(BaseModel):
    url: str = Field(..., description="Original Gemini video (mp4) URL")
    title: str = ""
    thumbnail: str = ""


class MediaOut(BaseModel):
    mp4_url: str = Field("", description="Original mp4 URL, if any")
    mp3_url: str = Field("", description="Original audio (mp3) URL, if any")
    title: str = ""
    thumbnail: str = ""
    mp3_thumbnail: str = ""


class GenerateResponse(BaseModel):
    account: str = Field(..., description="Label of the account that served the request")
    text: str = ""
    thoughts: str | None = None
    images: list[ImageOut] = []
    videos: list[VideoOut] = []
    audio: list[MediaOut] = []
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
    try:
        yield
    finally:
        await app.state.manager.close_all()


app = FastAPI(
    title="Gemini Web Service",
    version="2.1.0",
    summary="Multi-account Gemini proxy. Pass-through: returns Gemini's response as-is.",
    lifespan=lifespan,
)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != config.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


Auth = Depends(require_api_key)


def _store(request: Request) -> AccountStore:
    return request.app.state.manager.store


def _build_response(account_label: str, output: ModelOutput) -> GenerateResponse:
    """Map Gemini's output to the response schema using its original URLs."""

    return GenerateResponse(
        account=account_label,
        text=output.text,
        thoughts=output.thoughts,
        images=[ImageOut(url=i.url, title=i.title, alt=i.alt) for i in output.images],
        videos=[VideoOut(url=v.url, title=v.title, thumbnail=getattr(v, "thumbnail", ""))
                for v in output.videos],
        audio=[MediaOut(mp4_url=m.url, mp3_url=m.mp3_url, title=m.title,
                        thumbnail=m.thumbnail, mp3_thumbnail=m.mp3_thumbnail)
               for m in output.media],
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


# --------------------------------------------------------------------------- #
# Generation (with failover across accounts)
# --------------------------------------------------------------------------- #
@app.post("/generate", response_model=GenerateResponse, dependencies=[Auth])
async def generate(req: GenerateRequest, request: Request) -> GenerateResponse:
    model = req.model or Model.UNSPECIFIED
    try:
        output, account = await request.app.state.manager.generate(req.prompt, model)
    except NoHealthyAccount as exc:
        raise HTTPException(status_code=502, detail={"message": "All accounts failed",
                                                     "attempts": exc.attempts})
    return _build_response(account["label"], output)
