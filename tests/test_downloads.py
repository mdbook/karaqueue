import stat
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from karaqueue.app import create_app
from karaqueue.downloads import QUEUE, Downloads, Saver, file_stem
from karaqueue.settings import Settings
from karaqueue.store import Error, Store

# Stands in for yt-dlp: "downloads" by writing the file named after -o, and answers searches.
FAKE = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
# Tests hold a download "in flight" by making this file, and let it finish by deleting it.
while os.path.exists(os.environ.get("FAKE_HOLD", "/nonexistent")):
    time.sleep(0.05)
if os.environ.get("FAKE_LOG"):
    open(os.environ["FAKE_LOG"], "a").write(" ".join(args[-1:]) + "\\n")
if "-J" in args:
    print(json.dumps({{"entries": [
        {{"id": "dQw4w9WgXcQ", "title": "Africa (Karaoke)", "channel": "Sing King",
          "thumbnails": [{{"url": "https://i.ytimg.com/x.jpg"}}]}},
        {{"id": "not-an-id", "title": "junk"}}]}}))
elif os.environ.get("FAKE_FAIL"):
    sys.exit("ERROR: could not write /secret/server/path/file")
else:
    out = args[args.index("-o") + 1].replace("%(ext)s", "mp4").replace("%%", "%")
    data = b"v" * int(os.environ["FAKE_SIZE"]) if os.environ.get("FAKE_SIZE") else b"video"
    # Like yt-dlp: a video over --max-filesize is skipped, not an error.
    if ("--max-filesize" not in args or os.environ.get("FAKE_IGNORE_MAX")
            or len(data) <= int(args[args.index("--max-filesize") + 1])):
        open(out, "wb").write(data)
