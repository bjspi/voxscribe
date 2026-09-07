from __future__ import annotations

import asyncio
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from pyrogram import Client, filters, idle, raw
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from src.control_bot.service import configure_bot_commands, create_control_bot
from src.handlers import (
    handle_voice,
    set_global_transcription_mode,
    set_prompt,
    set_prompt_in,
    set_prompt_out,
    show_config,
    show_help,
    show_prompt,
    show_prompts,
    show_status,
    toggle_markdown_output,
    toggle_rephrasing,
    toggle_transcription_mode,
    toggle_voice_delete,
)
from src.helpers import get_bot_config_value
from src.logging import LOGS_DIR, get_logger

logger = get_logger(__name__)


def _get_config_string(keys: list[str], default: str = "") -> str:
    """Read a config value as a string.

    Args:
        keys: Ordered key path in ``config.yaml``.
        default: Fallback value when the key is missing or empty.

    Returns:
        The resolved config value as a string.
    """
    value = get_bot_config_value(keys, default=default)
    return str(value if value not in (None, "") else default)


def _get_config_int(keys: list[str], default: int, minimum: int = 1) -> int:
    """Read a config value as an integer with a lower bound.

    Args:
        keys: Ordered key path in ``config.yaml``.
        default: Fallback value when parsing fails.
        minimum: Minimum accepted integer value.

    Returns:
        The parsed integer, clamped to at least ``minimum``.
    """
    value = get_bot_config_value(keys, default=default)
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _get_config_bool(keys: list[str], default: bool) -> bool:
    """Read a config value as a boolean.

    Args:
        keys: Ordered key path in ``config.yaml``.
        default: Fallback value when the key is missing or unrecognised.

    Returns:
        The parsed boolean value.
    """
    value = get_bot_config_value(keys, default=default)
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


API_ID = _get_config_int(["telegram", "api_id"], default=0, minimum=0)
API_HASH = _get_config_string(["telegram", "api_hash"])
ACCOUNT = _get_config_string(["telegram", "account"])
PHONE_NR = _get_config_string(["telegram", "phone_nr"])
HEALTHCHECK_INTERVAL_SECONDS = _get_config_int(["recovery", "telegram_healthcheck_interval_seconds"], default=60)
HEALTHCHECK_TIMEOUT_SECONDS = _get_config_int(["recovery", "telegram_healthcheck_timeout_seconds"], default=20)
HEALTHCHECK_MAX_FAILURES = _get_config_int(["recovery", "telegram_healthcheck_max_failures"], default=5)
# How long the connection may stay unhealthy before the watchdog gives up. A
# pure failure count restarts the process every few minutes for as long as a WAN
# outage lasts, which buys nothing: Pyrogram keeps retrying the connection on
# its own. The other bots on this host use the same duration-based grace.
HEALTHCHECK_MAX_UNHEALTHY_SECONDS = _get_config_int(
    ["recovery", "telegram_healthcheck_max_unhealthy_seconds"], default=900
)
SHUTDOWN_TIMEOUT_SECONDS = _get_config_int(["recovery", "telegram_shutdown_timeout_seconds"], default=30)
# Captive-portal-style endpoints that answer a bodyless HTTP 204 and are built
# for exactly this kind of continuous probing (no rate limiting, no abuse
# detection). Two vendors, so a single vendor outage is not misread as the
# whole WAN being down.
WAN_PROBE_URLS: tuple[str, ...] = (
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://cp.cloudflare.com/generate_204",
)
WAN_PROBE_TIMEOUT_SECONDS = 5
# When the watchdog gives up on the Telegram connection, should the bot hard-exit
# (so a process manager restarts it) or keep running and rely on auto-reconnect?
# Defaults to True when the key is missing or null (e.g. older configs without
# this setting), so only an explicit `false` disables the hard exit.
WATCHDOG_HARD_EXIT = _get_config_bool(["recovery", "watchdog_hard_exit"], default=True)
PROJECT_DIR = Path(__file__).resolve().parent
SESSION_DIR = PROJECT_DIR / "session"
SESSION_DIR.mkdir(exist_ok=True)


