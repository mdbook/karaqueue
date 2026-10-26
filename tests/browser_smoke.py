"""Browser smoke test against a disposable local server, never a live party.

Install the ui optional dependencies and Chromium, then run:
    python tests/browser_smoke.py --screenshots /tmp/karaqueue-ui
"""

import argparse
from contextlib import contextmanager
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def track(page):
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Anything the Content-Security-Policy blocks shows up only as a console message.
    page.on("console", lambda m: "Content Security Policy" in m.text and errors.append(m.text))
    page.set_default_timeout(10000)


def shot(page, name):
    page.screenshot(path=str(OUT / (name + ".png")), full_page=True)


def no_overflow(page):
    result = page.evaluate(
        """() => ({width: innerWidth, scroll: document.documentElement.scrollWidth, bad: [...document.querySelectorAll('body *')].filter(el => {const r=el.getBoundingClientRect(); return r.width && r.right > innerWidth + 1 && getComputedStyle(el).position !== 'fixed';}).slice(0,8).map(el => el.tagName+'.'+el.className)})"""
    )
    assert result["scroll"] <= result["width"] + 1, (page.url, result)


def run_checks(base_url, output):
    global BASE, OUT, errors
    BASE, OUT, errors = base_url, output, []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        host_context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
        )
        host = host_context.new_page()
        track(host)
        host.goto(BASE)
        expect(host.locator("h1")).to_contain_text("Good friends.")
        for theme in ["midnight", "daylight", "sunset"]:
            host.get_by_label("Color theme").select_option(theme)
            host.reload()
            expect(host.locator("html")).to_have_attribute("data-theme", theme)
            shot(host, "home-" + theme)
        host.get_by_role("tab", name="Join the party").focus()
        host.keyboard.press("ArrowRight")
        expect(host.get_by_role("tab", name="Host a night")).to_have_attribute(
            "aria-selected", "true"
        )
        host.get_by_label("Give your night a name").fill("Friday night karaoke")
        host.get_by_role("button", name="Start a room", exact=True).click()
        host.wait_for_url("**/host")
        code = host.url.split("/")[-2]
        expect(host.locator("#room-name")).to_have_text("Friday night karaoke")
        expect(host.locator("#invite-qr svg")).to_be_visible()
        shot(host, "host-empty")
        host.get_by_role("button", name="Copy guest link", exact=True).click()
        expect(host.locator("#flash")).to_have_text("Guest link copied")
        # Tab navigation shows one workspace, with useful empty states.
        for view in ["screens", "songs", "settings", "recordings", "overview"]:
            host.locator('.host-nav [data-host-view="' + view + '"]').click()
            expect(host.locator("body")).to_have_attribute("data-view", view)
        guest_context = browser.new_context(
            viewport={"width": 390, "height": 844}, reduced_motion="reduce"
        )
        guest = guest_context.new_page()
        track(guest)
        guest.goto(BASE + "/r/" + code)
        expect(guest.locator("#join-card")).to_be_visible()
        shot(guest, "guest-join")
        guest.get_by_label("What should we call you?").fill("Maya")
        guest.get_by_role("button", name="Join the room").click()
        expect(guest.locator("#guest-nav")).to_be_visible()
        guest.get_by_role("button", name="Add a song", exact=True).click()
        shot(guest, "guest-request")
        for title, artist in [("Dancing Queen", "ABBA"), ("Valerie", "Amy Winehouse")]:
            guest.get_by_label("Song title").fill(title)
            guest.get_by_label("Artist", exact=True).fill(artist)
            guest.get_by_role("button", name="Add to my songs").click()
            expect(guest.locator("#mine")).to_contain_text(title)
            if title == "Dancing Queen":
                guest.get_by_role("button", name="Add a song", exact=True).click()
        expect(guest.locator("#my-song-count")).to_have_text("2")
        guest.get_by_role("button", name="Move Valerie up", exact=True).click()
        expect(guest.locator("#mine li").first).to_contain_text("Valerie")
        shot(guest, "guest-songs")
        # Second singer makes the live rotation observable across contexts.
        response = host.request.post(
            BASE + "/api/rooms/" + code + "/singers", data={"name": "Theo"}
        )
        theo = response.json()["token"]
        host.request.post(
            BASE + "/api/rooms/" + code + "/songs",
            headers={"Authorization": "Bearer " + theo},
            data={"title": "Mr. Brightside", "artist": "The Killers"},
        )
        expect(host.locator("#stat-singers")).to_have_text("2")
        expect(host.locator("#stat-songs")).to_have_text("3")
        host.locator("#next").click()
        expect(host.locator("#now")).to_contain_text("Maya")
        # Next mid-song asks first; saying no keeps Maya singing.
        asked = []
        host.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
        host.locator("#next").click()
        expect(host.locator("#now")).to_contain_text("Maya")
        assert asked and "Maya" in asked[0], asked
        guest.get_by_role("button", name="The queue", exact=True).click()
        expect(guest.locator("#turn")).to_contain_text("You're singing now!")
        shot(host, "host-live")
        shot(guest, "guest-queue")
        host.locator("#pause").click()
        guest.get_by_role("button", name="Add a song", exact=True).click()
        expect(guest.locator("#paused")).to_be_visible()
        expect(guest.locator("#request")).to_be_hidden()
        host.locator("#pause").click()
        expect(guest.locator("#request")).to_be_visible()
        # Host additions and settings still use the existing API.
        host.locator('.host-nav [data-host-view="songs"]').click()
        host.locator("#for").select_option(label="Theo")
        # Hosts pick from the library for someone, same as guests do for themselves.
        host.get_by_label("Find a song").fill("africa")
        host.get_by_label("Find a song").press("Enter")
        expect(host.locator("#find-results .result").first).to_be_visible()
        shot(host, "host-add-song")
        host.locator("#find-results .result", has_text="from the song library").click()
        expect(host.get_by_label("Song", exact=True)).to_have_value("Africa")
        expect(host.get_by_label("Artist", exact=True)).to_have_value("Toto")
        expect(host.locator("#find-results")).to_be_empty()
        host.get_by_role("button", name="Add song", exact=True).click()
        expect(host.locator("#flash")).to_have_text("Song added")
        host.locator('.host-nav [data-host-view="settings"]').click()
        host.locator("#limit").fill("5")
        host.locator("#limit").press("Tab")
        expect(host.locator("#limit-note")).to_have_text("Just for this room.")
        shot(host, "host-settings")
        # Guest preview must preserve the host credential and never offer a singer sign-in.
        preview = host_context.new_page()
        track(preview)
        preview.goto(BASE + "/r/" + code + "?guest=1")
        expect(preview.locator("#whoami")).to_contain_text("previewing")
        expect(preview.locator("#join-card")).to_be_hidden()
        assert (
            preview.evaluate(
                'room => JSON.parse(localStorage.getItem("kq:"+room)).role', code
            )
            == "host"
        )
        preview.close()
        # The screen registers and responds to the host's layout controls.
        tv = host_context.new_page()
        track(tv)
        tv.goto(BASE + "/r/" + code + "/screen")
        tv.locator("#start").click()
        expect(tv.locator("#start")).to_have_class("gone")
        host.locator('.host-nav [data-host-view="screens"]').click()
        expect(host.locator(".tv-tile")).to_have_count(1)
        shot(host, "host-screen-controls")
        for label, layout in [
            ("Queue", "queue"),
            ("Join screen", "splash"),
            ("Video", "video"),
            ("Singer monitor", "confidence"),
            ("Split", "split"),
        ]:
            host.locator(".layouts").get_by_role(
                "button", name=label, exact=True
            ).click()
            expect(tv.locator("body")).to_have_attribute("data-layout", layout)
        shot(tv, "tv-split")

        # A TV in its own browser waits with a code until the host lets it in.
        far_context = browser.new_context(viewport={"width": 1280, "height": 720})
        far_tv = far_context.new_page()
        track(far_tv)
        far_tv.goto(BASE + "/r/" + code + "/screen")
        expect(far_tv.locator("#pairing")).to_be_visible()
        pair_code = far_tv.locator("#pairing .pair-code").inner_text()
        shot(far_tv, "tv-waiting")
        expect(host.locator("#waiting .waiting-code")).to_have_text(pair_code)
        shot(host, "host-screen-waiting")
        host.locator("#waiting").get_by_role("button", name="Let in").click()
        expect(far_tv.locator("#pairing")).to_be_hidden()
        expect(host.locator(".tv-tile")).to_have_count(2)
        expect(host.locator("#waiting")).to_be_hidden()
        # It comes back without asking again.
        far_tv.reload()
        expect(host.locator(".tv-tile")).to_have_count(2)
        expect(far_tv.locator("#pairing")).to_be_hidden()
        # A screen for a room that's gone says so across the whole screen.
        far_tv.goto(BASE + "/r/NOROOM/screen")
        expect(far_tv.locator("#gone")).to_be_visible()
        shot(far_tv, "tv-room-gone")
        far_context.close()
        # Themes apply in another tab immediately.
        host.get_by_label("Color theme").select_option("daylight")
        expect(tv.locator("html")).to_have_attribute("data-theme", "daylight")
        shot(host, "host-daylight")
        host.get_by_label("Color theme").select_option("sunset")
        # Administration sign-in, setting search, and sections.
        admin = host_context.new_page()
        track(admin)
        admin.goto(BASE + "/admin")
        admin.get_by_label("Password", exact=True).fill("design-test-only")
        admin.get_by_role("button", name="Sign in", exact=True).click()
        expect(admin.locator("#page")).to_be_visible()
        admin.get_by_label("Find a setting").fill("songs")
        expect(admin.locator("#groups section:visible")).not_to_have_count(0)
        admin.get_by_label("Find a setting").fill("no-such-setting-xyz")
        expect(admin.locator("#no-settings")).to_be_visible()
        admin.get_by_label("Find a setting").fill("")
        shot(admin, "admin-settings")
        for view in ["rooms", "server", "access", "settings"]:
            admin.locator('button[data-admin-view="' + view + '"]').click()
            expect(admin.locator("body")).to_have_attribute("data-admin-view", view)
        # Getting back in: the host adds someone by hand and signs their phone in with a one-time code.
        host.locator('.host-nav [data-host-view="overview"]').click()
        host.get_by_label("Name of the singer to add").fill("Jules")
        host.get_by_role("button", name="Add singer").click()
        expect(host.locator("#singers")).to_contain_text("Jules")
        expect(host.locator("#flash")).to_be_hidden(timeout=15000)  # its toast sits over Jules's row
        host.get_by_role("button", name="More for Jules").scroll_into_view_if_needed()
        host.wait_for_timeout(300)  # the menu closes on scroll, so let the scroll land first
        host.get_by_role("button", name="More for Jules").click()
        host.get_by_role("button", name="Sign in Jules's phone").click()
        expect(host.locator("#move-dialog .move-qr svg")).to_be_visible()
        shot(host, "host-sign-in-phone")
        host.get_by_role("button", name="Done").click()
        link = host.evaluate("""async () => {
            const room = (await api("GET", "/rooms/" + code + "/host"));
            const jules = room.singers.find((s) => s.name === "Jules");
            return (await api("POST", "/rooms/" + code + "/singers/" + jules.id + "/link")).link;
        }""")
        new_phone_context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        new_phone = new_phone_context.new_page()
        track(new_phone)
        new_phone.goto(link)
        expect(new_phone.locator("#whoami")).to_contain_text("Jules")
        assert "claim" not in new_phone.url
        new_phone.get_by_role("button", name="Use another phone").click()
        expect(new_phone.locator("#move-box .move-qr svg")).to_be_visible()
        shot(new_phone, "guest-use-another-phone")
        # A singer who joined without an account ties their spot to one from their own page.
        new_phone.get_by_text("Tie this spot to an account").click()
        new_phone.get_by_label("Name", exact=True).fill("Jules K")
        new_phone.get_by_label("Password", exact=True).fill("sing it loud")
        shot(new_phone, "guest-tie-account")
        new_phone.get_by_role("button", name="Make an account").click()
        expect(new_phone.locator("#flash")).to_contain_text("tied to Jules K")
        expect(new_phone.locator("#tie-box")).to_be_hidden()
        expect(new_phone.locator("#whoami")).to_contain_text("Jules")
        # A guest account finds its spot again from another phone.
        account_context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        first = account_context.new_page()
        track(first)
        first.goto(BASE + "/r/" + code)
        first.get_by_text("Sign in, or make an account").click()
        first.get_by_label("Name", exact=True).fill("Rae")
        first.get_by_label("Password", exact=True).fill("sing it loud")
        shot(first, "guest-account")
        first.get_by_role("button", name="Make an account").click()
        expect(first.locator("#whoami")).to_contain_text("Rae")
        other_context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        other = other_context.new_page()
        track(other)
        other.goto(BASE + "/r/" + code)
        other.get_by_text("Sign in, or make an account").click()
        other.get_by_label("Name", exact=True).fill("rae")
        other.get_by_label("Password", exact=True).fill("sing it loud")
        other.get_by_role("button", name="Sign in", exact=True).click()
        expect(other.locator("#whoami")).to_contain_text("Rae")
        other.goto(BASE + "/r/" + code)
        expect(other.locator("#whoami")).to_contain_text("Rae")
        assert host.evaluate("""async () => (await api("GET", "/rooms/" + code + "/host")).singers.filter((s) => s.name === "Rae").length""") == 1
        admin.locator('button[data-admin-view="access"]').click()
        admin.reload()
        admin.locator('button[data-admin-view="access"]').click()
        expect(admin.locator("#people")).to_contain_text("Rae")
        # Invite a host by link: they pick their own name and password, and show up signed in.
        admin.get_by_role("button", name="Make a link").click()
        link = admin.locator("#invites .link-box input").input_value()
        invited_context = browser.new_context()
        invited = invited_context.new_page()
        track(invited)
        invited.goto(link)
        expect(invited.locator("#gate-title")).to_have_text("Make your account")
        invited.get_by_label("Your name").fill("Dana")
        invited.locator("#password").fill("hosting tonight")
        invited.get_by_label("Same password again").fill("hosting tonight")
        invited.get_by_role("button", name="Make my account").click()
        expect(invited.locator("#gate-title")).to_contain_text("You're all set, Dana")
        shot(invited, "invite-done")
        invited.goto("about:blank")
        invited.goto(link)
        expect(invited.locator("#flash")).to_contain_text("already been used")
        invited_context.close()
        admin.reload()
        admin.locator('button[data-admin-view="access"]').click()
        dana = admin.locator("#people > li", has_text="Dana")
        dana.locator("summary").click()
        expect(dana.locator(".sessions li")).to_have_count(1)
        shot(admin, "admin-people")
        dana.get_by_role("button", name="Sign out", exact=True).click()
        expect(admin.locator("#people > li", has_text="Dana").locator(".sessions")).to_have_count(0)
        admin.get_by_label("Find someone").fill("guest")
        expect(admin.locator("#people > li")).to_have_count(2)
        admin.get_by_label("Find someone").fill("rae")
        expect(admin.locator("#people > li")).to_have_count(1)
        admin.get_by_label("Find someone").fill("")
        admin.locator('button[data-admin-view="settings"]').click()

        # Desktop and phone overflow checks for all main views and themes.
        landing = guest_context.new_page()
        track(landing)
        landing.goto(BASE)
        for width in [320, 390, 768, 1440]:
            for page in [landing, host, guest, admin]:
                page.set_viewport_size({"width": width, "height": 900})
            for theme in ["sunset", "midnight", "daylight"]:
                for page in [landing, host, guest, admin]:
                    page.get_by_label("Color theme").select_option(theme)
                no_overflow(landing)
                for view in ["overview", "screens", "songs", "settings", "recordings"]:
                    host.locator('.host-nav [data-host-view="' + view + '"]').click()
                    no_overflow(host)
                for view in ["queue", "songs", "request"]:
                    guest.locator('button[data-guest-view="' + view + '"]').click()
                    no_overflow(guest)
                for view in ["settings", "rooms", "server", "access"]:
                    admin.locator('button[data-admin-view="' + view + '"]').click()
                    no_overflow(admin)
        host.set_viewport_size({"width": 390, "height": 844})
        host.get_by_label("Color theme").select_option("sunset")
        host.locator('.host-nav [data-host-view="overview"]').click()
        shot(host, "host-mobile")
        landing.set_viewport_size({"width": 390, "height": 844})
        landing.get_by_label("Color theme").select_option("sunset")
        shot(landing, "home-mobile")
        assert not errors, errors
        print(
            json.dumps(
                {
                    "status": "passed",
                    "room": code,
                    "screenshots": str(OUT),
                    "page_errors": errors,
                }
            )
        )
        browser.close()


@contextmanager
def preview_server(fill_library=None, admin_password="design-test-only", **settings):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="karaqueue-ui-") as scratch:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        songs = Path(scratch) / "songs"
        songs.mkdir()
        if fill_library:
            fill_library(songs)
        else:
            (songs / "Toto - Africa.mp4").write_bytes(b"")
        # Ignore real server settings so the test cannot touch a user's media or database.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("KARAQUEUE_")
        }
        env.update(
            KARAQUEUE_DB=str(Path(scratch) / "test.db"),
            KARAQUEUE_ADMIN_PASSWORD=admin_password,
            KARAQUEUE_NEW_ROOMS="anyone",
            KARAQUEUE_MEDIA_DIR=str(songs),
            PYTHONDONTWRITEBYTECODE="1",
            **settings,
        )
        base_url = f"http://127.0.0.1:{port}"
        with open(Path(scratch) / "server.log", "w+") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "karaqueue.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    try:
                        with urlopen(base_url + "/api/config", timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Preview server did not start")
                yield base_url
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--screenshots", type=Path, default=Path(tempfile.gettempdir()) / "karaqueue-ui"
    )
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with preview_server() as base_url:
        run_checks(base_url, args.screenshots)
