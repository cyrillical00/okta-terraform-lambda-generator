"""Pure-Python validator for Fleet GitOps YAML (`fleet_gitops_yaml` output key).

Mirrors `tf_validate.py:run_terraform` shape but operates on YAML instead of
HCL. The validator is structural: it confirms the YAML parses cleanly, the
top-level keys are on Fleet's allowlist, and each resource block carries the
fields Fleet's `fleetctl apply` requires. The validator does NOT exercise
Fleet's full schema — semantic checks like "is this osquery SQL valid?" or
"does this label name already exist on the server?" require a live Fleet
instance and are out of scope.

Two-pass design:

1. Pure-Python pass (always runs):
   - PyYAML `safe_load` parses the document.
   - Top-level keys validated against the allowlist.
   - Per-resource shape checks: required fields, mutually exclusive fields,
     enum values, type checks.
   - Apply runbook header presence.

2. Optional fleetctl dry-run (runs when `fleetctl` is on PATH):
   - Writes the YAML to a temp file and runs `fleetctl apply -f <tmp> --dry-run`.
   - Surfaces any server-side schema errors. Skipped in CI / Streamlit Cloud
     where fleetctl is not installed.

Public API: `validate_fleet_yaml(yaml_text) -> tuple[bool, str]`.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with Streamlit
    yaml = None  # type: ignore


_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "labels",
    "policies",
    "queries",
    "agent_options",
    "controls",
    "software",
    "org_settings",
    "fleets",
    "reports",
    "settings",
})

_VALID_PLATFORMS = frozenset({"darwin", "windows", "linux", "chrome"})
_VALID_LABEL_MEMBERSHIP_TYPES = frozenset({"dynamic", "manual"})

# The mandatory header lines emitted at the top of every fleet_gitops_yaml
# output per generator/prompts.py SECTION I. Validator confirms presence.
_RUNBOOK_HEADER_LINES = (
    "# FLEET GITOPS APPLY RUNBOOK",
    "fleetctl apply -f default.yml --dry-run",
)


def validate_fleet_yaml(yaml_text: str) -> tuple[bool, str]:
    """Parse Fleet GitOps YAML and validate structure.

    Args:
        yaml_text: The raw YAML string from `outputs["fleet_gitops_yaml"]`.

    Returns:
        (ok, message). On success message is "Fleet GitOps YAML is valid".
        On failure message is a short human-readable description of the first
        violation, prefixed with `yaml:` for syntax errors and `fleet:` for
        schema errors.
    """
    if yaml is None:
        return False, "fleet: PyYAML is not installed; cannot validate YAML output"

    if not yaml_text or not yaml_text.strip():
        return False, "fleet: empty YAML output"

    # Pass 1a — apply runbook header presence (before YAML parse so we don't
    # accidentally accept a syntactically valid YAML that omits the runbook).
    if not _has_runbook_header(yaml_text):
        return False, "fleet: missing `# FLEET GITOPS APPLY RUNBOOK` header (see SECTION I)"

    # Pass 1b — YAML syntax.
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            return False, f"yaml: line {mark.line + 1} col {mark.column + 1}: {e.problem or e}"
        return False, f"yaml: {e}"

    if doc is None:
        return False, "fleet: YAML parses to None (the document only contains comments)"

    if not isinstance(doc, dict):
        return False, f"fleet: top-level must be a mapping, got {type(doc).__name__}"

    # Pass 2 — top-level key allowlist.
    unknown_keys = set(doc.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        return False, f"fleet: unknown top-level key(s): {sorted(unknown_keys)}"

    # Pass 3 — per-resource shape checks.
    err = _validate_policies(doc.get("policies"))
    if err:
        return False, err
    err = _validate_labels(doc.get("labels"))
    if err:
        return False, err
    err = _validate_queries(doc.get("queries"))
    if err:
        return False, err
    err = _validate_controls(doc.get("controls"))
    if err:
        return False, err
    err = _validate_agent_options(doc.get("agent_options"))
    if err:
        return False, err

    return True, "Fleet GitOps YAML is valid"


def _has_runbook_header(yaml_text: str) -> bool:
    """Confirm the apply runbook header is present near the top of the file.

    Conservative: scans the first 20 lines for both the leading comment and
    a reference to `fleetctl apply --dry-run`. Order is not enforced but
    proximity is.
    """
    head = "\n".join(yaml_text.splitlines()[:20])
    return all(needle in head for needle in _RUNBOOK_HEADER_LINES)


def _validate_policies(policies) -> str | None:
    if policies is None:
        return None
    if not isinstance(policies, list):
        return "fleet: `policies` must be a list"
    for i, p in enumerate(policies):
        if not isinstance(p, dict):
            return f"fleet: policies[{i}] must be a mapping, got {type(p).__name__}"
        if not p.get("name"):
            return f"fleet: policies[{i}] missing required field `name`"
        if not p.get("query"):
            return f"fleet: policies[{i}] missing required field `query`"
        plat = p.get("platform")
        if not plat:
            return f"fleet: policies[{i}] missing required field `platform`"
        if not _is_valid_platform_string(plat):
            return f"fleet: policies[{i}] platform `{plat}` not in {sorted(_VALID_PLATFORMS)}"
    return None


def _validate_labels(labels) -> str | None:
    if labels is None:
        return None
    if not isinstance(labels, list):
        return "fleet: `labels` must be a list"
    for i, l in enumerate(labels):
        if not isinstance(l, dict):
            return f"fleet: labels[{i}] must be a mapping, got {type(l).__name__}"
        if not l.get("name"):
            return f"fleet: labels[{i}] missing required field `name`"
        membership_fields = [k for k in ("query", "hosts", "criteria") if k in l]
        if len(membership_fields) == 0:
            return f"fleet: labels[{i}] requires exactly one of `query`, `hosts`, or `criteria`"
        if len(membership_fields) > 1:
            return f"fleet: labels[{i}] has mutually exclusive fields: {membership_fields}"
        mtype = l.get("label_membership_type")
        if mtype is not None and mtype not in _VALID_LABEL_MEMBERSHIP_TYPES:
            return f"fleet: labels[{i}] label_membership_type `{mtype}` not in {sorted(_VALID_LABEL_MEMBERSHIP_TYPES)}"
    return None


def _validate_queries(queries) -> str | None:
    if queries is None:
        return None
    if not isinstance(queries, list):
        return "fleet: `queries` must be a list"
    for i, q in enumerate(queries):
        if not isinstance(q, dict):
            return f"fleet: queries[{i}] must be a mapping, got {type(q).__name__}"
        if not q.get("name"):
            return f"fleet: queries[{i}] missing required field `name`"
        if not q.get("query"):
            return f"fleet: queries[{i}] missing required field `query`"
        interval = q.get("interval")
        if interval is None:
            return f"fleet: queries[{i}] missing required field `interval`"
        if not isinstance(interval, int) or isinstance(interval, bool):
            return f"fleet: queries[{i}] interval must be an integer (seconds), got {type(interval).__name__}"
    return None


def _validate_controls(controls) -> str | None:
    if controls is None:
        return None
    if not isinstance(controls, dict):
        return "fleet: `controls` must be a mapping"

    for platform_key in ("apple_settings", "windows_settings"):
        settings = controls.get(platform_key)
        if settings is None:
            continue
        if not isinstance(settings, dict):
            return f"fleet: controls.{platform_key} must be a mapping"
        profiles = settings.get("configuration_profiles")
        if profiles is None:
            continue
        if not isinstance(profiles, list):
            return f"fleet: controls.{platform_key}.configuration_profiles must be a list"
        for i, prof in enumerate(profiles):
            if not isinstance(prof, dict):
                return f"fleet: controls.{platform_key}.configuration_profiles[{i}] must be a mapping"
            has_path = "path" in prof
            has_paths = "paths" in prof
            if not has_path and not has_paths:
                return f"fleet: controls.{platform_key}.configuration_profiles[{i}] requires `path` or `paths`"
            if has_path and has_paths:
                return f"fleet: controls.{platform_key}.configuration_profiles[{i}] has mutually exclusive `path` and `paths`"

    return None


def _validate_agent_options(agent_options) -> str | None:
    if agent_options is None:
        return None
    if not isinstance(agent_options, dict):
        return "fleet: `agent_options` must be a mapping"
    config = agent_options.get("config")
    if config is None:
        return "fleet: agent_options requires a `config` sub-key"
    if not isinstance(config, dict):
        return "fleet: agent_options.config must be a mapping"
    decorators = config.get("decorators")
    if decorators is not None:
        if not isinstance(decorators, dict):
            return "fleet: agent_options.config.decorators must be a mapping"
        load = decorators.get("load")
        if load is not None:
            if not isinstance(load, list):
                return "fleet: agent_options.config.decorators.load must be a list of strings"
            for i, item in enumerate(load):
                if not isinstance(item, str):
                    return f"fleet: agent_options.config.decorators.load[{i}] must be a string"
    return None


def _is_valid_platform_string(plat) -> bool:
    """Accept a single platform name or a comma-separated list of names."""
    if not isinstance(plat, str):
        return False
    parts = [p.strip() for p in plat.split(",") if p.strip()]
    return bool(parts) and all(p in _VALID_PLATFORMS for p in parts)


def fleetctl_dry_run(yaml_text: str) -> tuple[bool, str]:
    """Optional second-pass validator: write the YAML to a temp file and shell
    out to `fleetctl apply --dry-run`. No-op when fleetctl is not on PATH.

    Returns (True, "skipped") when fleetctl is not installed so callers can
    treat it as a soft signal.
    """
    fleetctl = shutil.which("fleetctl")
    if not fleetctl:
        return True, "skipped: fleetctl not on PATH"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        tmp_path = Path(f.name)

    try:
        result = subprocess.run(
            [fleetctl, "apply", "-f", str(tmp_path), "--dry-run"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return True, "fleetctl dry-run succeeded"
        excerpt = (result.stderr or result.stdout).strip().split("\n")
        first_err = next((l for l in excerpt if "Error" in l or "error" in l), excerpt[0] if excerpt else "fleetctl failed")
        return False, f"fleetctl: {first_err[:160]}"
    except subprocess.TimeoutExpired:
        return False, "fleetctl: dry-run timed out"
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
