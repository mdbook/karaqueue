from fastapi.testclient import TestClient

from karaqueue.app import create_app
from karaqueue.store import Store


def admin_client(monkeypatch, tmp_path, store=None):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "library-test")
    for key in ("MEDIA_DIR", "DOWNLOADS_DIR", "RECORDINGS_DIR"):
        monkeypatch.delenv("KARAQUEUE_" + key, raising=False)
    monkeypatch.setenv("KARAQUEUE_ALLOWED_FOLDERS", str(tmp_path))
    client = TestClient(create_app(store or Store()))
    token = client.post("/api/admin/login", json={"password": "library-test"}).json()["token"]
    client.headers["Authorization"] = "Bearer " + token
    return client


def test_folders_songs_fixes_and_deletes(tmp_path, monkeypatch):
    pop, rock = tmp_path / "Pop", tmp_path / "Rock"
    (pop / "deep").mkdir(parents=True)
    rock.mkdir()
    (pop / "Toto - Africa (Karaoke).mp4").write_bytes(b"1234")
    (pop / "deep" / "ABBA - Waterloo.mp3").write_bytes(b"12")
    (pop / "deep" / "ABBA - Waterloo.cdg").write_bytes(b"345")
    (rock / "Queen - Bohemian Rhapsody.webm").write_bytes(b"123456")
    store = Store()
    client = admin_client(monkeypatch, tmp_path, store)

    assert client.post("/api/admin/library/folders", json={"value": str(tmp_path / "nope")}).status_code == 400
    state = client.post("/api/admin/library/folders", json={"value": str(pop)}).json()
    [place] = state["places"]
    assert (place["label"], place["songs"], place["bytes"]) == ("Pop", 2, 9)
    assert state["disks"][0]["free"] > 0
    # One folder: song ids are paths inside it, like before.
    assert [s["id"] for s in client.get("/api/admin/library/songs").json()["songs"]] == \
        ["deep/ABBA - Waterloo.mp3", "Toto - Africa (Karaoke).mp4"]

    client.post("/api/admin/library/folders", json={"value": str(rock)})
    songs = client.get("/api/admin/library/songs?q=queen").json()
    assert songs["total"] == 1 and songs["songs"][0]["id"] == "Rock/Queen - Bohemian Rhapsody.webm"
    assert songs["songs"][0]["folder"] == "Rock" and songs["songs"][0]["bytes"] == 6

    # A guest queued a song back when there was one folder; its old id still plays.
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    host = {"Authorization": "Bearer " + room["host_token"]}
    me = client.post(f"/api/rooms/{room['code']}/singers", json={"name": "Alex"}).json()
    assert client.post(f"/api/rooms/{room['code']}/songs", headers=host, json={
        "title": "Africa", "artist": "Toto", "media": "Toto - Africa (Karaoke).mp4", "singer_id": me["singer_id"]}).status_code == 200
    link = lambda rel: client.app.state.song_link("media", rel, room["code"])
    assert client.get(link("Toto - Africa (Karaoke).mp4")).content == b"1234"
    assert client.get(link("Pop/Toto - Africa (Karaoke).mp4")).content == b"1234"

    # Fix a title: guests' search sees it, the file doesn't change, and it survives a restart.
    fixed = client.patch("/api/admin/library/songs", json={"id": "Pop/Toto - Africa (Karaoke).mp4",
                                                            "title": "  Africa  (Live) ", "artist": "TOTO"}).json()
    assert fixed == {"id": "Pop/Toto - Africa (Karaoke).mp4", "title": "Africa (Live)", "artist": "TOTO", "edited": True}
    assert client.get(f"/api/rooms/{room['code']}/library?q=live", headers=host).json()[0]["artist"] == "TOTO"
    again = admin_client(monkeypatch, tmp_path, store)
    assert again.get("/api/admin/library/songs?q=live").json()["songs"][0]["edited"] is True
    assert client.patch("/api/admin/library/songs", json={"id": "Pop/Toto - Africa (Karaoke).mp4", "title": " "}).status_code == 400
    undone = client.patch("/api/admin/library/songs", json={"id": "Pop/Toto - Africa (Karaoke).mp4", "title": None}).json()
    assert (undone["title"], undone["artist"], undone["edited"]) == ("Africa", "Toto", False)

    # Deleting removes the file, and an MP3+CDG pair goes together.
    assert client.delete("/api/admin/library/songs", params={"id": "Pop/deep/ABBA - Waterloo.mp3"}).status_code == 200
    assert sorted(p.name for p in (pop / "deep").iterdir()) == []
    assert client.delete("/api/admin/library/songs", params={"id": "Pop/../../etc/passwd"}).status_code == 404

    # Removing a folder only stops using it.
    state = client.delete("/api/admin/library/folders", params={"path": str(rock)}).json()
    assert [p["label"] for p in state["places"]] == ["Pop"] and (rock / "Queen - Bohemian Rhapsody.webm").exists()
    client.delete("/api/admin/library/folders", params={"path": str(pop)})
    assert client.get("/api/config").json()["library"] is False
    assert client.get("/api/admin/library/songs").status_code == 404


