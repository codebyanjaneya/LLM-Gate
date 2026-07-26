#!/usr/bin/env python3
"""
orchestrator.py - Run the full LLM-Gate validation pipeline (MVP glue).

Stages (repeated up to --max-retries times until trust score hits 100%):
  1. terraform plan + OPA policy check (security gate).
  2. Functional tests (Selenium + PyTest) against the running app.
  3. Trust-score report -> reports/report.json
  4. If anything failed and GROQ_API_KEY is set, ask the LLM to auto-fix the
     files and re-run. Without a key it runs in detect-only mode.

Run with the project's venv Python from the project root:
  venv\\Scripts\\python orchestrator.py
  venv\\Scripts\\python orchestrator.py --no-start-app --app-url http://localhost:5000
"""

import argparse
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ("policies", "feedback", "reports"):
    sys.path.insert(0, os.path.join(PROJECT_ROOT, _sub))
os.chdir(PROJECT_ROOT)

import run_check    # noqa: E402
import regenerate   # noqa: E402
import trust_score  # noqa: E402

PYTEST_REPORT = os.path.join("reports", "pytest_report.json")
DEFAULT_APP_URL = "http://localhost:5000"


def banner(text):
    print("\n" + "=" * 64)
    print(f" {text}")
    print("=" * 64)


class PlanRefreshError(RuntimeError):
    """Raised when `terraform plan` cannot regenerate the plan file."""


def _terraform(terraform_bin, tf_dir, *args):
    """Run a terraform subcommand inside tf_dir and return the CompletedProcess."""
    return subprocess.run(
        [terraform_bin, f"-chdir={tf_dir}", *args],
        capture_output=True, text=True,
    )


def _raise_terraform_error(stage, proc, tf_dir):
    """Raise PlanRefreshError carrying the real terraform stderr for `stage`."""
    detail = (proc.stderr or proc.stdout).strip()
    hint = ""
    low = detail.lower()
    if "terraform init" in low or "provider requirements" in low:
        hint = (f"\nHINT: run `terraform -chdir={tf_dir} init` "
                "(providers/modules changed), then retry.")
    raise PlanRefreshError(f"`terraform {stage}` failed:\n{detail}{hint}")


def refresh_plan(tf_dir="infra", plan="tfplan"):
    """Regenerate the Terraform plan so OPA evaluates the CURRENT infra/main.tf.

    Raises PlanRefreshError (carrying the real terraform stderr) if the plan
    cannot be regenerated. We deliberately do NOT fall back to a stale tfplan:
    that would make OPA score old infra and hide whether an LLM fix worked - the
    exact bug this function used to have.
    """
    terraform_bin = run_check.resolve_binary(None, "terraform")
    try:
        # 1) validate first - the clearest signal when the LLM writes bad HCL.
        validate = _terraform(terraform_bin, tf_dir, "validate", "-no-color")
        if validate.returncode != 0:
            _raise_terraform_error("validate", validate, tf_dir)
        # 2) plan - `-input=false` so it fails fast instead of prompting.
        plan_proc = _terraform(terraform_bin, tf_dir, "plan", "-no-color",
                               "-input=false", "-out", plan)
        if plan_proc.returncode != 0:
            _raise_terraform_error("plan", plan_proc, tf_dir)
    except FileNotFoundError as exc:
        raise PlanRefreshError(
            f"terraform executable not found ('{terraform_bin}'). Install "
            f"Terraform or add it to PATH.\n{exc}"
        ) from exc
    return plan_proc.stdout


def _print_plan_error(exc):
    """Print the real terraform output from a PlanRefreshError in a visible block."""
    print("  ERROR: could NOT refresh the Terraform plan (the config is invalid).")
    print("  Not falling back to a stale tfplan (that would score old infra and")
    print("  hide whether the fix worked). Real terraform output below:")
    print("  " + "-" * 60)
    for line in str(exc).splitlines():
        print(f"  | {line}")
    print("  " + "-" * 60)


def run_opa_stage():
    """Run the OPA gate; return the results dict, or None if OPA itself errors.

    Propagates PlanRefreshError so the caller can decide what to do: fail fast on
    the baseline run, or feed the terraform error back to the LLM on a retry.
    """
    banner("STAGE 1 - terraform plan + OPA policy check (security gate)")
    refresh_plan()  # raises PlanRefreshError if the infra cannot be planned
    print("  terraform plan refreshed -> infra/tfplan")
    try:
        opa_results = run_check.evaluate_policies()
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: could not run the OPA check: {exc}")
        print("Hint: run `terraform -chdir=infra plan -out=tfplan` first.")
        return None

    failed = [rule for rule, msgs in opa_results.items() if msgs]
    for rule in run_check.RULES:
        state = "FAIL" if opa_results[rule] else "PASS"
        print(f"  [{state}] {rule}")
        for msg in opa_results[rule]:
            print(f"          - {msg}")

    if failed:
        print(f"\nOPA gate: FAIL ({len(failed)} rule(s)). In a real run this blocks "
              "`terraform apply`; continuing to collect functional results for the "
              "trust score.")
    else:
        print("\nOPA gate: PASS - infra is policy-clean.")
    return opa_results


def wait_for_app(app_url, timeout=20):
    """Poll the app until it responds or the timeout elapses."""
    import requests
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(f"{app_url}/login", timeout=2)
            return True
        except requests.RequestException:
            time.sleep(0.5)
    return False


def start_app():
    """Launch the Flask app as a subprocess; return the Popen handle."""
    cmd = [sys.executable, os.path.join("generator", "sample_app.py")]
    creationflags = 0
    if os.name == "nt":
        # New process group so we can kill the debug reloader's child too.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, creationflags=creationflags)


