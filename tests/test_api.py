import pytest
from fastapi.testclient import TestClient

from karaqueue import displays
from karaqueue.app import create_app
from karaqueue.store import Store


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def one_loop():
    """A client whose sockets all share one event loop, like a real server. Without it each test
    socket gets its own loop, and a message one socket's handler sends to another may never wake it."""
    clients = []

    def make(app):
        clients.append(TestClient(app).__enter__())
        return clients[-1]
    yield make
    for client in clients:
        client.__exit__(None, None, None)


def test_a_night_of_karaoke():
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])

    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])
    assert client.post(f"/api/rooms/{code}/singers", json={"name": "alex"}).status_code == 409

    with client.websocket_connect(f"/ws/{code}") as ws:
        assert client.post(f"/api/rooms/{code}/songs", headers=alex,
                           json={"title": "Africa", "artist": "Toto"}).status_code == 200
        assert ws.receive_json() == {"type": "update"}

    assert client.post(f"/api/rooms/{code}/next", headers=alex).status_code == 403
    assert client.post(f"/api/rooms/{code}/next").status_code == 401
    assert client.post(f"/api/rooms/{code}/next", headers=host).status_code == 200

    state = client.get(f"/api/rooms/{code}").json()
    assert state["now_playing"]["singer"] == "Alex"
    assert state["rotation"] == [{"id": 1, "name": "Alex", "queued": 0}]

    assert client.get(f"/api/rooms/{code}/host", headers=alex).status_code == 403
    assert client.get(f"/api/rooms/{code}/host", headers=host).json()["singers"][0]["name"] == "Alex"

    info = client.get(f"/api/rooms/{code}/join-info").json()
    assert info["link"].endswith(f"/r/{code}")
    assert info["qr_svg"].startswith("<svg") and info["local"] is False
    local = TestClient(client.app, base_url="http://127.0.0.1:8000").get(f"/api/rooms/{code}/join-info").json()
    assert local["local"] is True  # the pages warn that phones can't follow this QR code


def test_new_rooms_need_an_account_by_default_and_rooms_fill_up(monkeypatch):
    monkeypatch.delenv("KARAQUEUE_NEW_ROOMS")
    client = TestClient(create_app(Store()))
    assert client.get("/api/config").json()["accounts_only"] is True
    assert client.post("/api/rooms", json={"name": "Friday"}).status_code == 401

    monkeypatch.setenv("KARAQUEUE_NEW_ROOMS", "anyone")
    monkeypatch.setenv("KARAQUEUE_SINGERS_PER_ROOM", "2")
    client = TestClient(create_app(Store()))
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    for name in ("Alex", "Sam"):
        assert client.post(f"/api/rooms/{code}/singers", json={"name": name}).status_code == 200
    assert client.post(f"/api/rooms/{code}/singers", json={"name": "Kit"}).status_code == 409


def test_errors_are_json():
    client = TestClient(create_app(Store()))
    assert client.get("/api/rooms/NOPE").json() == {"detail": "Room not found"}
    assert client.post("/api/rooms", json={"name": "   "}).status_code == 400


def test_pages_serve():
    client = TestClient(create_app(Store()))
    for path in ("/", "/admin", "/r/ABCD", "/r/ABCD/host", "/r/ABCD/screen", "/static/kq.js"):
        assert client.get(path).status_code == 200, path


