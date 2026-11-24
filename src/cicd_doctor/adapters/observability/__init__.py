"""Tracing adapters (D-118): the Langfuse tracer, the no-op default and a recorder for tests."""

from cicd_doctor.adapters.observability.noop import NoopTracer, RecordingTracer

__all__ = ["NoopTracer", "RecordingTracer"]
