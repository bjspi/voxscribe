"""Pyrogram handlers for the control bot: commands, button taps and typed input.

Every handler is gated to a single owner: messages must come from the owner's
user id *in the private chat with the bot*, and callback queries must come from
the owner. Messages from anyone else are logged (with the sender's id, which is
handy while setting ``control_bot.owner_id``) and otherwise ignored.
"""

from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from src.control_bot.router import ControlRouter

logger = logging.getLogger(__name__)


def _owner_callback_filter(owner_id: int):
    """Build a filter accepting only callback queries tapped by the owner."""

    async def check(_, __, update: CallbackQuery) -> bool:
        user = getattr(update, "from_user", None)
        return bool(user is not None and user.id == owner_id)

    return filters.create(check)


def register_control_handlers(bot: Client, router: ControlRouter, owner_id: int) -> None:
    """Register the owner-only command, callback and text handlers on the bot."""
    owner = filters.private & filters.user(owner_id)

    async def reply_screen(message: Message, screen: tuple) -> None:
        text, markup = screen
        await message.reply_text(text, reply_markup=markup)

    @bot.on_message(filters.command(["start", "menu"]) & owner)
    async def open_menu(_: Client, message: Message) -> None:
        """Open the main control menu and clear any half-finished flow."""
        try:
            router.pending.pop(message.from_user.id, None)
            await reply_screen(message, router.main_menu())
        except Exception:
            logger.exception("Control bot: failed to open the main menu.")

    @bot.on_message(filters.command("status") & owner)
    async def open_status(_: Client, message: Message) -> None:
        """Show the status screen directly, without going through the menu."""
        try:
            router.pending.pop(message.from_user.id, None)
            await reply_screen(message, router.overview())
        except Exception:
            logger.exception("Control bot: failed to open the status screen.")

    @bot.on_message(filters.command("chats") & owner)
    async def open_chats(_: Client, message: Message) -> None:
        """Show the chat list directly."""
        try:
            router.pending.pop(message.from_user.id, None)
            await reply_screen(message, router.chats_screen())
        except Exception:
            logger.exception("Control bot: failed to open the chat list.")

    @bot.on_callback_query(_owner_callback_filter(owner_id))
    async def on_callback(_: Client, callback_query: CallbackQuery) -> None:
        """Route an inline-button tap to the matching screen or config change."""
        data = str(callback_query.data or "")
        # Acknowledge first: routing may take a while (e.g. fetching dialogs)
        # and Telegram drops unanswered callback queries after a short timeout.
        try:
            await callback_query.answer()
        except Exception:
            logger.debug("Control bot: could not acknowledge callback %r.", data, exc_info=True)
        try:
            screen = await router.route(data, callback_query.from_user.id)
            if screen is not None:
                text, markup = screen
                try:
                    await callback_query.edit_message_text(text, reply_markup=markup)
                except MessageNotModified:
                    # A refresh that renders the identical screen is fine.
                    pass
        except Exception:
            logger.exception("Control bot: error handling callback %r.", data)
            try:
                message = callback_query.message
                if message is not None:
                    await message.reply_text("⚠️ Something went wrong. Send /menu to start over.")
            except Exception:
                logger.debug("Control bot: could not report the callback error.", exc_info=True)

    @bot.on_message(filters.text & owner)
    async def on_text(_: Client, message: Message) -> None:
        """Consume free-text input for a pending flow (chat id/@username or prompt)."""
        text = (message.text or "").strip()
        if text.startswith("/"):
            return
        try:
            result = await router.handle_text(message.from_user.id, text)
            if result is None:
                return
            if isinstance(result, str):
                await message.reply_text(result)
                return
            await reply_screen(message, result)
        except Exception:
            logger.exception("Control bot: error handling text input.")
            await message.reply_text("Something went wrong. Send /menu to start over.")

    @bot.on_message(filters.private & ~filters.user(owner_id))
    async def on_stranger(_: Client, message: Message) -> None:
        """Log (but never answer) messages from anyone who is not the owner."""
        user = getattr(message, "from_user", None)
        logger.warning(
            "Control bot: ignoring message from non-owner user id %s (set control_bot.owner_id to this id "
            "if that is you).",
            getattr(user, "id", "?"),
        )
