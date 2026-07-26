#!/usr/bin/env python3
"""
regenerate.py - Feedback loop (MVP).

Collects the failures from both gates and turns them into a single "fix_prompt"
that a future milestone will send back to the LLM to auto-correct its output.

Sources:
  - OPA policy failures : imported directly from policies/run_check.py
  - Functional failures : parsed from the pytest-json-report file
                          (reports/pytest_report.json)

It prints/saves the prompt (reports/fix_prompt.txt) and can also call an LLM
(Groq) to auto-fix the target files in place - see call_llm_for_fix().
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "policies"))

import run_check  # noqa: E402  (path configured above)

# Load GROQ_API_KEY (and any other secrets) from a local .env if present.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

PYTEST_REPORT = os.path.join("reports", "pytest_report.json")
FIX_PROMPT_PATH = os.path.join("reports", "fix_prompt.txt")

# Files the LLM may read and rewrite (allowlist = path-traversal guard).
TARGET_FILES = ["infra/main.tf", "generator/sample_app.py"]
GROQ_MODEL = "llama-3.3-70b-versatile"
BACKUP_ROOT = os.path.join("reports", "backups")


def collect_opa_failures(opa_results=None):
    """Return a flat list of OPA deny messages (failures only)."""
    if opa_results is None:
        opa_results = run_check.evaluate_policies()
    messages = []
    for rule_messages in opa_results.values():
        messages.extend(rule_messages)
    return messages


def _longrepr_text(longrepr):
    """pytest-json-report longrepr may be a str or a dict; normalize to a str."""
    if not longrepr:
        return ""
    if isinstance(longrepr, str):
        return longrepr
    if isinstance(longrepr, dict):
        return longrepr.get("reprcrash", {}).get("message", "") or str(longrepr)
    return str(longrepr)


def _failure_message(test):
    """Pull the most useful error message out of a failed/errored test object."""
    for phase_name in ("call", "setup", "teardown"):
        phase = test.get(phase_name)
        if isinstance(phase, dict) and phase.get("outcome") in ("failed", "error"):
            crash = phase.get("crash") or {}
            message = crash.get("message") or _longrepr_text(phase.get("longrepr"))
            if message:
                return message.strip()
    return ""


def parse_pytest_report(path=PYTEST_REPORT):
    """Return [{nodeid, outcome, message}] parsed from a pytest-json-report file."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    results = []
    for test in data.get("tests", []):
        outcome = test.get("outcome", "unknown")
        message = _failure_message(test) if outcome != "passed" else ""
        results.append({
            "nodeid": test.get("nodeid", "<unknown>"),
            "outcome": outcome,
            "message": message,
        })
    return results


def collect_pytest_failures(pytest_results=None):
    """Return [(nodeid, message)] for failing/errored tests."""
    if pytest_results is None:
        pytest_results = parse_pytest_report()
    return [
        (test["nodeid"], test["message"])
        for test in pytest_results
        if test["outcome"] in ("failed", "error")
    ]


def build_fix_prompt(opa_failures, pytest_failures):
    """Assemble the combined fix-prompt string from both failure lists."""
    lines = ["The following issues were found in AI-generated output:", ""]

    lines.append("[SECURITY] Terraform policy violations (infra/main.tf):")
    if opa_failures:
        lines += [f"  - {msg}" for msg in opa_failures]
    else:
        lines.append("  - none")
    lines.append("")

    lines.append("[FUNCTIONAL] Failing tests (generator/sample_app.py):")
    if pytest_failures:
        for nodeid, msg in pytest_failures:
            short = msg.splitlines()[0] if msg else "(no message)"
            lines.append(f"  - {nodeid}: {short}")
    else:
        lines.append("  - none")
    lines.append("")

    lines.append(
        "Fix these issues in the relevant files: infra/main.tf and/or "
        "generator/sample_app.py. Return only the corrected file contents."
    )
    return "\n".join(lines)


def generate_fix_prompt(opa_results=None, pytest_results=None, save=True):
    """Build the fix prompt from collected results, print it, and (optionally) save."""
    opa_failures = collect_opa_failures(opa_results)
    pytest_failures = collect_pytest_failures(pytest_results)
    prompt = build_fix_prompt(opa_failures, pytest_failures)

    print(prompt)
    if save:
        os.makedirs("reports", exist_ok=True)
        with open(FIX_PROMPT_PATH, "w", encoding="utf-8") as handle:
            handle.write(prompt + "\n")
        print(f"\n[saved] {FIX_PROMPT_PATH}")
    return prompt


