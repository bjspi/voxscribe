"""Text renderers (HTML parse mode) for the control bot's menu screens.

Every dynamic value (chat names, prompt texts) is HTML-escaped: the default
rephrasing prompt contains Markdown markup that would otherwise break rendering.
"""

from __future__ import annotations

from html import escape

from src.control_bot.common import (
    DIRECTION_LABELS,
    chat_label,
    direction_of,
    is_enabled,
    rephrasing_on,
    state_icon,
    uses_markdown,
)
from src.helpers import ChatConfig
from src.prompts import PromptTemplate, ResolvedPrompt

# Telegram caps messages at 4096 characters; leave headroom for headings.
MAX_PROMPT_DISPLAY_LENGTH = 3500
PREVIEW_LENGTH = 140
OVERVIEW_CHAT_LIMIT = 30

MAIN_MENU_TEXT = (
    "🎛 <b>Voice Transcriber — Control</b>\n\n"
    "Pick a section to view or change the live per-chat configuration.\n"
    "Every change applies to the next voice message immediately."
)

ADD_CHAT_TEXT = (
    "➕ <b>Add chat</b>\n\n"
    "Pick a recently active chat, or type its id / @username. The chat is created "
    "with the default settings and can be adjusted right away."
)
TYPE_CHAT_TEXT = "✍️ Send me the chat <b>id</b> (e.g. <code>-1001234567890</code>) or a <b>@username</b>."


