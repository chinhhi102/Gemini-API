"""Multi-account storage and failover management for the Gemini service.

Accounts are persisted to a JSON file (managed via the admin UI). The manager
keeps one initialised ``GeminiClient`` per account and, on ``generate``, walks
enabled accounts in priority order, falling through to the next when one errors.
"""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError, GeminiError, UsageLimitExceeded

from .quota import is_quota_limited


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mask(value: str) -> str:
    """Mask a secret for display, revealing only the last 4 characters."""

    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) > 4 else "…"


class NoHealthyAccount(Exception):
    """Raised when every enabled account failed to satisfy a request."""

    def __init__(self, attempts: list[dict]) -> None:
        self.attempts = attempts
        super().__init__("All accounts failed")


class QuotaLimited(NoHealthyAccount):
    """Raised when no account could serve and at least one is quota-locked.

    ``reset_at`` (ISO-8601 UTC) is the earliest moment any account's lock lifts.
    """

    def __init__(self, reset_at: str, attempts: list[dict]) -> None:
        super().__init__(attempts)
        self.reset_at = reset_at
        self.args = (f"Quota limit reached, resets at {reset_at}",)


@dataclass(frozen=True)
class ManagerOptions:
    proxy: str | None = None
    timeout: float = 300.0
    quota_lock_seconds: float = 3600.0


class AccountStore:
    """Thread-free JSON persistence for accounts (single-process service)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._accounts: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self._accounts = json.loads(self.path.read_text() or "{}")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._accounts, indent=2))
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        """Accounts sorted by ascending priority (0 = tried first)."""

        return sorted(self._accounts.values(), key=lambda a: a["priority"])

    def get(self, account_id: str) -> dict | None:
        return self._accounts.get(account_id)

    def add(self, label: str, psid: str, psidts: str) -> dict:
        next_priority = 1 + max((a["priority"] for a in self._accounts.values()), default=-1)
        account = {
            "id": uuid.uuid4().hex[:8],
            "label": label,
            "secure_1psid": psid,
            "secure_1psidts": psidts,
            "enabled": True,
            "priority": next_priority,
            "status": "untested",
            "status_detail": "",
            "updated_at": _now(),
        }
        self._accounts[account["id"]] = account
        self._save()
        return account

    def update(self, account_id: str, **fields) -> dict | None:
        account = self._accounts.get(account_id)
        if not account:
            return None
        account.update({k: v for k, v in fields.items() if v is not None})
        account["updated_at"] = _now()
        self._save()
        return account

    def set_status(self, account_id: str, status: str, detail: str = "") -> None:
        account = self._accounts.get(account_id)
        if account:
            account.update(status=status, status_detail=detail, updated_at=_now())
            self._save()

    @staticmethod
    def quota_lock(account: dict, now: datetime) -> str | None:
        """ISO time the account's quota lock lifts, or None if unlocked."""

        lock = account.get("quota_lock")
        if lock and datetime.fromisoformat(lock["until"]) > now:
            return lock["until"]
        return None

    def lock_quota(self, account_id: str, until: datetime, detail: str) -> str:
        """Lock the whole account until ``until`` (no requests sent meanwhile)."""

        until_iso = until.isoformat(timespec="seconds")
        account = self._accounts.get(account_id)
        if not account:  # deleted mid-request; nothing to lock
            return until_iso
        account.update(
            quota_lock={"until": until_iso, "detail": detail[:300]},
            status="limited",
            status_detail=f"Quota limit, locked until {until_iso}",
            updated_at=_now(),
        )
        self._save()
        return until_iso

    def delete(self, account_id: str) -> bool:
        if self._accounts.pop(account_id, None) is None:
            return False
        self._save()
        return True

    @staticmethod
    def public_view(account: dict) -> dict:
        """Account dict safe to return over the API (cookies masked)."""

        return {
            "id": account["id"],
            "label": account["label"],
            "enabled": account["enabled"],
            "priority": account["priority"],
            "status": account["status"],
            "status_detail": account["status_detail"],
            "updated_at": account["updated_at"],
            "secure_1psid": _mask(account["secure_1psid"]),
            "secure_1psidts": _mask(account["secure_1psidts"]),
            "quota_lock": account.get("quota_lock"),
        }


def _exhausted(attempts: list[dict]) -> NoHealthyAccount:
    """Pick the terminal error: quota-limited (with earliest reset) or plain failure."""

    resets = [a["reset_at"] for a in attempts if a.get("reset_at")]
    if resets:
        return QuotaLimited(min(resets, key=datetime.fromisoformat), attempts)
    return NoHealthyAccount(attempts)


