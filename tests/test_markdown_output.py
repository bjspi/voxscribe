from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.handlers import build_transcription_status_text, toggle_markdown_output
from src.helpers import get_chat_config
from src.transcription import _build_markdown_filename, get_current_config, transcribe_voice


class MarkdownTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = self.enterContext(ExitStack())
        with patch("src.transcription.load_bot_config", return_value={}):
            self.runtime = get_current_config()
        self.chat_config = {"markdown_output": 1, "rephrasing": 0}
        self.original = '"Original: Grüße & <Text>.\n\n' + "Vollständiger Inhalt. " * 600 + '\n\nEnde."'
        self.rephrased = "## Ein Thema\n\nÜberarbeiteter Text.\n\n- Ein Punkt\n- Noch ein Punkt"
        self.message = SimpleNamespace(
            id=42,
            date=datetime(2026, 9, 7, 8, 43, 12),
            chat=SimpleNamespace(id=1, first_name="Chat partner"),
            from_user=SimpleNamespace(username="alex_test", first_name="Alex", last_name="Example", id=2),
            outgoing=False,
            voice=SimpleNamespace(duration=843),
            delete=AsyncMock(),
            reply_text=AsyncMock(),
        )
        self.audio_path = None
        self.upload = None
        self.uploaded_text = None

        async def download(*, file_name):
            self.audio_path = Path(file_name)
            self.audio_path.write_bytes(b"test audio")
            return file_name

        async def reply_document(document, **kwargs):
            self.upload = document
            self.uploaded_text = document.read().decode("utf-8")
            return SimpleNamespace(id=43)

        self.message.download = AsyncMock(side_effect=download)
        self.message.reply_document = AsyncMock(side_effect=reply_document)
        self.stack.enter_context(patch("src.transcription.get_current_config", return_value=self.runtime))
        self.stack.enter_context(patch("src.transcription.get_chat_config", return_value=self.chat_config))
        self.stack.enter_context(patch("src.transcription.logger"))
        self.stack.enter_context(
            patch(
                "src.transcription._run_transcription_with_recovery",
                new=AsyncMock(
                    return_value={
                        "transcript": SimpleNamespace(text=self.original),
                        "provider_name": "GROQ (Transcription)",
                        "model_used": "whisper-large-v3",
                        "fallback_used": False,
                        "provider_used": "GROQ",
                        "provider_attempts": [],
                    }
                ),
            )
        )
        self.rephrase = self.stack.enter_context(
            patch(
                "src.transcription._rephrase_with_provider",
                return_value=(self.rephrased, "GROQ (Rephrasing)", "test-model"),
            )
        )

    async def test_long_voice_sends_both_complete_versions_in_one_file(self) -> None:
        self.chat_config["rephrase_prompt_in"] = "Custom incoming prompt"
        await transcribe_voice(None, self.message)

        self.message.reply_document.assert_awaited_once()
        self.message.reply_text.assert_not_awaited()
        self.message.delete.assert_not_awaited()
        self.rephrase.assert_called_once()
        self.assertEqual(self.rephrase.call_args.args[-2:], ("Custom incoming prompt", self.original))
        self.assertIn(f"## Original transcription\n\n{self.original}\n\n", self.uploaded_text)
        self.assertIn(f"## Rephrased version\n\n{self.rephrased}\n", self.uploaded_text)
        self.assertIn("Duration: 14:03", self.uploaded_text)
        kwargs = self.message.reply_document.call_args.kwargs
        self.assertEqual(kwargs["file_name"], "20260907_08h43m_alex_test_14m03s.md")
        self.assertEqual(kwargs["caption"], "")
        self.assertTrue(kwargs["quote"])
        self.assertTrue(self.upload.closed)
        self.assertFalse(self.audio_path.exists())

    async def test_markdown_off_by_default_keeps_text_output_and_rephrase_setting(self) -> None:
        del self.chat_config["markdown_output"]
        await transcribe_voice(None, self.message)

        self.rephrase.assert_not_called()
        self.message.reply_document.assert_not_awaited()
        self.assertGreater(self.message.reply_text.await_count, 1)
        replies = self.message.reply_text.await_args_list
        self.assertIn("Original: Grüße &amp; &lt;Text&gt;.", replies[0].args[0])
        self.assertIn("Ende.", replies[-1].args[0])
        self.assertFalse(self.audio_path.exists())

    async def test_explicit_markdown_off_still_rephrases_text_when_enabled(self) -> None:
        self.chat_config.update(markdown_output=0, rephrasing=1)
        await transcribe_voice(None, self.message)

        self.rephrase.assert_called_once()
        self.message.reply_document.assert_not_awaited()
        self.message.reply_text.assert_awaited_once()
        self.assertIn("Überarbeiteter Text.", self.message.reply_text.call_args.args[0])

    async def test_rephrase_failure_preserves_original_and_marks_missing_version(self) -> None:
        self.runtime["graceful_degradation_enabled"] = False
        self.rephrase.side_effect = TimeoutError("API request timed out")
        await transcribe_voice(None, self.message)

        self.message.reply_document.assert_awaited_once()
        self.message.reply_text.assert_not_awaited()
        self.assertIn(self.original, self.uploaded_text)
        self.assertIn("## Rephrased version\n\n> Rephrasing unavailable:", self.uploaded_text)
        self.assertNotIn(self.rephrased, self.uploaded_text)
        caption = self.message.reply_document.call_args.kwargs["caption"]
        self.assertTrue(caption.startswith("⚠️ Rephrasing unavailable"))
        self.assertNotIn("📝", caption)

    async def test_outgoing_voice_uses_sender_prompt_and_deletes_only_after_upload(self) -> None:
        self.message.outgoing = True
        self.message.from_user.username = "my_username"
        self.chat_config.update(delete_outgoing_voice=1, rephrase_prompt_out="Custom outgoing prompt")
        events = []
        self.message.reply_document.side_effect = lambda *args, **kwargs: (
            events.append("uploaded") or SimpleNamespace(id=43)
        )
        self.message.delete.side_effect = lambda: events.append("deleted")
        await transcribe_voice(None, self.message)

        self.assertEqual(events, ["uploaded", "deleted"])
        self.assertEqual(self.rephrase.call_args.args[-2], "Custom outgoing prompt")
        kwargs = self.message.reply_document.call_args.kwargs
        self.assertEqual(kwargs["file_name"], "20260907_08h43m_my_username_14m03s.md")
        self.assertFalse(kwargs["quote"])

    async def test_failed_upload_keeps_voice_and_cleans_temporary_files(self) -> None:
        self.chat_config["delete_incoming_voice"] = 1

        async def fail(document, **kwargs):
            self.upload = document
            raise OSError("Upload failed")

        self.message.reply_document.side_effect = fail
        await transcribe_voice(None, self.message)

        self.message.delete.assert_not_awaited()
        self.message.reply_text.assert_awaited_once()
        self.assertIn("Transcription failed", self.message.reply_text.call_args.args[0])
        self.assertTrue(self.upload.closed)
        self.assertFalse(self.audio_path.exists())

    def test_filename_falls_back_to_safe_display_name_or_id(self) -> None:
        self.message.from_user.username = None
        self.message.from_user.first_name = '../Alex: <Test>\\Name\n'
        self.message.voice.duration = 65
        self.assertEqual(_build_markdown_filename(self.message), "20260907_08h43m_Alex_Test_Name_Example_1m05s.md")
        self.message.from_user.first_name = None
        self.message.from_user.last_name = None
        self.assertEqual(_build_markdown_filename(self.message), "20260907_08h43m_2_1m05s.md")

    def test_filename_uses_current_time_when_message_date_is_missing(self) -> None:
        self.message.date = None
        self.message.voice.duration = 65

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 2, 3, 4)

        with patch("src.transcription.datetime", FrozenDatetime):
            self.assertEqual(_build_markdown_filename(self.message), "20260102_03h04m_alex_test_1m05s.md")


class MarkdownSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings_file = directory / "chats.json"
        self.settings_file.write_text(json.dumps({"1": {"rephrasing": 0}, "2": {"transcription": 0}}))
        self.enterContext(patch("src.helpers.CHATS_FILE", self.settings_file))
        self.enterContext(patch("src.helpers.BOT_CONFIG_FILE", directory / "config.yaml"))
        self.reply = self.enterContext(patch("src.handlers.send_and_delete_message", new=AsyncMock()))
        self.message = SimpleNamespace(text="/tmd on", chat=SimpleNamespace(id=1, first_name="Alex"))

    async def test_default_and_persisted_toggle_are_per_chat(self) -> None:
        self.assertEqual(get_chat_config("1")["markdown_output"], 0)
        self.assertEqual(get_chat_config("999")["markdown_output"], 0)
        await toggle_markdown_output(None, self.message)

        self.assertEqual(get_chat_config("1")["markdown_output"], 1)
        self.assertEqual(get_chat_config("1")["rephrasing"], 0)
        self.assertEqual(get_chat_config("2")["markdown_output"], 0)
        self.assertEqual(json.loads(self.settings_file.read_text())["2"], {"transcription": 0})
        self.assertIn("Markdown ✅", build_transcription_status_text(get_chat_config("1")))

        # Explicit on is idempotent; no argument toggles; explicit off persists.
        await toggle_markdown_output(None, self.message)
        self.assertEqual(get_chat_config("1")["markdown_output"], 1)
        self.message.text = "/tmd"
        await toggle_markdown_output(None, self.message)
        self.assertEqual(get_chat_config("1")["markdown_output"], 0)
        await toggle_markdown_output(None, self.message)
        self.assertEqual(get_chat_config("1")["markdown_output"], 1)
        self.message.text = "/tmd off"
        await toggle_markdown_output(None, self.message)
        self.assertEqual(get_chat_config("1")["markdown_output"], 0)

    async def test_invalid_argument_does_not_change_settings(self) -> None:
        before = self.settings_file.read_bytes()
        self.message.text = "/tmd maybe"
        await toggle_markdown_output(None, self.message)

        self.assertEqual(self.settings_file.read_bytes(), before)
        self.assertIn("Usage:", self.reply.call_args.args[2])

    async def test_save_failure_does_not_report_success(self) -> None:
        with patch("src.handlers.save_chat_settings", return_value=False):
            await toggle_markdown_output(None, self.message)
        self.assertEqual(get_chat_config("1")["markdown_output"], 0)
        self.assertIn("Could not save", self.reply.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
