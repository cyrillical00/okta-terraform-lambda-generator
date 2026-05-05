"""Core orchestration package for the TF Tool.

This package holds the framework-agnostic glue that drives the parse,
generate, refine, and post-process pipeline. The Streamlit app, the
qa_runner, and any future CLI / HTTP / Slack / JIRA frontend all call into
the same primitives so behaviour stays consistent across surfaces.
"""

from .service import (
    GenerateResult,
    GenerationCancelled,
    generate,
    generate_from_intent,
)

__all__ = [
    "GenerateResult",
    "GenerationCancelled",
    "generate",
    "generate_from_intent",
]
