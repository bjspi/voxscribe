from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.quality import (
    Candidate,
    QualitySettings,
    candidate_from_response,
    choose,
    is_doubtful,
    is_probably_silence,
)

SETTINGS = QualitySettings(second_pass="doubtful", logprob_threshold=-0.5, min_chars=0)


class CandidateTests(unittest.TestCase):
    def test_verbose_response_is_weighted_by_segment_duration(self) -> None:
        payload = {
            "text": " Hallo Welt ",
            "segments": [
                {"start": 0, "end": 9, "avg_logprob": -0.1, "no_speech_prob": 0.01},
                {"start": 9, "end": 10, "avg_logprob": -1.0, "no_speech_prob": 0.3},
            ],
        }
        candidate = candidate_from_response(payload)
        self.assertEqual(candidate, Candidate("Hallo Welt", -0.19, 0.3))

    def test_plain_and_placeholder_responses_have_no_confidence(self) -> None:
        self.assertEqual(candidate_from_response(SimpleNamespace(text="hi")), Candidate("hi"))
        placeholder = {"text": "hi", "segments": [{"start": 0, "end": 1, "avg_logprob": 0.0}]}
        self.assertIsNone(candidate_from_response(placeholder).avg_logprob)


class DecisionTests(unittest.TestCase):
    def test_doubtful_when_empty_short_or_unsure(self) -> None:
        self.assertTrue(is_doubtful(Candidate(""), SETTINGS))
        self.assertTrue(is_doubtful(Candidate("text", -0.8), SETTINGS))
        self.assertFalse(is_doubtful(Candidate("text", -0.2), SETTINGS))
        self.assertFalse(is_doubtful(Candidate("text"), SETTINGS))
        self.assertTrue(is_doubtful(Candidate("ok", -0.1), QualitySettings(min_chars=5)))

    def test_more_confident_second_result_wins(self) -> None:
        first, second = Candidate("a", -0.9), Candidate("b", -0.3)
        self.assertEqual(choose(first, second, SETTINGS), (second, "second"))
        self.assertEqual(choose(second, first, SETTINGS), (second, "first"))

    def test_failed_empty_or_echoed_second_result_keeps_first(self) -> None:
        first = Candidate("a", -0.9)
        self.assertEqual(choose(first, None, SETTINGS)[1], "first")
        self.assertEqual(choose(first, Candidate("", -0.1), SETTINGS)[1], "first")
        echo = QualitySettings(second_pass_prompt="Names: Joscha")
        self.assertEqual(choose(first, Candidate("names: joscha", -0.1), echo)[1], "first")

    def test_empty_first_needs_a_confident_non_silent_second(self) -> None:
        self.assertEqual(choose(Candidate(""), Candidate("hi", -0.2, 0.1), SETTINGS)[1], "second")
        self.assertEqual(choose(Candidate(""), Candidate("you", -0.7, 0.7), SETTINGS)[1], "first")
        self.assertEqual(choose(Candidate(""), Candidate("you", -0.2, 0.7), SETTINGS)[1], "first")

    def test_silence_detection_needs_low_confidence_and_no_speech(self) -> None:
        self.assertTrue(is_probably_silence(Candidate("you", -0.7, 0.7), SETTINGS))
        self.assertFalse(is_probably_silence(Candidate("you", -0.2, 0.7), SETTINGS))
        self.assertFalse(is_probably_silence(Candidate("hm", -0.9, 0.2), SETTINGS))
        self.assertTrue(is_probably_silence(Candidate(""), SETTINGS))
