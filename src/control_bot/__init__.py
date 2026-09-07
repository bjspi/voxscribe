"""Optional control bot: a BotFather bot with an inline-button settings menu.

This is a *separate* Pyrogram client running in bot mode next to the userbot in
the same event loop. It reads and writes the same ``chats.json`` through the
helpers in :mod:`src.helpers`, so every button tap takes effect for the next
voice message immediately. All handlers are locked to a single owner user id
(``control_bot.owner_id`` in ``config.yaml``); nobody else can see or change
anything.
"""
