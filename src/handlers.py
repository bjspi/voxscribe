"""Command handlers for help, configuration and voice-message routing."""

from __future__ import annotations

from html import escape

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
from src.prompts import MIN_PROMPT_LENGTH, resolve_rephrase_prompt
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

    lines.append(f"{'Markdown':<9}{icon(chat_config.get('markdown_output', 0))}")
    if chat_config.get("markdown_output", 0):
        lines.append("(one .md file: original + rephrased)")

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
/vox         Show ALL settings of this chat + the commands to change them.
/ton         Enable transcription globally for this chat.
/toff        Disable transcription globally for this chat.
/tin         Toggle transcription for incoming voices.
/tout        Toggle transcription for outgoing voices.
/rephrase    Toggle rephrasing of transcriptions.
/tmd [on|off] Markdown file: original + rephrased (default off).
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


def build_chat_config_text(chat_id: str, chat_config: ChatConfig, default_prompt: str) -> str:
    """Build the full per-chat settings panel shown by ``/vox``.

    Lists every setting with its current value and the slash command that
    changes it, so the whole configuration of a chat is visible in one place.

    Args:
        chat_id: The chat ID (JSON key) the settings belong to.
        chat_config: The resolved per-chat configuration.
        default_prompt: The global ``prompts.rephrase`` text (for prompt labels).

    Returns:
        A multi-line, column-aligned string intended for a ``<pre>`` block.
    """

    def icon(value: object) -> str:
        return "✅ on " if value else "❌ off"

    global_on = bool(chat_config.get("transcription", 1))
    markdown = bool(chat_config.get("markdown_output", 0))
    prompt_in = resolve_rephrase_prompt(chat_config, "in", default_prompt)
    prompt_out = resolve_rephrase_prompt(chat_config, "out", default_prompt)
    name = str(chat_config.get("chatname") or "").strip()
    header = f"{name} ({chat_id})" if name else chat_id

    rows: list[tuple[str, str, str]] = [
        ("Transcription", icon(global_on), "/ton · /toff"),
        ("  Incoming", icon(chat_config.get("transcription_in", 1)), "/tin"),
        ("  Outgoing", icon(chat_config.get("transcription_out", 1)), "/tout"),
        ("Output", "📄 Markdown file" if markdown else "💬 inline text", "/tmd on|off"),
        ("Rephrasing", icon(chat_config.get("rephrasing", 1)), "/rephrase"),
        ("Delete voice", "", ""),
        ("  Incoming", icon(chat_config.get("delete_incoming_voice", 0)), "/delin"),
        ("  Outgoing", icon(chat_config.get("delete_outgoing_voice", 0)), "/delout"),
        ("Prompt in", prompt_in.label, "/setprompt_in"),
        ("Prompt out", prompt_out.label, "/setprompt_out"),
    ]
    lines = ["🎙️ voxscribe — chat settings", escape(header), ""]
    for label, value, command in rows:
        lines.append(f"{label:<14}{escape(value):<18}{command}".rstrip())
    notes = []
    if not global_on:
        notes.append("(transcription off — direction settings paused)")
    if markdown:
        notes.append("(Markdown files always contain original + rephrased)")
    if notes:
        lines += [""] + notes
    lines += ["", "/prompts shows the full prompt texts."]
    return "\n".join(lines)


