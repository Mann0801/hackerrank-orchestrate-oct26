"""Entry point: audit every request in dataset/deletion_requests.csv and write output.csv.

Usage:  python code/main.py [--offline]

  --offline   never call an LLM; use cached answers, else keyword rules / no screenshots.

The output is validated against the submission contract before the command succeeds, and a
run summary (status counts, LLM calls and tokens) is written to code/evaluation/last_run.json.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import OP_PHASE, OP_STATE, PHASES, load_cases, ts  # noqa: E402
from ingest import DATASET, REPO_ROOT  # noqa: E402
from pipeline import build, run_cases  # noqa: E402

REQUEST_FILE = "deletion_requests.csv"
OUTPUT_PATH = REPO_ROOT / "output.csv"
SUMMARY_PATH = Path(__file__).resolve().parent / "evaluation" / "last_run.json"
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
STEP_FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def case_sort_key(case_id: str):
    m = re.search(r"(\d+)$", case_id)
    return (int(m.group(1)) if m else 0, case_id)


def evidence_times() -> dict[str, str]:
    """Every citable id (file, activity, identity audit) -> its timestamp."""
    out = {}
    with open(DATASET / "evidence_files.csv", newline="") as fh:
        out.update({r["file_id"]: r["captured_at"] for r in csv.DictReader(fh)})
    with open(DATASET / "system_activity.csv", newline="") as fh:
        out.update({r["activity_id"]: r["event_time"] for r in csv.DictReader(fh)})
    with open(DATASET / "user_audit_log.csv", newline="") as fh:
        out.update({r["audit_id"]: r["event_time"] for r in csv.DictReader(fh)})
    return out


def validate(results: dict[str, tuple[str, list[dict]]]) -> list[str]:
    """Contract violations (empty list = valid)."""
    errors = []
    cases = {c.case_id: c for c in load_cases(REQUEST_FILE)}
    if set(results) != set(cases):
        errors.append(f"case ids differ from {REQUEST_FILE}: "
                      f"{len(set(cases) - set(results))} missing, "
                      f"{len(set(results) - set(cases))} extra")
    times = evidence_times()
    for cid, (status, steps) in results.items():
        if status not in STATUSES:
            errors.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{cid}: NOT_APPLICABLE must have no steps")
        if status != "NOT_APPLICABLE" and not steps:
            errors.append(f"{cid}: {status} has no steps")
        last_phase = 0
        for i, s in enumerate(steps, 1):
            if set(s) != STEP_FIELDS:
                errors.append(f"{cid} step {i}: fields {sorted(s)}")
                continue
            if s["step_no"] != i:
                errors.append(f"{cid} step {i}: step_no {s['step_no']}")
            if OP_PHASE.get(s["operation"]) != s["phase"] or OP_STATE[s["operation"]] != s["state"]:
                errors.append(f"{cid} step {i}: {s['phase']}/{s['operation']}/{s['state']}")
                continue
            phase = PHASES.index(s["phase"])
            if phase < last_phase:
                errors.append(f"{cid} step {i}: phase out of order")
            last_phase = phase
            ev = s["evidence_id"]
            needs_evidence = s["phase"] == "FINDING" or status == "COMPLETE"
            if needs_evidence and not ev:
                errors.append(f"{cid} step {i}: missing evidence_id")
            if not needs_evidence and ev:
                errors.append(f"{cid} step {i}: future step cites {ev}")
            if ev and ev not in times:
                errors.append(f"{cid} step {i}: unknown evidence {ev}")
            elif ev and times[ev] and ts(times[ev]) > ts(cases[cid].audit_at):
                errors.append(f"{cid} step {i}: {ev} is after audit_at")
    return errors


def write_output(results: dict[str, tuple[str, list[dict]]]) -> None:
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for cid in sorted(results, key=case_sort_key):
            status, steps = results[cid]
            w.writerow([cid, status, json.dumps(steps, separators=(",", ":"), sort_keys=True)])


def main() -> int:
    offline = "--offline" in sys.argv
    results = run_cases(REQUEST_FILE, offline=offline)
    errors = validate(results)
    if errors:
        print(f"output failed validation ({len(errors)} problems):", file=sys.stderr)
        for e in errors[:30]:
            print("  " + e, file=sys.stderr)
        return 1
    write_output(results)
    _, _, reader = build(offline)
    summary = {
        "cases": len(results),
        "statuses": dict(sorted(Counter(s for s, _ in results.values()).items())),
        "steps": sum(len(st) for _, st in results.values()),
        "llm": reader.usage(),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=1) + "\n")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}: {summary['cases']} rows, "
          f"{summary['steps']} steps, {summary['statuses']}")
    run = summary["llm"]["this_run"]
    print(f"llm: {run['provider']}:{run['model']} - {run['api_calls']} API calls, "
          f"{run['cache_hits']} cached answers, {run['fallbacks']} fallbacks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
