"""Tests for `generator.gcp_cloudfunctions2_trigger_sanitizer`.

Standalone-runnable:
    python tests/test_gcp_cloudfunctions2_trigger_sanitizer.py
"""

from __future__ import annotations

import os
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from generator.gcp_cloudfunctions2_trigger_sanitizer import (
    sanitize_gcp_cloudfunctions2_trigger,
)


# ── Positive drift cases ───────────────────────────────────────────────────


def test_bare_trigger_block_rewritten_to_event_trigger():
    """GCP02 canonical drift: `trigger { ... }` -> `event_trigger { ... }`."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name     = var.function_name
          location = var.gcp_region

          trigger {
            event_type   = "google.cloud.pubsub.topic.v1.messagePublished"
            pubsub_topic = google_pubsub_topic.handler.id
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    out = result["terraform_gcp_hcl"]
    # Bare `trigger {` is gone.
    assert "\n  trigger {" not in out
    # event_trigger is now present.
    assert "event_trigger {" in out
    # Auto-filled defaults.
    assert "trigger_region" in out
    assert "RETRY_POLICY_RETRY" in out


def test_topic_name_rewritten_to_pubsub_topic():
    """Drift inside an event_trigger block: `topic_name` -> `pubsub_topic`."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          event_trigger {
            event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
            topic_name     = google_pubsub_topic.handler.id
            trigger_region = var.gcp_region
            retry_policy   = "RETRY_POLICY_RETRY"
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    out = result["terraform_gcp_hcl"]
    # `topic_name` line is gone.
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("topic_name"), f"leaked: {line!r}"
    assert "pubsub_topic" in out
    assert "google_pubsub_topic.handler.id" in out


def test_combined_drift_trigger_plus_topic_name():
    """Combined: bare `trigger {}` AND `topic_name` inside it."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          trigger {
            event_type = "google.cloud.pubsub.topic.v1.messagePublished"
            topic_name = google_pubsub_topic.handler.id
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    out = result["terraform_gcp_hcl"]
    assert "event_trigger {" in out
    assert "pubsub_topic" in out
    assert "trigger_region" in out
    assert "RETRY_POLICY_RETRY" in out
    # Original drift gone.
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("topic_name"), f"leaked: {line!r}"
    assert "\n  trigger {" not in out


def test_missing_trigger_region_auto_filled():
    """Pub/Sub event_trigger missing trigger_region gets it auto-filled."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          event_trigger {
            event_type   = "google.cloud.pubsub.topic.v1.messagePublished"
            pubsub_topic = google_pubsub_topic.handler.id
            retry_policy = "RETRY_POLICY_RETRY"
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    out = result["terraform_gcp_hcl"]
    assert "trigger_region" in out
    assert "var.gcp_region" in out


def test_missing_retry_policy_auto_filled():
    """Pub/Sub event_trigger missing retry_policy gets it auto-filled."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          event_trigger {
            event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
            pubsub_topic   = google_pubsub_topic.handler.id
            trigger_region = var.gcp_region
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    out = result["terraform_gcp_hcl"]
    assert 'retry_policy   = "RETRY_POLICY_RETRY"' in out


# ── Negative / idempotent cases ────────────────────────────────────────────


def test_clean_pubsub_event_trigger_unchanged():
    """A clean Pub/Sub event_trigger with all 4 fields is left alone."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          event_trigger {
            event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
            pubsub_topic   = google_pubsub_topic.handler.id
            trigger_region = var.gcp_region
            retry_policy   = "RETRY_POLICY_RETRY"
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    assert result["terraform_gcp_hcl"] == hcl


def test_gcs_event_trigger_not_auto_filled():
    """A GCS event_trigger (no pubsub_topic) must NOT have Pub/Sub defaults
    auto-filled. Only the topic_name -> pubsub_topic rewrite would fire on
    a GCS trigger, but GCS triggers do not use pubsub_topic at all."""
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          event_trigger {
            event_type     = "google.cloud.storage.object.v1.finalized"
            event_filters {
              attribute = "bucket"
              value     = "document-uploads"
            }
          }
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    out = result["terraform_gcp_hcl"]
    # No auto-fill for GCS triggers (no pubsub_topic and no Pub/Sub event_type).
    assert "trigger_region" not in out
    assert "RETRY_POLICY_RETRY" not in out


def test_no_function_block_is_noop():
    hcl = textwrap.dedent('''\
        resource "google_pubsub_topic" "events" {
          name = "events"
        }
        ''')
    result = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    assert result["terraform_gcp_hcl"] == hcl


def test_idempotent():
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          trigger {
            event_type = "google.cloud.pubsub.topic.v1.messagePublished"
            topic_name = google_pubsub_topic.handler.id
          }
        }
        ''')
    once = sanitize_gcp_cloudfunctions2_trigger({"terraform_gcp_hcl": hcl})
    twice = sanitize_gcp_cloudfunctions2_trigger(once)
    assert once["terraform_gcp_hcl"] == twice["terraform_gcp_hcl"]


def test_input_dict_not_mutated():
    hcl = textwrap.dedent('''\
        resource "google_cloudfunctions2_function" "handler" {
          name = var.function_name

          trigger {
            event_type = "google.cloud.pubsub.topic.v1.messagePublished"
            topic_name = google_pubsub_topic.handler.id
          }
        }
        ''')
    outputs = {"terraform_gcp_hcl": hcl}
    sanitize_gcp_cloudfunctions2_trigger(outputs)
    assert outputs["terraform_gcp_hcl"] == hcl


if __name__ == "__main__":
    import traceback
    failures = []
    tests = [
        test_bare_trigger_block_rewritten_to_event_trigger,
        test_topic_name_rewritten_to_pubsub_topic,
        test_combined_drift_trigger_plus_topic_name,
        test_missing_trigger_region_auto_filled,
        test_missing_retry_policy_auto_filled,
        test_clean_pubsub_event_trigger_unchanged,
        test_gcs_event_trigger_not_auto_filled,
        test_no_function_block_is_noop,
        test_idempotent,
        test_input_dict_not_mutated,
    ]
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
