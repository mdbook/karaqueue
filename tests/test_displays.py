import asyncio
import shutil
import subprocess

import pytest

from karaqueue.app import STATIC
from karaqueue.displays import Hub
from karaqueue.store import Error, Store


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


def room():
    store = Store()
    created = store.create_room("Friday")
    return Hub(store), created["code"], created["host_token"]


async def admit(hub, code, host, display_id, **settings):
    ws = Socket()
    hub.connect(code, ws)
    await hub.receive(code, ws, {"type": "hello", "host": host,
                               "display": {"id": display_id, **settings}})
    return ws


def test_two_screens_follow_one_clock_by_default_without_changing_sound():
    async def check():
        hub, code, host = room()
        first = await admit(hub, code, host, "first-screen")
        second = await admit(hub, code, host, "second-screen", mic=True)
        for ws in (first, second):
            welcome = ws.messages[0]
            assert welcome["sound"] == "" and welcome["leader"] == "first-screen"
            assert welcome["settings"]["muted"] is False
        assert second.messages[0]["settings"]["mic"] is True
        first.messages.clear()
        second.messages.clear()
        clock = {"type": "clock", "song": 7, "state": "playing", "time": 42.5}
        await hub.receive(code, second, clock)  # followers cannot steer the room's clock
        await hub.receive(code, Socket(), clock)  # nor can an unadmitted screen or phone
        assert first.messages == second.messages == []
        await hub.receive(code, first, clock)
        assert first.messages == []
        assert second.messages == [{**clock, "display": "first-screen"}]

    asyncio.run(check())


@pytest.mark.parametrize("leave", ["disconnect", "forget", "queue"])
def test_leader_fails_over_when_it_leaves_or_has_no_video(leave):
    async def check():
        hub, code, host = room()
        queue = await admit(hub, code, host, "queue-screen", layout="splash")
        first = await admit(hub, code, host, "first-screen")
        second = await admit(hub, code, host, "second-screen", layout="confidence")
        assert hub.leader(code) == "first-screen"
        assert any(m.get("leader") == "first-screen" for m in queue.messages)
        second.messages.clear()
        if leave == "disconnect":
            await hub.disconnect(code, first)
        elif leave == "forget":
            await hub.forget(code, "first-screen")
        else:
            await hub.command(code, "first-screen", {"layout": "queue"}, None)
        assert hub.leader(code) == "second-screen"
        assert {"type": "sound", "display": "", "leader": "second-screen"} in second.messages
        second.messages.clear()
        await hub.receive(code, first, {"type": "clock", "song": 7, "time": 2})
        assert second.messages == []
        await hub.receive(code, second, {"type": "clock", "song": 7, "state": "playing", "time": 50})
        assert queue.messages[-1] == {"type": "clock", "display": "second-screen", "song": 7,
                                      "state": "playing", "time": 50}

    asyncio.run(check())


def test_sound_from_takes_the_clock_and_falls_back_without_changing_the_sound_choice():
    async def check():
        hub, code, host = room()
        first = await admit(hub, code, host, "first-screen")
        panel = await admit(hub, code, host, "panel-screen", panel=True, layout="video")
        hub.store.set_options(code, host, {"sound": "panel-screen"})
        await hub.send_sound(code)
        assert first.messages[-1] == {"type": "sound", "display": "panel-screen", "leader": "panel-screen"}
        await hub.receive(code, panel, {"type": "clock", "song": 7, "state": "paused", "time": 12})
        assert first.messages[-1] == {"type": "clock", "display": "panel-screen", "song": 7,
                                      "state": "paused", "time": 12}
        await hub.disconnect(code, panel)
        assert first.messages[-1] == {"type": "sound", "display": "panel-screen", "leader": "first-screen"}
        assert hub.store.sound(code) == "panel-screen"
        # Reconnecting the chosen sound screen takes the clock back; clearing the choice
        # returns it to the oldest connected video screen, without changing any settings.
        await admit(hub, code, host, "panel-screen", panel=True, layout="video")
        assert hub.leader(code) == "panel-screen"
        hub.store.set_options(code, host, {"sound": ""})
        await hub.send_sound(code)
        assert first.messages[-1] == {"type": "sound", "display": "", "leader": "first-screen"}

    asyncio.run(check())


