# Ghost Data Still Exists: Deletion-Audit Agent

HackerRank Orchestrate, October 2026.

For every request in `dataset/deletion_requests.csv`, this agent decides whether the person actually asked for deletion. If they did, it rebuilds what really existed on the case's `audit_at` date from the supplied evidence (it never trusts the workflow's "Complete" claim) and applies `deletion_rules.csv`. It then writes `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with an ordered list of findings, remediation and verification steps to `output.csv`.

Most of the system is deterministic Python. An LLM (Google Gemini Flash) is used only for what code cannot do reliably: reading free-text requests in any language, and reading application screenshots. Every LLM answer is cached in `code/llm_cache.json`, so the full run is reproducible and needs **no API key and no API calls**.

## Setup

Requirements:
- Python 3.10 or newer (developed on 3.14).
- `pdftotext` from poppler, to read the PDF evidence:
  - Debian/Ubuntu: `sudo apt install poppler-utils`
  - Fedora: `sudo dnf install poppler-utils`
  - macOS: `brew install poppler`

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r code/requirements.txt
```

**Optional: an API key, only needed if you change a prompt or add new inputs.** Create `.env` in the repository root. It is read automatically and must never be committed or zipped.

```bash
GEMINI_API_KEY=your-key-here
# optional overrides:
# GEMINI_MODEL=gemini-3.5-flash
# LLM_PROVIDER=gemini            # gemini | anthropic | offline
# ANTHROPIC_API_KEY=...          # only with LLM_PROVIDER=anthropic
```

## Run

From the repository root:

```bash
python code/main.py               # writes ./output.csv (518 rows)
python code/main.py --offline     # never call an LLM: cache only, else keyword fallback
```

`main.py` validates the result before writing it: 518 rows, valid statuses, consecutive step numbers, phase order, allowed operation/state pairs, and cited evidence that exists and is not dated after `audit_at`. If anything fails, it stops with an error. It also writes a run summary (status counts, LLM calls and tokens) to `code/evaluation/last_run.json`.

Expected output:
```
wrote output.csv: 518 rows, 3250 steps, {'COMPLETE': 155, 'INCOMPLETE': 353, 'NOT_APPLICABLE': 10}
llm: gemini:gemini-3.5-flash - 0 API calls, 409 cached answers, 0 fallbacks
```

## Evaluate

```bash
python code/evaluation/main.py --verbose     # score against the 32 public sample answers
python code/evaluation/custom_cases.py       # 6 participant-made scenario tests
```

Results and analysis: [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).
LLM calls, tokens and cost: [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

| Public samples | Score |
|---|---|
| Status accuracy | 100% |
| Step F1 | 0.977 |
| Sequence accuracy | 81.25% |
| Custom cases | 6/6 pass |

## How it works

```
request ─▶ 1. intent        Gemini: does the text authorise deletion? ── no ─▶ NOT_APPLICABLE
              │ yes
              ▼
           2. identity      email (current or historical) ─▶ user_id + every past email/phone/hash
              ▼
           3. evidence      94 files (SQL, JSON, logs, PDFs, text, screenshots) ─▶ uniform observations
              ▼
           4. ownership     records linked by stored identifiers, or by deletion-run timing
              ▼
           5. snapshot      newest observation captured at or before audit_at (claims ignored)
              ▼
           6. policy        deletion_rules.csv: delete / redact / anonymize / retain / block / disable
              ▼
           7. steps         COMPLETE: evidence-backed verification
                            INCOMPLETE: findings ─▶ remediation (disable flows and block
                            restores first) ─▶ verification
```

| File | Role |
|---|---|
| `code/main.py` | Entry point: runs all 518 cases, validates them, writes `output.csv` |
| `code/pipeline.py` | Wires the stages together |
| `code/ingest.py` | Parses every evidence format into `Observation`s; maps screen labels to resource ids |
| `code/identity.py` | Resolves current and historical identifiers from `user_identities.csv` and `user_audit_log.csv` |
| `code/audit.py` | Ownership, deletion-run matching, audit-time snapshot, policy and step ordering |
| `code/llm.py` | LLM prompts and schemas, provider switch (Gemini / Anthropic / offline), batching, retry with backoff, cache and token log |
| `code/llm_cache.json` | Cached LLM answers plus a per-call token log |
| `code/evaluation/` | Scorer, custom cases, evaluation and usage reports |

Key design decisions:
- **Claims are not evidence.** Reported statuses and job results are never used as proof of state.
- **Time cutoff.** Only evidence captured at or before `audit_at` is used or cited.
- **Decoys.** A failed lookup (e.g. a 404 search) does not link a record to a person.
- **Replicas.** A copy left in a regional, reporting or failover replica counts as remaining data.
- **Safe order.** Data flows are disabled and restores blocked before records are changed.
- **Deterministic output.** Rows are in case order, JSON uses sorted keys, and the LLM runs from the cache.

## Packaging for submission

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*.env"
```

Submit `code.zip`, `output.csv` and `log.txt` (the chat transcript). Do not include `dataset/`, `.venv/` or `.env`.
