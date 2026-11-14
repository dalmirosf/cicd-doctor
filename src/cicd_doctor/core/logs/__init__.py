"""Job selection, log retention, cleaning, noise filters, excerpt and token budget (SPEC §7.3)."""

from cicd_doctor.core.logs.excerpt import (
    DEFAULT_CONFIG,
    ExcerptConfig,
    build_excerpt,
    build_excerpts,
    render_line,
    share_budget,
)
from cicd_doctor.core.logs.select import failed_step, select_failed_jobs
from cicd_doctor.core.logs.tail import TailBuffer, tail_text

__all__ = [
    "DEFAULT_CONFIG",
    "ExcerptConfig",
    "TailBuffer",
    "build_excerpt",
    "build_excerpts",
    "failed_step",
    "render_line",
    "select_failed_jobs",
    "share_budget",
    "tail_text",
]
