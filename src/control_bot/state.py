"""Per-owner conversational state for the control bot's multi-step flows.

The control bot has exactly one owner, but state is keyed by user id anyway so
the registry stays a plain, self-contained mapping. A pending action tracks
which flow is in progress (adding a chat, or typing a custom prompt), the most
recently shown recent-chats list (for index-based picks) and the chat/direction
a typed prompt belongs to. Any navigation button cancels the pending flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.control_bot.chats import ChatRef

KIND_ADD_CHAT = "add_chat"
KIND_CUSTOM_PROMPT = "custom_prompt"


@dataclass(slots=True)
class PendingAction:
    """One in-progress multi-step flow for a single owner."""

    kind: str
    recent: list[ChatRef] = field(default_factory=list)
    chat_id: str = ""
    direction: str = ""
    awaiting_text: bool = False
