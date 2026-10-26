"""Takes the screenshots in the README and docs, from a throwaway server full of made-up guests.

Needs the ui optional dependencies, Chromium and ffmpeg (for the sample video). Run it from
the repository root:
    python tests/readme_screenshots.py
and it writes PNGs into docs/screenshots/.
"""

import argparse
import subprocess
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_smoke import preview_server

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "screenshots-only"
LIBRARY = [
    "ABBA - Dancing Queen", "ABBA - Waterloo", "Toto - Africa", "Journey - Don't Stop Believin'",
    "Queen - Don't Stop Me Now", "Whitney Houston - I Wanna Dance with Somebody",
    "Bon Jovi - Livin' on a Prayer", "Cyndi Lauper - Girls Just Want to Have Fun",
]
# Who's at the party, and what they want to sing. The first one is on when the shots are taken.
GUESTS = [
    ("Maya", [("Dancing Queen", "ABBA"), ("Valerie", "Amy Winehouse")]),
    ("Theo", [("Mr. Brightside", "The Killers")]),
    ("Priya", [("I Wanna Dance with Somebody", "Whitney Houston"), ("Africa", "Toto")]),
    ("Sam", [("Livin' on a Prayer", "Bon Jovi")]),
    ("Jordan", [("Don't Stop Me Now", "Queen")]),
]


def fill_library(folder):
    """Empty files make library entries; the song that's on gets a real video so the TV shows something."""
    for name in LIBRARY[1:]:
        (folder / (name + ".mp4")).write_bytes(b"")
    font = next(Path("/usr/share/fonts").rglob("NotoSans-Bold.ttf"), None)
    text = "drawtext=text='Sing it like nobody is listening':fontcolor=white:fontsize=56:x=(w-tw)/2:y=h*0.72"
    if font:
        text += ":fontfile=" + str(font)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "gradients=s=1280x720:c0=0x3a0ca3:c1=0xf72585:speed=0.02:d=40",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=40", "-vf", text, "-shortest",
         "-c:v", "libvpx", "-pix_fmt", "yuv420p", "-b:v", "1M", "-deadline", "realtime", "-c:a", "libopus", str(folder / (LIBRARY[0] + ".webm"))],
        check=True,
    )


def take(base, out):
    def shot(page, name, full=False):
        page.screenshot(path=str(out / (name + ".png")), full_page=full)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        desk = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        host = desk.new_page()
        host.goto(base)
        host.get_by_role("tab", name="Host a night").click()
        host.get_by_label("Give your night a name").fill("Friday night karaoke")
        host.get_by_role("button", name="Start a room", exact=True).click()
        host.wait_for_url("**/host")
        code = host.url.split("/")[-2]
        room = base + "/api/rooms/" + code

        for name, songs in GUESTS:
            token = host.request.post(room + "/singers", data={"name": name}).json()["token"]
            auth = {"Authorization": "Bearer " + token}
            for title, artist in songs:
                found = host.request.get(room + "/library?q=" + title.replace(" ", "+"), headers=auth).json()
                song = {"title": title, "artist": artist}
                if found:
                    song["media"] = found[0]["id"]
                host.request.post(room + "/songs", headers=auth, data=song)
        host.locator("#next").click()
        expect(host.locator("#now")).to_contain_text("Maya")

        # A TV opened from the host's browser joins by itself.
        tv = desk.new_page()
        tv.set_viewport_size({"width": 1280, "height": 720})
        tv.goto(base + "/r/" + code + "/screen?name=Living room TV")
        tv.locator("#start").click()
        host.locator('.host-nav [data-host-view="screens"]').click()
        expect(host.locator(".tv-tile")).to_have_count(1)
        tv.wait_for_timeout(2500)  # let the video get going
        tv.mouse.move(0, 0)
        tv.wait_for_timeout(3500)  # and the theme picker hide again
        shot(tv, "tv-split")
        for label, name in [("Queue", "tv-queue"), ("Join screen", "tv-join")]:
            host.locator(".layouts").get_by_role("button", name=label, exact=True).click()
            tv.wait_for_timeout(800)
            shot(tv, name)
        host.locator(".layouts").get_by_role("button", name="Split", exact=True).click()
        shot(host, "host-screens")
        host.locator('.host-nav [data-host-view="overview"]').click()
        shot(host, "host-queue")
        host.locator('.host-nav [data-host-view="songs"]').click()
        host.get_by_label("Find a song").fill("abba")
        host.get_by_label("Find a song").press("Enter")
        expect(host.locator("#find-results .result").first).to_be_visible()
        shot(host, "host-add-song")

        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, reduced_motion="reduce")
        guest = phone.new_page()
        guest.goto(base + "/r/" + code)
        expect(guest.locator("#join-card")).to_be_visible()
        guest.get_by_label("What should we call you?").fill("Alex")
        shot(guest, "phone-join")
        guest.get_by_role("button", name="Join the room").click()
        guest.get_by_role("button", name="Add a song", exact=True).click()
        guest.get_by_label("Search songs").fill("queen")
        guest.get_by_label("Search songs").press("Enter")
        expect(guest.locator("#results .result").first).to_be_visible()
        shot(guest, "phone-search")
        guest.locator("#results .result", has_text="Don't Stop Me Now").click()
        guest.get_by_role("button", name="Add to my songs").click()
        guest.get_by_role("button", name="The queue", exact=True).click()
        expect(guest.locator("#flash")).to_be_hidden(timeout=15000)
        shot(guest, "phone-queue")

        admin = desk.new_page()
        admin.goto(base + "/admin")
        admin.get_by_label("Password", exact=True).fill(PASSWORD)
        admin.get_by_role("button", name="Sign in", exact=True).click()
        expect(admin.locator("#page")).to_be_visible()
        admin.wait_for_timeout(800)
        shot(admin, "admin-settings")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with preview_server(fill_library, PASSWORD, KARAQUEUE_PUBLIC_URL="https://karaoke.example.com",
                        KARAQUEUE_YTDLP="no-yt-dlp-here") as base:  # no real YouTube videos in the shots
        take(base, args.out)
    print("Screenshots in", args.out)