def test_screens_show_up_for_the_host_and_take_commands():
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])

    with client.websocket_connect(f"/ws/{code}") as tv:
        tv.send_json({"type": "hello", "display": {"id": "tv-1234abcd", "name": "Bar TV", "layout": "video"},
                      "host": room["host_token"]})
        assert tv.receive_json()["type"] == "welcome"
        assert tv.receive_json() == {"type": "displays"}
        tv.send_json({"type": "status", "state": "playing", "time": 12.34, "duration": "nope", "mic": "on", "recording": True})
        assert tv.receive_json() == {"type": "displays"}

        assert client.get(f"/api/rooms/{code}/displays", headers=alex).status_code == 403
        [screen] = client.get(f"/api/rooms/{code}/displays", headers=host).json()
        assert screen == {"id": "tv-1234abcd", "name": "Bar TV", "layout": "video", "volume": 100,
                          "muted": False, "sink": "", "mic": False, "mic_volume": 80, "reverb": 25, "record": False,
                          "state": "playing", "mic_state": "on", "recording": True, "time": 12.3, "duration": None,
                          "reason": "", "audio": "unsupported", "outputs": [], "panel": False}
        # A song that won't play says why, for the host's panel.
        tv.send_json({"type": "status", "state": "error", "reason": "  YouTube won't\nplay this " + "x" * 300})
        assert tv.receive_json() == {"type": "displays"}
        [screen] = client.get(f"/api/rooms/{code}/displays", headers=host).json()
        assert screen["state"] == "error" and screen["reason"].startswith("YouTube won't play this x")
        assert len(screen["reason"]) == 200

        tv.send_json({"type": "outputs", "audio": "ready", "outputs": [
            {"id": "default", "label": ""}, {"id": "hdmi-1", "label": "  TV   speakers "},
            {"id": 7, "label": "bad id"}, "junk", {"id": "x" * 201}] + [{"id": f"o{i}"} for i in range(30)]})
        assert tv.receive_json() == {"type": "displays"}
        [screen] = client.get(f"/api/rooms/{code}/displays", headers=host).json()
        assert screen["outputs"][:2] == [{"id": "default", "label": "Speaker 1"},
                                         {"id": "hdmi-1", "label": "TV speakers"}]
        assert len(screen["outputs"]) == 17  # 20 looked at, 3 of them malformed
        assert screen["audio"] == "ready"

        url = f"/api/rooms/{code}/displays/tv-1234abcd"
        assert client.patch(url, headers=host, json={"layout": "disco"}).status_code == 400
        assert client.patch(url, headers=host, json={"action": "explode"}).status_code == 400
        assert client.patch(url, headers=host, json={"reverb": "lots"}).status_code == 422
        res = client.patch(url, headers=host, json={"layout": "queue", "volume": 250, "action": "pause",
                                                     "mic": True, "reverb": -5})
        assert res.json()["volume"] == 100 and res.json()["layout"] == "queue"
        assert res.json()["mic"] is True and res.json()["reverb"] == 0
        command = tv.receive_json()
        assert command["type"] == "command" and command["action"] == "pause"
        assert command["settings"]["layout"] == "queue"
        assert client.patch(url, headers=host, json={"sink": "y" * 201}).status_code == 400
        assert client.patch(url, headers=host, json={"sink": "hdmi-1"}).json()["sink"] == "hdmi-1"
        assert tv.receive_json() == {"type": "displays"}  # from the pause PATCH
        assert tv.receive_json()["settings"]["sink"] == "hdmi-1"

        tv.send_json({"type": "hello", "display": {"id": "bad id!"}})  # ignored
        tv.send_text("not json")  # ignored
        assert tv.receive_json() == {"type": "displays"}  # from the PATCH above
        assert len(client.get(f"/api/rooms/{code}/displays", headers=host).json()) == 1

    assert client.get(f"/api/rooms/{code}/displays", headers=host).json() == []
    assert client.patch(f"/api/rooms/{code}/displays/tv-1234abcd", headers=host, json={}).status_code == 404


