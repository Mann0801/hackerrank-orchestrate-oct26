"""Deletion audit.

For one request: find every record that belongs to the subject, reconstruct each record's
state at audit_at from evidence, compare it with the treatment deletion_rules.csv requires,
and emit the ordered findings / remediation / verification steps.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from identity import Identity, IdentityIndex
from ingest import DATASET, Evidence, Observation, system_of

# ------------------------------------------------------------------ output vocabulary

OP_ORDER = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
                "REPORT_RESTORE_RISK"],
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD",
                    "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED",
                     "VERIFY_RESTORE_BLOCKED"],
}
PHASES = list(OP_ORDER)
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
OP_PHASE = {op: phase for phase, ops in OP_ORDER.items() for op in ops}

# ------------------------------------------------------------------ policy

# Category a system's records belong to when the evidence does not say.
DEFAULT_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "support_platform": "SUPPORT_MESSAGE",
    "analytics_platform": "ANALYTICS_PROFILE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "cache_service": "CACHE_ENTRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "marketing_platform": "MARKETING_PROFILE",
    "object_storage": "USER_EXPORT",
}
# Systems that hold more than one category: the deletion workflow's intended operation
# on the record tells which one it is.
HINT_CATEGORY = {
    ("support_platform", "DELETE_RECORD"): "SUPPORT_ATTACHMENT",
    ("support_platform", "REDACT_RECORD"): "SUPPORT_MESSAGE",
    ("analytics_platform", "DELETE_RECORD"): "SECONDARY_IDENTIFIER",
    ("analytics_platform", "ANONYMIZE_RECORD"): "ANALYTICS_PROFILE",
}
# Workflow result recorded for the primary profile when it reports each status.
RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL",
                     "NOT_STARTED": "FAILED"}
DIRECT_IDENTIFIERS = {"email", "phone", "name", "email_hash", "direct"}

RUN_WINDOW = timedelta(hours=3)  # deletion-worker activity happens within this of reported_at

# Words in a request that point at a system (offline fallback for the LLM's request tags).
SYSTEM_KEYWORDS = {
    "search_service": ["search", "búsqueda", "recherche", "検索", "suche"],
    "support_platform": ["support", "conversation", "ticket", "soporte", "サポート"],
    "analytics_platform": ["analytics", "statistic", "analítica", "analyse", "分析"],
    "integration_service": ["integration", "sync", "connector", "copy them back",
                            "send my data to other systems", "integración", "連携"],
    "backup_service": ["backup", "restore", "copia de seguridad", "sauvegarde", "バックアップ"],
    "export_service": ["export", "download", "archive", "descarga", "エクスポート"],
    "identity_service": ["deleted-account", "internal reference", "tombstone"],
}


def keyword_systems(text: str) -> set[str]:
    low = text.lower()
    return {s for s, words in SYSTEM_KEYWORDS.items() if any(w in low for w in words)}


def load_rules() -> dict[str, str]:
    with open(DATASET / "deletion_rules.csv", newline="") as fh:
        return {r["data_category"]: r["required_action"] for r in csv.DictReader(fh)}


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours(delta: timedelta) -> float:
    return abs(delta.total_seconds()) / 3600


@dataclass
class Case:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


def load_cases(filename: str) -> list[Case]:
    with open(DATASET / filename, newline="") as fh:
        return [Case(**r) for r in csv.DictReader(fh)]


@dataclass
class Snapshot:
    """A record's reconstructed state at audit time and the file that shows it."""
    present: bool | None
    state: str | None
    pii: set[str] | None
    file_id: str
    by_omission: bool = False  # absence inferred from a dump that does not list the record


# ------------------------------------------------------------------ auditor