async def show_config(client: Client, message: Message) -> None:
    """Show every setting of the current chat, with the command that changes it.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    chat_info = get_chat_info(message.chat)
    logger.info(f"Showing chat settings for {chat_info}")
    chat_id = _chat_id(message)
    chat_config = get_chat_config(chat_id)
    default_prompt = str(get_bot_config_value(["prompts", "rephrase"], default="") or "")
    config_text = build_chat_config_text(chat_id, chat_config, default_prompt)
    await send_and_delete_message(client, message, f"<pre>{config_text}</pre>", 15)


# Telegram messages are capped at 4096 characters; two full prompts plus
# markup must fit, so each prompt gets roughly half of the budget.
PROMPT_DISPLAY_LIMIT = 1800


def _clip_prompt(text: str, limit: int = PROMPT_DISPLAY_LIMIT) -> str:
    """Escape a prompt for HTML output and cut it so two of them fit in one message."""
    if len(text) > limit:
        return escape(text[:limit].rstrip()) + "\n… (truncated)"
    return escape(text)


async def _store_custom_prompt(client: Client, message: Message, directions: tuple[str, ...], label: str) -> None:
    """Store (or clear) the custom rephrasing prompt for the given directions.

    An empty argument resets to the default. A non-empty prompt must have at
    least ``MIN_PROMPT_LENGTH`` characters, because shorter ones would be
    ignored at runtime while silently dropping a chosen template.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The command message; its argument is the new prompt.
        directions: ``("in",)``, ``("out",)`` or ``("in", "out")``.
        label: Human wording for the affected directions, used in replies.
    """
    chat_info = get_chat_info(message.chat)
    new_prompt = _command_argument(message).strip()
    if new_prompt and len(new_prompt) < MIN_PROMPT_LENGTH:
        await send_and_delete_message(
            client,
            message,
            f"<pre>The prompt must have at least {MIN_PROMPT_LENGTH} characters (or be empty to reset).</pre>",
            5,
        )
        return

    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)
    for direction in directions:
        config[chat_id][f"rephrase_prompt_{direction}"] = new_prompt
        # A typed prompt replaces any template chosen through the control bot.
        config[chat_id][f"rephrase_template_{direction}"] = ""
    if not save_chat_settings(config):
        await send_and_delete_message(client, message, "<pre>Could not save the prompt.</pre>", 5)
        return

    if new_prompt:
        status_text = f"Custom prompt set for {label} messages."
        logger.info(f"Custom prompt set for {label} messages in {chat_info}")
    else:
        status_text = f"Rephrasing prompt for {label} messages reset to default."
        logger.info(f"Prompt for {label} messages reset to default in {chat_info}")
    await send_and_delete_message(client, message, f"<pre>{status_text}</pre>", 3)


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
    prompt_in = resolve_rephrase_prompt(chat_config, "in", default_prompt)
    prompt_out = resolve_rephrase_prompt(chat_config, "out", default_prompt)

    prompt_text = "<pre>Current Prompts:</pre>\n"
    prompt_text += (
        f"<blockquote expandable><b>Incoming ({escape(prompt_in.label)}):</b>\n"
        f"{_clip_prompt(prompt_in.text)}</blockquote>\n"
    )
    prompt_text += (
        f"<blockquote expandable><b>Outgoing ({escape(prompt_out.label)}):</b>\n"
        f"{_clip_prompt(prompt_out.text)}</blockquote>"
    )

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
    prompt_in = resolve_rephrase_prompt(chat_config, "in", default_prompt)
    prompt_out = resolve_rephrase_prompt(chat_config, "out", default_prompt)

    prompts_text = (
        f"<pre>Prompts Overview:\n"
        f"─────────────────────────\n"
        f"IN  [{escape(prompt_in.label.upper())}]:\n"
        f"{_clip_prompt(prompt_in.text)}\n"
        f"─────────────────────────\n"
        f"OUT [{escape(prompt_out.label.upper())}]:\n"
        f"{_clip_prompt(prompt_out.text)}</pre>"
    )

    await send_and_delete_message(client, message, prompts_text, 15)


async def set_prompt(client: Client, message: Message) -> None:
    """Set one custom rephrasing prompt for both directions.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    await _store_custom_prompt(client, message, ("in", "out"), "incoming and outgoing")


async def set_prompt_in(client: Client, message: Message) -> None:
    """Set a custom rephrasing prompt for incoming voice messages.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    await _store_custom_prompt(client, message, ("in",), "incoming")


async def set_prompt_out(client: Client, message: Message) -> None:
    """Set a custom rephrasing prompt for outgoing voice messages.

    Args:
        client: The Pyrogram client instance that received the command.
        message: The message that triggered the command.
    """
    await _store_custom_prompt(client, message, ("out",), "outgoing")

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
    notice = "\nMarkdown output always includes both versions." if config[chat_id].get("markdown_output", 0) else ""
    status_text = f"<pre>Rephrasing of text messages {status}.{notice}</pre>"
    logger.info(f"Rephrasing of transcriptions {status} in {chat_info}")
    await send_and_delete_message(client, message, status_text, 1.8)


async def toggle_markdown_output(client: Client, message: Message) -> None:
    """Toggle Markdown attachments for this chat, or explicitly set on/off."""
    argument = _command_argument(message).strip().lower()
    if argument not in {"", "on", "off"}:
        await send_and_delete_message(client, message, "<pre>Usage: /tmd [on|off]</pre>", 5)
        return

    chat_id = _chat_id(message)
    config = _ensure_chat_settings(message)
    enabled = argument == "on" if argument else not config[chat_id].get("markdown_output", 0)
    config[chat_id]["markdown_output"] = int(enabled)
    if not save_chat_settings(config):
        await send_and_delete_message(client, message, "<pre>Could not save Markdown output setting.</pre>", 5)
        return

    status = "enabled" if enabled else "disabled"
    detail = " One .md file per voice with original + rephrased text." if enabled else " Sending text messages."
    logger.info("Markdown output %s in %s", status, get_chat_info(message.chat))
    await send_and_delete_message(client, message, f"<pre>Markdown output {status} for this chat.{detail}</pre>", 5)


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
    # Make sure the chat has a stored entry: that is what the control bot lists,
    # and what a deleted chat is recreated from on its next voice.
    chat_config = _ensure_chat_settings(message)[_chat_id(message)]

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