def read_target_files():
    """Return {relpath: content} for the files the LLM is allowed to fix."""
    contents = {}
    for rel in TARGET_FILES:
        path = os.path.join(PROJECT_ROOT, rel)
        with open(path, "r", encoding="utf-8") as handle:
            contents[rel] = handle.read()
    return contents


def make_fix_prompt(opa_results, pytest_results):
    """Build the combined fix-prompt string from already-collected results."""
    return build_fix_prompt(
        collect_opa_failures(opa_results),
        collect_pytest_failures(pytest_results),
    )


def groq_available():
    """True only if both the API key and the groq package are present."""
    if not os.environ.get("GROQ_API_KEY"):
        return False
    try:
        import groq  # noqa: F401
    except ImportError:
        return False
    return True


def _strip_code_fence(content):
    """Remove a surrounding ```lang ... ``` fence if the LLM added one."""
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    return "\n".join(lines)


def parse_llm_files(text):
    """Parse '---FILE: <path>---' blocks into {relpath: content}."""
    marker = re.compile(r"^---FILE:\s*(.+?)\s*---\s*$", re.MULTILINE)
    matches = list(marker.finditer(text))
    files = {}
    for index, match in enumerate(matches):
        path = match.group(1).strip().replace("\\", "/")
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        files[path] = _strip_code_fence(text[start:end].strip("\n"))
    return files


def apply_llm_files(parsed):
    """Back up originals (timestamped) then overwrite allowlisted target files."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = os.path.join(PROJECT_ROOT, BACKUP_ROOT, timestamp)
    written = {}
    for rel, content in parsed.items():
        if rel not in TARGET_FILES:
            print(f"  [skip] LLM returned an unexpected path, ignoring: {rel}")
            continue
        target = os.path.join(PROJECT_ROOT, rel)
        if os.path.exists(target):
            backup_path = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            shutil.copy2(target, backup_path)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content if content.endswith("\n") else content + "\n")
        written[rel] = target
    if written:
        print(f"  [backup] originals saved to {os.path.join(BACKUP_ROOT, timestamp)}")
    return written


def _terraform_correction(terraform_error):
    """Instruction telling the LLM to fix the invalid HCL from its last attempt."""
    return (
        "Your previous fix produced invalid Terraform:\n"
        f"{terraform_error}\n"
        "Use real, valid values - no placeholders like 'your_ip_address' or "
        "'YOUR_IP_HERE'. For CIDR restrictions, use a real IP range like "
        "10.0.0.0/16, or just remove the offending rule instead of guessing a "
        "placeholder."
    )


def call_llm_for_fix(fix_prompt, file_contents, terraform_error=None):
    """Ask Groq to correct the files and write the results back.

    If terraform_error is given (a previous attempt produced invalid HCL), it is
    appended to the prompt so the LLM can self-correct. Returns {relpath: abspath}
    for files actually rewritten. Returns {} (safely, without raising) if the
    key/package is missing or the API call fails, so the caller can fall back to
    detect-only mode instead of crashing.
    """
    if not os.environ.get("GROQ_API_KEY"):
        print("  [warn] GROQ_API_KEY not set - skipping LLM auto-fix.")
        return {}
    try:
        from groq import Groq
    except ImportError:
        print("  [warn] groq package not installed - skipping LLM auto-fix.")
        return {}

    if terraform_error:
        fix_prompt = fix_prompt + "\n\n" + _terraform_correction(terraform_error)

    system_msg = (
        "You are a senior DevSecOps engineer. You will be given security/functional "
        "issues and the current contents of one or more files. Return ONLY the "
        "corrected file(s) - no explanations, no commentary, no markdown fences. "
        "Use exactly this format, once per file you change:\n"
        "---FILE: <path>---\n<full corrected file content>\n"
        "Only include files that need changes, and keep everything else intact."
    )
    parts = [fix_prompt, "", "Current file contents:"]
    for rel, content in file_contents.items():
        parts.append(f"\n---FILE: {rel}---\n{content}")
    user_msg = "\n".join(parts)

    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - external API: degrade to detect-only
        print(f"  [warn] Groq API call failed ({exc}); staying in detect-only mode.")
        return {}

    text = response.choices[0].message.content or ""
    parsed = parse_llm_files(text)
    if not parsed:
        print("  [warn] LLM response had no ---FILE: blocks; no changes applied.")
        return {}
    return apply_llm_files(parsed)


def main():
    os.chdir(PROJECT_ROOT)
    generate_fix_prompt()


if __name__ == "__main__":
    main()
