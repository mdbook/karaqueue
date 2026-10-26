import shutil
import subprocess
import threading
import time
import zipfile
from urllib.parse import unquote

import anyio
import pytest
from fastapi.testclient import TestClient

from karaqueue.app import STATIC, create_app
from karaqueue.media import Library, by_rule, naming_rule, parse
from karaqueue.store import Store


def test_file_names_become_title_and_artist():
    assert parse("Toto - Africa (Karaoke Version).mp4") == ("Africa", "Toto")
    assert parse("SC8123-05 - Journey - Don't Stop Believin' [Karaoke].mp4") == ("Don't Stop Believin'", "Journey")
    assert parse("Bohemian_Rhapsody.webm") == ("Bohemian Rhapsody", "")


def test_naming_rules_read_names_in_one_pass():
    rule = lambda pattern, rel: by_rule(naming_rule(pattern), rel)
    assert rule("{skip} - {artist} - {title}", "SC8123-05 - ABBA - Waterloo.mp4") == ("Waterloo", "ABBA")
    assert rule("{artist} - {title}", "AC_DC  -  Back In Black - Live.mp4") == ("Back In Black - Live", "AC DC")
    assert rule("{artist}/{skip}. {title}", "Pop/Toto/03. Africa (Karaoke).mp4") == ("Africa", "Toto")
    assert rule("Karaoke/{artist}/{title}", "karaoke/Toto/Africa.mp4") == ("Africa", "Toto")
    assert rule("{title} [{artist}]", "Africa [Toto].mp3") == ("Africa", "Toto")
    assert rule("{artist}/{title}", "Africa.mp4") is None  # no folder to read the artist from
    assert rule("{title} ({artist})", "Africa.mp4") is None
    for bad in ("{artist}{title}", "{title} {nope}", "{artist}", "{title} {title}"):
        with pytest.raises(ValueError):
            naming_rule(bad)
    # A pattern full of parts against a long name that doesn't fit used to backtrack for seconds.
    slow = naming_rule(" ".join(["{skip}"] * 30 + ["{title}", "x"]))
    started = time.monotonic()
    assert by_rule(slow, "a " * 120 + ".mp4") is None
    assert time.monotonic() - started < 0.5


def test_library_only_serves_what_it_listed(tmp_path):
    (tmp_path / "Toto - Africa.mp4").write_bytes(b"video")
    (tmp_path / "notes.txt").write_text("not a song")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "Secret - Song.mp4").write_bytes(b"x")
    library = Library(str(tmp_path))
    assert [e["id"] for e in library.search("africa")] == ["Toto - Africa.mp4"]
    assert library.search("secret") == []
    assert library.path("notes.txt") is None
    assert library.path("../etc/passwd") is None


