"""Inline-keyboard builders for the control bot menu.

Callback-data scheme (``|``-separated, well under Telegram's 64-byte limit).
The heads mirror the slash commands where they map one-to-one (``t`` for
``/ton``/``/toff``, ``tdir`` for ``/tin``/``/tout``, ``tmd``, ``rephrase``,
``del`` for ``/delin``/``/delout``, ``prompts``); the prompt-picker heads stay
two letters because template keys (up to 24 characters) ride along.

- ``home``                       — main menu
- ``ov``                         — status / overview screen
- ``cl|<page>``                  — chat list (paged)
- ``c|<cid>``                    — one chat's settings screen
- ``t|<cid>``                    — toggle the chat's master switch (/ton, /toff)
- ``tdir|<cid>|<in|out|both>``   — set the transcription direction (/tin, /tout)
- ``tmd|<cid>|<0|1>``            — output: 0 = inline text, 1 = Markdown file (/tmd)
- ``rephrase|<cid>``             — toggle AI rephrasing (/rephrase)
- ``del|<cid>|<in|out>``         — toggle deleting the voice afterwards (/delin, /delout)
- ``prompts|<cid>``              — the chat's prompt screen (/prompts)
- ``pp|<cid>|<in|out|both>``     — prompt picker for one direction (or both)
- ``pt|<cid>|<dir>|<key>``       — use a prompt template
- ``pd|<cid>|<dir>``             — use the global default prompt
- ``pc|<cid>|<dir>``             — type a custom prompt (text input)
- ``pv|<cid>|<in|out>``          — show the full active prompt
- ``tpl`` / ``tpl|<key>``        — templates list / one template's full text
- ``cadd`` / ``pick|<i>`` / ``typ`` / ``cancel`` — add-chat flow
- ``cdel|<cid>`` / ``cdel2|<cid>`` — delete a chat (confirm screen, confirmed)
- ``nop``                        — inert label button
"""

from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.control_bot.chats import ChatRef
from src.control_bot.common import (
    chat_label,
    direction_of,
    is_enabled,
    rephrasing_on,
    state_icon,
    uses_markdown,
)
from src.helpers import ChatConfig
from src.prompts import PromptTemplate, ResolvedPrompt


def _mark(active: bool, label: str) -> str:
    """Prefix a button label with a check mark when the option is active."""
    return f"✅ {label}" if active else f"▫️ {label}"


