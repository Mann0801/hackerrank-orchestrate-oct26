"""Identity resolution.

Maps a request email (current or historical) to the stable user_id and collects every
identifier that user has ever had, normalised the same way as ingest.norm_ident so they
can be compared directly with Observation.idents.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field

from ingest import DATASET, norm_ident

# Identity fields that are specific enough to link a record to one person on their own.
STRONG_FIELDS = {"email", "phone", "email_hash"}


@dataclass
class Identity:
    user_id: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    history: list[str] = field(default_factory=list)  # audit_ids that revealed old values

    @property
    def strong_keys(self) -> set[str]:
        """Normalised keys that link a record to this user on their own."""
        keys = {self.user_id}
        for v in self.emails | self.phones | self.hashes:
            keys |= norm_ident(v)
        return keys

    @property
    def name_keys(self) -> set[str]:
        return {k for n in self.names for k in norm_ident(n)}


class IdentityIndex:
    def __init__(self):
        self.users: dict[str, Identity] = {}
        self._by_key: dict[str, set[str]] = defaultdict(set)
        with open(DATASET / "user_identities.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                u = self.users.setdefault(r["user_id"], Identity(r["user_id"]))
                self._add(u, "email", r["email"])
                self._add(u, "phone", r["phone"])
                self._add(u, "name", r["name"])
                self._add(u, "email_hash", r["email_hash"])
        with open(DATASET / "user_audit_log.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                u = self.users.setdefault(r["user_id"], Identity(r["user_id"]))
                for value in (r["old_value"], r["new_value"]):
                    self._add(u, r["field_name"], value)
                u.history.append(r["audit_id"])

    def _add(self, u: Identity, field_name: str, value: str) -> None:
        if not value:
            return
        bucket = {"email": u.emails, "phone": u.phones, "name": u.names,
                  "full_name": u.names, "email_hash": u.hashes}.get(field_name)
        if bucket is None:
            return
        bucket.add(value)
        if field_name in STRONG_FIELDS:
            for k in norm_ident(value):
                self._by_key[k].add(u.user_id)

    def resolve(self, email: str) -> Identity | None:
        """User for a request email; None if unknown or ambiguous."""
        matches = set()
        for k in norm_ident(email):
            matches |= self._by_key.get(k, set())
        return self.users[matches.pop()] if len(matches) == 1 else None

    def owners(self, keys: set[str]) -> set[str]:
        """All users whose strong identifiers appear in `keys` (for decoy detection)."""
        out = set()
        for k in keys:
            out |= self._by_key.get(k, set())
            if k in self.users:  # a bare user_id reference
                out.add(k)
        return out

    def shared_keys(self) -> dict[str, set[str]]:
        return {k: v for k, v in self._by_key.items() if len(v) > 1}


if __name__ == "__main__":
    idx = IdentityIndex()
    print(f"{len(idx.users)} users; strong identifiers shared by >1 user: {len(idx.shared_keys())}")
    with open(DATASET / "sample_deletion_requests.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            u = idx.resolve(r["email"])
            print(r["case_id"], r["email"], "->", u.user_id if u else None,
                  f"emails={len(u.emails)} phones={len(u.phones)} audit={u.history}" if u else "")