def test_sound_from_one_screen_and_the_others_follow_its_clock(one_loop):
    client = one_loop(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    options = f"/api/rooms/{code}/options"
    assert client.get(f"/api/rooms/{code}/host", headers=host).json()["options"]["sound"] == ""
    assert client.put(options, headers=host, json={"sound": "not an id!"}).status_code == 400
    assert client.put(options, headers=host, json={"sound": "panel-1234abcd"}).status_code == 200
    assert client.get(f"/api/rooms/{code}/host", headers=host).json()["options"]["sound"] == "panel-1234abcd"
    # Guests don't need to know which screen it is, so nobody can pose as it.
    assert "sound" not in client.get(f"/api/rooms/{code}").json()["options"]

    with client.websocket_connect(f"/ws/{code}") as panel, client.websocket_connect(f"/ws/{code}") as tv, \
            client.websocket_connect(f"/ws/{code}") as phone:
        panel.send_json({"type": "hello", "display": {"id": "panel-1234abcd", "panel": True}, "host": room["host_token"]})
        assert panel.receive_json()["sound"] == "panel-1234abcd"
        assert tv.receive_json() == {"type": "displays"}
        tv.send_json({"type": "hello", "display": {"id": "tv-1234abcd", "panel": "yes"}, "host": room["host_token"]})
        assert tv.receive_json()["type"] == "welcome"
        shown = {d["id"]: d["panel"] for d in client.get(f"/api/rooms/{code}/displays", headers=host).json()}
        assert shown == {"panel-1234abcd": True, "tv-1234abcd": False}
        for ws, count in ((panel, 2), (tv, 1), (phone, 2)):  # "displays" for each hello it hasn't read yet
            assert [ws.receive_json() for _ in range(count)] == [{"type": "displays"}] * count

        panel.send_json({"type": "clock", "song": "seven", "state": "playing", "time": 1})  # ignored
        panel.send_json({"type": "clock", "song": 7, "state": "dancing", "time": 61.234})
        assert tv.receive_json() == {"type": "clock", "display": "panel-1234abcd", "song": 7, "state": "idle", "time": 61.2}
        # Only screens hear the clock: it isn't news for the host page or phones.
        phone.send_json({"type": "clock", "song": 7, "state": "playing", "time": 1})  # not a screen: ignored
        tv.send_json({"type": "clock", "song": 7, "state": "paused", "time": 1})  # not the sound screen: ignored
        tv.send_json({"type": "status", "state": "paused"})
        assert phone.receive_json() == {"type": "displays"}
        assert panel.receive_json() == {"type": "displays"}  # and no clock from the TV before it

        # Switching the sound tells the screens; nobody else.
        assert client.put(options, headers=host, json={"sound": "tv-1234abcd"}).status_code == 200
        assert tv.receive_json() == {"type": "displays"}  # from its own status
        assert tv.receive_json() == {"type": "sound", "display": "tv-1234abcd"}
        assert phone.receive_json() == {"type": "update"}


def test_two_screens_finishing_the_same_video_only_skip_once():
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    for name in ("Alex", "Sam"):
        me = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": name}).json()["token"])
        client.post(f"/api/rooms/{code}/songs", headers=me, json={"title": "Song", "artist": name})
    client.post(f"/api/rooms/{code}/next", headers=host)
    playing = client.get(f"/api/rooms/{code}").json()["now_playing"]
    for _ in range(2):
        assert client.post(f"/api/rooms/{code}/next", headers=host, json={"after": playing["id"]}).status_code == 200
    assert client.get(f"/api/rooms/{code}").json()["now_playing"]["singer"] == "Sam"


def test_a_recording_screen_saves_performances_for_the_singer(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setenv("KARAQUEUE_RECORDING_MAX_MB", "1")
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])
    sam = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Sam"}).json()["token"])
    song = client.post(f"/api/rooms/{code}/songs", headers=alex, json={"title": "Africa", "artist": "Toto"}).json()["id"]
    later = client.post(f"/api/rooms/{code}/songs", headers=alex, json={"title": "Rosanna", "artist": "Toto"}).json()["id"]
    client.post(f"/api/rooms/{code}/next", headers=host)

    with client.websocket_connect(f"/ws/{code}") as tv:
        # A screen can't switch recording on for itself.
        tv.send_json({"type": "hello", "display": {"id": "tv-1234abcd", "record": True}, "host": room["host_token"]})
        welcome = tv.receive_json()
        assert welcome["settings"]["record"] is False
        upload = welcome["upload"]
        url = f"/api/rooms/{code}/recordings?display=tv-1234abcd&song={song}"
        audio = {"X-Upload": upload, "Content-Type": "audio/webm;codecs=opus"}

        assert client.post(url, headers=audio, content=b"sound").status_code == 403  # recording is off
        client.patch(f"/api/rooms/{code}/displays/tv-1234abcd", headers=host, json={"record": True})
        assert client.post(url, headers={**audio, "X-Upload": "guess"}, content=b"sound").status_code == 403
        assert client.post(url, headers={**audio, "Content-Type": "text/html"}, content=b"<p>").status_code == 415
        assert client.post(url, headers=audio, content=b"x" * (1024 * 1024 + 1)).status_code == 413
        # Only songs that have been on, and a host skipping fast doesn't lose the take.
        assert client.post(url.replace(f"song={song}", f"song={later}"), headers=audio, content=b"x").status_code == 404
        for _ in range(2):
            client.post(f"/api/rooms/{code}/next", headers=host)
        assert client.post(url, headers=audio, content=b"sound").status_code == 200
        assert client.post(url, headers=audio, content=b"again").status_code == 409  # one take per screen per song

    [rec] = client.get(f"/api/rooms/{code}/recordings", headers=alex).json()
    assert rec["title"] == "Africa" and rec["size"] == 5
    assert client.get(f"/api/rooms/{code}/recordings", headers=sam).json() == []
    file_url = f"/api/rooms/{code}/recordings/{rec['id']}/file"
    assert client.get(file_url, headers=sam).status_code == 403
    assert client.get(file_url, headers=alex).content == b"sound"
    assert len(list(tmp_path.rglob("*.webm"))) == 1  # the rejected uploads left nothing behind
    # Removing a singer takes their recordings' files with them, not just the list entries.
    [alex_id] = [s["id"] for s in client.get(f"/api/rooms/{code}/host", headers=host).json()["singers"] if s["name"] == "Alex"]
    assert client.delete(f"/api/rooms/{code}/singers/{alex_id}", headers=host).status_code == 200
    assert list(tmp_path.rglob("*.webm")) == []
    assert client.delete(f"/api/rooms/{code}/recordings/{rec['id']}", headers=host).status_code == 404


