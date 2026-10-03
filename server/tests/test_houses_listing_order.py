"""The app can list every house in KCER order (KCER 101 first)."""


def _ingest(client, n, city):
    client.post("/api/houses/ingest", json={"listings": [{
        "url": f"https://www.airbnb.com/rooms/{n}", "title": f"Place {n}", "location": city}]})


def test_kcer_order_and_all_cities(client):
    for n, city in [(1, "Harare"), (2, "Bulawayo"), (3, "Harare"), (4, "Mutare")]:
        _ingest(client, n, city)
    harare = client.get("/api/houses/available", params={"location": "harare", "sort": "ref", "limit": 100}).json()
    assert [h["ref_code"] for h in harare] == ["KCER 101", "KCER 103"]
    everything = client.get("/api/houses/available", params={"location": "all", "sort": "ref", "limit": 100}).json()
    assert [h["ref_code"] for h in everything] == ["KCER 101", "KCER 102", "KCER 103", "KCER 104"]
    newest = client.get("/api/houses/available", params={"location": "all"}).json()
    assert newest[0]["ref_code"] == "KCER 104"                    # default stays newest first
    assert client.get("/api/houses/available", params={"location": "all", "sort": "x"}).status_code == 422