def migrate_legacy_session_files() -> None:
    """Move legacy session files from the project root into the session directory.

    Session files created by older versions lived in the project root. This moves
    them into ``session/`` once, skipping any whose target already exists.
    """
    for pattern in ("*.session", "*.session-journal"):
        for source_path in PROJECT_DIR.glob(pattern):
            target_path = SESSION_DIR / source_path.name
            if target_path.exists():
                logger.warning(
                    "Legacy session file %s was not moved because %s already exists", source_path, target_path
                )
                continue

            source_path.replace(target_path)
            logger.info("Moved legacy session file from %s to %s", source_path, target_path)


def _validate_credentials() -> None:
    """Fail fast with a clear message if required Telegram credentials are missing.

    Checks ``api_id``, ``api_hash`` and ``phone_nr`` from ``config.yaml`` before the
    Pyrogram client is created, so a misconfigured setup produces an actionable
    error instead of an obscure failure deep inside the library.

    Raises:
        SystemExit: If any required credential is missing or invalid.
    """
    missing: list[str] = []
    if API_ID <= 0:
        missing.append("telegram.api_id")
    if not API_HASH:
        missing.append("telegram.api_hash")
    if not PHONE_NR:
        missing.append("telegram.phone_nr")

    if missing:
        logger.error(
            "Missing/invalid Telegram credentials in config.yaml: %s. "
            "Copy config.example.yaml to config.yaml and fill in the values from "
            "https://my.telegram.org/ before starting the bot.",
            ", ".join(missing),
        )
        sys.exit(1)


_validate_credentials()

migrate_legacy_session_files()
SESSION_NAME = str(SESSION_DIR / (ACCOUNT or "userbot_session"))

# Initialize the client
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, phone_number=PHONE_NR)
app.set_parse_mode(ParseMode.HTML)

# Optional BotFather control bot with an inline-button settings menu. None when
# control_bot.token is not configured; it shares the userbot's event loop.
control_bot: Client | None = create_control_bot(app, API_ID, API_HASH, SESSION_DIR)

# Command handlers fire for both normal messages and scheduled messages
# (UpdateNewScheduledMessage). `~filters.from_scheduled` defensively prevents a
# second run if a scheduled command ever slipped through and got auto-sent.
_CMD_BASE = filters.me & ~filters.from_scheduled


# Register command handlers
@app.on_message(filters.command("helpv") & _CMD_BASE)
async def help_handler(client: Client, message: Message) -> None:
    """Handle ``/helpv``: show the help message and current settings."""
    await show_help(client, message)


@app.on_message(filters.command("statusv") & _CMD_BASE)
async def status_handler(client: Client, message: Message) -> None:
    """Handle ``/statusv``: show the current transcription status panel."""
    await show_status(client, message)


@app.on_message(filters.command("vox") & _CMD_BASE)
async def config_handler(client: Client, message: Message) -> None:
    """Handle ``/vox``: show every setting of this chat with the command that changes it."""
    await show_config(client, message)


@app.on_message(filters.command("prompt") & _CMD_BASE)
async def prompt_handler(client: Client, message: Message) -> None:
    """Handle ``/prompt``: show the active rephrasing prompts."""
    await show_prompt(client, message)


@app.on_message(filters.command("prompts") & _CMD_BASE)
async def prompts_handler(client: Client, message: Message) -> None:
    """Handle ``/prompts``: show the prompts overview (custom vs. default)."""
    await show_prompts(client, message)


@app.on_message(filters.command("setprompt") & _CMD_BASE)
async def setprompt_handler(client: Client, message: Message) -> None:
    """Handle ``/setprompt``: set a custom rephrasing prompt for both directions."""
    await set_prompt(client, message)


@app.on_message(filters.command("setprompt_in") & _CMD_BASE)
async def setprompt_in_handler(client: Client, message: Message) -> None:
    """Handle ``/setprompt_in``: set a custom rephrasing prompt for incoming voices."""
    await set_prompt_in(client, message)


@app.on_message(filters.command("setprompt_out") & _CMD_BASE)
async def setprompt_out_handler(client: Client, message: Message) -> None:
    """Handle ``/setprompt_out``: set a custom rephrasing prompt for outgoing voices."""
    await set_prompt_out(client, message)


@app.on_message(filters.command(["tin", "tout"]) & _CMD_BASE)
async def toggle_transcription_handler(client: Client, message: Message) -> None:
    """Handle ``/tin`` and ``/tout``: toggle per-direction transcription."""
    await toggle_transcription_mode(client, message)


