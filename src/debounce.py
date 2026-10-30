"""Debounce + collect: hold transcripts per sender until they stop sending voices.

Every voice is transcribed right away, but its rephrased output is held back
while the same sender keeps going: a new voice or a "recording voice" status
from them restarts the quiet window. Once the window passes without either,
all collected transcripts of that sender are handed over in one batch.

Telegram only reports other users' recording status, never your own (from
another device). Your own voices therefore have no quiet window: they are held
until something else happens in the chat (see ``request_flush``) or the
maximum wait runs out.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pyrogram import raw, utils

from src.logging import get_logger

logger = get_logger(__name__)

# (chat_id, sender_id) in Pyrogram's chat-id format.
SenderKey = tuple[int, int]

# Telegram drops a "recording" status after about 6 seconds unless the client
# repeats it, which clients do every few seconds while recording.
RECORDING_STATUS_TTL_SECONDS = 6.0
POLL_INTERVAL_SECONDS = 1.0

_RECORDING_ACTIONS = (raw.types.SendMessageRecordAudioAction, raw.types.SendMessageUploadAudioAction)


@dataclass
class _Batch[T]:
    """Transcripts of one sender that wait for the sender to stop."""

    # None: no quiet window, wait for request_flush() or the maximum wait.
    quiet_seconds: float | None
    max_wait_seconds: float
    started_at: float
    last_activity_at: float
    pending: int = 0
    flush_requested: bool = False
    items: list[T] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class DebounceCollector[T]:
    """Collect items per sender and flush them once the sender goes quiet."""

    def __init__(self, on_flush: Callable[[list[T]], Awaitable[None]]) -> None:
        """Create a collector.

        Args:
            on_flush: Called with all collected items of a sender, in arrival order.
        """
        self._on_flush = on_flush
        self._batches: dict[SenderKey, _Batch[T]] = {}
        self._recording_seen_at: dict[SenderKey, float] = {}

    def voice_started(self, key: SenderKey, quiet_seconds: float | None, max_wait_seconds: float) -> None:
        """Register a voice whose transcription is about to start.

        Args:
            key: The sender the voice belongs to.
            quiet_seconds: How long the sender must stay quiet before the flush;
                None waits for ``request_flush`` instead.
            max_wait_seconds: Flush at the latest this long after the first voice.
        """
        now = time.monotonic()
        batch = self._batches.get(key)
        if batch is None:
            batch = _Batch(quiet_seconds, max_wait_seconds, started_at=now, last_activity_at=now)
            self._batches[key] = batch
            batch.task = asyncio.create_task(self._wait_and_flush(key, batch))
        batch.pending += 1
        batch.last_activity_at = now
        # A finished recording is now a voice; do not wait for its stale status.
        self._recording_seen_at.pop(key, None)

    def voice_finished(self, key: SenderKey, item: T | None) -> None:
        """Store a finished transcription, or drop the slot when it failed.

        Args:
            key: The sender the voice belongs to.
            item: The transcription result, or None if transcription failed.
        """
        batch = self._batches.get(key)
        if batch is None:
            return
        batch.pending = max(0, batch.pending - 1)
        batch.last_activity_at = time.monotonic()
        if item is not None:
            batch.items.append(item)

    def note_recording(self, key: SenderKey, active: bool) -> None:
        """Record a sender's "recording voice" status while they have a batch open.

        Args:
            key: The sender whose status changed.
            active: True for recording/uploading a voice, False when cancelled.
        """
        if key not in self._batches:
            return
        if active:
            self._recording_seen_at[key] = time.monotonic()
        else:
            self._recording_seen_at.pop(key, None)

    def request_flush(self, key: SenderKey) -> bool:
        """Send a sender's batch as soon as its pending transcriptions are done.

        Args:
            key: The sender whose batch should be sent.

        Returns:
            True if the sender had an open batch.
        """
        batch = self._batches.get(key)
        if batch is None:
            return False
        batch.flush_requested = True
        return True

    def request_flush_chat(self, chat_id: int) -> int:
        """Send every open batch of a chat; return how many there were."""
        return sum(self.request_flush(key) for key in list(self._batches) if key[0] == chat_id)

    def _ready(self, key: SenderKey, batch: _Batch[T], now: float) -> bool:
        """Return whether a batch should be flushed now."""
        if batch.pending:
            return False
        if batch.flush_requested or now - batch.started_at >= batch.max_wait_seconds:
            return True
        if batch.quiet_seconds is None:
            return False
        recording_at = self._recording_seen_at.get(key, 0.0)
        if now - recording_at < RECORDING_STATUS_TTL_SECONDS:
            return False
        return now - max(batch.last_activity_at, recording_at) >= batch.quiet_seconds

    async def _wait_and_flush(self, key: SenderKey, batch: _Batch[T]) -> None:
        """Poll until the sender is quiet, then hand over the collected items."""
        while not self._ready(key, batch, time.monotonic()):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        # New voices from now on start a fresh batch.
        self._batches.pop(key, None)
        self._recording_seen_at.pop(key, None)
        if not batch.items:
            return
        try:
            await self._on_flush(batch.items)
        except Exception:
            logger.exception("Flushing %s collected voice(s) for %s failed", len(batch.items), key)


def recording_status_from_update(update: object) -> tuple[SenderKey, bool] | None:
    """Extract a voice-recording status change from a raw Telegram update.

    Args:
        update: Any raw update received by the client.

    Returns:
        ``((chat_id, user_id), active)`` for voice recording/cancel actions,
        otherwise None.
    """
    if isinstance(update, raw.types.UpdateUserTyping):
        chat_id = user_id = update.user_id
    elif isinstance(update, raw.types.UpdateChatUserTyping):
        chat_id, user_id = -update.chat_id, _user_id_of(update.from_id)
    elif isinstance(update, raw.types.UpdateChannelUserTyping):
        chat_id, user_id = utils.get_channel_id(update.channel_id), _user_id_of(update.from_id)
    else:
        return None
    if isinstance(update.action, _RECORDING_ACTIONS):
        return (chat_id, user_id), True
    if isinstance(update.action, raw.types.SendMessageCancelAction):
        return (chat_id, user_id), False
    return None


def _user_id_of(peer: object) -> int:
    """Return the user id of a raw peer, or 0 for non-user senders."""
    return int(getattr(peer, "user_id", 0) or 0)
