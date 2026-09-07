from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.handlers import build_chat_config_text
from src.helpers import delete_chat_config, get_chat_config, list_chat_configs, update_chat_config
from src.prompts import (
    BUILTIN_TEMPLATES,
    PromptTemplate,
    find_prompt_template,
    load_prompt_templates,
    resolve_rephrase_prompt,
)

DEFAULT = "Default rephrasing rules for the whole bot."


class PromptTemplateLoadingTests(unittest.TestCase):
    def test_builtin_templates_are_used_when_config_has_none(self) -> None:
        self.assertEqual(load_prompt_templates({}), list(BUILTIN_TEMPLATES))
        self.assertEqual(load_prompt_templates({"prompts": {"rephrase": DEFAULT}}), list(BUILTIN_TEMPLATES))

    def test_explicit_empty_list_disables_templates(self) -> None:
        self.assertEqual(load_prompt_templates({"prompts": {"templates": []}}), [])

    def test_invalid_entries_are_skipped_and_duplicates_dropped(self) -> None:
        config = {
            "prompts": {
                "templates": [
                    {"key": "ok", "name": "Fine", "prompt": "A valid prompt text."},
                    {"key": "ok", "name": "Duplicate", "prompt": "Ignored."},
                    {"key": "", "prompt": "No key."},
                    {"key": "no_prompt", "name": "Empty"},
                    {"key": "with|pipe", "prompt": "Pipes break callback data."},
                    {"key": "x" * 25, "prompt": "Too long for callback data."},
                    "not a mapping",
                    {"key": "nameless", "prompt": "Name falls back to the key."},
                ]
            }
        }
        with self.assertLogs("src.prompts", level="WARNING"):
            templates = load_prompt_templates(config)
        self.assertEqual([t.key for t in templates], ["ok", "nameless"])
        self.assertEqual(templates[0].name, "Fine")
        self.assertEqual(templates[1].name, "nameless")

    def test_placeholder_expands_to_default_prompt(self) -> None:
        template = PromptTemplate(key="s", name="S", prompt="{default_prompt}\n\nAdd a TL;DR.")
        self.assertEqual(template.render(DEFAULT + "\n"), f"{DEFAULT}\n\nAdd a TL;DR.")
        self.assertIsNone(find_prompt_template("missing", [template]))
        self.assertIs(find_prompt_template("s", [template]), template)


class PromptResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = [PromptTemplate(key="summary", name="Clean-up + summary", prompt="{default_prompt} + summary")]

    def test_precedence_custom_over_template_over_default(self) -> None:
        chat = {"rephrase_prompt_in": "A custom incoming prompt", "rephrase_template_in": "summary"}
        resolved = resolve_rephrase_prompt(chat, "in", DEFAULT, self.templates)
        self.assertEqual(
            (resolved.source, resolved.text, resolved.label), ("custom", "A custom incoming prompt", "Custom")
        )

        chat = {"rephrase_prompt_in": "short", "rephrase_template_in": "summary"}
        resolved = resolve_rephrase_prompt(chat, "in", DEFAULT, self.templates)
        self.assertEqual(resolved.source, "template")
        self.assertEqual(resolved.text, f"{DEFAULT} + summary")
        self.assertEqual(resolved.label, "Template: Clean-up + summary")

        resolved = resolve_rephrase_prompt({}, "out", DEFAULT, self.templates)
        self.assertEqual((resolved.source, resolved.text, resolved.label), ("default", DEFAULT, "Default"))

    def test_directions_are_independent_and_unknown_template_falls_back(self) -> None:
        chat = {"rephrase_template_in": "summary", "rephrase_template_out": "gone"}
        self.assertEqual(resolve_rephrase_prompt(chat, "in", DEFAULT, self.templates).source, "template")
        with self.assertLogs("src.prompts", level="WARNING"):
            self.assertEqual(resolve_rephrase_prompt(chat, "out", DEFAULT, self.templates).source, "default")


class ChatStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings_file = directory / "chats.json"
        self.settings_file.write_text(json.dumps({"1": {"rephrasing": 0, "chatname": "Alice"}, "2": {}}))
        self.enterContext(patch("src.helpers.CHATS_FILE", self.settings_file))
        self.enterContext(patch("src.helpers.BOT_CONFIG_FILE", directory / "config.yaml"))

    def test_list_applies_defaults_without_writing(self) -> None:
        before = self.settings_file.read_bytes()
        chats = list_chat_configs()
        self.assertEqual(set(chats), {"1", "2"})
        self.assertEqual(chats["1"]["rephrasing"], 0)
        self.assertEqual(chats["2"]["rephrasing"], 1)
        self.assertEqual(chats["2"]["rephrase_template_in"], "")
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_update_and_delete_persist(self) -> None:
        updated = update_chat_config("1", {"markdown_output": 1, "rephrase_template_out": "summary"})
        self.assertIsNotNone(updated)
        stored = json.loads(self.settings_file.read_text())["1"]
        self.assertEqual((stored["markdown_output"], stored["rephrase_template_out"]), (1, "summary"))
        self.assertEqual(stored["chatname"], "Alice")

        created = update_chat_config("3", {"transcription": 0}, chatname="Group")
        self.assertEqual((created["transcription"], created["chatname"]), (0, "Group"))

        self.assertTrue(delete_chat_config("2"))
        self.assertFalse(delete_chat_config("2"))
        self.assertEqual(set(json.loads(self.settings_file.read_text())), {"1", "3"})

    def test_vox_panel_lists_every_setting_with_its_command(self) -> None:
        update_chat_config("1", {"rephrase_template_in": "summary", "rephrase_prompt_out": "My own <prompt>"})
        text = build_chat_config_text("1", get_chat_config("1"), DEFAULT)
        for command in (
            "/ton",
            "/tin",
            "/tout",
            "/tmd",
            "/rephrase",
            "/delin",
            "/delout",
            "/setprompt_in",
            "/setprompt_out",
        ):
            self.assertIn(command, text)
        self.assertIn("Alice (1)", text)
        self.assertIn("Template: Clean-up + summary", text)
        self.assertIn("Custom", text)
        self.assertNotIn("<prompt>", text)
        self.assertIn("inline text", text)


if __name__ == "__main__":
    unittest.main()
