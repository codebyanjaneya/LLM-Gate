#!/usr/bin/env python3
"""
run_check.py - Policy gate for the LLM-Gate pipeline.

What it does:
  1. Gets the Terraform plan as JSON (via `terraform show -json <plan>`,
     or a pre-exported --plan-json file).
  2. Pipes that JSON into `opa eval` against policies/main.rego.
  3. Parses the per-rule results and prints PASS/FAIL for each rule.
  4. Exits 1 if ANY deny rule triggered, so the pipeline can block `apply`.

Run from the project root:
  python policies/run_check.py                        # uses infra/tfplan
  python policies/run_check.py --plan-json plan.json  # skip terraform
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# The named rules defined in main.rego (package terraform.security).
RULES = [
    "deny_open_security_group",
    "deny_unencrypted_volume",
    "deny_missing_tags",
    "deny_public_s3",
    "deny_ssh_open_to_world",
]

# Fallback locations for tools that may not be on PATH (Windows-friendly).
FALLBACKS = {
    "opa": [r"C:\opa\opa.exe"],
    "terraform": [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\terraform.exe")],
}


def resolve_binary(explicit, name):
    """Return a usable path for a CLI tool, honoring an explicit override."""
    if explicit:
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for candidate in FALLBACKS.get(name, []):
        if os.path.exists(candidate):
            return candidate
    return name  # let subprocess surface a clear "not found" error


def load_plan_json(tf_dir="infra", plan="tfplan", plan_json=None, terraform=None):
    """Return the Terraform plan as a JSON string (raises RuntimeError on failure)."""
    if plan_json:
        with open(plan_json, "r", encoding="utf-8") as handle:
            return handle.read()

    terraform_bin = resolve_binary(terraform, "terraform")
    cmd = [terraform_bin, f"-chdir={tf_dir}", "show", "-json", plan]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "`terraform show` failed. Did you run "
            f"`terraform -chdir={tf_dir} plan -out={plan}` first?\n{proc.stderr}"
        )
    return proc.stdout


def run_opa(plan_json, policy="policies/main.rego", opa=None):
    """Evaluate the policy package against the plan JSON via `opa eval`."""
    opa_bin = resolve_binary(opa, "opa")
    cmd = [
        opa_bin, "eval",
        "--stdin-input",          # read the plan JSON from stdin
        "--format", "json",
        "-d", policy,
        "data.terraform.security",
    ]
    proc = subprocess.run(cmd, input=plan_json, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"`opa eval` failed.\n{proc.stderr}")
    return json.loads(proc.stdout)


def extract_rule_results(opa_output):
    """Pull the {rule_name: [messages]} object out of the opa eval result."""
    try:
        return opa_output["result"][0]["expressions"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return {}


def evaluate_policies(tf_dir="infra", plan="tfplan", plan_json=None,
                      policy="policies/main.rego", opa=None, terraform=None):
    """Run the OPA check and return {rule_name: [messages]} for every rule.

    An empty message list means the rule passed. Does not print or exit, so it
    is safe to import and call from other pipeline stages (regenerate.py,
    trust_score.py, orchestrator.py).
    """
    plan_text = load_plan_json(tf_dir, plan, plan_json, terraform)
    raw = extract_rule_results(run_opa(plan_text, policy, opa))
    return {rule: list(raw.get(rule, [])) for rule in RULES}


def main():
    parser = argparse.ArgumentParser(description="LLM-Gate OPA policy gate.")
    parser.add_argument("--tf-dir", default="infra",
                        help="Terraform working directory (default: infra)")
    parser.add_argument("--plan", default="tfplan",
                        help="Plan file name inside --tf-dir (default: tfplan)")
    parser.add_argument("--plan-json",
                        help="Use a pre-exported plan JSON file instead of "
                             "running `terraform show`")
    parser.add_argument("--policy", default="policies/main.rego",
                        help="Path to the Rego policy (default: policies/main.rego)")
    parser.add_argument("--opa", help="Path to the opa binary (default: auto)")
    parser.add_argument("--terraform",
                        help="Path to the terraform binary (default: auto)")
    args = parser.parse_args()

    try:
        results = evaluate_policies(
            tf_dir=args.tf_dir, plan=args.plan, plan_json=args.plan_json,
            policy=args.policy, opa=args.opa, terraform=args.terraform,
        )
    except (RuntimeError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")

    print("=" * 60)
    print(" LLM-Gate - OPA Policy Check")
    print("=" * 60)

    failed_rules = []
    for rule in RULES:
        messages = results.get(rule, [])
        if messages:
            failed_rules.append(rule)
            print(f"[FAIL] {rule}")
            for message in messages:
                print(f"       - {message}")
        else:
            print(f"[PASS] {rule}")

    print("-" * 60)
    if failed_rules:
        print(f"RESULT: FAIL  ({len(failed_rules)} of {len(RULES)} rules triggered)")
        print("Blocking deployment - fix the issues above before `terraform apply`.")
        sys.exit(1)

    print(f"RESULT: PASS  (all {len(RULES)} rules satisfied)")
    sys.exit(0)


if __name__ == "__main__":
    main()
