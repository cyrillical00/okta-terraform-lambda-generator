"""Tests for qa_runner.py --batch mode (Phase 22b).

Mocks the Anthropic Message Batches API surface:
  - client.messages.batches.create(requests=[...]) -> SimpleNamespace(id=...)
  - client.messages.batches.retrieve(batch_id) -> ns(processing_status=...)
  - client.messages.batches.results(batch_id) -> iterable of result dicts

Covers:
  - _build_batch_request_for_tc shape (custom_id, system w/ cache_control,
    user message, model, max_tokens)
  - _decode_batch_result_to_intent applies build_intent overrides
  - run_batch end-to-end with succeeded + errored entries
  - _poll_batch_until_done eventually returns "ended"
  - _poll_batch_until_done raises TimeoutError when the simulated clock
    advances past 24h
  - Batch usage accumulator tracks tokens for cost reporting

No live Anthropic API calls; every interaction is mocked.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import qa_runner


# ── helpers ──────────────────────────────────────────────────────────────


def _make_msg(text: str, *, input_tokens: int = 100, output_tokens: int = 50,
              cache_read: int = 0, cache_write: int = 0):
    """Build a mock anthropic Message object with .content[0].text + .usage."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


class _MockBatchesAPI:
    def __init__(self, results_map: dict[str, dict], status_sequence: list[str] | None = None):
        """
        results_map: {custom_id -> result dict (e.g. {"type": "succeeded", "message": <msg>})}
        status_sequence: ordered list of statuses returned by retrieve. Defaults to ["ended"].
        """
        self._results_map = results_map
        self._statuses = status_sequence or ["ended"]
        self._status_idx = 0
        self.created_requests: list[dict] | None = None
        self.created_batch_id = "batch_mock_abc123"

    def create(self, *, requests):
        self.created_requests = list(requests)
        return SimpleNamespace(id=self.created_batch_id)

    def retrieve(self, batch_id):
        idx = min(self._status_idx, len(self._statuses) - 1)
        self._status_idx += 1
        return SimpleNamespace(processing_status=self._statuses[idx])

    def results(self, batch_id):
        for custom_id, result in self._results_map.items():
            yield {"custom_id": custom_id, "result": result}


class _MockClient:
    def __init__(self, batches_api):
        self.messages = SimpleNamespace(batches=batches_api)


def _reset_batch_totals():
    for k in qa_runner._BATCH_USAGE_TOTALS:
        qa_runner._BATCH_USAGE_TOTALS[k] = 0


# ── tests: request shape ────────────────────────────────────────────────


def test_build_batch_request_shape():
    tc = qa_runner.TestCase("X01", "Create an okta group", okta_types=["okta_group"])
    req = qa_runner._build_batch_request_for_tc(tc, "claude-haiku-4-5-20251001")

    assert req["custom_id"] == "X01"
    params = req["params"]
    assert params["model"] == "claude-haiku-4-5-20251001"
    assert params["max_tokens"] == 4096

    # system prompt MUST be wrapped with cache_control ephemeral.
    sys_block = params["system"]
    assert isinstance(sys_block, list), "system must be a list of blocks"
    assert len(sys_block) == 1
    assert sys_block[0].get("cache_control") == {"type": "ephemeral"}

    # user message carries the prompt + the hint section.
    user_msg = params["messages"][0]
    assert user_msg["role"] == "user"
    assert "Create an okta group" in user_msg["content"]
    assert "okta_group" in user_msg["content"]


def test_build_batch_request_without_hints():
    """No okta_types -> no hint section in the user message."""
    tc = qa_runner.TestCase("X02", "Make a thing")
    req = qa_runner._build_batch_request_for_tc(tc, "claude-haiku-4-5-20251001")
    content = req["params"]["messages"][0]["content"]
    assert "Resource types explicitly selected" not in content


# ── tests: intent decoding ──────────────────────────────────────────────


