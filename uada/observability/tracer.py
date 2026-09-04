"""
Observability -- OpenTelemetry -> Langfuse tracing
=====================================================
Installs the process-global OpenTelemetry TracerProvider the rest of the
pipeline traces against.

- `settings.otel_enabled` and both Langfuse keys set: exports spans to
  the configured Langfuse instance over OTLP/HTTP.
- `settings.otel_enabled` alone: exports to the console (dev mode -- no
  external service required).
- `settings.otel_enabled=False`: a real TracerProvider is still
  installed, just with no span processor attached, so every
  `trace.get_tracer(__name__).start_as_current_span(...)` call elsewhere
  in the pipeline is always safe to make regardless of configuration --
  it just goes nowhere. (opentelemetry-api's own default, pre-configure,
  TracerProvider is already a safe no-op, so pipeline code never needs to
  guard against configure_tracing() not having been called at all.)

Verification note: OTLP export to a real Langfuse instance could only be
exercised end-to-end against a running Langfuse server (e.g. via
docker-compose.yml's langfuse-web service), which isn't available in
this environment. The exporter/auth wiring below follows Langfuse's
documented OTLP ingestion contract (HTTP Basic Auth: public key as
username, secret key as password, against `{host}/api/public/otel`), but
that specific piece hasn't been verified against a live instance.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from uada.config import Settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "uada"


def configure_tracing(settings: Settings) -> TracerProvider:
    """
    Build a TracerProvider for `settings` and install it as the global
    default (`opentelemetry.trace.set_tracer_provider`).

    Returns the constructed provider. The global `set_tracer_provider`
    call only ever takes effect once per process (OpenTelemetry silently
    ignores later calls) -- callers that need to inspect what a specific
    Settings configuration would produce should use the returned
    provider directly rather than `trace.get_tracer_provider()`.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))

    if not settings.otel_enabled:
        logger.info("Tracing disabled (settings.otel_enabled=False).")
    elif settings.langfuse_secret_key and settings.langfuse_public_key:
        provider.add_span_processor(BatchSpanProcessor(_build_langfuse_exporter(settings)))
        logger.info("Tracing enabled: exporting to Langfuse at '%s'.", settings.langfuse_host)
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("Tracing enabled in dev mode: exporting to console (no Langfuse configured).")

    trace.set_tracer_provider(provider)

    if settings.otel_enabled:
        import logfire

        # logfire.instrument_pydantic_ai() is a no-op (with a warning)
        # until logfire.configure() has run at least once in this
        # process. Calling it here with send_to_logfire=False does not
        # send anything to Logfire's own cloud and does not override the
        # provider set above (OpenTelemetry ignores a second
        # set_tracer_provider call) -- it only flips logfire's internal
        # "configured" flag so instrument_pydantic_ai() actually attaches,
        # emitting pydantic-ai's spans onto the same provider as the
        # pipeline's own manual spans.
        logfire.configure(send_to_logfire=False)
        logfire.instrument_pydantic_ai()

    return provider


def _build_langfuse_exporter(settings: Settings) -> OTLPSpanExporter:
    """
    Build an OTLP/HTTP exporter for Langfuse's OTLP ingestion endpoint.

    Langfuse authenticates OTLP requests with HTTP Basic Auth: the
    public key as username, the secret key as password.
    """
    public_key = (
        settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else ""
    )
    secret_key = (
        settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    )
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")

    endpoint = settings.langfuse_host.rstrip("/") + "/api/public/otel/v1/traces"
    return OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": f"Basic {credentials}"})


def instrument_app(app: FastAPI) -> None:
    """Instrument a FastAPI app's request handling with OpenTelemetry."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
