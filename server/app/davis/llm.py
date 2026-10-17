"""davis/llm.py — the model providers behind Davis's replies, with the
plumbing that keeps a chat fast when a provider is slow or down:

- one long-lived client per provider (its HTTP connection pool is reused,
  no TLS handshake per message);
- a hard timeout per call, then the rules template is sent instead
  (a late answer is worth less than a plain one: Dean & Barroso, "The Tail
  at Scale", 2013);
- a circuit breaker: after BREAKER_FAILURES failures in a row the provider
  is skipped for BREAKER_COOLDOWN_SEC, so a dead provider costs nothing;
- a small response cache keyed on the exact prompt (a re-processed message
  never pays twice);
- the system prompt is marked cacheable, so the API serves it from its
  prompt cache after the first call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Optional

import requests

from . import config, prompts


class LLMError(Exception):
    pass


class Breaker:
    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def ok(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= config.BREAKER_FAILURES:
            self.open_until = time.monotonic() + config.BREAKER_COOLDOWN_SEC

    def state(self) -> dict:
        return {"open": self.is_open, "failures": self.failures,
                "retry_in_sec": max(0, round(self.open_until - time.monotonic()))}


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError("model returned no JSON")
        try:
            return json.loads(m.group(0))
        except ValueError as e:
            raise LLMError(f"bad JSON from model: {e}")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, effort: str) -> None:
        if not config.ANTHROPIC_API_KEY:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        import anthropic
        self._anthropic = anthropic
        self.model, self.effort = model, effort
        self.client = anthropic.AsyncAnthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=anthropic.Timeout(config.LLM_TIMEOUT_SEC, connect=5.0),
            max_retries=1,
        )

    async def complete(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        a = self._anthropic
        kwargs = dict(
            model=self.model,
            max_tokens=config.LLM_MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        # Server-side fallback re-runs a declined request on another model in
        # the same call. Not offered for Haiku models.
        if not self.model.startswith("claude-haiku"):
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            resp = await self.client.beta.messages.create(**kwargs)
        except a.APITimeoutError as e:
            raise LLMError(f"timeout: {e}")
        except a.RateLimitError as e:
            raise LLMError(f"rate limited: {e}")
        except a.APIStatusError as e:
            raise LLMError(f"API {e.status_code}: {str(e)[:200]}")
        except a.APIConnectionError as e:
            raise LLMError(f"connection: {e}")
        if resp.stop_reason == "refusal":
            raise LLMError("model declined")
        if resp.stop_reason == "max_tokens":
            raise LLMError("ran out of tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        u = resp.usage
        meta = {"model": getattr(resp, "model", self.model),
                "input_tokens": getattr(u, "input_tokens", None),
                "output_tokens": getattr(u, "output_tokens", None),
                "cache_read_tokens": getattr(u, "cache_read_input_tokens", None),
                "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None)}
        return _parse_json(text), meta


class _HTTPProvider:
    """Shared by the Conscious Worker and Ollama: a pooled requests.Session
    used from a worker thread, so the event loop never blocks."""

    def __init__(self) -> None:
        self.http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=config.CONCURRENCY)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    async def _post(self, url: str, body: dict, headers: dict) -> dict:
        def go():
            r = self.http.post(url, json=body, headers=headers, timeout=config.LLM_TIMEOUT_SEC)
            if r.status_code == 503:
                raise LLMError("provider saturated (503)")
            r.raise_for_status()
            return r.json()
        try:
            return await asyncio.to_thread(go)
        except requests.Timeout:
            raise LLMError("timeout")
        except requests.RequestException as e:
            raise LLMError(str(e)[:200])

    @staticmethod
    def _messages(system: str, user: str, schema: dict) -> list[dict]:
        keys = ", ".join(schema["properties"])
        return [{"role": "system", "content": system + f"\nReturn only a JSON object with keys: {keys}."},
                {"role": "user", "content": user}]


class WorkerProvider(_HTTPProvider):
    """The Conscious Worker Space (POST /api/jarvis/conscious, SEDA-gated,
    phi4-mini on CPU). Answers in minutes, not seconds: fine for shadow mode
    drafts, too slow for live chat."""
    name = "worker"

    def __init__(self) -> None:
        super().__init__()
        self.url = config.WORKER_URL + "/api/jarvis/conscious"

    async def complete(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        body = {"calls": [{"call_id": "davis", "messages": self._messages(system, user, schema),
                           "options": {"temperature": 0.2, "num_predict": 220},
                           "deadline_sec": config.LLM_TIMEOUT_SEC,
                           "context_meta": {"role_label": "Davis"}}]}
        data = await self._post(self.url, body, {"X-API-Key": config.WORKER_KEY} if config.WORKER_KEY else {})
        res = (data.get("results") or [{}])[0]
        if res.get("error") or res.get("timed_out"):
            raise LLMError(f"worker: {res.get('error') or 'timed out'}")
        return _parse_json(res.get("content", "")), {"model": res.get("model"), "elapsed_ms": res.get("elapsed_ms")}


class OllamaProvider(_HTTPProvider):
    name = "ollama"

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model if model and not model.startswith("claude") else config.OLLAMA_MODEL

    async def complete(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        body = {"model": self.model, "messages": self._messages(system, user, schema), "stream": False,
                "format": schema, "keep_alive": -1, "options": {"temperature": 0.2, "num_predict": 220}}
        data = await self._post(config.OLLAMA_URL + "/api/chat", body, {})
        return _parse_json((data.get("message") or {}).get("content", "")), {"model": self.model}


class Router:
    """Picks the configured provider, applies breaker + cache, never raises:
    returns (result or None, info) and the caller falls back to rules."""

    def __init__(self) -> None:
        self._providers: dict[tuple, object] = {}
        self.breakers: dict[str, Breaker] = {}
        self._cache: OrderedDict[str, tuple[float, dict, dict]] = OrderedDict()
        self.stats = {"calls": 0, "errors": 0, "cache_hits": 0, "breaker_skips": 0, "rejected": 0}

    def _provider(self, name: str, model: str, effort: str):
        key = (name, model, effort)
        if key not in self._providers:
            if name == "anthropic":
                self._providers[key] = AnthropicProvider(model, effort)
            elif name == "worker":
                self._providers[key] = WorkerProvider()
            elif name == "ollama":
                self._providers[key] = OllamaProvider(model)
            else:
                raise LLMError(f"unknown provider {name}")
        return self._providers[key]

    def breaker(self, name: str) -> Breaker:
        return self.breakers.setdefault(name, Breaker())

    async def complete(self, settings: dict, user: str) -> tuple[Optional[dict], dict]:
        name = settings.get("provider") or "rules"
        info: dict = {"provider": name}
        if name == "rules":
            return None, info
        br = self.breaker(name)
        if br.is_open:
            self.stats["breaker_skips"] += 1
            info["skipped"] = "breaker_open"
            return None, info
        key = hashlib.sha256(f"{name}|{settings.get('model')}|{user}".encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < max(config.CACHE_TTL_SEC, 300):
            self.stats["cache_hits"] += 1
            return hit[1], {**hit[2], "provider": name, "cache_hit": True, "latency_ms": 0}
        t0 = time.monotonic()
        self.stats["calls"] += 1
        try:
            p = self._provider(name, settings.get("model") or config.MODEL, settings.get("effort") or config.EFFORT)
            out, meta = await asyncio.wait_for(p.complete(prompts.SYSTEM, user, prompts.SCHEMA),
                                               timeout=config.LLM_TIMEOUT_SEC + 5)
        except (LLMError, asyncio.TimeoutError, Exception) as e:     # never let a provider break a chat
            br.fail()
            self.stats["errors"] += 1
            info.update(error=(str(e) or type(e).__name__)[:300], latency_ms=int((time.monotonic() - t0) * 1000))
            return None, info
        br.ok()
        info.update(meta, latency_ms=int((time.monotonic() - t0) * 1000))
        self._cache[key] = (time.monotonic(), out, meta)
        while len(self._cache) > 500:
            self._cache.popitem(last=False)
        return out, info

    def state(self) -> dict:
        return {"stats": dict(self.stats), "breakers": {k: b.state() for k, b in self.breakers.items()}}