"""


def fake_ytdlp(tmp_path):
    path = tmp_path / "yt-dlp"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def wait_for(check, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_youtube_songs_are_downloaded_and_played_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("KARAQUEUE_YTDLP", fake_ytdlp(tmp_path))
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}

    assert client.get("/api/config").json()["youtube_search"] is True  # no API key needed
    hits = client.get(f"/api/rooms/{code}/search?q=africa", headers=host).json()
    assert [h["video_id"] for h in hits] == ["dQw4w9WgXcQ"]

    alex = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"]
    assert client.post(f"/api/rooms/{code}/songs", headers={"Authorization": "Bearer " + alex},
                       json={"title": "Africa", "artist": "Toto",
                             "video": "https://youtu.be/dQw4w9WgXcQ"}).status_code == 200
    assert wait_for(lambda: (tmp_path / "dl" / "dQw4w9WgXcQ.mp4").exists())
    client.post(f"/api/rooms/{code}/next", headers=host)
    playing = client.get(f"/api/rooms/{code}").json()["now_playing"]
    assert playing["file"].startswith("/downloads/dQw4w9WgXcQ?room=" + code + "&until=")
    assert client.get(playing["file"]).content == b"video"
    assert client.get("/downloads/dQw4w9WgXcQ").status_code == 403
    assert client.get(client.app.state.song_link("downloads", "../yt-dlp", code)).status_code == 404


def test_screen_waits_for_the_file_and_never_gets_the_embed_while_downloading(tmp_path, monkeypatch):
    hold = tmp_path / "hold"
    hold.touch()
    monkeypatch.setenv("FAKE_HOLD", str(hold))
    monkeypatch.setenv("KARAQUEUE_DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("KARAQUEUE_YTDLP", fake_ytdlp(tmp_path))
    store = Store()
    client = TestClient(create_app(store))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}
    alex = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()
    # Queued straight into the store, as if downloads were off back then: the room's state still fetches it.
    store.add_song(code, room["host_token"], "Africa", "Toto", alex["singer_id"], "dQw4w9WgXcQ")
    store.add_song(code, room["host_token"], "Rosanna", "Toto", alex["singer_id"], "aaaaaaaaaaa")
    client.post(f"/api/rooms/{code}/next", headers=host)

    playing = client.get(f"/api/rooms/{code}").json()["now_playing"]
    assert playing["download"] == "pending" and "file" not in playing
    hold.unlink()
    assert wait_for(lambda: "file" in client.get(f"/api/rooms/{code}").json()["now_playing"])
    assert wait_for(lambda: (tmp_path / "dl" / "aaaaaaaaaaa.mp4").exists())  # the next one came along too


def test_searches_are_remembered_and_new_downloads_are_capped_per_room(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    monkeypatch.setenv("FAKE_LOG", str(calls))
    monkeypatch.setenv("KARAQUEUE_DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("KARAQUEUE_YTDLP", fake_ytdlp(tmp_path))
    monkeypatch.setenv("KARAQUEUE_DOWNLOADS_PER_HOUR", "2")
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}

    first = client.get(f"/api/rooms/{code}/search?q=Africa", headers=host).json()
    assert client.get(f"/api/rooms/{code}/search?q=%20africa%20", headers=host).json() == first
    assert calls.read_text().count("ytsearch") == 1

    me = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()
    add = lambda video: client.post(f"/api/rooms/{code}/songs", headers=host, json={
        "title": "Africa", "artist": "Toto", "video": video, "singer_id": me["singer_id"]}).status_code
    assert add("dQw4w9WgXcQ") == 200 and add("aaaaaaaaaaa") == 200
    assert add("bbbbbbbbbbb") == 200  # past the cap it still goes in the queue...
    assert wait_for(lambda: (tmp_path / "dl" / "dQw4w9WgXcQ.mp4").exists())
    assert wait_for(lambda: (tmp_path / "dl" / "aaaaaaaaaaa.mp4").exists())
    assert "bbbbbbbbbbb" not in calls.read_text()  # ...but doesn't download yet
    assert add("dQw4w9WgXcQ") == 200  # already on the server, so it doesn't count
    other = client.post("/api/rooms", json={"name": "Saturday"}).json()
    sam = client.post(f"/api/rooms/{other['code']}/singers", json={"name": "Sam"}).json()
    assert client.post(f"/api/rooms/{other['code']}/songs", headers={"Authorization": "Bearer " + sam["token"]},
                       json={"title": "x", "artist": "y", "video": "bbbbbbbbbbb"}).status_code == 200


def test_a_failed_download_tells_the_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("KARAQUEUE_YTDLP", "/bin/false")
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}
    client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"})
    client.post(f"/api/rooms/{code}/songs", headers=host,
                json={"title": "Africa", "artist": "Toto", "video": "dQw4w9WgXcQ", "singer_id": 1})
    client.post(f"/api/rooms/{code}/next", headers=host)
    assert wait_for(lambda: client.get(f"/api/rooms/{code}").json()["now_playing"]["download"] == "failed")


def test_cache_drops_the_least_recently_played(tmp_path):
    downloads = Downloads(str(tmp_path), max_bytes=10)
    for i, name in enumerate(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]):
        path = tmp_path / (name + ".mp4")
        path.write_bytes(b"12345")
        path.touch()
        time.sleep(0.01)
    downloads.touch(tmp_path / "aaaaaaaaaaa.mp4")  # played just now
    downloads._trim()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aaaaaaaaaaa.mp4", "ccccccccccc.mp4"]


def test_songs_saved_from_youtube_join_the_library_for_good(tmp_path, monkeypatch):
    songs = tmp_path / "songs"
    songs.mkdir()
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(songs))
    monkeypatch.setenv("KARAQUEUE_YTDLP", fake_ytdlp(tmp_path))
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}
    admin = {"Authorization": "Bearer " + client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"]}
    # No downloads folder, but yt-dlp and a library: search and saving both work.
    assert client.get("/api/config").json()["saving"] is True
    assert client.get("/api/admin/youtube?q=africa", headers=admin).json()[0]["video_id"] == "dQw4w9WgXcQ"

    # A host saves one: it lands in a YouTube folder in the library, named so the library reads it right.
    job = client.post(f"/api/rooms/{code}/saves", headers=host,
                      json={"video": "https://youtu.be/dQw4w9WgXcQ", "title": "Africa", "artist": "To/to"}).json()
    assert job["state"] in ("waiting", "saving", "saved")
    saved = songs / "YouTube" / "To to - Africa [dQw4w9WgXcQ].mp4"
    assert wait_for(lambda: saved.exists())
    assert wait_for(lambda: client.get(f"/api/rooms/{code}/saves", headers=host).json()[0]["state"] == "saved")
    hits = client.get(f"/api/rooms/{code}/library?q=africa", headers=host).json()
    assert hits == [{"id": "YouTube/To to - Africa [dQw4w9WgXcQ].mp4", "title": "Africa", "artist": "To to"}]
    assert not list((songs / "YouTube" / ".saving").iterdir())  # nothing half-done left behind
    # Saving it again is a no-op, and the admin sees every room's saves.
    assert client.post("/api/admin/saves", headers=admin, json={"video": "dQw4w9WgXcQ", "title": "Africa"}).json()["state"] == "saved"
    assert [j["video_id"] for j in client.get("/api/admin/saves", headers=admin).json()] == ["dQw4w9WgXcQ"]

    # Guests can't save, links have to be YouTube, songs need a title.
    alex = {"Authorization": "Bearer " + client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"]}
    assert client.post(f"/api/rooms/{code}/saves", headers=alex, json={"video": "dQw4w9WgXcQ", "title": "x"}).status_code == 403
    assert client.post("/api/admin/saves", headers=admin, json={"video": "https://example.com/x", "title": "x"}).status_code == 400
    assert client.post("/api/admin/saves", headers=admin, json={"video": "aaaaaaaaaaa", "title": "  "}).status_code == 400
    assert client.post("/api/admin/saves", headers=host, json={"video": "aaaaaaaaaaa", "title": "x"}).status_code == 401

    # Admins only: hosts are turned away, the admin page still saves.
    assert client.put("/api/admin/settings/who_saves", headers=admin, json={"value": "admins"}).status_code == 200
    assert client.get("/api/config").json()["saving"] is False
    assert client.post(f"/api/rooms/{code}/saves", headers=host, json={"video": "aaaaaaaaaaa", "title": "x"}).status_code == 403


def test_a_separate_save_folder_is_part_of_the_library(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_SAVE_DIR", str(tmp_path / "saved"))
    monkeypatch.setenv("KARAQUEUE_YTDLP", fake_ytdlp(tmp_path))
    monkeypatch.setenv("KARAQUEUE_DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("KARAQUEUE_SAVES_PER_HOUR", "1")
    log = tmp_path / "calls"
    monkeypatch.setenv("FAKE_LOG", str(log))
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}
    # Already in the downloads folder: copied over, not downloaded again.
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "dQw4w9WgXcQ.mp4").write_bytes(b"cached")
    client.post(f"/api/rooms/{code}/saves", headers=host, json={"video": "dQw4w9WgXcQ", "title": "Africa (Karaoke)"})
    saved = tmp_path / "saved" / "Africa (Karaoke) [dQw4w9WgXcQ].mp4"
    assert wait_for(lambda: saved.exists())
    assert saved.read_bytes() == b"cached" and not log.exists()
    assert client.get("/api/config").json()["library"] is True
    assert client.get(f"/api/rooms/{code}/library?q=africa", headers=host).json()[0]["title"] == "Africa"
    # Saves have their own, smaller count per room an hour.
    assert client.post(f"/api/rooms/{code}/saves", headers=host, json={"video": "aaaaaaaaaaa", "title": "x"}).status_code == 429


def test_hostile_titles_make_safe_short_names():
    for title in ("..", "../../etc/passwd", "a/b\\c", "‮evil.exe", "   "):
        stem = file_stem("dQw4w9WgXcQ", title, "..")
        assert "/" not in stem and "\\" not in stem and "‮" not in stem and not stem.startswith(".")
    long = file_stem("dQw4w9WgXcQ", "日本語のとても長い曲名" * 20, "歌手" * 50)
    assert len((long + ".webm").encode()) <= 255 and long.endswith(" [dQw4w9WgXcQ]")
    long.encode().decode()  # cut on a character boundary


def test_a_title_naming_another_video_doesnt_count_as_that_video_saved(tmp_path):
    (tmp_path / "x [AAAAAAAAAAA]. y [BBBBBBBBBBB].mp4").write_bytes(b"v")
    assert Saver.saved(tmp_path, "AAAAAAAAAAA") is None
    assert Saver.saved(tmp_path, "BBBBBBBBBBB").name == "x [AAAAAAAAAAA]. y [BBBBBBBBBBB].mp4"


def save_and_wait(saver, folder, video="dQw4w9WgXcQ"):
    saver.save(folder, video, "Africa")
    assert wait_for(lambda: saver.jobs[video]["state"] in ("saved", "failed"))
    return saver.jobs[video]


def test_saves_stop_at_the_size_cap_the_disk_floor_and_a_full_queue(tmp_path, monkeypatch):
    folder = tmp_path / "100% songs"  # a % in the folder can't confuse yt-dlp's -o template
    folder.mkdir()
    ytdlp = fake_ytdlp(tmp_path)
    assert save_and_wait(Saver(ytdlp), folder)["state"] == "saved"
    assert (folder / "Africa [dQw4w9WgXcQ].mp4").exists()

    monkeypatch.setenv("FAKE_SIZE", str(2 * 1024 ** 2))
    job = save_and_wait(Saver(ytdlp, max_bytes=1024 ** 2), folder, "aaaaaaaaaaa")
    assert job["state"] == "failed" and "under 1 MB" in job["error"]
    # Checked again after the download, in case yt-dlp let a big one through anyway.
    monkeypatch.setenv("FAKE_IGNORE_MAX", "1")
    job = save_and_wait(Saver(ytdlp, max_bytes=1024 ** 2), folder, "aaaaaaaaaab")
    assert job["state"] == "failed" and "bigger than 1 MB" in job["error"]
    # And a copy already in the downloads folder is checked before it's copied.
    big = tmp_path / "big.mp4"
    big.write_bytes(b"v" * 2 * 1024 ** 2)
    saver = Saver(ytdlp, max_bytes=1024 ** 2)
    saver.save(folder, "aaaaaaaaaac", "Africa", cached=big)
    assert wait_for(lambda: saver.jobs["aaaaaaaaaac"]["state"] == "failed")
    assert "bigger than 1 MB" in saver.jobs["aaaaaaaaaac"]["error"]
    monkeypatch.delenv("FAKE_SIZE")
    monkeypatch.delenv("FAKE_IGNORE_MAX")
    job = save_and_wait(Saver(ytdlp, keep_free=10 ** 18), folder, "bbbbbbbbbbb")
    assert job["state"] == "failed" and "disk is nearly full" in job["error"]
    # The download itself can fill the disk: plenty of room before, too little after.
    free = iter([10 ** 12, 0])
    with monkeypatch.context() as m:
        m.setattr("karaqueue.downloads.shutil.disk_usage", lambda path: SimpleNamespace(free=next(free, 0)))
        job = save_and_wait(Saver(ytdlp), folder, "bbbbbbbbbbc")
    assert job["state"] == "failed" and "disk is nearly full" in job["error"]
    assert not list(folder.glob("*bbbbbbbbbbc*"))

    # A failed download leaves nothing half-done, and doesn't tell a host the server's paths.
    monkeypatch.setenv("FAKE_FAIL", "1")
    job = save_and_wait(Saver(ytdlp), folder, "ccccccccccc")
    assert job["state"] == "failed" and "/secret" not in job["error"]
    monkeypatch.delenv("FAKE_FAIL")
    assert list((folder / ".saving").iterdir()) == []

    hold = tmp_path / "hold"
    hold.touch()
    monkeypatch.setenv("FAKE_HOLD", str(hold))
    saver = Saver(ytdlp)
    try:
        ids = [f"q{n:010d}" for n in range(QUEUE + 1)]
        saver.save(folder, ids[0], "x")
        # Until the first one starts saving it still counts as waiting, so let it start first.
        assert wait_for(lambda: saver.jobs[ids[0]]["state"] == "saving")
        for video in ids[1:]:
            saver.save(folder, video, "x")
        with pytest.raises(Error) as caught:
            saver.save(folder, "zzzzzzzzzzz", "x")
        assert caught.value.status == 429
        assert saver.save(folder, ids[5], "x")["state"] == "waiting"  # one already waiting is fine
    finally:
        hold.unlink()


def test_save_folders_must_be_one_full_allowed_path(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ALLOWED_FOLDERS", str(tmp_path))
    cfg = Settings(Store())
    for bad, status in ((":" + str(tmp_path), 400), (str(tmp_path) + ":" + str(tmp_path), 400), (":", 400),
                        ("relative/dir", 400), ("/etc", 403)):
        for key in ("save_dir", "downloads_dir", "recordings_dir"):
            with pytest.raises(Error) as caught:
                cfg.set(key, bad)
            assert caught.value.status == status, (key, bad)
    cfg.set("save_dir", " " + str(tmp_path / "saved") + " ")
    assert cfg.get("save_dir") == str(tmp_path / "saved") and (tmp_path / "saved").is_dir()


def test_a_full_queue_doesnt_use_up_a_rooms_saves_and_saves_never_show_another_room(tmp_path, monkeypatch):
    songs = tmp_path / "songs"
    songs.mkdir()
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(songs))
    monkeypatch.setenv("KARAQUEUE_YTDLP", fake_ytdlp(tmp_path))
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    monkeypatch.setenv("KARAQUEUE_SAVES_PER_HOUR", "1")
    hold = tmp_path / "hold"
    hold.touch()
    monkeypatch.setenv("FAKE_HOLD", str(hold))
    client = TestClient(create_app(Store()))
    admin = {"Authorization": "Bearer " + client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"]}
    rooms = [client.post("/api/rooms", json={"name": n}).json() for n in ("One", "Two")]
    (one, one_host), (two, two_host) = [(r["code"], {"Authorization": "Bearer " + r["host_token"]}) for r in rooms]
    try:
        # Room one starts a save; room two asking for the same video doesn't learn room one's code.
        assert client.post(f"/api/rooms/{one}/saves", headers=one_host, json={"video": "dQw4w9WgXcQ", "title": "x"}).status_code == 200
        same = client.post(f"/api/rooms/{two}/saves", headers=two_host, json={"video": "dQw4w9WgXcQ", "title": "x"}).json()
        assert "room" not in same and one not in str(same)
        assert wait_for(lambda: client.get("/api/admin/saves", headers=admin).json()[0]["state"] == "saving")
        # Admins aren't held to the hourly limit: they fill the queue.
        for n in range(QUEUE):
            assert client.post("/api/admin/saves", headers=admin, json={"video": f"q{n:010d}", "title": "x"}).status_code == 200
        # A full queue turns room two away without using its one save this hour.
        assert client.post(f"/api/rooms/{two}/saves", headers=two_host, json={"video": "aaaaaaaaaaa", "title": "x"}).status_code == 429
    finally:
        hold.unlink()
    assert wait_for(lambda: all(j["state"] == "saved" for j in client.get("/api/admin/saves", headers=admin).json()), 30)
    assert client.post(f"/api/rooms/{two}/saves", headers=two_host, json={"video": "aaaaaaaaaaa", "title": "x"}).status_code == 200
    assert client.post(f"/api/rooms/{two}/saves", headers=two_host, json={"video": "bbbbbbbbbbb", "title": "x"}).status_code == 429
    # A title with a lone surrogate (hand-made JSON) saves instead of crashing.
    res = client.post("/api/admin/saves", headers={**admin, "Content-Type": "application/json"},
                      content='{"video": "ccccccccccc", "title": "Afr\\ud800ica"}')
    assert res.status_code == 200 and res.json()["title"] == "Africa"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are a POSIX thing")
def test_a_stuck_download_ends_with_its_helpers_and_leftovers_get_swept(tmp_path):
    import os
    import subprocess

    from karaqueue.downloads import HALF_DONE, run, sweep
    pid_file = tmp_path / "child.pid"
    # Like yt-dlp waiting on the ffmpeg it started to merge video and sound.
    script = f"sleep 30 & echo $! > {pid_file}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run(["sh", "-c", script], timeout=1)
    child = int(pid_file.read_text())

    def gone():
        try:
            return "Z" in open(f"/proc/{child}/stat").read().split()[2]
        except (FileNotFoundError, ProcessLookupError):  # reaped, maybe between open and read
            return True
    assert wait_for(gone)

    old, new = time.time() - 7200, time.time()
    for name, when in (("abcdefghijk.mp4.part", old), ("abcdefghijk.f137.mp4", old), ("bcdefghijkl.mp4.part", new),
                       ("cdefghijklm.mp4", old)):
        (tmp_path / name).write_bytes(b"x")
        os.utime(tmp_path / name, (when, when))
    sweep(tmp_path, HALF_DONE)
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix != ".pid") == ["bcdefghijkl.mp4.part", "cdefghijklm.mp4"]
