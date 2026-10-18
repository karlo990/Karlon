"""crumb/llm.py — Claude writes Crumb's replies.

One Messages API call per visitor question: Crumb's instructions as the
(cached) system prompt, the visitor's recent conversation as messages.
Claude Opus 5.5 thinks adaptively on its own; effort is kept low because
these are short chat replies. A refusal is retried server-side on the model
Anthropic recommends for that category (fallbacks="default"). When no key is
set, or the API fails, Crumb answers from the bakery facts instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import anthropic

from . import config, knowledge

REFUSAL_REPLY = ("I'm just a bakery assistant, so that's not one for me 🥐 Ask me about the bakes, the pastry "
                 "box or the Sunday Crumb story!")
_client: Optional[anthropic.AsyncAnthropic] = None


@dataclass
class Reply:
    text: str
    source: str                  # model | offline | refusal
    latency_ms: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error: Optional[str] = None


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=config.LLM_TIMEOUT_SEC,
                                           max_retries=2)
    return _client


def clean_history(messages: list[dict]) -> list[dict]:
    """What the widget sent, made safe for the API: user/assistant only, each
    capped, starting with the visitor and ending with their question."""
    out: list[dict] = []
    for m in messages[-config.MAX_SENT:]:
        role, text = m.get("role"), str(m.get("content") or "").strip()[:config.MAX_CHARS]
        if role not in ("user", "assistant") or not text:
            continue
        if out and out[-1]["role"] == role:                 # the API wants turns to alternate
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


async def reply(history: list[dict], notes: str = "") -> Reply:
    question = history[-1]["content"] if history else ""
    if not available():
        return Reply(knowledge.offline_answer(question), "offline", error="no_api_key")
    t0 = time.monotonic()
    try:
        response = await client().beta.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=[{"type": "text", "text": knowledge.system_prompt(notes), "cache_control": {"type": "ephemeral"}}],
            messages=history,
            output_config={"effort": config.EFFORT},
            betas=[config.FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.RateLimitError:
        return _offline(question, t0, "rate_limited")
    except anthropic.AuthenticationError:
        return _offline(question, t0, "bad_api_key")
    except anthropic.BadRequestError as e:
        return _offline(question, t0, f"bad_request:{e.message[:120]}")
    except anthropic.APIStatusError as e:
        return _offline(question, t0, f"api_{e.status_code}")
    except anthropic.APIConnectionError:
        return _offline(question, t0, "connection")
    ms = int((time.monotonic() - t0) * 1000)
    usage = response.usage
    tokens = {"input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None)}
    if response.stop_reason == "refusal":
        return Reply(REFUSAL_REPLY, "refusal", ms, **tokens)
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return Reply(knowledge.offline_answer(question), "offline", ms, error="empty_reply", **tokens)
    return Reply(text, "model", ms, **tokens)


def _offline(question: str, t0: float, why: str) -> Reply:
    return Reply(knowledge.offline_answer(question), "offline", int((time.monotonic() - t0) * 1000), error=why)
