"""A failed invoice can be put back in the worker's queue."""


def _invoice(client):
    return client.post("/api/invoices", json={"guest_name": "Ahmed", "id_number": "P1", "location": "harare",
                                              "property_name": "KCER 101", "nights": 3, "rate": 140}).json()


def test_failed_invoice_can_be_retried(client):
    inv = _invoice(client)
    assert client.patch(f"/api/invoices/{inv['id']}/claim").json()["claimed"]
    client.patch(f"/api/invoices/{inv['id']}/error", json={"error_message": "LibreOffice not found"})
    assert client.get("/api/invoices/worker/pending").json() == []
    r = client.post(f"/api/invoices/{inv['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert [i["id"] for i in client.get("/api/invoices/worker/pending").json()] == [inv["id"]]


def test_only_failed_or_stuck_invoices_are_retried(client):
    inv = _invoice(client)
    assert client.post(f"/api/invoices/{inv['id']}/retry").status_code == 409     # still pending
    assert client.post("/api/invoices/nope/retry").status_code == 404


def test_terms_tapped_twice_queues_one_pdf(client, db, monkeypatch, tmp_path):
    import app.routers.messages as messages
    pdf = tmp_path / "terms_test.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(messages, "TERMS_PDF_PATH", pdf)
    client.post("/api/chats", json={"id": "c1", "name": "+971521462917", "avatar_emoji": "17",
                                    "wa_name": "+971521462917"})
    a = client.post("/api/chats/c1/send-terms").json()
    b = client.post("/api/chats/c1/send-terms").json()
    assert a["id"] == b["id"]
    assert db.execute("SELECT COUNT(*) FROM messages WHERE chat_id='c1'").fetchone()[0] == 1
    assert client.patch(f"/api/chats/c1/messages/{a['id']}/wa-ack?status=superseded").status_code == 200