@pytest.mark.parametrize("layout", ["queue", "splash"])
def test_sound_from_without_video_relays_heartbeats_without_taking_the_clock(layout):
    async def check():
        hub, code, host = room()
        sound = await admit(hub, code, host, "sound-screen", layout=layout)
        first = await admit(hub, code, host, "first-screen")
        second = await admit(hub, code, host, "second-screen")
        hub.store.set_options(code, host, {"sound": "sound-screen"})
        await hub.send_sound(code)
        assert hub.leader(code) == "first-screen"
        assert first.messages[-1] == {"type": "sound", "display": "sound-screen", "leader": "first-screen"}
        for ws in (sound, first, second):
            ws.messages.clear()
        heartbeat = {"type": "clock", "song": None, "state": "idle", "time": None}
        await hub.receive(code, second, {**heartbeat, "display": "sound-screen"})
        await hub.receive(code, Socket(), {**heartbeat, "display": "sound-screen"})
        assert first.messages == []  # only the admitted sound socket can keep it alive
        for _ in range(10):
            await hub.receive(code, sound, heartbeat)
        expected = [{**heartbeat, "display": "sound-screen"}] * 10
        assert first.messages == second.messages == expected
        assert sound.messages == []
        assert hub.leader(code) == "first-screen"
        await hub.disconnect(code, sound)
        first.messages.clear()
        await hub.receive(code, sound, heartbeat)
        assert first.messages == []  # a departed source no longer keeps followers quiet
        assert hub.store.sound(code) == "sound-screen"
        hub.store.delete_room(code)
        await hub.receive(code, first, heartbeat)  # late clocks after room removal are harmless

    asyncio.run(check())


def test_disconnecting_after_the_room_is_removed_still_forgets_the_screen():
    async def check():
        hub, code, host = room()
        first = await admit(hub, code, host, "first-screen")
        await admit(hub, code, host, "second-screen")
        hub.store.delete_room(code)
        await hub.disconnect(code, first)
        assert "first-screen" not in hub.displays[code]

    asyncio.run(check())


