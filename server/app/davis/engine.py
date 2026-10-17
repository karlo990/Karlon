"""davis/engine.py — runs Davis inside the Karlon server.

Pipeline for one chat (each step is a stage with its own queue and limit,
in the spirit of SEDA, Welsh, Culler & Brewer, SOSP 2001):

    hook (sync / import / sweeper)
      → debounce per chat (DEBOUNCE_SEC: the guest may still be typing)
      → admission: global semaphore (CONCURRENCY) + one lock per chat
      → preprocess (nlu) → gate (answer or not, with a reason)
      → claim the message (atomic, never answered twice)
      → policy plan (actions) → model rewrites the reply (optional, guarded)
      → live: execute on the outbox  |  shadow: save a draft for approval
      → events + metrics → dashboard feed

Speed matters commercially: in Oldroyd, McElheran & Elkington's audit of
2,241 firms ("The Short Life of Online Sales Leads", HBR 2011) only 37%
answered a web lead within an hour; Davis answers in seconds.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, WebSocket

from ..config import DISPLAY_TZ_OFFSET_MINUTES
from ..database import get_db
from ..models import HouseRefreshRequestIn, InvoiceCreateIn, serialize_message
from ..ws_manager import manager
from . import catalogue, config, gate, llm, nlu, policy, prompts, store
from .persona import line as L


def _pct(values, p: float) -> Optional[int]:
    v = sorted(values)
    if not v:
        return None
    return int(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))])


class Metrics:
    def __init__(self) -> None:
        self.started = time.time()
        self.counters: dict[str, int] = defaultdict(int)
        self.skips: dict[str, int] = defaultdict(int)
        self.pipeline_ms: deque = deque(maxlen=500)      # hook → actions done (no debounce)
        self.llm_ms: deque = deque(maxlen=500)
        self.speed_to_lead_s: deque = deque(maxlen=500)  # guest's message time → Davis's reply queued

    def snapshot(self) -> dict:
        def lat(d):
            return {"p50": _pct(d, 50), "p95": _pct(d, 95), "p99": _pct(d, 99), "n": len(d)}
        return {"uptime_s": int(time.time() - self.started), "counters": dict(self.counters),
                "skips": dict(self.skips), "pipeline_ms": lat(self.pipeline_ms), "llm_ms": lat(self.llm_ms),
                "speed_to_lead_s": lat(self.speed_to_lead_s)}


class _Cat:
    """The catalogue as policy.plan sees it, bound to one chat."""

    def __init__(self, conn, chat_id: str) -> None:
        self.conn, self.chat_id = conn, chat_id

    def find_options(self, slots: dict, exclude_sent: bool = True):
        rows, status = catalogue.find_options(self.conn, self.chat_id, slots, exclude_sent=exclude_sent)
        return catalogue.with_notes(self.conn, [catalogue.option_facts(r) for r in rows]), status

    def resolve_choice(self, feats, offers, slots, stage):
        return catalogue.resolve_choice(self.conn, feats, offers, slots, stage)


class Engine:
    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.sem: Optional[asyncio.Semaphore] = None
        self.locks: dict[str, asyncio.Lock] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()
        self.listeners: set[WebSocket] = set()
        self.router = llm.Router()
        self.metrics = Metrics()
        self._places: tuple[float, dict] = (0.0, {})
        self._sweeper: Optional[asyncio.Task] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.sem = asyncio.Semaphore(config.CONCURRENCY)
        conn = get_db()
        try:
            store.init(conn)
        finally:
            conn.close()
        self._sweeper = asyncio.create_task(self._sweep_loop())
        st = self.settings()
        if st["provider"] == "local":
            self.warm_local()
        print(f"[davis] started: {st}")

    def warm_local(self) -> None:
        """Load the local model and its persona prefix in the background, so
        the first guest doesn't wait for it."""
        from .local_llm import model
        if config.LOCAL_PRELOAD and model.state in ("not_loaded", "error"):
            self._spawn(asyncio.to_thread(model.warm, prompts.messages("GUEST: Hi"), prompts.SCHEMA))

    async def stop(self) -> None:
        for t in [self._sweeper, *self.timers.values(), *self.tasks]:
            if t and not t.done():
                t.cancel()
        self.timers.clear()
        self.loop = None

    @property
    def running(self) -> bool:
        return self.loop is not None

    def settings(self) -> dict:
        return store.get_settings()

    def _submit(self, fn, *args) -> None:
        """Run fn(*args) on the engine's loop, from any thread."""
        if not self.loop:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            fn(*args)
        else:
            self.loop.call_soon_threadsafe(fn, *args)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    # ── hooks ─────────────────────────────────────────────────────────────────
    def notify_inbound(self, chat_id: str) -> None:
        self._submit(self._schedule, chat_id, None)

    def notify_refresh_done(self, job_id: int) -> None:
        self._submit(lambda: self._spawn(self._refresh_done(job_id)))

    def _schedule(self, chat_id: str, delay: Optional[float]) -> None:
        old = self.timers.pop(chat_id, None)
        if old and not old.done():
            old.cancel()                     # the guest wrote again: restart the wait
        self.timers[chat_id] = self._spawn(self._debounced(chat_id, config.DEBOUNCE_SEC if delay is None else delay))

    async def _debounced(self, chat_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self.timers.pop(chat_id, None)
        try:
            await self.process(chat_id)
        except Exception as e:
            self.metrics.counters["errors"] += 1
            await self.emit(chat_id, "error", {"error": str(e)[:300]})
            print(f"[davis] {chat_id}: {e!r}")

    async def _refresh_done(self, job_id: int) -> None:
        catalogue.clear_cache()
        conn = get_db()
        try:
            ids = [r["chat_id"] for r in conn.execute(
                "SELECT chat_id FROM davis_sessions WHERE refresh_job_id=? AND stage='searching'", (job_id,))]
        finally:
            conn.close()
        for chat_id in ids:
            try:
                await self.process(chat_id, trigger="refresh_done")
            except Exception as e:
                await self.emit(chat_id, "error", {"error": str(e)[:300]})

    async def _sweep_loop(self) -> None:
        """Safety net: answers guests a hook missed (legacy import path,
        restart mid-debounce) and follows up searches whose live prices
        never arrived."""
        while True:
            try:
                await asyncio.sleep(config.SWEEP_SEC)
                st = self.settings()
                if st["mode"] == "off" or st["paused"]:
                    continue
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=config.FRESH_MINUTES)).isoformat()
                conn = get_db()
                try:
                    rows = conn.execute(
                        "SELECT m.chat_id, m.id FROM messages m JOIN ("
                        "  SELECT MAX(rowid) AS r FROM messages WHERE created_at >= ? "
                        "  AND (direction = 'in' OR kind = 'text') GROUP BY chat_id) x ON m.rowid = x.r "
                        "LEFT JOIN davis_sessions s ON s.chat_id = m.chat_id "
                        "WHERE m.direction = 'in' AND (s.last_inbound_id IS NULL OR s.last_inbound_id != m.id)",
                        (cutoff,)).fetchall()
                    stuck_before = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
                    stuck = [r["chat_id"] for r in conn.execute(
                        "SELECT chat_id FROM davis_sessions WHERE stage='searching' AND updated_at < ?",
                        (stuck_before,))]
                finally:
                    conn.close()
                for r in rows:
                    if r["chat_id"] not in self.timers:
                        self._schedule(r["chat_id"], 0)
                for chat_id in stuck:
                    await self.process(chat_id, trigger="refresh_timeout")
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[davis] sweep: {e!r}")

    # ── live feed ─────────────────────────────────────────────────────────────
    async def emit(self, chat_id: Optional[str], kind: str, detail: Any = None,
                   latency_ms: Optional[int] = None, log: bool = True) -> None:
        if log:
            conn = get_db()
            try:
                ev = store.log_event(conn, chat_id, kind, detail, latency_ms)
            finally:
                conn.close()
        else:
            ev = {"chat_id": chat_id, "ts": store.now_iso(), "kind": kind, "detail": detail,
                  "latency_ms": latency_ms}
        dead = []
        for ws in list(self.listeners):
            try:
                await ws.send_json({"type": "event", "data": ev})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.listeners.discard(ws)

    # ── the pipeline ──────────────────────────────────────────────────────────
    def _places_map(self, conn) -> dict:
        if time.monotonic() - self._places[0] > 60:
            self._places = (time.monotonic(), catalogue.places(conn))
        return self._places[1]

    @staticmethod
    def _today():
        return (datetime.now(timezone.utc) + timedelta(minutes=DISPLAY_TZ_OFFSET_MINUTES)).date()

    def _context(self, conn, chat_id: str, settings: dict) -> Optional[dict]:
        chat = conn.execute("SELECT id, name FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            return None
        rows = conn.execute(
            "SELECT id, direction, sender, kind, text, created_at, wa_status, media_url FROM messages "
            "WHERE chat_id=? ORDER BY rowid DESC LIMIT ?",               # arrival order: see gate.speaking
            (chat_id, max(60, config.HISTORY_MESSAGES * 2))).fetchall()
        return {"chat_id": chat_id, "chat_name": chat["name"], "messages": [dict(r) for r in reversed(rows)],
                "session": store.get_session(conn, chat_id), "settings": settings,
                "now": datetime.now(timezone.utc)}

    @staticmethod
    def _guest_turn(messages: list[dict], resume: bool) -> list[dict]:
        if not resume:
            return gate.turn(messages)
        i = gate.latest_inbound_index(messages)
        if i is None:
            return []
        out = []
        for m in reversed(messages[:i + 1]):
            if not gate.speaking(m):
                continue
            if m.get("direction") != "in":
                break
            out.append(m)
        return list(reversed(out))

    async def process(self, chat_id: str, trigger: str = "message", force: bool = False) -> dict:
        """Handle one chat now. trigger: message | refresh_done | refresh_timeout.
        force=True (dashboard "run now") ignores mode off, never sends in shadow."""
        if not self.sem:
            raise RuntimeError("Davis engine not started")
        async with self.sem:
            lock = self.locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                return await self._process_locked(chat_id, trigger, force)

    async def _process_locked(self, chat_id: str, trigger: str, force: bool) -> dict:
        t0 = time.monotonic()
        resume = trigger in ("refresh_done", "refresh_timeout")
        conn = get_db()
        try:
            settings = store.get_settings(conn)
            if force and settings["mode"] == "off":
                settings = {**settings, "mode": "shadow"}
            ctx = self._context(conn, chat_id, settings)
            if not ctx:
                return {"status": "skipped", "reason": "no_such_chat"}
            msgs, session = ctx["messages"], ctx["session"]
            today, places = self._today(), self._places_map(conn)
            guest_turn = self._guest_turn(msgs, resume)
            texts = [m["text"] for m in guest_turn if m.get("kind", "text") == "text" and m.get("text")]
            feats = policy.turn_features(texts, today, places,
                                         has_image=any(m.get("kind") == "image" for m in guest_turn))
            if not session.get("slots"):
                turn_ids = {m["id"] for m in guest_turn}
                history = [m["text"] for m in msgs if m.get("direction") == "in" and m["id"] not in turn_ids
                           and m.get("kind", "text") == "text" and m.get("text")]
                session["slots"] = policy.seed_slots(history, today, places)

            decision = gate.decide(ctx, feats, resume=resume)
            latest_in = msgs[gate.latest_inbound_index(msgs)] if gate.latest_inbound_index(msgs) is not None else None
            if not decision["respond"]:
                self.metrics.skips[decision["reason"]] += 1
                if decision["reason"] not in ("already_handled", "last_message_not_from_guest", "mode_off"):
                    await self.emit(chat_id, "skip", {"reason": decision["reason"], "trigger": trigger,
                                                      "text": (texts[-1] if texts else "")[:160]})
                return {"status": "skipped", "reason": decision["reason"]}
            if not resume and not store.claim_inbound(conn, chat_id, latest_in["id"]):
                self.metrics.skips["already_handled"] += 1
                return {"status": "skipped", "reason": "already_handled"}
            self.metrics.counters["turns"] += 1
            await self.emit(chat_id, "inbound", {
                "trigger": trigger, "text": " / ".join(texts)[:300],
                "features": {k: v for k, v in feats.items() if v and k not in ("texts", "words", "empty")}}, log=True)

            ctx["offers"] = catalogue.offered(conn, chat_id)
            ctx["session"] = session
            cat = _Cat(conn, chat_id)
            plan_ = policy.plan(ctx, feats, cat, trigger="refresh_done" if resume else "message")
            if trigger == "refresh_timeout" and not any(a["type"] == "send_options" for a in plan_["actions"]):
                plan_["actions"] = [{"type": "handoff", "reason": "live prices did not arrive"},
                                    {"type": "reply", "text": L("prices_late")}]
                plan_["stage"] = "qualify"
            plan_ = await self._phrase(ctx, feats, plan_, cat, settings)
            plan_["actions"] = self._cap(plan_["actions"], msgs)
            plan_["inbound_id"] = latest_in["id"] if latest_in else None
            plan_["guest_text"] = " / ".join(texts)[:300]

            if settings["mode"] == "live" and not force:
                done = await self.execute(conn, chat_id, plan_, session)
                status = "sent"
                if latest_in and done.get("replied") and not resume:
                    ts = gate.parse_ts(latest_in.get("created_at"))
                    if ts:
                        self.metrics.speed_to_lead_s.append(int((datetime.now(timezone.utc) - ts).total_seconds()))
            else:
                store.update_session(conn, chat_id, slots=plan_["slots"])
                draft_id = store.add_draft(conn, chat_id, plan_["inbound_id"], plan_)
                done = {"draft_id": draft_id}
                status = "drafted"
                self.metrics.counters["drafts"] += 1
            ms = int((time.monotonic() - t0) * 1000)
            self.metrics.pipeline_ms.append(ms)
            await self.emit(chat_id, "plan", {
                "status": status, "stage": plan_["stage"], "intent": plan_.get("intent"),
                "actions": [_brief(a) for a in plan_["actions"]], "llm": plan_.get("llm"), **done}, latency_ms=ms)
            return {"status": status, "plan": plan_, **done}
        finally:
            conn.close()

    async def _phrase(self, ctx: dict, feats: dict, plan_: dict, cat: _Cat, settings: dict) -> dict:
        """Let the model rewrite the persona's template reply in Davis's voice.
        Its text is used only if it passes policy.check_reply; otherwise the
        template goes out unchanged."""
        reply = next((a for a in plan_["actions"] if a["type"] == "reply"), None)
        open_q = plan_.get("intent") == "open_question"
        use_model = reply is not None and settings.get("provider", "rules") != "rules" and (
            settings.get("llm_for", "open") == "all" or open_q)
        if open_q and not plan_.get("note_terms"):
            use_model = False          # no team note covers the question: the team answers (no model wait)
        draft = reply["text"] if reply else None
        model_handoff = None
        if use_model:
            user = prompts.user_turn(ctx["chat_name"], ctx["messages"], plan_, self._today().isoformat())
            out, info = await self.router.complete(settings, user)
            plan_["llm"] = {k: info.get(k) for k in ("provider", "model", "latency_ms", "cache_hit", "error",
                                                       "skipped", "prompt_tokens", "output_tokens",
                                                       "tokens_per_s", "queued_s") if info.get(k) is not None}
            if info.get("latency_ms") and not info.get("cache_hit"):
                self.metrics.llm_ms.append(info["latency_ms"])
            if out:
                text = nlu.clip_words(str(out.get("reply") or "").strip(), config.MAX_WORDS)
                problem = policy.check_reply(text, plan_["facts"], draft, keep_draft=not open_q)
                if not problem and open_q and not out.get("handoff") and not any(
                        t in text.lower() for t in plan_.get("note_terms") or []):
                    problem = "answer_not_grounded_in_note"
                if problem:
                    self.router.stats["rejected"] += 1
                    plan_["llm"]["rejected"] = problem
                else:
                    reply["text"] = text
                    reply["by"] = "model"
                model_handoff = {"wants": bool(out.get("handoff")), "reason": str(out.get("handoff_reason") or "")[:120],
                                 "answered": reply.get("by") == "model"}
        return _resolve_maybe(plan_, model_handoff)

    @staticmethod
    def _cap(actions: list[dict], msgs: list[dict]) -> list[dict]:
        left = max(0, config.MAX_REPLIES_PER_TURN - gate.davis_replies_this_turn(msgs))
        out = []
        for a in actions:
            if a["type"] == "reply":
                if left <= 0 or not a.get("text", "").strip():
                    continue
                left -= 1
            out.append(a)
        return out

    # ── the hands: actions on the outbox ──────────────────────────────────────
    async def execute(self, conn, chat_id: str, plan_: dict, session: dict) -> dict:
        from ..routers import house_refresh, houses, invoices, messages as messages_router
        new_ids: list[str] = []
        patch: dict = {"stage": plan_["stage"], "slots": plan_["slots"], "choice": plan_.get("choice")}
        result: dict = {"replied": False}
        t = datetime.now(timezone.utc)
        order = {"reply": 0, "send_options": 1, "create_invoice": 2, "send_terms": 3}
        for a in sorted(plan_["actions"], key=lambda a: order.get(a["type"], 9)):   # the reply leads
            kind = a["type"]
            try:
                if kind == "reply":
                    mid = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO messages (id, chat_id, sender, kind, text, poll_id, created_at, direction, "
                        "wa_status) VALUES (?, ?, ?, 'text', ?, NULL, ?, 'out', 'pending')",
                        (mid, chat_id, config.SENDER, nlu.clip_words(a["text"], config.MAX_WORDS), t.isoformat()))
                    conn.commit()
                    new_ids.append(mid)
                    t += timedelta(milliseconds=50)
                    result["replied"] = True
                    patch["replies"] = (session.get("replies") or 0) + 1
                    patch["last_reply_at"] = store.now_iso()
                    self.metrics.counters["replies"] += 1
                elif kind == "send_options":
                    opts = a["options"]
                    ids = houses.queue_listings(conn, chat_id, [o["url"] for o in opts],
                                                [o.get("offer_id") for o in opts], sender=config.SENDER_CATALOGUE,
                                                max_images=config.IMAGES_PER_OPTION, builder=catalogue.compact_card,
                                                start=t)
                    new_ids += ids
                    t += timedelta(seconds=len(opts) + 1)
                    self.metrics.counters["options_sent"] += len(opts)
                elif kind == "send_terms":
                    row, created = messages_router.queue_terms(conn, chat_id, sender=config.SENDER_DOCUMENTS)
                    if created:
                        new_ids.append(row["id"])
                    patch["terms_sent_at"] = store.now_iso()
                    self.metrics.counters["terms_sent"] += 1
                elif kind == "create_invoice":
                    inv = invoices.create_invoice(InvoiceCreateIn(**a["invoice"]))
                    patch["invoice_id"], patch["invoice_key"] = inv["id"], a["key"]
                    result["invoice_id"] = inv["id"]
                    self.metrics.counters["invoices"] += 1
                elif kind == "request_refresh":
                    job = house_refresh.request_refresh(HouseRefreshRequestIn(
                        location=a["city"], check_in=a["check_in"], check_out=a["check_out"]))
                    patch["refresh_job_id"] = job["id"]
                    result["refresh_job_id"] = job["id"]
                    self.metrics.counters["refresh_requests"] += 1
                elif kind == "handoff":
                    patch["handoff"], patch["handoff_reason"] = True, a.get("reason") or "handoff"
                    self.metrics.counters["handoffs"] += 1
                elif kind == "opt_out":
                    patch["opted_out"] = True
                elif kind == "opt_in":
                    patch["opted_out"] = False
            except FileNotFoundError:
                await self.emit(chat_id, "error", {"action": kind, "error": "Terms PDF missing on the server"})
            except HTTPException as e:
                await self.emit(chat_id, "error", {"action": kind, "error": str(e.detail)[:300]})
            except Exception as e:
                await self.emit(chat_id, "error", {"action": kind, "error": repr(e)[:300]})
        if plan_["stage"] != "searching":
            patch.setdefault("refresh_job_id", None)
        store.update_session(conn, chat_id, **patch)
        for mid in new_ids:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if row:
                await manager.broadcast(chat_id, {"type": "message", "data": serialize_message(conn, row)})
        result["queued_messages"] = len(new_ids)
        return result

    # ── dashboard operations ──────────────────────────────────────────────────
    async def approve(self, draft_id: int, text: Optional[str] = None, force: bool = False) -> dict:
        conn = get_db()
        try:
            d = store.draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            if d["status"] != "pending":
                raise HTTPException(409, f"draft is {d['status']}")
            latest = conn.execute(
                "SELECT id FROM messages WHERE chat_id=? AND direction='in' ORDER BY rowid DESC LIMIT 1",
                (d["chat_id"],)).fetchone()
            if latest and d["inbound_id"] and latest["id"] != d["inbound_id"] and not force:
                raise HTTPException(409, "the guest wrote again since this draft; approve with force=true "
                                         "or let Davis draft a new reply")
            plan_ = d["plan"]
            if text is not None:
                text = nlu.clip_words(text.strip(), config.MAX_WORDS)
                for a in plan_["actions"]:
                    if a["type"] == "reply":
                        a["text"], a["by"] = text, "staff"
                        break
                else:
                    if text:
                        plan_["actions"].insert(0, {"type": "reply", "text": text, "by": "staff"})
            if not store.decide_draft(conn, draft_id, "sent"):
                raise HTTPException(409, "draft already decided")
            session = store.get_session(conn, d["chat_id"])
            done = await self.execute(conn, d["chat_id"], plan_, session)
        finally:
            conn.close()
        await self.emit(d["chat_id"], "draft_sent", {"draft_id": draft_id, **done})
        return {"ok": True, **done}


def _brief(a: dict) -> dict:
    if a["type"] == "reply":
        return {"type": "reply", "text": a.get("text"), "by": a.get("by", "template")}
    if a["type"] == "send_options":
        return {"type": "send_options", "refs": [o.get("ref") for o in a["options"]]}
    if a["type"] == "create_invoice":
        inv = a["invoice"]
        return {"type": "create_invoice", "property": inv["property_name"], "total_usd": inv.get("rate"),
                "guest": inv["guest_name"]}
    return {k: v for k, v in a.items() if k != "options"}


def _resolve_maybe(plan_: dict, model: Optional[dict]) -> dict:
    """maybe_handoff marks a question the rules can't answer. It is dropped
    when the model answered it (reply accepted, no handoff asked); otherwise
    the chat goes to staff. The model's handoff flag counts only there: a
    small model's opinion never takes a chat away from Davis by itself
    (complaints and requests for a person are caught by the rules)."""
    acts = []
    for a in plan_["actions"]:
        if a["type"] == "maybe_handoff":
            if model is None or model["wants"] or not model["answered"]:
                acts.append({"type": "handoff", "reason": (model and model["reason"]) or a["reason"]})
            continue
        acts.append(a)
    plan_["actions"] = acts
    return plan_


engine = Engine()