class AccountManager:
    """Owns live GeminiClient instances and implements failover."""

    def __init__(self, store: AccountStore, options: ManagerOptions) -> None:
        self.store = store
        self.proxy = options.proxy
        self.timeout = options.timeout
        self.quota_lock_seconds = options.quota_lock_seconds
        self._clients: dict[str, GeminiClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Serialises sends per account: one in-flight Gemini request each.
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._rotation = 0  # round-robin cursor across enabled accounts

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def session_for(self, account_id: str):
        """The live account's authenticated AsyncSession, for media downloads."""

        client = self._clients.get(account_id)
        return client.client if client else None

    async def _get_client(self, account: dict) -> GeminiClient:
        """Return an initialised client for the account, creating it lazily."""

        async with self._lock(account["id"]):
            client = self._clients.get(account["id"])
            if client is not None:
                return client
            client = GeminiClient(
                secure_1psid=account["secure_1psid"],
                secure_1psidts=account["secure_1psidts"],
                proxy=self.proxy,
            )
            await client.init(timeout=self.timeout, auto_refresh=True)
            if client.account_status != AccountStatus.AVAILABLE:
                detail = client.account_status
                await client.close()
                raise AuthError(f"{detail.name}: {detail.description}")
            self._clients[account["id"]] = client
            return client

    async def invalidate(self, account_id: str) -> None:
        """Drop and close the live client (e.g. after a cookie edit or failure)."""

        client = self._clients.pop(account_id, None)
        if client is not None:
            await client.close()

    async def generate(self, prompt: str, model) -> tuple:
        """Round-robin across enabled accounts; fail over to the rest on error.

        Each call starts from the next account in rotation so traffic is spread
        evenly (no single account absorbs every request and burns its usage
        window while the others idle). On error the remaining accounts are still
        tried, so a single call only fails when every enabled account fails.

        Quota-locked accounts are skipped without contacting Gemini. A reply
        that turns out to be a quota-limit refusal locks the whole account for
        ``quota_lock_seconds`` and fails over like any other error.
        """

        attempts: list[dict] = []
        ordered = self._rotation_order()
        now = datetime.now(timezone.utc)
        for account in ordered:
            locked_until = self.store.quota_lock(account, now)
            if locked_until:
                attempts.append({"label": account["label"], "error": "quota limit (locked)",
                                 "reset_at": locked_until})
                continue
            output = await self._try_account(account, prompt, model, attempts)
            if output is not None:
                return output, account
        raise _exhausted(attempts)

    def _rotation_order(self) -> list[dict]:
        enabled = [a for a in self.store.list() if a["enabled"]]
        if not enabled:
            return []
        start = self._rotation % len(enabled)
        self._rotation = (start + 1) % len(enabled)
        return enabled[start:] + enabled[:start]

    async def _try_account(self, account: dict, prompt: str, model, attempts: list[dict]):
        """One generation attempt; returns the output, or None after recording why."""

        try:
            client = await self._get_client(account)
            output = await self._send(account["id"], client, prompt, model)
        except UsageLimitExceeded as exc:
            # Quota, not a broken session: keep the live client.
            self._lock_quota(account, str(exc), attempts)
            return None
        except (AuthError, GeminiError, Exception) as exc:
            self.store.set_status(account["id"], "error", str(exc))
            await self.invalidate(account["id"])
            attempts.append({"label": account["label"], "error": str(exc)})
            return None
        if is_quota_limited(output):
            self._lock_quota(account, output.text, attempts)
            return None
        self.store.set_status(account["id"], "ok")
        return output

    async def _send(self, account_id: str, client: GeminiClient, prompt: str, model):
        """Send the prompt in a brand-new Gemini chat, one request per account at a time.

        Every request gets its own chat on purpose: in a shared chat Gemini reads
        the earlier prompts and redraws their images instead of the new one.
        """

        async with self._send_locks.setdefault(account_id, asyncio.Lock()):
            # Cap the generation itself, not just init: a hung Gemini response
            # would otherwise pin a job worker forever (TimeoutError is caught
            # by the caller, so it just fails over to the next account).
            return await asyncio.wait_for(
                client.generate_content(prompt, model=model), timeout=self.timeout
            )

    def _lock_quota(self, account: dict, message: str, attempts: list[dict]) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=self.quota_lock_seconds)
        until_iso = self.store.lock_quota(account["id"], until, message)
        attempts.append({"label": account["label"], "error": message, "reset_at": until_iso})

    async def test(self, psid: str, psidts: str) -> tuple[bool, str]:
        """Validate cookies by initialising a throwaway client. No quota spent."""

        client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts, proxy=self.proxy)
        try:
            await client.init(timeout=self.timeout, auto_refresh=False)
            status = client.account_status
            if status == AccountStatus.AVAILABLE:
                return True, status.description
            return False, f"{status.name}: {status.description}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            return False, str(exc)
        finally:
            await client.close()

    async def close_all(self) -> None:
        for account_id in list(self._clients):
            await self.invalidate(account_id)
