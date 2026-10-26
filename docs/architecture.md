# How it's built

KaraQueue is deliberately small: one Python process, one SQLite file, and plain HTML and JavaScript pages with no build step. You can read the whole thing in an afternoon.

## The pieces

| File | What it does |
| --- | --- |
| `karaqueue/app.py` | The FastAPI app: the HTTP API, the `/ws/<code>` WebSocket, the pages, and `main()` for the `karaqueue` command. |
| `karaqueue/store.py` | Rooms, singers, songs, the rotation, accounts and sign-ins, all in SQLite. The rules about who can do what live here. |
| `karaqueue/displays.py` | Live sockets per room, and the screens: pairing, layouts, sound and the commands the host sends them. |
| `karaqueue/settings.py` | Every server setting, where its value comes from (admin page, environment, default), and its limits. |
| `karaqueue/media.py` | The song library: scanning folders, reading names, naming rules, MP3+CDG pairs, converting old formats with ffmpeg, MusicBrainz lookups. |
| `karaqueue/downloads.py` | yt-dlp: the downloads cache, search without an API key, and saving songs into the library. |
| `karaqueue/youtube.py` | Pulling a video id out of whatever people paste, and YouTube Data API search. |
| `karaqueue/limits.py` | Per-address rate limits. |
| `karaqueue/guards.py` | Security headers, request size caps and slowing down password guessing. |
| `karaqueue/static/` | The pages: `index.html` (home), `room.html` (guests), `host.html`, `screen.html` (TVs), `admin.html`, plus shared `kq.js`, the CD+G renderer `kq-cdg.js` and the styles. |

## How a song gets sung

1. A guest's phone `POST`s a song to `/api/rooms/<code>/songs` with their singer key.
2. The store adds it to the end of that singer's list and the app tells every socket in the room that something changed.
3. Every phone, host page and screen fetches the new state and redraws. There's no client-side state worth losing, so a reload is always safe.
4. When the song on the screen ends, the screen tells the server, which picks the first song of the next singer in the rotation. With several screens in a room, the server makes sure the show only moves on once.
5. Library songs and downloaded YouTube songs play from a signed link that works for one room and runs out after a few hours, so knowing a room code doesn't open up the whole library.

## Keys and trust

Nobody needs a password to sing. Starting a room gives the host's browser a host key, and joining gives a guest a singer key. Keys are random and the server only keeps a hash of each. Screens have to be let in by the host once, by a code they show, before they can be controlled or save recordings. Admin, host and guest account passwords are salted scrypt hashes. The details are in [Admin and accounts](admin.md#how-sign-in-works), and how to report a problem is in [SECURITY.md](../SECURITY.md).

## Tests

`tests/test_store.py` covers the rotation and permission rules, `tests/test_api.py` the web API and sockets, and the rest one module each. `tests/browser_smoke.py` drives every page in Chromium against a throwaway server, and `tests/readme_screenshots.py` takes the pictures in these docs the same way. See [CONTRIBUTING.md](../CONTRIBUTING.md).
