# Security

KaraQueue is meant to be safe to put on the internet for a party: guests get in with a room code, but they can't take over the TV, read the server's files, or get into the admin page. If you find a way around any of that, I'd really like to know.

## Reporting a problem

Please report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. Don't open a public issue, and give me a reasonable chance to fix it before you share it anywhere.

Helpful things to include: what someone could do, the steps to show it, and which version (or commit) you tested.

## What's in scope

The latest release and the `main` branch. Things like:

- getting host, admin or another singer's access without their key or password,
- controlling a screen the host hasn't let in,
- reading, writing or deleting files outside the folders KaraQueue is set up to use,
- making the server fetch something other than YouTube, MusicBrainz or the YouTube API,
- running scripts in someone else's browser through KaraQueue's pages.

Out of scope: rate limits being too loose or too tight (they're a setting), anything that needs the admin password, and problems in a reverse proxy or network you put in front of KaraQueue.

## How it protects itself

[Admin and accounts](docs/admin.md#how-sign-in-works) explains keys, passwords and sign-ins, and [Configuration](docs/configuration.md#two-settings-the-admin-page-cant-change) covers the limits on what the admin page can change.
