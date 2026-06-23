"""Multi-account storage and failover management for the Gemini service.

Accounts are persisted to a JSON file (managed via the admin UI). The manager
keeps one initialised ``GeminiClient`` per account and, on ``generate``, walks
enabled accounts in priority order, falling through to the next when one errors.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError, GeminiError, UsageLimitExceeded


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
        }


class AccountManager:
    """Owns live GeminiClient instances and implements failover."""

    def __init__(self, store: AccountStore, proxy: str | None, timeout: float) -> None:
        self.store = store
        self.proxy = proxy
        self.timeout = timeout
        self._clients: dict[str, GeminiClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
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
        """

        attempts: list[dict] = []
        enabled = [a for a in self.store.list() if a["enabled"]]
        if not enabled:
            raise NoHealthyAccount(attempts)

        start = self._rotation % len(enabled)
        self._rotation = (start + 1) % len(enabled)
        ordered = enabled[start:] + enabled[:start]

        for account in ordered:
            try:
                client = await self._get_client(account)
                output = await client.generate_content(prompt, model=model)
                self.store.set_status(account["id"], "ok")
                return output, account
            except (AuthError, UsageLimitExceeded, GeminiError, Exception) as exc:
                self.store.set_status(account["id"], "error", str(exc))
                await self.invalidate(account["id"])
                attempts.append({"label": account["label"], "error": str(exc)})
        raise NoHealthyAccount(attempts)

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
