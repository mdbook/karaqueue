# Configuration

Everything is an environment variable, and all of them are optional. Out of the box KaraQueue runs with no setup at all: guests can paste YouTube links, the server keeps its database in `karaqueue.db`, and the server writes a one-time link for picking an admin password (see [The first time](install.md#the-first-time)).

Apart from the ones marked "set at startup only", you can also change any of these on the admin page while the server runs. A value saved there wins over the environment until you reset it, and the admin page shows where each value comes from (set here, the environment, or the default). [`.env.example`](../.env.example) has the common ones ready to fill in.

| Variable | Default | What it does |
| --- | --- | --- |
| `KARAQUEUE_DB` | `karaqueue.db` | Where the SQLite database lives. Set at startup only. |
| `KARAQUEUE_HOST` | `0.0.0.0` | Address to listen on. Set at startup only. |
| `KARAQUEUE_PORT` | `8000` | Port to listen on. Set at startup only. |
| `KARAQUEUE_ADMIN_PASSWORD` | (none) | The admin page password. Without it, the server saves a one-time link to pick one, see below. Set at startup only. |
| `KARAQUEUE_ALLOWED_FOLDERS` | (the folders below) | Folders, separated by `:`, that the admin page may point the library, downloads and recordings at. Set at startup only. |
| `KARAQUEUE_PUBLIC_URL` | (from the request) | The address guests should use, for the join link and QR code. Set this if you run behind a reverse proxy. |
| `KARAQUEUE_NEW_ROOMS` | `accounts` | Who can start rooms: `accounts` for people with an account or the admin password (see [Accounts](admin.md#accounts)), or `anyone` who can reach the server. |
| `KARAQUEUE_SINGERS_PER_ROOM` | `100` | How many guests can join one room. The host can still add more by hand. |
| `KARAQUEUE_SCREENS_PER_ADDRESS` | `4` | How many screens on one internet connection can wait for the host to let them in at once. |
| `KARAQUEUE_SONGS_PER_SINGER` | `10` | How many songs a guest can have queued at once. The host isn't limited. |
| `KARAQUEUE_NAME_LIMIT` | `32` | Longest allowed singer name. |
| `KARAQUEUE_CODE_LENGTH` | `6` | Length of room codes, 5 to 12. Longer codes are harder to guess. |
| `KARAQUEUE_RATE_LIMIT` | `1` | Caps how fast one address can guess room codes, start rooms, join and search. `2` doubles every cap, `0` turns them off. Wrong passwords have their own cap (see [Admin and accounts](admin.md#how-sign-in-works)). Raise it if lots of guests share one internet connection. |
| `KARAQUEUE_TRUSTED_PROXIES` | `127.0.0.1` | Addresses (or networks, comma-separated) of your reverse proxy, so KaraQueue sees each guest's real address. See [Behind a reverse proxy](install.md#behind-a-reverse-proxy). Set at startup only. |
| `KARAQUEUE_MEDIA_DIR` | (none) | A folder of karaoke videos on the server, or several separated by `:`. Turns on the song library, see below. |
| `KARAQUEUE_YOUTUBE_API_KEY` | (none) | A YouTube Data API key. Turns on song search in the request form. Without it, guests can still paste YouTube links. |
| `KARAQUEUE_RECORDINGS_DIR` | (none) | A folder to keep recorded performances in. Turns on recording, see Screens. |
| `KARAQUEUE_RECORDING_MAX_MB` | `100` | Biggest recording the server will take, in megabytes. An hour of singing is about 30. |
| `KARAQUEUE_RECORDINGS_ROOM_MB` | `1000` | How much one room's recordings can add up to, in megabytes. |
| `KARAQUEUE_RECORDINGS_MAX_GB` | `20` | How much every room's recordings can add up to, in gigabytes. |
| `KARAQUEUE_SAVE_DIR` | (none) | Where songs saved from YouTube go for good, see below. Empty means a `YouTube` folder inside the first library folder. |
| `KARAQUEUE_WHO_SAVES` | `hosts` | `hosts` lets anyone hosting a room save YouTube songs into the library from the host page; `admins` keeps it to the admin page. |
| `KARAQUEUE_SAVES_PER_HOUR` | `10` | YouTube songs one room's host can save into the library in an hour. Admins aren't limited. |
| `KARAQUEUE_SAVE_MAX_MB` | `500` | Songs bigger than this aren't saved. |
| `KARAQUEUE_DOWNLOADS_DIR` | (none) | A folder to keep downloaded YouTube songs in. Turns on yt-dlp, see below. |
| `KARAQUEUE_DOWNLOADS_MAX_GB` | `10` | How big the downloads folder can get. The songs played longest ago go first. |
| `KARAQUEUE_DOWNLOADS_PER_HOUR` | `200` | How many songs that aren't on the server yet one room can download in an hour. Songs past that still go in the queue and download later, or play from YouTube's player when their turn comes. |
| `KARAQUEUE_SONG_LOOKUP` | `musicbrainz` | Where Look up finds a song's real title and artist on the admin page. `off` means the server never asks anyone. |
| `KARAQUEUE_CONVERSIONS` | `2` | How many old-format videos (AVI, MPG and so on) can convert at once, across every screen. |
| `KARAQUEUE_YTDLP` | `yt-dlp` | The yt-dlp command, if it isn't on the `PATH`. Set at startup only. |
| `KARAQUEUE_YTDLP_FORMAT` | (mp4, up to 1080p) | A yt-dlp `-f` format string, if you want something else. |
| `KARAQUEUE_ROOM_DAYS` | `30` | Rooms nobody has changed in this many days get deleted, along with their recordings. A room with a phone or screen still open is kept. `0` keeps rooms forever. |

## Two settings the admin page can't change

So that whoever gets into the admin page still can't take over the server:

- The yt-dlp command comes from `KARAQUEUE_YTDLP` only.
- Library, saved-songs, downloads and recordings folders have to be inside `KARAQUEUE_ALLOWED_FOLDERS`. Without it, they have to be inside the folders the environment already sets. So with `KARAQUEUE_MEDIA_DIR=/media`, the admin page can add `/media/Rock` but not `/etc`.

The server logs a warning at startup for any saved value it ignores because of these rules.

## Where things live

| What | Where | Worth backing up? |
| --- | --- | --- |
| Rooms, singers, songs, accounts, settings | The SQLite file at `KARAQUEUE_DB` | Yes, see [Backups](install.md#backups) |
| Admin setup link (first run only) | `admin-setup-link.txt` next to the database | No, it goes away once you pick a password |
| Recordings | `KARAQUEUE_RECORDINGS_DIR` | If your guests want their takes |
| Songs saved from YouTube | `KARAQUEUE_SAVE_DIR`, or `YouTube` inside the first library folder | Yes, they're part of your library now |
| Downloaded YouTube songs | `KARAQUEUE_DOWNLOADS_DIR` | No, it's a cache that refills itself |
