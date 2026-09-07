from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from src.control_bot.keyboards import chat_detail_keyboard, prompt_picker_keyboard
from src.control_bot.router import ControlRouter
from src.control_bot.service import create_control_bot, load_control_bot_settings
from src.control_bot.views import render_overview_text
from src.helpers import get_chat_config
from src.prompts import PromptTemplate

DEFAULT = "Default rephrasing rules for the whole bot."


def buttons(markup) -> dict[str, str]:
    """Map button labels to their callback data."""
    return {button.text: button.callback_data for row in markup.inline_keyboard for button in row}


def callback_data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class FakeUserbot:
    """Just enough of the userbot for chat lookups."""

    def __init__(self) -> None:
        self.chats = {
            -100123: SimpleNamespace(id=-100123, title="Dev Group", first_name=None, last_name=None, username=None),
            555: SimpleNamespace(id=555, title=None, first_name="Alice", last_name="A.", username="alice"),
        }
        self.get_chat = AsyncMock(side_effect=self._get_chat)
        self.dialog_limits: list[int] = []

    async def _get_chat(self, identifier):
        if identifier == "@alice":
            return self.chats[555]
        if identifier in self.chats:
            return self.chats[identifier]
        raise ValueError("unknown chat")

    def get_dialogs(self, limit=0, chat_list=0):
        self.dialog_limits.append(limit)

        async def generator():
            if chat_list == 0:
                # Mixed activity data: one timezone-aware date, one dialog without
                # a top message. Sorting must cope with both.
                dialogs = [
                    SimpleNamespace(chat=self.chats[555], top_message=None),
                    SimpleNamespace(chat=self.chats[-100123], top_message=SimpleNamespace(date=datetime.now(UTC))),
                ]
                for dialog in dialogs:
                    yield dialog

        return generator()


class ControlRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings_file = directory / "chats.json"
        self.settings_file.write_text(
            json.dumps({"1": {"chatname": "Bob", "markdown_output": 1}, "2": {"chatname": "Anna"}, "3": {}})
        )
        config_file = directory / "config.yaml"
        config_file.write_text(
            yaml.safe_dump(
                {
                    "prompts": {"rephrase": DEFAULT},
                    "control_bot": {"token": "123:abc", "owner_id": "42"},
                    "transcription_enabled_new_chats": False,
                }
            )
        )
        self.enterContext(patch("src.helpers.CHATS_FILE", self.settings_file))
        self.enterContext(patch("src.helpers.BOT_CONFIG_FILE", config_file))
        self.enterContext(patch("src.control_bot.router.logger"))
        self.userbot = FakeUserbot()
        self.router = ControlRouter(self.userbot)
        self.owner = 42

    async def route(self, data: str):
        return await self.router.route(data, self.owner)

    def stored(self, chat_id: str) -> dict:
        return json.loads(self.settings_file.read_text())[chat_id]

    async def test_menu_status_and_sorted_chat_list(self) -> None:
        text, markup = await self.route("home")
        self.assertIn("Control", text)
        self.assertEqual(buttons(markup)["💬 Chats"], "cl|0")

        text, markup = await self.route("ov")
        self.assertIn("3 active · 0 paused (3 stored)", text)
        self.assertIn("New chats transcribe by default: off", text)

        text, markup = await self.route("cl|0")
        self.assertIn("(3)", text)
        chat_buttons = [data for data in callback_data(markup) if data.startswith("c|")]
        self.assertEqual(chat_buttons, ["c|2", "c|1", "c|3"])
        self.assertIsNone(await self.route("nop"))

    async def test_missing_chat_name_is_resolved_through_the_userbot(self) -> None:
        self.settings_file.write_text(json.dumps({"555": {}}))
        text, markup = await self.route("c|555")
        self.assertIn("Alice A.", text)
        self.assertEqual(self.stored("555")["chatname"], "Alice A.")
        self.userbot.get_chat.assert_awaited_once_with(555)

    async def test_settings_buttons_persist_each_setting(self) -> None:
        text, markup = await self.route("t|1")
        self.assertEqual(self.stored("1")["transcription"], 0)
        self.assertIn("paused", text)
        self.assertEqual(buttons(markup)["▶️ Resume chat"], "t|1")
        await self.route("t|1")
        self.assertEqual(self.stored("1")["transcription"], 1)

        await self.route("tdir|1|in")
        self.assertEqual((self.stored("1")["transcription_in"], self.stored("1")["transcription_out"]), (1, 0))
        text, markup = await self.route("tdir|1|both")
        self.assertEqual((self.stored("1")["transcription_in"], self.stored("1")["transcription_out"]), (1, 1))
        self.assertIn("✅ Both", buttons(markup))

        await self.route("tmd|1|0")
        self.assertEqual(self.stored("1")["markdown_output"], 0)
        await self.route("rephrase|1")
        self.assertEqual(self.stored("1")["rephrasing"], 0)
        await self.route("del|1|out")
        self.assertEqual(self.stored("1")["delete_outgoing_voice"], 1)
        text, markup = await self.route("del|1|in")
        self.assertEqual(self.stored("1")["delete_incoming_voice"], 1)
        self.assertIn("incoming on · outgoing on", text)

        # Settings of other chats are untouched.
        self.assertEqual(self.stored("2"), {"chatname": "Anna"})

    async def test_prompt_templates_default_and_custom_text(self) -> None:
        text, markup = await self.route("pt|1|both|summary")
        self.assertEqual(self.stored("1")["rephrase_template_in"], "summary")
        self.assertEqual(self.stored("1")["rephrase_template_out"], "summary")
        self.assertIn("Incoming — Template: Clean-up + summary", text)
        self.assertEqual(buttons(markup)["📥 Incoming: Template: Clean-up + summary"], "pp|1|in")

        text, markup = await self.route("pp|1|in")
        self.assertEqual(buttons(markup)["✅ Clean-up + summary"], "pt|1|in|summary")
        await self.route("pd|1|in")
        self.assertEqual(self.stored("1")["rephrase_template_in"], "")
        self.assertEqual(self.stored("1")["rephrase_template_out"], "summary")

        # Unknown template keys are ignored.
        await self.route("pt|1|out|nope")
        self.assertEqual(self.stored("1")["rephrase_template_out"], "summary")

        text, markup = await self.route("pc|1|out")
        self.assertIn("Send the custom rephrasing prompt", text)
        self.assertTrue(str(await self.router.handle_text(self.owner, "short")).startswith("⚠️"))
        screen = await self.router.handle_text(self.owner, "Rewrite everything as a haiku.")
        self.assertIsInstance(screen, tuple)
        self.assertEqual(self.stored("1")["rephrase_prompt_out"], "Rewrite everything as a haiku.")
        self.assertEqual(self.stored("1")["rephrase_template_out"], "")
        self.assertNotIn(self.owner, self.router.pending)

        text, markup = await self.route("pv|1|out")
        self.assertIn("Outgoing — Custom", text)
        self.assertIn("Rewrite everything as a haiku.", text)
        self.assertIsNone(await self.router.handle_text(self.owner, "stray text"))

    async def test_navigation_cancels_a_pending_flow(self) -> None:
        await self.route("pc|1|in")
        self.assertIn(self.owner, self.router.pending)
        await self.route("home")
        self.assertNotIn(self.owner, self.router.pending)
        self.assertIsNone(await self.router.handle_text(self.owner, "A prompt that is long enough."))
        self.assertEqual(self.stored("1").get("rephrase_prompt_in", ""), "")

    async def test_add_chat_by_pick_and_by_typing(self) -> None:
        text, markup = await self.route("cadd")
        # Newest activity first; dialogs without a top message go last; lookups are bounded.
        self.assertEqual(buttons(markup)["1. Dev Group"], "pick|0")
        self.assertEqual(buttons(markup)["2. Alice A."], "pick|1")
        self.assertTrue(all(limit > 0 for limit in self.userbot.dialog_limits))
        self.assertIsNone(await self.route("pick|-1"))
        self.assertIsNone(await self.route("pick|7"))
        text, markup = await self.route("pick|0")
        self.assertIn("Dev Group", text)
        stored = self.stored("-100123")
        self.assertEqual(stored["chatname"], "Dev Group")
        # transcription_enabled_new_chats: false applies to chats added here too.
        self.assertEqual(stored["transcription"], 0)

        await self.route("cadd")
        text, markup = await self.route("typ")
        self.assertIn("@username", text)
        self.assertTrue((await self.router.handle_text(self.owner, "nobody")).startswith("⚠️"))
        screen = await self.router.handle_text(self.owner, "@alice")
        self.assertIn("Alice A.", screen[0])
        self.assertEqual(self.stored("555")["chatname"], "Alice A.")

        # Picking without a pending flow is ignored.
        self.assertIsNone(await self.route("pick|0"))

    async def test_delete_needs_confirmation(self) -> None:
        text, markup = await self.route("cdel|2")
        self.assertIn("Delete", text)
        self.assertEqual(buttons(markup)["🗑 Yes, delete"], "cdel2|2")
        self.assertIn("2", json.loads(self.settings_file.read_text()))
        text, markup = await self.route("cdel2|2")
        self.assertNotIn("2", json.loads(self.settings_file.read_text()))
        self.assertIn("(2)", text)
        # Unknown chats fall back to the list.
        text, markup = await self.route("c|999")
        self.assertIn("Chats", text)

    async def test_templates_screens_render_expanded_placeholder(self) -> None:
        text, markup = await self.route("tpl")
        self.assertEqual(buttons(markup)["Clean-up + summary"], "tpl|summary")
        text, markup = await self.route("tpl|summary")
        self.assertIn(DEFAULT, text)
        self.assertIn("TL;DR", text)

    def test_callback_data_stays_within_telegram_limit(self) -> None:
        chat_id = "-1001234567890123"
        templates = [PromptTemplate(key="k" * 24, name="Long key", prompt="Long enough prompt.")]
        chat_config = get_chat_config(chat_id)
        for markup in (
            chat_detail_keyboard(chat_id, chat_config),
            prompt_picker_keyboard(chat_id, "both", chat_config, templates),
        ):
            for data in callback_data(markup):
                self.assertLessEqual(len(data.encode("utf-8")), 64, data)

    def test_control_bot_settings_are_read_from_config(self) -> None:
        self.assertEqual(load_control_bot_settings(), ("123:abc", 42))

    def test_session_file_is_keyed_by_bot_id(self) -> None:
        with patch("src.control_bot.service.Client") as client_cls, patch("src.control_bot.service.register_control_handlers"):
            create_control_bot(None, 1, "hash", Path("sessions"))
        self.assertEqual(client_cls.call_args.args[0], str(Path("sessions") / "control_bot_123"))
        self.assertEqual(client_cls.call_args.kwargs["bot_token"], "123:abc")

    def test_long_names_and_many_templates_stay_within_limits(self) -> None:
        many = [PromptTemplate(key=f"t{i}", name="n" * 300, prompt="Long enough prompt.") for i in range(80)]
        chat_config = get_chat_config("1")
        markup = prompt_picker_keyboard("1", "in", chat_config, many)
        self.assertLessEqual(sum(len(row) for row in markup.inline_keyboard), 100)
        self.assertIn("…", markup.inline_keyboard[1][0].text)
        chats = [(str(i), {"chatname": "x" * 200}) for i in range(60)]
        text = render_overview_text(chats, {}, many, True)
        self.assertLessEqual(len(text), 4096)


if __name__ == "__main__":
    unittest.main()
