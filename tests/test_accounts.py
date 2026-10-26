from fastapi.testclient import TestClient

from karaqueue import store as store_module
from karaqueue.app import create_app
from karaqueue.store import Store


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_a_guest_account_finds_its_spot_from_a_new_phone_and_in_other_rooms():
    client = TestClient(create_app(Store()))
    friday = client.post("/api/rooms", json={"name": "Friday"}).json()
    saturday = client.post("/api/rooms", json={"name": "Saturday"}).json()
    assert client.post("/api/guests", json={"name": "Alex", "password": "short"}).status_code == 400
    phone = client.post("/api/guests", json={"name": "Alex", "password": "sing it loud"}).json()
    assert client.post("/api/guests", json={"name": "alex", "password": "sing it loud"}).status_code == 409

    joined = client.post(f"/api/rooms/{friday['code']}/singers", json={"guest": phone["token"]}).json()
    assert joined["name"] == "Alex"
    me = bearer(joined["token"])
    client.post(f"/api/rooms/{friday['code']}/songs", headers=me, json={"title": "Dancing Queen", "artist": "ABBA"})

    # A new phone: sign in, join again, same singer and same queue, and the first phone still works.
    assert client.post("/api/guests/login", json={"name": "ALEX", "password": "wrong one!"}).status_code == 401
    other = client.post("/api/guests/login", json={"name": "ALEX", "password": "sing it loud"}).json()
    assert [r["code"] for r in other["rooms"]] == [friday["code"]]
    again = client.post(f"/api/rooms/{friday['code']}/singers", json={"guest": other["token"]}).json()
    assert again["singer_id"] == joined["singer_id"] and again["token"] != joined["token"]
    for token in (joined["token"], again["token"]):
        mine = client.get(f"/api/rooms/{friday['code']}/me", headers=bearer(token)).json()
        assert [s["title"] for s in mine["songs"]] == ["Dancing Queen"]

    # The same account in another room is a new singer there.
    there = client.post(f"/api/rooms/{saturday['code']}/singers", json={"guest": other["token"]}).json()
    assert there["singer_id"] != joined["singer_id"]

    # Signing out ends that session only; a made-up one doesn't join.
    client.delete("/api/guests/me", headers=bearer(other["token"]))
    assert client.get("/api/guests/me", headers=bearer(other["token"])).status_code == 401
    assert client.get("/api/guests/me", headers=bearer(phone["token"])).json()["name"] == "Alex"
    assert client.post(f"/api/rooms/{friday['code']}/singers", json={"guest": "made-up"}).status_code == 401


def test_too_many_phones_signs_the_oldest_out():
    client = TestClient(create_app(Store()))
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    guest = client.post("/api/guests", json={"name": "Alex", "password": "sing it loud"}).json()["token"]
    tokens = [client.post(f"/api/rooms/{code}/singers", json={"guest": guest}).json()["token"] for _ in range(7)]
    working = [t for t in tokens if client.get(f"/api/rooms/{code}/me", headers=bearer(t)).status_code == 200]
    assert working == tokens[-store_module.DEVICES:]