def test_decode_succeeded_result_yields_intent():
    _reset_batch_totals()
    raw_intent = {
        "resource_type": "okta_group",
        "operation_type": "create",
        "resource_name": "Engineering",
    }
    msg = _make_msg(json.dumps(raw_intent), input_tokens=500, output_tokens=200)
    entry = {"custom_id": "X01", "result": {"type": "succeeded", "message": msg}}
    tc = qa_runner.TestCase("X01", "Create eng group", okta_types=["okta_group"])

    intent = qa_runner._decode_batch_result_to_intent(entry, tc)
    assert intent["resource_type"] == "okta_group"
    assert intent["output_mode"] == "Okta Terraform only"
    assert intent["resource_types"] == ["okta_group"]
    assert intent["provider_version"] == "~> 4.0"
    assert intent["answers"] == {}

    # Usage was tallied to the batch accumulator.
    assert qa_runner._BATCH_USAGE_TOTALS["calls"] == 1
    assert qa_runner._BATCH_USAGE_TOTALS["input_tokens"] == 500
    assert qa_runner._BATCH_USAGE_TOTALS["output_tokens"] == 200


def test_decode_errored_result_raises():
    _reset_batch_totals()
    entry = {"custom_id": "X02", "result": {"type": "errored", "error": {"message": "rate_limit"}}}
    tc = qa_runner.TestCase("X02", "anything")
    try:
        qa_runner._decode_batch_result_to_intent(entry, tc)
    except RuntimeError as e:
        assert "errored" in str(e)
    else:
        raise AssertionError("expected RuntimeError on errored batch entry")


def test_decode_non_json_message_raises():
    _reset_batch_totals()
    msg = _make_msg("this is not json", input_tokens=10, output_tokens=5)
    entry = {"custom_id": "X03", "result": {"type": "succeeded", "message": msg}}
    tc = qa_runner.TestCase("X03", "test")
    try:
        qa_runner._decode_batch_result_to_intent(entry, tc)
    except RuntimeError as e:
        assert "JSON" in str(e) or "json" in str(e)
    else:
        raise AssertionError("expected RuntimeError on non-JSON response")


def test_decode_applies_output_mode_for_jamf():
    _reset_batch_totals()
    raw = {
        "resource_type": "jamfpro_smart_computer_group",
        "operation_type": "create",
        "resource_name": "macos-laptops",
    }
    msg = _make_msg(json.dumps(raw))
    entry = {"custom_id": "JF01", "result": {"type": "succeeded", "message": msg}}
    tc = qa_runner.TestCase(
        "JF01", "smart group for macos", jamf_types=["jamfpro_smart_computer_group"]
    )
    intent = qa_runner._decode_batch_result_to_intent(entry, tc)
    assert intent["output_mode"] == "JAMF only"
    assert intent["jamf_resource_types"] == ["jamfpro_smart_computer_group"]


# ── tests: polling cadence + timeout ────────────────────────────────────


def test_poll_returns_ended_immediately():
    api = _MockBatchesAPI(results_map={}, status_sequence=["ended"])
    client = _MockClient(api)
    sleeps = []
    status = qa_runner._poll_batch_until_done(
        client, "batch_x", sleep_fn=sleeps.append, now_fn=lambda: 0
    )
    assert status == "ended"
    assert sleeps == [], "no sleep should be needed when first poll already returns ended"


def test_poll_waits_through_in_progress():
    api = _MockBatchesAPI(results_map={}, status_sequence=["in_progress", "in_progress", "ended"])
    client = _MockClient(api)
    sleeps = []
    fake_clock = {"t": 0}

    def fake_now():
        return fake_clock["t"]

    def fake_sleep(s):
        sleeps.append(s)
        fake_clock["t"] += s

    status = qa_runner._poll_batch_until_done(client, "batch_x", sleep_fn=fake_sleep, now_fn=fake_now)
    assert status == "ended"
    # Two sleeps before terminal status; both within the fast phase = 30s each
    assert sleeps == [30, 30], f"expected two 30s sleeps, got {sleeps}"


def test_poll_switches_to_slow_phase_after_5min():
    """After 5 min elapsed, polling cadence drops to 120s."""
    # We need many in_progress entries to outlast 5min of fast-phase sleeps.
    status_seq = ["in_progress"] * 30 + ["ended"]
    api = _MockBatchesAPI(results_map={}, status_sequence=status_seq)
    client = _MockClient(api)
    sleeps = []
    fake_clock = {"t": 0}

    def fake_now():
        return fake_clock["t"]

    def fake_sleep(s):
        sleeps.append(s)
        fake_clock["t"] += s

    status = qa_runner._poll_batch_until_done(client, "batch_x", sleep_fn=fake_sleep, now_fn=fake_now)
    assert status == "ended"
    # First several sleeps should be 30s; later sleeps 120s.
    assert sleeps[0] == 30
    assert 120 in sleeps, f"expected at least one 120s sleep, got {sleeps[:15]}..."


