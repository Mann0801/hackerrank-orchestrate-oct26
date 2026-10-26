"""LLM-backed reading of free text and screenshots.

Two jobs, each answered with a strict JSON schema:
  * classify_requests - for each request text: does it authorise deletion, and which systems
                        does it name (any language)? Sent in batches of BATCH_SIZE texts.
  * parse_image       - turn one application screenshot into record dicts, which go through
                        ingest.observe() exactly like rows from SQL/JSON/log evidence.

Providers (LLM_PROVIDER in env/.env, default: gemini if GEMINI_API_KEY is set, else
anthropic if ANTHROPIC_API_KEY is set, else offline):
  * gemini    - Google Gemini API, model GEMINI_MODEL (default a Flash model)
  * anthropic - Anthropic API, model claude-opus-5-5
  * offline   - no calls; uncached inputs fall back to keyword rules / no image evidence

Answers are cached per item in code/llm_cache.json, keyed by (provider, model, prompt
version, input), so re-runs are deterministic and only send what is missing. Every API call
is logged there with its token usage for evaluation/usage_report.md.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from ingest import REPO_ROOT, EvidenceFile, Observation, display_aliases, observe, system_of

# Bump a version when its prompt changes: cached answers from other versions are ignored.
PROMPT_VERSIONS = {"request": "v3", "image": "v2"}
CACHE_PATH = Path(__file__).resolve().parent / "llm_cache.json"
BATCH_SIZE = 40
MAX_RETRIES = 6

DEFAULT_MODELS = {"gemini": "gemini-3.5-flash", "anthropic": "claude-opus-5-5"}
# USD per million tokens (input, output). Gemini free tier is not billed; the paid-tier rate
# is only used for the "estimated cost if billed" line of the usage report.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "gemini-3.8-flash": (0.75, 3.75),  # paid standard tier, introductory rate through 2026
    "gemini-3.5-flash": (1.50, 9.00),
}

SYSTEMS = Literal[
    "account_service", "search_service", "support_platform", "analytics_platform",
    "export_service", "integration_service", "backup_service", "security_service",
    "identity_service", "cache_service", "email_delivery_service", "legal_service",
    "marketing_platform", "object_storage",
]

REQUEST_PROMPT = """You review privacy requests sent to a commerce platform. Each request \
below has an id and may be written in any language. For every request return one result \
with the same id, answering:

1. authorizes_deletion: true if the person asks for their account or personal data to be \
deleted or erased (closing the account and removing its data counts), and also when they \
follow up on a deletion they already requested: asking to check, verify or confirm that it \
was completed, that nothing was left behind, or that remaining records cannot identify them. \
Such follow-ups are part of the same deletion request.
   false when the person wants their data or account kept: requests to access, download, \
export or receive a copy of data, to correct or update it, for login or account help, for \
invoices, to opt out of marketing, or other questions, even when they mention data or privacy.
2. systems: the platform systems the text explicitly points at, from this list:
   search_service (search results/index), support_platform (support tickets, conversations, \
attachments), analytics_platform (analytics, statistics, telemetry), integration_service \
(syncs, connectors, sending data to other systems or copying it back), backup_service \
(backups, restores), export_service (exports, downloads, archives the user generated), \
identity_service (deleted-account reference, tombstone, internal reference), \
marketing_platform, email_delivery_service, object_storage (uploaded files), cache_service.
   Generic phrases such as "all my data" or "any copies" name no specific system.

Judge each request on its own text only.

{requests}"""

IMAGE_PROMPT = """This is a screenshot of an internal admin console of a commerce platform \
(origin: {origin}). Extract every data record the screen displays as its own subject \
(a profile, index entry, ticket, attachment, export, connector, backup, audit entry, object, \
tombstone ...). Record IDs look like prefix_digits, e.g. account_5762, search_0042, \
support_9538, data_flow_12, backup_9287, object_19, audit_record_003, tombstone_17, \
marketing_001.

For each record give:
- resource_id exactly as shown.
- status: the record's displayed state, normalised to one of FOUND, NOT_FOUND, DELETED, \
ACTIVE, DISABLED, BLOCKED, REDACTED, ANONYMIZED, IDENTIFIABLE; null if none is shown.
- enabled: for a sync/connector/data flow, whether it is enabled or running; else null.
- restore_allowed: for a backup, whether restore is allowed/eligible; else null.
- email, phone, full_name, email_hash, user_id: the values literally visible on the record, \
null when absent, blank, masked or shown as [redacted].
- file: an attachment file name shown on a support record, else null.
- record_type: a data category label if the screen shows one (e.g. ACCOUNT_TOMBSTONE, \
SUPPORT_ATTACHMENT), else null.