def test_the_host_adds_a_singer_and_signs_their_phone_in_with_a_one_time_code(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_SINGERS_PER_ROOM", "1")
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    alex = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()
    assert client.post(f"/api/rooms/{code}/singers", json={"name": "Sam"}).status_code == 409  # full
    sam = client.post(f"/api/rooms/{code}/singers", headers=host, json={"name": "Sam"}).json()
    assert "token" not in sam  # the host never holds a guest's sign-in

    # Only the host or Sam can make Sam's code, and it works once.
    assert client.post(f"/api/rooms/{code}/singers/{sam['singer_id']}/link", headers=bearer(alex["token"])).status_code == 403
    claim = client.post(f"/api/rooms/{code}/singers/{sam['singer_id']}/link", headers=host).json()["claim"]
    assert client.post("/api/rooms/OTHER1/claim", json={"claim": claim}).status_code in (403, 404)
    phone = client.post(f"/api/rooms/{code}/claim", json={"claim": claim}).json()
    assert client.get(f"/api/rooms/{code}/me", headers=bearer(phone["token"])).json()["name"] == "Sam"
    assert client.post(f"/api/rooms/{code}/claim", json={"claim": claim}).status_code == 403

    # A singer can move themselves to another phone too. A new code replaces the last one,
    # and one that has run out doesn't work.
    link = f"/api/rooms/{code}/singers/{alex['singer_id']}/link"
    first = client.post(link, headers=bearer(alex["token"])).json()["claim"]
    claim = client.post(link, headers=bearer(alex["token"])).json()["claim"]
    assert client.post(f"/api/rooms/{code}/claim", json={"claim": first}).status_code == 403
    assert client.post(f"/api/rooms/{code}/claim", json={"claim": claim}).status_code == 200
    monkeypatch.setattr(store_module, "CLAIM_SECONDS", -1)
    late = client.post(link, headers=bearer(alex["token"])).json()["claim"]
    assert client.post(f"/api/rooms/{code}/claim", json={"claim": late}).status_code == 403


def test_a_code_never_signs_in_as_someone_else_after_the_singer_is_removed():
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    other = client.post("/api/rooms", json={"name": "Saturday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    troll = client.post(f"/api/rooms/{code}/singers", json={"name": "Troll"}).json()
    claim = client.post(f"/api/rooms/{code}/singers/{troll['singer_id']}/link", headers=bearer(troll["token"])).json()["claim"]
    client.delete(f"/api/rooms/{code}/singers/{troll['singer_id']}", headers=host)
    # The next singer can get the removed one's id; the old code still mustn't sign in as them.
    client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"})
    assert client.post(f"/api/rooms/{code}/claim", json={"claim": claim}).status_code == 403

    # A code only works in its own room, even once its singer and room are gone.
    sam = client.post(f"/api/rooms/{code}/singers", json={"name": "Sam"}).json()
    claim = client.post(f"/api/rooms/{code}/singers/{sam['singer_id']}/link", headers=bearer(sam["token"])).json()["claim"]
    client.post(f"/api/rooms/{other['code']}/singers", json={"name": "Pat"})
    assert client.post(f"/api/rooms/{other['code']}/claim", json={"claim": claim}).status_code == 403
    client.delete(f"/api/rooms/{code}/singers/{sam['singer_id']}", headers=host)
    assert client.post(f"/api/rooms/{code}/claim", json={"claim": claim}).status_code == 403


def test_signing_in_on_a_phone_already_in_the_room_ties_that_spot_to_the_account():
    client = TestClient(create_app(Store()))
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    joined = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()
    assert client.get(f"/api/rooms/{code}/me", headers=bearer(joined["token"])).json()["account"] is False
    account = client.post("/api/guests", json={"name": "Alex", "password": "sing it loud"}).json()["token"]
    # From a new phone the name is taken, and the answer says what to do.
    taken = client.post(f"/api/rooms/{code}/singers", json={"guest": account})
    assert taken.status_code == 409 and "host" in taken.json()["detail"]
    # From the phone that joined, the account takes over that spot.
    tied = client.post(f"/api/rooms/{code}/singers", headers=bearer(joined["token"]), json={"guest": account}).json()
    assert tied["singer_id"] == joined["singer_id"]
    assert client.get(f"/api/rooms/{code}/me", headers=bearer(joined["token"])).json()["account"] is True
    again = client.post(f"/api/rooms/{code}/singers", json={"guest": account}).json()
    assert again["singer_id"] == joined["singer_id"]


def test_guest_typos_on_the_party_wifi_dont_lock_out_the_admin(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    app = create_app(Store())
    client = TestClient(app)
    client.post("/api/guests", json={"name": "Alex", "password": "sing it loud"})
    for _ in range(10):
        assert client.post("/api/guests/login", json={"name": "Alex", "password": "wrong one!"}).status_code == 401
    # Ten wrong tries lock that name for a while, from that phone only, and nothing else.
    assert client.post("/api/guests/login", json={"name": "Alex", "password": "sing it loud"}).status_code == 429
    alex = TestClient(app, client=("10.0.0.7", 50000))
    assert alex.post("/api/guests/login", json={"name": "Alex", "password": "sing it loud"}).status_code == 200
    assert client.post("/api/guests/login", json={"name": "Sam", "password": "wrong one!"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "correct horse"}).status_code == 200


def test_admins_turn_off_reset_and_delete_guest_accounts(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    client = TestClient(create_app(Store()))
    admin = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    guest = client.post("/api/guests", json={"name": "Alex", "password": "sing it loud"}).json()["token"]
    phone = bearer(client.post(f"/api/rooms/{code}/singers", json={"guest": guest}).json()["token"])
    assert client.get("/api/admin/guests").status_code == 401
    [alex] = client.get("/api/admin/guests", headers=admin).json()
    assert alex["name"] == "Alex" and alex["rooms"] == 1 and "password" not in alex

    client.patch(f"/api/admin/guests/{alex['id']}", headers=admin, json={"disabled": True})
    assert client.get("/api/guests/me", headers=bearer(guest)).status_code == 401  # signed out everywhere,
    assert client.get(f"/api/rooms/{code}/me", headers=phone).status_code == 401  # rooms too
    assert client.post("/api/guests/login", json={"name": "Alex", "password": "sing it loud"}).status_code == 403
    client.patch(f"/api/admin/guests/{alex['id']}", headers=admin, json={"disabled": False, "password": "a new one!"})
    assert client.post("/api/guests/login", json={"name": "Alex", "password": "a new one!"}).status_code == 200
    assert client.delete(f"/api/admin/guests/{alex['id']}", headers=admin).json()["guests"] == []
    # The singer stays in the room, just without the account.
    assert [s["name"] for s in client.get(f"/api/rooms/{code}").json()["rotation"]] == ["Alex"]
