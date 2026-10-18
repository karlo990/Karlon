"""davis/local_llm.py — Davis's own model, running inside the Karlon server.

llama.cpp (llama-cpp-python) on the CPU with a 4-bit GGUF instruct model.
One model instance per process, loaded once (warm-up at startup, so the
first guest doesn't wait for it), used by one generation at a time (a
llama.cpp context is not thread-safe; replies are short, so a queue of
two or three is seconds). Generation runs in a worker thread, so the event
loop keeps serving the app and the bridge meanwhile.

Speed comes from three things:
- the system prompt (persona) is identical on every call, so llama.cpp
  reuses its computed prefix and only reads the new part of each turn;
- the answer is constrained to a tiny JSON schema by a grammar, so the
  model can't ramble or produce invalid JSON;
- threads = the cores this container really has (cgroup quota), not the
  host's count: oversubscribing threads is what collapsed the Conscious
  Worker's decode speed to under 1 token/s.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from . import config


def container_cpus() -> int:
    """Cores this container may use (cgroup v2 / v1 quota), else os.cpu_count()."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max" and int(period) > 0:
            return max(1, int(quota) // int(period))
    except Exception:
        pass
    try:
        q = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        p = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if q > 0 and p > 0:
            return max(1, q // p)
    except Exception:
        pass
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return max(1, os.cpu_count() or 1)


class LocalModel:
    def __init__(self) -> None:
        self._llm = None
        self._load_lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self.state = "not_loaded"        # not_loaded | downloading | loading | ready | error
        self.error: Optional[str] = None
        self.path: Optional[str] = None
        self.load_s: Optional[float] = None
        self.threads = config.LOCAL_THREADS or container_cpus()
        self.calls = 0
        self.last: dict = {}

    # ── loading ───────────────────────────────────────────────────────────────
    def model_path(self) -> Path:
        return Path(config.LOCAL_MODEL_DIR).resolve() / config.LOCAL_MODEL_FILE

    def _ensure_file(self) -> Path:
        path = self.model_path()
        if path.exists() and path.stat().st_size > 0:
            return path
        self.state = "downloading"
        from huggingface_hub import hf_hub_download
        path.parent.mkdir(parents=True, exist_ok=True)
        got = hf_hub_download(config.LOCAL_MODEL_REPO, config.LOCAL_MODEL_FILE, local_dir=str(path.parent))
        return Path(got)

    def load(self):
        """Load the model (download it first if needed). Safe to call from
        several threads; only the first one does the work."""
        if self._llm is not None:
            return self._llm
        with self._load_lock:
            if self._llm is not None:
                return self._llm
            t0 = time.monotonic()
            try:
                path = self._ensure_file()
                self.state = "loading"
                from llama_cpp import Llama
                self._llm = Llama(model_path=str(path), n_ctx=config.LOCAL_CTX, n_threads=self.threads,
                                  n_threads_batch=self.threads, n_batch=256, verbose=False)
                self.path = str(path)
                self.state, self.error = "ready", None
                self.load_s = round(time.monotonic() - t0, 1)
                print(f"[davis] local model ready: {path.name}, {self.threads} threads, {self.load_s}s")
            except Exception as e:
                self.state, self.error = "error", f"{type(e).__name__}: {e}"[:400]
                print(f"[davis] local model failed to load: {self.error}")
                raise
            return self._llm

    def warm(self, msgs: list[dict], schema: Optional[dict] = None) -> None:
        """Load the model and compute the persona + examples prefix once, at
        startup, the same way a real call does (same JSON constraint)."""
        try:
            llm = self.load()
            with self._gen_lock:
                kw = {"response_format": {"type": "json_object", "schema": schema}} if schema else {}
                llm.create_chat_completion(messages=msgs, max_tokens=1, **kw)
        except Exception:
            pass

    # ── generation ────────────────────────────────────────────────────────────
    def generate(self, msgs: list[dict], schema: dict) -> tuple[dict, dict]:
        """Blocking: one JSON answer. Call from a worker thread."""
        llm = self.load()
        t0 = time.monotonic()
        with self._gen_lock:
            waited = time.monotonic() - t0
            out = llm.create_chat_completion(
                messages=msgs,
                response_format={"type": "json_object", "schema": schema},
                temperature=config.LOCAL_TEMPERATURE, max_tokens=config.LOCAL_MAX_TOKENS)
        took = time.monotonic() - t0 - waited
        usage = out.get("usage") or {}
        text = out["choices"][0]["message"]["content"] or ""
        self.calls += 1
        self.last = {"prompt_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                     "seconds": round(took, 2), "queued_s": round(waited, 2),
                     "tokens_per_s": round((usage.get("completion_tokens") or 0) / took, 1) if took > 0 else None}
        if out["choices"][0].get("finish_reason") == "length":
            # Cut off at max_tokens (digits are one token each, so a reply
            # with a payment number, KCER and totals runs long): keep its
            # complete sentences; the guard still checks what's left.
            cut = salvage(text)
            if cut is None:
                raise ValueError("local model ran out of tokens")
            self.last["truncated"] = True
            return cut, {"model": config.LOCAL_MODEL_FILE, **self.last}
        return json.loads(text), {"model": config.LOCAL_MODEL_FILE, **self.last}

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "file": config.LOCAL_MODEL_FILE,
                "repo": config.LOCAL_MODEL_REPO, "threads": self.threads, "load_s": self.load_s,
                "calls": self.calls, "last": self.last}


def salvage(text: str) -> Optional[dict]:
    """The reply out of JSON that stopped mid-way: its complete sentences, or
    None if fewer than 5 words survive."""
    m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)', text or "", re.S)
    if not m:
        return None
    raw, body = m.group(1), None
    for cut in range(0, 7):                     # a half-written escape at the end
        try:
            body = json.loads('"' + raw[:len(raw) - cut] + '"')
            break
        except ValueError:
            continue
    if body is None:
        return None
    ends = [i for i, ch in enumerate(body) if ch in ".!?"]
    body = body[:ends[-1] + 1] if ends else ""
    if len(body.split()) < 5:
        return None
    return {"reply": body.strip()}


model = LocalModel()
