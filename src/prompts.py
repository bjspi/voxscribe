"""Rephrasing prompt templates and per-chat prompt resolution.

A chat can steer the rephrasing step in three ways, checked in this order:

1. A **custom prompt** typed for the chat (``rephrase_prompt_in`` /
   ``rephrase_prompt_out`` in ``chats.json``, set via ``/setprompt*`` or the
   control bot).
2. A **template** chosen from the configurable list under ``prompts.templates``
   in ``config.yaml`` (``rephrase_template_in`` / ``rephrase_template_out`` hold
   the template key). Templates are resolved at runtime, so editing a template
   in ``config.yaml`` immediately affects every chat using it.
3. The global **default** prompt ``prompts.rephrase``.

Templates may contain the placeholder ``{default_prompt}``, which expands to the
global default prompt. That keeps "default rules + something extra" templates
(for example: add a summary) short and in sync with the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.helpers import ChatConfig, YamlConfig, get_config_value, load_bot_config
from src.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT_PLACEHOLDER = "{default_prompt}"
# Prompts shorter than this are treated as "not set" (matches the legacy rule
# used for /setprompt).
MIN_PROMPT_LENGTH = 10
# Template keys travel inside Telegram callback data (64-byte limit).
MAX_TEMPLATE_KEY_LENGTH = 24

Direction = Literal["in", "out"]
PromptSource = Literal["default", "template", "custom"]


@dataclass(slots=True, frozen=True)
class PromptTemplate:
    """One named rephrasing prompt template from ``config.yaml``."""

    key: str
    name: str
    prompt: str

    def render(self, default_prompt: str) -> str:
        """Expand the ``{default_prompt}`` placeholder against the global default."""
        return self.prompt.replace(DEFAULT_PROMPT_PLACEHOLDER, default_prompt.rstrip()).strip()


@dataclass(slots=True, frozen=True)
class ResolvedPrompt:
    """The rephrasing prompt chosen for one chat and direction."""

    text: str
    source: PromptSource
    template: PromptTemplate | None = None

    @property
    def label(self) -> str:
        """Short human label: ``Default``, ``Template: <name>`` or ``Custom``."""
        if self.source == "template" and self.template is not None:
            return f"Template: {self.template.name}"
        return self.source.capitalize()


# Shipped templates, used when ``prompts.templates`` is absent from config.yaml.
# Users can replace the whole list in config.yaml; the placeholder keeps the
# "default rules plus ..." variants in sync with their tuned default prompt.
BUILTIN_TEMPLATES: tuple[PromptTemplate, ...] = (
    PromptTemplate(
        key="summary",
        name="Clean-up + summary",
        prompt=(
            f"{DEFAULT_PROMPT_PLACEHOLDER}\n\n"
            "**Additional rule — summary**\n"
            "Start your answer with a one- or two-sentence summary of the message, "
            'prefixed with "TL;DR:", followed by a blank line and then the revised text.\n'
            "The summary must be in the same language as the message."
        ),
    ),
    PromptTemplate(
        key="summary_only",
        name="Summary only",
        prompt=(
            "You summarize the transcription of a voice message. Return only a concise summary "
            "in the language of the input: at most five short bullet points covering the key "
            "statements, questions and requested actions. Keep names, numbers, dates and "
            "decisions exactly as spoken. Do not add opinions, interpretations or a preface."
        ),
    ),
    PromptTemplate(
        key="bullets",
        name="Bullet points",
        prompt=(
            "You restructure the transcription of a voice message into clear bullet points in "
            "the language of the input. Keep the speaker's wording and tone; only remove filler "
            "words and repetitions. Group related statements, put questions and to-dos in their "
            "own bullets, and keep all names, numbers and dates unchanged. Return only the "
            "bullet list, without any preface or closing remarks."
        ),
    ),
    PromptTemplate(
        key="verbatim",
        name="Verbatim (minimal edits)",
        prompt=(
            "You lightly clean up the transcription of a voice message. Only fix punctuation and "
            'capitalization and remove obvious filler words such as "uh" or "um". Do not '
            "reorder, merge, shorten or rephrase anything else; keep the wording exactly as "
            "spoken, in the original language. Return only the cleaned text."
        ),
    ),
)


def _parse_template(raw: object, index: int) -> PromptTemplate | None:
    """Turn one ``prompts.templates`` entry into a template, or None if invalid."""
    if not isinstance(raw, dict):
        logger.warning("Ignoring prompt template #%s: expected a mapping with key/name/prompt", index + 1)
        return None
    key = str(raw.get("key") or "").strip()
    name = str(raw.get("name") or key).strip()
    prompt = str(raw.get("prompt") or "").strip()
    if not key or not prompt:
        logger.warning("Ignoring prompt template #%s: 'key' and 'prompt' are required", index + 1)
        return None
    if "|" in key or len(key) > MAX_TEMPLATE_KEY_LENGTH:
        logger.warning(
            "Ignoring prompt template %r: keys must be at most %s characters and must not contain '|'",
            key,
            MAX_TEMPLATE_KEY_LENGTH,
        )
        return None
    return PromptTemplate(key=key, name=name, prompt=prompt)


def load_prompt_templates(config: YamlConfig | None = None) -> list[PromptTemplate]:
    """Return the configured prompt templates (or the built-in set when unset).

    Args:
        config: Parsed ``config.yaml``; loaded from disk when omitted.

    Returns:
        Templates in configuration order, with duplicate keys dropped. An
        explicit empty list in ``config.yaml`` disables templates entirely.
    """
    if config is None:
        config = load_bot_config()
    raw_templates = get_config_value(config, ["prompts", "templates"], default=None)
    if raw_templates is None:
        return list(BUILTIN_TEMPLATES)
    if not isinstance(raw_templates, list):
        logger.warning("prompts.templates must be a list; using the built-in templates")
        return list(BUILTIN_TEMPLATES)

    templates: list[PromptTemplate] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_templates):
        template = _parse_template(raw, index)
        if template is None:
            continue
        if template.key in seen:
            logger.warning("Ignoring duplicate prompt template key %r", template.key)
            continue
        seen.add(template.key)
        templates.append(template)
    return templates


def find_prompt_template(key: str, templates: list[PromptTemplate] | None = None) -> PromptTemplate | None:
    """Look up a template by key, or return None when it is unknown."""
    if not key:
        return None
    if templates is None:
        templates = load_prompt_templates()
    return next((template for template in templates if template.key == key), None)


def resolve_rephrase_prompt(
    chat_config: ChatConfig,
    direction: Direction,
    default_prompt: str,
    templates: list[PromptTemplate] | None = None,
) -> ResolvedPrompt:
    """Pick the rephrasing prompt for a chat and direction.

    Args:
        chat_config: The chat's settings (defaults applied or not).
        direction: ``"in"`` for incoming voices, ``"out"`` for outgoing ones.
        default_prompt: The global ``prompts.rephrase`` text.
        templates: Loaded templates; loaded from ``config.yaml`` when omitted.

    Returns:
        The prompt text plus where it came from (custom, template or default).
    """
    custom = str(chat_config.get(f"rephrase_prompt_{direction}") or "").strip()
    if len(custom) >= MIN_PROMPT_LENGTH:
        return ResolvedPrompt(text=custom, source="custom")

    template_key = str(chat_config.get(f"rephrase_template_{direction}") or "").strip()
    if template_key:
        template = find_prompt_template(template_key, templates)
        if template is not None:
            rendered = template.render(default_prompt)
            if len(rendered) >= MIN_PROMPT_LENGTH:
                return ResolvedPrompt(text=rendered, source="template", template=template)
        logger.warning(
            "Prompt template %r configured for a chat is unknown or empty; falling back to the default prompt",
            template_key,
        )

    return ResolvedPrompt(text=default_prompt.strip(), source="default")
