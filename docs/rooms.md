# Rooms, the rotation and hosting

## Rooms

A room is one party. The host starts it from the home page (Host a night), gives it a name, and gets a short code like `K7QM2X`. One server can run as many rooms as you want at once, and each has its own guests, songs, screens and rules. A room nobody has touched in `KARAQUEUE_ROOM_DAYS` days (30 by default) gets deleted, unless a phone or screen still has it open.

Guests join by scanning the QR code on the big screen or the host page, or by going to the server and typing the code. They pick a name and they're in. No account, no app.

## How the rotation works

KaraQueue keeps a list of singers in order, and each singer keeps their own list of songs. When a song ends (or the host presses Next song), the next singer after whoever just sang gets their turn, and they sing the first song on their own list.

That gives you a few nice properties without anyone having to referee:

- **Everyone sings once before anyone sings twice.** Queuing ten songs doesn't get you ten turns in a row, it just means you're never out of ideas.
- **Late arrivals join at the back of the line.** Someone who shows up at midnight waits their turn like everyone else.
- **An empty list skips you, not the party.** Singers with nothing queued are passed over until they add something, and keep their spot.
- **You control your own setlist.** Guests reorder or remove their songs from their phone any time before they come up.

Guests see where they are on their phone ("You're 5th in line") and the big screen shows who's up next.

![A guest's phone: their place in line, who's singing, and who's coming up](screenshots/phone-queue.png)

## What the host can do

The host page (`/r/<code>/host`) is the control booth:

- **The queue.** Next song, pause requests (guests can still look, they just can't add), and the whole rotation. Move singers earlier or later, skip someone for now (they keep their spot and songs, and come back when you put them back in), or remove them, their songs and their recordings.
- **Add a song for someone.** Search the library or YouTube for a guest and add it to their list, or add a guest by hand for someone without a phone. The host isn't held to the song limit.
- **Screens & sound.** Every TV, what it shows, where the sound comes from, the mic and recording. See [Screens](screens.md).
- **Room settings.** How many songs each guest can queue, and whether guests can use YouTube, the song library, or both.
- **Recordings.** Everyone's takes, when recording is on.
- **Invite co-host.** Copies a link that signs another device in as host, for when you want to sing too.

Next song asks first if someone is still mid-song, so a stray tap doesn't cut off a singer.

## Getting back in

Phones die, browsers get cleared, people switch to a friend's phone. A guest can get their spot and songs back a few ways:

- **From another phone they're signed in on:** Use another phone, at the bottom of their page, shows a QR code that signs the new phone in as them.
- **From the host:** the menu next to each singer has Sign in their phone, which shows the same kind of code. It works once, for 10 minutes.
- **With a guest account:** guests can make one from the join page with just a name and a password (no email). It works in every room on the server, so signing in and pressing Join as (their name) puts them right back on their spot. A guest who joined first can tie their spot to an account later, from the bottom of their page.

One singer can be signed in on 5 phones at once. A sixth signs the oldest out.
