"""
OpenTelemetry and SigNoz integration for observability.

This module provides:
- setup_telemetry() function for OpenTelemetry initialization
- SigNoz OTLP exporter configuration (gRPC)
- Resource attributes setup
- Auto-instrumentation for FastAPI, requests, httpx, SQLAlchemy (asyncpg), redis
- Custom tracer/meter helper functions
"""

import logging
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

# Global flag to track initialization
_telemetry_initialized = False


def setup_telemetry(
    service_name: str,
    service_version: str,
    environment: str,
    otlp_endpoint: str,
    enabled: bool = True,
) -> None:
    """
    Initialize OpenTelemetry with SigNoz OTLP exporter.

    Args:
        service_name: Name of the service (e.g., "auto-trader")
        service_version: Version of the service (e.g., "0.1.0")
        environment: Deployment environment (e.g., "development", "production")
        otlp_endpoint: OTLP gRPC endpoint (e.g., "localhost:4317")
        enabled: Whether telemetry is enabled

    Example:
        setup_telemetry(
            service_name="auto-trader",
            service_version="0.1.0",
            environment="development",
            otlp_endpoint="localhost:4317",
            enabled=True
        )
    """
    global _telemetry_initialized

    if not enabled:
        logger.info("Telemetry is disabled")
        return

    if _telemetry_initialized:
        logger.warning("Telemetry already initialized")
        return

    try:
        # Create resource with service information
        resource = Resource(
            attributes={
                SERVICE_NAME: service_name,
                SERVICE_VERSION: service_version,
                "deployment.environment": environment,
            }
        )

        # Setup trace provider with OTLP exporter
        trace_exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
            insecure=True,  # Use insecure for local development
        )
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(trace_provider)

        # Setup metrics provider with OTLP exporter
        metric_exporter = OTLPMetricExporter(
            endpoint=otlp_endpoint,
            insecure=True,  # Use insecure for local development
        )
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

        # Auto-instrument libraries
        _instrument_libraries()

        _telemetry_initialized = True
        logger.info(
            f"Telemetry initialized: service={service_name}, "
            f"version={service_version}, env={environment}, "
            f"endpoint={otlp_endpoint}"
        )

    except Exception as e:
        logger.error(f"Failed to initialize telemetry: {e}", exc_info=True)
        # Don't raise - allow application to continue without telemetry


def _instrument_libraries() -> None:
    """Auto-instrument supported libraries."""
    try:
        # Instrument requests for HTTP client tracing
        RequestsInstrumentor().instrument()
        logger.debug("requests instrumented")

        # Instrument httpx for async HTTP client tracing
        HTTPXClientInstrumentor().instrument()
        logger.debug("httpx instrumented")

        # Instrument SQLAlchemy for database tracing (works with asyncpg)
        # Note: This will automatically trace all SQLAlchemy operations
        try:
            SQLAlchemyInstrumentor().instrument()
            logger.debug("sqlalchemy instrumented")
        except Exception as e:
            logger.debug(f"sqlalchemy instrumentation skipped: {e}")

        # Instrument Redis for Redis operation tracing
        RedisInstrumentor().instrument()
        logger.debug("redis instrumented")

    except Exception as e:
        logger.warning(f"Failed to instrument some libraries: {e}")


def instrument_fastapi(app) -> None:
    """
    Instrument FastAPI application for automatic tracing.

    This should be called after creating the FastAPI app instance.

    Args:
        app: FastAPI application instance

    Example:
        app = FastAPI()
        instrument_fastapi(app)
    """
    if not _telemetry_initialized:
        logger.warning("Telemetry not initialized, skipping FastAPI instrumentation")
        return

    try:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI instrumented for telemetry")
    except Exception as e:
        logger.error(f"Failed to instrument FastAPI: {e}", exc_info=True)


def get_tracer(name: str) -> trace.Tracer:
    """
    Get a tracer instance for creating custom spans.

    Args:
        name: Name of the tracer (usually module name)

    Returns:
        Tracer instance

    Example:
        tracer = get_tracer(__name__)

        with tracer.start_as_current_span("my_operation") as span:
            span.set_attribute("user_id", user_id)
            # Your code here
    """
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    """
    Get a meter instance for creating custom metrics.

    Args:
        name: Name of the meter (usually module name)

    Returns:
        Meter instance

    Example:
        meter = get_meter(__name__)

        # Create a counter
        request_counter = meter.create_counter(
            name="requests",
            description="Number of requests",
            unit="1"
        )
        request_counter.add(1, {"endpoint": "/api/analyze"})

        # Create a histogram
        duration_histogram = meter.create_histogram(
            name="request_duration",
            description="Request duration",
            unit="ms"
        )
        duration_histogram.record(123.45, {"endpoint": "/api/analyze"})
    """
    return metrics.get_meter(name)


def shutdown_telemetry() -> None:
    """
    Shutdown telemetry and flush remaining data.

    This should be called on application shutdown.
    """
    global _telemetry_initialized

    if not _telemetry_initialized:
        return

    try:
        # Flush trace provider
        trace_provider = trace.get_tracer_provider()
        if hasattr(trace_provider, "shutdown"):
            trace_provider.shutdown()

        # Flush meter provider
        meter_provider = metrics.get_meter_provider()
        if hasattr(meter_provider, "shutdown"):
            meter_provider.shutdown()

        logger.info("Telemetry shutdown complete")
        _telemetry_initialized = False

    except Exception as e:
        logger.error(f"Error during telemetry shutdown: {e}", exc_info=True)


def is_telemetry_initialized() -> bool:
    """
    Check if telemetry is initialized.

    Returns:
        True if telemetry is initialized, False otherwise
    """
    return _telemetry_initialized
