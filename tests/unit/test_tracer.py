"""
Tests for uada/observability/tracer.py.

OpenTelemetry's `trace.set_tracer_provider()` only ever takes effect
once per process -- later calls are silently ignored (with a warning).
So these tests assert against the `TracerProvider` object
`configure_tracing()` *returns*, not the process-global
`trace.get_tracer_provider()`, which stays whatever the first call in
the test session installed. That keeps each scenario independent of
test execution order.
"""

from __future__ import annotations

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from uada.config import Settings
from uada.observability.tracer import configure_tracing

pytestmark = pytest.mark.unit


def _processors(provider: object) -> tuple[object, ...]:
    return provider._active_span_processor._span_processors  # type: ignore[attr-defined]


class TestConfigureTracing:
    def test_disabled_installs_no_processors(self) -> None:
        settings = Settings(db_url="sqlite:///:memory:", otel_enabled=False)  # type: ignore[call-arg]
        provider = configure_tracing(settings)
        assert _processors(provider) == ()

    def test_enabled_without_langfuse_uses_console_exporter(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            db_url="sqlite:///:memory:",
            otel_enabled=True,
            langfuse_secret_key=None,
            langfuse_public_key=None,
        )
        provider = configure_tracing(settings)
        processors = _processors(provider)

        assert len(processors) == 1
        assert isinstance(processors[0], SimpleSpanProcessor)
        assert isinstance(processors[0].span_exporter, ConsoleSpanExporter)

    def test_enabled_with_langfuse_keys_uses_otlp_exporter(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            db_url="sqlite:///:memory:",
            otel_enabled=True,
            langfuse_secret_key="sk-lf-test-secret",
            langfuse_public_key="pk-lf-test-public",
            langfuse_host="http://localhost:3000",
        )
        provider = configure_tracing(settings)
        processors = _processors(provider)

        assert len(processors) == 1
        assert isinstance(processors[0], BatchSpanProcessor)
        exporter = processors[0].span_exporter
        assert isinstance(exporter, OTLPSpanExporter)
        assert exporter._endpoint == "http://localhost:3000/api/public/otel/v1/traces"
        # public:secret, base64-encoded, per Langfuse's documented OTLP auth contract.
        assert exporter._headers == {
            "Authorization": "Basic cGstbGYtdGVzdC1wdWJsaWM6c2stbGYtdGVzdC1zZWNyZXQ="
        }

    def test_missing_one_langfuse_key_falls_back_to_console(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            db_url="sqlite:///:memory:",
            otel_enabled=True,
            langfuse_secret_key="sk-lf-test-secret",
            langfuse_public_key=None,
        )
        provider = configure_tracing(settings)
        processors = _processors(provider)
        assert isinstance(processors[0], SimpleSpanProcessor)

    def test_langfuse_endpoint_strips_trailing_slash(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            db_url="sqlite:///:memory:",
            otel_enabled=True,
            langfuse_secret_key="sk-lf-test-secret",
            langfuse_public_key="pk-lf-test-public",
            langfuse_host="http://localhost:3000/",
        )
        provider = configure_tracing(settings)
        exporter = _processors(provider)[0].span_exporter
        assert exporter._endpoint == "http://localhost:3000/api/public/otel/v1/traces"

    def test_does_not_raise_when_called_repeatedly(self) -> None:
        # trace.set_tracer_provider() is a process-global, first-wins
        # call; later invocations must not raise even though they're
        # necessarily overridden.
        for enabled in (False, True, False):
            settings = Settings(db_url="sqlite:///:memory:", otel_enabled=enabled)  # type: ignore[call-arg]
            configure_tracing(settings)