def _preview(text: str, limit: int = PREVIEW_LENGTH) -> str:
    """Return a single-line, escaped preview of a prompt."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return escape(flat)


def _clip(text: str, limit: int = MAX_PROMPT_DISPLAY_LENGTH) -> str:
    """Escape a long text and cut it to fit into one Telegram message."""
    if len(text) > limit:
        return escape(text[:limit].rstrip()) + "\n\n… (truncated)"
    return escape(text)


def _heading(chat_id: str, chat_config: ChatConfig, icon: str = "💬") -> str:
    """Return the bold chat name plus its id in code style."""
    return f"{icon} <b>{escape(chat_label(chat_id, chat_config))}</b> <code>{escape(chat_id)}</code>"


def _direction_word(direction: str) -> str:
    """Return a readable direction label."""
    return DIRECTION_LABELS.get(direction, direction)


def _yes_no(value: object) -> str:
    return "on" if value else "off"


def _summary_chips(chat_config: ChatConfig) -> str:
    """Return the compact one-line summary used in lists."""
    chips = [direction_of(chat_config), "📄 md" if uses_markdown(chat_config) else "💬 text"]
    if rephrasing_on(chat_config) or uses_markdown(chat_config):
        chips.append("🧠")
    if chat_config.get("delete_incoming_voice") or chat_config.get("delete_outgoing_voice"):
        chips.append("🗑")
    return " · ".join(chips)


def render_overview_text(
    chats: list[tuple[str, ChatConfig]],
    runtime: dict[str, object],
    templates: list[PromptTemplate],
    new_chats_enabled: bool,
) -> str:
    """Build the status screen: providers, counts and a compact chat summary."""
    active = sum(1 for _, chat_config in chats if is_enabled(chat_config))
    paused = len(chats) - active
    transcription_provider = str(runtime.get("transcription_provider", "?"))
    rephrase_provider = str(runtime.get("rephrase_provider", "?"))
    transcription_model = str(runtime.get(f"transcription_model_{transcription_provider.lower()}", "?"))
    rephrase_model = str(runtime.get(f"rephrase_model_{rephrase_provider.lower()}", "?"))

    lines = [
        "📊 <b>Voice Transcriber — Status</b>",
        "",
        f"🎙 Transcription: {escape(transcription_provider)} · <code>{escape(transcription_model)}</code>",
        f"🧠 Rephrasing: {escape(rephrase_provider)} · <code>{escape(rephrase_model)}</code>",
        f"🧩 Prompt templates: {len(templates)}",
        f"💬 Chats: {active} active · {paused} paused ({len(chats)} stored)",
        f"🆕 New chats transcribe by default: {_yes_no(new_chats_enabled)}",
    ]
    if chats:
        lines += ["", "<b>Chats</b>"]
        for chat_id, chat_config in chats[:OVERVIEW_CHAT_LIMIT]:
            lines.append(
                f"{state_icon(is_enabled(chat_config))} {escape(chat_label(chat_id, chat_config))} "
                f"· {_summary_chips(chat_config)}"
            )
        if len(chats) > OVERVIEW_CHAT_LIMIT:
            lines.append(f"… and {len(chats) - OVERVIEW_CHAT_LIMIT} more")
    return "\n".join(lines)


def render_chats_text(count: int, page: int, pages: int) -> str:
    """Build the chat-list screen header."""
    if not count:
        return "💬 <b>Chats</b>\n\nNo chats configured yet. Add one, or just send a voice in any chat."
    page_note = f" · page {page + 1}/{pages}" if pages > 1 else ""
    return (
        f"💬 <b>Chats</b> ({count}{page_note})\n\n"
        "Tap a chat to change its settings. ▶️ = active, ⏸ = paused; "
        "the chips show direction and output mode."
    )


def render_chat_detail_text(
    chat_id: str,
    chat_config: ChatConfig,
    prompt_in: ResolvedPrompt,
    prompt_out: ResolvedPrompt,
) -> str:
    """Build the per-chat settings screen with every setting spelled out."""
    enabled = is_enabled(chat_config)
    output = "📄 Markdown file (original + rephrased)" if uses_markdown(chat_config) else "💬 inline text"
    rephrasing = _yes_no(rephrasing_on(chat_config))
    if uses_markdown(chat_config):
        rephrasing += " (Markdown files always include both versions)"
    return (
        f"{_heading(chat_id, chat_config)}\n\n"
        f"<b>State:</b> {state_icon(enabled)} {'active' if enabled else 'paused'}\n"
        f"<b>Direction:</b> {_direction_word(direction_of(chat_config))}\n"
        f"<b>Output:</b> {output}\n"
        f"<b>Rephrasing:</b> {rephrasing}\n"
        f"<b>Delete voice:</b> incoming {_yes_no(chat_config.get('delete_incoming_voice'))} · "
        f"outgoing {_yes_no(chat_config.get('delete_outgoing_voice'))}\n"
        f"<b>Prompt in:</b> {escape(prompt_in.label)}\n"
        f"<b>Prompt out:</b> {escape(prompt_out.label)}"
    )


def render_prompts_text(
    chat_id: str,
    chat_config: ChatConfig,
    prompt_in: ResolvedPrompt,
    prompt_out: ResolvedPrompt,
) -> str:
    """Build the per-chat prompt screen with a preview per direction."""
    return (
        f"{_heading(chat_id, chat_config, '🧩')}\n\n"
        f"📥 <b>Incoming — {escape(prompt_in.label)}</b>\n<i>{_preview(prompt_in.text)}</i>\n\n"
        f"📤 <b>Outgoing — {escape(prompt_out.label)}</b>\n<i>{_preview(prompt_out.text)}</i>\n\n"
        "Pick a direction to choose a template, the global default, or type your own prompt."
    )


def render_prompt_picker_text(chat_id: str, chat_config: ChatConfig, direction: str) -> str:
    """Build the template picker header for one direction."""
    target = {"in": "incoming", "out": "outgoing", "both": "incoming and outgoing"}.get(direction, direction)
    return (
        f"{_heading(chat_id, chat_config, '🧩')}\n\n"
        f"Choose the rephrasing prompt for <b>{target}</b> voices.\n"
        "Templates live in <code>config.yaml</code> under <code>prompts.templates</code> "
        "and are applied live."
    )


def render_prompt_view_text(chat_id: str, chat_config: ChatConfig, direction: str, prompt: ResolvedPrompt) -> str:
    """Build the full-text view of the prompt active for one direction."""
    target = "incoming" if direction == "in" else "outgoing"
    return (
        f"{_heading(chat_id, chat_config, '👁')}\n\n"
        f"<b>{target.capitalize()} — {escape(prompt.label)}</b>\n\n"
        f"<blockquote expandable>{_clip(prompt.text)}</blockquote>"
    )


def render_custom_prompt_text(chat_id: str, chat_config: ChatConfig, direction: str) -> str:
    """Build the prompt asking the owner to type a custom rephrasing prompt."""
    target = {"in": "incoming", "out": "outgoing", "both": "both directions"}.get(direction, direction)
    return (
        f"{_heading(chat_id, chat_config, '✍️')}\n\n"
        f"Send the custom rephrasing prompt for <b>{target}</b> as one message "
        "(at least 10 characters). It replaces any template for that direction."
    )


def render_templates_text(templates: list[PromptTemplate]) -> str:
    """Build the global templates list screen."""
    if not templates:
        return (
            "🧩 <b>Prompt templates</b>\n\n"
            "No templates configured. Add a list under <code>prompts.templates</code> in "
            "<code>config.yaml</code> (key, name, prompt)."
        )
    lines = [f"🧩 <b>Prompt templates</b> ({len(templates)})", ""]
    for template in templates:
        lines.append(f"• <b>{escape(template.name)}</b> <code>{escape(template.key)}</code>")
        lines.append(f"  <i>{_preview(template.prompt, 100)}</i>")
    lines += ["", "Tap a template to read its full text. Edit them in <code>config.yaml</code>."]
    return "\n".join(lines)


def render_template_detail_text(template: PromptTemplate, default_prompt: str) -> str:
    """Build one template's full text, with the default placeholder expanded."""
    return (
        f"🧩 <b>{escape(template.name)}</b> <code>{escape(template.key)}</code>\n\n"
        f"<blockquote expandable>{_clip(template.render(default_prompt))}</blockquote>"
    )


def render_delete_chat_text(chat_id: str, chat_config: ChatConfig) -> str:
    """Build the confirm screen for deleting a chat entry."""
    return (
        f"🗑 <b>Delete</b> {escape(chat_label(chat_id, chat_config))} <code>{escape(chat_id)}</code>?\n\n"
        "This removes the chat's stored settings (including custom prompts). The next voice in "
        "that chat recreates it with the defaults.\nTo stop transcribing temporarily, pause the chat instead."
    )