def test_deleting_recordings_stays_in_the_folder_and_survives_a_stuck_file(tmp_path, monkeypatch, caplog):
    rec, outside = tmp_path / "rec", tmp_path / "outside.webm"
    rec.mkdir()
    outside.write_bytes(b"keep")
    (rec / "link.webm").symlink_to(outside)
    (rec / "stuck.webm").mkdir()  # unlink() fails on a folder
    (rec / "loop.webm").symlink_to(rec / "loop.webm")  # resolve() fails on a loop
    (rec / "a.webm").write_bytes(b"sound")
    monkeypatch.setenv("KARAQUEUE_RECORDINGS_DIR", str(rec))
    store = Store()
    client = TestClient(create_app(store))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"})
    [alex_id] = [s["id"] for s in client.get(f"/api/rooms/{code}/host", headers=host).json()["singers"]]
    # Names normally come from the database; pretend one went bad.
    monkeypatch.setattr(store, "remove_singer", lambda *a: ["../outside.webm", "link.webm", "stuck.webm", "loop.webm", "", "a.webm"])
    with caplog.at_level("WARNING"):
        assert client.delete(f"/api/rooms/{code}/singers/{alex_id}", headers=host).status_code == 200
    assert outside.read_bytes() == b"keep" and (rec / "stuck.webm").is_dir() and rec.is_dir()
    assert not (rec / "a.webm").exists()
    warned = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Couldn't delete recording")]
    assert any("stuck.webm" in m for m in warned)
    # Python 3.13+ resolves a loop to itself and deletes the link; older ones raise and warn.
    assert any("loop.webm" in m for m in warned) or not (rec / "loop.webm").is_symlink()


