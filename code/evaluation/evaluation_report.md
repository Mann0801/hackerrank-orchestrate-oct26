# Evaluation Report: Ghost Data Still Exists

## 1. How the system was evaluated

| What | How | Command |
|---|---|---|
| Public samples | The full pipeline runs on the 32 requests in `dataset/sample_deletion_requests.csv`; results are compared with `dataset/sample_output.csv`. | `python code/evaluation/main.py [--verbose]` |
| Custom cases | 6 scenarios built by perturbing real inputs (request email, audit time, injected evidence, new request texts). | `python code/evaluation/custom_cases.py` |
| Contract check | Every `code/main.py` run validates all 518 output rows before writing `output.csv`. | `python code/main.py` |

How the metrics are defined (see `code/evaluation/main.py`):

- **Status accuracy:** `deletion_status` matches exactly, per case.
- **Step precision / recall / F1:** a step counts only if *all* of phase, system_id, resource_id, operation, state and evidence_id match (step_no excluded), as a multiset per case.
- **Field accuracy:** per field, over steps aligned by position.
- **Sequence accuracy:** the whole `steps_json` array matches, including order.

## 2. Results on the 32 public samples (final system)

| Metric | Value |
|---|---|
| Case-level deletion-status accuracy | **100.0%** (32/32) |
| Sequence accuracy (whole step list exact) | **81.25%** (26/32) |
| Step precision | 0.984 (187/190) |
| Step recall | 0.969 (187/193) |
| Step F1 | **0.977** |

| Field | Accuracy |
|---|---|
| phase | 98.4% |
| system_id | 95.9% |
| resource_id | 94.3% |
| operation | 95.9% |
| state | 95.9% |
| evidence_id | 97.9% |

**Caveat:** these samples were used while developing the rules, so they overstate accuracy on unseen data. The custom cases below exercise behaviour the samples do not cover.

### Full evaluation run (`output.csv`)

518 rows: 155 COMPLETE, 353 INCOMPLETE, 10 NOT_APPLICABLE, with 3,250 steps. All rows pass the contract validator. The run made 0 API calls because every LLM answer was cached.

The 10 NOT_APPLICABLE cases (case_533 to case_542) were classified by Gemini from the request text alone. Every one of them independently matches a `CLASSIFY_REQUEST … as non-deletion` event in `system_activity.csv` for that user's account, which the model never saw.

## 3. Custom evaluation cases

Each case starts from real dataset inputs, changes one thing, and checks the outcome the challenge rules require. Injected evidence lives only in memory and the dataset is never modified.

| ID | Scenario | Rule exercised | Expected | Result |
|---|---|---|---|---|
| C1 | case_10 re-sent from the user's **old** email (`nadia.petrescu@…`, changed in `user_audit_log.csv`) | Resolve current *and* historical identifiers | Same status and all 4 steps as the sample answer | **PASS** |
| C2 | case_06 with `audit_at` moved to 08:30, **before** its decisive file `evidence_57` (08:45) | Use only information at or before `audit_at`; never cite later evidence | `evidence_57` not cited; nothing cited after `audit_at` | **PASS** (cites evidence_10 and evidence_54 only) |
| C3 | Injected copy of `tombstone_17` that still shows the user's email (eval case_46) | ACCOUNT_TOMBSTONE: keep the user_id, redact direct identifiers | INCOMPLETE; `REPORT_EXPOSED_PII → REDACT_RECORD → VERIFY_RECORD_REDACTED` | **PASS** |
| C4 | Injected **enabled sync** carrying the user's email into otherwise-complete case_01 | Active data flows block COMPLETE; disable flows before changing records | INCOMPLETE; first remediation step `DISABLE_DATA_FLOW`; finding cites the injected evidence | **PASS** |
| C5 | Injected copy of `account_001` captured **after** case_01's `audit_at` | Later evidence must not change the answer | Still COMPLETE, steps identical to the sample answer | **PASS** |
| C6 | 6 new request texts: Spanish copy request ("no borren nada"), French delete, Japanese marketing opt-out, German "verify my deletion" follow-up, Hindi delete, English address change | Decide whether the text authorises deletion, in any language | 3 deletion, 3 non-deletion | **PASS** (6/6, one batched Gemini call) |

