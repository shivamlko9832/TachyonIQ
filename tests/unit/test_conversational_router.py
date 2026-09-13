"""Tests for strict non-analytical conversation routing."""

from uada.pipeline.conversational_router import ConversationalRouter


def test_greeting_is_answered_without_requesting_data() -> None:
    reply = ConversationalRouter.route("Hi TachyonIQ!")

    assert reply is not None
    assert reply.answer.startswith("Hello!")
    assert len(reply.suggested_questions) == 3


def test_greeting_prefixed_business_question_continues_to_analytics() -> None:
    reply = ConversationalRouter.route(
        "Hi TachyonIQ, tell me about the revenue generated for this month"
    )

    assert reply is None


def test_help_and_thanks_are_supported_without_capturing_business_questions() -> None:
    assert ConversationalRouter.route("What can you do?") is not None
    assert ConversationalRouter.route("Thank you") is not None
    assert ConversationalRouter.route("Thanks, now show revenue") is None
