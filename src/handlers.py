"""Command handlers for help, configuration and voice-message routing."""

from __future__ import annotations

from pyrogram import Client
from pyrogram.types import Message

from src.helpers import (
    ChatConfig,
    ensure_chat_config,
    get_bot_config_value,
    get_chat_config,
    get_chat_info,
    save_chat_settings,
    send_and_delete_message,
)
from src.logging import get_logger
from src.transcription import transcribe_voice

logger = get_logger(__name__)

# ==== COMMAND HANDLERS ====


def _chat_id(message: Message) -> str:
    """Return the current message's chat ID as the JSON settings key."""
    return str(message.chat.id)


def _chat_display_name(message: Message) -> str:
    """Return the best display name for storing a chat entry."""
    return getattr(message.chat, "first_name", "") or getattr(message.chat, "title", "") or ""


def _message_text(message: Message) -> str:
    """Return message text defensively for command parsing."""
    return message.text or ""


def _command_name(message: Message) -> str:
    """Extract the normalized command name without slash, suffix or arguments."""
    text = _message_text(message)
    command_token = text.split(maxsplit=1)[0] if text else ""
    return command_token.split("@", 1)[0].strip("/").lower()


def _command_argument(message: Message) -> str:
    """Return everything after the command token, or an empty string."""
    parts = _message_text(message).split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _ensure_chat_settings(message: Message) -> dict[str, ChatConfig]:
    """Ensure and return persistent settings for the message's chat."""
    return ensure_chat_config(_chat_id(message), _chat_display_name(message))


def build_transcription_status_text(chat_config: ChatConfig) -> str:
    """Build a compact, icon-based transcription status panel for a chat.

    Args:
        chat_config: The resolved per-chat configuration.

    Returns:
        A multi-line status string using ✅/❌ icons, intended to be wrapped in a
        Telegram ``<pre>`` block.
    """

    def icon(value: object) -> str:
        """Return a green check for truthy values, a red cross otherwise."""
        return "✅" if value else "❌"

    global_on = bool(chat_config.get("transcription", 1))

    lines = [
        "🎙️ Transcription",
        f"{'Global':<9}{icon(global_on)}",
    ]
    if global_on:
        lines.append(f"{'Incoming':<9}{icon(chat_config.get('transcription_in', 1))}")
        lines.append(f"{'Outgoing':<9}{icon(chat_config.get('transcription_out', 1))}")
    else:
        lines.append("(global off — per-direction settings paused)")

    return "\n".join(lines)


async def show_help(client: Client, message: Message) -> None:
    """Show a help message with all available voice-bot commands.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Showing help to user in {chat_info}")
    chat_config = get_chat_config(_chat_id(message))
    settings_text = build_transcription_status_text(chat_config)
    help_text = f"""<pre>Current Settings:
{settings_text}

Available Commands:
/helpv       Show this help message.
/statusv     Show current transcription settings.
/ton         Enable transcription globally for this chat.
/toff        Disable transcription globally for this chat.
/tin         Toggle transcription for incoming voices.
/tout        Toggle transcription for outgoing voices.
/rephrase    Toggle rephrasing of transcriptions.
/delin       Toggle deletion of incoming voices.
/delout      Toggle deletion of outgoing voices.
/prompt      Show the current rephrasing prompt.
/prompts     Show prompts overview (custom/default).
/setprompt   Set a custom rephrasing prompt.
/setprompt_in Set a custom rephrasing prompt for incoming messages.
/setprompt_out Set a custom rephrasing prompt for outgoing messages.

Tip: Run any command from the chat's "scheduled messages" view to
keep both the command and its reply invisible to the chat partner.</pre>"""

    await send_and_delete_message(client, message, help_text, 10)


async def show_status(client: Client, message: Message) -> None:
    """Show compact transcription status for the current chat.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Showing status for {chat_info}")
    chat_config = get_chat_config(_chat_id(message))
    status_text = build_transcription_status_text(chat_config)
    await send_and_delete_message(client, message, f"<pre>{status_text}</pre>", 5)


