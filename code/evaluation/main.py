"""Score predictions against the public sample answers.

Usage:  python code/evaluation/main.py [--verbose]

Runs the full pipeline on dataset/sample_deletion_requests.csv and compares it with
dataset/sample_output.csv:
  * status accuracy      - deletion_status exact match per case
  * step P / R / F1      - a step is a hit only if every field matches (step_no excluded)
  * field accuracy       - per field, over steps aligned by position
  * sequence accuracy    - whole steps_json array exactly equal
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import DATASET  # noqa: E402
from pipeline import run_cases  # noqa: E402

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def key(step: dict) -> tuple:
    return tuple(step[f] for f in FIELDS)


def score(pred: dict[str, tuple[str, list]], gold: dict[str, tuple[str, list]], verbose=False):
    status_ok = seq_ok = 0
    tp = fp = fn = 0
    field_hits, field_total = Counter(), 0
    for cid, (g_status, g_steps) in sorted(gold.items()):
        p_status, p_steps = pred.get(cid, ("", []))
        status_ok += p_status == g_status
        seq_ok += p_status == g_status and [key(s) for s in p_steps] == [key(s) for s in g_steps]
        pk, gk = Counter(map(key, p_steps)), Counter(map(key, g_steps))
        hit = sum((pk & gk).values())
        tp += hit
        fp += sum(pk.values()) - hit
        fn += sum(gk.values()) - hit
        for ps, gs in zip(p_steps, g_steps):
            for f in FIELDS:
                field_hits[f] += ps[f] == gs[f]
        field_total += max(len(p_steps), len(g_steps))
        if verbose and (p_status != g_status or pk != gk or
                        [key(s) for s in p_steps] != [key(s) for s in g_steps]):
            print(f"--- {cid}: predicted {p_status}, expected {g_status}")
            for s in sorted(gk - pk):
                print("    missing ", s)
            for s in sorted(pk - gk):
                print("    extra   ", s)
            if pk == gk:
                print("    (same steps, different order)")
    n = len(gold)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "sequence_accuracy": seq_ok / n,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "field_accuracy": {f: field_hits[f] / field_total for f in FIELDS} if field_total else {},
        "steps_expected": tp + fn,
        "steps_predicted": tp + fp,
    }


def load_gold() -> dict[str, tuple[str, list]]:
    with open(DATASET / "sample_output.csv", newline="") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
                for r in csv.DictReader(fh)}


def main():
    verbose = "--verbose" in sys.argv
    pred = run_cases("sample_deletion_requests.csv")
    metrics = score(pred, load_gold(), verbose=verbose)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
