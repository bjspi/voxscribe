"""Voice transcription and optional rephrasing for Telegram voice messages."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import textwrap
from html import escape
from typing import Any, BinaryIO, Literal, TypedDict, cast

import openai
from groq import Groq
from pyrogram import Client
from pyrogram.types import Message

from src.helpers import YamlConfig, get_chat_config, get_chat_info, get_config_value, load_bot_config
from src.logging import get_logger

# Set up logging
logger = get_logger(__name__)

# When verbose logging is off, message content is logged only up to this many
# characters (privacy + smaller log files).
LOG_PREVIEW_CHARS = 200

ProviderName = Literal["GROQ", "OPENAI"]
ProviderErrorCode = Literal[
    "dns",
    "timeout",
    "auth",
    "rate_limit",
    "provider_unavailable",
    "connection",
    "unknown",
]


class RuntimeConfig(TypedDict):
    """Resolved runtime settings used by one transcription workflow."""

    transcription_provider: ProviderName
    rephrase_provider: ProviderName
    openai_api_key: str
    groq_api_key: str
    transcription_model_openai: str
    rephrase_model_openai: str
    transcription_model_groq: str
    rephrase_model_groq: str
    transcription_prompt: str
    rephrase_prompt: str
    graceful_degradation_enabled: bool
    provider_fallback_enabled: bool
    transcription_retry_count: int
    verbose_logging: bool


class ProviderAttempt(TypedDict):
    """One attempted provider call captured for logging and failure summaries."""

    provider: ProviderName
    attempt: int
    error_code: ProviderErrorCode
    hint: str


class TranscriptionRecoveryResult(TypedDict):
    """Successful transcription result returned by the retry/fallback layer."""

    transcript: Any
    provider_name: str
    model_used: str
    provider_used: ProviderName
    fallback_used: bool
    provider_attempts: list[ProviderAttempt]


def _config_string(config: YamlConfig, keys: list[str], default: str = "") -> str:
    """Read a config value and coerce it to a string.

    Args:
        config: Parsed YAML configuration.
        keys: Ordered key path to read from the config.
        default: Fallback value when the key is missing or empty.

    Returns:
        The resolved value as a string.
    """
    value = get_config_value(config, keys, default=default)
    return str(value if value not in (None, "") else default)


def _log_content(text: str, verbose: bool) -> str:
    """Return message content for logging, redacted unless verbose logging is on.

    Args:
        text: The (potentially sensitive) content to log.
        verbose: Whether full content logging is enabled.

    Returns:
        The full text when ``verbose`` is True, otherwise a short preview with the
        number of redacted characters appended.
    """
    text = text or ""
    if verbose or len(text) <= LOG_PREVIEW_CHARS:
        return text
    return f"{text[:LOG_PREVIEW_CHARS]}… [+{len(text) - LOG_PREVIEW_CHARS} chars redacted]"


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce common YAML/env-style values into a boolean.

    Args:
        value: Raw value to interpret.
        default: Fallback value for unknown strings or empty values.

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


def _coerce_positive_int(value: object, default: int) -> int:
    """Coerce a raw value into a positive integer.

    Args:
        value: Raw value to parse.
        default: Fallback value when parsing fails or the value is below one.

    Returns:
        A positive integer.
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return max(1, default)
    return parsed if parsed > 0 else max(1, default)


def _format_voice_duration(duration_seconds: int | float | None) -> str:
    """Format a voice duration as minutes and seconds.

    Args:
        duration_seconds: The voice note length in seconds (may be None).

    Returns:
        The duration formatted as ``M:SS``.
    """
    try:
        total_seconds = max(0, int(duration_seconds or 0))
    except (TypeError, ValueError):
        total_seconds = 0

    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _get_sender_label(message: Message) -> str:
    """Return the best available sender label for a Telegram message.

    Args:
        message: The Telegram message whose sender should be displayed.

    Returns:
        Username, full name, sender chat title, chat name or ``"Unknown"``.
    """
    user = getattr(message, "from_user", None)
    if user:
        if getattr(user, "username", None):
            return user.username

        full_name = " ".join(
            name for name in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if name
        ).strip()
        if full_name:
            return full_name

        if getattr(user, "id", None):
            return str(user.id)

    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "title", None):
        return sender_chat.title

    chat = getattr(message, "chat", None)
    if chat:
        return getattr(chat, "first_name", None) or getattr(chat, "title", None) or str(getattr(chat, "id", "Unknown"))

    return "Unknown"


