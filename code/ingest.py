"""Evidence ingestion.

Turns every evidence file listed in dataset/evidence_files.csv into a flat list of
uniform `Observation`s: "resource R in system S was seen in file F at time T,
present/absent, in state X, carrying identifiers I and still exposing PII fields P".

Images are not parsed here; llm.py extracts them and returns Observations in the
same shape.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "dataset"

# Resource-id prefix -> owning system. Prefixes are unambiguous across the dataset.
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
}
RESOURCE_RE = re.compile(r"\b(" + "|".join(sorted(PREFIX_SYSTEM, key=len, reverse=True)) + r")_\d+\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"^user_\d+$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

STATES = {"ACTIVE", "DISABLED", "BLOCKED", "ANONYMIZED", "REDACTED", "IDENTIFIABLE"}
ABSENT_RESULTS = {"NOT_FOUND", "MISS", "ABSENT", "DELETED"}
PRESENT_RESULTS = {"FOUND", "HIT", "PRESENT"} | STATES

# Fields whose value is the record's own content (counts as stored PII).
CONTENT_FIELDS = {
    "email": "email", "owner_email": "email", "customer_email": "email", "requester_email": "email",
    "phone": "phone", "owner_phone": "phone", "requester_phone": "phone", "customer_phone": "phone",
    "full_name": "name", "display_name": "name", "requester_name": "name", "name": "name",
    "email_hash": "email_hash",
    "user_id": "user_ref", "customer_reference": "user_ref",
}
# Fields that only describe how the record was looked up / selected: link, not PII.
LINK_FIELDS = {
    "lookup_key", "match_key", "scan_key", "query", "correlation_key", "subject_selector",
    "subject_reference", "subject_ref",
}
# Free-text fields that may embed an email address.
TEXT_FIELDS = {"message_text", "message_excerpt", "note", "notes", "detail", "details"}


def system_of(resource_id: str) -> str | None:
    m = RESOURCE_RE.fullmatch(resource_id or "")
    return PREFIX_SYSTEM[m.group(1)] if m else None


def norm_ident(value: str) -> set[str]:
    """Normalised identifier keys for a raw value (lower-case; phones also as digits)."""
    v = str(value).strip().lower()
    if not v or v in {"null", "none", "no direct identifier"}:
        return set()
    out = {v}
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 7 and not EMAIL_RE.search(v) and not HASH_RE.match(v) and not USER_RE.match(v):
        out.add("tel:" + digits)
    return out


def truthy(v: str) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def is_null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in {"", "null", "none", "[redacted]"})


@dataclass
class Observation:
    file_id: str
    captured_at: str
    observed_at: str
    system: str
    resource_id: str
    present: bool | None = None
    state: str | None = None
    category: str | None = None
    idents: set[str] = field(default_factory=set)
    pii: set[str] | None = None  # None = file does not show the record's content
    source: str = ""
    content_idents: set[str] = field(default_factory=set)  # idents stored in the record itself

    @property
    def weak(self) -> bool:
        """Only tied to its identifiers by a lookup that did not find it."""
        return self.present is not True and not self.content_idents


@dataclass
class EvidenceFile:
    file_id: str
    path: Path
    origin: str
    captured_at: str
    systems: set[str] = field(default_factory=set)  # systems this file has a view of

    @property
    def is_snapshot(self) -> bool:
        """SQL dumps copy whole tables, so a record they omit is absent. (JSON exports and
        logs are targeted lookups: omission there proves nothing.)"""
        return self.path.suffix.lower() == ".sql"


def observe(rec: dict, file: EvidenceFile, observed_at: str | None = None, source: str = "",
            extra_idents: set[str] | None = None, listed_means_present: bool = True
            ) -> Observation | None:
    """Normalise one flat-ish record dict into an Observation.

    `listed_means_present`: a row in a snapshot/export implies the record exists; a log
    event that merely mentions a resource does not.
    """
    rid = rec.get("resource_id")
    system = system_of(rid) if isinstance(rid, str) else None
    if not system:
        return None
    idents: set[str] = set(extra_idents or ())
    content_idents: set[str] = set()
    pii: set[str] = set()
    returned_content = False
    present: bool | None = None
    state = category = None
    ledger_style = "record_type" in rec and "result" in rec

    for key, val in rec.items():
        k = key.lower()
        if k in CONTENT_FIELDS or k in TEXT_FIELDS or k in {"enabled", "restore_allowed"}:
            returned_content = True  # the lookup returned the record's fields (even if null)
        if is_null(val):
            continue
        if isinstance(val, (dict, list)):
            continue
        sval = str(val)
        if k in CONTENT_FIELDS:
            idents |= norm_ident(sval)
            if not ledger_style:
                content_idents |= norm_ident(sval)
                pii.add(CONTENT_FIELDS[k])
        elif k in LINK_FIELDS:
            idents |= norm_ident(sval)
        elif k in TEXT_FIELDS:
            for e in EMAIL_RE.findall(sval):
                idents |= norm_ident(e)
                content_idents |= norm_ident(e)
                pii.add("email")
        elif k in {"status_code", "http_status"}:
            present = sval != "404" if sval.isdigit() else present
        elif k in {"rows", "total_hits", "total"}:
            if sval.isdigit():
                present = int(sval) > 0
        elif k in {"result", "status"}:
            u = sval.upper()
            if u in ABSENT_RESULTS:
                present = False
            elif u in PRESENT_RESULTS:
                present = True
            if u in STATES:
                state = u
        elif k in {"archive_present", "available"}:
            present = truthy(sval)
        elif k == "marker_found" and not truthy(sval):
            present = True  # no deletion marker: the record was never removed
        elif k == "enabled":
            state = "ACTIVE" if truthy(sval) else "DISABLED"
        elif k == "scheduler_registered":
            state = state or ("ACTIVE" if truthy(sval) else "DISABLED")
        elif k == "last_delivery_status":
            state = state or {"delivered": "ACTIVE", "stopped": "DISABLED"}.get(sval.lower())
        elif k in {"restore_allowed", "eligible"}:
            state = "ACTIVE" if truthy(sval) else "BLOCKED"
        elif k == "deletion_block":
            state = "BLOCKED" if truthy(sval) else "ACTIVE"
        elif k == "access":
            state = "ACTIVE" if sval.lower() == "allowed" else "BLOCKED"
        elif k == "decision":
            state = "BLOCKED" if sval.lower() == "deny" else state
        elif k == "matched_value":  # a PII scanner found this identifier inside the record
            idents |= norm_ident(sval)
            content_idents |= norm_ident(sval)
            pii.add("email" if EMAIL_RE.fullmatch(sval) else "direct")
        elif k == "direct_identifiers":
            if sval.isdigit() and int(sval) > 0:
                pii.add("direct")
            returned_content = True
        elif k == "file" and system == "support_platform":
            category = "SUPPORT_ATTACHMENT"
        elif k == "record_type":
            category = sval.upper()

    # A profile lookup that returned no profile object.
    if "profile" in rec and rec["profile"] is None and present is None:
        present = False
    if present is None and not ledger_style and (listed_means_present or returned_content):
        present = True
    return Observation(
        file_id=file.file_id, captured_at=file.captured_at,
        observed_at=observed_at or file.captured_at, system=system, resource_id=rid,
        present=present, state=state, category=category, idents=idents,
        pii=None if ledger_style else pii, source=source, content_idents=content_idents,
    )


def flatten(rec: dict) -> dict:
    """Merge nested dicts (without their own resource_id) into one level; last key wins."""
    out = {}
    for k, v in rec.items():
        if isinstance(v, dict) and "resource_id" not in v:
            for k2, v2 in flatten(v).items():
                out.setdefault(k2, v2)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- JSON

def iter_resource_dicts(node, ctx: str = ""):
    """Yield (dict, context) for every dict carrying a resource_id, at any depth."""
    if isinstance(node, dict):
        if "resource_id" in node:
            yield node, ctx
        for k, v in node.items():
            yield from iter_resource_dicts(v, f"{ctx}.{k}" if ctx else k)
    elif isinstance(node, list):
        for v in node:
            yield from iter_resource_dicts(v, ctx)


def parse_json_obj(data, file: EvidenceFile, source: str = "") -> list[Observation]:
    obs = []
    for rec, ctx in iter_resource_dicts(data, source):
        flat = flatten(rec)
        ts = flat.get("observed_at") or flat.get("recorded_at")
        o = observe(flat, file, observed_at=ts if isinstance(ts, str) else None, source=ctx)
        if o:
            obs.append(o)
    return obs


def parse_json(file: EvidenceFile) -> list[Observation]:
    return parse_json_obj(json.loads(file.path.read_text()), file)


# --------------------------------------------------------------------------- SQL

def parse_sql(file: EvidenceFile) -> list[Observation]:
    con = sqlite3.connect(":memory:")
    con.executescript(file.path.read_text())
    con.row_factory = sqlite3.Row
    obs = []
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table == "snapshot_scope":  # declared scope; coverage comes from actual rows
            continue
        for row in con.execute(f'SELECT * FROM "{table}"'):
            rec = dict(row)
            if "record_json" in rec:
                inner = json.loads(rec.pop("record_json"))
                rec = {**rec, **flatten(inner)}
            if "enabled" in rec:
                rec["enabled"] = bool(rec["enabled"])
            if "restore_allowed" in rec:
                rec["restore_allowed"] = bool(rec["restore_allowed"])
            o = observe(rec, file, source=table)
            if o:
                obs.append(o)
    con.close()
    return obs


# --------------------------------------------------------------------------- logs

KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|[^\s|]+)')
TRACE_RE = re.compile(r"\b((?:tr|rb)-[\w-]+)\b")
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")


def unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"')
    return v


def parse_log_line(line: str) -> tuple[str | None, dict]:
    """Return (timestamp, key/value dict) for any of the supported log styles."""
    m = TS_RE.match(line)
    ts = m.group(1) if m else None
    kv: dict = {}
    brace = line.find("{")
    if brace != -1 and line.rstrip().endswith("}"):
        try:
            kv.update(json.loads(line[brace:]))
        except json.JSONDecodeError:
            pass
    for k, v in KV_RE.findall(line):
        v = unquote(v)
        if k == "message":  # cloud-runtime style: message="k=\"v\" ..."
            for k2, v2 in KV_RE.findall(v):
                kv.setdefault(k2, unquote(v2))
        else:
            kv.setdefault(k, v)
    if "trace_id" not in kv:
        t = TRACE_RE.search(line)
        if t:
            kv["trace_id"] = t.group(1)
    return ts, kv


def parse_log(file: EvidenceFile) -> list[Observation]:
    lines = [parse_log_line(l) for l in file.path.read_text().splitlines() if l.strip()]
    # Lookup keys are logged on *_started lines; join them to the result line by trace.
    trace_keys: dict[str, set[str]] = defaultdict(set)
    for _, kv in lines:
        tr = kv.get("trace_id")
        if tr:
            for k in LINK_FIELDS:
                if k in kv and not is_null(kv[k]):
                    trace_keys[tr] |= norm_ident(kv[k])
    obs = []
    for ts, kv in lines:
        if "resource_id" not in kv:
            continue
        extra = trace_keys.get(kv.get("trace_id"), set())
        o = observe(kv, file, observed_at=ts, source=kv.get("event", ""), extra_idents=extra,
                    listed_means_present=False)
        if o:
            obs.append(o)
    return obs


# --------------------------------------------------------------------------- text / PDF

def parse_pipe_text(text: str, file: EvidenceFile) -> list[Observation]:
    """Rows like `ts | system | resource_id | CATEGORY | RESULT | identifier`."""
    obs = []
    for line in text.splitlines():
        parts = [p.strip(" -\t") for p in line.split("|")]
        rid_idx = next((i for i, p in enumerate(parts) if RESOURCE_RE.fullmatch(p)), None)
        if rid_idx is None or len(parts) < rid_idx + 3:
            continue
        ts = parts[0] if TS_RE.match(parts[0]) else None
        rec = {"resource_id": parts[rid_idx], "record_type": parts[rid_idx + 1],
               "result": parts[rid_idx + 2]}
        if len(parts) > rid_idx + 3:
            rec["subject_reference"] = parts[rid_idx + 3]
        o = observe(rec, file, observed_at=ts, source="pipe_row")
        if o:
            obs.append(o)
    return obs


def pdf_text(path: Path) -> str:
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                          text=True, check=True).stdout


PHONE_RE = re.compile(r"\+\d[\d ()-]{6,}\d")


def parse_text_mentions(text: str, file: EvidenceFile) -> list[Observation]:
    """Prose documents (PDF attachments, statements): the document itself is the record.

    Every resource the document names is linked to every identifier it contains, and the
    identifiers it shows are PII still held by that document.
    """
    rids = sorted({m.group(0) for m in RESOURCE_RE.finditer(text)})
    idents, pii = set(), set()
    for e in EMAIL_RE.findall(text):
        idents |= norm_ident(e)
        pii.add("email")
    for p in PHONE_RE.findall(text):
        idents |= norm_ident(p)
        pii.add("phone")
    idents |= set(re.findall(r"\buser_\d+\b", text))
    return [Observation(file.file_id, file.captured_at, file.captured_at, system_of(rid), rid,
                        present=True, idents=set(idents), pii=set(pii), source="document",
                        content_idents=set(idents))
            for rid in rids]


def parse_text(file: EvidenceFile, text: str | None = None) -> list[Observation]:
    text = text if text is not None else file.path.read_text()
    return parse_pipe_text(text, file) or parse_text_mentions(text, file)


# --------------------------------------------------------------------------- driver

@dataclass
class Evidence:
    files: dict[str, EvidenceFile]
    observations: list[Observation]
    texts: dict[str, str]  # raw text of PDFs / txt, kept for identifier search

    def by_resource(self) -> dict[str, list[Observation]]:
        idx = defaultdict(list)
        for o in self.observations:
            idx[o.resource_id].append(o)
        return idx


def display_aliases() -> dict[str, str]:
    """Labels screens show instead of resource ids, from data-catalog link events:
    "linked tombstone_17 (user_17) to ..." -> {"user_17": "tombstone_17"}."""
    out = {}
    with open(DATASET / "system_activity.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = re.search(r"\b(?:linked|confirmed) (\S+) \(([^)]+)\)", r["details"])
            if m and m.group(1) == r["resource_id"] and m.group(2) != m.group(1):
                out[m.group(2).strip().lower()] = m.group(1)
    return out


def load_manifest() -> dict[str, EvidenceFile]:
    with open(DATASET / "evidence_files.csv", newline="") as fh:
        return {r["file_id"]: EvidenceFile(r["file_id"], DATASET / r["file_path"], r["origin"],
                                           r["captured_at"])
                for r in csv.DictReader(fh)}


def ingest(image_parser=None) -> Evidence:
    """Parse every evidence file. `image_parser(EvidenceFile) -> list[Observation]` handles PNGs."""
    files = load_manifest()
    observations: list[Observation] = []
    texts: dict[str, str] = {}
    for f in files.values():
        suffix = f.path.suffix.lower()
        if suffix == ".sql":
            obs = parse_sql(f)
        elif suffix == ".json":
            obs = parse_json(f)
        elif suffix == ".log":
            obs = parse_log(f)
        elif suffix == ".txt":
            texts[f.file_id] = f.path.read_text()
            obs = parse_text(f, texts[f.file_id])
        elif suffix == ".pdf":
            texts[f.file_id] = pdf_text(f.path)
            obs = parse_text(f, texts[f.file_id])
        elif suffix in {".png", ".jpg", ".jpeg"}:
            obs = image_parser(f) if image_parser else []
        else:
            obs = []
        f.systems |= {o.system for o in obs}
        observations.extend(obs)
    return Evidence(files, observations, texts)


if __name__ == "__main__":
    ev = ingest()
    from collections import Counter
    print(f"{len(ev.files)} files, {len(ev.observations)} observations")
    print(Counter(Path(f.path).suffix for f in ev.files.values()))
    per_file = Counter(o.file_id for o in ev.observations)
    empty = [fid for fid, f in ev.files.items() if fid not in per_file and f.path.suffix != ".png"]
    print("non-image files with 0 observations:", empty)
