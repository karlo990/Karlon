"""Crumb on WhatsApp: the bridge endpoints and when Crumb answers.
Run: python -m pytest -q tests"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CRUMB_DB_PATH", str(Path(tempfile.mkdtemp()) / "crumb-test.db"))
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from crumb import config, guard, llm, store, whatsapp  # noqa: E402
from crumb.main import app  # noqa: E402
from crumb.wa_clean import chat_slug  # noqa: E402

TOKEN = "bridge-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    store.init()
    monkeypatch.setattr(config, "BRIDGE_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "WA_DEBOUNCE_SEC", 0)
    monkeypatch.setattr(whatsapp, "_per_chat", guard.RateLimiter(1000, 600))
    whatsapp.bridge.update(last_seen=None, last_sync=None, syncs=0, replies_queued=0)
    yield


@pytest.fixture
def client():
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def no_autorun(monkeypatch):
    """Sync records messages but doesn't answer by itself; tests call answer()."""
    monkeypatch.setattr(whatsapp, "schedule", lambda chat_id: None)


def ago(minutes: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def snapshot(name: str, *messages: tuple) -> dict:
    """messages: (key, direction, text, minutes_ago)."""
    return {"schema": "karlon.chat_snapshot/1",
            "chat": {"id": chat_slug(name), "name": name, "wa_name": name, "avatar_emoji": name[0]},
            "messages": [{"external_key": k, "wa_id": f"wa-{k}", "direction": d, "sender": name if d == "in" else "You",
                          "kind": "text", "text": t, "created_at": ago(m)} for k, d, t, m in messages]}


def sync(client, name, *messages):
    r = client.post(f"/api/chats/{chat_slug(name)}/sync", json=snapshot(name, *messages), headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def answer(name):
    return asyncio.run(whatsapp.answer(chat_slug(name)))


# ── the bridge's door ───────────────────────────────────────────────────────

def test_closed_without_token(client, monkeypatch):
    monkeypatch.setattr(config, "BRIDGE_TOKEN", "")
    assert client.get("/api/outbox/urgent", headers=AUTH).status_code == 503


def test_wrong_token(client):
    assert client.get("/api/outbox/urgent").status_code == 401
    assert client.get("/api/outbox/urgent", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/outbox/urgent", headers=AUTH).json() == []
    assert client.get("/api/outbox/documents", headers=AUTH).json() == []


def test_chat_id_must_match_name(client):
    body = snapshot("Thandi", ("k1", "in", "hi", 1))
    r = client.post("/api/chats/wrong-id/sync", json=body, headers=AUTH)
    assert r.status_code == 422


# ── the whole round trip ────────────────────────────────────────────────────

def test_message_gets_answered_and_delivered(client):
    report = sync(client, "Thandi", ("k1", "in", "What should I try first?", 1))
    assert report["inserted"] == 1 and report["missing_images"] == []

    items = []
    for _ in range(50):                                   # the reply is written in the background
        items = client.get("/api/outbox/urgent", headers=AUTH).json()
        if items:
            break
        time.sleep(0.05)
    assert len(items) == 1
    item = items[0]
    assert item["wa_name"] == "Thandi" and item["kind"] == "text"
    assert "butter half" in item["text"].lower() and "**" not in item["text"]

    r = client.patch(f"/api/chats/{item['chat_id']}/messages/{item['id']}/wa-ack", params={"status": "sent"},
                     headers=AUTH)
    assert r.status_code == 200
    assert client.get("/api/outbox/urgent", headers=AUTH).json() == []

    # Next scan reads Crumb's reply back from WhatsApp (bold markers gone): linked, not added again.
    echo = item["text"].replace("*", "")
    report = sync(client, "Thandi", ("k1", "in", "What should I try first?", 1), ("k2", "out", echo, 0))
    assert report["linked_app_messages"] == 1 and report["inserted"] == 0
    chat = whatsapp.chat(chat_slug("Thandi"))
    assert [m["direction"] for m in chat["messages"]] == ["in", "out"]
    assert chat["why_quiet"] == "nothing new from the person"


def test_resync_is_idempotent(client, no_autorun):
    msgs = (("k1", "in", "hello", 1), ("k2", "in", "anyone there?", 0.5))
    assert sync(client, "Sipho", *msgs)["inserted"] == 2
    again = sync(client, "Sipho", *msgs)
    assert again["inserted"] == 0 and again["unchanged"] == 2


# ── when Crumb stays quiet ──────────────────────────────────────────────────

def test_old_messages_are_not_answered(client, no_autorun):
    sync(client, "Lindiwe", ("k1", "in", "are you open?", config.WA_REPLY_WINDOW_MIN + 5))
    assert answer("Lindiwe") == {"queued": False, "reason": "the message is too old"}


def test_quiet_when_someone_answers_from_the_phone(client, no_autorun):
    sync(client, "Kudzi", ("k1", "in", "hi", 20), ("k2", "out", "Hey Kudzi, one sec", 15),
         ("k3", "in", "ok and the cookie?", 1))
    assert answer("Kudzi")["reason"] == "someone is answering from the phone"


def test_auto_reply_switches(client, no_autorun):
    sync(client, "Rudo", ("k1", "in", "hi", 1))
    whatsapp.set_auto_reply(chat_slug("Rudo"), False)
    assert answer("Rudo")["reason"] == "auto-reply is off for this chat"
    whatsapp.set_auto_reply(chat_slug("Rudo"), True)
    store.put_settings({"wa_auto_reply": False})
    assert answer("Rudo")["reason"] == "auto-reply is off"
    store.put_settings({"wa_auto_reply": True})
    assert answer("Rudo")["queued"] is True
    assert answer("Rudo")["reason"] == "nothing new from the person"
    sync(client, "Rudo", ("k1", "in", "hi", 1), ("k2", "in", "also, the cookie?", 0))   # wrote again, reply not out yet
    assert answer("Rudo")["reason"] == "a reply is already waiting to go out"


def test_same_answer_to_two_people_is_personalised(client, no_autorun):
    sync(client, "Tariro", ("k1", "in", "how much is the bun?", 1))
    sync(client, "Nyasha", ("k1", "in", "how much is the bun?", 1))
    first, second = answer("Tariro"), answer("Nyasha")
    assert first["queued"] and second["queued"]
    assert first["text"] != second["text"] and second["text"].startswith("Hi Nyasha! ")


def test_model_reply_uses_whatsapp_formatting(client, no_autorun, monkeypatch):
    calls = []

    class Messages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="text", text="Try **The butter half** 🥐")])

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
    sync(client, "Farai", ("k1", "in", "hi!", 3), ("k2", "out", "Hello Farai", 2.5), ("k3", "in", "what to try?", 1))
    store.put_settings({"wa_auto_reply": True})
    monkeypatch.setattr(config, "WA_HUMAN_PAUSE_MIN", 0)
    out = answer("Farai")
    assert out["text"] == "Try *The butter half* 🥐" and out["source"] == "model"
    assert "WhatsApp" in calls[0]["system"][0]["text"]
    assert [m["role"] for m in calls[0]["messages"]] == ["user", "assistant", "user"]