def test_screens_wait_until_the_host_lets_them_in_and_cant_be_hijacked(one_loop):
    client = one_loop(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])
    tv_hello = {"type": "hello", "display": {"id": "tv-1234abcd", "name": "Bar TV"}}

    with client.websocket_connect(f"/ws/{code}") as tv:
        tv.send_json({**tv_hello, "host": room["host_token"][:-1] + "x", "key": "made-up"})
        waiting = tv.receive_json()
        assert waiting["type"] == "waiting" and len(waiting["code"]) == 6
        assert tv.receive_json() == {"type": "waiting"}
        assert client.get(f"/api/rooms/{code}/displays", headers=host).json() == []
        assert client.get(f"/api/rooms/{code}/waiting", headers=alex).status_code == 403
        [asking] = client.get(f"/api/rooms/{code}/waiting", headers=host).json()
        assert asking["name"] == "Bar TV" and asking["code"] == waiting["code"]

        assert client.post(f"/api/rooms/{code}/waiting/{asking['id']}", headers=alex, json={"value": True}).status_code == 403
        assert client.post(f"/api/rooms/{code}/waiting/{asking['id']}", headers=host, json={"value": True}).status_code == 200
        welcome = tv.receive_json()
        assert welcome["type"] == "welcome" and welcome["key"]
        assert client.post(f"/api/rooms/{code}/waiting/{asking['id']}", headers=host, json={"value": True}).status_code == 404
    key = welcome["key"]

    client.put(f"/api/rooms/{code}/options", headers=host, json={"sound": "tv-1234abcd"})
    with client.websocket_connect(f"/ws/{code}") as tv, client.websocket_connect(f"/ws/{code}") as other_tv, \
            client.websocket_connect(f"/ws/{code}") as attacker:
        # The TV comes back with its key, straight in, as does a second screen opened by the host.
        tv.send_json({**tv_hello, "key": key})
        assert tv.receive_json()["type"] == "welcome"
        other_tv.send_json({"type": "hello", "display": {"id": "tv-9999abcd"}, "host": room["host_token"]})
        assert until(other_tv, "welcome")
        # Someone who knows the TV's id but not its key only gets to wait, and the TV stays put.
        attacker.send_json(tv_hello)
        assert until(attacker, "waiting")["code"]
        attacker.send_json({"type": "clock", "song": 1, "state": "paused", "time": 3})
        attacker.send_json({"type": "status", "state": "paused"})
        tv.send_json({"type": "clock", "song": 1, "state": "playing", "time": 5})
        clock = until(other_tv, "clock")
        assert clock["display"] == "tv-1234abcd" and clock["state"] == "playing"  # the attacker's clock went nowhere
        [shown] = [d for d in client.get(f"/api/rooms/{code}/displays", headers=host).json() if d["id"] == "tv-1234abcd"]
        assert shown["state"] == "idle"
        client.patch(f"/api/rooms/{code}/displays/tv-1234abcd", headers=host, json={"action": "pause"})
        assert until(tv, "command")["action"] == "pause"
        [wid] = [w["id"] for w in client.get(f"/api/rooms/{code}/waiting", headers=host).json()]
        client.post(f"/api/rooms/{code}/waiting/{wid}", headers=host, json={"value": False})
        assert "didn't let" in until(attacker, "error")["detail"]

        # Removing a screen ends its key.
        assert client.delete(f"/api/rooms/{code}/displays/tv-1234abcd", headers=alex).status_code == 403
        assert client.delete(f"/api/rooms/{code}/displays/tv-1234abcd", headers=host).status_code == 200
        assert until(tv, "removed")
        tv.send_json({**tv_hello, "key": key})
        assert until(tv, "waiting")["code"]


