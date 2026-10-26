import pytest
from fastapi.testclient import TestClient

from karaqueue import youtube
from karaqueue.app import create_app
from karaqueue.store import Error, Store

ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("value", [
    ID,
    f"https://www.youtube.com/watch?v={ID}&t=42s",
    f"youtube.com/watch?v={ID}",
    f"https://m.youtube.com/watch?v={ID}",
    f"https://youtu.be/{ID}?si=abc",
    f"https://www.youtube.com/embed/{ID}",
    f"https://www.youtube.com/shorts/{ID}",
    f"https://music.youtube.com/watch?v={ID}",
])
def test_video_id_accepts_common_links(value):
    assert youtube.video_id(value) == ID


@pytest.mark.parametrize("value", [
    "https://vimeo.com/123",
    f"https://notyoutube.com/watch?v={ID}",
    "https://www.youtube.com/watch?v=short",
    "javascript:alert(1)",
])
def test_video_id_rejects_everything_else(value):
    with pytest.raises(Error):
        youtube.video_id(value)


def test_blank_means_no_video():
    assert youtube.video_id("  ") is None
    assert youtube.video_id(None) is None


def test_songs_carry_their_video(monkeypatch):
    monkeypatch.delenv("KARAQUEUE_YOUTUBE_API_KEY", raising=False)
    client = TestClient(create_app(Store()))
    room = client.post("/api/rooms", json={"name": "Friday"}).json()
    code = room["code"]
    token = client.post(f"/api/rooms/{code}/singers", json={"name": "Alex"}).json()["token"]
    auth = {"Authorization": "Bearer " + token}

    bad = client.post(f"/api/rooms/{code}/songs", headers=auth,
                      json={"title": "x", "artist": "y", "video": "https://vimeo.com/1"})
    assert bad.status_code == 400
    client.post(f"/api/rooms/{code}/songs", headers=auth,
                json={"title": "Never Gonna", "artist": "Rick", "video": f"https://youtu.be/{ID}"})
    client.post(f"/api/rooms/{code}/next", headers={"Authorization": "Bearer " + room["host_token"]})
    assert client.get(f"/api/rooms/{code}").json()["now_playing"]["video_id"] == ID

    assert client.get("/api/config").json()["youtube_search"] is False
    assert client.get(f"/api/rooms/{code}/search?q=africa").status_code == 401
    assert client.get(f"/api/rooms/{code}/search?q=africa", headers=auth).status_code == 404
