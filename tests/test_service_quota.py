import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from service.accounts import AccountManager, AccountStore, ManagerOptions, QuotaLimited
from service.quota import is_quota_limited

UTC = timezone.utc
LIMIT = "I can create more images as soon as your limit resets."


def _output(text: str = "", images: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(text=text, images=list(images), videos=[], media=[])


class TestQuotaDetection(unittest.TestCase):
    def test_known_limit_messages(self):
        for text in (
            LIMIT,
            "I can create more images as soon as your limit resets. Check your usage in Settings.",
            "Check your usage in Settings.",
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
        self.assertFalse(is_quota_limited(_output(LIMIT, images=("img",))))


class _FakeClient:
    def __init__(self, outputs: list):
        self.outputs = outputs
        self.calls = 0

        self.chats: list = []

    def start_chat(self):
        chat = SimpleNamespace(geminiclient=self)
        self.chats.append(chat)
        return chat

    async def generate_content(self, prompt, model=None, chat=None):
        self.calls += 1
        self.last_chat = chat
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

    def _assert_locked_for_an_hour(self, account_id: str) -> None:
        until = datetime.fromisoformat(self.store.get(account_id)["quota_lock"]["until"])
        expected = datetime.now(UTC) + timedelta(hours=1)
        self.assertLess(abs((until - expected).total_seconds()), 5)

    async def test_limited_account_is_locked_and_failed_over(self):
        # Any stated reset time is ignored: the lock is always the fixed hour.
        self.clients[self.a["id"]] = _FakeClient([_output("Your limit resets Oct 21 at 2:15 PM")])
        self.clients[self.b["id"]] = _FakeClient([_output("ok", images=("img",))] * 2)

        _, served = await self.manager.generate("draw", "gemini-3-flash")
        self.assertEqual(served["label"], "b")
        self.assertEqual(self.store.get(self.a["id"])["status"], "limited")
        self._assert_locked_for_an_hour(self.a["id"])

        # Next call rotates to "a" first, but it is locked: Gemini is not contacted,
        # whatever the model (the whole account is locked).
        _, served = await self.manager.generate("draw", "gemini-3-pro")
        self.assertEqual(served["label"], "b")
        self.assertEqual(self.clients[self.a["id"]].calls, 1)

    async def test_all_limited_raises_with_earliest_reset(self):
        self.clients[self.a["id"]] = _FakeClient([_output(LIMIT)])
        self.clients[self.b["id"]] = _FakeClient([_output(LIMIT)])

        with self.assertRaises(QuotaLimited) as ctx:
            await self.manager.generate("draw", "gemini-3-flash")
        reset = datetime.fromisoformat(ctx.exception.reset_at)
        self.assertLess(abs((reset - datetime.now(UTC) - timedelta(hours=1)).total_seconds()), 5)
        self.assertIn("Quota limit reached, resets at", str(ctx.exception))

        # While locked, no request reaches Gemini at all.
        with self.assertRaises(QuotaLimited):
            await self.manager.generate("draw", "gemini-3-flash")
        self.assertEqual(self.clients[self.a["id"]].calls + self.clients[self.b["id"]].calls, 2)

    async def test_expired_lock_is_ignored(self):
        self.clients[self.a["id"]] = _FakeClient([_output("ok", images=("img",))])
        self.store.update(self.b["id"], enabled=False)
        self.store.lock_quota(self.a["id"], datetime.now(UTC) - timedelta(seconds=1), "x")

        _, served = await self.manager.generate("draw", "gemini-3-flash")
        self.assertEqual(served["label"], "a")


class TestManagerChatSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = AccountStore(Path(self.tmp.name) / "accounts.json")
        self.account = store.add("a", "psid-a", "")
        self.manager = AccountManager(store, ManagerOptions(chat_session_seconds=900))
        self.client = _FakeClient([_output("ok", images=("img",))] * 3)

        async def fake_get_client(account):
            return self.client

        self.manager._get_client = fake_get_client

    def tearDown(self):
        self.tmp.cleanup()

    async def test_requests_share_one_chat_until_it_ages_out(self):
        await self.manager.generate("one", "gemini-3-flash")
        await self.manager.generate("two", "gemini-3-pro")
        self.assertEqual(len(self.client.chats), 1)
        self.assertIs(self.client.last_chat, self.client.chats[0])

        chat, started = self.manager._chats[self.account["id"]]
        self.manager._chats[self.account["id"]] = (chat, started - 901)
        await self.manager.generate("three", "gemini-3-flash")
        self.assertEqual(len(self.client.chats), 2)
        self.assertIs(self.client.last_chat, self.client.chats[1])


if __name__ == "__main__":
    unittest.main()