def _build_voice_reference(message: Message) -> str:
    """Build the sender and duration reference for a voice transcript.

    Args:
        message: The voice message being transcribed.

    Returns:
        A compact reference such as ``"alice (0:42)"``.
    """
    voice = getattr(message, "voice", None)
    duration = _format_voice_duration(getattr(voice, "duration", 0))
    return f"{_get_sender_label(message)} ({duration})"


def _collect_exception_messages(exc: Exception) -> list[str]:
    """Flatten an exception chain into lowercase messages for classification.

    Args:
        exc: The exception to inspect.

    Returns:
        Lowercase messages from the exception, its cause and its context.
    """
    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc

    while current and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        if message:
            messages.append(message.lower())
        current = current.__cause__ or current.__context__

    return messages


def _classify_provider_error(exc: Exception) -> tuple[ProviderErrorCode, str]:
    """Classify a provider exception for retries, logging and user feedback.

    Args:
        exc: The provider exception to classify.

    Returns:
        A stable internal error code and a short user-facing hint.
    """
    combined = " | ".join(_collect_exception_messages(exc))

    if any(
        token in combined
        for token in [
            "temporary failure in name resolution",
            "name resolution",
            "nodename nor servname",
            "failed to resolve",
            "getaddrinfo",
        ]
    ):
        return "dns", "DNS/host resolution failed"
    if "timed out" in combined or "timeout" in combined:
        return "timeout", "API request timed out"
    if any(
        token in combined
        for token in ["401", "unauthorized", "authentication", "invalid api key", "api key", "not configured"]
    ):
        return "auth", "Authentication failed"
    if any(token in combined for token in ["429", "rate limit", "too many requests"]):
        return "rate_limit", "Rate limit reached"
    if any(token in combined for token in ["500", "502", "503", "504", "service unavailable", "bad gateway"]):
        return "provider_unavailable", "Provider is currently unavailable"
    if any(
        token in combined
        for token in ["connect error", "connection error", "connection aborted", "network is unreachable"]
    ):
        return "connection", "Network connection to the provider failed"

    return "unknown", "Unknown API error"


def _is_retryable_error(error_code: ProviderErrorCode) -> bool:
    """Return whether an error should be retried or fall back to another provider.

    Args:
        error_code: Stable provider error code returned by
            ``_classify_provider_error``.

    Returns:
        True for transient failures, false for configuration/auth/permanent
        failures.
    """
    return error_code in {"dns", "timeout", "provider_unavailable", "connection", "rate_limit"}


def _normalize_provider(provider: object, default: ProviderName = "GROQ") -> ProviderName:
    """Normalize provider names to supported literal values.

    Args:
        provider: Raw provider value from config.
        default: Provider returned when ``provider`` is empty or unsupported.

    Returns:
        ``"GROQ"`` or ``"OPENAI"``.
    """
    normalized = str(provider or default or "").strip().upper()
    return cast(ProviderName, normalized) if normalized in {"GROQ", "OPENAI"} else default


def _get_provider_candidates(primary_provider: ProviderName, current_config: RuntimeConfig) -> list[ProviderName]:
    """Return provider order, with optional automatic fallback if both keys are present.

    Args:
        primary_provider: The provider to try first (``GROQ`` or ``OPENAI``).
        current_config: The resolved runtime configuration.

    Returns:
        Provider names in the order they should be attempted.
    """
    primary_provider = _normalize_provider(primary_provider)
    providers: list[ProviderName] = [primary_provider]
    alternate_provider: ProviderName = "OPENAI" if primary_provider == "GROQ" else "GROQ"

    if current_config.get("provider_fallback_enabled", True):
        if alternate_provider == "OPENAI" and current_config.get("openai_api_key"):
            providers.append(alternate_provider)
        elif alternate_provider == "GROQ" and current_config.get("groq_api_key"):
            providers.append(alternate_provider)

    return providers


