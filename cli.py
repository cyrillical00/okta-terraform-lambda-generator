"""Command-line entry point for the TF Tool generate pipeline.

Wraps core.service.generate() so the same code path that powers the
Streamlit app can be driven from a script, CI job, or shell command.
No Streamlit dependency. Auth via ANTHROPIC_API_KEY env var (and
GITHUB_TOKEN if --push is used).

Usage:
    python cli.py "Create a SAML app for Salesforce"
    python cli.py --stdin --output-dir ./out < prompt.txt
    python cli.py "..." --output-mode "Both" --no-refine
    python cli.py "..." --push owner/repo --branch feature/auto-tfgen
    python cli.py "..." --json   (print outputs to stdout, do not write files)

Exit codes:
    0  success
    1  bad usage / config (missing API key, empty prompt, etc.)
    2  generation error or model returned nothing usable
    3  GitHub push failed (only when --push is set)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic

from core import service as core_service


VALID_MODES = ("Both", "Okta Terraform only", "Lambda only", "GCP only", "Okta + GCP", "JAMF only", "Okta + JAMF", "Fleet GitOps only", "Okta + Fleet GitOps", "Fleet TF only", "Okta + Fleet TF")


def _read_prompt(args: argparse.Namespace) -> str:
    """Resolve the prompt from --prompt, --stdin, or piped stdin (no flag)."""
    if args.prompt:
        return args.prompt.strip()
    if args.stdin or not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise ValueError("provide prompt as positional arg or pipe via stdin (or pass --stdin)")


def _build_file_map(outputs: dict, mode: str, basename: str = "") -> dict[str, str]:
    """Map service outputs to filename, content pairs.

    Mirrors the legacy single-prompt path of app.py:_build_files. The
    optional basename arg namespaces filenames so multiple prompts can
    coexist in the same output directory (e.g. terraform/hr_portal.tf
    vs terraform/okta.tf). When basename is empty, the legacy fixed
    paths are used.
    """
    base = (basename or "").strip()

    def n(default: str, ns_template: str) -> str:
        return ns_template.format(base=base) if base else default

    files: dict[str, str] = {}
    if mode in ("Both", "Okta Terraform only", "Okta + GCP", "Okta + JAMF", "Okta + Fleet GitOps", "Okta + Fleet TF"):
        v = (outputs.get("terraform_okta_hcl") or "").strip()
        if v:
            files[n("terraform/okta.tf", "terraform/{base}.tf")] = v
    if mode == "Both":
        v = (outputs.get("terraform_lambda_hcl") or "").strip()
        if v:
            files[n("terraform/lambda.tf", "terraform/{base}_lambda.tf")] = v
    if mode in ("Both", "Lambda only"):
        v = (outputs.get("lambda_python") or "").strip()
        if v:
            files[n("lambda/lambda_function.py", "lambda/{base}.py")] = v
        v = (outputs.get("lambda_requirements") or "").strip()
        if v:
            files[n("lambda/requirements.txt", "lambda/{base}_requirements.txt")] = v
    if mode in ("GCP only", "Okta + GCP"):
        v = (outputs.get("terraform_gcp_hcl") or "").strip()
        if v:
            files[n("terraform/gcp.tf", "terraform/{base}_gcp.tf")] = v
        v = (outputs.get("cloud_function_python") or "").strip()
        if v:
            files[n("cloud_function/main.py", "cloud_function/{base}.py")] = v
        v = (outputs.get("cloud_function_requirements") or "").strip()
        if v:
            files[n("cloud_function/requirements.txt", "cloud_function/{base}_requirements.txt")] = v
    if mode in ("JAMF only", "Okta + JAMF"):
        v = (outputs.get("terraform_jamf_hcl") or "").strip()
        if v:
            files[n("terraform/jamf.tf", "terraform/{base}_jamf.tf")] = v
    if mode in ("Fleet GitOps only", "Okta + Fleet GitOps"):
        v = (outputs.get("fleet_gitops_yaml") or "").strip()
        if v:
            files[n("fleet/default.yml", "fleet/{base}.yml")] = v
    if mode in ("Fleet TF only", "Okta + Fleet TF"):
        v = (outputs.get("terraform_fleet_hcl") or "").strip()
        if v:
            files[n("terraform/fleet.tf", "terraform/{base}_fleet.tf")] = v
    v = (outputs.get("optional_tf") or "").strip()
    if v:
        files[n("terraform/optional_extensions.tf", "terraform/{base}_optional_extensions.tf")] = v
    v = (outputs.get("terraform_tfvars_example") or "").strip()
    if v:
        files[n("terraform/terraform.tfvars.example", "terraform/{base}.tfvars.example")] = v
    return files


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tfgen",
        description="Generate Okta + AWS Lambda + GCP Terraform from plain English.",
        epilog="Auth: set ANTHROPIC_API_KEY (and GITHUB_TOKEN for --push).",
    )
    p.add_argument("prompt", nargs="?", help="Plain-English description of the operation.")
    p.add_argument("--stdin", action="store_true", help="Read prompt from stdin (also auto-on if stdin is piped).")
    p.add_argument("--output-dir", default="./tfgen-out",
                   help="Directory to write generated files into (default: ./tfgen-out).")
    p.add_argument("--basename", default="",
                   help="Filename base for namespacing (e.g. 'hr_portal' yields terraform/hr_portal.tf).")
    p.add_argument("--output-mode", default="Both", choices=VALID_MODES,
                   help="Which files to generate.")
    p.add_argument("--provider-version", default="~> 4.0",
                   help="Okta provider version constraint (default: ~> 4.0).")
    p.add_argument("--max-passes", type=int, default=3,
                   help="Validate-and-fix passes (1 to 5; default 3).")
    p.add_argument("--no-refine", action="store_true",
                   help="Skip the validate-and-fix loop entirely (one pass only, faster, less reliable).")
    p.add_argument("--push", metavar="OWNER/REPO",
                   help="After generation, push files to this GitHub repo.")
    p.add_argument("--branch", default="main", help="Target branch for --push (default: main).")
    p.add_argument("--commit-message", default="",
                   help="Custom commit message for --push (defaults to auto-derived from intent).")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="Anthropic model id.")
    p.add_argument("--print-intent", action="store_true",
                   help="Print the parsed intent JSON to stderr before generation.")
    p.add_argument("--json", action="store_true",
                   help="Print outputs as JSON to stdout instead of writing files (incompatible with --push).")
    return p


def _on_pass(pass_num: int, result: dict, has_issues: bool) -> None:
    n_tf = len(result.get("terraform_issues") or [])
    n_lam = len(result.get("lambda_issues") or [])
    if has_issues:
        print(f"  pass {pass_num}/3: refining ({n_tf}+{n_lam} issues)", file=sys.stderr)
    else:
        print(f"  pass {pass_num}/3: clean", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1
    if not api_key.startswith("sk-ant"):
        print(f"ERROR: ANTHROPIC_API_KEY does not look right (starts with '{api_key[:8]}...')",
              file=sys.stderr)
        return 1

    try:
        prompt = _read_prompt(args)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not prompt:
        print("ERROR: empty prompt", file=sys.stderr)
        return 1

    client = anthropic.Anthropic(api_key=api_key)
    max_passes = 1 if args.no_refine else max(1, min(5, args.max_passes))

    print(f"tfgen: mode={args.output_mode}, passes={max_passes}", file=sys.stderr)
    print(f"tfgen: parsing intent + generating...", file=sys.stderr)
    result = core_service.generate(
        prompt,
        client=client,
        model=args.model,
        output_mode=args.output_mode,
        provider_version=args.provider_version,
        max_passes=max_passes,
        on_pass=_on_pass,
    )

    if args.print_intent:
        print("--- intent ---", file=sys.stderr)
        print(json.dumps(result.intent, indent=2), file=sys.stderr)
        print("--- end intent ---", file=sys.stderr)

    if result.cancelled:
        print("ERROR: generation cancelled", file=sys.stderr)
        return 2

    if result.error:
        print(f"ERROR: {result.error}", file=sys.stderr)
        if result.error_raw_response:
            print("--- raw response ---", file=sys.stderr)
            print(result.error_raw_response, file=sys.stderr)
        return 2

    outputs = result.outputs or {}
    files = _build_file_map(outputs, args.output_mode, args.basename)
    if not files:
        print("ERROR: generation returned no files for the chosen mode", file=sys.stderr)
        return 2

    if args.json:
        if args.push:
            print("ERROR: --json and --push cannot be combined", file=sys.stderr)
            return 1
        json.dump({"intent": result.intent, "files": files}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in files.items():
            full = out_dir / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        print(f"tfgen: wrote {len(files)} file(s) to {out_dir}", file=sys.stderr)
        for rel_path in sorted(files):
            print(f"  {rel_path}", file=sys.stderr)

    if args.push:
        gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        if not gh_token:
            print("ERROR: --push requires GITHUB_TOKEN env var", file=sys.stderr)
            return 3
        from gh_push.push import push_to_github, build_commit_message
        msg = args.commit_message or build_commit_message(result.intent)
        try:
            url = push_to_github(files, args.push, gh_token, msg, branch=args.branch)
            print(f"tfgen: pushed -> {url}", file=sys.stderr)
        except (RuntimeError, Exception) as e:
            print(f"ERROR: push failed: {e}", file=sys.stderr)
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