def test_queue_a_song_from_the_library(tmp_path, monkeypatch):
    (tmp_path / "Pop").mkdir()
    (tmp_path / "Pop" / "Toto - Africa.mp4").write_bytes(b"video bytes")
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(tmp_path))
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code = room["code"]
    alex = {"Authorization": "Bearer " + client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"]}

    assert client.get("/api/config").json()["library"] is True
    assert client.get(f"/api/rooms/{code}/library?q=toto").status_code == 401
    [hit] = client.get(f"/api/rooms/{code}/library?q=toto", headers=alex).json()
    assert hit == {"id": "Pop/Toto - Africa.mp4", "title": "Africa", "artist": "Toto"}

    bad = {"title": "x", "artist": "y", "media": "Pop/nope.mp4"}
    assert client.post(f"/api/rooms/{code}/songs", headers=alex, json=bad).status_code == 400
    song = {"title": hit["title"], "artist": hit["artist"], "media": hit["id"]}
    assert client.post(f"/api/rooms/{code}/songs", headers=alex, json=song).status_code == 200
    assert client.get(f"/api/rooms/{code}/me", headers=alex).json()["songs"][0]["media"] == hit["id"]

    # Files only play from the signed link the room hands out for the song that's on.
    assert client.get("/media/Pop/Toto - Africa.mp4").status_code == 403
    host = {"Authorization": "Bearer " + room["host_token"]}
    client.post(f"/api/rooms/{code}/next", headers=host)
    link = client.get(f"/api/rooms/{code}").json()["now_playing"]["file"]
    assert link.startswith("/media/Pop/Toto%20-%20Africa.mp4?room=" + code)
    res = client.get(link, headers={"Range": "bytes=0-4"})
    assert res.status_code == 206 and res.content == b"video"
    sign = client.app.state.song_link
    assert client.get(sign("media", "Pop/../../secrets", code)).status_code == 404

    # A link is for one file in one room, and it runs out.
    other = client.post("/api/rooms", json={"name": "Saturday"}).json()["code"]
    assert client.get(link.replace("room=" + code, "room=" + other)).status_code == 403
    assert client.get(link.replace("Africa", "Rosanna")).status_code == 403
    assert client.get(link.replace("sig=", "sig=0")).status_code == 403
    monkeypatch.setattr("karaqueue.app.time.time", lambda: 10 ** 11)
    assert client.get(link).status_code == 403


def test_mp3_cdg_pairs_loose_and_zipped(tmp_path, monkeypatch):
    (tmp_path / "Toto - Africa.mp3").write_bytes(b"music")
    (tmp_path / "Toto - Africa.CDG").write_bytes(b"lyrics")
    with zipfile.ZipFile(tmp_path / "SC1234-01 - Queen - Bohemian Rhapsody.zip", "w") as z:
        z.writestr("track.mp3", b"zipped music")
        z.writestr("track.cdg", b"zipped lyrics")
    with zipfile.ZipFile(tmp_path / "Just - Audio.zip", "w") as z:
        z.writestr("track.mp3", b"no lyrics, so not a karaoke song")
    (tmp_path / "Old - Movie.avi").write_bytes(b"avi")
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(tmp_path))
    store = Store()
    client = TestClient(create_app(store))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code, host = room["code"], {"Authorization": "Bearer " + room["host_token"]}

    hits = client.get(f"/api/rooms/{code}/library?q=a", headers=host).json()
    ids = {h["id"] for h in hits}
    assert {"Toto - Africa.mp3", "SC1234-01 - Queen - Bohemian Rhapsody.zip"} <= ids
    assert "Just - Audio.zip" not in ids and "Toto - Africa.CDG" not in ids
    assert ("Old - Movie.avi" in ids) == bool(shutil.which("ffmpeg"))

    link = lambda rel: client.app.state.song_link("media", rel, code)
    assert client.get(link("Toto - Africa.mp3") + "&part=cdg").content == b"lyrics"
    res = client.get(link("SC1234-01 - Queen - Bohemian Rhapsody.zip"))
    assert res.content == b"zipped music" and res.headers["content-type"] == "audio/mpeg"
    assert client.get(link("SC1234-01 - Queen - Bohemian Rhapsody.zip") + "&part=cdg").content == b"zipped lyrics"

    alex = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"]
    client.post(f"/api/rooms/{code}/songs", headers={"Authorization": "Bearer " + alex},
                json={"title": "Bohemian Rhapsody", "artist": "Queen",
                      "media": "SC1234-01 - Queen - Bohemian Rhapsody.zip"})
    client.post(f"/api/rooms/{code}/next", headers=host)
    assert client.get(f"/api/rooms/{code}").json()["now_playing"]["cdg"] is True


