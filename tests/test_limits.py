from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from karaqueue.app import client_address, create_app
from karaqueue.limits import LIMITS, Limiter
from karaqueue.store import Store


def test_limiter_counts_per_address_and_forgets_old_hits():
    limiter = Limiter()
    count, window = LIMITS["create"]
    for _ in range(count):
        assert limiter.take("create", "a", 1, now=100)
    assert not limiter.take("create", "a", 1, now=100)
    assert limiter.take("create", "b", 1, now=100)  # another address has its own
    assert limiter.take("create", "a", 1, now=100 + window)  # the old ones have aged out
    assert limiter.take("create", "a", 0, now=100 + window)  # 0 turns limits off
    limiter.prune(now=100 + 10 * window)
    assert not limiter.hits


def test_guessing_room_codes_gets_cut_off():
    app = create_app(Store())
    client = TestClient(app)
    app.state.settings.set("rate_limit", 0.2)  # 6 misses
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code = room["code"]
    for _ in range(10):  # a page retrying one typo counts once
        assert client.get("/api/rooms/NOPE0").status_code == 404
    for guess in range(1, 6):
        assert client.get(f"/api/rooms/nope{guess}").status_code == 404
    assert client.get(f"/api/rooms/{code}").status_code == 429  # even a right guess, now
    # Whoever is already signed in to the room carries on; a made-up token doesn't help.
    assert client.get(f"/api/rooms/{code}", headers={"Authorization": "Bearer " + room["host_token"]}).status_code == 200
    assert client.get(f"/api/rooms/{code}", headers={"Authorization": "Bearer made-up"}).status_code == 429
    assert client.get("/api/rooms/NOPE9", headers={"Authorization": "Bearer made-up"}).status_code == 429
    assert client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).status_code == 429
    with client.websocket_connect("/ws/NOPE8") as ws:
        try:
            ws.receive_json()
            raise AssertionError("socket stayed open")
        except WebSocketDisconnect as e:
            assert e.code == 4429
    # The real room's phones and screens keep their sockets: only wrong codes are cut off there.
    with client.websocket_connect(f"/ws/{code}") as ws:
        ws.send_json({"type": "hello", "display": {"id": "tv-1234abcd"}, "host": room["host_token"]})
        assert ws.receive_json()["type"] == "welcome"
    assert client.post("/api/rooms", json={"name": "Saturday"}).status_code == 200  # not a room lookup
    app.state.settings.set("rate_limit", 0)
    assert client.get(f"/api/rooms/{code}").status_code == 200


def test_unknown_room_socket_is_told_so():
    client = TestClient(create_app(Store()))
    with client.websocket_connect("/ws/NOPE") as ws:
        try:
            ws.receive_json()
            raise AssertionError("socket stayed open")
        except WebSocketDisconnect as e:
            assert e.code == 4404  # so the page stops reconnecting


def test_starting_rooms_is_capped():
    app = create_app(Store())
    client = TestClient(app)
    app.state.settings.set("rate_limit", 0.1)  # 2 rooms an hour
    assert client.post("/api/rooms", json={"name": "A"}).status_code == 200
    assert client.post("/api/rooms", json={"name": "B"}).status_code == 200
    assert client.post("/api/rooms", json={"name": "C"}).status_code == 429


def test_new_rooms_get_six_character_codes():
    client = TestClient(create_app(Store()))
    assert len(client.post("/api/rooms", json={"name": "Friday"}).json()["code"]) == 6


def test_one_ipv6_network_counts_as_one_address():
    assert client_address("2001:db8:1:2::5") == client_address("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert client_address("2001:db8:1:3::5") != client_address("2001:db8:1:2::5")
    assert client_address("::ffff:192.0.2.7") == client_address("192.0.2.7") == "192.0.2.7"
    assert client_address("testclient") == "testclient"
