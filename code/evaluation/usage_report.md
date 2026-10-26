# Usage Report: Ghost Data Still Exists

## Summary

| | |
|---|---|
| Provider | Google Gemini API (free tier) |
| Models | `gemini-3.5-flash` (request classification, 2 screenshots), `gemini-3.8-flash` (9 screenshots) |
| What the LLM does | (1) decides whether each request text authorises deletion and which systems it names, in batches of 40 texts per call; (2) extracts records from the 11 PNG screenshots, one call per image |
| What it does not do | Deletion decisions, policy, step planning and evidence citation are all deterministic Python |
| Final full-dataset run (`python code/main.py`) | **0 API calls**: all 409 answers (398 unique request texts + 11 screenshots) came from `code/llm_cache.json` |
| Actual cost | **$0.00** (free tier) |
| Estimated cost if billed at paid-tier rates | **$0.50** for the answers behind `output.csv`; $0.71 for every call made during the project |

The 550 requests (518 evaluation + 32 samples) contain only 398 distinct texts. Each distinct text is classified once and the answer is reused for every request with that text.

## Calls behind the final `output.csv`

These calls produced the cached answers that the final run used.

| Model | Job | Calls | Items | Input tokens | Output tokens | Est. paid cost (USD) |
|---|---|---|---|---|---|---|
| gemini-3.5-flash | Request classification (prompt v3, 40 texts/call) | 10 | 398 texts | 22,434 | 38,385 | 0.3791 |
| gemini-3.8-flash | Screenshot extraction | 9 | 9 images | 13,186 | 14,764 | 0.0653 |
| gemini-3.5-flash | Screenshot extraction | 2 | 2 images | 2,926 | 5,621 | 0.0550 |
| **Total** | | **21** | 398 texts + 11 images | **38,546** | **58,770** | **0.4993** |

Per model, for the output: gemini-3.5-flash had 12 calls, 25,360 input and 44,006 output tokens ($0.4341); gemini-3.8-flash had 9 calls, 13,186 input and 14,764 output tokens ($0.0653).

### Averages per evaluation request (518 requests)

| | Per request |
|---|---|
| Input tokens | 74.4 |
| Output tokens | 113.5 |
| Total tokens | 187.9 |
| Estimated paid cost | $0.00096 |
| Model calls | 0.041 |

Output tokens include the model's internal "thinking" tokens, which Gemini bills as output. That is why output exceeds input.

## All calls made during the project

| Model | Calls | Input tokens | Output tokens | Est. paid cost (USD) | Notes |
|---|---|---|---|---|---|
| gemini-3.8-flash | 19 | 34,870 | 52,242 | 0.2221 | 10 request batches with the first prompt (v2), **superseded**; 9 screenshots, used |
| gemini-3.5-flash | 13 | 25,875 | 49,705 | 0.4862 | 10 request batches (v3), used; 2 screenshots, used; 1 custom-test batch (6 texts, case C6) |
| **Overall** | **32** | **60,745** | **101,947** | **0.7082** | 162,692 tokens in total |

- **Superseded calls:** the first request prompt labelled deletion follow-ups ("can you verify my account is gone?") as non-deletion. Those 10 batches (21,684 in / 37,478 out, about $0.16) were re-run with the fixed prompt, and their answers are ignored through the cache's prompt version. See evaluation_report.md, failure #8.
- **Failed attempts** (HTTP 503 "high demand", 429 daily quota) returned no tokens and are not counted. They were retried with exponential backoff.
- **Daily quota:** gemini-3.8-flash hit its free-tier daily request limit after 19 calls, so the remaining work moved to gemini-3.5-flash.

## Pricing used for estimates

| Model | Input $/1M | Output $/1M | Source |
|---|---|---|---|
| gemini-3.8-flash | 0.75 | 3.75 | Gemini API pricing page, paid standard tier, introductory rate through 31 Dec 2026 |
| gemini-3.5-flash | 1.50 | 9.00 | Google release note for 3.6 Flash (3.5 Flash's output rate) and a developer forum post. Google lists 3.5 Flash as deprecated, with requests routed to 3.6 Flash. |

## Reproducing these numbers

Every API call is appended to `calls` in `code/llm_cache.json` with provider, model, job, item count and token counts. `python code/main.py` writes per-model totals and that run's API-call, cache-hit and fallback counts to `code/evaluation/last_run.json`. No API keys or credentials are stored in either file.
