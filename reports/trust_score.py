#!/usr/bin/env python3
"""
trust_score.py - Compute the LLM-Gate trust score and write reports/report.json.

    trust_score = (passed OPA rules + passed pytest tests) / total checks * 100

Can be imported (call `generate(opa_results, pytest_results)`) or run standalone,
in which case it gathers OPA results itself and reads reports/pytest_report.json.
"""

import json
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "policies"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "feedback"))

import run_check   # noqa: E402
import regenerate  # noqa: E402  (reuse its pytest-report parser)

REPORT_PATH = os.path.join("reports", "report.json")


def compute_trust(opa_results, pytest_results):
    """Return (score_float, breakdown_dict) from the two result sets."""
    opa_passed = [rule for rule, msgs in opa_results.items() if not msgs]
    opa_failed = [rule for rule, msgs in opa_results.items() if msgs]
    py_passed = [t for t in pytest_results if t["outcome"] == "passed"]
    py_failed = [t for t in pytest_results if t["outcome"] != "passed"]

    total = len(opa_results) + len(pytest_results)
    passed = len(opa_passed) + len(py_passed)
    score = round(passed / total * 100, 1) if total else 0.0

    breakdown = {
        "opa_passed": len(opa_passed),
        "opa_failed": len(opa_failed),
        "pytest_passed": len(py_passed),
        "pytest_failed": len(py_failed),
        "total_checks": total,
        "total_passed": passed,
    }
    return score, breakdown


def build_report(opa_results, pytest_results):
    """Assemble the full report dictionary."""
    score, breakdown = compute_trust(opa_results, pytest_results)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trust_score": score,
        "breakdown": breakdown,
        "opa_results": {
            rule: {"passed": not msgs, "messages": msgs}
            for rule, msgs in opa_results.items()
        },
        "pytest_results": [
            {
                "nodeid": t["nodeid"],
                "outcome": t["outcome"],
                "message": (t["message"].splitlines()[0] if t["message"] else ""),
            }
            for t in pytest_results
        ],
    }


def write_report(report, path=REPORT_PATH):
    """Write the report dict to disk as pretty JSON; return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    return path


def generate(opa_results=None, pytest_results=None):
    """Build + write the report. Gathers results itself if not provided."""
    if opa_results is None:
        opa_results = run_check.evaluate_policies()
    if pytest_results is None:
        pytest_results = regenerate.parse_pytest_report()

    report = build_report(opa_results, pytest_results)
    path = write_report(report)
    print(f"Trust score: {report['trust_score']}%  ->  {path}")
    return report


def main():
    os.chdir(PROJECT_ROOT)
    generate()


if __name__ == "__main__":
    main()