def test_a_screen_let_in_by_its_code_moves_the_show_along(one_loop):
    client = one_loop(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    for name in ("Alex", "Sam"):
        me = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": name}).json()["token"])
        client.post(f"/api/rooms/{code}/songs", headers=me, json={"title": "Song for " + name, "artist": "x"})
    client.post(f"/api/rooms/{code}/next", headers=host, json={"after": 0})
    playing = lambda: client.get(f"/api/rooms/{code}").json()["now_playing"]  # noqa: E731
    first = playing()

    with client.websocket_connect(f"/ws/{code}") as tv, client.websocket_connect(f"/ws/{code}") as stranger:
        tv.send_json({"type": "hello", "display": {"id": "tv-1234abcd"}})
        until(tv, "waiting")
        [asking] = client.get(f"/api/rooms/{code}/waiting", headers=host).json()
        client.post(f"/api/rooms/{code}/waiting/{asking['id']}", headers=host, json={"value": True})
        until(tv, "welcome")
        # A screen nobody let in can't move anything.
        stranger.send_json({"type": "hello", "display": {"id": "tv-9999abcd"}})
        until(stranger, "waiting")
        stranger.send_json({"type": "ended", "song": first["id"]})
        tv.send_json({"type": "status", "state": "idle"})
        until(tv, "displays")
        assert playing()["id"] == first["id"]
        # The TV says the song ended: next singer. Saying it again changes nothing.
        tv.send_json({"type": "ended", "song": first["id"]})
        until(tv, "update")
        second = playing()
        assert second["singer"] == "Sam"
        tv.send_json({"type": "ended", "song": first["id"]})
        tv.send_json({"type": "status", "state": "idle"})
        until(tv, "displays")
        assert playing()["id"] == second["id"]


def test_screens_asking_to_join_cant_push_each_other_out(one_loop):
    client = one_loop(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    hello = lambda n: {"type": "hello", "display": {"id": f"tv-{n}234abcd", "name": f"TV {n}"}}  # noqa: E731
    asking = lambda: client.get(f"/api/rooms/{code}/waiting", headers=host).json()  # noqa: E731
    client.app.state.settings.set("screens_per_address", 2)

    with client.websocket_connect(f"/ws/{code}") as one, client.websocket_connect(f"/ws/{code}") as two, \
            client.websocket_connect(f"/ws/{code}") as three:
        one.send_json(hello(1))
        first = until(one, "waiting")["code"]
        # Saying hello again keeps the screen's place and its code.
        [before] = asking()
        one.send_json(hello(1))
        assert pairing(one) == first
        assert asking() == [before]
        two.send_json(hello(2))
        until(two, "waiting")
        # A third from the same address is refused, and the two waiting stay put.
        three.send_json(hello(3))
        refused = until(three, "error")
        assert refused["waiting"] and refused["retry"]
        assert [w["name"] for w in asking()] == ["TV 1", "TV 2"]
        # Let one in and there's room again.
        client.post(f"/api/rooms/{code}/waiting/{before['id']}", headers=host, json={"value": True})
        until(one, "welcome")
        three.send_json(hello(3))
        assert len(pairing(three)) == 6


def test_a_screen_the_host_never_answers_gives_up_its_place(one_loop, monkeypatch):
    client = one_loop(create_app(Store()))
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    client.app.state.settings.set("screens_per_address", 1)
    with client.websocket_connect(f"/ws/{code}") as one, client.websocket_connect(f"/ws/{code}") as two:
        one.send_json({"type": "hello", "display": {"id": "tv-1234abcd"}})
        until(one, "waiting")
        two.send_json({"type": "hello", "display": {"id": "tv-2234abcd"}})
        assert until(two, "error")["retry"]  # one address, one place
        monkeypatch.setattr(displays, "WAIT_SECONDS", 0)  # ten minutes later
        two.send_json({"type": "hello", "display": {"id": "tv-2234abcd"}})
        assert len(pairing(two)) == 6
        gave_up = until(one, "error")
        assert gave_up["retry"] and "in time" in gave_up["detail"]


def test_an_address_that_keeps_asking_to_join_is_refused(one_loop):
    client = one_loop(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code = room["code"]
    for n in range(20):
        with client.websocket_connect(f"/ws/{code}") as tv:
            tv.send_json({"type": "hello", "display": {"id": f"tv-{n:02d}34abcd"}})
            until(tv, "waiting")
    with client.websocket_connect(f"/ws/{code}") as tv:
        tv.send_json({"type": "hello", "display": {"id": "tv-9934abcd"}})
        assert until(tv, "error")["retry"]


def pairing(ws):
    """The code a waiting screen is told to show, skipping the room's news that someone's waiting."""
    for _ in range(50):
        message = until(ws, "waiting")
        if "code" in message:
            return message["code"]
    raise AssertionError("no code")


def until(ws, kind):
    """The next message of one type, skipping the "something changed" pings before it."""
    for _ in range(50):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError("no " + kind)


def test_recordings_only_for_the_song_that_just_played_and_within_quotas(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setenv("KARAQUEUE_RECORDINGS_ROOM_MB", "1")
    store = Store()
    client = TestClient(create_app(store))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])
    songs = [client.post(f"/api/rooms/{code}/songs", headers=alex, json={"title": f"Song {i}", "artist": "Me"}).json()["id"]
             for i in range(3)]
    client.post(f"/api/rooms/{code}/next", headers=host)

    with client.websocket_connect(f"/ws/{code}") as tv:
        tv.send_json({"type": "hello", "display": {"id": "tv-1234abcd"}, "host": room["host_token"]})
        audio = {"X-Upload": tv.receive_json()["upload"], "Content-Type": "audio/ogg"}
        client.patch(f"/api/rooms/{code}/displays/tv-1234abcd", headers=host, json={"record": True})
        url = f"/api/rooms/{code}/recordings?display=tv-1234abcd&song="
        assert client.post(url + str(songs[2]), headers=audio, content=b"x").status_code == 404  # not on yet
        client.post(f"/api/rooms/{code}/next", headers=host)
        client.post(f"/api/rooms/{code}/next", headers=host)
        store.db.execute("UPDATE songs SET ended_at = ended_at - 16 * 60 WHERE id = ?", (songs[0],))
        assert client.post(url + str(songs[0]), headers=audio, content=b"x").status_code == 404  # too long ago
        assert client.post(url + str(songs[1]), headers=audio, content=b"x" * 700_000).status_code == 200  # just ended
        # The room's 1 MB share is nearly used up.
        assert client.post(url + str(songs[2]), headers=audio, content=b"x" * 400_000).status_code == 413
        assert client.post(url + str(songs[2]), headers=audio, content=b"x" * 300_000).status_code == 200
    assert len(list(tmp_path.rglob("*.ogg"))) == 2
    first = client.get(f"/api/rooms/{code}/recordings", headers=alex).json()[0]["id"]
    assert client.delete(f"/api/rooms/{code}/recordings/{first}", headers=alex).status_code == 200
    assert len(list(tmp_path.rglob("*.ogg"))) == 1


def test_old_rooms_and_their_recordings_are_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_RECORDINGS_DIR", str(tmp_path))
    store = Store()
    client = TestClient(create_app(store))
    old = client.post("/api/rooms", json={"name": "Last month"}).json()["code"]
    still_open = client.post("/api/rooms", json={"name": "Still going"}).json()["code"]
    (tmp_path / old).mkdir()
    (tmp_path / old / "1-take.webm").write_bytes(b"x")
    store.db.execute("UPDATE rooms SET created_at = 0")  # both made a long time ago

    with client.websocket_connect(f"/ws/{still_open}"):
        client.post("/api/rooms", json={"name": "Tonight"})  # making a room sweeps
    assert client.get(f"/api/rooms/{old}").status_code == 404
    assert not (tmp_path / old).exists()
    assert client.get(f"/api/rooms/{still_open}").status_code == 200


def test_responses_carry_security_headers_and_big_bodies_are_refused():
    client = TestClient(create_app(Store()))
    res = client.get("/")
    csp = res.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'self'" in csp and "object-src 'none'" in csp
    assert "'sha256-" in csp and csp.endswith("connect-src 'self' ws://testserver wss://testserver")
    assert res.headers["x-content-type-options"] == "nosniff" and res.headers["x-frame-options"] == "SAMEORIGIN"
    assert "strict-transport-security" not in res.headers  # only over https
    assert client.get("https://testserver/").headers["strict-transport-security"] == "max-age=31536000"

    res = client.post("/api/rooms", json={"name": "x" * 70000})
    assert res.status_code == 413 and res.json() == {"detail": "That request is too big"}
    assert client.post("/api/rooms", json={"name": "Friday"}).status_code == 200


def test_a_socket_that_stops_reading_cant_stall_the_room(monkeypatch):
    import asyncio
    from karaqueue import displays

    class Socket:
        def __init__(self, stuck):
            self.stuck, self.got, self.closed = stuck, [], False

        async def send_json(self, message):
            if self.stuck:
                await asyncio.Event().wait()  # its buffers are full: never returns
            self.got.append(message)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(displays, "SEND_TIMEOUT", 0.2)
    hub, stuck, phone = displays.Hub(Store()), Socket(True), Socket(False)
    hub.connect("ABCDEF", stuck)
    hub.connect("ABCDEF", phone)
    asyncio.run(hub.notify("ABCDEF"))
    assert phone.got == [{"type": "update"}] and stuck.closed
    assert hub.rooms["ABCDEF"] == {phone}
