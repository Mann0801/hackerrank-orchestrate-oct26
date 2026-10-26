"""Participant-made evaluation cases (beyond the 32 public samples).

Usage:  python code/evaluation/custom_cases.py

Each case perturbs real dataset inputs (request fields, audit time, or one injected piece of
evidence) and checks the pipeline's answer against the outcome the challenge rules require.
Injected evidence lives only in memory; the dataset is never modified. Case 6 sends new
request texts to the LLM (one batched call, then cached).
"""

from __future__ import annotations

import csv
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit import Auditor, load_cases, ts  # noqa: E402
from identity import IdentityIndex  # noqa: E402
from ingest import DATASET, EvidenceFile, Observation, ingest, norm_ident  # noqa: E402
from pipeline import REQUEST_FILES, build, read_requests  # noqa: E402

SAMPLES = {c.case_id: c for c in load_cases("sample_deletion_requests.csv")}
EVAL = {c.case_id: c for c in load_cases("deletion_requests.csv")}


def gold(case_id: str) -> tuple[str, list[dict]]:
    import json
    with open(DATASET / "sample_output.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["case_id"] == case_id:
                return r["deletion_status"], json.loads(r["steps_json"])
    raise KeyError(case_id)


def strip(steps: list[dict]) -> list[tuple]:
    return [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps]


def auditor_with(extra: list[Observation]) -> Auditor:
    """A fresh auditor whose evidence also contains `extra` observations (in memory only)."""
    _, non_deletion, reader = build()
    ev = ingest(reader.parse_image)
    for o in extra:
        ev.files.setdefault(o.file_id, EvidenceFile(o.file_id, Path(f"custom/{o.file_id}.json"),
                                                    "custom_test", o.captured_at, {o.system}))
        ev.observations.append(o)
    cases = [c for f in REQUEST_FILES for c in load_cases(f)]
    _, systems = read_requests(cases, reader)
    return Auditor(ev, IdentityIndex(), cases, non_deletion, request_systems=systems)


def obs(file_id, captured_at, system, rid, present=True, state=None, email=None) -> Observation:
    content = norm_ident(email) if email else set()
    return Observation(file_id=file_id, captured_at=captured_at, observed_at=captured_at,
                       system=system, resource_id=rid, present=present, state=state,
                       idents=set(content), pii={"email"} if email else set(),
                       source="custom", content_idents=set(content))


# --------------------------------------------------------------------- cases
def case_historical_email():
    """Request sent from an email the user changed away from must give the same answer."""
    auditor, _, _ = build()
    case = dataclasses.replace(SAMPLES["case_10"], email="nadia.petrescu@yahoo.com")
    got = auditor.evaluate(case)
    exp = gold("case_10")
    return got[0] == exp[0] and strip(got[1]) == strip(exp[1]), \
        f"status {got[0]} (expected {exp[0]}), {len(got[1])} steps identical to sample answer: " \
        f"{strip(got[1]) == strip(exp[1])}"


def case_time_cutoff():
    """Moving audit_at before the decisive evidence capture must stop that evidence being
    used: nothing captured after audit_at may be cited."""
    auditor, _, _ = build()
    base = SAMPLES["case_06"]
    early = dataclasses.replace(base, audit_at="2026-09-10T08:30:00Z")  # evidence_57 is 08:45
    with open(DATASET / "evidence_files.csv", newline="") as fh:
        captured = {r["file_id"]: r["captured_at"] for r in csv.DictReader(fh)}
    _, steps = auditor.evaluate(early)
    cited = {s["evidence_id"] for s in steps if s["evidence_id"]}
    late = [e for e in cited if e in captured and ts(captured[e]) > ts(early.audit_at)]
    return not late and "evidence_57" not in cited, \
        f"cited {sorted(cited) or 'nothing'}; cited after audit_at: {late or 'none'}"


def case_tombstone_leak():
    """A tombstone that still shows the user's email violates the REDACT rule: the case
    must become INCOMPLETE with report -> redact -> verify redacted."""
    case = EVAL["case_46"]
    leak = obs("custom_tomb", "2026-03-10T09:00:00Z", "identity_service", "tombstone_17",
               email=case.email)
    status, steps = auditor_with([leak]).evaluate(case)
    ops = [s["operation"] for s in steps if s["resource_id"] == "tombstone_17"]
    want = ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"]
    return status == "INCOMPLETE" and ops == want, f"status {status}, tombstone ops {ops}"


def case_active_sync():
    """An enabled sync still carrying the user's email makes a COMPLETE case INCOMPLETE,
    and the flow must be disabled before any record is changed."""
    case = SAMPLES["case_01"]
    flow = obs("custom_flow", "2026-09-03T09:00:00Z", "integration_service", "data_flow_99001",
               state="ACTIVE", email=case.email)
    status, steps = auditor_with([flow]).evaluate(case)
    rem = [s["operation"] for s in steps if s["phase"] == "REMEDIATION"]
    ok = (status == "INCOMPLETE" and rem and rem[0] == "DISABLE_DATA_FLOW"
          and any(s["operation"] == "REPORT_ACTIVE_DATA_FLOW" and s["evidence_id"] == "custom_flow"
                  for s in steps))
    return ok, f"status {status}, remediation order {rem}"


def case_later_evidence_ignored():
    """A copy that reappears only after audit_at must not change the answer."""
    case = SAMPLES["case_01"]
    late = obs("custom_late", "2026-09-20T00:00:00Z", "account_service", "account_001",
               email=case.email)
    got = auditor_with([late]).evaluate(case)
    exp = gold("case_01")
    return got[0] == exp[0] and strip(got[1]) == strip(exp[1]), \
        f"status {got[0]} (expected {exp[0]}), steps unchanged: {strip(got[1]) == strip(exp[1])}"


INTENT_TEXTS = [
    ("¿Podrían enviarme una copia de todos los datos que tienen sobre mí? No borren nada.", False),
    ("Merci de supprimer définitivement mon compte et toutes mes données personnelles.", True),
    ("宣伝メールの配信を停止してください。アカウントはこのまま使い続けます。", False),
    ("Ich habe die Löschung meines Kontos beantragt. Bitte prüfen Sie, ob wirklich alle "
     "Kopien entfernt wurden.", True),
    ("कृपया मेरा खाता और उससे जुड़ा सारा निजी डेटा हटा दें।", True),
    ("Please update the shipping address on my account; I'm moving next month.", False),
]


def case_multilingual_intent():
    """Deletion intent in Spanish, French, Japanese, German, Hindi and English, including a
    verify-my-deletion follow-up (deletion) and a copy request that mentions 'borren'."""
    _, _, reader = build()
    answers = reader.classify_requests([t for t, _ in INTENT_TEXTS])
    wrong = [t[:40] for t, want in INTENT_TEXTS
             if t not in answers or answers[t]["authorizes_deletion"] != want]
    return not wrong, f"{len(INTENT_TEXTS) - len(wrong)}/{len(INTENT_TEXTS)} correct" + \
        (f"; wrong: {wrong}" if wrong else "")


CASES = [
    ("C1", "Historical email resolves to the same subject", case_historical_email),
    ("C2", "Evidence captured after audit_at is never used", case_time_cutoff),
    ("C3", "Tombstone still exposing an email -> redact", case_tombstone_leak),
    ("C4", "Active sync -> disable it before other remediation", case_active_sync),
    ("C5", "A copy appearing after audit_at does not change the answer", case_later_evidence_ignored),
    ("C6", "Multilingual deletion intent (incl. follow-ups, opt-outs)", case_multilingual_intent),
]


def main() -> int:
    failed = 0
    print("| id | scenario | result | detail |\n|---|---|---|---|")
    for cid, name, fn in CASES:
        ok, detail = fn()
        failed += not ok
        print(f"| {cid} | {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