def _btn(text: str, data: str) -> InlineKeyboardButton:
    """Shorthand for a callback button."""
    return InlineKeyboardButton(text, callback_data=data)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the top-level menu keyboard."""
    return InlineKeyboardMarkup(
        [
            [_btn("📊 Status", "ov")],
            [_btn("💬 Chats", "cl|0"), _btn("🧩 Prompt templates", "tpl")],
        ]
    )


def overview_keyboard() -> InlineKeyboardMarkup:
    """Build the status screen keyboard with a refresh shortcut."""
    return InlineKeyboardMarkup(
        [
            [_btn("🔄 Refresh", "ov")],
            [_btn("💬 Chats", "cl|0"), _btn("🧩 Prompt templates", "tpl")],
            [_btn("⬅️ Back", "home")],
        ]
    )


def chats_keyboard(chats: list[tuple[str, ChatConfig]], page: int, pages: int) -> InlineKeyboardMarkup:
    """Build one page of the chat list, one button per chat."""
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, chat_config in chats:
        output = "📄" if uses_markdown(chat_config) else "💬"
        rows.append(
            [
                _btn(
                    f"{state_icon(is_enabled(chat_config))} {chat_label(chat_id, chat_config)} "
                    f"· {direction_of(chat_config)} · {output}",
                    f"c|{chat_id}",
                )
            ]
        )
    if not rows:
        rows.append([_btn("No chats configured yet", "nop")])
    if pages > 1:
        rows.append(
            [
                _btn("◀️" if page > 0 else "·", f"cl|{page - 1}" if page > 0 else "nop"),
                _btn(f"{page + 1}/{pages}", "nop"),
                _btn("▶️" if page < pages - 1 else "·", f"cl|{page + 1}" if page < pages - 1 else "nop"),
            ]
        )
    rows.append([_btn("➕ Add chat", "cadd")])
    rows.append([_btn("⬅️ Back", "home")])
    return InlineKeyboardMarkup(rows)


def chat_detail_keyboard(chat_id: str, chat_config: ChatConfig, back_page: int = 0) -> InlineKeyboardMarkup:
    """Build the per-chat settings keyboard with live toggle states."""
    cid = chat_id
    direction = direction_of(chat_config)
    markdown = uses_markdown(chat_config)
    delete_in = bool(chat_config.get("delete_incoming_voice", 0))
    delete_out = bool(chat_config.get("delete_outgoing_voice", 0))

    rows: list[list[InlineKeyboardButton]] = [
        [_btn("⏸ Pause chat" if is_enabled(chat_config) else "▶️ Resume chat", f"t|{cid}")],
        [_btn("— Direction —", "nop")],
        [
            _btn(_mark(direction == "in", "Incoming"), f"tdir|{cid}|in"),
            _btn(_mark(direction == "out", "Outgoing"), f"tdir|{cid}|out"),
            _btn(_mark(direction == "both", "Both"), f"tdir|{cid}|both"),
        ],
        [_btn("— Output —", "nop")],
        [
            _btn(_mark(not markdown, "💬 Inline text"), f"tmd|{cid}|0"),
            _btn(_mark(markdown, "📄 Markdown file"), f"tmd|{cid}|1"),
        ],
        [_btn("— Rephrasing —", "nop")],
        [_btn(_mark(rephrasing_on(chat_config), "🧠 AI rephrasing"), f"rephrase|{cid}")],
        [_btn("— Delete voice after transcription —", "nop")],
        [
            _btn(_mark(delete_in, "🗑 Incoming"), f"del|{cid}|in"),
            _btn(_mark(delete_out, "🗑 Outgoing"), f"del|{cid}|out"),
        ],
        [_btn("🧩 Prompts", f"prompts|{cid}")],
        [_btn("🗑 Delete chat", f"cdel|{cid}")],
        [_btn("⬅️ Back", f"cl|{back_page}")],
    ]
    return InlineKeyboardMarkup(rows)


def prompts_keyboard(chat_id: str, prompt_in: ResolvedPrompt, prompt_out: ResolvedPrompt) -> InlineKeyboardMarkup:
    """Build the per-chat prompt screen: pick per direction, or view the full text."""
    cid = chat_id
    return InlineKeyboardMarkup(
        [
            [_btn(f"📥 Incoming: {prompt_in.label}", f"pp|{cid}|in")],
            [_btn(f"📤 Outgoing: {prompt_out.label}", f"pp|{cid}|out")],
            [_btn("🔁 Set both directions", f"pp|{cid}|both")],
            [_btn("👁 View incoming", f"pv|{cid}|in"), _btn("👁 View outgoing", f"pv|{cid}|out")],
            [_btn("⬅️ Back", f"c|{cid}")],
        ]
    )


def prompt_picker_keyboard(
    chat_id: str,
    direction: str,
    chat_config: ChatConfig,
    templates: list[PromptTemplate],
) -> InlineKeyboardMarkup:
    """Build the template picker for one direction (or both at once)."""
    cid = chat_id
    directions = ["in", "out"] if direction == "both" else [direction]

    def active(predicate) -> bool:
        """True when the predicate holds for every affected direction."""
        return all(predicate(d) for d in directions)

    def custom(d: str) -> bool:
        return len(str(chat_config.get(f"rephrase_prompt_{d}") or "").strip()) >= 10

    def template_key(d: str) -> str:
        return "" if custom(d) else str(chat_config.get(f"rephrase_template_{d}") or "")

    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn(
                _mark(active(lambda d: not custom(d) and not template_key(d)), "🌐 Default (config.yaml)"),
                f"pd|{cid}|{direction}",
            )
        ]
    ]
    for template in templates:
        rows.append(
            [
                _btn(
                    _mark(active(lambda d, key=template.key: template_key(d) == key), template.name),
                    f"pt|{cid}|{direction}|{template.key}",
                )
            ]
        )
    rows.append([_btn(_mark(active(custom), "✍️ Custom text…"), f"pc|{cid}|{direction}")])
    rows.append([_btn("⬅️ Back", f"prompts|{cid}")])
    return InlineKeyboardMarkup(rows)


def prompt_view_keyboard(chat_id: str, direction: str) -> InlineKeyboardMarkup:
    """Build the keyboard under a full prompt view."""
    return InlineKeyboardMarkup(
        [
            [_btn("✏️ Change", f"pp|{chat_id}|{direction}")],
            [_btn("⬅️ Back", f"prompts|{chat_id}")],
        ]
    )


def templates_keyboard(templates: list[PromptTemplate]) -> InlineKeyboardMarkup:
    """Build the global templates list (read-only; edit them in config.yaml)."""
    rows: list[list[InlineKeyboardButton]] = [[_btn(template.name, f"tpl|{template.key}")] for template in templates]
    if not rows:
        rows.append([_btn("No templates configured", "nop")])
    rows.append([_btn("⬅️ Back", "home")])
    return InlineKeyboardMarkup(rows)


def template_detail_keyboard() -> InlineKeyboardMarkup:
    """Build the keyboard under one template's full text."""
    return InlineKeyboardMarkup([[_btn("⬅️ Back", "tpl")]])


def recent_chats_keyboard(recent: list[ChatRef]) -> InlineKeyboardMarkup:
    """Build a picker of recently active chats plus a type/cancel fallback.

    Buttons carry the chat's index in ``recent`` (``pick|<index>``) rather than
    its id, keeping callback data short; the router maps it back via the
    pending action's stored list.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [_btn(f"{index + 1}. {chat.label}", f"pick|{index}")] for index, chat in enumerate(recent)
    ]
    rows.append([_btn("✍️ Type id / @username", "typ")])
    rows.append([_btn("✖️ Cancel", "cancel")])
    return InlineKeyboardMarkup(rows)


def cancel_keyboard(back_data: str = "cancel") -> InlineKeyboardMarkup:
    """Build a single-button cancel keyboard for text-input steps."""
    return InlineKeyboardMarkup([[_btn("✖️ Cancel", back_data)]])


def confirm_keyboard(confirm_data: str, back_data: str) -> InlineKeyboardMarkup:
    """Build a yes/back keyboard for a destructive action's confirm screen."""
    return InlineKeyboardMarkup(
        [
            [_btn("🗑 Yes, delete", confirm_data)],
            [_btn("⬅️ Back", back_data)],
        ]
    )
