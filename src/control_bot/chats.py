"""Chat lookup through the userbot: recent dialogs and id/@username resolution."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pyrogram import Client


@dataclass(slots=True, frozen=True)
class ChatRef:
    """A chat id together with a readable display name."""

    chat_id: int
    chat_name: str = ""

    @property
    def label(self) -> str:
        """The display name, or the id when no name is known."""
        return self.chat_name or str(self.chat_id)


def chat_display_name(chat: Any) -> str:
    """Build a readable display name from a Pyrogram chat object."""
    return str(
        getattr(chat, "title", None)
        or " ".join(
            part for part in [getattr(chat, "first_name", None), getattr(chat, "last_name", None)] if part
        ).strip()
        or (f"@{chat.username}" if getattr(chat, "username", None) else "")
        or getattr(chat, "id", "")
    )


async def resolve_chat_ref(userbot: Client, raw_value: str) -> ChatRef:
    """Resolve a numeric chat id or @username into a chat reference.

    Args:
        userbot: The running userbot client (bots cannot look up arbitrary chats).
        raw_value: The typed chat id (e.g. ``-1001234567890``) or ``@username``.

    Returns:
        The resolved chat id and display name.

    Raises:
        ValueError: When the input is empty.
        Exception: Whatever Pyrogram raises for an unknown chat.
    """
    candidate = raw_value.strip()
    if not candidate:
        raise ValueError("Please send a chat id or @username.")
    identifier: int | str = int(candidate) if candidate.lstrip("-").isdigit() else candidate
    chat = await userbot.get_chat(identifier)
    return ChatRef(chat_id=int(chat.id), chat_name=chat_display_name(chat))


def _dialog_activity(dialog: Any) -> float:
    """Return the dialog's last-activity time as a POSIX timestamp (oldest when unknown).

    A numeric key sidesteps comparing Pyrogram's timezone-aware message dates
    with naive fallbacks, which would raise ``TypeError`` while sorting.
    """
    date = getattr(getattr(dialog, "top_message", None), "date", None)
    if isinstance(date, datetime):
        try:
            return date.timestamp()
        except (OverflowError, OSError, ValueError):
            return float("-inf")
    return float("-inf")


async def _collect_recent_dialogs(userbot: Client, limit: int) -> list[Any]:
    """Collect the most recently active dialogs (main + archive), newest first.

    Telegram already returns dialogs newest-first, so each list is only read up
    to ``limit`` entries; large accounts are never enumerated completely.
    """
    collected: list[Any] = []
    seen: set[int] = set()

    for chat_list in (0, 1):
        try:
            dialogs: Any = userbot.get_dialogs(limit=limit, chat_list=chat_list)
        except TypeError:
            # Older Pyrogram builds have no chat_list argument (main list only).
            if chat_list:
                break
            dialogs = userbot.get_dialogs(limit=limit)

        if inspect.isasyncgen(dialogs):
            async for dialog in dialogs:
                chat_id = int(dialog.chat.id)
                if chat_id not in seen:
                    seen.add(chat_id)
                    collected.append(dialog)
        else:
            for dialog in dialogs:
                chat_id = int(dialog.chat.id)
                if chat_id not in seen:
                    seen.add(chat_id)
                    collected.append(dialog)

    collected.sort(key=_dialog_activity, reverse=True)
    return collected[:limit]


async def get_recent_chats(userbot: Client, limit: int = 20) -> list[ChatRef]:
    """Return the most recently active dialogs as chat references."""
    dialogs = await _collect_recent_dialogs(userbot, limit)
    return [ChatRef(chat_id=int(dialog.chat.id), chat_name=chat_display_name(dialog.chat)) for dialog in dialogs]