def test_old_formats_are_left_out_without_ffmpeg(tmp_path):
    (tmp_path / "Old - Movie.avi").write_bytes(b"avi")
    assert Library(str(tmp_path), ffmpeg=None).search("movie") == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_old_formats_stream_as_mp4(tmp_path):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48",
                    "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "mpeg4", "-c:a", "mp2",
                    str(tmp_path / "Old - Movie.avi")], check=True)
    library = Library(str(tmp_path))
    out = b"".join(library.convert("Old - Movie.avi"))
    assert out[4:8] == b"ftyp" and len(out) > 1000


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_only_so_many_old_videos_convert_at_once(tmp_path, monkeypatch):
    (tmp_path / "Old - Movie.avi").write_bytes(b"avi")
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(tmp_path))
    monkeypatch.setenv("KARAQUEUE_CONVERSIONS", "1")
    started, finish = threading.Event(), threading.Event()

    def convert(self, rel):
        started.set()
        yield b"mp4"
        finish.wait(10)  # still playing until the test says so
    monkeypatch.setattr(Library, "convert", convert)
    client = TestClient(create_app(Store()))
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    link = client.app.state.song_link("media", "Old - Movie.avi", code)

    first = []
    watching = threading.Thread(target=lambda: first.append(client.get(link)))
    watching.start()
    assert started.wait(10)
    assert client.get(link).status_code == 503  # the first one is still going
    finish.set()
    watching.join(10)
    assert first[0].content == b"mp4"
    assert client.get(link).status_code == 200  # its slot was freed when it ended


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_screen_that_leaves_mid_video_frees_its_conversion(tmp_path, monkeypatch):
    (tmp_path / "Old - Movie.avi").write_bytes(b"avi")
    monkeypatch.setenv("KARAQUEUE_MEDIA_DIR", str(tmp_path))
    monkeypatch.setenv("KARAQUEUE_CONVERSIONS", "1")
    stopped = []  # chunks each conversion sent before it was stopped
    # Holding on to every stream stands in for a live server, where a dropped one can sit in a
    # reference cycle until a full garbage collection. Only an explicit close stops it then.
    streams = []

    def convert(self, rel):
        sent = 0
        try:
            for sent in range(1, 1001):
                yield b"mp4"
        finally:
            stopped.append(sent)
    monkeypatch.setattr(Library, "convert", lambda self, rel: streams.append(convert(self, rel)) or streams[-1])
    app = create_app(Store())
    client = TestClient(app)
    code = client.post("/api/rooms", json={"name": "Friday"}).json()["code"]
    link = app.state.song_link("media", "Old - Movie.avi", code)
    path, _, query = link.partition("?")

    # TestClient always reads to the end, so talk to the app directly and hang up after the first chunk.
    async def watch_then_leave():
        playing, asked = anyio.Event(), False

        async def receive():
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await playing.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                playing.set()

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": unquote(path), "raw_path": path.encode(), "query_string": query.encode(),
                 "root_path": "", "headers": [(b"host", b"testserver")],
                 "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
        await app(scope, receive, send)

    anyio.run(watch_then_leave)
    assert len(stopped) == 1 and stopped[0] < 1000  # stopped early, not left running
    assert client.get(link).status_code == 200  # and its slot is free


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_cdg_renderer_paints_tiles():
    def packet(instruction, data):
        return bytes([9, instruction, 0, 0]) + bytes(data).ljust(16, b"\0") + bytes(4)
    red, blue = [0x3C, 0x00], [0x00, 0x0F]  # 4-bit RGB packed into two 6-bit bytes
    stream = (packet(30, [0, 0] + red + blue)       # colours 0 black, 1 red, 2 blue
              + packet(1, [1])                       # clear the screen to red
              + packet(6, [1, 2, 1, 1] + [0x3F] * 12))  # tile at row 1, column 1, all blue
    script = """
const { CdgPlayer } = require(process.argv[1]);
let img;
const ctx = { createImageData: (w, h) => (img = { data: new Uint8ClampedArray(w * h * 4) }),
              putImageData() {}, clearRect() {} };
const p = CdgPlayer({ getContext: () => ctx });
p.load(Buffer.from(process.argv[2], "hex"));
p.sync(10);
const at = (x, y) => Array.from(img.data.slice((y * 300 + x) * 4, (y * 300 + x) * 4 + 3)).join(",");
console.log(at(0, 0), at(6, 12), at(11, 23), at(12, 12));
"""
    out = subprocess.run(["node", "-e", script, str(STATIC / "kq-cdg.js"), stream.hex()],
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["255,0,0", "0,0,255", "0,0,255", "255,0,0"]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_recordings_get_a_length_so_players_can_seek(tmp_path):
    from karaqueue.app import make_seekable

    # Written to a pipe like a browser's recorder does: no length in the file.
    take = tmp_path / "take.webm"
    take.write_bytes(subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=3", "-c:a", "libopus", "-f", "webm", "-"],
        capture_output=True, check=True).stdout)
    length = lambda: subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(take)], capture_output=True, text=True).stdout.strip()
    assert length() in ("", "N/A")
    make_seekable(take)
    assert abs(float(length()) - 3) < 0.2
    assert [p.name for p in tmp_path.iterdir()] == ["take.webm"]
