# The desktop app

KaraQueue as an app for your computer: download it, open it, and the party's ready. It runs the same server as the Docker setup, in a window of its own, with ffmpeg, yt-dlp and a tunnel program built in, so YouTube songs and guests from anywhere work without installing anything else.

Use it when the karaoke computer is a laptop or desktop you're already at. For a server that stays on all the time, the [Docker setup](install.md) is still the better fit.

## Download and open it

Get the file for your computer from the [latest release](https://github.com/mdbook/karaqueue/releases/latest):

| Computer | File |
| --- | --- |
| Windows 10 or 11 (64-bit) | `KaraQueue-Setup-windows-x64.exe`, or `KaraQueue-windows-x64.zip` to run it without installing |
| Mac with Apple silicon (M1 or newer) | `KaraQueue-macos-arm64.dmg` |
| Linux (64-bit) | `KaraQueue-linux-x64.tar.gz` |

The app isn't signed by Apple or Microsoft yet, so the first time, your computer asks if you're sure:

- **Windows:** run the installer. If SmartScreen says "Windows protected your PC", click **More info**, then **Run anyway**. When KaraQueue first starts, Windows asks whether to let it through the firewall: allow it on **private networks**, or phones on your Wi-Fi can't reach it.
- **Mac:** open the `.dmg` and drag KaraQueue into Applications. The first time, right-click (or Control-click) KaraQueue in Applications and choose **Open**, then **Open** again. If your Mac only offers to move it to the bin, open Terminal and run `xattr -dr com.apple.quarantine /Applications/KaraQueue.app`, then open it normally.
- **Linux:** extract it with `tar -xzf KaraQueue-linux-x64.tar.gz` and run `KaraQueue/KaraQueue`. It needs a desktop with X11 or Wayland; on Ubuntu or Debian, if no window appears, run `sudo apt install libxcb-cursor0`. Without a window it opens in your browser instead and keeps running.

## The first five minutes

1. **Pick an admin password.** The window opens on the dashboard with a welcome card. Click **Pick a password** and choose one: it's what you sign in with to host. Use a few random words.
2. **Add songs.** Put karaoke files (MP4, MP3+CDG and the rest, see [Your songs](library.md)) in the Songs folder. It starts as `Music/KaraQueue` in your home folder; **Change** points it anywhere else. No files? YouTube works straight away.
3. **Let guests in from anywhere (optional).** Turn on **Public tunnel**. In a few seconds the dashboard shows an `https://...trycloudflare.com` address and its QR code. It's free and needs no account. Leave it off and guests on your Wi-Fi can still join.
4. **Host a night.** Click **Host a night**, sign in with the admin password, start a room, and open its screen link on the TV. Guests scan the QR code on the TV.

## What's on the dashboard

- **Guests join here:** the address phones should use, as a QR code and a link to copy. That's this computer's address on your Wi-Fi, or the tunnel's address while the tunnel is up. Join links and QR codes on the host page and TV screen use the same address.
- **Let guests in from anywhere:** the tunnel switch and how it's doing. It uses a [Cloudflare quick tunnel](public-access.md#a-tunnel-the-easy-way), which gets a new random address each time KaraQueue starts. For an address that stays the same, pick a Cloudflare named tunnel or Tailscale Funnel under Tunnel on the admin page.
- **Host a night** and **Admin page** open those pages in the window. The KaraQueue menu (Dashboard) brings you back.
- **Songs folder:** where the library lives.

The menus also open the songs folder, the data folder and the log.

Closing the window stops KaraQueue, and with it the tunnel, so guests and screens disconnect. It asks first.

## Where things live

Everything KaraQueue keeps (the database, YouTube downloads, recordings and the log) goes in its data folder:

| Computer | Data folder |
| --- | --- |
| Windows | `%APPDATA%\KaraQueue` |
| Mac | `~/Library/Application Support/KaraQueue` |
| Linux | `~/.local/share/karaqueue` |

To back up, copy the data folder while KaraQueue is closed. Uninstalling the app leaves it, so your rooms, accounts and settings are there if you install again.

## Settings

Most settings are on the admin page, the same as any KaraQueue server. The few that only apply at startup go in `config.env` in the data folder, one per line, like:

```dotenv
KARAQUEUE_PORT=8000
```

Restart KaraQueue after changing it. The names are in the [configuration reference](configuration.md). The app starts on port 8000, or the next free one if something else has it. The admin page can point the library at any folder in your home folder.

## Updating

Download the new release and install it over the old one (on a Mac, replace the app in Applications). Your data folder stays as it is, and the database upgrades itself on the first start.

## Building it yourself

The release files are built by [the desktop workflow](../.github/workflows/desktop.yml) on GitHub, for each system on its own runner. To build on your own computer, with Python 3.9 or newer:

```bash
pip install . pyinstaller pillow "yt-dlp[default]" pywebview   # on Linux: "pywebview[qt]"
python desktop-build/fetch_tools.py     # downloads ffmpeg, deno and cloudflared for this system
pyinstaller desktop-build/karaqueue.spec
python desktop-build/smoke.py dist/KaraQueue/KaraQueue   # starts it and checks it works
```

The app lands in `dist/` (`dist/KaraQueue.app` on a Mac). To run the desktop app from a source checkout instead, `pip install -e '.[desktop]'` and run `karaqueue-desktop`; it uses whatever ffmpeg, yt-dlp and cloudflared are on your `PATH`.
