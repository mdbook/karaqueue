# Public access

Guests on your wifi can always reach KaraQueue. For guests on their phone's data, or a party somewhere else, the server needs an address on the internet. If you run it at home with no domain of your own, the admin page can do most of that for you, under Settings, Public access:

1. **A name that follows you.** Home internet addresses change now and then. A free dynamic DNS name, like `myparty.duckdns.org`, always points at your current one, and KaraQueue keeps it up to date.
2. **A way in through your router.** Your router turns away visits from the internet unless you tell it to send them to KaraQueue. That's a port forward, and KaraQueue can ask the router for one.

Both are off until you turn them on. With them off, KaraQueue never contacts any of these services.

If your internet provider uses carrier-grade NAT, neither will work, and you want a [tunnel](#when-it-cant-work-use-a-tunnel) instead. The admin page tells you when it looks that way.

## Step 1: a dynamic DNS name

Make a free account and a hostname at one of these, then pick it under Dynamic DNS and fill in the fields it needs:

| Provider | Hostname | Username | Token or password |
| --- | --- | --- | --- |
| [DuckDNS](https://www.duckdns.org) | `myparty.duckdns.org` (or just `myparty`) | not used | The token at the top of your DuckDNS page |
| [No-IP](https://www.noip.com) | `myparty.ddns.net` | Your No-IP username or email | Your No-IP password |
| [Dynu](https://www.dynu.com) | `myparty.freeddns.org` | Your Dynu username | Your Dynu password |
| [FreeDNS](https://freedns.afraid.org) | Optional, for the link shown | not used | The update token: open Dynamic DNS, copy the Direct URL, and take the long part after `update.php?` |
| Custom update URL | Optional, fills in `{hostname}` | Optional, sent with the token as a password | Optional |

For any other provider, pick Custom update URL and paste its update address, with `{ip}` where your address goes and `{hostname}` where the name goes, like `https://dns.example.net/update?host={hostname}&ip={ip}&key=...`. KaraQueue counts any 2xx answer as success.

**No-IP's free names expire.** No-IP emails you every 30 days asking you to confirm the hostname. Click the link in that email, or the name stops working. Updates from KaraQueue don't count as confirming it.

KaraQueue looks up your public address every few minutes (Check the address every), using [ipify](https://www.ipify.org) and, if that doesn't answer, [icanhazip](https://icanhazip.com). It tells the provider only when the address changes, plus once a day in case the provider lost it. If the provider turns the update down (a wrong password, say), KaraQueue stops trying until you change a setting or press Check now, since providers block names that keep sending bad updates. If the provider says it's having trouble (No-IP's `911`, say), KaraQueue waits 30 minutes before trying again, then twice as long each time it still fails, up to a day. KaraQueue never follows a redirect from a provider, so a password can't be sent on to some other address.

The token, password and custom update URL are kept like the YouTube API key: the admin page only shows whether they're set, and they never go in the server log. You can also set them in the environment instead, see [Configuration](configuration.md).

## Step 2: open the port on your router

Turn on Open the port on the router. KaraQueue asks the router for a forward, trying UPnP first, then NAT-PMP, then PCP, and renews it while the server runs. The forward comes off when the server stops. The admin page shows which one worked and the port guests use. Port on the router picks that port; `0` uses the same one as the forward leads to on this machine, which is the one KaraQueue listens on (`KARAQUEUE_PORT`, `8000` by default) unless you set `KARAQUEUE_FORWARD_TO_PORT`.

Plenty of routers do this out of the box. Some need UPnP or NAT-PMP turned on in their settings first, and some never allow it.

KaraQueue remembers every forward it made, in its database, until the router confirms it's off. If the server crashes or is killed before it can take the forward off, if a renewal or removal fails because the router didn't answer, or if you turn Open the port on the router off while the server is down, KaraQueue takes the old forward off at its next start, and keeps trying every few minutes until the router says it's gone. With UPnP it first checks that the forward is still its own (named KaraQueue, pointing at this machine), so it never removes another device's.

Most forwards last an hour and are renewed while the server runs, so a forward left by a crash closes by itself within the hour. Some routers only take permanent forwards. KaraQueue says so on the admin page and in the log when that happens: a permanent forward left by a crash stays open until KaraQueue starts again and takes it off.

### https through the forward

By default the forward leads straight to KaraQueue, over plain `http`. To give guests `https` instead, run an https server like [Caddy](install.md#https-at-home-with-caddy) on the same machine, in front of KaraQueue, and point the forward at it:

- `KARAQUEUE_FORWARD_TO_PORT=443`, the port Caddy listens on for https, and
- Port on the router `443`, so the guest link becomes `https://myparty.duckdns.org`.

Caddy gets its certificate over port 443 by itself, so that one forward is enough. `KARAQUEUE_FORWARD_TO_PORT` is set in the environment only, so whoever gets into the admin page can't use it to open some other program on the machine to the internet. The forward only ever leads to this machine.

### Forwarding the port by hand

If the router says no, set up the forward yourself. The admin page shows the numbers to use:

1. Open your router's admin page. It's usually printed on a sticker on the router, or try `http://192.168.1.1` or `http://192.168.0.1`.
2. Find Port forwarding. Some routers call it Virtual server, NAT, or Applications and gaming.
3. Add a rule: protocol TCP, outside port `8000` (or your Port on the router), inside address the KaraQueue machine's address on your network, inside port `8000` (or your `KARAQUEUE_PORT`).
4. Give the KaraQueue machine a fixed address on your network (often called DHCP reservation), so the rule keeps pointing at it.

### In Docker

The router can only be asked from inside your home network, and Docker's own network hides it. For KaraQueue's port forwarding to work, run the container with host networking: in `compose.yaml`, replace the `ports:` lines with `network_mode: host`. Otherwise forward the port by hand. Dynamic DNS works either way.

## Check it works

The admin page shows the guest link, like `http://myparty.duckdns.org:8000`, with a button to make it the Public address used for join links and QR codes. Try it from a phone with wifi turned off. From inside your own network the link may not open even when it works for everyone else, because some routers don't loop back to themselves.

The link is plain `http` unless the forward goes to an https server. For `https`, which browsers need for the mic, casting and speaker picking on screens, and which keeps passwords from travelling unencrypted, put [Caddy in front](install.md#https-at-home-with-caddy) and [point the forward at it](#https-through-the-forward). Caddy gets and renews a free certificate for your dynamic DNS name by itself.

## When it can't work: use a tunnel

A port forward only helps if your router has its own address on the internet. Many internet providers, especially mobile and satellite ones and some fiber ones, share one address between lots of customers (carrier-grade NAT). Then visits from the internet never reach your router, whatever you set.

KaraQueue asks your router for its internet address and warns you on the admin page when:

- the router's address is in `100.64.0.0/10`, the range set aside for carrier-grade NAT,
- the router's address is a home network one (`10.x`, `172.16.x` to `172.31.x`, `192.168.x`), so there's another router in front of yours, or
- the router's address isn't the one the internet sees.

In those cases, use a tunnel instead. The KaraQueue machine reaches out to the tunnel service, and guests reach it through there, with https included, and no port forward or dynamic DNS needed:

- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/), free, needs a domain on Cloudflare. Point the tunnel at `http://localhost:8000`.
- [Tailscale Funnel](https://tailscale.com/kb/1223/funnel), free for personal use, gives you a `ts.net` address. Run `tailscale funnel 8000`.

Then set Public address on the admin page to the tunnel's https address, and turn Dynamic DNS and Open the port on the router off. The tunnel connects from the same machine, so the default `KARAQUEUE_TRUSTED_PROXIES` sees each guest's real address if the tunnel passes it on in `X-Forwarded-For`.

## Keeping it safe on the internet

Over a plain http link, passwords and sign-ins travel unencrypted. Don't sign in as admin from outside your network until you have https (see [https through the forward](#https-through-the-forward)). On a public wifi, anyone listening could pick up the admin password otherwise.

Once it's public, anyone who finds the address can open the home page. Guests still need a room code to join, and wrong guesses are rate limited. Wrong admin passwords from many addresses can lock the admin password for a while, but never for an address that hasn't got it wrong itself, so strangers can't keep you out.

That has a cost. Someone with lots of addresses gets one guess at the admin password from each fresh one, and the only cap on them is the limit per address (10 wrong tries in 15 minutes from one IPv4 address, or one IPv6 /64). Named accounts don't get this exception: 50 wrong tries for one name lock it for everyone, wherever they come from. So make the admin password long, a few random words at least, and use named accounts for everyone else. Keep Who can start rooms on account holders, use a strong admin password, and see [Admin and accounts](admin.md).