async def show_prompt(client: Client, message: Message) -> None:
    """Show the current incoming and outgoing rephrasing prompts.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Showing prompt for {chat_info}")
    chat_config = get_chat_config(_chat_id(message))
    # Load the default prompt dynamically each time
    default_prompt = str(get_bot_config_value(["prompts", "rephrase"], default="") or "")
    prompt_in = str(chat_config.get("rephrase_prompt_in") or default_prompt)
    prompt_out = str(chat_config.get("rephrase_prompt_out") or default_prompt)

    prompt_text = "<pre>Current Prompts:</pre>\\n"
    prompt_text += "<blockquote expandable><b>Incoming:</b>\\n" + prompt_in + "</blockquote>\\n"
    prompt_text += "<blockquote expandable><b>Outgoing:</b>\\n" + prompt_out + "</blockquote>"

    await send_and_delete_message(client, message, prompt_text, 15)


async def show_prompts(client: Client, message: Message) -> None:
    """Show active rephrasing prompts and whether each one is custom or default.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Showing prompts for {chat_info}")
    chat_config = get_chat_config(_chat_id(message))
    # Load the default prompt dynamically each time
    default_prompt = str(get_bot_config_value(["prompts", "rephrase"], default="") or "")
    prompt_in = str(chat_config.get("rephrase_prompt_in") or "")
    prompt_out = str(chat_config.get("rephrase_prompt_out") or "")

    # Determine source for incoming prompt
    if prompt_in and len(prompt_in) >= 10:
        in_source = "CUSTOM"
        in_display = prompt_in
    else:
        in_source = "DEFAULT"
        in_display = default_prompt

    # Determine source for outgoing prompt
    if prompt_out and len(prompt_out) >= 10:
        out_source = "CUSTOM"
        out_display = prompt_out
    else:
        out_source = "DEFAULT"
        out_display = default_prompt

    prompts_text = (
        f"<pre>Prompts Overview:\n"
        f"─────────────────────────\n"
        f"IN  [{in_source}]:\n"
        f"{in_display}\n"
        f"─────────────────────────\n"
        f"OUT [{out_source}]:\n"
        f"{out_display}</pre>"
    )

    await send_and_delete_message(client, message, prompts_text, 15)


async def set_prompt(client: Client, message: Message) -> None:
    """Set one custom rephrasing prompt for both directions.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Setting prompts for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)

    new_prompt = _command_argument(message)
    config[chat_id]["rephrase_prompt_in"] = new_prompt
    config[chat_id]["rephrase_prompt_out"] = new_prompt
    # Save the updated config
    save_chat_settings(config)

    if new_prompt:
        status_text = "Custom prompts set for both incoming and outgoing messages."
        logger.info(f"Custom prompts set for {chat_info}")
    else:
        status_text = "Rephrasing prompts reset to default."
        logger.info(f"Prompts reset to default for {chat_info}")

    await send_and_delete_message(client, message, f"<pre>{status_text}</pre>", 3)


async def set_prompt_in(client: Client, message: Message) -> None:
    """Set a custom rephrasing prompt for incoming voice messages.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Setting incoming prompt for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)

    new_prompt = _command_argument(message)
    config[chat_id]["rephrase_prompt_in"] = new_prompt
    # Save the updated config
    save_chat_settings(config)

    if new_prompt:
        status_text = "Custom prompt set for incoming messages."
        logger.info(f"Custom incoming prompt set for {chat_info}")
    else:
        status_text = "Incoming rephrasing prompt reset to default."
        logger.info(f"Incoming prompt reset to default for {chat_info}")

    await send_and_delete_message(client, message, f"<pre>{status_text}</pre>", 3)