def _transcribe_with_provider(
    provider: ProviderName,
    current_config: RuntimeConfig,
    groq_client: Groq | None,
    openai_client: openai.OpenAI | None,
    audio_file: BinaryIO,
) -> tuple[Any, str, str]:
    """Run the transcription request with the selected provider.

    Args:
        provider: The provider to use (``GROQ`` or ``OPENAI``).
        current_config: The resolved runtime configuration.
        groq_client: An initialized Groq client (required for the GROQ provider).
        openai_client: An initialized OpenAI client (required for the OPENAI provider).
        audio_file: The opened audio file to transcribe.

    Returns:
        A tuple of ``(transcript, provider_label, model_name)``.
    """
    audio_file.seek(0)

    if provider == "GROQ":
        if groq_client is None:
            raise RuntimeError("GROQ client is not configured; check the Groq API key")
        transcript: Any = groq_client.audio.transcriptions.create(
            model=current_config["transcription_model_groq"],
            file=audio_file,
            prompt=current_config["transcription_prompt"],
        )
        return transcript, "GROQ (Transcription)", current_config["transcription_model_groq"]

    if openai_client is None:
        raise RuntimeError("OpenAI client is not configured; check the OpenAI API key")
    transcript = openai_client.audio.transcriptions.create(
        model=current_config["transcription_model_openai"],
        file=audio_file,
        prompt=current_config["transcription_prompt"],
    )
    return transcript, "OPENAI (Transcription)", current_config["transcription_model_openai"]


def _rephrase_with_provider(
    provider: ProviderName,
    current_config: RuntimeConfig,
    groq_client: Groq | None,
    openai_client: openai.OpenAI | None,
    system_prompt: str,
    text: str,
) -> tuple[str, str, str]:
    """Run the rephrasing request with the selected provider.

    Args:
        provider: The provider to use (``GROQ`` or ``OPENAI``).
        current_config: The resolved runtime configuration.
        groq_client: An initialized Groq client (required for the GROQ provider).
        openai_client: An initialized OpenAI client (required for the OPENAI provider).
        system_prompt: The system prompt steering the rephrasing.
        text: The transcript text to rephrase.

    Returns:
        A tuple of ``(rephrased_text, provider_label, model_name)``.
    """
    if provider == "GROQ":
        if groq_client is None:
            raise RuntimeError("GROQ client is not configured; check the Groq API key")
        client: Any = groq_client
        model = current_config["rephrase_model_groq"]
        provider_name = "GROQ (Rephrasing)"
    else:
        if openai_client is None:
            raise RuntimeError("OpenAI client is not configured; check the OpenAI API key")
        client = openai_client
        model = current_config["rephrase_model_openai"]
        provider_name = "OPENAI (Rephrasing)"

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        stream=False,
    )

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"{provider} returned an empty rephrasing response")

    return content.strip(), provider_name, model


