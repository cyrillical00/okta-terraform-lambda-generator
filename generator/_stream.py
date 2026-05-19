"""Streaming-aware wrapper around `client.messages.create`.

Every Anthropic call in the codebase routes through `streamed_create`. When
the caller supplies an `on_text_delta` callback the call runs as a stream and
each text chunk is forwarded to the callback; when the callback is absent the
call falls through to the standard non-streaming `.create()` path. The final
`Message` object returned has identical shape in both modes (`.content[0].text`,
`.usage`, `.id`, ...), so downstream code stays untouched.

Streamlit wires this callback to a placeholder so the user sees tokens
appearing live during demos instead of staring at a faded-out screen.
qa_runner, CLI, HTTP, Slack, JIRA all pass no callback and stay on the
non-streaming path.
"""
from __future__ import annotations

from typing import Callable, Optional


def streamed_create(
    client,
    *,
    on_text_delta: Optional[Callable[[str], None]] = None,
    **kwargs,
):
    if on_text_delta is None:
        return client.messages.create(**kwargs)
    with client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:
            on_text_delta(chunk)
        return stream.get_final_message()
