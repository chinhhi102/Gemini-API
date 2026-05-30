"""Server-side media proxying.

Downloads Gemini-generated media through the producing account's authenticated
session so callers can receive the bytes directly — inline as base64 or via a
short-lived streamed file — instead of Gemini URLs that need the session to fetch.
"""

import mimetypes
import shutil
import time
import uuid
from pathlib import Path


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _primary_path(result, key: str | None) -> str | None:
    """Normalize a save() return (str path or dict of paths) to one file path."""

    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get(key) or next(
            (v for v in result.values() if isinstance(v, str)), None
        )
    return None


async def download_one(kind: str, item, session, dest: str) -> str | None:
    """Download a single media object via the account session; return its path.

    `session` is the authenticated curl_cffi AsyncSession from the account's
    live GeminiClient, so cookie-gated media URLs resolve correctly.
    """

    if kind == "audio":
        result = await item.save(
            path=dest, client=session, verbose=False, download_type="audio"
        )
        return _primary_path(result, "audio")
    result = await item.save(path=dest, client=session, verbose=False)
    return _primary_path(result, "video" if kind == "video" else None)


class MediaCache:
    """Short-lived on-disk cache for streamed media, keyed by opaque id.

    Entries older than `ttl` seconds are evicted (and their files deleted) on
    every access. Intended as a transient buffer, not durable storage.
    """

    def __init__(self, root: Path, ttl: float) -> None:
        self.root = root
        self.ttl = ttl
        self.root.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict] = {}

    def add(self, src_path: str, mime: str, filename: str) -> str:
        self._evict()
        media_id = uuid.uuid4().hex
        dest = self.root / f"{media_id}_{filename}"
        shutil.move(src_path, dest)
        self._index[media_id] = {
            "path": dest,
            "mime": mime,
            "filename": filename,
            "ts": time.monotonic(),
        }
        return media_id

    def get(self, media_id: str) -> dict | None:
        self._evict()
        return self._index.get(media_id)

    def _evict(self) -> None:
        now = time.monotonic()
        stale = [m for m, e in self._index.items() if now - e["ts"] > self.ttl]
        for media_id in stale:
            entry = self._index.pop(media_id)
            Path(entry["path"]).unlink(missing_ok=True)
