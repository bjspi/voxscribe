"""Shared helper functions for configuration, chat settings and bot messages."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml
from pyrogram import Client
from pyrogram.types import Chat, Message

from src.logging import get_logger

logger = get_logger(__name__)

# Serializes writes to chats.json so concurrent commands (or threads spawned via
# asyncio.to_thread) can never interleave and corrupt the file.
_CHATS_FILE_LOCK = threading.Lock()

type YamlConfig = dict[str, Any]
type ChatConfigValue = str | int | bool | None
type ChatConfig = dict[str, ChatConfigValue]
type ChatSettings = dict[str, ChatConfig]

SCHEDULED_RESPONSE_DELETE_DELAY_SECONDS = 30
SCHEDULED_RESPONSE_DELETE_NOTICE = f"(AutoDelete in {SCHEDULED_RESPONSE_DELETE_DELAY_SECONDS}s)\n\n"

# Configuration file paths (absolute paths relative to project root)
PROJECT_ROOT = Path(__file__).parent.parent
BOT_CONFIG_FILE = PROJECT_ROOT / "config.yaml"
CHATS_FILE = PROJECT_ROOT / "chats.json"
LEGACY_CHAT_SETTINGS_FILE = PROJECT_ROOT / "chat_settings.json"

# Default configuration values for each chat
# These can be overridden per chat in the chats.json file
CONFIG_DEFAULTS: ChatConfig = {
    "transcription": 1,  # Global transcription switch for the chat
    "transcription_in": 1,  # Transcribe incoming voice messages
    "transcription_out": 1,  # Transcribe outgoing voice messages
    "rephrasing": 1,  # Rephrase transcriptions for better readability
    "delete_outgoing_voice": 0,  # Delete outgoing voice messages after transcription
    "delete_incoming_voice": 0,  # Delete incoming voice messages after transcription
    "rephrase_prompt_in": "",  # Custom rephrasing prompt for incoming messages (empty = use default)
    "rephrase_prompt_out": "",  # Custom rephrasing prompt for outgoing messages (empty = use default)
}


def _as_bool(value: object, default: bool) -> bool:
    """Coerce a YAML/env-style value into a boolean.

    Args:
        value: Raw value to interpret (bool, number, or string).
        default: Fallback for None, empty, or unrecognised values.

    Returns:
        The parsed boolean value.
    """
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def new_chat_transcription_default() -> int:
    """Return the global transcription flag (1/0) used for chats not yet stored.

    Reads ``transcription_enabled_new_chats`` from ``config.yaml`` (default True),
    deciding whether the bot transcribes in a brand-new chat before any ``/ton`` /
    ``/toff`` has been issued.

    Returns:
        1 if new chats should transcribe by default, otherwise 0.
    """
    raw = get_bot_config_value(["transcription_enabled_new_chats"], default=True)
    return 1 if _as_bool(raw, default=True) else 0


def migrate_legacy_chat_settings_file() -> None:
    """Move legacy chat settings storage to the current ``chats.json`` filename.

    Older versions used ``chat_settings.json``. If the new file already exists,
    no migration is attempted so existing settings are never overwritten.
    """
    if CHATS_FILE.exists() or not LEGACY_CHAT_SETTINGS_FILE.exists():
        return

    LEGACY_CHAT_SETTINGS_FILE.replace(CHATS_FILE)
    logger.info(
        "Moved legacy chat storage file from %s to %s",
        LEGACY_CHAT_SETTINGS_FILE,
        CHATS_FILE,
    )


def get_chat_info(chat: Chat) -> str:
    """Get a formatted string with chat ID and name for logging.

    Args:
        chat: The Pyrogram chat object to describe.

    Returns:
        A stable human-readable label containing chat ID and chat name.
    """
    chat_id = str(chat.id)
    chat_name = getattr(chat, "first_name", None) or getattr(chat, "title", None) or "Unknown"
    return f"Chat {chat_id} ({chat_name})"


def load_bot_config() -> YamlConfig:
    """Load bot configuration from YAML file.

    Returns:
        The parsed YAML mapping, or an empty dict if the file is missing or
        cannot be parsed.
    """
    if BOT_CONFIG_FILE.exists():
        try:
            with open(BOT_CONFIG_FILE, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Error loading bot config: {e}")
            return {}
    return {}


def get_config_value(
    config: YamlConfig,
    keys: str | list[str],
    default: Any = None,
) -> Any:
    """Read a nested value from a parsed YAML configuration mapping.

    Args:
        config: The parsed configuration mapping to traverse.
        keys: A single key or ordered list of keys representing the path.
        default: Value returned when the key path is missing.

    Returns:
        The value at the specified key path, or ``default`` if any segment is
        missing.
    """
    if isinstance(keys, str):
        keys = [keys]

    current: Any = config
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default

    return current


def get_bot_config_value(keys: str | list[str], default: Any = None) -> Any:
    """Get a value from ``config.yaml`` using a key path.

    Args:
        keys: A single key or ordered list of keys representing the path.
        default: Value returned when the key path is missing.

    Returns:
        The value at the specified key path, or ``default`` if any segment is
        missing.
    """
    return get_config_value(load_bot_config(), keys, default)


def save_bot_config(config: YamlConfig) -> bool:
    """Save bot configuration to YAML file.

    Args:
        config: The configuration dictionary to save.

    Returns:
        True if the file was written successfully, otherwise False.
    """
    try:
        with open(BOT_CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, indent=2)
        logger.info("Bot configuration saved successfully")
        return True
    except Exception as e:
        logger.error(f"Error saving bot config: {e}")
        return False


def load_chat_settings() -> ChatSettings:
    """Load chat settings from ``chats.json``.

    Returns:
        The chat settings mapping, or an empty dict if the file is missing or
        invalid.
    """
    logger.info("Loading chat settings")
    migrate_legacy_chat_settings_file()
    if not CHATS_FILE.exists():
        logger.info("Chat settings file not found, creating new one")
        save_chat_settings({})
    try:
        with open(CHATS_FILE, encoding="utf-8") as f:
            config = json.load(f)
            logger.info(f"Loaded chat settings with {len(config)} chat entries")
            return cast(ChatSettings, config)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.error(f"Error loading chat settings: {e}")
        # If there's an error reading the config, return empty dict
        return {}


def save_chat_settings(config: ChatSettings) -> bool:
    """Save chat settings to ``chats.json``.

    Args:
        config: The chat settings dictionary to write.

    Returns:
        True if the file was written successfully, otherwise False.
    """
    try:
        logger.info(f"Saving chat settings with {len(config)} chat entries")
        # Serialize the whole read-backup-write so two callers cannot interleave.
        with _CHATS_FILE_LOCK:
            migrate_legacy_chat_settings_file()
            # Create backup of existing config
            if CHATS_FILE.exists():
                backup_path = CHATS_FILE.with_suffix(".json.backup")
                with open(CHATS_FILE, encoding="utf-8") as src, open(backup_path, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
                logger.info("Backup of chat settings file created")

            # Write to a temp file in the same directory, then atomically replace
            # the target so a crash mid-write can never leave a half-written file.
            fd, tmp_name = tempfile.mkstemp(dir=str(CHATS_FILE.parent), prefix=".chats.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, CHATS_FILE)
            except BaseException:
                # Never leave a stray temp file behind on failure.
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
                raise
        logger.info("Chat settings saved successfully")
        return True
    except Exception as e:
        logger.error(f"Error saving chat settings: {e}", exc_info=True)
        return False


def ensure_chat_config(chat_id: str, chatname: str = "") -> ChatSettings:
    """Ensure a configuration block exists for ``chat_id``.

    This function checks if a configuration exists for the given chat_id. If not,
    it creates a new configuration with default values. It also updates the chat
    name if provided.

    Args:
        chat_id: The chat ID to ensure configuration for.
        chatname: Optional chat display name stored when the chat is first seen.

    Returns:
        The full chat settings mapping, including the ensured chat entry.
    """
    logger.info(f"Ensuring chat config for chat {chat_id}")
    config = load_chat_settings()
    changed = False

    is_new_chat = chat_id not in config
    if is_new_chat:
        logger.info(f"Creating new config for chat {chat_id}")
        config[chat_id] = {}
        changed = True

    for key, val in CONFIG_DEFAULTS.items():
        if key not in config[chat_id]:
            # Brand-new chats honour the configurable transcription default.
            config[chat_id][key] = new_chat_transcription_default() if (key == "transcription" and is_new_chat) else val
            changed = True

    if chatname and "chatname" not in config[chat_id]:
        config[chat_id]["chatname"] = chatname
        changed = True

    if not changed:
        logger.info(f"Chat config already up to date for chat {chat_id}")
        return config

    if save_chat_settings(config):
        logger.info(f"Chat config ensured for chat {chat_id}")
        return config

    logger.warning(f"Failed to save config for chat {chat_id}")
    return config


def get_chat_config(chat_id: str) -> ChatConfig:
    """Get the configuration for a specific chat with defaults applied.

    This function retrieves the configuration for a specific chat, applying
    default values for any missing keys.

    Args:
        chat_id: The chat ID to get configuration for.

    Returns:
        The chat-specific settings dictionary with runtime defaults filled in.
    """
    logger.info(f"Getting chat config for chat {chat_id}")
    config = load_chat_settings()
    is_new_chat = chat_id not in config
    chat_config = config.get(chat_id, {})

    # Apply defaults for any missing keys
    for key, val in CONFIG_DEFAULTS.items():
        if key not in chat_config:
            # Brand-new chats honour the configurable transcription default.
            chat_config[key] = new_chat_transcription_default() if (key == "transcription" and is_new_chat) else val

    logger.info(f"Returning chat config with {len(chat_config)} settings for chat {chat_id}")
    return chat_config


def _is_scheduled_message(message: Message) -> bool:
    """Return True for messages still sitting in Telegram's scheduled queue.

    Args:
        message: The Pyrogram message to inspect.

    Returns:
        True if the message is a scheduled (not yet sent) message.
    """
    return bool(getattr(message, "scheduled", False))


def _format_scheduled_response_text(text: str) -> str:
    """Prefix a covert scheduled reply with the auto-delete notice.

    Args:
        text: The reply text to send.

    Returns:
        The text with the auto-delete notice prepended (idempotent).
    """
    if text.startswith(SCHEDULED_RESPONSE_DELETE_NOTICE):
        return text

    return SCHEDULED_RESPONSE_DELETE_NOTICE + text


async def send_and_delete_message(client: Client, message: Message, text: str, delay: float) -> None:
    """Sends a reply, waits for a delay, and then deletes both messages.

    Has two modes:
    - Normal: reply in chat, wait, delete reply + original (visible to chat partner).
    - Covert (when ``message.scheduled`` is True): send the answer as a scheduled
      message in the same chat so it shows up only in the user's scheduled-messages
      view. Both the original scheduled command and the scheduled answer are then
      deleted before they could actually fire, so the chat partner never sees
      anything.

    Args:
        client: The Pyrogram client used to send the covert scheduled reply.
        message: The original message to reply to.
        text: The text to send in the reply.
        delay: The delay in seconds before deleting the messages. Scheduled-message
            replies intentionally use SCHEDULED_RESPONSE_DELETE_DELAY_SECONDS instead.
    """
    try:
        if _is_scheduled_message(message):
            delete_delay = SCHEDULED_RESPONSE_DELETE_DELAY_SECONDS
            response_text = _format_scheduled_response_text(text)
            # Match the command's own schedule_date so the reply sits next to it
            # in the scheduled-messages view. Both get deleted before they fire.
            # Fallback: 30 days out if the command somehow has no date.
            schedule_date = message.date
            now = (
                datetime.now(schedule_date.tzinfo)
                if schedule_date and schedule_date.tzinfo and schedule_date.utcoffset() is not None
                else datetime.now()
            )
            safe_fallback = now + timedelta(days=30)
            schedule_date = schedule_date or safe_fallback
            if schedule_date <= now + timedelta(seconds=delete_delay + 10):
                # Too close to fire safely before our delete - push it out.
                schedule_date = safe_fallback
            logger.info("Sending covert scheduled reply (delay=%s) in chat %s", delete_delay, message.chat.id)
            status_message = await client.send_message(
                chat_id=message.chat.id,
                text=response_text,
                schedule_date=schedule_date,
            )
            await asyncio.sleep(delete_delay)
            await status_message.delete()
            await message.delete()
            logger.info("Covert scheduled reply sent and cleaned up")
        else:
            logger.info(f"Sending message with delay {delay}")
            status_message = await message.reply_text(text)
            await asyncio.sleep(delay)
            await status_message.delete()
            await message.delete()
            logger.info("Message sent and deleted successfully")
    except Exception as e:
        logger.error(f"Error in send_and_delete_message: {e}", exc_info=True)
