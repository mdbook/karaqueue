import time

import pytest

from karaqueue.store import Error, Store


@pytest.fixture
def room():
    store = Store()
    created = store.create_room("Friday Night")
    return store, created["code"], created["host_token"]


def join(store, code, name):
    return store.join(code, name)["token"]


def now_singing(store, code):
    playing = store.state(code)["now_playing"]
    return playing and (playing["singer"], playing["title"])


def test_rotation_is_round_robin(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    store.add_song(code, alex, "Africa", "Toto")
    store.add_song(code, alex, "Hold the Line", "Toto")
    store.add_song(code, sam, "Dreams", "Fleetwood Mac")

    store.next(code, host)
    assert now_singing(store, code) == ("Alex", "Africa")
    assert store.state(code)["up_next"] == {"singer": "Sam"}
    store.next(code, host)
    assert now_singing(store, code) == ("Sam", "Dreams")
    store.next(code, host)
    assert now_singing(store, code) == ("Alex", "Hold the Line")
    store.next(code, host)
    assert now_singing(store, code) is None


def test_late_joiner_goes_to_back_of_rotation(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    store.add_song(code, alex, "A1", "x")
    store.add_song(code, alex, "A2", "x")
    store.add_song(code, sam, "S1", "x")
    store.next(code, host)  # Alex
    kim = join(store, code, "Kim")
    store.add_song(code, kim, "K1", "x")
    order = []
    for _ in range(3):
        store.next(code, host)
        order.append(now_singing(store, code)[0])
    assert order == ["Sam", "Kim", "Alex"]


def test_hidden_singers_are_skipped(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    store.add_song(code, alex, "A1", "x")
    store.add_song(code, sam, "S1", "x")
    alex_id = store.me(code, alex)["id"]
    store.set_hidden(code, host, alex_id, True)
    store.next(code, host)
    assert now_singing(store, code) == ("Sam", "S1")
    assert [s["name"] for s in store.state(code)["rotation"]] == ["Sam"]


def test_reorder_keeps_ids_left_out(room):
    store, code, host = room
    alex = join(store, code, "Alex")
    ids = [store.add_song(code, alex, t, "x")["id"] for t in ("one", "two", "three")]
    me = store.me(code, alex)
    store.reorder_songs(code, alex, me["id"], [ids[2], 9999, ids[2]])
    assert [s["title"] for s in store.me(code, alex)["songs"]] == ["three", "one", "two"]


def test_names_are_case_insensitive_and_trimmed(room):
    store, code, _ = room
    join(store, code, "  Alex  ")
    with pytest.raises(Error) as err:
        join(store, code, "ALEX")
    assert err.value.status == 409
    with pytest.raises(Error):
        join(store, code, "x" * 33)


def test_permissions(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    song = store.add_song(code, alex, "A1", "x")["id"]
    with pytest.raises(Error) as err:
        store.remove_song(code, sam, song)
    assert err.value.status == 403
    with pytest.raises(Error) as err:
        store.next(code, alex)
    assert err.value.status == 403
    with pytest.raises(Error) as err:
        store.add_song(code, "not-a-token", "A1", "x")
    assert err.value.status == 401
    other = store.create_room("Other")
    with pytest.raises(Error):
        store.next(other["code"], host)
    store.remove_song(code, host, song)


def test_pause_blocks_singers_not_host(room):
    store, code, host = room
    alex = join(store, code, "Alex")
    store.set_paused(code, host, True)
    with pytest.raises(Error) as err:
        store.add_song(code, alex, "A1", "x")
    assert err.value.status == 423
    store.add_song(code, host, "A1", "x", singer_id=store.me(code, alex)["id"])


def test_song_limit(room):
    store, code, _ = room
    store.songs_per_singer = 2
    alex = join(store, code, "Alex")
    store.add_song(code, alex, "1", "x")
    store.add_song(code, alex, "2", "x")
    with pytest.raises(Error):
        store.add_song(code, alex, "3", "x")


def test_removing_current_singer_clears_now_playing(room):
    store, code, host = room
    alex = join(store, code, "Alex")
    store.add_song(code, alex, "A1", "x")
    store.next(code, host)
    store.remove_singer(code, host, store.me(code, alex)["id"])
    assert store.state(code)["now_playing"] is None
    assert store.state(code)["rotation"] == []


def test_codes_are_case_insensitive(room):
    store, code, _ = room
    assert store.state(code.lower())["name"] == "Friday Night"


def test_recordings_belong_to_the_singer_and_the_host(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    song = store.add_song(code, alex, "Africa", "Toto")["id"]
    with pytest.raises(Error):
        store.add_recording(code, song, "a.webm", 10)  # still queued, nobody sang it yet
    store.next(code, host)
    rec = store.add_recording(code, song, "a.webm", 10)

    assert [r["title"] for r in store.recordings(code, alex)] == ["Africa"]
    assert store.recordings(code, sam) == []
    assert store.recordings(code, host)[0]["singer"] == "Alex"
    assert store.recording_file(code, alex, rec) == "a.webm"
    with pytest.raises(Error) as err:
        store.recording_file(code, sam, rec)
    assert err.value.status == 403
    assert store.remove_recording(code, host, rec) == "a.webm"
    assert store.recordings(code, host) == []


def test_expire_drops_untouched_rooms(room):
    store, code, host = room
    alex = join(store, code, "Alex")
    store.add_song(code, alex, "Africa", "Toto")
    other = store.create_room("Saturday")["code"]
    kept = store.create_room("Sunday")["code"]
    later = time.time() + 1

    assert store.expire(later - 100) == []  # everything is newer than that
    store.touch(other)
    db_time = store.db.execute("SELECT touched_at FROM rooms WHERE code = ?", (other,)).fetchone()[0]
    assert sorted(store.expire(db_time - 0.0001, keep={kept})) == [code]
    with pytest.raises(Error):
        store.state(code)
    assert store.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 0  # cascaded
    assert store.state(other)["code"] == other
    assert store.state(kept)["code"] == kept


def test_next_only_moves_on_from_the_song_it_means(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    store.add_song(code, alex, "Africa", "Toto")
    store.add_song(code, sam, "Dreams", "Fleetwood Mac")
    store.next(code, host, after=0)  # nothing playing yet, so it starts
    africa = store.state(code)["now_playing"]["id"]
    store.next(code, host, after=0)  # a second tap: something is playing now, so nothing happens
    assert now_singing(store, code) == ("Alex", "Africa")
    store.next(code, host, after=africa)  # the screen says Africa ended
    store.next(code, host, after=africa)  # the host's tap crossed it: Sam keeps the mic
    assert now_singing(store, code) == ("Sam", "Dreams")


def test_removing_whoever_is_singing_passes_the_turn_on_not_back_to_the_top():
    store = Store()
    room = store.create_room("Party")
    code, host = room["code"], room["host_token"]
    ids = {}
    for name in "ABCD":
        joined = store.join(code, name)
        ids[name] = joined["singer_id"]
        store.add_song(code, joined["token"], name + " song", "x")
        store.add_song(code, joined["token"], name + " encore", "x")
    for _ in range(3):
        store.next(code, host)  # A, B, then C is singing
    assert store.state(code)["now_playing"]["singer"] == "C"
    store.remove_singer(code, host, ids["C"])
    store.next(code, host)
    assert store.state(code)["now_playing"]["singer"] == "D"


def test_the_artist_is_optional_but_the_title_is_not(room):
    store, code, host = room
    alex = join(store, code, "Alex")
    store.add_song(code, alex, "Happy Birthday", "  ")
    assert store.me(code, alex)["songs"][0]["artist"] == ""
    with pytest.raises(Error):
        store.add_song(code, alex, " ", "Toto")