@pytest.mark.parametrize("action", ["seek", "play", "pause", "restart"])
def test_transport_on_a_follower_moves_every_screen_but_settings_stay_local(action):
    async def check():
        hub, code, host = room()
        first = await admit(hub, code, host, "first-screen")
        second = await admit(hub, code, host, "second-screen")
        first.messages.clear()
        second.messages.clear()
        if action == "seek":
            with pytest.raises(Error):
                await hub.command(code, "second-screen", {}, action)
            assert first.messages == second.messages == []
        await hub.command(code, "second-screen", {"volume": 25}, action, 42.5)
        assert first.messages[0] == {"type": "command", "action": action, "time": 42.5}
        assert second.messages[0]["action"] == action and second.messages[0]["time"] == 42.5
        assert second.messages[0]["settings"]["volume"] == 25
        assert hub.list(code)[0]["volume"] == 100

    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_screen_reuses_clock_sync_without_muting_default_followers():
    # Exercise the actual page functions with a fake deck and clock, without a browser or YouTube.
    page = (STATIC / "screen.html").read_text()
    sync = page[page.index("  const SILENT_AFTER_MS"):page.index("  function deckFor")]
    welcome = page[page.index("  function onWelcome"):page.index("  function applySettings")]
    audio = page[page.index("  function applyAudio"):page.index("  // ---- One clock")]
    command = page[page.index("  function onCommand"):page.index("  // ---- Which speakers")]
    script = """
const assert = require("assert/strict");
let now = 10000, pos = 1, status = "playing", muted = null, mics = 0, micActive = true, clocks = [];
const performance = { now: () => now }, setInterval = () => {};
let displayId = "second-screen", hasVideo = true;
const display = { volume: 80, muted: false, mic: true };
const state = { now_playing: { id: 7 } }, loadedSong = 7, interacted = true;
const body = { classList: { contains: () => hasVideo } }, youtube = {}, player = null;
const applyKey = () => {}, applyMic = () => { mics++; micActive = display.mic && !following(); };
const send = (msg) => clocks.push(msg);
const deck = { time: () => pos, state: () => status, seek: (t) => { pos = t; },
  play: () => { status = "playing"; }, pause: () => { status = "paused"; },
  audio: (volume, quiet) => { assert.equal(volume, 80); muted = quiet; } };
let uploadKey;
const localStorage = { setItem() {} }, screenKey = "test", showPairing = () => {}, syncRecording = () => {};
const applySettings = () => {};
""" + sync + welcome + audio + command + """
onWelcome({ sound: "", leader: "first-screen" });
applyAudio();
assert.equal(muted, false);
onClock({ display: "first-screen", song: 7, state: "playing", time: 40 });
assert.equal(pos, 40.1); // the existing drift correction also works with local sound
assert.equal(muted, false);
onClock({ display: "intruder", song: 7, state: "playing", time: 90 });
assert.equal(pos, 40.1);
now += 4000;
onClock({ display: "first-screen", song: 7, state: "paused", time: 45 });
assert.equal(status, "paused");
assert.equal(pos, 45); // paused screens catch up too
onCommand({ action: "seek", time: 80 });
assert.equal(pos, 80);
onClock({ display: "first-screen", song: 7, state: "paused", time: 45 });
assert.equal(pos, 80); // a clock already in flight must not undo a shared jump
onSound({ display: "first-screen", leader: "first-screen" });
assert.equal(muted, true);
assert.ok(mics > 0);
now += 5000;
onClock({ display: "first-screen", song: 7, state: "paused", time: 45 });
now += 5000;
checkFollowing();
assert.equal(muted, true); // the selected sound screen's clock keeps followers quiet
onSound({ display: "first-screen", leader: "second-screen" }); // sound screen leaves
sendClock();
assert.equal(clocks.length, 1); // this screen now leads the same clock path
now += 2000;
checkFollowing();
assert.equal(muted, false); // sound resumes after the existing six-second grace period
onSound({ display: "", leader: "second-screen" });
assert.equal(muted, false);

// A sound source without video sends idle clocks even though another screen leads playback.
onSound({ display: "sound-screen", leader: "first-screen" });
displayId = "sound-screen";
hasVideo = false;
clocks = [];
sendClock();
assert.deepEqual(clocks, [{ type: "clock", song: 7, state: "idle", time: null }]);
const heartbeat = { ...clocks[0], display: "sound-screen" };
displayId = "second-screen";
hasVideo = true;
const beforeHeartbeat = { pos, status };

// Both the playback leader and its followers stay muted past the grace period.
for (const leader of ["first-screen", "second-screen"]) {
  onSound({ display: "sound-screen", leader });
  for (let i = 0; i < 4; i++) {
    now += 5000;
    onClock(heartbeat);
    onClock({ display: "sound-screen", song: 7, state: "playing", time: 120 });
    checkFollowing();
    assert.equal(muted, true);
    assert.equal(micActive, false);
    assert.deepEqual({ pos, status }, beforeHeartbeat); // sound heartbeats never steer playback
  }
  // Only sound-source liveness counts: other clocks cannot prevent failover when it leaves.
  now += 6001;
  onClock({ display: leader, song: null, state: "idle", time: null });
  checkFollowing();
  assert.equal(muted, false);
  assert.equal(micActive, true);
}
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
