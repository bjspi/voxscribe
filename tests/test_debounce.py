from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram import raw

from src import debounce
from src.debounce import DebounceCollector, recording_status_from_update
from src.helpers import debounce_scope
from src.transcription import (
    VoiceTranscript,
    _deliver_transcripts,
    _split_for_telegram,
    get_current_config,
    note_chat_message,
    transcribe_voice,
)

KEY = (100, 7)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.flushed: list[list[str]] = []

        async def on_flush(items: list[str]) -> None:
            self.flushed.append(items)

        self.collector: DebounceCollector[str] = DebounceCollector(on_flush)
        self.enterContext(patch.object(debounce, "POLL_INTERVAL_SECONDS", 0.01))
        self.enterContext(patch.object(debounce, "RECORDING_STATUS_TTL_SECONDS", 0.15))

    async def test_voices_within_quiet_window_flush_together(self) -> None:
        self.collector.voice_started(KEY, 0.1, 10)
        self.collector.voice_finished(KEY, "one")
        await asyncio.sleep(0.05)
        self.collector.voice_started(KEY, 0.1, 10)
        self.collector.voice_finished(KEY, "two")
        await asyncio.sleep(0.3)
        self.assertEqual(self.flushed, [["one", "two"]])

    async def test_recording_status_holds_the_batch(self) -> None:
        self.collector.voice_started(KEY, 0.05, 10)
        self.collector.voice_finished(KEY, "one")
        self.collector.note_recording(KEY, True)
        await asyncio.sleep(0.1)
        self.assertEqual(self.flushed, [])
        self.collector.note_recording(KEY, False)
        await asyncio.sleep(0.15)
        self.assertEqual(self.flushed, [["one"]])

    async def test_pending_transcription_blocks_flush(self) -> None:
        self.collector.voice_started(KEY, 0.02, 10)
        self.collector.voice_started(KEY, 0.02, 10)
        self.collector.voice_finished(KEY, "one")
        await asyncio.sleep(0.1)
        self.assertEqual(self.flushed, [])
        self.collector.voice_finished(KEY, "two")
        await asyncio.sleep(0.1)
        self.assertEqual(self.flushed, [["one", "two"]])

    async def test_max_wait_flushes_despite_recording(self) -> None:
        self.collector.voice_started(KEY, 5, 0.1)
        self.collector.voice_finished(KEY, "one")
        self.collector.note_recording(KEY, True)
        await asyncio.sleep(0.2)
        self.assertEqual(self.flushed, [["one"]])

    async def test_senders_are_independent_and_failed_voices_are_dropped(self) -> None:
        other = (100, 8)
        self.collector.voice_started(KEY, 0.03, 10)
        self.collector.voice_started(other, 0.03, 10)
        self.collector.voice_finished(KEY, "mine")
        self.collector.voice_finished(other, None)
        await asyncio.sleep(0.15)
        self.assertEqual(self.flushed, [["mine"]])

    async def test_recording_without_open_batch_is_ignored(self) -> None:
        self.collector.note_recording(KEY, True)
        self.collector.voice_started(KEY, 0.03, 10)
        self.collector.voice_finished(KEY, "one")
        await asyncio.sleep(0.1)
        self.assertEqual(self.flushed, [["one"]])


class RecordingStatusTests(unittest.TestCase):
    def test_private_group_and_channel_updates(self) -> None:
        record = raw.types.SendMessageRecordAudioAction()
        user = raw.types.PeerUser(user_id=7)
        self.assertEqual(
            recording_status_from_update(raw.types.UpdateUserTyping(user_id=7, action=record)), ((7, 7), True)
        )
        self.assertEqual(
            recording_status_from_update(raw.types.UpdateChatUserTyping(chat_id=55, from_id=user, action=record)),
            ((-55, 7), True),
        )
        cancel = raw.types.SendMessageCancelAction()
        self.assertEqual(
            recording_status_from_update(raw.types.UpdateChannelUserTyping(channel_id=55, from_id=user, action=cancel)),
            ((-1000000000055, 7), False),
        )

    def test_text_typing_and_other_updates_are_ignored(self) -> None:
        typing = raw.types.SendMessageTypingAction()
        self.assertIsNone(recording_status_from_update(raw.types.UpdateUserTyping(user_id=7, action=typing)))
        self.assertIsNone(recording_status_from_update(object()))


