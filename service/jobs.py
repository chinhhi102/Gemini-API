"""Asynchronous generation jobs.

Decouples request submission from completion so a client never has to hold a
connection open for a slow generation:

  1. POST /jobs          -> 202 Accepted, returns a job id with status "queued"
  2. a background worker picks it up, status -> "processing"
  3. GET  /jobs/{id}      -> poll; on success status "completed" carries the result
     and/or, if a callback_url was supplied, the result is POSTed there.

Durable job state lives in Redis when a REDIS_URL is configured, otherwise in a
local JSON file. Either way, status and results survive a restart; in-flight work
itself cannot resume, so any job left "processing" when the service stopped is
re-queued when the store loads.
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from gemini_webapi import logger

_REDIS_PREFIX = "gemini:jobs"


def _now() -> str:
    # Microsecond precision so newest-first listings stay deterministic even for
    # jobs created within the same second.
    return datetime.now(timezone.utc).isoformat()


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL = {JobStatus.COMPLETED.value, JobStatus.FAILED.value}
ACTIVE = {JobStatus.QUEUED.value, JobStatus.PROCESSING.value}

# A handler turns a persisted job's stored request into a JSON-serialisable
# result. It is the only generation-aware piece the queue needs, which keeps
# this module free of any Gemini specifics.
JobHandler = Callable[[dict], Awaitable[dict]]


def _new_job(request: dict, callback_url: str | None) -> dict:
    ts = _now()
    return {
        "id": uuid.uuid4().hex,
        "status": JobStatus.QUEUED.value,
        "request": request,
        "callback_url": callback_url,
        "result": None,
        "error": None,
        "created_at": ts,
        "updated_at": ts,
    }


# --------------------------------------------------------------------------- #
# Durable job state — one interface, two backends (file / Redis)
# --------------------------------------------------------------------------- #
class JobStore(abc.ABC):
    """Persistence for jobs. Implementations: FileJobStore, RedisJobStore."""

    @abc.abstractmethod
    async def create(self, request: dict, callback_url: str | None) -> dict: ...

    @abc.abstractmethod
    async def get(self, job_id: str) -> dict | None: ...

    @abc.abstractmethod
    async def list(self) -> list[dict]: ...

    @abc.abstractmethod
    async def update(self, job_id: str, **fields) -> dict | None: ...

    @abc.abstractmethod
    async def delete(self, job_id: str) -> bool: ...

    @abc.abstractmethod
    async def requeue_interrupted(self) -> list[str]:
        """Reset jobs left mid-flight by a restart back to queued; return ids."""

    async def close(self) -> None:
        """Release backing resources (default: nothing)."""


class FileJobStore(JobStore):
    """Disk-backed JSON persistence (single-process). Mirrors AccountStore.

    Completed and failed jobs older than ``ttl`` seconds are evicted on create
    to keep the file bounded.
    """

    def __init__(self, path: Path, ttl: float) -> None:
        self.path = path
        self.ttl = ttl
        self._jobs: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self._jobs = json.loads(self.path.read_text() or "{}")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._jobs, indent=2))
        os.replace(tmp, self.path)

    def _evict(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - self.ttl
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job["status"] in TERMINAL and _epoch(job["updated_at"]) < cutoff
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    async def create(self, request: dict, callback_url: str | None) -> dict:
        async with self._lock:
            self._evict()
            job = _new_job(request, callback_url)
            self._jobs[job["id"]] = job
            self._save()
            return job

    async def get(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    async def list(self) -> list[dict]:
        return sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)

    async def update(self, job_id: str, **fields) -> dict | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.update(fields)
            job["updated_at"] = _now()
            self._save()
            return job

    async def delete(self, job_id: str) -> bool:
        async with self._lock:
            if self._jobs.pop(job_id, None) is None:
                return False
            self._save()
            return True

    async def requeue_interrupted(self) -> list[str]:
        async with self._lock:
            revived = [jid for jid, j in self._jobs.items() if j["status"] in ACTIVE]
            for jid in revived:
                self._jobs[jid]["status"] = JobStatus.QUEUED.value
            if revived:
                self._save()
            return revived


class RedisJobStore(JobStore):
    """Redis-backed persistence — survives restart and can be shared.

    Each job is a JSON string at ``gemini:jobs:job:{id}``; a sorted set
    ``gemini:jobs:index`` (scored by creation time) orders listings. Terminal
    jobs are given a TTL via Redis EXPIRE; ``list`` self-heals the index by
    dropping ids whose job key has since expired.
    """

    def __init__(self, url: str, ttl: float, client=None) -> None:
        self.ttl = ttl
        if client is not None:
            self._r = client
        else:
            import redis.asyncio as aioredis  # lazy: only when REDIS_URL is set

            self._r = aioredis.from_url(url, decode_responses=True)
        self._index = f"{_REDIS_PREFIX}:index"

    def _key(self, job_id: str) -> str:
        return f"{_REDIS_PREFIX}:job:{job_id}"

    async def ping(self) -> None:
        await self._r.ping()

    async def create(self, request: dict, callback_url: str | None) -> dict:
        job = _new_job(request, callback_url)
        await self._r.set(self._key(job["id"]), json.dumps(job))
        await self._r.zadd(self._index, {job["id"]: _epoch(job["created_at"])})
        return job

    async def get(self, job_id: str) -> dict | None:
        raw = await self._r.get(self._key(job_id))
        return json.loads(raw) if raw else None

    async def list(self) -> list[dict]:
        ids = await self._r.zrange(self._index, 0, -1, desc=True)
        if not ids:
            return []
        raws = await self._r.mget([self._key(i) for i in ids])
        jobs, missing = [], []
        for jid, raw in zip(ids, raws):
            if raw is None:
                missing.append(jid)  # job key expired — drop the dangling index entry
            else:
                jobs.append(json.loads(raw))
        if missing:
            await self._r.zrem(self._index, *missing)
        return jobs

    async def update(self, job_id: str, **fields) -> dict | None:
        raw = await self._r.get(self._key(job_id))
        if not raw:
            return None
        job = json.loads(raw)
        job.update(fields)
        job["updated_at"] = _now()
        if job["status"] in TERMINAL:
            await self._r.set(self._key(job_id), json.dumps(job), ex=int(self.ttl))
        else:
            await self._r.set(self._key(job_id), json.dumps(job))
        return job

    async def delete(self, job_id: str) -> bool:
        deleted = await self._r.delete(self._key(job_id))
        await self._r.zrem(self._index, job_id)
        return bool(deleted)

    async def requeue_interrupted(self) -> list[str]:
        revived = []
        for job in await self.list():
            if job["status"] in ACTIVE:
                await self.update(job["id"], status=JobStatus.QUEUED.value)
                revived.append(job["id"])
        return revived

    async def close(self) -> None:
        await self._r.aclose()


async def build_job_store(redis_url: str | None, jobs_path: Path, ttl: float) -> JobStore:
    """Pick the durable backend: Redis when reachable, else a local JSON file.

    A configured-but-unreachable Redis (or a missing ``redis`` package) is logged
    and degraded to the file store so the service still starts.
    """

    if redis_url:
        try:
            store = RedisJobStore(redis_url, ttl)
            await store.ping()
            logger.info("Job state backed by Redis")
            return store
        except Exception as exc:  # noqa: BLE001 - degrade rather than fail startup
            logger.error(
                f"REDIS_URL is set but Redis is unavailable ({exc}); "
                "falling back to the file-backed job store"
            )
    logger.info(f"Job state backed by file {jobs_path}")
    return FileJobStore(jobs_path, ttl)


# --------------------------------------------------------------------------- #
# Worker pool
# --------------------------------------------------------------------------- #
class JobQueue:
    """A small pool of asyncio workers draining an in-process job queue.

    Each worker pulls a job id, runs ``handler`` against its stored request, and
    records the result or error on the job. Concurrency is bounded by the worker
    count so submissions never fan out into unbounded load on the accounts. When
    a finished job carries a ``callback_url``, its result is POSTed there (with
    retries) without blocking the worker.
    """

    def __init__(
        self,
        store: JobStore,
        handler: JobHandler,
        workers: int,
        callback_timeout: float = 15.0,
        callback_retries: int = 3,
    ) -> None:
        self.store = store
        self.handler = handler
        self.workers = max(1, workers)
        self.callback_timeout = callback_timeout
        self.callback_retries = max(1, callback_retries)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._callbacks: set[asyncio.Task] = set()

    async def start(self) -> None:
        for jid in await self.store.requeue_interrupted():
            self._queue.put_nowait(jid)
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"job-worker-{i}")
            for i in range(self.workers)
        ]
        logger.info(f"Job queue started with {self.workers} worker(s)")

    async def submit(self, request: dict, callback_url: str | None = None) -> dict:
        job = await self.store.create(request, callback_url)
        self._queue.put_nowait(job["id"])
        return job

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - last-resort guard; keep worker alive
                logger.error(f"Worker {index} crashed on job {job_id}: {exc}")
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        job = await self.store.get(job_id)
        if not job:  # deleted between submit and pickup
            return
        await self.store.update(job_id, status=JobStatus.PROCESSING.value)
        try:
            result = await self.handler(job["request"])
            done = await self.store.update(
                job_id, status=JobStatus.COMPLETED.value, result=result
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client via the job
            logger.warning(f"Job {job_id} failed: {exc}")
            done = await self.store.update(
                job_id, status=JobStatus.FAILED.value, error=str(exc)
            )
        self._spawn_callback(done)

    def _spawn_callback(self, job: dict | None) -> None:
        if not job or not job.get("callback_url"):
            return
        task = asyncio.create_task(self._deliver_callback(job))
        self._callbacks.add(task)
        task.add_done_callback(self._callbacks.discard)

    async def _deliver_callback(self, job: dict) -> None:
        """POST the finished job to its callback_url, retrying with backoff."""

        url = job["callback_url"]
        payload = {
            k: job[k]
            for k in ("id", "status", "result", "error", "created_at", "updated_at")
        }
        delay = 1.0
        async with httpx.AsyncClient(timeout=self.callback_timeout) as client:
            for attempt in range(1, self.callback_retries + 1):
                try:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    logger.info(f"Job {job['id']} callback delivered to {url}")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry transient failures
                    logger.warning(
                        f"Job {job['id']} callback attempt "
                        f"{attempt}/{self.callback_retries} to {url} failed: {exc}"
                    )
                    if attempt < self.callback_retries:
                        await asyncio.sleep(delay)
                        delay *= 2
        logger.error(
            f"Job {job['id']} callback to {url} gave up "
            f"after {self.callback_retries} attempts"
        )

    async def close(self) -> None:
        pending = [*self._tasks, *list(self._callbacks)]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        await self.store.close()