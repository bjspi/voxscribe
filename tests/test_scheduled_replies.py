from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.helpers import send_and_delete_message


class ScheduledReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_is_deleted_before_reply_and_display_delay(self) -> None:
        for seconds in (5, 3600):
            with self.subTest(seconds=seconds):
                await self._exercise_scheduled_reply(seconds)

    async def _exercise_scheduled_reply(self, seconds: int) -> None:
        events = []
        date = datetime.now(UTC) + timedelta(seconds=seconds)
        message = SimpleNamespace(scheduled=True, date=date, chat=SimpleNamespace(id=1))
        message.delete = AsyncMock(side_effect=lambda: events.append("delete-original") or True)
        reply = SimpleNamespace(delete=AsyncMock(side_effect=lambda: events.append("delete-reply")))

        async def send(**kwargs):
            self.assertEqual(events, ["delete-original"])
            if seconds == 5:
                self.assertGreater(kwargs["schedule_date"], date + timedelta(days=29))
            else:
                self.assertEqual(kwargs["schedule_date"], date)
            events.append("send")
            return reply

        client = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        with patch("src.helpers.asyncio.sleep", new=AsyncMock(side_effect=lambda _: events.append("wait"))):
            await send_and_delete_message(client, message, "test", 30)
        self.assertEqual(events, ["delete-original", "send", "wait", "delete-reply"])

    async def test_failed_original_delete_does_not_create_another_scheduled_message(self) -> None:
        message = SimpleNamespace(scheduled=True, delete=AsyncMock(return_value=False))
        client = SimpleNamespace(send_message=AsyncMock())
        with self.assertLogs("src.helpers", level="ERROR"):
            await send_and_delete_message(client, message, "test", 30)
        client.send_message.assert_not_awaited()

    async def test_cancellation_during_display_still_cleans_up_reply(self) -> None:
        message = SimpleNamespace(scheduled=True, date=None, chat=SimpleNamespace(id=1), delete=AsyncMock())
        reply = SimpleNamespace(delete=AsyncMock())
        client = SimpleNamespace(send_message=AsyncMock(return_value=reply))
        with patch("src.helpers.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await send_and_delete_message(client, message, "test", 30)
        message.delete.assert_awaited_once()
        reply.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