class SplitTests(unittest.TestCase):
    def test_paragraphs_survive_and_long_text_is_packed(self) -> None:
        self.assertEqual(_split_for_telegram("A.\n\nB.\n- x\n- y"), ["A.\n\nB.\n- x\n- y"])
        text = "\n\n".join(["word " * 100] * 10)
        parts = _split_for_telegram(text, limit=1200)
        self.assertTrue(all(len(part) <= 1200 for part in parts))
        self.assertEqual("\n\n".join(parts), text)

    def test_oversized_paragraph_is_wrapped(self) -> None:
        parts = _split_for_telegram("word " * 500, limit=1000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 1000 for part in parts))


class CustomProviderConfigTests(unittest.TestCase):
    def _runtime(self, provider: str, custom: object) -> dict:
        config = {"api": {"provider": {"rephrase": provider}, "custom_providers": custom}}
        with patch("src.transcription.load_bot_config", return_value=config):
            return dict(get_current_config())

    def test_named_provider_is_selected_case_insensitively(self) -> None:
        entry = {"base_url": "https://x/v1", "api_key": "k", "model": "m1", "reasoning_effort": "low"}
        runtime = self._runtime("Gemini", {"gemini": entry})
        self.assertEqual(runtime["rephrase_provider"], "gemini")
        self.assertEqual(runtime["rephrase_model"], "m1")

    def test_incomplete_or_reserved_entries_fall_back_to_groq(self) -> None:
        runtime = self._runtime("gemini", {"gemini": {"base_url": "https://x/v1"}, "groq": {}})
        self.assertEqual(runtime["custom_providers"], {})
        self.assertEqual(runtime["rephrase_provider"], "GROQ")


class BatchDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _voice(self, message_id: int, duration: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            date=datetime(2026, 9, 7, 8, 43),
            chat=SimpleNamespace(id=1, first_name="Chat partner"),
            from_user=SimpleNamespace(username="joscha", id=7),
            outgoing=False,
            voice=SimpleNamespace(duration=duration),
            delete=AsyncMock(),
            reply_text=AsyncMock(),
        )

    async def test_collected_voices_are_rephrased_together_and_answered_once(self) -> None:
        with patch("src.transcription.load_bot_config", return_value={}):
            runtime = get_current_config()
        chat_config = {"rephrasing": 1, "delete_incoming_voice": 1}
        first, second = self._voice(10, 30), self._voice(11, 45)
        with ExitStack() as stack:
            stack.enter_context(patch("src.transcription.get_current_config", return_value=runtime))
            stack.enter_context(patch("src.transcription.get_chat_config", return_value=chat_config))
            stack.enter_context(patch("src.transcription.logger"))
            rephrase = stack.enter_context(
                patch("src.transcription._rephrase_with_provider", return_value=("Absatz 1.\n\nAbsatz 2.", "G", "m"))
            )
            await _deliver_transcripts(
                [VoiceTranscript(second, "zwei", ""), VoiceTranscript(first, "eins", "")]
            )

        self.assertEqual(rephrase.call_args.args[-1], "eins\n\nzwei")
        first.reply_text.assert_not_awaited()
        second.reply_text.assert_awaited_once()
        reply = second.reply_text.call_args.args[0]
        self.assertIn("joscha (2 voices, 1:15)", reply)
        self.assertIn("Absatz 1.\n\nAbsatz 2.", reply)
        first.delete.assert_awaited_once()
        second.delete.assert_awaited_once()


class HoldUntilTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.flushed: list[list[str]] = []

        async def on_flush(items: list[str]) -> None:
            self.flushed.append(items)

        self.collector: DebounceCollector[str] = DebounceCollector(on_flush)
        self.enterContext(patch.object(debounce, "POLL_INTERVAL_SECONDS", 0.01))

    async def test_own_voices_wait_for_a_flush_request(self) -> None:
        self.collector.voice_started(KEY, None, 10)
        self.collector.voice_finished(KEY, "one")
        await asyncio.sleep(0.1)
        self.collector.voice_started(KEY, None, 10)
        self.collector.voice_finished(KEY, "two")
        await asyncio.sleep(0.1)
        self.assertEqual(self.flushed, [])
        self.assertEqual(self.collector.request_flush_chat(KEY[0]), 1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.flushed, [["one", "two"]])

    async def test_flush_request_waits_for_pending_transcription(self) -> None:
        self.collector.voice_started(KEY, None, 10)
        self.assertTrue(self.collector.request_flush(KEY))
        await asyncio.sleep(0.05)
        self.assertEqual(self.flushed, [])
        self.collector.voice_finished(KEY, "one")
        await asyncio.sleep(0.05)
        self.assertEqual(self.flushed, [["one"]])

    async def test_max_wait_releases_held_voices(self) -> None:
        self.collector.voice_started(KEY, None, 0.05)
        self.collector.voice_finished(KEY, "one")
        await asyncio.sleep(0.15)
        self.assertEqual(self.flushed, [["one"]])

    def test_flush_request_without_batch_is_a_no_op(self) -> None:
        self.assertFalse(self.collector.request_flush(KEY))
        self.assertEqual(self.collector.request_flush_chat(KEY[0]), 0)


class ChatMessageTriggerTests(unittest.TestCase):
    def _message(self, outgoing: bool, text: str = "", voice: object = None) -> SimpleNamespace:
        return SimpleNamespace(outgoing=outgoing, text=text, voice=voice, chat=SimpleNamespace(id=100, title="t"))

    def test_triggers_for_own_text_and_partner_messages_only(self) -> None:
        client = SimpleNamespace(me=SimpleNamespace(id=7))
        with patch("src.transcription._collector") as collector, patch("src.transcription.logger"):
            collector.request_flush.return_value = False
            note_chat_message(client, self._message(True, "ok"))
            note_chat_message(client, self._message(False, voice=object()))
            note_chat_message(client, self._message(True, voice=object()))
            note_chat_message(client, self._message(True, "❌ Transcription failed"))
            note_chat_message(client, self._message(True, "🔇 No speech recognized."))
        self.assertEqual(collector.request_flush.call_count, 2)
        collector.request_flush.assert_called_with((100, 7))


class MaxWaitSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def _transcribe(self, outgoing: bool, chat_config: dict[str, int]) -> MagicMock:
        """Run transcribe_voice for one voice and return the mocked collector."""
        config = {"debounce": {"quiet_seconds": 4, "max_wait_seconds": 60, "own_max_wait_seconds": 900}}
        with patch("src.transcription.load_bot_config", return_value=config):
            runtime = get_current_config()
        message = SimpleNamespace(
            outgoing=outgoing,
            chat=SimpleNamespace(id=1, first_name="x"),
            from_user=SimpleNamespace(id=7),
        )
        with ExitStack() as stack:
            stack.enter_context(patch("src.transcription.get_current_config", return_value=runtime))
            stack.enter_context(patch("src.transcription.get_chat_config", return_value=chat_config))
            stack.enter_context(patch("src.transcription.logger"))
            stack.enter_context(patch("src.transcription._transcribe_message", new=AsyncMock(return_value=None)))
            collector = stack.enter_context(patch("src.transcription._collector"))
            await transcribe_voice(None, message)
        return collector

    async def test_own_and_other_voices_use_their_own_limits(self) -> None:
        both = {"rephrase_debounce": 1, "rephrase_debounce_out": 1}
        for outgoing, expected in ((True, (None, 900)), (False, (4, 60))):
            collector = await self._transcribe(outgoing, both)
            collector.voice_started.assert_called_once_with((1, 7), *expected)

    async def test_incoming_only_leaves_own_voices_alone(self) -> None:
        incoming_only = {"rephrase_debounce": 1}
        collector = await self._transcribe(True, incoming_only)
        collector.voice_started.assert_not_called()
        collector = await self._transcribe(False, incoming_only)
        collector.voice_started.assert_called_once_with((1, 7), 4, 60)


class DebounceScopeTests(unittest.TestCase):
    def test_on_means_incoming_only_unless_own_voices_are_enabled(self) -> None:
        self.assertEqual(debounce_scope({}), "off")
        self.assertEqual(debounce_scope({"rephrase_debounce_out": 1}), "off")
        self.assertEqual(debounce_scope({"rephrase_debounce": 1}), "in")
        self.assertEqual(debounce_scope({"rephrase_debounce": 1, "rephrase_debounce_out": 1}), "both")
