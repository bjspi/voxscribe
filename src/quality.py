"""Quality fallback for transcriptions: judge a result and pick the better of two.

Whisper's ``verbose_json`` response carries an average log-probability and a
no-speech probability per segment. A result that is empty, too short or less
confident than a threshold is "doubtful"; a second transcription of the same
audio (by default without a prompt, since a prompt can make Whisper drop or
invent text) may then replace it. The first result is kept unless it is
doubtful and the second one is more confident: with a prompt, even good
results score a bit lower, so "best log-probability always wins" would throw
away the prompt's benefit (names, jargon) almost every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SecondPassMode = Literal["off", "doubtful", "always"]

# Whisper's own silence rule: a segment this likely to hold no speech is probably invented text.
NO_SPEECH_LIMIT = 0.6


@dataclass(frozen=True)
class QualitySettings:
    """The ``transcription_quality`` section of the bot config."""

    second_pass: SecondPassMode = "off"
    logprob_threshold: float = -0.5
    min_chars: int = 0
    second_pass_prompt: str = ""


@dataclass(frozen=True)
class Candidate:
    """One transcription result with its confidence summary."""

    text: str
    # Duration-weighted average log-probability; None when the response had none.
    avg_logprob: float | None = None
    max_no_speech_prob: float | None = None

    def describe(self) -> str:
        """Return a compact log representation (length and confidence, no content)."""
        return f"chars={len(self.text)} avg_logprob={self.avg_logprob} no_speech={self.max_no_speech_prob}"


def candidate_from_response(response: Any) -> Candidate:
    """Build a candidate from a Groq/OpenAI transcription response.

    Responses without ``segments`` (plain JSON, models without ``verbose_json``)
    give a candidate without confidence data.

    Args:
        response: The SDK response object (or a plain dict).

    Returns:
        The text plus its average log-probability and highest no-speech probability.
    """
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    if not isinstance(payload, dict):
        payload = {"text": getattr(response, "text", "")}
    text = str(payload.get("text") or "").strip()
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        return Candidate(text)

    weighted: list[tuple[float, float]] = []
    no_speech: list[float] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        logprob = segment.get("avg_logprob")
        if isinstance(logprob, (int, float)):
            duration = max(0.0, float(segment.get("end") or 0) - float(segment.get("start") or 0))
            weighted.append((float(logprob), duration))
        if isinstance(segment.get("no_speech_prob"), (int, float)):
            no_speech.append(float(segment["no_speech_prob"]))
    if not weighted:
        return Candidate(text, max_no_speech_prob=max(no_speech, default=None))
    total = sum(duration for _, duration in weighted)
    # Weighted by segment duration, so a short garbled segment does not hide in a long good one.
    average = (
        sum(logprob * duration for logprob, duration in weighted) / total
        if total
        else sum(logprob for logprob, _ in weighted) / len(weighted)
    )
    # Servers without real log-probabilities report exactly 0 as a placeholder.
    return Candidate(
        text,
        avg_logprob=None if average == 0.0 else round(average, 4),
        max_no_speech_prob=max(no_speech, default=None),
    )


def is_doubtful(candidate: Candidate, settings: QualitySettings) -> bool:
    """Return whether a result is empty, too short or below the confidence threshold."""
    if not candidate.text or len(candidate.text) < settings.min_chars:
        return True
    return candidate.avg_logprob is not None and candidate.avg_logprob < settings.logprob_threshold


def choose(first: Candidate, second: Candidate | None, settings: QualitySettings) -> tuple[Candidate, str]:
    """Pick between a doubtful first result and the second one.

    An empty first result loses only to a second text that is confident itself
    and not likely silence. A missing second result, an empty one or one that
    only echoes its prompt keeps the first.

    Args:
        first: The doubtful first result (with the main prompt).
        second: The second result, or None if that request failed.
        settings: The quality settings.

    Returns:
        ``(chosen candidate, "first" | "second")``.
    """
    if second is None or not second.text:
        return first, "first"
    prompt = settings.second_pass_prompt.strip().lower()
    if prompt and second.text.lower() == prompt:
        return first, "first"
    if not first.text:
        confident = second.avg_logprob is not None and second.avg_logprob >= settings.logprob_threshold
        silent = second.max_no_speech_prob is not None and second.max_no_speech_prob >= NO_SPEECH_LIMIT
        return (second, "second") if confident and not silent else (first, "first")
    if first.avg_logprob is None or second.avg_logprob is None:
        # Without confidence data only the length can tell them apart.
        return (second, "second") if len(second.text) > len(first.text) else (first, "first")
    return (second, "second") if second.avg_logprob > first.avg_logprob else (first, "first")


def is_probably_silence(candidate: Candidate, settings: QualitySettings) -> bool:
    """Return whether a final result is most likely text Whisper invented for silence."""
    if not candidate.text:
        return True
    low_confidence = candidate.avg_logprob is not None and candidate.avg_logprob < settings.logprob_threshold
    silent = candidate.max_no_speech_prob is not None and candidate.max_no_speech_prob >= NO_SPEECH_LIMIT
    return low_confidence and silent