async def _run_transcription_with_recovery(
    current_config: RuntimeConfig,
    groq_client: Groq | None,
    openai_client: openai.OpenAI | None,
    audio_file: BinaryIO,
    chat_info: str,
) -> TranscriptionRecoveryResult:
    """Retry transcription on the primary provider and optionally fall back to the alternate one.

    Args:
        current_config: The resolved runtime configuration.
        groq_client: An initialized Groq client, or None if unavailable.
        openai_client: An initialized OpenAI client, or None if unavailable.
        audio_file: The opened audio file to transcribe.
        chat_info: Human-readable chat identifier for logging.

    Returns:
        A result dict with the transcript, provider/model used, fallback flag and
        the list of per-provider attempts.

    Raises:
        RuntimeError: If every provider attempt fails.
    """
    primary_provider = _normalize_provider(current_config["transcription_provider"])
    retry_count = max(1, current_config["transcription_retry_count"])
    provider_attempts: list[ProviderAttempt] = []
    last_error: Exception | None = None
    last_error_code: ProviderErrorCode = "unknown"

    for provider_idx, provider in enumerate(_get_provider_candidates(primary_provider, current_config)):
        for attempt_idx in range(retry_count):
            try:
                transcript, provider_name, model_used = await asyncio.to_thread(
                    _transcribe_with_provider,
                    provider,
                    current_config,
                    groq_client,
                    openai_client,
                    audio_file,
                )
                return {
                    "transcript": transcript,
                    "provider_name": provider_name,
                    "model_used": model_used,
                    "provider_used": provider,
                    "fallback_used": provider_idx > 0,
                    "provider_attempts": provider_attempts,
                }
            except Exception as provider_exc:
                last_error = provider_exc
                last_error_code, last_error_hint = _classify_provider_error(provider_exc)
                provider_attempts.append(
                    {
                        "provider": provider,
                        "attempt": attempt_idx + 1,
                        "error_code": last_error_code,
                        "hint": last_error_hint,
                    }
                )
                logger.warning(
                    f"Transcription attempt failed in {chat_info}: provider={provider}, attempt={attempt_idx + 1}/{retry_count}, code={last_error_code}",
                    exc_info=True,
                )

                if not _is_retryable_error(last_error_code):
                    break

                if attempt_idx < retry_count - 1:
                    backoff_seconds = round(0.8 * (2**attempt_idx), 2)
                    await asyncio.sleep(backoff_seconds)

        if last_error_code == "auth":
            break

    details = ", ".join(
        f"{attempt['provider']}#{attempt['attempt']}={attempt['error_code']}" for attempt in provider_attempts
    )
    raise RuntimeError(
        f"TRANSCRIPTION_FAILED[{last_error_code}] primary={primary_provider} attempts={details}"
    ) from last_error


def get_current_config() -> RuntimeConfig:
    """Resolve current configuration values for one transcription workflow.

    The YAML file is loaded once per call so runtime changes are still picked up,
    while avoiding repeated file reads for every individual setting.

    Returns:
        A typed runtime configuration dictionary with defaults applied.
    """
    # Try to get separate providers first, fall back to single provider for backward compatibility
    config = load_bot_config()
    provider_root: object = get_config_value(config, ["api", "provider"], default={})
    transcription_provider: object = get_config_value(config, ["api", "provider", "transcription"])
    rephrase_provider: object = get_config_value(config, ["api", "provider", "rephrase"])

    if isinstance(provider_root, str):
        transcription_provider = transcription_provider or provider_root
        rephrase_provider = rephrase_provider or provider_root

    return {
        "transcription_provider": _normalize_provider(transcription_provider, default="GROQ"),
        "rephrase_provider": _normalize_provider(rephrase_provider, default="GROQ"),
        "openai_api_key": _config_string(config, ["api", "keys", "openai"]),
        "groq_api_key": _config_string(config, ["api", "keys", "groq"]),
        "transcription_model_openai": _config_string(
            config, ["models", "openai", "transcription"], default="whisper-1"
        ),
        "rephrase_model_openai": _config_string(config, ["models", "openai", "rephrase"], default="gpt-4o-mini"),
        "transcription_model_groq": _config_string(
            config, ["models", "groq", "transcription"], default="whisper-large-v3"
        ),
        "rephrase_model_groq": _config_string(config, ["models", "groq", "rephrase"], default="openai/gpt-oss-120b"),
        "transcription_prompt": _config_string(config, ["prompts", "transcription"]),
        "rephrase_prompt": _config_string(config, ["prompts", "rephrase"]),
        "graceful_degradation_enabled": _coerce_bool(
            get_config_value(config, ["recovery", "graceful_degradation"], default=True),
            default=True,
        ),
        "provider_fallback_enabled": _coerce_bool(
            get_config_value(config, ["recovery", "provider_fallback"], default=True),
            default=True,
        ),
        "transcription_retry_count": _coerce_positive_int(
            get_config_value(config, ["recovery", "retry_count"], default=3),
            default=3,
        ),
        "verbose_logging": _coerce_bool(
            get_config_value(config, ["logging", "verbose"], default=False),
            default=False,
        ),
    }