class Auditor:
    def __init__(self, evidence: Evidence, identities: IdentityIndex, all_cases: list[Case],
                 non_deletion: set[str] = frozenset(),
                 request_systems: dict[str, set[str]] | None = None):
        self.ev = evidence
        self.ids = identities
        # Systems each request text points at (LLM tags, else keyword fallback).
        self.request_systems = request_systems or {c.case_id: keyword_systems(c.request_text)
                                                   for c in all_cases}
        self.cases_by_user: dict[str, list[Case]] = defaultdict(list)
        for c in all_cases:
            u = identities.resolve(c.email)
            if u and c.case_id not in non_deletion:
                self.cases_by_user[u.user_id].append(c)
        self.rules = load_rules()
        self.obs_by_res = evidence.by_resource()
        with open(DATASET / "system_activity.csv", newline="") as fh:
            self.activity = list(csv.DictReader(fh))
        self.worker_events = [a for a in self.activity if a["source_system"] == "deletion_worker"]
        self.worker_ops: dict[str, set[str]] = defaultdict(set)
        for a in self.worker_events:
            self.worker_ops[a["resource_id"]].add(a["event_type"])
        self.owner = self._owners(strong_only=True)
        self.linked = self._owners(strong_only=False)
        self.assigned = self._assign_unowned(all_cases, non_deletion)

    # ---------------------------------------------------------- ownership

    def _owners(self, strong_only: bool) -> dict[str, set[str]]:
        """resource_id -> users that evidence ties it to (any capture time).

        strong_only drops links that rest only on a lookup which did not find the record."""
        owner: dict[str, set[str]] = defaultdict(set)
        for o in self.ev.observations:
            if not (strong_only and o.weak):
                owner[o.resource_id] |= self.ids.owners(o.idents)
        for a in self.activity:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                owner[a["resource_id"]] |= set(re.findall(r"\buser_\d+\b", a["details"]))
        return {r: u for r, u in owner.items() if u}

    def _window(self, case: Case) -> list[dict]:
        rep = ts(case.reported_at)
        return [a for a in self.worker_events if abs(ts(a["event_time"]) - rep) <= RUN_WINDOW]

    def _assign_unowned(self, cases: list[Case], non_deletion: set[str]) -> dict[str, set[str]]:
        """Share deletion-run records that no evidence ties to anyone among the cases whose
        run window contains them: at most one per system per case, best-matching first."""
        candidates = {}
        for case in cases:
            if case.case_id in non_deletion:
                continue
            user = self.ids.resolve(case.email)
            if not user:
                continue
            window = self._window(case)
            own = self._linked_resources(case, user, window)
            owned_systems = {system_of(r) for r in own}
            # Times of this case's own deletion run (worker events on records it owns). The
            # profile is always handled ~1h before the report for every case, so it says
            # nothing about which run a record belongs to.
            run_times = [ts(a["event_time"]) for a in window if a["resource_id"] in own
                         and system_of(a["resource_id"]) != "account_service"]
            rep = ts(case.reported_at)
            mentioned = self.request_systems.get(case.case_id, set())
            for a in window:
                rid, system = a["resource_id"], system_of(a["resource_id"])
                if rid in self.linked or system in owned_systems:
                    continue
                t = ts(a["event_time"])
                match = False
                if system == "account_service":
                    # The profile is processed ~1h before the report, and the report is
                    # derived from that operation's result.
                    hours_off = hours(t - (rep - timedelta(hours=1)))
                    match = RESULT_FOR_STATUS.get(case.reported_status) == a["reported_result"]
                elif run_times:  # a run's records are processed back to back
                    hours_off = min(hours(t - r) for r in run_times)
                    if hours_off < 2 / 60:  # that slot of this run is already taken
                        continue
                else:
                    succeeded = a["reported_result"] == "SUCCESS" and a["event_type"] != "CHECK_DATA_FLOW"
                    target = rep - timedelta(minutes=30) if succeeded else rep + timedelta(hours=1)
                    hours_off = hours(t - target) + 1  # weaker evidence than a known run
                score = 10 * match + 5 * (system in mentioned) - hours_off
                key = (case.case_id, rid)
                candidates[key] = max(candidates.get(key, -1e9), score)
        assigned: dict[str, set[str]] = defaultdict(set)
        taken_res, taken_slot = set(), set()
        for (case_id, rid), _ in sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0])):
            slot = (case_id, system_of(rid))
            if rid in taken_res or slot in taken_slot:
                continue
            assigned[case_id].add(rid)
            taken_res.add(rid)
            taken_slot.add(slot)
        return assigned

    def _anchor_times(self, rid: str, user: Identity) -> list[datetime]:
        """When the record was seen tied to this user, or touched by the deletion worker."""
        times = [ts(o.observed_at) for o in self.obs_by_res.get(rid, [])
                 if user.user_id in self.ids.owners(o.idents)]
        times += [ts(a["event_time"]) for a in self.activity if a["resource_id"] == rid
                  and a["event_type"] in {"LINK_RESOURCE_IDENTITY"} | set(OP_STATE)]
        return times

    def _belongs_to_case(self, rid: str, case: Case, user: Identity) -> bool:
        """For users with several requests, give each record to the request nearest in time."""
        cases = self.cases_by_user.get(user.user_id, [case])
        if len(cases) <= 1:
            return True
        anchors = self._anchor_times(rid, user)
        if not anchors:
            return True

        def distance(c: Case) -> float:
            rep = ts(c.reported_at)
            return min(abs((t - rep).total_seconds()) for t in anchors)
        return min(cases, key=lambda c: (distance(c), c.case_id)).case_id == case.case_id

    def _linked_resources(self, case: Case, user: Identity, window: list[dict]) -> set[str]:
        """Records evidence ties to this case: strong links, plus weak (lookup-only) links
        that the case's own deletion run also touched."""
        keys = user.strong_keys
        strong = {r for r, us in self.owner.items()
                  if user.user_id in us and self._belongs_to_case(r, case, user)}
        touched = {a["resource_id"] for a in window}
        weak = {o.resource_id for o in self.ev.observations if o.weak and o.idents & keys}
        return strong | (weak & touched)

    def resources_for(self, case: Case, user: Identity) -> set[str]:
        return (self._linked_resources(case, user, self._window(case))
                | self.assigned.get(case.case_id, set()))

    # ---------------------------------------------------------- state

    def snapshot(self, rid: str, audit_at: str) -> Snapshot | None:
        """State of `rid` per the newest evidence captured at or before audit_at."""
        by_file: dict[str, list[Observation]] = defaultdict(list)
        for o in self.obs_by_res.get(rid, []):
            if o.captured_at <= audit_at and (o.present is not None or o.state or o.pii is not None):
                by_file[o.file_id].append(o)
        newest_obs = max(by_file, key=lambda f: (self.ev.files[f].captured_at, f), default=None)
        if not newest_obs:
            # Never sighted: the newest SQL dump holding that system's rows shows it is gone.
            # (evaluate() may swap in the case's main evidence file.)
            system = system_of(rid)
            dumps = [f for f in self.ev.files.values()
                     if f.is_snapshot and system in f.systems and f.captured_at <= audit_at]
            newest_dump = max(dumps, key=lambda f: (f.captured_at, f.file_id), default=None)
            return Snapshot(False, None, None, newest_dump.file_id, by_omission=True) \
                if newest_dump else None
        obs = by_file[newest_obs]
        present = True if any(o.present for o in obs) else (
            False if any(o.present is False for o in obs) else None)
        states = [o.state for o in obs if o.state]
        piis = [o.pii for o in obs if o.pii is not None]
        return Snapshot(present, states[-1] if states else None,
                        set().union(*piis) if piis else None, newest_obs)

    def category(self, rid: str) -> str:
        for o in self.obs_by_res.get(rid, []):
            if o.category and o.category in self.rules:
                return o.category
        system = system_of(rid)
        if system == "support_platform" and any(o.source == "document"
                                                for o in self.obs_by_res.get(rid, [])):
            return "SUPPORT_ATTACHMENT"  # a file the customer sent, not a shared thread
        for op in sorted(self.worker_ops.get(rid, ())):
            if (system, op) in HINT_CATEGORY:
                return HINT_CATEGORY[(system, op)]
        return DEFAULT_CATEGORY.get(system, "USER_PROFILE")

    # ---------------------------------------------------------- decision

    def judge(self, rid: str, snap: Snapshot | None) -> tuple[list[str], list[str], str]:
        """(finding ops, remediation ops, verification op) for one record."""
        category = self.category(rid)
        action = self.rules.get(category, "DELETE")
        present = snap.present if snap else None
        state = snap.state if snap else None
        pii = snap.pii if snap else None

        if category == "DATA_FLOW" or system_of(rid) == "integration_service":
            if state == "ACTIVE":
                return ["REPORT_ACTIVE_DATA_FLOW"], ["DISABLE_DATA_FLOW"], "VERIFY_DATA_FLOW_DISABLED"
            return [], [], "VERIFY_DATA_FLOW_DISABLED"
        if category == "BACKUP_ARCHIVE":
            if state == "ACTIVE":
                return ["REPORT_RESTORE_RISK"], ["BLOCK_RESTORE"], "VERIFY_RESTORE_BLOCKED"
            return [], [], "VERIFY_RESTORE_BLOCKED"
        if action == "RETAIN":
            return [], [], "VERIFY_RECORD_PRESENT"
        if action == "DELETE":
            if present:
                return ["REPORT_REMAINING_RECORD"], ["DELETE_RECORD"], "VERIFY_RECORD_ABSENT"
            return [], [], "VERIFY_RECORD_ABSENT"
        if action == "REDACT":
            exposed = state == "IDENTIFIABLE" or bool(pii and pii & DIRECT_IDENTIFIERS)
            if present is not False and state != "REDACTED" and exposed:
                return ["REPORT_EXPOSED_PII"], ["REDACT_RECORD"], "VERIFY_RECORD_REDACTED"
            return [], [], "VERIFY_RECORD_REDACTED"
        if action == "ANONYMIZE":
            exposed = state == "IDENTIFIABLE" or bool(pii)
            if present is not False and state != "ANONYMIZED" and exposed:
                return ["REPORT_EXPOSED_PII"], ["ANONYMIZE_RECORD"], "VERIFY_RECORD_ANONYMIZED"
            return [], [], "VERIFY_RECORD_ANONYMIZED"
        return [], [], "VERIFY_RECORD_ABSENT"

    def evaluate(self, case: Case) -> tuple[str, list[dict]]:
        user = self.ids.resolve(case.email)
        if not user:
            return "COMPLETE", []
        raw = []  # (op, system, rid, evidence)
        snaps = {rid: self.snapshot(rid, case.audit_at) for rid in self.resources_for(case, user)}
        # The file that directly shows most of this case's records; a record it does not
        # list is best evidenced as absent by that same point-in-time view.
        direct = Counter(s.file_id for s in snaps.values() if s and not s.by_omission)
        main_file = min(direct, key=lambda f: (-direct[f], f)) if direct else None
        for rid in sorted(snaps):
            snap = snaps[rid]
            if snap and snap.by_omission and main_file:
                snap.file_id = main_file
            evidence = snap.file_id if snap else ""
            findings, remediation, verification = self.judge(rid, snap)
            raw += [(op, rid, evidence) for op in findings]
            raw += [(op, rid, "") for op in remediation]
            raw.append((verification, rid, evidence))
        status = "INCOMPLETE" if any(OP_PHASE[op] == "FINDING" for op, _, _ in raw) else "COMPLETE"
        if status == "INCOMPLETE":  # verification describes future checks
            raw = [(op, rid, "" if OP_PHASE[op] == "VERIFICATION" else ev) for op, rid, ev in raw]
        raw.sort(key=lambda s: (PHASES.index(OP_PHASE[s[0]]), OP_ORDER[OP_PHASE[s[0]]].index(s[0]),
                                system_of(s[1]), s[1]))
        steps = [{"step_no": i, "phase": OP_PHASE[op], "system_id": system_of(rid),
                  "resource_id": rid, "operation": op, "state": OP_STATE[op], "evidence_id": ev}
                 for i, (op, rid, ev) in enumerate(raw, 1)]
        return status, steps
