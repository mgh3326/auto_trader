"""
OpenTelemetry and SigNoz integration for observability.

This module provides:
- OpenTelemetry instrumentation setup
- SigNoz exporter configuration
- Custom span and metric helpers
- Auto-instrumentation for FastAPI, httpx, SQLAlchemy, Redis
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)


class TelemetryConfig:
    """Configuration for telemetry setup."""

    def __init__(
        self,
        service_name: str,
        otlp_endpoint: str,
        environment: str = "development",
        enabled: bool = True,
    ):
        self.service_name = service_name
        self.otlp_endpoint = otlp_endpoint
        self.environment = environment
        self.enabled = enabled


class TelemetryManager:
    """Manages OpenTelemetry instrumentation and SigNoz integration."""

    def __init__(self, config: TelemetryConfig):
        self.config = config
        self._tracer: Optional[trace.Tracer] = None
        self._meter: Optional[metrics.Meter] = None
        self._initialized = False

    def initialize(self) -> None:
        """Initialize OpenTelemetry instrumentation."""
        if not self.config.enabled:
            logger.info("Telemetry is disabled")
            return

        if self._initialized:
            logger.warning("Telemetry already initialized")
            return

        try:
            # Create resource with service information
            resource = Resource(
                attributes={
                    SERVICE_NAME: self.config.service_name,
                    "environment": self.config.environment,
                }
            )

            # Setup trace provider
            trace_provider = TracerProvider(resource=resource)
            trace_exporter = OTLPSpanExporter(endpoint=self.config.otlp_endpoint)
            trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
            trace.set_tracer_provider(trace_provider)

            # Setup metrics provider
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=self.config.otlp_endpoint)
            )
            meter_provider = MeterProvider(
                resource=resource, metric_readers=[metric_reader]
            )
            metrics.set_meter_provider(meter_provider)

            # Get tracer and meter instances
            self._tracer = trace.get_tracer(__name__)
            self._meter = metrics.get_meter(__name__)

            # Auto-instrument libraries
            self._instrument_libraries()

            self._initialized = True
            logger.info(
                f"Telemetry initialized for service '{self.config.service_name}' "
                f"(endpoint: {self.config.otlp_endpoint})"
            )

        except Exception as e:
            logger.error(f"Failed to initialize telemetry: {e}", exc_info=True)
            # Don't raise - allow application to continue without telemetry

    def _instrument_libraries(self) -> None:
        """Auto-instrument supported libraries."""
        try:
            # Instrument httpx for HTTP client tracing
            HTTPXClientInstrumentor().instrument()
            logger.debug("HTTPx instrumented")

            # Instrument SQLAlchemy for database query tracing
            SQLAlchemyInstrumentor().instrument()
            logger.debug("SQLAlchemy instrumented")

            # Instrument Redis for Redis operation tracing
            RedisInstrumentor().instrument()
            logger.debug("Redis instrumented")

        except Exception as e:
            logger.warning(f"Failed to instrument some libraries: {e}")

    def instrument_fastapi(self, app) -> None:
        """
        Instrument FastAPI application.

        Args:
            app: FastAPI application instance
        """
        if not self.config.enabled or not self._initialized:
            return

        try:
            FastAPIInstrumentor.instrument_app(app)
            logger.info("FastAPI instrumented for telemetry")
        except Exception as e:
            logger.error(f"Failed to instrument FastAPI: {e}", exc_info=True)

    @contextmanager
    def trace_operation(
        self,
        operation_name: str,
        attributes: Optional[Dict[str, Any]] = None,
        record_exception: bool = True,
    ):
        """
        Context manager for tracing custom operations.

        Args:
            operation_name: Name of the operation being traced
            attributes: Optional attributes to add to the span
            record_exception: Whether to record exceptions in the span

        Usage:
            with telemetry_manager.trace_operation("process_data", {"user_id": 123}):
                # Your code here
                pass
        """
        if not self.config.enabled or not self._tracer:
            # If telemetry is disabled, just yield without tracing
            yield None
            return

        with self._tracer.start_as_current_span(operation_name) as span:
            if attributes:
                span.set_attributes(attributes)

            try:
                yield span
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                if record_exception:
                    span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise

    def add_span_attribute(self, key: str, value: Any) -> None:
        """
        Add an attribute to the current span.

        Args:
            key: Attribute key
            value: Attribute value
        """
        if not self.config.enabled:
            return

        try:
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute(key, value)
        except Exception as e:
            logger.debug(f"Failed to add span attribute: {e}")

    def record_counter(
        self, name: str, value: int = 1, attributes: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Record a counter metric.

        Args:
            name: Counter name
            value: Counter value (default: 1)
            attributes: Optional attributes for the metric
        """
        if not self.config.enabled or not self._meter:
            return

        try:
            counter = self._meter.create_counter(name)
            counter.add(value, attributes or {})
        except Exception as e:
            logger.debug(f"Failed to record counter: {e}")

    def record_histogram(
        self, name: str, value: float, attributes: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Record a histogram metric.

        Args:
            name: Histogram name
            value: Value to record
            attributes: Optional attributes for the metric
        """
        if not self.config.enabled or not self._meter:
            return

        try:
            histogram = self._meter.create_histogram(name)
            histogram.record(value, attributes or {})
        except Exception as e:
            logger.debug(f"Failed to record histogram: {e}")

    def shutdown(self) -> None:
        """Shutdown telemetry and flush remaining data."""
        if not self._initialized:
            return

        try:
            # Flush trace provider
            if trace.get_tracer_provider():
                trace.get_tracer_provider().shutdown()

            # Flush meter provider
            if metrics.get_meter_provider():
                metrics.get_meter_provider().shutdown()

            logger.info("Telemetry shutdown complete")
        except Exception as e:
            logger.error(f"Error during telemetry shutdown: {e}", exc_info=True)


# Global telemetry manager instance
_telemetry_manager: Optional[TelemetryManager] = None


def get_telemetry_manager() -> Optional[TelemetryManager]:
    """Get the global telemetry manager instance."""
    return _telemetry_manager


def set_telemetry_manager(manager: TelemetryManager) -> None:
    """Set the global telemetry manager instance."""
    global _telemetry_manager
    _telemetry_manager = manager