# ── extras: pictures, the dashboard ─────────────────────────────────────────

def test_profile_picture(client, no_autorun):
    sync(client, "Chipo", ("k1", "in", "hi", 1))
    cid = chat_slug("Chipo")
    r = client.post(f"/api/chats/{cid}/profile-pic", files={"file": ("p.jpg", b"\xff\xd8jpegbytes", "image/jpeg")},
                    headers=AUTH)
    version = r.json()["version"]
    assert sync(client, "Chipo", ("k1", "in", "hi", 1))["profile_pic_version"] == version
    assert client.post("/api/chats/nobody/profile-pic", files={"file": ("p.jpg", b"x", "image/jpeg")},
                       headers=AUTH).status_code == 404


def test_dashboard_whatsapp(client, no_autorun, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pa55")
    admin = {"X-Crumb-Token": "pa55"}
    sync(client, "Anesu", ("k1", "in", "hello", 1))
    cid = chat_slug("Anesu")

    st = client.get("/api/admin/wa/status", headers=admin).json()
    assert st["configured"] and st["bridge_online"] and st["chats"] == 1 and st["received"] == 1
    assert client.get("/api/admin/status", headers=admin).json()["whatsapp"]["chats"] == 1

    chats = client.get("/api/admin/wa/chats", headers=admin).json()
    assert chats[0]["id"] == cid and chats[0]["last_text"] == "hello"
    assert client.get(f"/api/admin/wa/chats/{cid}", headers=admin).json()["why_quiet"] is None

    assert client.put(f"/api/admin/wa/chats/{cid}", json={"auto_reply": False}, headers=admin).json()["auto_reply"] is False
    r = client.post(f"/api/admin/wa/chats/{cid}/send", json={"text": "Hi from the kitchen"}, headers=admin)
    assert r.json()["queued"]
    out = client.get("/api/outbox/urgent", headers=AUTH).json()
    assert out[0]["text"] == "Hi from the kitchen"

    client.put(f"/api/admin/wa/chats/{cid}", json={"auto_reply": True}, headers=admin)
    assert client.post(f"/api/admin/wa/chats/{cid}/answer", headers=admin).json()["queued"] is False
    assert client.get("/api/admin/wa/chats", headers={"X-Crumb-Token": "x"}).status_code == 403
    assert client.get(f"/api/admin/wa/chats/{cid}/picture", headers=admin).status_code == 404


def test_wa_clean_matches_the_bridge():
    here = Path(__file__).resolve().parent.parent
    bridge = here.parent / "crumb-pc-bridge" / "wa_clean.py"
    if not bridge.exists():
        pytest.skip("crumb-pc-bridge not next to crumb-space")
    assert bridge.read_bytes() == (here / "crumb" / "wa_clean.py").read_bytes()