async def set_prompt_out(client: Client, message: Message) -> None:
    """Set a custom rephrasing prompt for outgoing voice messages.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Setting outgoing prompt for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)

    new_prompt = _command_argument(message)
    config[chat_id]["rephrase_prompt_out"] = new_prompt
    # Save the updated config
    save_chat_settings(config)

    if new_prompt:
        status_text = "Custom prompt set for outgoing messages."
        logger.info(f"Custom outgoing prompt set for {chat_info}")
    else:
        status_text = "Outgoing rephrasing prompt reset to default."
        logger.info(f"Outgoing prompt reset to default for {chat_info}")

    await send_and_delete_message(client, message, f"<pre>{status_text}</pre>", 3)


async def toggle_transcription_mode(client: Client, message: Message) -> None:
    """Toggle transcription for incoming or outgoing voice messages.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Toggling transcription mode for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)
    command = _command_name(message)

    key = "transcription_in" if command == "tin" else "transcription_out"
    config[chat_id][key] = 1 - int(config[chat_id].get(key, 1) or 0)
    # Save the updated config
    save_chat_settings(config)

    direction = "incoming" if command == "tin" else "outgoing"
    status = "enabled" if config[chat_id][key] == 1 else "disabled"
    status_text = f"<pre>Transcription for {direction} voices {status}.</pre>"
    logger.info(f"Transcription for {direction} voices {status} in {chat_info}")
    await send_and_delete_message(client, message, status_text, 1.8)


async def set_global_transcription_mode(client: Client, message: Message) -> None:
    """Enable or disable transcription globally for the current chat.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Setting global transcription mode for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)
    command = _command_name(message)

    enabled = 1 if command == "ton" else 0
    config[chat_id]["transcription"] = enabled
    save_chat_settings(config)

    status = "enabled" if enabled else "disabled"
    status_text = f"<pre>Global transcription {status} for this chat.</pre>"
    logger.info(f"Global transcription {status} in {chat_info}")
    await send_and_delete_message(client, message, status_text, 1.8)


async def toggle_rephrasing(client: Client, message: Message) -> None:
    """Toggle rephrasing of transcriptions for the current chat.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Toggling rephrasing for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)

    config[chat_id]["rephrasing"] = 1 - int(config[chat_id].get("rephrasing", 1) or 0)
    # Save the updated config
    save_chat_settings(config)

    status = "enabled" if config[chat_id]["rephrasing"] == 1 else "disabled"
    status_text = f"<pre>Rephrasing of transcriptions {status}.</pre>"
    logger.info(f"Rephrasing of transcriptions {status} in {chat_info}")
    await send_and_delete_message(client, message, status_text, 1.8)


async def toggle_voice_delete(client: Client, message: Message) -> None:
    """Toggle deletion of incoming or outgoing voice messages after transcription.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Toggling voice deletion for {chat_info}")
    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)
    command = _command_name(message)
    key = "delete_outgoing_voice" if command == "delout" else "delete_incoming_voice"
    config[chat_id][key] = 1 if not config[chat_id].get(key, 0) else 0
    # Save the updated config
    save_chat_settings(config)

    # Define current status as text and show it to the user briefly
    voicetype = "outgoing" if command == "delout" else "incoming"
    status = "enabled" if config[chat_id][key] else "disabled"
    status_text = f"<pre>Deleting {voicetype} voices {status}.</pre>"
    logger.info(f"Deleting {voicetype} voices {status} in {chat_info}")
    await send_and_delete_message(client, message, status_text, 1.8)


async def handle_voice(client: Client, message: Message) -> None:
    """Route an incoming or outgoing voice message through transcription settings.

    Args:
        client: The Pyrogram client instance that received the voice message.
        message: The voice message to process.
    """
    chat_info = get_chat_info(message.chat)
    message_direction = "outgoing" if message.outgoing else "incoming"
    logger.info(f"Handling {message_direction} voice message for {chat_info}")
    chat_config = get_chat_config(_chat_id(message))

    # Respect global chat toggle first.
    transcription_enabled_globally = bool(chat_config.get("transcription", 1))
    transcribe = False
    if transcription_enabled_globally:
        if message.outgoing and chat_config.get("transcription_out", 1):
            transcribe = True
        elif not message.outgoing and chat_config.get("transcription_in", 1):
            transcribe = True

    # Log transcription decision
    logger.info(
        f"Transcription {'enabled' if transcribe else 'disabled'} for {message_direction} voice in {chat_info} "
        f"(global={int(transcription_enabled_globally)})"
    )

    # Process messages based on transcription settings
    if transcribe:
        # Only process messages from non-bot users
        if message.from_user and not message.from_user.is_bot:
            await transcribe_voice(client, message)
        else:
            logger.info(f"Skipping voice message from bot user in {chat_info}")
    else:
        logger.info(f"Voice message not transcribed due to settings in {chat_info}")
