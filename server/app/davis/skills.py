"""davis/skills.py — answers Davis learns from the team's own replies.

When a guest asked something and a person on the team answered in the chat,
that question → answer pair becomes a skill. Later, when another guest asks
something similar that no FAQ or house note covers, Davis gives the team's
answer instead of "I'll check". Staff can add skills by hand, switch any
off, or delete them on the dashboard (Skills tab).

Privacy and safety: a learned answer is only kept if it is 3–40 words, has
no digits (no prices, dates, phone or ID numbers, KCER refs), no links, and
doesn't contain the other guest's name; it is reused only when the new
question shares at least two content words with the original.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

from . import config, nlu, store
from .gate import is_davis, parse_ts

_STOP = set("""a an the is are was were be been do does did can could will would shall should may might must
i we you he she it they me us my our your its their this that these those there here what which who whom
whose when where why how any some much many more most no not yes please pls plz hi hello hey ok okay thanks
thank kcer just also really very still even like want need get got have has had with for of in on at to
from by about into out up down over so then than too also and or but if because as is're i'm im u ur""".split())
_ANSWER_STARTERS_SKIP = ("ok", "okay", "noted", "sure", "yes", "no", "thanks", "thank", "hi", "hello")


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z']{2,}", (text or "").lower())
    return {w.rstrip("s") if len(w) > 4 else w for w in words if w not in _STOP}


def qkey(text: str) -> str:
    return " ".join(sorted(tokens(text)))


def _good_question(text: str) -> bool:
    f = nlu.extract(text)
    return f.get("question") and len(tokens(text)) >= 2 and not f.get("opt_out")


def _good_answer(text: str, guest_name: str) -> bool:
    t = (text or "").strip()
    n = nlu.word_count(t)
    if not 3 <= n <= config.MAX_WORDS or re.search(r"\d|https?://|www\.|@", t):
        return False
    if len(tokens(t)) < 2:
        return False
    low = t.lower()
    if low.split()[0].strip(",.!") in _ANSWER_STARTERS_SKIP and len(tokens(t)) < 3:
        return False
    for part in re.findall(r"[a-z]{3,}", (guest_name or "").lower()):
        if re.search(rf"\b{re.escape(part)}\b", low):
            return False                                   # never repeat another guest's name
    return ".pdf" not in low


def learn(conn, max_rows: int = 6000) -> int:
    """Scan recent chats for guest question → team answer pairs. Returns how
    many new skills were added. Never touches manual or disabled skills."""
    rows = conn.execute(
        "SELECT * FROM (SELECT m.rowid AS rid, m.chat_id, m.direction, m.sender, m.kind, m.text, m.created_at, "
        "c.name AS chat_name FROM messages m LEFT JOIN chats c ON c.id = m.chat_id ORDER BY m.rowid DESC LIMIT ?) "
        "ORDER BY chat_id, rid", (max_rows,)).fetchall()
    added, prev = 0, None
    for r in rows:
        r = dict(r)
        if r["direction"] != "in" and r.get("kind", "text") != "text":
            continue                                       # photos/PDFs coming back don't take a turn
        if prev and prev["chat_id"] == r["chat_id"] and prev["direction"] == "in" and r["direction"] == "out" \
                and not is_davis(r["sender"]) and (prev.get("kind") or "text") == "text":
            q, a = (prev["text"] or "").strip(), (r["text"] or "").strip()
            t0, t1 = parse_ts(prev["created_at"]), parse_ts(r["created_at"])
            in_time = not (t0 and t1) or timedelta(0) <= t1 - t0 <= timedelta(hours=12)
            if in_time and _good_question(q) and _good_answer(a, r.get("chat_name") or ""):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO davis_skills (qkey, question, answer, source, chat_id, created_at, "
                    "updated_at) VALUES (?, ?, ?, 'learned', ?, ?, ?)",
                    (qkey(q), q[:300], a[:400], r["chat_id"], store.now_iso(), store.now_iso()))
                added += cur.rowcount
        prev = r
    conn.commit()
    return added


def match(conn, text: str) -> Optional[dict]:
    """The enabled skill whose question best matches this one, if close enough."""
    qt = tokens(text)
    if len(qt) < 2:
        return None
    best, best_score = None, 0.0
    for r in conn.execute("SELECT * FROM davis_skills WHERE enabled=1"):
        st = tokens(r["question"])
        inter = len(qt & st)
        if inter < 2:
            continue
        jaccard = inter / len(qt | st)
        overlap = inter / min(len(qt), len(st))
        score = (jaccard + overlap) / 2
        if overlap >= 0.6 and jaccard >= 0.3 and score > best_score:
            best, best_score = dict(r), score
    if best:
        best["score"] = round(best_score, 2)
    return best


def used(conn, skill_id: int) -> None:
    conn.execute("UPDATE davis_skills SET uses = uses + 1, updated_at=? WHERE id=?", (store.now_iso(), skill_id))
    conn.commit()


def add(conn, question: str, answer: str) -> dict:
    key = qkey(question)
    if len(key.split()) < 2:
        raise ValueError("the question needs at least two meaningful words")
    conn.execute(
        "INSERT INTO davis_skills (qkey, question, answer, source, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, 'manual', 1, ?, ?) ON CONFLICT(qkey) DO UPDATE SET answer=excluded.answer, "
        "source='manual', enabled=1, updated_at=excluded.updated_at",
        (key, question.strip()[:300], nlu.clip_words(answer.strip(), config.MAX_WORDS)[:400],
         store.now_iso(), store.now_iso()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM davis_skills WHERE qkey=?", (key,)).fetchone())


def listing(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM davis_skills ORDER BY enabled DESC, uses DESC, source = 'manual' DESC, id DESC")]
