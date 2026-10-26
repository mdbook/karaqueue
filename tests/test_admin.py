import time

import pytest
from fastapi.testclient import TestClient

from karaqueue.app import create_app
from karaqueue.guards import Logins
from karaqueue.settings import Settings
from karaqueue.store import Error, Store


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_saved_settings_beat_the_environment_which_beats_the_default(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_SONGS_PER_SINGER", "5")
    store = Store()
    cfg = Settings(store)
    assert cfg.get("songs_per_singer") == 5 and cfg.source("songs_per_singer") == "env"
    cfg.set("songs_per_singer", "7")
    assert Settings(store).get("songs_per_singer") == 7  # survives a restart
    cfg.reset("songs_per_singer")
    assert cfg.get("songs_per_singer") == 5
    assert cfg.get("code_length") == 6 and cfg.source("code_length") == "default"
    for bad in ("lots", 0, 2.5, True):
        try:
            cfg.set("songs_per_singer", bad)
            raise AssertionError(bad)
        except Exception as exc:
            assert getattr(exc, "status", None) == 400, bad


def test_first_run_setup_link_works_once_then_the_password_does(monkeypatch):
    monkeypatch.delenv("KARAQUEUE_ADMIN_PASSWORD", raising=False)
    store = Store()
    client = TestClient(create_app(store))
    assert client.get("/api/admin").status_code == 401
    assert client.get("/api/admin/status").json() == {"setup": True, "password_env": False}

    setup = client.app.state.settings.setup_token  # the one the server log prints
    assert client.post("/api/admin/login", json={"password": "x" * 8, "setup": "nope"}).status_code == 403
    assert client.post("/api/admin/login", json={"password": "short", "setup": setup}).status_code == 400
    admin = bearer(client.post("/api/admin/login", json={"password": "karaoke night", "setup": setup}).json()["token"])
    assert client.get("/api/admin", headers=admin).status_code == 200
    assert client.post("/api/admin/login", json={"password": "x" * 8, "setup": setup}).status_code == 403

    # After a restart there's no setup link any more, just the password.
    client = TestClient(create_app(store))
    assert client.get("/api/admin/status").json()["setup"] is False
    assert client.post("/api/admin/login", json={"password": "karaoke nigh"}).status_code == 401
    admin = bearer(client.post("/api/admin/login", json={"password": "karaoke night"}).json()["token"])
    assert client.put("/api/admin/password", headers=admin, json={"password": "new password"}).status_code == 200
    assert client.post("/api/admin/login", json={"password": "new password"}).status_code == 200


def test_the_setup_link_goes_in_a_private_file_never_the_log(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("KARAQUEUE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("KARAQUEUE_DB", str(tmp_path / "karaqueue.db"))
    link_file = tmp_path / "admin-setup-link.txt"
    with caplog.at_level("WARNING", logger="karaqueue"):
        client = TestClient(create_app())
    token = client.app.state.settings.setup_token
    assert token and token not in caplog.text and str(link_file) in caplog.text
    assert link_file.read_text().strip() == "/admin#setup=" + token
    assert link_file.stat().st_mode & 0o077 == 0  # only the server's own user can read it

    # A restart keeps the same link, so it can't be read from an old log line either way.
    assert TestClient(create_app()).app.state.settings.setup_token == token
    client = TestClient(create_app())
    assert client.post("/api/admin/login", json={"password": "karaoke night"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "karaoke night", "setup": token}).status_code == 200
    assert not link_file.exists()


def test_admin_page_cant_pick_programs_or_folders_outside_the_allowed_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    for key in ("MEDIA_DIR", "DOWNLOADS_DIR", "RECORDINGS_DIR", "ALLOWED_FOLDERS"):
        monkeypatch.delenv("KARAQUEUE_" + key, raising=False)
    store = Store()
    client = TestClient(create_app(store))
    admin = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])
    put = lambda key, value: client.put(f"/api/admin/settings/{key}", headers=admin, json={"value": value})  # noqa: E731

    # yt-dlp's path is the environment's call only.
    assert put("ytdlp", "/bin/sh").status_code == 409
    ytdlp = next(s for s in client.get("/api/admin", headers=admin).json()["settings"] if s["key"] == "ytdlp")
    assert ytdlp["env_only"] is True

    # Nothing in the environment: no folders at all.
    songs = tmp_path / "songs"
    songs.mkdir()
    assert put("media_dir", str(songs)).status_code == 403
    assert put("recordings_dir", str(tmp_path / "rec")).status_code == 403
    assert not (tmp_path / "rec").exists()

    # The environment's folders, and folders inside them, are fine; anything else isn't.
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(tmp_path))
    assert put("media_dir", str(songs)).status_code == 200
    assert put("media_dir", "/etc").status_code == 403
    assert put("downloads_dir", str(songs / ".." / ".." / "elsewhere")).status_code == 403
    monkeypatch.setenv("KARAQUEUE_ALLOWED_FOLDERS", str(songs))
    assert put("recordings_dir", str(songs / "rec")).status_code == 200

    # Values saved before these rules are ignored, not trusted.
    store.save_setting("ytdlp", "/bin/sh")
    store.save_setting("media_dir", "/etc")
    cfg = Settings(store)
    assert cfg.get("ytdlp") == "yt-dlp" and cfg.get("media_dir") == str(tmp_path)
    assert cfg.source("media_dir") == "env"
    store.save_setting("code_length", 4)  # fine before the minimum went up to 5
    cfg = Settings(store)
    assert cfg.get("code_length") == 6
    assert sorted(cfg.ignored()) == ["code_length", "media_dir", "ytdlp"]  # warned about at startup


