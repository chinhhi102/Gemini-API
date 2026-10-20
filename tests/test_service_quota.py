import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from service.accounts import AccountManager, AccountStore, ManagerOptions, QuotaLimited
from service.quota import is_quota_limited, lock_until, parse_reset_at

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
NO_TIME = "I can create more images as soon as your limit resets."


def _output(text: str = "", images: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(text=text, images=list(images), videos=[], media=[])


class TestQuotaDetection(unittest.TestCase):
    def test_known_limit_messages(self):
        for text in (
            NO_TIME,
            "You've reached your limit for creating images. Your limit resets Oct 21 at 2:15 PM.",
            "I can't create more images for you today.",
            "You've hit your image generation limit.",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_quota_limited(_output(text)))

    def test_not_limit(self):
        signed_out = ("I can search for images, but can't create any for you at the moment. "
                      "You might be signed out or image creation may not be available in your location yet.")
        self.assertFalse(is_quota_limited(_output(signed_out)))
        self.assertFalse(is_quota_limited(_output("Here is your cat.")))
        # Media present means it did generate, whatever the text says.
        self.assertFalse(is_quota_limited(_output(NO_TIME, images=("img",))))


class TestResetParsing(unittest.TestCase):
    def test_absolute_date(self):
        got = parse_reset_at("Your limit resets Oct 21 at 2:15 PM.", NOW, UTC)
        self.assertEqual(got, datetime(2026, 10, 21, 14, 15, tzinfo=UTC))

    def test_absolute_with_year_and_on(self):
        got = parse_reset_at("Limit resets on July 20, 2027 at 6:52 PM", NOW, UTC)
        self.assertEqual(got, datetime(2027, 7, 20, 18, 52, tzinfo=UTC))

    def test_year_rollover(self):
        dec31 = datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
        got = parse_reset_at("Your limit resets Jan 1 at 9 AM", dec31, UTC)
        self.assertEqual(got, datetime(2027, 1, 1, 9, 0, tzinfo=UTC))

    def test_tomorrow(self):
        got = parse_reset_at("Your limit resets tomorrow at 1:45 AM.", NOW, UTC)
        self.assertEqual(got, datetime(2026, 10, 21, 1, 45, tzinfo=UTC))

    def test_today_clock_already_passed_means_tomorrow(self):
        got = parse_reset_at("Your limit resets at 9:00 AM", NOW, UTC)
        self.assertEqual(got, datetime(2026, 10, 21, 9, 0, tzinfo=UTC))

    def test_relative(self):
        got = parse_reset_at("Your limit resets in 3 hours.", NOW, UTC)
        self.assertEqual(got, NOW + timedelta(hours=3))

    def test_timezone_applied(self):
        got = parse_reset_at("Your limit resets Oct 21 at 2:15 PM", NOW, ZoneInfo("Asia/Ho_Chi_Minh"))
        self.assertEqual(got, datetime(2026, 10, 21, 7, 15, tzinfo=UTC))

    def test_no_time_falls_back(self):
        self.assertIsNone(parse_reset_at(NO_TIME, NOW, UTC))
        self.assertEqual(lock_until(NO_TIME, NOW, UTC, 3600), NOW + timedelta(hours=1))


class _FakeClient:
    def __init__(self, outputs: list):
        self.outputs = outputs
        self.calls = 0

    async def generate_content(self, prompt, model=None):
        self.calls += 1
        return self.outputs.pop(0)


class TestManagerQuotaLock(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.tmp.name) / "accounts.json")
        self.a = self.store.add("a", "psid-a", "")
        self.b = self.store.add("b", "psid-b", "")
        self.manager = AccountManager(self.store, ManagerOptions(quota_lock_seconds=3600))
        self.clients: dict[str, _FakeClient] = {}

        async def fake_get_client(account):
            return self.clients[account["id"]]

        self.manager._get_client = fake_get_client

    def tearDown(self):
        self.tmp.cleanup()

    async def test_limited_account_is_locked_and_failed_over(self):
        self.clients[self.a["id"]] = _FakeClient([_output("Your limit resets Oct 21 at 2:15 PM")])
        self.clients[self.b["id"]] = _FakeClient([_output("ok", images=("img",)), _output("ok2", images=("img",))])

        _, served = await self.manager.generate("draw", "gemini-3-flash")
        self.assertEqual(served["label"], "b")
        lock = self.store.get(self.a["id"])["quota_locks"]["gemini-3-flash"]
        self.assertEqual(self.store.get(self.a["id"])["status"], "limited")

        # Next call rotates to "a" first, but it is locked: Gemini is not contacted.
        _, served = await self.manager.generate("draw", "gemini-3-flash")
        self.assertEqual(served["label"], "b")
        self.assertEqual(self.clients[self.a["id"]].calls, 1)
        self.assertTrue(lock["until"] > NOW.isoformat())

    async def test_all_limited_raises_with_earliest_reset(self):
        self.clients[self.a["id"]] = _FakeClient([_output("Your limit resets in 5 hours")])
        self.clients[self.b["id"]] = _FakeClient([_output(NO_TIME)])  # fallback: 1h

        with self.assertRaises(QuotaLimited) as ctx:
            await self.manager.generate("draw", "gemini-3-flash")
        reset = datetime.fromisoformat(ctx.exception.reset_at)
        expected = datetime.now(UTC) + timedelta(hours=1)
        self.assertLess(abs((reset - expected).total_seconds()), 5)
        self.assertIn("Quota limit reached, resets at", str(ctx.exception))

        # While locked, no request reaches Gemini at all.
        with self.assertRaises(QuotaLimited):
            await self.manager.generate("draw", "gemini-3-flash")
        self.assertEqual(self.clients[self.a["id"]].calls + self.clients[self.b["id"]].calls, 2)

    async def test_expired_lock_is_ignored(self):
        self.clients[self.a["id"]] = _FakeClient([_output("ok", images=("img",))])
        self.store.update(self.b["id"], enabled=False)
        past = datetime.now(UTC) - timedelta(seconds=1)
        self.store.lock_quota(self.a["id"], "gemini-3-flash", datetime.now(UTC) + timedelta(hours=1), "x")
        self.store.get(self.a["id"])["quota_locks"]["gemini-3-flash"]["until"] = past.isoformat()

        _, served = await self.manager.generate("draw", "gemini-3-flash")  # expired lock
        self.assertEqual(served["label"], "a")


if __name__ == "__main__":
    unittest.main()
