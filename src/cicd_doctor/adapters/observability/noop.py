"""The tracers that send nothing: the default without ``LANGFUSE_*`` and a recorder for tests."""

from cicd_doctor.core.models import LLMTrace


class NoopTracer:
    """Drops every trace. What the app and the evals use when Langfuse is not configured."""

    def record(self, trace: LLMTrace) -> None:
        return None

    async def aclose(self, timeout_s: float) -> None:
        return None


class RecordingTracer:
    """Keeps the traces it was given, for tests of the pipeline."""

    def __init__(self) -> None:
        self.traces: list[LLMTrace] = []

    def record(self, trace: LLMTrace) -> None:
        self.traces.append(trace)

    async def aclose(self, timeout_s: float) -> None:
        return None