def test_admin_changes_take_effect_without_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    monkeypatch.delenv("KARAQUEUE_MEDIA_DIR", raising=False)
    monkeypatch.setenv("KARAQUEUE_ALLOWED_FOLDERS", str(tmp_path))
    client = TestClient(create_app(Store()))
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    admin = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])

    state = client.get("/api/admin", headers=admin).json()
    key = next(s for s in state["settings"] if s["key"] == "youtube_api_key")
    assert key["secret"] and key["value"] is False
    assert state["library"] is None

    # A guest limit of 1, live.
    assert client.put("/api/admin/settings/songs_per_singer", headers=admin, json={"value": 1}).status_code == 200
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code = room["code"]
    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])
    song = {"title": "Africa", "artist": "Toto"}
    assert client.post(f"/api/rooms/{code}/songs", headers=alex, json=song).status_code == 200
    assert client.post(f"/api/rooms/{code}/songs", headers=alex, json=song).status_code == 409

    # The library turns on when its folder is set, and a typo is caught.
    (tmp_path / "Toto - Africa.mp4").write_bytes(b"x")
    assert client.put("/api/admin/settings/media_dir", headers=admin,
                      json={"value": str(tmp_path / "nope")}).status_code == 400
    state = client.put("/api/admin/settings/media_dir", headers=admin, json={"value": str(tmp_path)}).json()
    assert state["library"] == {"file": 1}
    assert client.get("/api/config").json()["library"] is True
    assert client.delete("/api/admin/settings/media_dir", headers=admin).json()["library"] is None

    # Rooms show up and can be deleted.
    assert [r["code"] for r in state["rooms"]] == [code]
    assert client.delete(f"/api/admin/rooms/{code}", headers=admin).json()["rooms"] == []
    assert client.get(f"/api/rooms/{code}").status_code == 404

    # The password lives in the environment, so the page can't change it.
    assert client.put("/api/admin/password", headers=admin, json={"password": "x" * 10}).status_code == 409