def test_downloaded_songs_are_listed_by_name_and_deleted_one_at_a_time(tmp_path, monkeypatch):
    client = admin_client(monkeypatch, tmp_path)
    assert client.get("/api/admin/downloads").status_code == 404
    dl = tmp_path / "dl"
    client.put("/api/admin/settings/downloads_dir", json={"value": str(dl)})
    (dl / "dQw4w9WgXcQ.mp4").write_bytes(b"video")
    (dl / "aaaaaaaaaaa.mp4").write_bytes(b"vid")
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    host = {"Authorization": "Bearer " + room["host_token"]}
    me = client.post(f"/api/rooms/{room['code']}/singers", json={"name": "Alex"}).json()
    client.post(f"/api/rooms/{room['code']}/songs", headers=host, json={
        "title": "Never Gonna", "artist": "Rick", "video": "dQw4w9WgXcQ", "singer_id": me["singer_id"]})

    listed = {f["video_id"]: f for f in client.get("/api/admin/downloads").json()}
    assert listed["dQw4w9WgXcQ"]["title"] == "Never Gonna" and listed["dQw4w9WgXcQ"]["bytes"] == 5
    assert listed["aaaaaaaaaaa"]["title"] == ""  # nobody queued it here: shown by its id
    assert client.delete("/api/admin/downloads/dQw4w9WgXcQ").status_code == 200
    assert client.delete("/api/admin/downloads/..%2Fx").status_code == 404
    assert [f["video_id"] for f in client.get("/api/admin/downloads").json()] == ["aaaaaaaaaaa"]
    places = client.get("/api/admin/library").json()["places"]
    assert [(p["kind"], p["bytes"]) for p in places] == [("downloads", 3)]


def test_naming_rules_read_a_folder_the_way_its_files_are_named(tmp_path, monkeypatch):
    discs = tmp_path / "Discs"
    (discs / "ABBA").mkdir(parents=True)
    (discs / "SC8123-05 - ABBA - Waterloo.mp4").write_bytes(b"1")
    (discs / "SC8123-06 - Toto - Africa.mp4").write_bytes(b"1")
    (discs / "ABBA" / "Dancing Queen.mp4").write_bytes(b"1")
    store = Store()
    client = admin_client(monkeypatch, tmp_path, store)
    folder = client.post("/api/admin/library/folders", json={"value": str(discs)}).json()["places"][0]["path"]
    # Out of the box a song in an artist's own folder has no artist.
    names = {s["id"]: (s["title"], s["artist"]) for s in client.get("/api/admin/library/songs").json()["songs"]}
    assert names["ABBA/Dancing Queen.mp4"] == ("Dancing Queen", "")

    assert client.post("/api/admin/library/rules/preview", json={"folder": folder, "pattern": "{artist}"}).status_code == 400
    assert client.post("/api/admin/library/rules/preview", json={"folder": "/etc", "pattern": "{title}"}).status_code == 404
    preview = client.post("/api/admin/library/rules/preview", json={"folder": folder, "pattern": "{skip} - {artist} - {title}"}).json()
    assert (preview["songs"], preview["fits"], preview["misses"]) == (3, 2, ["ABBA/Dancing Queen.mp4"])
    assert preview["examples"][0] == {"file": "SC8123-05 - ABBA - Waterloo.mp4", "title": "Waterloo", "artist": "ABBA", "edited": False}

    # A song fixed by hand keeps its fix whatever the rule says.
    client.patch("/api/admin/library/songs", json={"id": "SC8123-06 - Toto - Africa.mp4", "title": "Africa (Live)", "artist": "Toto"})
    state = client.put("/api/admin/library/rules", json={"folder": folder, "pattern": "{artist}/{title}"}).json()
    assert state["places"][0]["rule"] == "{artist}/{title}"
    names = {s["id"]: (s["title"], s["artist"]) for s in client.get("/api/admin/library/songs").json()["songs"]}
    assert names["ABBA/Dancing Queen.mp4"] == ("Dancing Queen", "ABBA")
    assert names["SC8123-05 - ABBA - Waterloo.mp4"] == ("Waterloo", "ABBA")  # doesn't fit: guessed as before
    assert names["SC8123-06 - Toto - Africa.mp4"] == ("Africa (Live)", "Toto")
    # Rules survive a restart, and an empty pattern goes back to guessing.
    again = admin_client(monkeypatch, tmp_path, store)
    assert again.get("/api/admin/library/songs?q=dancing").json()["songs"][0]["artist"] == "ABBA"
    assert again.put("/api/admin/library/rules", json={"folder": folder, "pattern": ""}).json()["places"][0]["rule"] == ""
    assert again.get("/api/admin/library/songs?q=dancing").json()["songs"][0]["artist"] == ""