def stop_app(proc):
    """Terminate the app subprocess (and its reloader child)."""
    if proc is None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_pytest_stage(app_url):
    """Run the functional tests (JSON report) and return parsed results."""
    banner("STAGE 2 - Functional tests (Selenium + PyTest)")
    os.makedirs("reports", exist_ok=True)
    env = os.environ.copy()
    env["APP_URL"] = app_url
    cmd = [
        sys.executable, "-m", "pytest", "tests",
        "--json-report",
        f"--json-report-file={PYTEST_REPORT}",
    ]
    subprocess.run(cmd, env=env)

    results = regenerate.parse_pytest_report(PYTEST_REPORT)
    passed = sum(1 for t in results if t["outcome"] == "passed")
    print(f"\npytest: {passed}/{len(results)} passed")
    return results


def run_full_pipeline(app_url, start_app_flag):
    """Run OPA + functional tests once and write the report.

    Returns (report, opa_results, pytest_results), or None if OPA could not run.
    """
    opa_results = run_opa_stage()
    if opa_results is None:
        return None

    app_proc = None
    try:
        if start_app_flag:
            print("\nStarting sample app for functional tests...")
            app_proc = start_app()
            if not wait_for_app(app_url):
                print("WARNING: app did not become reachable in time; tests may fail.")
        pytest_results = run_pytest_stage(app_url)
    finally:
        if app_proc is not None:
            print("Stopping sample app...")
            stop_app(app_proc)

    banner("STAGE 3 - Trust score")
    report = trust_score.generate(opa_results=opa_results,
                                  pytest_results=pytest_results)
    return report, opa_results, pytest_results


def main():
    parser = argparse.ArgumentParser(description="Run the LLM-Gate validation pipeline.")
    parser.add_argument("--app-url", default=DEFAULT_APP_URL,
                        help="URL of the app under test (default: %(default)s)")
    parser.add_argument("--no-start-app", action="store_true",
                        help="Assume the app is already running; do not start/stop it")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max LLM auto-fix attempts (default: %(default)s)")
    args = parser.parse_args()
    start_app_flag = not args.no_start_app

    # Baseline run - strict fail-fast: a broken plan here is a setup problem, not
    # something an LLM fix could be blamed for (there is no prior fix to correct).
    try:
        result = run_full_pipeline(args.app_url, start_app_flag)
    except PlanRefreshError as exc:
        _print_plan_error(exc)
        print("Baseline Terraform plan is invalid - fix infra/main.tf before running.")
        sys.exit(2)
    if result is None:
        sys.exit(2)
    report, opa_results, pytest_results = result

    baseline_score = report["trust_score"]
    scores = [baseline_score]

    llm_ready = regenerate.groq_available()
    if not llm_ready:
        banner("AUTO-FIX - detect-only mode")
        print("GROQ_API_KEY not set (or groq package missing): skipping the auto-fix "
              "loop.\nCopy .env.example to .env and add your key to enable it. Writing "
              "the fix prompt for reference...")
        regenerate.generate_fix_prompt(opa_results=opa_results,
                                       pytest_results=pytest_results)

    attempt = 0
    tf_error = None  # terraform error from the previous attempt's fix, if any
    while report["trust_score"] < 100.0 and attempt < args.max_retries and llm_ready:
        attempt += 1
        banner(f"AUTO-FIX ATTEMPT {attempt}/{args.max_retries}")
        print(f"Attempt {attempt}/{args.max_retries}: trust_score "
              f"{report['trust_score']}% -> re-generating with LLM...")
        if tf_error:
            print("  (feeding the previous terraform validation error back to the LLM)")

        fix_prompt = regenerate.make_fix_prompt(opa_results, pytest_results)
        file_contents = regenerate.read_target_files()
        written = regenerate.call_llm_for_fix(fix_prompt, file_contents,
                                              terraform_error=tf_error)
        if not written:
            print("No files were changed by the LLM; stopping the retry loop.")
            break
        print("LLM rewrote: " + ", ".join(written.keys()))

        # Re-run. If the fix produced invalid HCL, capture the terraform error and
        # feed it back on the NEXT attempt instead of stopping the loop.
        try:
            result = run_full_pipeline(args.app_url, start_app_flag)
        except PlanRefreshError as exc:
            _print_plan_error(exc)
            tf_error = str(exc)
            print("The fix produced invalid Terraform - asking the LLM to correct it "
                  "on the next attempt.")
            continue
        if result is None:
            print("Pipeline could not run after the fix; stopping.")
            break
        report, opa_results, pytest_results = result
        tf_error = None  # plan is valid again
        scores.append(report["trust_score"])

    _print_summary(baseline_score, report["trust_score"], attempt,
                   args.max_retries, scores)
    sys.exit(0 if report["trust_score"] >= 100.0 else 1)


def _print_summary(baseline, final, attempts, max_retries, scores):
    banner("PIPELINE SUMMARY")
    print(f"  Attempts used : {attempts}/{max_retries}")
    print(f"  Trust score   : {baseline}%  ->  {final}%")
    if final >= 100.0:
        print("  Status        : ALL CLEAR (100%)")
    elif final > baseline:
        print(f"  Status        : IMPROVED (+{round(final - baseline, 1)} pts), not yet 100%")
    else:
        print("  Status        : NO IMPROVEMENT")
    print("  Score history : " + " -> ".join(f"{s}%" for s in scores))
    print("  Report        : reports/report.json")


if __name__ == "__main__":
    main()