def test_admins_manage_accounts_and_hosts_sign_in_to_start_rooms(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    client = TestClient(create_app(Store()))
    root = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])
    me = client.get("/api/admin", headers=root).json()["me"]
    assert me == {"id": None, "name": "admin", "role": "admin", "session": me["session"]} and len(me["session"]) == 16

    add = lambda name, role, pw="long enough": client.post(  # noqa: E731
        "/api/admin/users", headers=root, json={"name": name, "role": role, "password": pw})
    assert add("Sam", "host").status_code == 200
    assert add("sam", "host").status_code == 409  # names ignore case
    assert add("Kit", "boss").status_code == 400
    assert add("Kit", "admin", "short").status_code == 400
    users = add("Kit", "admin").json()["users"]
    assert [(u["name"], u["role"], u["disabled"]) for u in users] == [("Kit", "admin", False), ("Sam", "host", False)]
    assert all("password" not in u for u in users)
    sam_id, kit_id = users[1]["id"], users[0]["id"]

    def log_in(name, password="long enough"):
        return client.post("/api/admin/login", json={"name": name, "password": password})

    assert log_in("SAM", "wrong password").status_code == 401
    sam = log_in("SAM").json()
    assert (sam["name"], sam["role"]) == ("Sam", "host")
    sam = bearer(sam["token"])
    # A host account can't touch the admin page.
    assert client.get("/api/admin", headers=sam).status_code == 403
    assert client.get("/api/admin/me", headers=sam).json()["role"] == "host"

    # Only account holders start rooms once that's turned on; guests still join freely.
    assert client.put("/api/admin/settings/new_rooms", headers=root, json={"value": "nobody"}).status_code == 400
    client.put("/api/admin/settings/new_rooms", headers=root, json={"value": "accounts"})
    assert client.get("/api/config").json()["accounts_only"] is True
    assert client.post("/api/rooms", json={"name": "Friday"}).status_code == 401
    code = client.post("/api/rooms", headers=sam, json={"name": "Friday"}).json()["code"]
    assert client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).status_code == 200

    # Admin accounts can run the admin page, but not lock themselves out.
    kit_token = log_in("kit").json()["token"]
    kit = bearer(kit_token)
    assert client.patch(f"/api/admin/users/{kit_id}", headers=kit, json={"role": "host"}).status_code == 409
    assert client.patch(f"/api/admin/users/{kit_id}", headers=kit, json={"disabled": True}).status_code == 409
    assert client.delete(f"/api/admin/users/{kit_id}", headers=kit).status_code == 409

    # Turning an account off signs it out at once; a reset password replaces the old one.
    assert client.patch(f"/api/admin/users/{sam_id}", headers=kit, json={"disabled": True}).status_code == 200
    assert client.post("/api/rooms", headers=sam, json={"name": "Again"}).status_code == 401
    assert log_in("Sam").status_code == 403
    client.patch(f"/api/admin/users/{sam_id}", headers=kit, json={"disabled": False, "password": "brand new pw"})
    assert log_in("Sam").status_code == 401
    assert log_in("Sam", "brand new pw").status_code == 200
    client.patch(f"/api/admin/users/{sam_id}", headers=kit, json={"role": "admin", "name": "Samantha"})
    assert client.get("/api/admin", headers=bearer(log_in("samantha", "brand new pw").json()["token"])).status_code == 200

    # Deleting an account ends its sessions too, and the built-in password still works.
    assert client.delete(f"/api/admin/users/{kit_id}", headers=root).status_code == 200
    assert client.get("/api/admin", headers=kit).status_code == 401
    assert client.delete(f"/api/admin/users/{kit_id}", headers=root).status_code == 404
    assert client.get("/api/admin", headers=root).status_code == 200