@app.on_message(filters.command(["ton", "toff"]) & _CMD_BASE)
async def set_global_transcription_handler(client: Client, message: Message) -> None:
    """Handle ``/ton`` and ``/toff``: enable or disable transcription for the chat."""
    await set_global_transcription_mode(client, message)


@app.on_message(filters.command("rephrase") & _CMD_BASE)
async def rephrase_handler(client: Client, message: Message) -> None:
    """Handle ``/rephrase``: toggle AI rephrasing of transcriptions."""
    await toggle_rephrasing(client, message)


@app.on_message(filters.command("tmd") & _CMD_BASE)
async def markdown_handler(client: Client, message: Message) -> None:
    """Handle ``/tmd [on|off]``: choose Markdown files for this chat."""
    await toggle_markdown_output(client, message)


@app.on_message(filters.command(["delin", "delout"]) & _CMD_BASE)
async def toggle_delete_handler(client: Client, message: Message) -> None:
    """Handle ``/delin`` and ``/delout``: toggle deletion of voices after transcription."""
    await toggle_voice_delete(client, message)


# Voice handler only fires for actual voice messages, never for scheduled ones
# (would otherwise transcribe twice: once at schedule time, once when sent).
@app.on_message(filters.voice & ~filters.scheduled)
async def voice_handler(client: Client, message: Message) -> None:
    """Handle every incoming/outgoing voice message and route it to transcription."""
    await handle_voice(client, message)


def _is_pyrogram_shutdown_error(exc: OSError) -> bool:
    """Return True for the benign Pyrogram error raised during a torn-down shutdown.

    Args:
        exc: The OSError raised while stopping the client.

    Returns:
        True if the error is Pyrogram's known harmless shutdown race.
    """
    return "Value after * must be an iterable, not NoneType" in str(exc)


class ConnectionHealthError(RuntimeError):
    """Raised when the Telegram connection cannot recover, signalling a hard restart."""


def _wan_probe(urls: tuple[str, ...] = WAN_PROBE_URLS, timeout: float = WAN_PROBE_TIMEOUT_SECONDS) -> bool:
    """Blocking WAN probe used to classify a failed Telegram healthcheck.

    Args:
        urls: Probe endpoints tried in order.
        timeout: Per-request timeout in seconds.

    Returns:
        True when any probe URL answers with HTTP 204 (the internet is up).
    """
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status == 204:
                    return True
        except OSError:
            continue
    return False


async def wan_is_up() -> bool:
    """Run the blocking WAN probe off the event loop.

    Returns:
        True when the internet connection itself is working.
    """
    return await asyncio.to_thread(_wan_probe)