Do not list records that are only mentioned as a link or reference to another screen \
(e.g. "Support: CS-8538 · support_9538" inside a customer profile). Copy values exactly; \
never guess or complete them."""


class RequestResult(BaseModel):
    id: int
    authorizes_deletion: bool
    systems: list[SYSTEMS]


class RequestBatch(BaseModel):
    results: list[RequestResult]


class ScreenRecord(BaseModel):
    resource_id: str
    status: str | None
    enabled: bool | None
    restore_allowed: bool | None
    email: str | None
    phone: str | None
    full_name: str | None
    email_hash: str | None
    user_id: str | None
    file: str | None
    record_type: str | None


class ScreenAnswer(BaseModel):
    records: list[ScreenRecord]


class QuotaExhausted(Exception):
    """The provider refused for a daily quota: retrying today will not help."""


def load_env() -> None:
    """Read KEY=VALUE lines from repo/.env into os.environ (existing env vars win)."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def with_backoff(call, is_retryable, is_daily_quota):
    """Run `call()`, retrying rate-limit (429) and overload (5xx) errors with exponential
    backoff (2s, 4s, ... 64s)."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return call()
        except Exception as e:  # noqa: BLE001 - provider-specific checks below
            if not is_retryable(e):
                raise
            if is_daily_quota(e):
                raise QuotaExhausted(str(e)) from e
            if attempt == MAX_RETRIES:
                raise
            delay = min(2 ** (attempt + 1), 64)
            print(f"[llm] {type(e).__name__}, retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)


# ------------------------------------------------------------------- providers
# Each provider takes content parts [("text", str) | ("image", bytes)] and a pydantic
# schema, and returns (parsed dict, input_tokens, output_tokens).

class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str):
        from google import genai
        from google.genai import errors, types
        self.model = model
        self.types = types
        self.errors = errors
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.api_error = errors.APIError

    def _is_retryable(self, e) -> bool:
        return isinstance(e, self.errors.APIError) and e.code in {429, 500, 503}

    @staticmethod
    def _is_daily_quota(e) -> bool:
        return bool(re.search(r"PerDay|per day|RPD", str(e)))

    def generate(self, parts, schema: type[BaseModel]):
        contents = [self.types.Part.from_bytes(data=v, mime_type="image/png") if kind == "image"
                    else self.types.Part.from_text(text=v) for kind, v in parts]
        config = self.types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema, temperature=0)
        response = with_backoff(
            lambda: self.client.models.generate_content(
                model=self.model, contents=contents, config=config),
            self._is_retryable, self._is_daily_quota)
        parsed = response.parsed
        if parsed is None:
            parsed = schema.model_validate_json(response.text or "")
        meta = response.usage_metadata
        out_tokens = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        return parsed.model_dump(), meta.prompt_token_count or 0, out_tokens


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str):
        import anthropic
        self.model = model
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.api_error = anthropic.APIError

    def _is_retryable(self, e) -> bool:
        return isinstance(e, (self.anthropic.RateLimitError, self.anthropic.InternalServerError))

    def generate(self, parts, schema: type[BaseModel]):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": base64.standard_b64encode(v).decode()}}
                   if kind == "image" else {"type": "text", "text": v} for kind, v in parts]
        response = with_backoff(
            lambda: self.client.messages.parse(
                model=self.model, max_tokens=16000, output_config={"effort": "low"},
                messages=[{"role": "user", "content": content}], output_format=schema),
            self._is_retryable, lambda e: False)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise ValueError(f"no parsed output (stop_reason={response.stop_reason})")
        return (response.parsed_output.model_dump(), response.usage.input_tokens,
                response.usage.output_tokens)


def pick_provider() -> str:
    choice = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if choice:
        return choice
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "offline"


# ---------------------------------------------------------------------- reader
class LLMReader:
    def __init__(self, offline: bool = False):
        load_env()
        self.provider_name = "offline" if offline else pick_provider()
        self.model = (os.environ.get(f"{self.provider_name.upper()}_MODEL")
                      or DEFAULT_MODELS.get(self.provider_name, "none"))
        self.provider = None
        if self.provider_name == "gemini":
            self.provider = GeminiProvider(self.model)
        elif self.provider_name == "anthropic":
            self.provider = AnthropicProvider(self.model)
        cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        self.answers: dict[str, dict] = cache.get("answers", {})
        self.call_log: list[dict] = cache.get("calls", [])
        self.quota_hit = False
        self.aliases: dict[str, str] | None = None
        # This run only.
        self.calls = 0
        self.cache_hits = 0
        self.fallbacks = 0

    def _key(self, kind: str, payload: str) -> str:
        raw = f"{self.provider_name}|{self.model}|{PROMPT_VERSIONS[kind]}|{kind}|{payload}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _lookup(self, kind: str, payload: str):
        """Cached answer for this provider/model, else another model's answer to the same
        prompt version (so a cache built with one model still serves an offline run or a
        model switch)."""
        key = self._key(kind, payload)
        if key in self.answers:
            return self.answers[key]["answer"]
        for _, entry in sorted(self.answers.items()):
            if (entry.get("kind") == kind and entry.get("payload_hash") == _h(payload)
                    and entry.get("prompt_version", "v2") == PROMPT_VERSIONS[kind]):
                return entry["answer"]
        return None

    def _store(self, kind: str, payload: str, answer: dict) -> None:
        self.answers[self._key(kind, payload)] = {
            "kind": kind, "provider": self.provider_name, "model": self.model,
            "prompt_version": PROMPT_VERSIONS[kind], "payload_hash": _h(payload),
            "answer": answer}

    def _call(self, kind: str, parts, schema, items: int):
        """One API call; logs tokens. Returns None (and stops further calls on a daily quota)."""
        if self.provider is None or self.quota_hit:
            return None
        try:
            answer, tin, tout = self.provider.generate(parts, schema)
        except QuotaExhausted:
            print("[llm] daily quota exhausted; remaining items use the fallback",
                  file=sys.stderr)
            self.quota_hit = True
            return None
        except (ValidationError, ValueError) as e:
            print(f"[llm] {kind} call returned unusable output: {e}", file=sys.stderr)
            return None
        except self.provider.api_error as e:  # still failing after retries: skip this item
            print(f"[llm] {kind} call failed: {e}", file=sys.stderr)
            return None
        self.calls += 1
        self.call_log.append({"kind": kind, "provider": self.provider_name, "model": self.model,
                              "items": items, "input_tokens": tin, "output_tokens": tout})
        return answer

    def save(self) -> None:
        CACHE_PATH.write_text(json.dumps({"answers": self.answers, "calls": self.call_log},
                                         indent=1, sort_keys=True, ensure_ascii=False))

    # ------------------------------------------------------------------- jobs
    def classify_requests(self, texts: list[str]) -> dict[str, dict]:
        """text -> {'authorizes_deletion': bool, 'systems': [...]}; texts with no answer
        (offline, quota, bad output) are left out so the caller can fall back."""
        unique = sorted(set(texts))
        out, todo = {}, []
        for t in unique:
            cached = self._lookup("request", t)
            if cached is not None:
                out[t] = cached
                self.cache_hits += 1
            else:
                todo.append(t)
        for start in range(0, len(todo), BATCH_SIZE):
            batch = todo[start:start + BATCH_SIZE]
            listing = "\n\n".join(f'<request id="{i}">\n{t}\n</request>'
                                  for i, t in enumerate(batch))
            answer = self._call("request_batch",
                                [("text", REQUEST_PROMPT.format(requests=listing))],
                                RequestBatch, items=len(batch))
            for r in (answer or {}).get("results", []):
                if 0 <= r["id"] < len(batch):
                    t = batch[r["id"]]
                    out[t] = {"authorizes_deletion": r["authorizes_deletion"],
                              "systems": sorted(set(r["systems"]))}
                    self._store("request", t, out[t])
            self.save()
        self.fallbacks += len(unique) - len(out)
        return out

    def parse_image(self, file: EvidenceFile) -> list[Observation]:
        data = file.path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        answer = self._lookup("image", digest)
        if answer is not None:
            self.cache_hits += 1
        else:
            answer = self._call("image", [("image", data),
                                          ("text", IMAGE_PROMPT.format(origin=file.origin))],
                                ScreenAnswer, items=1)
            if answer is None:
                self.fallbacks += 1
                return []
            self._store("image", digest, answer)
            self.save()
        if self.aliases is None:
            self.aliases = display_aliases()
        out = []
        for rec in answer.get("records", []):
            # observe() reads flag fields as text, like the other evidence formats.
            flat = {k: (str(v).lower() if isinstance(v, bool) else v)
                    for k, v in rec.items() if v is not None}
            # Screens may show a display label (user_17, a file name) instead of the id.
            if not system_of(flat["resource_id"]):
                for label in (flat["resource_id"], flat.get("file", "")):
                    if label.strip().lower() in self.aliases:
                        flat["resource_id"] = self.aliases[label.strip().lower()]
                        break
            obs = observe(flat, file, source="image")
            if obs:
                out.append(obs)
        return out

    # ------------------------------------------------------------------ usage
    def usage(self) -> dict:
        """Calls and tokens per provider/model over every logged call (the calls that built
        the cached answers behind this output), plus this run's cache behaviour."""
        per_model: dict[str, dict] = {}
        for c in self.call_log:
            m = per_model.setdefault(f'{c["provider"]}:{c["model"]}', {
                "calls": 0, "items": 0, "input_tokens": 0, "output_tokens": 0})
            for f in ("items", "input_tokens", "output_tokens"):
                m[f] += c[f]
            m["calls"] += 1
        for name, m in per_model.items():
            price = PRICES.get(name.split(":", 1)[1])
            m["cost_usd"] = (round(m["input_tokens"] / 1e6 * price[0]
                                   + m["output_tokens"] / 1e6 * price[1], 4)
                             if price else 0.0)
        return {"per_model": per_model,
                "this_run": {"provider": self.provider_name, "model": self.model,
                             "api_calls": self.calls, "cache_hits": self.cache_hits,
                             "fallbacks": self.fallbacks}}


def _h(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()
