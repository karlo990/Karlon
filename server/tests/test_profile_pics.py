"""WhatsApp profile pictures: bridge uploads the bytes, the app gets a URL
that exists on this server (never a PC-local path)."""
from app.wa_clean import chat_slug

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
JPEG2 = b"\xff\xd8\xff\xe0" + b"\x01" * 200
KARL = chat_slug("Karl")


def _chat(client, **extra):
    body = {"id": KARL, "name": "Karl", "avatar_emoji": "KA", "wa_name": "Karl"}
    body.update(extra)
    return client.post("/api/chats", json=body)


def _upload(client, data=JPEG, chat=KARL):
    return client.post(f"/api/chats/{chat}/profile-pic", files={"file": ("p.jpg", data, "image/jpeg")})


def _listed(client):
    return {c["id"]: c for c in client.get("/api/chats").json()}[KARL]["profile_pic_url"]


def test_upload_sets_a_versioned_url_the_app_can_load(client):
    _chat(client)
    r = _upload(client).json()
    assert r["profile_pic_url"].startswith(f"/static/profile_pics/{KARL}.jpg?v=")
    assert _listed(client) == r["profile_pic_url"]
    r2 = _upload(client, JPEG2).json()                       # changed picture -> new URL
    assert r2["version"] != r["version"] and _listed(client) == r2["profile_pic_url"]


def test_plain_upsert_and_old_bridge_paths_never_wipe_or_fake_a_picture(client):
    _chat(client, profile_pic_url=f"/static/profile_pics/{KARL}.jpg")   # old bridge: PC-only path
    assert _listed(client) is None
    url = _upload(client).json()["profile_pic_url"]
    _chat(client)                                                         # bridge upsert, no pic
    _chat(client, profile_pic_url=f"/static/profile_pics/{KARL}.jpg")
    assert _listed(client) == url


def test_missing_file_after_restart_shows_initials_and_tells_the_bridge(client, db):
    import app.config as config
    _chat(client)
    _upload(client)
    for f in config.PROFILE_PICS_DIR.iterdir():
        f.unlink()                                            # HF Space restart wipes static/
    assert _listed(client) is None
    snap = {"schema": "karlon.chat_snapshot/1", "chat": {"id": KARL, "name": "Karl", "wa_name": "Karl",
            "avatar_emoji": "KA"}, "messages": [], "stats": {}}
    rep = client.post(f"/api/chats/{KARL}/sync", json=snap).json()
    assert rep["profile_pic_version"] is None                 # -> bridge re-uploads
    v = _upload(client).json()["version"]
    assert client.post(f"/api/chats/{KARL}/sync", json=snap).json()["profile_pic_version"] == v


def test_rejects_non_images_unknown_chats_and_huge_files(client):
    _chat(client)
    assert _upload(client, b"<html>nope</html>").status_code == 415
    assert _upload(client, chat="nobody").status_code == 404
    assert _upload(client, JPEG + b"\x00" * (2 * 1024 * 1024)).status_code == 413
