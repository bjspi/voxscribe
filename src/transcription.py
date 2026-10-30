"""Voice transcription and optional rephrasing for Telegram voice messages."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import datetime
from html import escape
from io import BytesIO
from typing import Any, BinaryIO, Literal, TypedDict, cast

import openai
from groq import Groq
from pyrogram import Client
from pyrogram.types import Message

from src.debounce import DebounceCollector, SenderKey, recording_status_from_update
from src.helpers import ChatConfig, YamlConfig, get_chat_config, get_chat_info, get_config_value, load_bot_config
from src.logging import get_logger
from src.prompts import resolve_rephrase_prompt

# Set up logging
logger = get_logger(__name__)

# When verbose logging is off, message content is logged only up to this many
# characters (privacy + smaller log files).
LOG_PREVIEW_CHARS = 200

# Inline replies are split into parts of at most this many characters, which
# leaves room for the reference, HTML escaping and blockquote tags.
INLINE_CHUNK_CHARS = 3000

ProviderName = Literal["GROQ", "OPENAI"]
BUILTIN_PROVIDERS = ("GROQ", "OPENAI")
ProviderErrorCode = Literal[
    "dns",
    "timeout",
    "auth",
    "rate_limit",
    "provider_unavailable",
    "connection",
    "unknown",
]


class CustomProvider(TypedDict):
    """One user-defined OpenAI-compatible chat endpoint from ``api.custom_providers``."""

    base_url: str
    api_key: str
    model: str
    reasoning_effort: str


class RuntimeConfig(TypedDict):
    """Resolved runtime settings used by one transcription workflow."""

    transcription_provider: ProviderName
    # GROQ, OPENAI or the name of an entry in custom_providers.
    rephrase_provider: str
    openai_api_key: str
    groq_api_key: str
    custom_providers: dict[str, CustomProvider]
    transcription_model_openai: str
    rephrase_model_openai: str
    transcription_model_groq: str
    rephrase_model_groq: str
    # Model of the selected rephrasing provider, for status displays.
    rephrase_model: str
    transcription_prompt: str
    rephrase_prompt: str
    graceful_degradation_enabled: bool
    provider_fallback_enabled: bool
    transcription_retry_count: int
    verbose_logging: bool
    debounce_quiet_seconds: int
    debounce_max_wait_seconds: int


class ProviderAttempt(TypedDict):
    """One attempted provider call captured for logging and failure summaries."""

    provider: ProviderName
    attempt: int
    error_code: ProviderErrorCode
    hint: str


@dataclass(frozen=True)
class VoiceTranscript:
    """The transcription of one voice, waiting to be rephrased and sent."""

    message: Message
    text: str
    fallback_notice: str


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


def _voice_seconds(message: Message) -> int:
    """Return a voice message's duration in whole seconds (0 if unknown)."""
    try:
        return max(0, int(getattr(getattr(message, "voice", None), "duration", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _total_duration(messages: list[Message]) -> str:
    """Format the summed duration of several voices as ``M:SS``."""
    return _format_voice_duration(sum(_voice_seconds(message) for message in messages))


def _build_voice_reference(messages: list[Message]) -> str:
    """Build the sender and duration reference for one or several collected voices.

    Args:
        messages: The voices of one sender, oldest first.

    Returns:
        A compact reference such as ``"alice (0:42)"`` or ``"alice (3 voices, 2:07)"``.
    """
    count = f"{len(messages)} voices, " if len(messages) > 1 else ""
    return f"{_get_sender_label(messages[-1])} ({count}{_total_duration(messages)})"


def _build_markdown_filename(messages: list[Message]) -> str:
    """Prefix the first voice's timestamp so files sort chronologically on any platform."""
    sent_at = getattr(messages[0], "date", None)
    if not isinstance(sent_at, datetime):
        sent_at = datetime.now()
    sender = re.sub(r"[^\w-]+", "_", _get_sender_label(messages[0]))[:80].strip("_-") or "voice"
    minutes, seconds = _total_duration(messages).split(":")
    return f"{sent_at:%Y%m%d_%Hh%Mm}_{sender}_{minutes}m{seconds}s.md"


def _build_markdown_document(
    transcripts: list[VoiceTranscript], rephrased_text: str | None, rephrase_notice: str = ""
) -> str:
    """Keep both complete versions, including paragraphs, in a single document."""
    messages = [transcript.message for transcript in transcripts]
    # Keep user-controlled names on one metadata line and escape Markdown syntax.
    sender = re.sub(r"([\\`*_{}\[\]<>()#!|])", r"\\\1", " ".join(_get_sender_label(messages[0]).split()))
    if len(transcripts) == 1:
        original_section = transcripts[0].text
        voices_line = ""
    else:
        original_section = "\n\n".join(
            f"### Voice {index} ({_format_voice_duration(_voice_seconds(transcript.message))})\n\n{transcript.text}"
            for index, transcript in enumerate(transcripts, start=1)
        )
        voices_line = f"  \nVoices: {len(transcripts)}"
    rephrased_section = rephrased_text if rephrased_text is not None else f"> {rephrase_notice}"
    return (
        f"# Voice transcription\n\n"
        f"Sender: {sender}  \nDuration: {_total_duration(messages)}{voices_line}\n\n"
        f"## Original transcription\n\n{original_section}\n\n"
        f"## Rephrased version\n\n{rephrased_section}\n"
    )


def _split_for_telegram(text: str, limit: int = INLINE_CHUNK_CHARS) -> list[str]:
    """Split text into message-sized parts without losing its paragraphs.

    Whole paragraphs are packed into parts; only a single paragraph longer than
    the limit is wrapped at word boundaries.

    Args:
        text: The text to send.
        limit: Maximum characters per part.

    Returns:
        The parts in order; at least one (possibly empty) part.
    """
    pieces: list[str] = []
    for paragraph in text.split("\n\n"):
        if len(paragraph) <= limit:
            pieces.append(paragraph)
        else:
            pieces += textwrap.wrap(paragraph, width=limit, break_long_words=False, break_on_hyphens=False)

    parts: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if current and len(candidate) > limit:
            parts.append(current)
            current = piece
        else:
            current = candidate
    parts.append(current)
    return parts


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


def _load_custom_providers(config: YamlConfig) -> dict[str, CustomProvider]:
    """Read the user-defined OpenAI-compatible providers from ``api.custom_providers``.

    Entries without a base URL, key or model are skipped with a warning, and
    names that clash with a built-in provider are ignored.

    Args:
        config: Parsed YAML configuration.

    Returns:
        Complete provider entries keyed by their configured name.
    """
    raw: object = get_config_value(config, ["api", "custom_providers"], default={})
    providers: dict[str, CustomProvider] = {}
    if not isinstance(raw, dict):
        return providers
    for raw_name, entry in raw.items():
        name = str(raw_name).strip()
        if not isinstance(entry, dict) or not name or name.upper() in BUILTIN_PROVIDERS:
            logger.warning("Ignoring custom provider %r: invalid entry or reserved name", name)
            continue
        provider: CustomProvider = {
            "base_url": str(entry.get("base_url") or "").strip(),
            "api_key": str(entry.get("api_key") or "").strip(),
            "model": str(entry.get("model") or "").strip(),
            "reasoning_effort": str(entry.get("reasoning_effort") or "").strip(),
        }
        if not (provider["base_url"] and provider["api_key"] and provider["model"]):
            logger.warning("Ignoring custom provider %r: base_url, api_key and model are required", name)
            continue
        providers[name] = provider
    return providers


def _normalize_rephrase_provider(provider: object, custom_providers: dict[str, CustomProvider]) -> str:
    """Resolve the rephrasing provider to GROQ, OPENAI or a custom provider name.

    Custom names match case-insensitively; unknown names fall back to GROQ.

    Args:
        provider: Raw provider value from config.
        custom_providers: The configured custom providers.

    Returns:
        ``"GROQ"``, ``"OPENAI"`` or the exact name of a custom provider.
    """
    wanted = str(provider or "").strip()
    for name in custom_providers:
        if name.lower() == wanted.lower():
            return name
    if wanted and wanted.upper() not in BUILTIN_PROVIDERS:
        logger.warning("Unknown rephrasing provider %r; using GROQ", wanted)
    return _normalize_provider(wanted, default="GROQ")


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
    provider: str,
    current_config: RuntimeConfig,
    groq_client: Groq | None,
    openai_client: openai.OpenAI | None,
    system_prompt: str,
    text: str,
) -> tuple[str, str, str]:
    """Run the rephrasing request with the selected provider.

    Args:
        provider: The provider to use (``GROQ``, ``OPENAI`` or a custom provider name).
        current_config: The resolved runtime configuration.
        groq_client: An initialized Groq client (required for the GROQ provider).
        openai_client: An initialized OpenAI client (required for the OPENAI provider).
            Custom providers build their own client from ``api.custom_providers``.
        system_prompt: The system prompt steering the rephrasing.
        text: The transcript text to rephrase.

    Returns:
        A tuple of ``(rephrased_text, provider_label, model_name)``.
    """
    custom = current_config["custom_providers"].get(provider)
    reasoning_effort = ""
    if custom is not None:
        client: Any = openai.OpenAI(api_key=custom["api_key"], base_url=custom["base_url"])
        model = custom["model"]
        reasoning_effort = custom["reasoning_effort"]
        provider_name = f"{provider} (Rephrasing)"
    elif provider == "GROQ":
        if groq_client is None:
            raise RuntimeError("GROQ client is not configured; check the Groq API key")
        client = groq_client
        model = current_config["rephrase_model_groq"]
        provider_name = "GROQ (Rephrasing)"
    else:
        if openai_client is None:
            raise RuntimeError("OpenAI client is not configured; check the OpenAI API key")
        client = openai_client
        model = current_config["rephrase_model_openai"]
        provider_name = "OPENAI (Rephrasing)"

    # Sent as extra body field so older SDK versions without the parameter still work.
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        stream=False,
        extra_body={"reasoning_effort": reasoning_effort} if reasoning_effort else None,
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

    custom_providers = _load_custom_providers(config)
    resolved_rephrase_provider = _normalize_rephrase_provider(rephrase_provider, custom_providers)
    rephrase_model_openai = _config_string(config, ["models", "openai", "rephrase"], default="gpt-4o-mini")
    rephrase_model_groq = _config_string(config, ["models", "groq", "rephrase"], default="openai/gpt-oss-120b")
    if resolved_rephrase_provider in custom_providers:
        rephrase_model = custom_providers[resolved_rephrase_provider]["model"]
    else:
        rephrase_model = rephrase_model_groq if resolved_rephrase_provider == "GROQ" else rephrase_model_openai

    return {
        "transcription_provider": _normalize_provider(transcription_provider, default="GROQ"),
        "rephrase_provider": resolved_rephrase_provider,
        "openai_api_key": _config_string(config, ["api", "keys", "openai"]),
        "groq_api_key": _config_string(config, ["api", "keys", "groq"]),
        "custom_providers": custom_providers,
        "transcription_model_openai": _config_string(
            config, ["models", "openai", "transcription"], default="whisper-1"
        ),
        "rephrase_model_openai": rephrase_model_openai,
        "transcription_model_groq": _config_string(
            config, ["models", "groq", "transcription"], default="whisper-large-v3"
        ),
        "rephrase_model_groq": rephrase_model_groq,
        "rephrase_model": rephrase_model,
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
        "debounce_quiet_seconds": _coerce_positive_int(
            get_config_value(config, ["debounce", "quiet_seconds"], default=8),
            default=8,
        ),
        "debounce_max_wait_seconds": _coerce_positive_int(
            get_config_value(config, ["debounce", "max_wait_seconds"], default=600),
            default=600,
        ),
    }


def _build_clients(current_config: RuntimeConfig) -> tuple[Groq | None, openai.OpenAI | None]:
    """Create the Groq and OpenAI clients the current configuration may need.

    Args:
        current_config: The resolved runtime configuration.

    Returns:
        ``(groq_client, openai_client)``; a client is None when not needed or
        when its key is missing.
    """
    providers = {current_config["transcription_provider"], current_config["rephrase_provider"]}
    fallback = current_config["provider_fallback_enabled"]
    groq_client = Groq(api_key=current_config["groq_api_key"]) if (
        ("GROQ" in providers or fallback) and current_config["groq_api_key"]
    ) else None
    openai_client = openai.OpenAI(api_key=current_config["openai_api_key"]) if (
        ("OPENAI" in providers or fallback) and current_config["openai_api_key"]
    ) else None
    return groq_client, openai_client


def _sender_key(message: Message) -> SenderKey:
    """Return the per-chat sender key used to collect a sender's voices."""
    sender = getattr(message, "from_user", None) or getattr(message, "sender_chat", None)
    return message.chat.id, int(getattr(sender, "id", 0) or 0)


def _delete_voice_wanted(chat_config: ChatConfig, message: Message) -> bool:
    """Return whether the chat wants this voice deleted after transcription."""
    key = "delete_outgoing_voice" if message.outgoing else "delete_incoming_voice"
    return bool(chat_config.get(key, 0))


async def _transcribe_message(
    message: Message,
    current_config: RuntimeConfig,
    groq_client: Groq | None,
    openai_client: openai.OpenAI | None,
) -> VoiceTranscript | None:
    """Download and transcribe one voice; report failures in the chat.

    Args:
        message: The voice message to transcribe.
        current_config: The resolved runtime configuration.
        groq_client: An initialized Groq client, or None if unavailable.
        openai_client: An initialized OpenAI client, or None if unavailable.

    Returns:
        The transcript, or None when transcription failed (the error reply has
        already been sent).
    """
    chat_info = get_chat_info(message.chat)
    tmp_path: str | None = None
    try:
        # Download voice message to a temporary path. The file is closed before
        # Pyrogram writes to it, which avoids Windows file-handle surprises.
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await message.download(file_name=tmp_path)
        with open(tmp_path, "rb") as audio_file:
            transcription_result = await _run_transcription_with_recovery(
                current_config, groq_client, openai_client, audio_file, chat_info
            )

        provider_used = transcription_result["provider_used"]
        text = str(getattr(transcription_result["transcript"], "text", "") or "").strip()
        if not text:
            raise RuntimeError(f"{provider_used} returned an empty transcription response")

        log_data = {
            "event": "transcription_completed",
            "provider": transcription_result["provider_name"],
            "chat_info": chat_info,
            "model": transcription_result["model_used"],
            "provider_used": provider_used,
            "fallback_used": transcription_result["fallback_used"],
            "provider_attempts": transcription_result["provider_attempts"],
            "transcription_result_length": len(text),
            "transcription_result": _log_content(text, current_config["verbose_logging"]),
        }
        logger.info(f"Transcription Process Details: {json.dumps(log_data, indent=2)}")

        fallback_notice = ""
        if transcription_result["fallback_used"]:
            fallback_notice = (
                f"\n⚠️ Provider fallback active: {current_config['transcription_provider']} -> {provider_used}"
            )
        return VoiceTranscript(message=message, text=text, fallback_notice=fallback_notice)
    except Exception as e:
        error_code, error_hint = _classify_provider_error(e)
        provider = current_config.get("transcription_provider", "UNKNOWN")
        logger.error(
            f"Transcription failed in {chat_info}: code={error_code}, provider={provider}, error={str(e)}",
            exc_info=True,
        )
        await message.reply_text(f"❌ Transcription failed: {error_hint} ({provider})")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
            logger.info(f"Temporary file removed for {chat_info}")


async def _deliver_transcripts(transcripts: list[VoiceTranscript]) -> None:
    """Rephrase one or several transcripts of one sender and send a single reply.

    The reply goes to the newest voice. Several transcripts are rephrased
    together, so the result reads as one message.

    Args:
        transcripts: Transcripts of one sender in one chat, oldest first.
    """
    transcripts = sorted(transcripts, key=lambda transcript: transcript.message.id)
    messages = [transcript.message for transcript in transcripts]
    message = messages[-1]
    chat_info = get_chat_info(message.chat)
    current_config = get_current_config()
    chat_config = get_chat_config(str(message.chat.id))
    markdown_output = bool(chat_config.get("markdown_output", 0))
    delete_voice_flag = _delete_voice_wanted(chat_config, message)
    verbose_logging = current_config["verbose_logging"]

    try:
        original_text = "\n\n".join(transcript.text for transcript in transcripts)
        text = original_text
        rephrased_text: str | None = None
        rephrase_notice = ""

        # Markdown files always include both versions, independent of text mode.
        if markdown_output or chat_config.get("rephrasing", 1):
            # Custom prompt > template > global default, per direction.
            resolved_prompt = resolve_rephrase_prompt(
                chat_config, "out" if message.outgoing else "in", current_config["rephrase_prompt"]
            )
            logger.info(f"Rephrasing prompt for {chat_info}: {resolved_prompt.label}")
            groq_client, openai_client = _build_clients(current_config)

            try:
                text, provider, model = await asyncio.to_thread(
                    _rephrase_with_provider,
                    current_config["rephrase_provider"],
                    current_config,
                    groq_client,
                    openai_client,
                    resolved_prompt.text,
                    original_text,
                )
                rephrased_text = text
                log_data = {
                    "provider": provider,
                    "event": "rephrasing_completed",
                    "chat_info": chat_info,
                    "type": "rephrasing",
                    "model": model,
                    "voices": len(transcripts),
                    "rephrasing_input_length": len(original_text),
                    "rephrasing_result_length": len(text),
                    "rephrasing_input": _log_content(original_text, verbose_logging),
                    "rephrasing_result": _log_content(text, verbose_logging),
                }
                logger.info(f"Transcription Process Details: {json.dumps(log_data, indent=2)}")
            except Exception as rephrase_exc:
                rephrase_error_code, rephrase_hint = _classify_provider_error(rephrase_exc)
                if markdown_output or current_config["graceful_degradation_enabled"]:
                    logger.warning(
                        f"Rephrasing skipped in {chat_info}: {rephrase_error_code} ({rephrase_hint})", exc_info=True
                    )
                    rephrase_notice = f"Rephrasing unavailable: {rephrase_hint} ({current_config['rephrase_provider']})."
                    if not markdown_output:
                        await message.reply_text(
                            f"⚠️ Rephrasing skipped: {rephrase_hint} ({current_config['rephrase_provider']})"
                        )
                else:
                    raise rephrase_exc

        fallback_notice = next((t.fallback_notice for t in transcripts if t.fallback_notice), "")
        voice_reference = escape(_build_voice_reference(messages))
        if markdown_output:
            document_text = _build_markdown_document(transcripts, rephrased_text, rephrase_notice)
            filename = _build_markdown_filename(messages)
            # The filename already carries sender, time and duration: send only the
            # file and reserve the caption for warnings.
            caption = fallback_notice.strip()
            if rephrase_notice:
                caption = f"{caption}\n⚠️ {rephrase_notice}".strip()
            with BytesIO(document_text.encode("utf-8")) as document:
                document.name = filename
                sent_message = await message.reply_document(
                    document, file_name=filename, caption=caption, quote=not delete_voice_flag
                )
            logger.info(
                "Markdown document sent in %s: voice_ids=%s, document_message_id=%s, "
                "filename=%s, original_length=%s, rephrased_length=%s",
                chat_info, [m.id for m in messages], sent_message.id, filename, len(original_text),
                len(rephrased_text) if rephrased_text is not None else None,
            )
        else:
            chunks = _split_for_telegram(text.strip(' "'))
            for i, chunk in enumerate(chunks):
                part_reference = f" Part {i + 1}/{len(chunks)}" if len(chunks) > 1 else ""
                first_chunk_notice = fallback_notice if i == 0 else ""
                txt = (
                    f"📝 {voice_reference}{part_reference}{first_chunk_notice}\n"
                    f"<blockquote expandable>{escape(chunk)}</blockquote>"
                )

                # Quote only on the first part if the voice will be kept.
                await message.reply_text(txt, quote=(i == 0) and not delete_voice_flag)

        # Delete the original voice messages if configured to do so
        for voice_message in messages:
            if _delete_voice_wanted(chat_config, voice_message):
                await voice_message.delete()

    except Exception as e:
        error_code, error_hint = _classify_provider_error(e)
        logger.error(f"Sending the transcription failed in {chat_info}: code={error_code}, error={e}", exc_info=True)
        await message.reply_text(f"❌ Transcription failed: {error_hint} ({current_config['rephrase_provider']})")


# Holds rephrasing per sender while they keep sending voices (per-chat setting).
_collector: DebounceCollector[VoiceTranscript] = DebounceCollector(_deliver_transcripts)


def note_recording_update(update: object) -> None:
    """Feed a raw Telegram update into the debounce collector.

    Only "recording voice" status changes matter; every other update is ignored.

    Args:
        update: Any raw update received by the client.
    """
    status = recording_status_from_update(update)
    if status is not None:
        (chat_id, user_id), active = status
        logger.info(f"Voice recording {'started' if active else 'cancelled'}: chat={chat_id}, user={user_id}")
        _collector.note_recording(*status)


async def transcribe_voice(client: Client, message: Message) -> None:
    """Transcribe a voice message, optionally rephrase it and reply with the text.

    This function handles the complete workflow of voice message processing:
    1. Downloads the voice message and transcribes it with the configured API
    2. With debouncing on, holds the transcript until the sender stops sending
       voices, then rephrases all collected transcripts together
    3. Otherwise rephrases (if enabled) and replies right away
    4. Optionally deletes the original voice message(s)

    Args:
        client: The Pyrogram client instance that received the voice message.
        message: The voice message to transcribe.
    """
    chat_info = get_chat_info(message.chat)
    log_data: dict[str, object] = {
        "event": "transcription_start",
        "chat_info": chat_info,
        "message_direction": "outgoing" if message.outgoing else "incoming",
    }
    logger.info(f"Transcription Process Details: {json.dumps(log_data, indent=2)}")

    # Load current configuration dynamically for each transcription
    current_config = get_current_config()
    chat_config = get_chat_config(str(message.chat.id))
    # Debouncing only delays rephrased output; plain transcripts are sent at once.
    debounce = bool(chat_config.get("rephrase_debounce", 0)) and bool(
        chat_config.get("markdown_output", 0) or chat_config.get("rephrasing", 1)
    )
    key = _sender_key(message)
    if debounce:
        _collector.voice_started(
            key, current_config["debounce_quiet_seconds"], current_config["debounce_max_wait_seconds"]
        )

    groq_client, openai_client = _build_clients(current_config)
    transcript = await _transcribe_message(message, current_config, groq_client, openai_client)
    if debounce:
        _collector.voice_finished(key, transcript)
        logger.info(f"Transcript held for debouncing in {chat_info} (sender {key[1]})")
    elif transcript is not None:
        await _deliver_transcripts([transcript])
