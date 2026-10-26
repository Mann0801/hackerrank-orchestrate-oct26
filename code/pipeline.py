"""Wires ingest -> identity -> llm -> audit together for a request file."""

from __future__ import annotations

from functools import lru_cache

from audit import Auditor, Case, keyword_systems, load_cases
from identity import IdentityIndex
from ingest import ingest
from llm import LLMReader

REQUEST_FILES = ("sample_deletion_requests.csv", "deletion_requests.csv")


def read_requests(cases: list[Case], reader: LLMReader) -> tuple[set[str], dict[str, set[str]]]:
    """Non-deletion case_ids and the systems each request names (keyword fallback)."""
    non_deletion, systems = set(), {}
    answers = reader.classify_requests([c.request_text for c in cases])
    for c in cases:
        answer = answers.get(c.request_text)
        if answer is None:
            systems[c.case_id] = keyword_systems(c.request_text)
            continue
        if not answer["authorizes_deletion"]:
            non_deletion.add(c.case_id)
        systems[c.case_id] = set(answer["systems"])
    return non_deletion, systems


@lru_cache(maxsize=None)
def build(offline: bool = False) -> tuple[Auditor, set[str], LLMReader]:
    reader = LLMReader(offline=offline)
    # Every request (sample and evaluation) competes for the same deletion-run records.
    all_cases = [c for f in REQUEST_FILES for c in load_cases(f)]
    non_deletion, systems = read_requests(all_cases, reader)
    auditor = Auditor(ingest(reader.parse_image), IdentityIndex(), all_cases, non_deletion,
                      request_systems=systems)
    return auditor, non_deletion, reader


def run_cases(request_file: str, offline: bool = False) -> dict[str, tuple[str, list[dict]]]:
    auditor, non_deletion, _ = build(offline)
    out = {}
    for case in load_cases(request_file):
        if case.case_id in non_deletion:
            out[case.case_id] = ("NOT_APPLICABLE", [])
        else:
            out[case.case_id] = auditor.evaluate(case)
    return out
