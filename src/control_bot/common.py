"""Small shared helpers for rendering chat settings in the control bot."""

from __future__ import annotations

from src.helpers import ChatConfig

# Chats per page in the chat list keyboard.
PAGE_SIZE = 12
# Display names are cut to this length in buttons and lists (Telegram truncates
# long button labels anyway, and it keeps list screens under the 4096 limit).
MAX_LABEL_LENGTH = 40
# Telegram's message text limit, with headroom for HTML tags added on top.
MAX_MESSAGE_LENGTH = 4000


def short(text: str, limit: int = MAX_LABEL_LENGTH) -> str:
    """Cut a label to ``limit`` characters, marking the cut with an ellipsis."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def fit_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Cut a rendered screen text so it fits into one Telegram message."""
    if len(text) <= limit:
        return text
    return text[: limit - 16].rstrip() + "\n\n… (truncated)"


DIRECTION_LABELS: dict[str, str] = {
    "both": "both (incoming + outgoing)",
    "in": "incoming only",
    "out": "outgoing only",
    "none": "none (both directions off)",
}


def chat_label(chat_id: str, chat_config: ChatConfig) -> str:
    """Return the cached display name of a chat (shortened), or its id."""
    return short(str(chat_config.get("chatname") or "")) or chat_id


def is_enabled(chat_config: ChatConfig) -> bool:
    """Return whether the chat's master transcription switch is on."""
    return bool(chat_config.get("transcription", 1))


def direction_of(chat_config: ChatConfig) -> str:
    """Return ``both`` / ``in`` / ``out`` / ``none`` for the per-direction switches."""
    incoming = bool(chat_config.get("transcription_in", 1))
    outgoing = bool(chat_config.get("transcription_out", 1))
    if incoming and outgoing:
        return "both"
    if incoming:
        return "in"
    if outgoing:
        return "out"
    return "none"


def uses_markdown(chat_config: ChatConfig) -> bool:
    """Return whether the chat receives Markdown files instead of inline text."""
    return bool(chat_config.get("markdown_output", 0))


def rephrasing_on(chat_config: ChatConfig) -> bool:
    """Return whether AI rephrasing is enabled for inline text replies."""
    return bool(chat_config.get("rephrasing", 1))


def state_icon(active: bool) -> str:
    """Return the running/paused chip used across lists and detail screens."""
    return "▶️" if active else "⏸"


def sort_chats(chats: dict[str, ChatConfig]) -> list[tuple[str, ChatConfig]]:
    """Return chats sorted by display name (case-insensitive); unnamed chats last."""
    return sorted(
        chats.items(),
        key=lambda item: (not item[1].get("chatname"), chat_label(item[0], item[1]).lower(), item[0]),
    )