def test_fix_many_songs_at_once(tmp_path, monkeypatch):
    pop = tmp_path / "Pop"
    pop.mkdir()
    for name in ("Waterloo - ABBA.mp4", "Africa - Toto.mp4", "Mystery Song.mp4"):
        (pop / name).write_bytes(b"1")
    client = admin_client(monkeypatch, tmp_path)
    client.post("/api/admin/library/folders", json={"value": str(pop)})
    ids = ["Waterloo - ABBA.mp4", "Africa - Toto.mp4", "Mystery Song.mp4", "gone.mp4"]

    swapped = client.post("/api/admin/library/songs/fix", json={"ids": ids, "action": "swap"}).json()["songs"]
    # A song with no artist has nothing to swap, and an unknown id is skipped.
    assert [(s["title"], s["artist"]) for s in swapped] == [("Waterloo", "ABBA"), ("Africa", "Toto")]
    # An empty artist box is a slip, not "wipe every artist".
    assert client.post("/api/admin/library/songs/fix", json={"ids": ids, "action": "artist", "artist": "  "}).status_code == 400
    one = client.post("/api/admin/library/songs/fix", json={"ids": ids[2:3], "action": "artist", "artist": " Unknown  Band "}).json()
    assert one["songs"] == [{"id": "Mystery Song.mp4", "title": "Mystery Song", "artist": "Unknown Band", "edited": True}]
    assert client.get("/api/admin/library/songs?q=unknown").json()["total"] == 1
    undone = client.post("/api/admin/library/songs/fix", json={"ids": ids, "action": "undo"}).json()["songs"]
    assert [s["edited"] for s in undone] == [False, False, False]
    assert client.post("/api/admin/library/songs/fix", json={"ids": ids, "action": "rm"}).status_code == 400


def test_look_up_a_song_on_musicbrainz(tmp_path, monkeypatch):
    import io, json
    from karaqueue import media
    asked = []

    def fake(req, timeout):
        asked.append(req.full_url)
        assert "KaraQueue" in req.get_header("User-agent")
        return io.BytesIO(json.dumps({"recordings": [
            {"title": "Dancing Queen", "artist-credit": [{"name": "ABBA", "joinphrase": ""}],
             "releases": [{"date": "1976-08-16"}, {"date": "1992"}]},
            {"title": "Dancing Queen", "artist-credit": [{"name": "ABBA"}]},
            {"title": "Dancing Queen", "artist-credit": [{"name": "A*Teens"}]}]}).encode())
    monkeypatch.setattr(media.urllib.request, "urlopen", fake)
    monkeypatch.setattr(media, "_asked", [0.0])
    client = admin_client(monkeypatch, tmp_path)
    found = client.get("/api/admin/library/lookup", params={"title": "dancing queen", "artist": "abba"}).json()["matches"]
    assert found == [{"title": "Dancing Queen", "artist": "ABBA", "year": "1976"},
                     {"title": "Dancing Queen", "artist": "A*Teens", "year": ""}]
    assert "artist%3A%22abba%22" in asked[0]
    assert client.put("/api/admin/settings/song_lookup", json={"value": "off"}).status_code == 200
    assert client.get("/api/admin/library/lookup", params={"title": "x"}).status_code == 404
    assert len(asked) == 1
