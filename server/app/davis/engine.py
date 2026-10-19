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
from . import catalogue, config, gate, llm, mind, nlu, policy, prompts, skills, store


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

    def skill_answer(self, text: str):
        return skills.match(self.conn, text)


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
        self._sweeps = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.sem = asyncio.Semaphore(config.CONCURRENCY)
        conn = get_db()
        try:
            store.init(conn)
            learned = skills.learn(conn)            # the team's past answers become skills
            if learned:
                print(f"[davis] learned {learned} skill(s) from the team's replies")
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
            self._spawn(asyncio.to_thread(model.warm, mind.understand_messages([], {}, [], self._today()),
                                          mind.STATE_SCHEMA))

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
        self._submit(self._ensure_sweeper)

    def _ensure_sweeper(self) -> None:
        """Watchdog: the safety-net loop must always run; restart it if it died."""
        if self.loop and (self._sweeper is None or self._sweeper.done()):
            self._sweeper = asyncio.create_task(self._sweep_loop())
            self.metrics.counters["sweeper_restarts"] += 1

    def notify_refresh_done(self, job_id: int, early: bool = False) -> None:
        self._submit(lambda: self._spawn(self._refresh_done(job_id, early)))

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

    async def _refresh_done(self, job_id: int, early: bool = False) -> None:
        """Guests waiting on this request get their homes. early: the scraper
        has priced the first EARLY_PRESENT homes and is still going - a guest
        is answered now only if those homes suit them (otherwise the final
        result decides, so nobody hears "nothing free" too soon)."""
        catalogue.clear_cache()
        conn = get_db()
        try:
            ids = []
            for r in conn.execute("SELECT chat_id FROM davis_sessions WHERE refresh_job_id=? AND stage='searching'",
                                  (job_id,)).fetchall():
                if early:
                    slots = store.get_session(conn, r["chat_id"]).get("slots") or {}
                    if catalogue.find_options(conn, r["chat_id"], slots)[1] != "ok":
                        continue
                ids.append(r["chat_id"])
        finally:
            conn.close()
        for chat_id in ids:
            try:
                await self.process(chat_id, trigger="refresh_done")
            except Exception as e:
                await self.emit(chat_id, "error", {"error": str(e)[:300]})

    async def _sweep_loop(self) -> None:
        """Safety net: answers guests a hook missed (legacy import path,
        restart mid-debounce), follows up searches whose live homes never
        arrived, and sends the one nudge to guests who went quiet."""
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
                    stuck_before = (datetime.now(timezone.utc)
                                    - timedelta(minutes=config.REFRESH_TIMEOUT_MINUTES)).isoformat()
                    stuck = [r["chat_id"] for r in conn.execute(
                        "SELECT chat_id FROM davis_sessions WHERE stage='searching' AND updated_at < ?",
                        (stuck_before,))]
                    quiet = self._quiet_guests(conn)
                finally:
                    conn.close()
                for r in rows:
                    if r["chat_id"] not in self.timers:
                        self._schedule(r["chat_id"], 0)
                for chat_id in stuck:
                    await self.process(chat_id, trigger="refresh_timeout")
                for chat_id in quiet:
                    if chat_id not in self.timers:
                        await self.process(chat_id, trigger="followup")
                self._sweeps += 1
                if self._sweeps % max(1, 600 // config.SWEEP_SEC) == 0:   # every ~10 minutes
                    conn = get_db()
                    try:
                        if skills.learn(conn):
                            await self.emit(None, "skills", {"learned": True})
                    finally:
                        conn.close()
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[davis] sweep: {e!r}")

    @staticmethod
    def _quiet_guests(conn) -> list[str]:
        """Chats mid-booking where Davis spoke last FOLLOWUP_HOURS ago and the
        guest hasn't answered: each gets at most FOLLOWUP_MAX nudges, and only
        while the guest's last message is under FOLLOWUP_WINDOW_HOURS old."""
        if config.FOLLOWUP_HOURS <= 0 or config.FOLLOWUP_MAX <= 0:
            return []
        now = datetime.now(timezone.utc)
        quiet_before = (now - timedelta(hours=config.FOLLOWUP_HOURS)).isoformat()
        window = (now - timedelta(hours=config.FOLLOWUP_WINDOW_HOURS)).isoformat()
        rows = conn.execute(
            "SELECT s.chat_id FROM davis_sessions s "
            "WHERE s.stage IN ('qualify','present','details','invoice') AND s.followups < ? "
            "AND s.opted_out=0 AND s.handoff=0 AND s.paused=0 AND s.last_reply_at IS NOT NULL "
            "AND s.last_reply_at < ? "
            "AND (SELECT MAX(created_at) FROM messages m WHERE m.chat_id=s.chat_id AND m.direction='in') >= ? "
            "AND (SELECT direction FROM messages m WHERE m.chat_id=s.chat_id "
            "     AND (m.direction='in' OR m.kind='text') ORDER BY m.rowid DESC LIMIT 1) = 'out'",
            (config.FOLLOWUP_MAX, quiet_before, window)).fetchall()
        return [r["chat_id"] for r in rows]

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
        total = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
        return {"chat_id": chat_id, "chat_name": chat["name"], "messages": [dict(r) for r in reversed(rows)],
                "msg_offset": max(0, total - len(rows)),          # older messages not loaded (mind.window_start)
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
        """Handle one chat now. trigger: message | refresh_done | refresh_timeout | followup.
        force=True (dashboard "run now") ignores mode off, never sends in shadow."""
        if not self.sem:
            raise RuntimeError("Davis engine not started")
        async with self.sem:
            lock = self.locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                return await self._process_locked(chat_id, trigger, force)

    async def _process_locked(self, chat_id: str, trigger: str, force: bool) -> dict:
        """Runs one pass; if it fails after claiming the guest's message, the
        claim is released so the sweeper retries it (a guest never ends up
        unanswered because of an error)."""
        claim: dict = {}
        try:
            return await self._process_body(chat_id, trigger, force, claim)
        except Exception:
            if claim.get("claimed"):
                conn = get_db()
                try:
                    store.update_session(conn, chat_id, last_inbound_id=claim.get("prev"))
                finally:
                    conn.close()
                self.metrics.counters["retries_queued"] += 1
            raise

    async def _process_body(self, chat_id: str, trigger: str, force: bool, claim: dict) -> dict:
        t0 = time.monotonic()
        resume = trigger in ("refresh_done", "refresh_timeout", "followup")
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
            if trigger == "followup":
                last = next((m for m in reversed(msgs) if gate.speaking(m)), None)
                if not last or last.get("direction") == "in":
                    return {"status": "skipped", "reason": "guest_wrote"}     # the normal path answers it
            guest_turn = [] if trigger == "followup" else self._guest_turn(msgs, resume)
            texts = [m["text"] for m in guest_turn if m.get("kind", "text") == "text" and m.get("text")]
            recent = [m for m in msgs[-14:] if m.get("kind", "text") == "text" and m.get("text")
                      and not gate.is_davis(m.get("sender"))]
            context = [m["text"] for m in recent]
            feats = policy.turn_features(texts, today, places,
                                         has_image=any(m.get("kind") == "image" for m in guest_turn), context=context)
            if not session.get("slots"):
                turn_ids = {m["id"] for m in guest_turn}
                history = [m["text"] for m in msgs if m.get("direction") == "in" and m["id"] not in turn_ids
                           and m.get("kind", "text") == "text" and m.get("text")]
                session["slots"] = policy.seed_slots(history, today, places)
            # Dates the team already agreed in the chat ("let me use 21 to 22") count too.
            # Not the dates printed on house cards: those say when a house is free.
            month = nlu.last_month(context)
            for m in recent:
                if m.get("direction") == "out" and not _is_card(m.get("text")):
                    f = nlu.extract(m["text"], today, places, month)
                    if f.get("check_in") and f.get("check_out") and not feats.get("check_in"):
                        session["slots"] = nlu.merge_slots(session.get("slots") or {},
                                                           {"check_in": f["check_in"], "check_out": f["check_out"]})

            decision = gate.decide(ctx, feats, resume=resume)
            latest_in = msgs[gate.latest_inbound_index(msgs)] if gate.latest_inbound_index(msgs) is not None else None
            if not decision["respond"]:
                self.metrics.skips[decision["reason"]] += 1
                if decision["reason"] not in ("already_handled", "last_message_not_from_guest", "mode_off"):
                    await self.emit(chat_id, "skip", {"reason": decision["reason"], "trigger": trigger,
                                                      "text": (texts[-1] if texts else "")[:160]})
                return {"status": "skipped", "reason": decision["reason"]}
            prev_inbound = session.get("last_inbound_id")
            if not resume and not store.claim_inbound(conn, chat_id, latest_in["id"]):
                self.metrics.skips["already_handled"] += 1
                return {"status": "skipped", "reason": "already_handled"}
            if not resume:
                claim.update(claimed=True, prev=prev_inbound)
            self.metrics.counters["turns"] += 1
            await self.emit(chat_id, "inbound", {
                "trigger": trigger, "text": " / ".join(texts)[:300],
                "features": {k: v for k, v in feats.items() if v and k not in ("texts", "words", "empty")}}, log=True)

            ctx["offers"] = catalogue.offered(conn, chat_id)
            # Guest messages no pass answered (a staff reply came first) still
            # count: "4november" sent before the team's reply is not lost.
            missed = self._missed_guest_texts(msgs, prev_inbound, {m["id"] for m in guest_turn})
            for t in missed:
                f = nlu.extract(t, today, places, month)
                session["slots"] = nlu.merge_slots(session.get("slots") or {},
                                                   {k: v for k, v in f.items() if k in nlu.SLOT_KEYS})
            ctx["session"] = session
            ctx["slots_before"] = dict(session.get("slots") or {})       # the shared prompt prefix (mind.py)
            ctx["turn_texts"] = texts
            ctx["month"] = month
            if settings.get("provider") in ("local", "ollama") and config.KARL_UNDERSTAND != "off":
                why = "always" if config.KARL_UNDERSTAND == "always" else mind.needs_model(
                    texts, feats, session.get("stage") or "new")
                if why:
                    feats = await self._understand(ctx, feats, today, places, settings, why)
                else:
                    self.metrics.counters["understood_by_rules"] += 1
            cat = _Cat(conn, chat_id)
            plan_ = policy.plan(ctx, feats, cat, trigger=trigger)
            plan_["trigger"] = trigger
            plan_ = await self._phrase(ctx, feats, plan_, cat, settings)
            plan_["actions"] = self._cap(plan_["actions"], msgs, plan_.get("intent") or "")
            plan_["inbound_id"] = latest_in["id"] if latest_in else None
            plan_["guest_text"] = " / ".join(texts)[:300]

            tries = session.get("ai_retries") or 0
            if settings["mode"] == "live" and not force and plan_.get("ai_failed") and tries < config.AI_RETRIES \
                    and trigger == "message":
                # The model couldn't write a safe reply (busy, cut off, guard):
                # nothing is sent yet; this turn runs again shortly. No person
                # is asked to step in.
                store.update_session(conn, chat_id, ai_retries=tries + 1, last_inbound_id=prev_inbound)
                claim.clear()
                self._schedule(chat_id, config.AI_RETRY_SEC)
                self.metrics.counters["ai_retries"] += 1
                await self.emit(chat_id, "retry", {"reason": plan_["ai_failed"], "try": tries + 1,
                                                   "in_s": config.AI_RETRY_SEC})
                return {"status": "retry", "reason": plan_["ai_failed"]}
            if settings["mode"] == "live" and not force:
                if plan_.get("ai_failed"):
                    # Still no safe AI reply after the retries: the plan's own
                    # wording goes out, so the guest is never left waiting.
                    for a in plan_["actions"]:
                        if a["type"] == "reply":
                            a["by"] = "plan"
                    self.metrics.counters["ai_fallbacks"] += 1
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

    @staticmethod
    def _missed_guest_texts(msgs: list[dict], last_id: Optional[str], turn_ids: set) -> list[str]:
        if not last_id:
            return []
        i = next((k for k, m in enumerate(msgs) if m.get("id") == last_id), None)
        if i is None:
            return []
        return [m["text"] for m in msgs[i + 1:] if m.get("direction") == "in" and m["id"] not in turn_ids
                and m.get("kind", "text") == "text" and m.get("text")][-8:]

    async def _understand(self, ctx: dict, feats: dict, today, places: dict, settings: dict, why: str = "") -> dict:
        """Karl reads the whole recent conversation into the booking state
        (mind.understand_messages); the checked result is merged over the
        rules' reading of the newest messages. If the model can't answer,
        the rules' reading stands on its own."""
        msgs = mind.understand_messages(ctx["messages"], ctx["slots_before"], ctx["offers"], today,
                                        DISPLAY_TZ_OFFSET_MINUTES, ctx.get("msg_offset", 0))
        out, info = await self.router.complete(settings, None, msgs=msgs, schema=mind.UNDERSTAND_SCHEMA,
                                               max_tokens=config.KARL_STATE_MAX_TOKENS)
        if info.get("latency_ms") and not info.get("cache_hit"):
            self.metrics.llm_ms.append(info["latency_ms"])
        if not out:
            self.metrics.counters["understand_failed"] += 1
            await self.emit(ctx["chat_id"], "understand", {"error": info.get("error") or info.get("skipped")})
            return feats
        guest_texts = [m["text"] for m in ctx["messages"][-config.KARL_TRANSCRIPT:]
                       if m.get("direction") == "in" and m.get("kind", "text") == "text" and m.get("text")]
        mf = mind.to_features(out, today, places, ctx["offers"], ctx.get("month"), guest_texts,
                              newest=ctx.get("turn_texts"))
        self.metrics.counters["understood"] += 1
        await self.emit(ctx["chat_id"], "understand", {
            "need": mf.get("latest_need"), "questions": mf.get("open_questions"),
            "state": {k: mf[k] for k in ("city", "check_in", "check_out", "guests", "budget_usd", "option_ref",
                                         "full_name", "long_term", "photos_for") if mf.get(k)},
            "why": why, "latency_ms": info.get("latency_ms")})
        return mind.merge(feats, mf)

    async def _phrase(self, ctx: dict, feats: dict, plan_: dict, cat: _Cat, settings: dict) -> dict:
        """The model writes the reply. In "ai" mode (the default) it writes every
        reply itself from the conversation and the plan's key points; a reply
        that fails policy.check_reply is retried once with the reason, and if it
        still fails plan_["ai_failed"] is set: the engine runs the turn again
        after AI_RETRY_SEC, and after AI_RETRIES sends the plan's own wording.
        In "all"/"open" modes the persona's line is the fallback."""
        reply = next((a for a in plan_["actions"] if a["type"] == "reply"), None)
        open_q = plan_.get("intent") == "open_question"
        mode = settings.get("llm_for", config.LLM_FOR)
        ai_only = mode == "ai"
        use_model = reply is not None and settings.get("provider", "rules") != "rules" and (
            mode in ("all", "ai") or open_q)
        if open_q and not plan_.get("note_terms") and not ai_only:
            use_model = False          # no team note covers the question: the team answers (no model wait)
        draft = reply["text"] if reply else None
        feedback = ""
        karl = settings.get("provider") in ("local", "ollama")      # the worker keeps the old short prompt
        limit = config.KARL_MAX_WORDS if karl else config.MAX_WORDS
        for attempt in range(2 if (use_model and ai_only) else (1 if use_model else 0)):
            if karl:
                msgs = mind.reply_messages(ctx["chat_name"], ctx["messages"], plan_, feats, self._today(), limit,
                                           feedback, DISPLAY_TZ_OFFSET_MINUTES,
                                           slots_before=ctx.get("slots_before"), offers=ctx.get("offers"),
                                           offset=ctx.get("msg_offset", 0))
                out, info = await self.router.complete(settings, None, msgs=msgs, schema=mind.REPLY_SCHEMA,
                                                       max_tokens=config.KARL_REPLY_MAX_TOKENS)
            else:
                user = prompts.user_turn(ctx["chat_name"], ctx["messages"], plan_, self._today().isoformat(), feedback)
                out, info = await self.router.complete(settings, user)
            plan_["llm"] = {k: info.get(k) for k in ("provider", "model", "latency_ms", "cache_hit", "error",
                                                       "skipped", "prompt_tokens", "output_tokens",
                                                       "tokens_per_s", "queued_s") if info.get(k) is not None}
            if info.get("latency_ms") and not info.get("cache_hit"):
                self.metrics.llm_ms.append(info["latency_ms"])
            if out:
                text = nlu.clip_words(str(out.get("reply") or "").strip(), limit)
                problem = policy.check_reply(text, plan_["facts"], draft, keep_draft=not open_q and not ai_only,
                                             keep_numbers=ai_only and not open_q, max_words=limit)
                if not problem and _repeats(text, ctx["messages"]):
                    problem = "repeats_an_earlier_message"
                if not problem and ai_only:
                    missed = [m for m in prompts.must_include(draft) if not prompts.covers(text, m)]
                    if missed:
                        problem = "dropped:" + missed[0]
                if not problem and open_q and plan_.get("note_terms") and not any(
                        t in text.lower() for t in plan_.get("note_terms") or []):
                    problem = "answer_not_grounded_in_note"
                if problem:
                    self.router.stats["rejected"] += 1
                    reply["max_words"] = limit
                    plan_["llm"]["rejected"] = problem
                    feedback = problem
                else:
                    reply["text"] = text
                    reply["by"] = "model"
                    reply["max_words"] = limit
                    plan_["llm"].pop("rejected", None)
                if reply.get("by") == "model":
                    break
            elif "ran out of tokens" in str(info.get("error") or ""):
                feedback = f"too long: at most {limit} words"   # a shorter try can work
            else:
                break                                   # model down / skipped: no point retrying now
        if ai_only and use_model and reply is not None and reply.get("by") != "model":
            llm = plan_.get("llm") or {}
            plan_["ai_failed"] = llm.get("rejected") or llm.get("error") or llm.get("skipped") or "model unavailable"
        return plan_

    @staticmethod
    def _cap(actions: list[dict], msgs: list[dict], intent: str = "") -> list[dict]:
        """At most MAX_REPLIES_PER_TURN replies per guest turn, and never the
        same booking question twice in a row (the chats showed the same
        greeting twice in a minute and "How many nights?" four times): a
        repeated ask becomes "Sorry, I didn't catch that" + the ask, and is
        dropped if even that was said already."""
        left = max(0, config.MAX_REPLIES_PER_TURN - gate.davis_replies_this_turn(msgs))
        out = []
        for a in actions:
            if a["type"] == "reply":
                if a.get("text") and not any(x["type"] == "send_options" for x in actions):
                    a["text"] = _dedupe(a["text"], msgs, intent) or ""
                if left <= 0 or not a.get("text", "").strip():
                    continue
                left -= 1
            out.append(a)
        return out

    # ── the hands: actions on the outbox ──────────────────────────────────────
    async def execute(self, conn, chat_id: str, plan_: dict, session: dict) -> dict:
        from ..routers import house_refresh, houses, invoices, messages as messages_router
        new_ids: list[str] = []
        patch: dict = {"stage": plan_["stage"], "slots": plan_["slots"], "choice": plan_.get("choice"),
                       "ai_retries": 0}
        if plan_.get("trigger") == "followup":
            patch["followups"] = (session.get("followups") or 0) + 1
            patch["last_followup_at"] = store.now_iso()
            self.metrics.counters["followups"] += 1
        elif plan_.get("trigger", "message") == "message":
            patch["followups"] = 0                   # the guest wrote: a new silence may get its nudge
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
                        (mid, chat_id, config.SENDER, nlu.clip_words(a["text"], a.get("max_words") or config.MAX_WORDS),
                         t.isoformat()))
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
                                                max_images=a.get("max_images") or config.IMAGES_PER_OPTION,
                                                builder=catalogue.compact_card,
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
                    patch["refresh_tries"] = (session.get("refresh_tries") or 0) + 1 if a.get("retry") else 1
                    result["refresh_job_id"] = job["id"]
                    self.metrics.counters["refresh_requests"] += 1
                elif kind == "skill_used":
                    skills.used(conn, a["id"])
                    self.metrics.counters["skills_used"] += 1
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
            patch.setdefault("refresh_tries", 0)
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


def _norm(text: Optional[str]) -> str:
    return " ".join("".join(ch for ch in (text or "").lower() if ch.isalnum() or ch.isspace()).split())


def _repeats(text: str, msgs: list[dict], last: int = 6) -> bool:
    """The same words as one of Karl's last few text replies in this chat."""
    mine = [m for m in msgs if m.get("direction") == "out" and gate.is_reply_sender(m.get("sender"))
            and m.get("kind", "text") == "text"][-last:]
    n = _norm(text)
    return bool(n) and any(_norm(m.get("text")) == n for m in mine)


# Booking asks that can come out word for word again; answers to complaints
# and open questions may repeat (the guest asked again).
_REPEAT_SENSITIVE = {"qualify", "present", "searching", "collect_details", "followup", "invoice", "price", "faq",
                     "payment_info", "location", "photos"}


def _dedupe(text: str, msgs: list[dict], intent: str) -> Optional[str]:
    if intent not in _REPEAT_SENSITIVE or not _repeats(text, msgs):
        return text
    import re
    from .persona import line as L
    t = re.sub(r"^Hello[^!]{0,40}! I'm Karl from [^🏡]*🏡\s*", "", text).strip()   # never greet twice
    if t == text.strip() or _repeats(t, msgs):
        t = L("not_caught") + t
    return None if _repeats(t, msgs) else t


def _is_card(text: Optional[str]) -> bool:
    """A house card (ours or the team's): its dates are when the house is free."""
    t = (text or "").lower()
    return "kcer" in t and ("📅" in (text or "") or "💵" in (text or "") or "free " in t)


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


engine = Engine()
