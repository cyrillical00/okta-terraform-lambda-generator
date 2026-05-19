"""Unit tests for generator._stream.streamed_create.

Covers the two branches:
- on_text_delta=None routes to client.messages.create and returns it untouched.
- on_text_delta=callable routes to client.messages.stream, forwards every text
  chunk to the callback in order, and returns stream.get_final_message().

No network calls; the Anthropic client is a hand-rolled stub.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from generator._stream import streamed_create


class _FakeStream:
    def __init__(self, chunks, final_message):
        self._chunks = chunks
        self._final = final_message

    @property
    def text_stream(self):
        for chunk in self._chunks:
            yield chunk

    def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, chunks=None, final_message=None, create_return=None):
        self._chunks = chunks or []
        self._final = final_message
        self._create_return = create_return
        self.create_calls = []
        self.stream_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._create_return

    @contextmanager
    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        yield _FakeStream(self._chunks, self._final)


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


def test_no_callback_routes_to_create_and_returns_it():
    sentinel_return = MagicMock(name="sentinel_message")
    messages = _FakeMessages(create_return=sentinel_return)
    client = _FakeClient(messages)

    result = streamed_create(
        client,
        on_text_delta=None,
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result is sentinel_return
    assert len(messages.create_calls) == 1
    assert messages.create_calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert messages.create_calls[0]["max_tokens"] == 1024
    assert len(messages.stream_calls) == 0


def test_callback_forwards_chunks_in_order():
    chunks = ["{", '"resource', "_type", '": "', 'okta_group', '"}']
    final = MagicMock(name="final_message")
    messages = _FakeMessages(chunks=chunks, final_message=final)
    client = _FakeClient(messages)

    received = []

    def _on_delta(chunk):
        received.append(chunk)

    result = streamed_create(
        client,
        on_text_delta=_on_delta,
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
    )

    assert received == chunks
    assert result is final
    assert len(messages.stream_calls) == 1
    assert len(messages.create_calls) == 0


def test_callback_kwargs_forwarded_unchanged():
    chunks = ["abc"]
    final = MagicMock(name="final")
    messages = _FakeMessages(chunks=chunks, final_message=final)
    client = _FakeClient(messages)

    streamed_create(
        client,
        on_text_delta=lambda c: None,
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        temperature=0.2,
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "hi"}],
    )

    kw = messages.stream_calls[0]
    assert kw["model"] == "claude-haiku-4-5-20251001"
    assert kw["max_tokens"] == 8192
    assert kw["temperature"] == 0.2
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_empty_stream_still_returns_final_message():
    final = MagicMock(name="final")
    messages = _FakeMessages(chunks=[], final_message=final)
    client = _FakeClient(messages)

    received = []
    result = streamed_create(
        client,
        on_text_delta=lambda c: received.append(c),
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
    )

    assert received == []
    assert result is final