async def connection_watchdog(client: Client) -> None:
    """Periodically ping Telegram and react to an unrecoverable connection failure.

    Pings the Telegram connection on a fixed interval. A failed check triggers a
    WAN probe (:func:`wan_is_up`, only ever called while Telegram is already
    unreachable) that classifies the outage: if the internet itself is down, the
    give-up clock pauses — a restart cannot bring the line back, and Pyrogram
    reconnects on its own once it returns. The watchdog only gives up when both
    conditions hold with a *working* internet connection: enough consecutive
    failures to rule out a single blip (``HEALTHCHECK_MAX_FAILURES``) and
    ``HEALTHCHECK_MAX_UNHEALTHY_SECONDS`` of continuous Telegram-only failure —
    the stuck-session case a restart actually repairs.

    Giving up then depends on ``WATCHDOG_HARD_EXIT`` (config
    ``recovery.watchdog_hard_exit``): if enabled (default) it raises
    ``ConnectionHealthError`` so the process can exit and be restarted by a
    process manager; if disabled it logs the failure, resets the counters and
    keeps monitoring, relying on Pyrogram's auto-reconnect.

    Args:
        client: The running Pyrogram client to health-check.

    Raises:
        ConnectionHealthError: After Telegram stays unreachable despite a working
            internet connection, unless ``WATCHDOG_HARD_EXIT`` is disabled.
    """
    failures = 0
    unhealthy_since: float | None = None
    telegram_only_since: float | None = None

    while True:
        await asyncio.sleep(HEALTHCHECK_INTERVAL_SECONDS)

        try:
            await asyncio.wait_for(client.invoke(raw.functions.updates.GetState()), timeout=HEALTHCHECK_TIMEOUT_SECONDS)
            if failures:
                logger.info(
                    "Telegram healthcheck recovered after %s failed checks (%.0fs unhealthy)",
                    failures,
                    time.monotonic() - (unhealthy_since or time.monotonic()),
                )
            failures = 0
            unhealthy_since = None
            telegram_only_since = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            now = time.monotonic()
            if unhealthy_since is None:
                unhealthy_since = now
            unhealthy_for = now - unhealthy_since
            wan_up = await wan_is_up()
            if wan_up:
                if telegram_only_since is None:
                    telegram_only_since = now
                telegram_only_for = now - telegram_only_since
            else:
                # A dead line is not something a restart can fix: hold the
                # give-up clock until the WAN is back.
                telegram_only_since = None
                telegram_only_for = 0.0
            # Both must be true: enough failures to rule out a single blip, and
            # long enough Telegram-only failure to rule out a short outage
            # Pyrogram will ride out by itself. Restarting every five minutes
            # through a two-hour WAN outage only churns the process.
            give_up = failures >= HEALTHCHECK_MAX_FAILURES and telegram_only_for >= HEALTHCHECK_MAX_UNHEALTHY_SECONDS
            logger.warning(
                "Telegram healthcheck failed %s/%s (%.0fs/%ss unhealthy, internet %s): %s",
                failures,
                HEALTHCHECK_MAX_FAILURES,
                unhealthy_for,
                HEALTHCHECK_MAX_UNHEALTHY_SECONDS,
                "up" if wan_up else "down",
                exc,
                exc_info=give_up,
            )

            if give_up:
                if WATCHDOG_HARD_EXIT:
                    raise ConnectionHealthError(
                        f"Telegram connection did not recover within {unhealthy_for:.0f}s "
                        f"({failures} healthcheck failures)"
                    ) from exc
                logger.error(
                    "Telegram connection did not recover within %.0fs (%s healthcheck failures); "
                    "watchdog hard-exit is disabled, continuing to monitor and relying on "
                    "Pyrogram auto-reconnect",
                    unhealthy_for,
                    failures,
                )
                failures = 0
                unhealthy_since = None
                telegram_only_since = None


async def main() -> None:
    """Start the client, run the idle loop and watchdog, and shut down cleanly.

    Runs the Pyrogram idle loop alongside the connection watchdog and waits for
    whichever finishes first. On a watchdog failure it cancels the remaining tasks,
    re-raises ``ConnectionHealthError`` and always stops the client in cleanup.

    Raises:
        ConnectionHealthError: Propagated from the watchdog to trigger a restart.
    """
    await app.start()
    logger.info("Bot started")
    control = control_bot
    if control is not None:
        try:
            await control.start()
            await configure_bot_commands(control)
            logger.info("Control bot started")
        except Exception:
            # A bad token must not take the userbot down: continue without the menu.
            logger.exception("Control bot failed to start; continuing without it")
            control = None
    idle_task = asyncio.create_task(idle(), name="pyrogram-idle")
    watchdog_task = asyncio.create_task(connection_watchdog(app), name="telegram-connection-watchdog")
    healthcheck_failed = False

    try:
        done, pending = await asyncio.wait({idle_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            try:
                await task
            except ConnectionHealthError:
                healthcheck_failed = True
                raise
    finally:
        if control is not None and control.is_connected:
            try:
                await asyncio.wait_for(control.stop(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Control bot could not be stopped cleanly", exc_info=True)
        try:
            await asyncio.wait_for(app.stop(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("Timed out while stopping Pyrogram client")
        except OSError as exc:
            if healthcheck_failed or _is_pyrogram_shutdown_error(exc):
                logger.warning("Ignoring Pyrogram shutdown error: %s", exc)
            else:
                raise


# ==== START ====
if __name__ == "__main__":
    try:
        logger.info("Starting Telegram Voice Transcription Bot")
        app.run(main())
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)
        # Create logs directory if it doesn't exist
        logs_dir = LOGS_DIR
        logs_dir.mkdir(exist_ok=True)

        crash_log_path = logs_dir / f"log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        with open(crash_log_path, "w+", encoding="utf-8") as f:
            f.write(str(e) + "\n\n" + "#" * 30 + "\n\n")
            f.write(traceback.format_exc())

        sys.exit(1)