**6/6 pass.**

## 4. Failures found and improvements made

### Score history on the public samples

| Version | Status acc. | Step F1 | Sequence acc. | What changed |
|---|---|---|---|---|
| First end-to-end rules | 93.75% | 0.87 | 37.5% | Baseline |
| Evidence and ownership fixes (below) | 100% | 0.977 | 81.25% | Deterministic pipeline only |
| + Gemini, first request prompt | 81.25% | 0.899 | 65.6% | LLM marked deletion follow-ups as non-deletion |
| + prompt fix (final) | **100%** | **0.977** | **81.25%** | Follow-ups count as deletion |

### Failures and fixes

| # | Failure observed | Root cause | Fix |
|---|---|---|---|
| 1 | An empty replica SQL dump "proved" accounts absent | A dump's declared scope was treated as coverage even with zero rows | Coverage comes only from actual rows. Absence-by-omission is used only for SQL snapshots and only when no informative observation exists. |
| 2 | Log mentions counted as live records | Log events defaulted to `present=True` | Log lines imply presence only when they return record content |
| 3 | 404 lookups tied decoy records to the wrong user | A failed lookup's search key was treated as the record's identity | Records with no stored identifiers (`content_idents`) are "weak" links and need deletion-run timing to be assigned |
| 4 | Records leaked between cases of users who sent two requests | Ownership was per user, not per request | Each record goes to the request with the nearest `reported_at` |
| 5 | Unidentified analytics and search records assigned to the wrong case | Run-time matching was dominated by account events | Global one-record-per-system assignment scored by result match, request-text mentions and time distance (account events excluded), with a 2-minute slot-collision rule |
| 6 | PDF identifiers missed | Values sat on different lines from the resource id | PDFs are parsed at document level |
| 7 | Gemini crashed the run with 503 "high demand" | Retry only handled 429 | Exponential backoff on 429/500/503; after retries, the item falls back instead of crashing; a daily-quota 429 stops further calls |
| 8 | 6/32 samples wrongly NOT_APPLICABLE ("I deleted my account, can you verify it's gone?") | The prompt read deletion follow-ups as information requests | The prompt defines follow-ups and verification of a prior deletion as deletion, and lists data-keeping requests as non-deletion |
| 9 | Fixed prompt would have re-used the old wrong answers from the cache | The cache fallback ignored the prompt version | Per-job prompt versions (`request` v3, `image` v2) are stored and checked in the cache |
| 10 | Output validator: case_46 and case_61 verification steps had no evidence | Screens show display labels (`user_17`, `delivery_confirmation_19.pdf`), not resource ids | Labels are mapped to ids using the data-catalog `LINK_RESOURCE_IDENTITY` events. case_46 is now proven by the tombstone screenshot, and case_61 correctly became INCOMPLETE because its object was still in storage. |

### Remaining known errors (6 of 32 samples, all with the correct status)

| Case | Miss | Why |
|---|---|---|
| case_04, case_18, case_20 | Missing step for `account_004`, `audit_record_6645`, `audit_record_5569` | These resource ids appear in no participant-visible file or activity row, so they cannot be discovered from the evidence |
| case_23, case_28, case_29 | Picks `account_7015` instead of `account_410`, and swaps `account_5482` / `account_7012` | Two unlinked accounts match the deletion-run timing equally; the tie is broken the wrong way |

### Possible next improvements

- A tie-break for equally timed unlinked accounts that uses more run signals (the source of the worker's input, the order of secondary operations).
- A small held-out split of the samples, so accuracy isn't measured on data the rules were tuned on.
- Cross-checking the LLM's NOT_APPLICABLE labels with `CLASSIFY_REQUEST` activity automatically, rather than only in analysis.
