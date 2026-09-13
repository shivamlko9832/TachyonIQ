"""Deterministic routing for messages that do not request data analysis.

Social messages are handled before schema retrieval so greetings and thanks do
not become failed SQL questions.  Matching is deliberately strict: a greeting
that also contains a business question continues through the governed analytics
pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationalReply:
    """A safe response that contains no analytical claims or data values."""

    answer: str
    suggested_questions: tuple[str, ...]


class ConversationalRouter:
    """Recognise complete social utterances without classifying data questions."""

    _GREETINGS = {
        "hi",
        "hello",
        "hey",
        "hello there",
        "good morning",
        "good afternoon",
        "good evening",
        "how are you",
    }
    _THANKS = {"thanks", "thank you", "thank you very much", "many thanks"}
    _FAREWELLS = {"bye", "goodbye", "see you", "see you later"}
    _HELP = {
        "help",
        "help me",
        "what can you do",
        "how can you help",
        "what can i ask",
    }
    _SUGGESTIONS = (
        "Give me an executive summary of the latest complete month.",
        "Show the revenue trend and highlight unusual periods.",
        "Which customer segments have the highest churn risk?",
    )

    @classmethod
    def route(cls, message: str) -> ConversationalReply | None:
        """Return a conversational response only when the whole message is social."""
        normalised = re.sub(r"[^a-z0-9'\s]", " ", message.casefold())
        tokens = [token for token in normalised.split() if token != "tachyoniq"]
        utterance = " ".join(tokens).strip()

        if utterance in cls._GREETINGS:
            return ConversationalReply(
                answer=(
                    "Hello! Ask me a business question about revenue, customers, "
                    "support, marketing, trends, anomalies, comparisons, or forecasts."
                ),
                suggested_questions=cls._SUGGESTIONS,
            )
        if utterance in cls._THANKS:
            return ConversationalReply(
                answer="You're welcome. You can ask a follow-up about the result or its evidence.",
                suggested_questions=(
                    "Show the evidence behind the latest result.",
                    "Break the result down by region.",
                    "Compare it with the preceding period.",
                ),
            )
        if utterance in cls._FAREWELLS:
            return ConversationalReply(
                answer="Goodbye. Your analytical context will be here when you return.",
                suggested_questions=(),
            )
        if utterance in cls._HELP:
            return ConversationalReply(
                answer=(
                    "I can answer governed business questions, explain the calculation, "
                    "compare periods and segments, detect anomalies, and produce forecasts "
                    "with uncertainty and validation evidence."
                ),
                suggested_questions=cls._SUGGESTIONS,
            )
        return None
