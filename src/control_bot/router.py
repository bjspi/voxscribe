"""Maps callback data and typed text to rendered screens, applying config changes.

The router is deliberately free of Pyrogram handler plumbing so it can be unit
tested with a fake userbot: :meth:`ControlRouter.route` takes callback data and
returns ``(text, keyboard)``; :meth:`ControlRouter.handle_text` consumes free
text for a pending flow. All persistence goes through :mod:`src.helpers`, the
same functions the slash commands and the voice handler use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup

from src.control_bot.chats import ChatRef, chat_display_name, get_recent_chats, resolve_chat_ref
from src.control_bot.common import PAGE_SIZE, sort_chats
from src.control_bot.keyboards import (
    cancel_keyboard,
    chat_detail_keyboard,
    chats_keyboard,
    confirm_keyboard,
    main_menu_keyboard,
    overview_keyboard,
    prompt_picker_keyboard,
    prompt_view_keyboard,
    prompts_keyboard,
    recent_chats_keyboard,
    template_detail_keyboard,
    templates_keyboard,
)
from src.control_bot.state import KIND_ADD_CHAT, KIND_CUSTOM_PROMPT, PendingAction
from src.control_bot.views import (
    ADD_CHAT_TEXT,
    MAIN_MENU_TEXT,
    TYPE_CHAT_TEXT,
    render_chat_detail_text,
    render_chats_text,
    render_custom_prompt_text,
    render_delete_chat_text,
    render_overview_text,
    render_prompt_picker_text,
    render_prompt_view_text,
    render_prompts_text,
    render_template_detail_text,
    render_templates_text,
)
from src.helpers import (
    ChatConfig,
    delete_chat_config,
    ensure_chat_config,
    get_bot_config_value,
    get_chat_config,
    list_chat_configs,
    new_chat_transcription_default,
    update_chat_config,
)
from src.prompts import (
    MIN_PROMPT_LENGTH,
    PromptTemplate,
    ResolvedPrompt,
    find_prompt_template,
    load_prompt_templates,
    resolve_rephrase_prompt,
)
from src.transcription import get_current_config

logger = logging.getLogger(__name__)

Screen = tuple[str, InlineKeyboardMarkup]
RECENT_CHATS_LIMIT = 20
# Callbacks that continue a pending flow instead of cancelling it.
_FLOW_CALLBACKS = {"nop", "typ"}
_DIRECTIONS = {"in", "out", "both"}


@dataclass(slots=True)
class ChatPrompts:
    """The resolved incoming and outgoing prompts of one chat."""

    incoming: ResolvedPrompt
    outgoing: ResolvedPrompt


class ControlRouter:
    """Stateful screen router for the control bot (one owner, in-memory flows)."""

    def __init__(self, userbot: Client | None) -> None:
        self.userbot = userbot
        self.pending: dict[int, PendingAction] = {}

    # ---- config access -------------------------------------------------------

    @staticmethod
    def _default_prompt() -> str:
        return str(get_bot_config_value(["prompts", "rephrase"], default="") or "")

    @staticmethod
    def _templates() -> list[PromptTemplate]:
        return load_prompt_templates()

    def _prompts_for(self, chat_config: ChatConfig, templates: list[PromptTemplate] | None = None) -> ChatPrompts:
        default_prompt = self._default_prompt()
        templates = self._templates() if templates is None else templates
        return ChatPrompts(
            incoming=resolve_rephrase_prompt(chat_config, "in", default_prompt, templates),
            outgoing=resolve_rephrase_prompt(chat_config, "out", default_prompt, templates),
        )

    @staticmethod
    def _stored_chat(chat_id: str) -> ChatConfig | None:
        """Return a chat's settings (defaults applied) only when it is stored."""
        chats = list_chat_configs()
        return chats.get(chat_id)

    # ---- screens -------------------------------------------------------------

    @staticmethod
    def main_menu() -> Screen:
        return MAIN_MENU_TEXT, main_menu_keyboard()

    def overview(self) -> Screen:
        chats = sort_chats(list_chat_configs())
        runtime = dict(get_current_config())
        templates = self._templates()
        return (
            render_overview_text(chats, runtime, templates, bool(new_chat_transcription_default())),
            overview_keyboard(),
        )

    def chats_screen(self, page: int = 0) -> Screen:
        chats = sort_chats(list_chat_configs())
        pages = max(1, -(-len(chats) // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        window = chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        return render_chats_text(len(chats), page, pages), chats_keyboard(window, page, pages)

    def templates_screen(self) -> Screen:
        templates = self._templates()
        return render_templates_text(templates), templates_keyboard(templates)

    def template_detail(self, key: str) -> Screen:
        template = find_prompt_template(key, self._templates())
        if template is None:
            return self.templates_screen()
        return render_template_detail_text(template, self._default_prompt()), template_detail_keyboard()

    async def chat_detail(self, chat_id: str) -> Screen | None:
        chat_config = self._stored_chat(chat_id)
        if chat_config is None:
            return None
        chat_config = await self._ensure_chat_name(chat_id, chat_config)
        prompts = self._prompts_for(chat_config)
        return (
            render_chat_detail_text(chat_id, chat_config, prompts.incoming, prompts.outgoing),
            chat_detail_keyboard(chat_id, chat_config),
        )

    def prompts_screen(self, chat_id: str) -> Screen | None:
        chat_config = self._stored_chat(chat_id)
        if chat_config is None:
            return None
        prompts = self._prompts_for(chat_config)
        return (
            render_prompts_text(chat_id, chat_config, prompts.incoming, prompts.outgoing),
            prompts_keyboard(chat_id, prompts.incoming, prompts.outgoing),
        )

    def prompt_picker(self, chat_id: str, direction: str) -> Screen | None:
        chat_config = self._stored_chat(chat_id)
        if chat_config is None or direction not in _DIRECTIONS:
            return None
        return (
            render_prompt_picker_text(chat_id, chat_config, direction),
            prompt_picker_keyboard(chat_id, direction, chat_config, self._templates()),
        )

    def prompt_view(self, chat_id: str, direction: str) -> Screen | None:
        chat_config = self._stored_chat(chat_id)
        if chat_config is None or direction not in ("in", "out"):
            return None
        prompts = self._prompts_for(chat_config)
        prompt = prompts.incoming if direction == "in" else prompts.outgoing
        return render_prompt_view_text(chat_id, chat_config, direction, prompt), prompt_view_keyboard(
            chat_id, direction
        )

    async def _ensure_chat_name(self, chat_id: str, chat_config: ChatConfig) -> ChatConfig:
        """Fill in a missing display name through the userbot (best effort)."""
        if chat_config.get("chatname") or self.userbot is None:
            return chat_config
        try:
            chat = await self.userbot.get_chat(int(chat_id))
        except Exception:
            logger.debug("Control bot: could not resolve a name for chat %s.", chat_id, exc_info=True)
            return chat_config
        name = chat_display_name(chat)
        if not name or name == chat_id:
            return chat_config
        updated = update_chat_config(chat_id, {"chatname": name})
        return updated if updated is not None else chat_config

    # ---- mutations -----------------------------------------------------------

    @staticmethod
    def _apply(chat_id: str, changes: ChatConfig) -> bool:
        """Persist changes for a stored chat; False when unknown or unsaved."""
        if chat_id not in list_chat_configs():
            return False
        return update_chat_config(chat_id, changes) is not None

    def _set_prompt_choice(self, chat_id: str, direction: str, template_key: str, custom: str = "") -> bool:
        """Store a template / default / custom prompt for one or both directions."""
        if direction not in _DIRECTIONS:
            return False
        changes: ChatConfig = {}
        for d in ["in", "out"] if direction == "both" else [direction]:
            changes[f"rephrase_template_{d}"] = template_key
            changes[f"rephrase_prompt_{d}"] = custom
        return self._apply(chat_id, changes)

    def _add_chat(self, user_id: int, chosen: ChatRef) -> Screen:
        """Create (or reuse) the chat entry for a picked/typed chat."""
        chat_id = str(chosen.chat_id)
        ensure_chat_config(chat_id, chosen.chat_name)
        if chosen.chat_name and not str(get_chat_config(chat_id).get("chatname") or ""):
            update_chat_config(chat_id, {"chatname": chosen.chat_name})
        self.pending.pop(user_id, None)
        prompts = self._prompts_for(get_chat_config(chat_id))
        chat_config = get_chat_config(chat_id)
        return (
            render_chat_detail_text(chat_id, chat_config, prompts.incoming, prompts.outgoing),
            chat_detail_keyboard(chat_id, chat_config),
        )

    # ---- routing -------------------------------------------------------------

    async def route(self, data: str, user_id: int) -> Screen | None:
        """Map callback data to the next screen, applying any config change first.

        Returns None for inert buttons or when nothing should be re-rendered.
        """
        if data in ("nop", ""):
            return None
        if data not in _FLOW_CALLBACKS and not data.startswith("pick|"):
            # Any navigation cancels a half-finished flow.
            self.pending.pop(user_id, None)

        if data == "home":
            return self.main_menu()
        if data == "ov":
            return self.overview()
        if data == "tpl":
            return self.templates_screen()
        if data == "cadd":
            if self.userbot is None:
                return ADD_CHAT_TEXT, recent_chats_keyboard([])
            try:
                recent = await get_recent_chats(self.userbot, RECENT_CHATS_LIMIT)
            except Exception:
                logger.warning("Control bot: could not fetch recent chats.", exc_info=True)
                recent = []
            self.pending[user_id] = PendingAction(kind=KIND_ADD_CHAT, recent=recent)
            return ADD_CHAT_TEXT, recent_chats_keyboard(recent)
        if data == "cancel":
            return self.chats_screen()
        if data == "typ":
            action = self.pending.get(user_id)
            if action is None or action.kind != KIND_ADD_CHAT:
                return self.chats_screen()
            action.awaiting_text = True
            return TYPE_CHAT_TEXT, cancel_keyboard()

        parts = data.split("|")
        head = parts[0]
        handler: Callable | None = self._callbacks().get(head)
        if handler is None:
            logger.debug("Control bot: unknown callback %r.", data)
            return None
        try:
            return await handler(user_id, parts[1:])
        except (IndexError, ValueError):
            logger.debug("Control bot: malformed callback %r.", data)
            return None

    def _callbacks(self) -> dict[str, Callable]:
        return {
            "cl": self._cb_chat_list,
            "c": self._cb_chat,
            "t": self._cb_toggle_enabled,
            "tdir": self._cb_direction,
            "tmd": self._cb_output,
            "rephrase": self._cb_rephrasing,
            "del": self._cb_delete_voice,
            "prompts": self._cb_prompts,
            "pp": self._cb_prompt_picker,
            "pt": self._cb_prompt_template,
            "pd": self._cb_prompt_default,
            "pc": self._cb_prompt_custom,
            "pv": self._cb_prompt_view,
            "tpl": self._cb_template,
            "pick": self._cb_pick,
            "cdel": self._cb_delete_ask,
            "cdel2": self._cb_delete_confirmed,
        }

    async def _cb_chat_list(self, _: int, args: list[str]) -> Screen:
        return self.chats_screen(int(args[0]) if args else 0)

    async def _cb_chat(self, _: int, args: list[str]) -> Screen:
        return await self.chat_detail(args[0]) or self.chats_screen()

    async def _cb_toggle_enabled(self, _: int, args: list[str]) -> Screen:
        chat_id = args[0]
        chat_config = self._stored_chat(chat_id)
        if chat_config is not None:
            self._apply(chat_id, {"transcription": 0 if chat_config.get("transcription", 1) else 1})
        return await self.chat_detail(chat_id) or self.chats_screen()

    async def _cb_direction(self, _: int, args: list[str]) -> Screen:
        chat_id, direction = args[0], args[1]
        if direction in _DIRECTIONS:
            self._apply(
                chat_id,
                {
                    "transcription_in": int(direction in ("in", "both")),
                    "transcription_out": int(direction in ("out", "both")),
                },
            )
        return await self.chat_detail(chat_id) or self.chats_screen()

    async def _cb_output(self, _: int, args: list[str]) -> Screen:
        chat_id, value = args[0], args[1]
        if value in ("0", "1"):
            self._apply(chat_id, {"markdown_output": int(value)})
        return await self.chat_detail(chat_id) or self.chats_screen()

    async def _cb_rephrasing(self, _: int, args: list[str]) -> Screen:
        chat_id = args[0]
        chat_config = self._stored_chat(chat_id)
        if chat_config is not None:
            self._apply(chat_id, {"rephrasing": 0 if chat_config.get("rephrasing", 1) else 1})
        return await self.chat_detail(chat_id) or self.chats_screen()

    async def _cb_delete_voice(self, _: int, args: list[str]) -> Screen:
        chat_id, direction = args[0], args[1]
        chat_config = self._stored_chat(chat_id)
        if chat_config is not None and direction in ("in", "out"):
            key = "delete_incoming_voice" if direction == "in" else "delete_outgoing_voice"
            self._apply(chat_id, {key: 0 if chat_config.get(key, 0) else 1})
        return await self.chat_detail(chat_id) or self.chats_screen()

    async def _cb_prompts(self, _: int, args: list[str]) -> Screen:
        return self.prompts_screen(args[0]) or self.chats_screen()

    async def _cb_prompt_picker(self, _: int, args: list[str]) -> Screen:
        return self.prompt_picker(args[0], args[1]) or self.chats_screen()

    async def _cb_prompt_template(self, _: int, args: list[str]) -> Screen:
        chat_id, direction, key = args[0], args[1], args[2]
        if find_prompt_template(key, self._templates()) is not None:
            self._set_prompt_choice(chat_id, direction, template_key=key)
        return self.prompts_screen(chat_id) or self.chats_screen()

    async def _cb_prompt_default(self, _: int, args: list[str]) -> Screen:
        chat_id, direction = args[0], args[1]
        self._set_prompt_choice(chat_id, direction, template_key="")
        return self.prompts_screen(chat_id) or self.chats_screen()

    async def _cb_prompt_custom(self, user_id: int, args: list[str]) -> Screen:
        chat_id, direction = args[0], args[1]
        chat_config = self._stored_chat(chat_id)
        if chat_config is None or direction not in _DIRECTIONS:
            return self.chats_screen()
        self.pending[user_id] = PendingAction(
            kind=KIND_CUSTOM_PROMPT, chat_id=chat_id, direction=direction, awaiting_text=True
        )
        return render_custom_prompt_text(chat_id, chat_config, direction), cancel_keyboard(f"prompts|{chat_id}")

    async def _cb_prompt_view(self, _: int, args: list[str]) -> Screen:
        return self.prompt_view(args[0], args[1]) or self.chats_screen()

    async def _cb_template(self, _: int, args: list[str]) -> Screen:
        return self.template_detail(args[0])

    async def _cb_pick(self, user_id: int, args: list[str]) -> Screen | None:
        action = self.pending.get(user_id)
        index = int(args[0])
        if action is None or action.kind != KIND_ADD_CHAT or index >= len(action.recent):
            return None
        return self._add_chat(user_id, action.recent[index])

    async def _cb_delete_ask(self, _: int, args: list[str]) -> Screen:
        chat_id = args[0]
        chat_config = self._stored_chat(chat_id)
        if chat_config is None:
            return self.chats_screen()
        return render_delete_chat_text(chat_id, chat_config), confirm_keyboard(f"cdel2|{chat_id}", f"c|{chat_id}")

    async def _cb_delete_confirmed(self, _: int, args: list[str]) -> Screen:
        delete_chat_config(args[0])
        return self.chats_screen()

    # ---- free text -----------------------------------------------------------

    async def handle_text(self, user_id: int, text: str) -> Screen | str | None:
        """Consume typed input for a pending flow.

        Returns the next screen on success, an error string to show the owner,
        or None when no flow is waiting for text (the message is ignored).
        """
        action = self.pending.get(user_id)
        if action is None or not action.awaiting_text:
            return None
        text = text.strip()

        if action.kind == KIND_CUSTOM_PROMPT:
            if len(text) < MIN_PROMPT_LENGTH:
                return f"⚠️ The prompt must have at least {MIN_PROMPT_LENGTH} characters. Try again, or tap Cancel."
            if not self._set_prompt_choice(action.chat_id, action.direction, template_key="", custom=text):
                self.pending.pop(user_id, None)
                return "⚠️ That chat no longer exists. Send /menu to start over."
            self.pending.pop(user_id, None)
            return self.prompts_screen(action.chat_id) or self.chats_screen()

        if action.kind == KIND_ADD_CHAT:
            if self.userbot is None:
                return "⚠️ The userbot is not available to look up chats right now."
            try:
                chosen = await resolve_chat_ref(self.userbot, text)
            except ValueError as exc:
                return f"⚠️ {exc}"
            except Exception:
                logger.warning("Control bot: could not resolve %r.", text, exc_info=True)
                return "⚠️ Couldn't find that chat. Send a valid id or @username, or tap Cancel."
            return self._add_chat(user_id, chosen)

        self.pending.pop(user_id, None)
        return None