async def transcribe_voice(client: Client, message: Message) -> None:
    """Transcribe a voice message, optionally rephrase it and reply with the text.

    This function handles the complete workflow of voice message processing:
    1. Downloads the voice message to a temporary file
    2. Transcribes the audio using the configured API
    3. Optionally rephrases the transcription for better readability
    4. Sends the result as a reply to the original message
    5. Optionally deletes the original voice message

    Args:
        client: The Pyrogram client instance that received the voice message.
        message: The voice message to transcribe.
    """
    # Log that transcription is starting with message details
    chat_info = get_chat_info(message.chat)
    message_direction = "outgoing" if message.outgoing else "incoming"

    # Create a dictionary to collect all logging information
    log_data: dict[str, object] = {
        "event": "transcription_start",
        "chat_info": chat_info,
        "message_direction": message_direction,
    }

    logger.info(f"Transcription Process Details: {json.dumps(log_data, indent=2)}")

    # Load current configuration dynamically for each transcription
    current_config = get_current_config()

    # Initialize API clients based on the current configuration
    openai_client: openai.OpenAI | None = None
    groq_client: Groq | None = None

    # Initialize clients only if they're needed
    needs_groq = (
        current_config["transcription_provider"] == "GROQ"
        or current_config["rephrase_provider"] == "GROQ"
        or current_config["provider_fallback_enabled"]
    )
    needs_openai = (
        current_config["transcription_provider"] == "OPENAI"
        or current_config["rephrase_provider"] == "OPENAI"
        or current_config["provider_fallback_enabled"]
    )

    if needs_groq and current_config["groq_api_key"]:
        groq_client = Groq(api_key=current_config["groq_api_key"])
    if needs_openai and current_config["openai_api_key"]:
        openai_client = openai.OpenAI(api_key=current_config["openai_api_key"])

    chat_config = get_chat_config(str(message.chat.id))
    tmp_path: str | None = None

    try:
        # Determine if we should delete the voice message after processing
        delete_voice_flag = (chat_config.get("delete_outgoing_voice", 0) and message.outgoing) or (
            chat_config.get("delete_incoming_voice", 0) and not message.outgoing
        )

        # Download voice message to a temporary path. The file is closed before
        # Pyrogram writes to it, which avoids Windows file-handle surprises.
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await message.download(file_name=tmp_path)
        with open(tmp_path, "rb") as audio_file:
            transcription_result = await _run_transcription_with_recovery(
                current_config, groq_client, openai_client, audio_file, chat_info
            )
            transcript = transcription_result["transcript"]
            provider_name = transcription_result["provider_name"]
            model_used = transcription_result["model_used"]
            fallback_used = transcription_result["fallback_used"]
            provider_used = transcription_result["provider_used"]
            provider_attempts = transcription_result["provider_attempts"]

        text = str(getattr(transcript, "text", "") or "").strip()
        if not text:
            raise RuntimeError(f"{provider_used} returned an empty transcription response")

        verbose_logging = current_config["verbose_logging"]
        log_data = {
            "event": "transcription_completed",
            "provider": provider_name,
            "chat_info": chat_info,
            "model": model_used,
            "provider_used": provider_used,
            "fallback_used": fallback_used,
            "provider_attempts": provider_attempts,
            "transcription_result_length": len(text),
            "transcription_result": _log_content(text, verbose_logging),
        }
        logger.info(f"Transcription Process Details: {json.dumps(log_data, indent=2)}")

        # Rephrase transcription if enabled
        if chat_config.get("rephrasing", 1):
            # Determine which prompt to use based on message direction
            if message.outgoing:
                # Use outgoing specific prompt if available, otherwise fall back to default
                system_prompt = str(chat_config.get("rephrase_prompt_out") or current_config["rephrase_prompt"])
            else:
                # Use incoming specific prompt if available, otherwise fall back to default
                system_prompt = str(chat_config.get("rephrase_prompt_in") or current_config["rephrase_prompt"])

            # Only use the prompt if it's long enough (at least 10 characters)
            if len(system_prompt) < 10:
                system_prompt = current_config["rephrase_prompt"]

            try:
                original_text = text
                text, provider, model = await asyncio.to_thread(
                    _rephrase_with_provider,
                    current_config["rephrase_provider"],
                    current_config,
                    groq_client,
                    openai_client,
                    system_prompt,
                    text,
                )
                log_data = {
                    "provider": provider,
                    "event": "rephrasing_completed",
                    "chat_info": chat_info,
                    "type": "rephrasing",
                    "model": model,
                    "rephrasing_input": _log_content(original_text, verbose_logging),
                    "rephrasing_result": _log_content(text, verbose_logging),
                }
                logger.info(f"Transcription Process Details: {json.dumps(log_data, indent=2)}")
            except Exception as rephrase_exc:
                rephrase_error_code, rephrase_hint = _classify_provider_error(rephrase_exc)
                if current_config["graceful_degradation_enabled"]:
                    logger.warning(
                        f"Rephrasing skipped in {chat_info}: {rephrase_error_code} ({rephrase_hint})", exc_info=True
                    )
                    await message.reply_text(
                        f"⚠️ Rephrasing skipped: {rephrase_hint} ({current_config['rephrase_provider']})"
                    )
                else:
                    raise rephrase_exc

        # Strip leading/trailing quotes and spaces
        text = text.strip(' "')

        # Telegram's hard limit is 4096 characters. We wrap on the RAW text, but
        # each chunk is afterwards HTML-escaped (``<`` -> ``&lt;`` etc.) and wrapped
        # in a reference line plus <blockquote> tags, all of which grow the final
        # message. Keep the chunk width well below 4096 so the escaped result stays
        # safely under the limit even for escape-heavy text.
        max_length = 3000

        # Split text into chunks if it's too long
        chunks = textwrap.wrap(text, width=max_length, break_long_words=False, break_on_hyphens=False)

        fallback_notice = ""
        if fallback_used:
            fallback_notice = (
                f"\n⚠️ Provider fallback active: {current_config['transcription_provider']} -> {provider_used}"
            )

        voice_reference = escape(_build_voice_reference(message))
        for i, chunk in enumerate(chunks):
            part_reference = f" Part {i + 1}/{len(chunks)}" if len(chunks) > 1 else ""
            first_chunk_notice = fallback_notice if i == 0 else ""
            txt = (
                f"📝 {voice_reference}{part_reference}{first_chunk_notice}\n"
                f"<blockquote expandable>{escape(chunk)}</blockquote>"
            )

            # Send the transcription as a reply (quote only on first part if not deleting)
            await message.reply_text(txt, quote=(i == 0) and not delete_voice_flag)

        # Delete the original voice message if configured to do so
        if delete_voice_flag:
            await message.delete()

    except Exception as e:
        chat_info = get_chat_info(message.chat)
        error_code, error_hint = _classify_provider_error(e)
        provider = current_config.get("transcription_provider", "UNKNOWN")
        error_msg = f"❌ Transcription failed: {error_hint} ({provider})"
        logger.error(
            f"Transcription failed in {chat_info}: code={error_code}, provider={provider}, error={str(e)}",
            exc_info=True,
        )
        await message.reply_text(error_msg)
    finally:
        # Clean up temporary file
        chat_info = get_chat_info(message.chat)
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
            logger.info(f"Temporary file removed for {chat_info}")