def test_invite_links_reset_links_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    db = str(tmp_path / "kq.db")
    client = TestClient(create_app(Store(db)))
    phone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"}
    root_token = client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"]
    root = bearer(root_token)

    # An invite makes an account without the admin ever knowing its password, and works once.
    assert client.post("/api/admin/invites", headers=root, json={}).status_code == 400
    assert client.post("/api/admin/invites", headers=root, json={"role": "boss"}).status_code == 400
    made = client.post("/api/admin/invites", headers=root, json={"role": "host"}).json()
    link = made["link"].split("#invite=")[1]
    assert [(i["role"], i["made_by"]) for i in made["invites"]] == [("host", "admin")]
    assert client.post("/api/invites/look", json={"link": link}).json() == {"role": "host", "name": None}
    assert client.post("/api/invites/accept", json={"link": link, "name": "Sam", "password": "short"}).status_code == 400
    sam = client.post("/api/invites/accept", headers=phone, json={"link": link, "name": "Sam", "password": "long enough"}).json()
    assert (sam["name"], sam["role"]) == ("Sam", "host")
    assert client.get("/api/admin/me", headers=bearer(sam["token"])).json()["name"] == "Sam"
    assert client.post("/api/invites/look", json={"link": link}).status_code == 404
    assert client.post("/api/invites/accept", json={"link": link, "name": "Eve", "password": "long enough"}).status_code == 404
    assert client.post("/api/invites/look", json={"link": "made-up"}).status_code == 404

    # A reset link picks a new password and signs the account out everywhere else.
    state = client.get("/api/admin", headers=root).json()
    sam_id = state["users"][0]["id"]
    assert [(s["kind"], s["account"], s["device"]) for s in state["sessions"]] == [
        ("staff", sam_id, "Safari on iPhone"), ("staff", None, "Unknown browser")]
    reset = client.post("/api/admin/invites", headers=root, json={"user": sam_id}).json()["link"].split("#invite=")[1]
    again = client.post("/api/admin/invites", headers=root, json={"user": sam_id}).json()
    assert client.post("/api/invites/look", json={"link": reset}).status_code == 404  # a new link replaces the last
    assert len(again["invites"]) == 1
    reset = again["link"].split("#invite=")[1]
    assert client.post("/api/invites/look", json={"link": reset}).json() == {"role": None, "name": "Sam"}
    fresh = client.post("/api/invites/accept", json={"link": reset, "password": "brand new pw"}).json()["token"]
    assert client.get("/api/admin/me", headers=bearer(sam["token"])).status_code == 401
    assert client.get("/api/admin/me", headers=bearer(fresh)).status_code == 200
    assert client.post("/api/admin/login", json={"name": "Sam", "password": "brand new pw"}).status_code == 200

    # Admins see every sign-in and can end one, or all of an account's.
    sessions = client.get("/api/admin", headers=root).json()["sessions"]
    assert [s["account"] for s in sessions].count(sam_id) == 2
    one = next(s for s in sessions if s["account"] == sam_id)
    left = client.delete(f"/api/admin/sessions/staff/{one['id']}", headers=root).json()["sessions"]
    assert [s["account"] for s in left].count(sam_id) == 1
    assert client.delete(f"/api/admin/users/{sam_id}/sessions", headers=root).status_code == 200
    assert client.get("/api/admin/me", headers=bearer(fresh)).status_code == 401
    assert client.delete("/api/admin/sessions/bogus/1", headers=root).status_code == 404

    # Guest sign-ins are listed too, and signing a guest out everywhere ends them.
    guest = client.post("/api/guests", headers=phone, json={"name": "Rae", "password": "long enough"}).json()["token"]
    state = client.get("/api/admin", headers=root).json()
    rae_id = state["guests"][0]["id"]
    assert [(s["kind"], s["account"], s["device"]) for s in state["sessions"] if s["kind"] == "guest"] == [
        ("guest", rae_id, "Safari on iPhone")]
    client.delete(f"/api/admin/guests/{rae_id}/sessions", headers=root)
    assert client.get("/api/guests/me", headers=bearer(guest)).status_code == 401

    # Sign-ins outlast a restart, but not a sign-out or a new admin password.
    client = TestClient(create_app(Store(db)))
    assert client.get("/api/admin", headers=root).status_code == 200
    client.post("/api/admin/logout", headers=root)
    assert client.get("/api/admin", headers=root).status_code == 401
    root = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "a new password")
    client = TestClient(create_app(Store(db)))
    assert client.get("/api/admin", headers=root).status_code == 401

    # Only admins make links; a link to a turned-off account doesn't sign it in.
    root = bearer(client.post("/api/admin/login", json={"password": "a new password"}).json()["token"])
    sam = bearer(client.post("/api/admin/login", json={"name": "Sam", "password": "brand new pw"}).json()["token"])
    assert client.post("/api/admin/invites", headers=sam, json={"role": "admin"}).status_code == 403
    reset = client.post("/api/admin/invites", headers=root, json={"user": sam_id}).json()["link"].split("#invite=")[1]
    client.patch(f"/api/admin/users/{sam_id}", headers=root, json={"disabled": True})
    assert client.post("/api/invites/accept", json={"link": reset, "password": "another one"}).status_code == 403
    invite_id = client.get("/api/admin", headers=root).json()["invites"][0]["id"]
    assert client.delete(f"/api/admin/invites/{invite_id}", headers=root).json()["invites"] == []


