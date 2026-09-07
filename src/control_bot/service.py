"""High-level setup of the optional control bot."""

from __future__ import annotations

import logging
from pathlib import Path

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import BotCommand

from src.control_bot.handlers import register_control_handlers
from src.control_bot.router import ControlRouter
from src.helpers import get_bot_config_value

logger = logging.getLogger(__name__)


def load_control_bot_settings() -> tuple[str, int]:
    """Read ``control_bot.token`` and ``control_bot.owner_id`` from ``config.yaml``.

    Returns:
        The bot token (empty when disabled) and the owner's numeric user id
        (0 when unset or invalid).
    """
    token = str(get_bot_config_value(["control_bot", "token"], default="") or "").strip()
    raw_owner = get_bot_config_value(["control_bot", "owner_id"], default=0)
    try:
        owner_id = int(str(raw_owner).strip() or 0)
    except (TypeError, ValueError):
        owner_id = 0
    return token, owner_id


def create_control_bot(userbot: Client, api_id: int, api_hash: str, session_dir: Path) -> Client | None:
    """Build and wire the control bot, or return None when it is not configured.

    Requires both ``control_bot.token`` and ``control_bot.owner_id``: without an
    owner the bot would accept commands from anyone, so it refuses to start.

    Args:
        userbot: The running userbot client, used to look up chats.
        api_id: Telegram API id (shared with the userbot).
        api_hash: Telegram API hash (shared with the userbot).
        session_dir: Directory for the bot's own session file.

    Returns:
        The configured (not yet started) bot client, or None.
    """
    token, owner_id = load_control_bot_settings()
    if not token:
        return None
    if owner_id <= 0:
        logger.warning(
            "control_bot.token is set but control_bot.owner_id is missing; "
            "refusing to start the control bot without an owner."
        )
        return None

    bot = Client(str(session_dir / "control_bot"), api_id=api_id, api_hash=api_hash, bot_token=token)
    bot.set_parse_mode(ParseMode.HTML)
    register_control_handlers(bot, ControlRouter(userbot), owner_id)
    logger.info("Control bot configured for owner %s", owner_id)
    return bot


async def configure_bot_commands(bot: Client) -> None:
    """Register the bot's slash-command menu (the list Telegram shows next to the input)."""
    await bot.set_bot_commands(
        [
            BotCommand("menu", "Open the control menu"),
            BotCommand("chats", "List configured chats"),
            BotCommand("status", "Show providers and chat status"),
            BotCommand("start", "Open the control menu"),
        ]
    )
