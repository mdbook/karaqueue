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


# ---- C11: the same name typed a different way is the same name ----

def test_names_that_differ_only_in_accents_form_case_or_invisible_characters_are_one_singer(room):
    store, code, host = room
    assert store.join(code, "José")["name"] == "José"
    for twin in ("José", "JOSÉ", "josé", "  José  ", "Jos​é", "Ｊｏｓé"):
        with pytest.raises(Error) as err:
            store.join(code, twin)
        assert err.value.status == 409, twin
    assert store.join(code, "Jose")["name"] == "Jose"  # a different name, not a twin
    assert [s["name"] for s in store.state(code)["rotation"]] == ["José", "Jose"]


def test_non_ascii_case_counts_as_the_same_name(room):
    store, code, host = room
    store.join(code, "Élodie")
    with pytest.raises(Error):
        store.join(code, "élodie")  # SQLite's NOCASE alone only folds a-z


def test_staff_and_guest_accounts_fold_names_like_singers_do(room):
    store, code, host = room
    uid = store.add_user("Élodie", "host", "pw")
    for twin in ("élodie", "ÉLODIE", " Élodie "):
        with pytest.raises(Error) as err:
            store.add_user(twin, "host", "pw")
        assert err.value.status == 409, twin
    assert store.user(name="élodie")["id"] == uid  # signing in with other casing still finds it
    other = store.add_user("Zoë", "host", "pw")
    with pytest.raises(Error):
        store.update_user(other, name="élodie")
    store.update_user(uid, name="ÉLODIE")  # renaming to its own name in another case is fine
    gid = store.add_guest("Élodie", "pw")
    with pytest.raises(Error) as err:
        store.add_guest("élodie", "pw")
    assert err.value.status == 409
    assert store.guest(name="ÉLODIE")["id"] == gid


def test_a_joiner_survives_in_names_but_other_invisible_characters_do_not(room):
    store, code, host = room
    family = "👨‍👩‍👧"
    assert store.join(code, family)["name"] == family
    assert store.join(code, "Al​ex⁠")["name"] == "Alex"


def test_song_titles_are_stored_in_one_form(room):
    store, code, host = room
    alex = join(store, code, "Alex")
    song = store.add_song(code, alex, "Café​  Song", "Béyoncé")
    shown = store.me(code, alex)["songs"][0]
    assert (shown["title"], shown["artist"]) == ("Café Song", "Béyoncé")


# ---- C12: every screen has its own secret ----

def test_a_screens_key_only_opens_that_screen(room):
    store, code, host = room
    one, two = store.pair_screen(code, "tv-one"), store.pair_screen(code, "tv-two")
    assert one != two
    assert store.screen(code, "tv-one", one) is not None and store.screen(code, "tv-two", two) is not None
    assert store.screen(code, "tv-two", one) is None and store.screen(code, "tv-one", two) is None
    assert store.screen(code, "tv-three", one) is None
    other = store.create_room("Saturday")["code"]
    assert store.screen(other, "tv-one", one) is None  # nor does it open the same id in another room
    store.pair_screen(code, "tv-one")
    assert store.screen(code, "tv-one", one) is None  # pairing again replaces the old key


# ---- C4: downloads go in the order songs will be sung ----

def test_queued_videos_come_in_singing_order(room):
    store, code, host = room
    alex, sam = join(store, code, "Alex"), join(store, code, "Sam")
    ids = {"a1": "aaaaaaaaaa1", "a2": "aaaaaaaaaa2", "s1": "sssssssss11", "s2": "sssssssss12"}
    for who, tok in (("a", alex), ("s", sam)):
        for n in (1, 2):
            store.add_song(code, tok, f"{who}{n}", "x", video_id=ids[f"{who}{n}"])
    # Sam queued after Alex, but Alex's second song is not next: it is Sam's first.
    assert store.queued_videos(code) == [ids["a1"], ids["s1"], ids["a2"], ids["s2"]]
    store.next(code, host)  # Alex sings; Sam is up
    assert store.queued_videos(code) == [ids["s1"], ids["a2"], ids["s2"]]