def test_links_and_sign_ins_end_with_the_access_behind_them(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    db = str(tmp_path / "kq.db")
    store = Store(db)
    client = TestClient(create_app(store))
    root = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])
    kit_id = client.post("/api/admin/users", headers=root, json={"name": "Kit", "role": "admin", "password": "long enough"}).json()["users"][0]["id"]
    kit = bearer(client.post("/api/admin/login", json={"name": "Kit", "password": "long enough"}).json()["token"])
    links = lambda: [(i["role"], i["made_by"]) for i in client.get("/api/admin", headers=root).json()["invites"]]

    # Links a person made stop working once they can't make them any more.
    client.post("/api/admin/invites", headers=kit, json={"role": "admin"})
    client.post("/api/admin/invites", headers=root, json={"role": "host"})
    client.patch(f"/api/admin/users/{kit_id}", headers=root, json={"role": "host"})
    assert links() == [("host", "admin")]
    assert client.post("/api/admin/invites", headers=kit, json={"role": "admin"}).status_code == 403

    # Hosts and guests can't end anyone's sign-in.
    for path in ("/api/admin/sessions/staff/abc", f"/api/admin/users/{kit_id}/sessions", "/api/admin/invites/abc"):
        assert client.delete(path, headers=kit).status_code == 403

    # An account is signed in on at most 10 devices: the oldest goes. And never for more than 90 days.
    tokens = [client.post("/api/admin/login", json={"name": "Kit", "password": "long enough"}).json()["token"] for _ in range(10)]
    assert client.get("/api/admin/me", headers=kit).status_code == 401
    assert client.get("/api/admin/me", headers=bearer(tokens[0])).status_code == 200
    store.db.execute("UPDATE staff_sessions SET created_at = created_at - 91 * 86400 WHERE user_id = ?", (kit_id,))
    assert client.get("/api/admin/me", headers=bearer(tokens[-1])).status_code == 401

    # Taking KARAQUEUE_ADMIN_PASSWORD out signs out everyone who used it, and putting it back counts as new.
    monkeypatch.delenv("KARAQUEUE_ADMIN_PASSWORD")
    client = TestClient(create_app(Store(db)))
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    client = TestClient(create_app(Store(db)))
    assert client.get("/api/admin", headers=root).status_code == 401
    root = bearer(client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"])
    assert links() == []


def test_sign_in_lists_upgrade_from_older_databases(tmp_path):
    import sqlite3
    db = str(tmp_path / "kq.db")
    old = sqlite3.connect(db)  # as a crash halfway through an upgrade left it: a device, but no seen_at
    old.execute("CREATE TABLE guest_sessions (token_hash TEXT PRIMARY KEY, guest_id INTEGER NOT NULL, created_at REAL NOT NULL, "
                "device TEXT NOT NULL DEFAULT '')")
    old.execute("INSERT INTO guest_sessions VALUES ('ab' || hex(randomblob(31)), 1, 1e12, 'Firefox on Linux')")
    # And links from before they knew who made them: they still work, and go when the built-in admin's do.
    old.execute("CREATE TABLE invites (token_hash TEXT PRIMARY KEY, role TEXT, user_id INTEGER, made_by TEXT NOT NULL, "
                "created_at REAL NOT NULL, expires_at REAL NOT NULL)")
    old.execute("INSERT INTO invites VALUES ('cd' || hex(randomblob(31)), 'host', NULL, 'admin', 1, 1e12)")
    old.commit()
    old.close()
    store = Store(db)
    assert [(s["kind"], s["device"], s["seen_at"]) for s in store.sessions()] == [("guest", "Firefox on Linux", 1e12)]
    assert [(i["role"], i["made_by"]) for i in store.invites()] == [("host", "admin")]
    store.drop_invites_by(None)
    assert store.invites() == []


def test_admin_password_change_signs_out_the_old_password_elsewhere():
    cfg = Settings(Store())
    cfg.set_password("first password")
    a, b = cfg.log_in(None, "first password"), cfg.log_in(None, "first password")
    cfg.set_password("second password", cfg.whoami(a))
    assert cfg.whoami(a) is not None and cfg.whoami(b) is None


def test_hosts_set_what_their_room_allows():
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], bearer(room["host_token"])
    alex = bearer(client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"])
    assert client.put(f"/api/rooms/{code}/options", headers=alex, json={"youtube": False}).status_code == 403
    assert client.put(f"/api/rooms/{code}/options", headers=host,
                      json={"youtube": False, "songs_per_singer": 2}).status_code == 200
    assert client.get(f"/api/rooms/{code}").json()["options"] == {
        "songs_per_singer": 2, "youtube": False, "library": True, "limit": 2}

    yt = {"title": "Africa", "artist": "Toto", "video": "https://youtu.be/FTQbiNvZqaY"}
    assert client.post(f"/api/rooms/{code}/songs", headers=alex, json=yt).status_code == 403
    assert client.get(f"/api/rooms/{code}/search?q=toto", headers=alex).status_code == 403
    plain = {"title": "Africa", "artist": "Toto"}
    for status in (200, 200, 409):
        assert client.post(f"/api/rooms/{code}/songs", headers=alex, json=plain).status_code == status
    # The host can still add anything.
    assert client.post(f"/api/rooms/{code}/songs", headers=host, json={**yt, "singer_id": 1}).status_code == 200

    client.put(f"/api/rooms/{code}/options", headers=host, json={"songs_per_singer": None})
    assert client.get(f"/api/rooms/{code}").json()["options"]["limit"] == 10


def test_wrong_passwords_get_a_quick_429_instead_of_a_slow_server(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    client = TestClient(create_app(Store()))
    start = time.time()
    for _ in range(10):
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert time.time() - start < 5  # each wrong try used to hold a thread for a second
    res = client.post("/api/admin/login", json={"password": "correct horse"})
    assert res.status_code == 429 and "Try again in 15 minutes" in res.json()["detail"]


def test_guessing_one_name_from_many_addresses_is_limited_too():
    logins = Logins(per_address=2, per_name=3)
    for address in ("a", "b", "c"):
        logins.failed([("address", address), ("name", "sam")])
    logins.check([("address", "d"), ("name", "kit")])
    with pytest.raises(Error) as caught:
        logins.check([("address", "d"), ("name", "sam")])
    assert caught.value.status == 429
    logins.passed(("name", "sam"))
    logins.check([("address", "d"), ("name", "sam")])