def test_poll_raises_timeout_after_24h():
    api = _MockBatchesAPI(results_map={}, status_sequence=["in_progress"] * 10_000)
    client = _MockClient(api)
    fake_clock = {"t": 0}

    def fake_now():
        return fake_clock["t"]

    def fake_sleep(s):
        # Jump straight past the 24h boundary so the next loop iteration trips it.
        fake_clock["t"] += 25 * 60 * 60

    try:
        qa_runner._poll_batch_until_done(client, "batch_x", sleep_fn=fake_sleep, now_fn=fake_now)
    except TimeoutError as e:
        assert "24h" in str(e) or "batch_x" in str(e)
    else:
        raise AssertionError("expected TimeoutError on 24h elapsed")


# ── tests: run_batch end-to-end ─────────────────────────────────────────


def test_run_batch_decodes_mixed_succeeded_and_errored():
    _reset_batch_totals()
    cases = [
        qa_runner.TestCase("GO1", "Create eng group", okta_types=["okta_group"]),
        qa_runner.TestCase("GO2", "Create hr group", okta_types=["okta_group"]),
    ]
    succ_msg = _make_msg(json.dumps({
        "resource_type": "okta_group",
        "operation_type": "create",
        "resource_name": "Engineering",
    }))
    results_map = {
        "GO1": {"type": "succeeded", "message": succ_msg},
        "GO2": {"type": "errored", "error": {"message": "transient_overload"}},
    }
    api = _MockBatchesAPI(results_map=results_map)
    client = _MockClient(api)

    decoded = qa_runner.run_batch(cases, client, "claude-haiku-4-5-20251001")

    assert "GO1" in decoded and "GO2" in decoded
    assert decoded["GO1"]["resource_type"] == "okta_group"
    assert "_error" in decoded["GO2"], f"errored entry should carry _error, got {decoded['GO2']}"
    assert api.created_requests is not None and len(api.created_requests) == 2


def test_run_test_with_batched_intent_errored_returns_error_row():
    tc = qa_runner.TestCase("E01", "Create a thing", okta_types=["okta_group"])
    r = qa_runner.run_test_with_batched_intent(tc, {"_error": "batch decode failed"}, client=None, model="x")
    assert r["status"] == "ERROR"
    assert any("BatchError" in iss for iss in r["issues"])


def test_run_test_with_batched_intent_validation_failure(monkeypatch):
    """Bad intent (missing operation_type) -> FAIL with validation issues."""
    tc = qa_runner.TestCase("E02", "Create a group", okta_types=["okta_group"])
    # An intent missing operation_type will trip validate_intent.
    bad_intent = {
        "resource_type": "okta_group",
        # operation_type intentionally missing
        "resource_name": "Engineering",
        "output_mode": "Okta Terraform only",
        "provider_version": "~> 4.0",
        "answers": {},
    }
    r = qa_runner.run_test_with_batched_intent(tc, bad_intent, client=None, model="x")
    # Status is FAIL or ERROR depending on which guard catches the bad intent.
    assert r["status"] in ("FAIL", "ERROR")
    joined = " | ".join(r["issues"])
    assert "Intent validation" in joined or "validation" in joined.lower() or r["status"] == "ERROR"


# ── tests: batch usage totals ───────────────────────────────────────────


def test_batch_usage_accumulator_sums_correctly():
    _reset_batch_totals()
    qa_runner._accumulate_batch_usage(SimpleNamespace(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=200, cache_creation_input_tokens=50,
    ))
    qa_runner._accumulate_batch_usage(SimpleNamespace(
        input_tokens=300, output_tokens=100, cache_read_input_tokens=10, cache_creation_input_tokens=0,
    ))
    t = qa_runner._BATCH_USAGE_TOTALS
    assert t["calls"] == 2
    assert t["input_tokens"] == 1300
    assert t["output_tokens"] == 600
    assert t["cache_read_input_tokens"] == 210
    assert t["cache_creation_input_tokens"] == 50
