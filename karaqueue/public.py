"""Putting a home server on the internet: a free dynamic DNS name that follows the home
connection's address (DuckDNS, No-IP, Dynu, FreeDNS, or any update URL), and asking the router
to forward the port (UPnP IGD, NAT-PMP or PCP). Standard library only.

None of it helps behind carrier-grade NAT, where the router itself has no public address.
`nat_problem` spots the likely cases, so the admin page can point to a tunnel instead.

Provider tokens never go in the log or in a status message: errors are described in our own
words, never by echoing an exception (urllib's can carry the request URL, and with it the token).
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import logging
import os
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterator, NamedTuple, Optional, Tuple

log = logging.getLogger("karaqueue")

USER_AGENT = "KaraQueue/2 https://github.com/mdbook/karaqueue"  # No-IP turns away updates without one
IP_SERVICES = ("https://api.ipify.org", "https://ipv4.icanhazip.com")  # tried in order
CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Home network ranges (not ip_address.is_private, which also counts the documentation ranges).
LAN = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
LEASE = 3600  # seconds a router port mapping lasts; renewed at half time
REFRESH = 86400  # send an unchanged address again once a day, in case the provider lost it
RETRY = 1800  # after a provider's "try later" (No-IP asks for 30 minutes), doubling up to REFRESH
SWEEP = 300  # how soon to try again to take a stale forward off the router
DESCRIPTION = "KaraQueue"  # how our forwards are named on the router, to tell them from others
FORWARDS_KEY = "public:forwards"  # in the store: forwards that may still be on the router
HTTP_ERRORS = (OSError, http.client.HTTPException, ValueError)
TUNNEL_DOCS = {"Cloudflare Tunnel": "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/",
               "Tailscale Funnel": "https://tailscale.com/kb/1223/funnel"}


# ---- dynamic DNS providers ---------------------------------------------------------------

class Result(NamedTuple):
    ok: bool
    message: str
    retry: bool = True  # False: trying again with the same settings only makes things worse


def _request(url: str, user: str = "", password: str = "") -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if user or password:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    return req


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. None of these services needs one, and urllib would carry the Basic
    auth header along to wherever it points, another host or plain http included. The 3xx comes
    back as the answer instead, which reads as a failure."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def open_url(req: urllib.request.Request, timeout: float) -> Any:
    return OPENER.open(req, timeout=timeout)


def fetch(req: urllib.request.Request, timeout: float = 10, limit: int = 4096) -> Tuple[int, str]:
    """Status and the start of the body, error statuses included. Can't connect: an OSError."""
    try:
        with open_url(req, timeout=timeout) as res:
            return res.status, res.read(limit).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(limit).decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        return exc.code, body


def duckdns_request(hostname: str, username: str, token: str, ip: str, url: str = "") -> urllib.request.Request:
    sub = hostname.lower()
    if sub.endswith(".duckdns.org"):
        sub = sub[:-len(".duckdns.org")]
    return _request("https://www.duckdns.org/update?" + urllib.parse.urlencode({"domains": sub, "token": token, "ip": ip}))


def duckdns_result(status: int, body: str) -> Result:
    if status == 200 and body.startswith("OK"):
        return Result(True, "Updated")
    if status == 200:  # DuckDNS answers KO for a wrong token or subdomain, and says nothing else
        return Result(False, "DuckDNS turned it down. Check the hostname and the token.", retry=False)
    return Result(False, f"DuckDNS answered with an error ({status}). Trying again later.")


def _dyndns2(base: str) -> Callable[..., urllib.request.Request]:
    def build(hostname: str, username: str, token: str, ip: str, url: str = "") -> urllib.request.Request:
        return _request(base + "?" + urllib.parse.urlencode({"hostname": hostname, "myip": ip}), username, token)
    return build


DYNDNS2 = {  # the replies No-IP, Dynu and many others share
    "badauth": ("The username or password is wrong.", False),
    "nohost": ("That hostname isn't in this account.", False),
    "notfqdn": ("Type the whole hostname, like myparty.ddns.net.", False),
    "numhost": ("Too many hostnames in one update.", False),
    "badagent": ("The provider turned KaraQueue's updater away.", False),
    "!donator": ("That needs a paid account at the provider.", False),
    "abuse": ("The provider blocked this hostname for too many updates.", False),
    "dnserr": ("The provider is having trouble. Trying again later.", True),
    "911": ("The provider is having trouble. Trying again later.", True),
}


def _dyndns2_result(name: str) -> Callable[[int, str], Result]:
    def check(status: int, body: str) -> Result:
        word = body.split()[0] if body else ""
        if status == 200 and word in ("good", "nochg"):
            return Result(True, "Updated" if word == "good" else "Up to date")
        if word in DYNDNS2:
            message, retry = DYNDNS2[word]
            return Result(False, f"{name}: {message}", retry)
        if status == 401:
            return Result(False, f"{name}: {DYNDNS2['badauth'][0]}", False)
        return Result(False, f"{name} answered with something unexpected ({status}). Trying again later.")
    return check


def freedns_request(hostname: str, username: str, token: str, ip: str, url: str = "") -> urllib.request.Request:
    # The token is the long string after "update.php?" in FreeDNS's Direct URL; it names the host.
    return _request(f"https://freedns.afraid.org/dynamic/update.php?{urllib.parse.quote(token, safe='')}"
                    f"&address={urllib.parse.quote(ip)}")


def freedns_result(status: int, body: str) -> Result:
    if status == 200 and body.startswith("Updated"):
        return Result(True, "Updated")
    if "has not changed" in body:  # FreeDNS words this one as an ERROR, but it's fine
        return Result(True, "Up to date")
    if body.startswith("ERROR"):
        return Result(False, "FreeDNS turned it down. Check the update token.", retry=False)
    return Result(False, f"FreeDNS answered with something unexpected ({status}). Trying again later.")


def custom_request(hostname: str, username: str, token: str, ip: str, url: str = "") -> urllib.request.Request:
    filled = url.replace("{ip}", urllib.parse.quote(ip)).replace("{hostname}", urllib.parse.quote(hostname))
    return _request(filled, username, token) if username else _request(filled)


def custom_result(status: int, body: str) -> Result:
    word = body.split()[0].lower() if body else ""
    if 200 <= status < 300 and word not in ("ko", "error", "error:", *DYNDNS2):
        return Result(True, "Updated")
    if status in (401, 403) or word in ("badauth", "ko"):
        return Result(False, "The update URL turned it down. Check it, and the username and password.", retry=False)
    return Result(False, f"The update URL answered with an error ({status}). Trying again later.")


class Provider(NamedTuple):
    label: str
    needs: Tuple[str, ...]  # settings that have to be filled in
    request: Callable[..., urllib.request.Request]
    result: Callable[[int, str], Result]


PROVIDERS = {
    "duckdns": Provider("DuckDNS", ("hostname", "token"), duckdns_request, duckdns_result),
    "noip": Provider("No-IP", ("hostname", "username", "token"),
                     _dyndns2("https://dynupdate.no-ip.com/nic/update"), _dyndns2_result("No-IP")),
    "dynu": Provider("Dynu", ("hostname", "username", "token"),
                     _dyndns2("https://api.dynu.com/nic/update"), _dyndns2_result("Dynu")),
    "freedns": Provider("FreeDNS", ("token",), freedns_request, freedns_result),
    "custom": Provider("Custom update URL", ("url",), custom_request, custom_result),
}
FIELD_LABELS = {"hostname": "Hostname", "username": "Username", "token": "Token or password", "url": "Custom update URL"}


def update(provider: str, ip: str, hostname: str = "", username: str = "", token: str = "", url: str = "") -> Result:
    """Point the provider's name at `ip`."""
    p = PROVIDERS[provider]
    values = {"hostname": hostname, "username": username, "token": token, "url": url}
    missing = [FIELD_LABELS[k] for k in p.needs if not values[k]]
    if missing:
        return Result(False, f"{p.label} needs: {', '.join(missing)}.", retry=False)
    try:
        status, body = fetch(p.request(hostname, username, token, ip, url))
    except HTTP_ERRORS:
        return Result(False, f"Couldn't reach {p.label}. Trying again later.")
    return p.result(status, body)


def public_ip() -> Optional[str]:
    """This connection's IPv4 address as the internet sees it."""
    for service in IP_SERVICES:
        try:
            status, body = fetch(_request(service), timeout=5)
            if status == 200:
                return str(ipaddress.IPv4Address(body.strip()))
        except HTTP_ERRORS:
            continue
    return None


def nat_problem(router_ip: Optional[str], public: Optional[str]) -> Optional[str]:
    """Why a port forward on this router can't reach the internet, or None if it looks fine
    (or there's no telling). `router_ip` is the address the router says it has on the internet."""
    try:
        wan = ipaddress.ip_address(router_ip or "")
    except ValueError:
        return None
    if wan in CGNAT:
        return (f"Your router's internet address ({wan}) is a shared one from your internet provider "
                "(carrier-grade NAT), so guests can't reach a port on it. Use a tunnel instead.")
    if any(wan in net for net in LAN):
        return (f"Your router's internet address ({wan}) is a private one, so there's another router in front of it. "
                "Forward the port on that one too, or use a tunnel instead.")
    if public and str(wan) != public:
        return (f"Your router says its internet address is {wan}, but the internet sees {public}. That usually "
                "means carrier-grade NAT, and guests can't reach a port on it. Use a tunnel instead.")
    return None


# ---- asking the router to forward the port ----------------------------------------------

class GatewayError(Exception):
    """The router said no, or didn't answer. The message is shown on the admin page. `code` is
    the router's own error code, when it gave one (UPnP's 725, say)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class OddReply(GatewayError):
    """A packet that isn't the answer to what we asked. Over UDP, the next one may still be."""

    def __init__(self) -> None:
        super().__init__("the router sent something odd")


def clean_text(text: str, limit: int = 120) -> str:
    """Words from a device on the network, made safe for the log and the admin page: one line of
    printable characters, not too long (so nothing on the network can forge log lines)."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:limit]


def in_container() -> bool:
    """Running in Docker or Podman, where this machine's own address is the container's."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def udp_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)


def local_ip_for(host: str) -> Optional[str]:
    """This machine's address on the way to `host`. A UDP connect sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def default_gateway() -> Optional[str]:
    """The router's address, from the routing table (Linux). None elsewhere, or if there's none."""
    try:
        with open("/proc/net/route") as table:
            for line in table.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError, IndexError, struct.error):
        pass
    return None


SSDP = ("239.255.255.250", 1900)
IGD_TYPES = ("urn:schemas-upnp-org:device:InternetGatewayDevice:2",
             "urn:schemas-upnp-org:device:InternetGatewayDevice:1")
WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2",
                "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")
UPNP_ERRORS = {"718": "the port is already forwarded to another device",
               "724": "the router wants the same port inside and out",
               "725": "the router only takes permanent forwards",
               "606": "the router doesn't let programs open ports. Turn on UPnP in its settings"}


def ssdp_search(st: str) -> bytes:
    return ("M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP[0]}:{SSDP[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            f"ST: {st}\r\n\r\n").encode()


def ssdp_location(reply: bytes, sender: str) -> Optional[str]:
    """The description URL in a reply to our search, if it points back at the device that sent it
    (so nothing else on the network can send us off to fetch some other address)."""
    for line in reply.decode("latin-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            url = value.strip()
            parts = urllib.parse.urlsplit(url)
            if parts.scheme == "http" and parts.hostname == sender:
                return url
    return None


def parse_xml(text: str) -> ET.Element:
    """XML from a device on the network, which could be anyone's. UPnP never needs a document
    type, and turning those away means no entity tricks, even on a Python linked against an old
    expat (before 2.4.1) that doesn't guard against them itself."""
    if "<!DOCTYPE" in text.upper():
        raise ET.ParseError("no document types")
    return ET.fromstring(text)


def wan_service(description: str, location: str) -> Optional[Tuple[str, str]]:
    """The control URL and type of the router's WAN connection service, from its description XML.
    Only a control URL on the device that answered counts: a URLBase or a full controlURL could
    otherwise send our requests anywhere."""
    root = parse_xml(description)
    local = lambda el: el.tag.rsplit("}", 1)[-1]
    base = next((el.text.strip() for el in root.iter() if local(el) == "URLBase" and el.text), location)
    device = urllib.parse.urlsplit(location).hostname
    found = {}
    for service in root.iter():
        if local(service) != "service":
            continue
        fields = {local(child): (child.text or "").strip() for child in service}
        if fields.get("serviceType") in WAN_SERVICES and fields.get("controlURL"):
            control = urllib.parse.urljoin(base, fields["controlURL"])
            parts = urllib.parse.urlsplit(control)
            if parts.scheme == "http" and parts.hostname == device:
                found.setdefault(fields["serviceType"], control)
    for kind in WAN_SERVICES:
        if kind in found:
            return found[kind], kind
    return None


def soap_envelope(service: str, action: str, args: Tuple[Tuple[str, Any], ...] = ()) -> bytes:
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    return ('<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service}">{inner}</u:{action}></s:Body></s:Envelope>').encode()


def soap_fields(xml: str) -> dict:
    """Every leaf element's text, by tag without its namespace."""
    root = parse_xml(xml)
    return {el.tag.rsplit("}", 1)[-1]: (el.text or "").strip() for el in root.iter() if len(el) == 0}


class UPnP:
    label = "UPnP"

    def __init__(self, control: str, service: str, local_ip: str):
        self.control, self.service, self.local_ip = control, service, local_ip

    @classmethod
    def discover(cls, timeout: float = 2.0) -> Optional["UPnP"]:
        try:
            with udp_socket() as sock:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                sock.settimeout(0.3)
                for st in IGD_TYPES:
                    sock.sendto(ssdp_search(st), SSDP)
                until, seen = time.monotonic() + timeout, set()
                while time.monotonic() < until:
                    try:
                        reply, (sender, _) = sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    location = ssdp_location(reply, sender)
                    if not location or location in seen:
                        continue
                    seen.add(location)
                    try:
                        status, body = fetch(urllib.request.Request(location, headers={"User-Agent": USER_AGENT}),
                                             timeout=3, limit=256 * 1024)
                        found = wan_service(body, location) if status == 200 else None
                    except (*HTTP_ERRORS, ET.ParseError):
                        continue
                    local = local_ip_for(sender)
                    if found and local:
                        return cls(found[0], found[1], local)
        except OSError:  # no multicast route, say ("Network is unreachable"): no UPnP to be had here
            return None
        return None

    def call(self, action: str, *args: Tuple[str, Any]) -> dict:
        req = urllib.request.Request(self.control, data=soap_envelope(self.service, action, args), headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{self.service}#{action}"',
            "User-Agent": USER_AGENT})
        try:
            status, body = fetch(req, timeout=4, limit=64 * 1024)
            fields = soap_fields(body) if body else {}
        except (*HTTP_ERRORS, ET.ParseError):
            raise GatewayError("the router didn't answer")
        if status != 200:
            code = clean_text(fields.get("errorCode", ""), 8)
            detail = " ".join(x for x in (code or str(status), clean_text(fields.get("errorDescription", ""))) if x)
            raise GatewayError(UPNP_ERRORS.get(code) or f"the router said no ({detail})", code)
        return fields

    def external_ip(self) -> Optional[str]:
        return self.call("GetExternalIPAddress").get("NewExternalIPAddress") or None

    def add(self, internal: int, external: int, lease: int) -> Tuple[int, int]:
        def ask(lease: int) -> None:
            self.call("AddPortMapping", ("NewRemoteHost", ""), ("NewExternalPort", external), ("NewProtocol", "TCP"),
                      ("NewInternalPort", internal), ("NewInternalClient", self.local_ip), ("NewEnabled", 1),
                      ("NewPortMappingDescription", DESCRIPTION), ("NewLeaseDuration", lease))
        try:
            ask(lease)
        except GatewayError as exc:
            if exc.code != "725":
                raise
            ask(0)  # permanent: the updater says so on the admin page, and cleans up after a crash
            return external, 0
        return external, lease

    def remove(self, internal: int, external: int) -> None:
        self.call("DeletePortMapping", ("NewRemoteHost", ""), ("NewExternalPort", external), ("NewProtocol", "TCP"))

    def memo(self) -> dict:
        """What `forget` needs to know later, perhaps after a restart."""
        return {"client": self.local_ip}

    def forget(self, internal: int, external: int, memo: dict) -> None:
        """Take off a forward made earlier, if it's still ours. By now the port could belong to
        another device's forward, which must stay."""
        try:
            entry = self.call("GetSpecificPortMappingEntry", ("NewRemoteHost", ""), ("NewExternalPort", external),
                              ("NewProtocol", "TCP"))
        except GatewayError as exc:
            if exc.code == "714":  # NoSuchEntryInArray: gone already
                return
            raise
        if (entry.get("NewInternalClient") == memo.get("client", self.local_ip)
                and entry.get("NewInternalPort") == str(internal)
                and entry.get("NewPortMappingDescription") == DESCRIPTION):
            self.remove(internal, external)


NATPMP_PORT = 5351
NATPMP_ERRORS = {1: "unsupported version", 2: "the router doesn't allow it", 3: "the router has no internet connection",
                 4: "the router is out of room for forwards", 5: "unsupported request"}


def natpmp_request(op: int, internal: int = 0, external: int = 0, lifetime: int = 0) -> bytes:
    """Op 0 asks for the router's address; op 2 maps a TCP port (a lifetime of 0 removes it)."""
    if op == 0:
        return struct.pack("!BB", 0, 0)
    return struct.pack("!BBHHHI", 0, op, 0, internal, external, lifetime)


def natpmp_reply(data: bytes, op: int, internal: Optional[int] = None) -> dict:
    """The router's answer to `op`. With `internal`, only an answer about that port counts."""
    if len(data) < 8 or data[0] != 0 or data[1] != 128 + op:
        raise OddReply()
    result = struct.unpack("!H", data[2:4])[0]
    if op != 0 and len(data) >= 10 and internal is not None and struct.unpack("!H", data[8:10])[0] != internal:
        raise OddReply()  # about some other port: not our answer
    if result:
        raise GatewayError(NATPMP_ERRORS.get(result, f"the router said no ({result})"))
    if op == 0 and len(data) >= 12:
        return {"ip": socket.inet_ntoa(data[8:12])}
    if op != 0 and len(data) >= 16:
        port, external, lifetime = struct.unpack("!HHI", data[8:16])
        return {"internal": port, "external": external, "lifetime": lifetime}
    raise OddReply()


def udp_exchange(gateway: str, port: int, message: bytes, check: Callable[[bytes], Any], tries: int = 3) -> Any:
    """Send `message` to the router and return `check` of its answer, asking again a few times.
    Packets from anyone else, and ones `check` finds aren't the answer, are passed over."""
    odd = None
    with udp_socket() as sock:
        wait = 0.25
        for _ in range(tries):
            try:
                sock.sendto(message, (gateway, port))
                until = time.monotonic() + wait
                while True:
                    left = until - time.monotonic()
                    if left <= 0:
                        break
                    sock.settimeout(left)
                    data, (sender, _) = sock.recvfrom(1100)
                    if sender != gateway:
                        continue
                    try:
                        return check(data)
                    except OddReply as exc:
                        odd = exc
            except socket.timeout:
                pass
            except OSError:
                raise GatewayError("the router didn't answer")
            wait *= 2
    raise odd or GatewayError("the router didn't answer")


class NatPmp:
    label = "NAT-PMP"

    def __init__(self, gateway: str):
        self.gateway = gateway

    def external_ip(self) -> Optional[str]:
        return udp_exchange(self.gateway, NATPMP_PORT, natpmp_request(0), lambda d: natpmp_reply(d, 0))["ip"]

    def add(self, internal: int, external: int, lease: int) -> Tuple[int, int]:
        got = udp_exchange(self.gateway, NATPMP_PORT, natpmp_request(2, internal, external, lease),
                           lambda d: natpmp_reply(d, 2, internal))
        return got["external"], got["lifetime"]

    def remove(self, internal: int, external: int) -> None:
        udp_exchange(self.gateway, NATPMP_PORT, natpmp_request(2, internal, 0, 0),
                     lambda d: natpmp_reply(d, 2, internal))

    def memo(self) -> dict:
        return {}

    def forget(self, internal: int, external: int, memo: dict) -> None:
        self.remove(internal, external)  # the router only ever removes this machine's own forwards


PCP_ERRORS = {1: "unsupported version", 2: "the router doesn't allow it", 3: "the router sent something odd",
              8: "the router has no internet connection", 9: "the router doesn't allow it",
              11: "the router is out of room for forwards", 12: "the router can't keep that port"}


def _v6(ip: str) -> bytes:
    """An IPv4 address the way PCP carries it, as ::ffff:a.b.c.d."""
    return b"\0" * 10 + b"\xff\xff" + socket.inet_aton(ip)


def pcp_map_request(client_ip: str, internal: int, external: int, lifetime: int, nonce: bytes) -> bytes:
    header = struct.pack("!BBHI16s", 2, 1, 0, lifetime, _v6(client_ip))
    return header + struct.pack("!12sB3xHH16s", nonce, 6, internal, external, _v6("0.0.0.0"))


def pcp_map_reply(data: bytes, nonce: bytes, port: Optional[int] = None) -> dict:
    """The router's answer to our MAP request. With `port`, only an answer about that internal
    port counts."""
    if len(data) < 60 or data[0] != 2 or data[1] != 0x81:
        raise OddReply()
    result, lifetime = data[3], struct.unpack("!I", data[4:8])[0]
    got_nonce, protocol, internal, external, ip = struct.unpack("!12sB3xHH16s", data[24:60])
    if got_nonce != nonce or protocol != 6 or (port is not None and internal != port):
        raise OddReply()
    if result:
        raise GatewayError(PCP_ERRORS.get(result, f"the router said no ({result})"))
    return {"internal": internal, "external": external, "lifetime": lifetime, "ip": socket.inet_ntoa(ip[12:])}


class Pcp:
    label = "PCP"

    def __init__(self, gateway: str, local_ip: str):
        self.gateway, self.local_ip = gateway, local_ip
        self.nonce = secrets.token_bytes(12)  # the same one renews and removes the mapping
        self.wan: Optional[str] = None

    def _map(self, internal: int, external: int, lifetime: int, nonce: Optional[bytes] = None) -> dict:
        nonce = nonce or self.nonce
        return udp_exchange(self.gateway, NATPMP_PORT, pcp_map_request(self.local_ip, internal, external, lifetime, nonce),
                            lambda d: pcp_map_reply(d, nonce, internal))

    def external_ip(self) -> Optional[str]:
        return self.wan  # PCP tells it with a mapping, not on its own

    def add(self, internal: int, external: int, lease: int) -> Tuple[int, int]:
        got = self._map(internal, external, lease)
        self.wan = got["ip"]
        return got["external"], got["lifetime"]

    def remove(self, internal: int, external: int) -> None:
        self._map(internal, external, 0)

    def memo(self) -> dict:
        return {"nonce": self.nonce.hex()}

    def forget(self, internal: int, external: int, memo: dict) -> None:
        try:
            nonce = bytes.fromhex(memo.get("nonce", ""))
        except ValueError:
            nonce = b""
        # The router only takes a forward off for the nonce that made it.
        self._map(internal, external, 0, nonce if len(nonce) == 12 else None)


# How a router tells one forward from another: by the port outside (UPnP), or by the port inside
# (NAT-PMP and PCP, which keep one per port on each machine).
FORWARD_KEY = {"UPnP": "external", "NAT-PMP": "internal", "PCP": "internal"}


def find_gateways() -> Iterator[Any]:
    """The ways to ask this network's router for a port, best first. Each is only looked for when
    the one before didn't work out."""
    found = UPnP.discover()
    if found:
        yield found
    gateway = default_gateway()
    if gateway:
        yield NatPmp(gateway)
        local = local_ip_for(gateway)
        if local:
            yield Pcp(gateway, local)


# ---- the background updater ---------------------------------------------------------------

def _valid_record(record: Any) -> bool:
    """A forward saved in the store, still in the shape we write (the store could be hand-edited)."""
    return (isinstance(record, dict) and isinstance(record.get("method"), str) and isinstance(record.get("memo"), dict)
            and all(isinstance(record.get(k), int) and not isinstance(record.get(k), bool) for k in ("internal", "external")))


def _same_forward(a: dict, b: dict) -> bool:
    key = FORWARD_KEY.get(a["method"], "external")
    return a["method"] == b["method"] and a[key] == b[key]


class PublicAccess:
    """Keeps the dynamic DNS name pointed at this connection and the router port open, in a
    thread of its own. It looks again every few minutes, when a setting changes, and when the
    admin page asks. With both turned off it does nothing at all, not even look up the address,
    unless a forward from before is still waiting to come off the router.

    Every forward that may be on the router is kept in the store (`FORWARDS_KEY`), so one left
    behind by a crash, a failed renewal or a failed removal is taken off later, even after a
    restart with the setting turned off."""

    def __init__(self, get: Callable[[str], Any], listen_port: int, store: Any = None):
        self.get = get
        self.listen_port = listen_port
        self.store = store  # anything with load_settings() and save_setting(key, value)
        self.lookup_ip: Callable[[], Optional[str]] = public_ip  # swapped out by tests
        self.gateways: Callable[[], Iterator[Any]] = find_gateways
        self.clock: Callable[[], float] = time.time
        self.lock = threading.Lock()  # one check at a time
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.look_now = False
        self.conf: Optional[dict] = None
        self.sent: Optional[Tuple[str, float]] = None  # the address the provider last took, and when
        self.stuck: Optional[dict] = None  # settings the provider turned down; not retried until they change
        self.backoff, self.retry_at = 0.0, 0.0  # after a "try later" from the provider
        # (gateway, internal port, external port it got, external port asked for, lease)
        self.mapped: Optional[Tuple[Any, int, int, int, int]] = None
        self.renew_at = 0.0
        # Forwards to take off: (record, gateway or None). A record is {method, internal, external,
        # memo}; with no gateway (one from an earlier run), the router is looked for again.
        saved = store.load_settings().get(FORWARDS_KEY) if store is not None else None
        self.stale: list = [(r, None) for r in saved or [] if _valid_record(r)]
        self.state: dict = {}

    def settings(self) -> dict:
        target = self.get("forward_to_port") or self.listen_port
        return {"provider": self.get("ddns_provider"), "hostname": self.get("ddns_hostname"),
                "username": self.get("ddns_username"), "token": self.get("ddns_token"), "url": self.get("ddns_url"),
                "forward": self.get("port_forward") == "on", "target_port": target,
                "router_port": self.get("router_port") or target}

    def check(self, force: bool = False) -> None:
        """Look now. `force` sends the address to the provider even if it hasn't changed."""
        with self.lock:
            self._check(force)

    def look(self, force: bool = False) -> None:
        """`check`, for callers that carry on whatever happens: the thread, and saving a setting
        (which would otherwise answer 500 after the setting was already saved)."""
        try:
            self.check(force)
            self.state.pop("error", None)
        except Exception as exc:  # never the exception's text: it could hold a URL with a token in it
            log.warning("Public access check failed (%s)", type(exc).__name__)
            self.state["error"] = "Something went wrong checking public access. Trying again in a few minutes."

    def _check(self, force: bool) -> None:
        conf = self.settings()
        changed, self.conf = conf != self.conf, conf
        now, ddns = self.clock(), conf["provider"] in PROVIDERS
        if conf["forward"]:
            if changed or force or not self.mapped or now >= self.renew_at:
                self._open(conf, now)
        else:
            self._close()
        self._sweep()
        if not ddns and not conf["forward"]:
            self.state, self.sent, self.stuck = {}, None, None
            self.backoff = self.retry_at = 0.0
            return
        ip = self.lookup_ip()
        self.state.update(public_ip=ip, checked_at=now)
        if ddns and (changed or force or "router_ip" not in self.state) and not conf["forward"]:
            self.state["router_ip"] = self._router_ip()
        self.state["warning"] = nat_problem(self.state.get("router_ip"), ip)
        # In a container that's the container's own address, which would only mislead.
        self.state["lan_ip"] = None if in_container() else local_ip_for(default_gateway() or "192.0.2.1")
        if not ddns:
            self.state.pop("ddns", None)
            return
        if ip is None:
            self.state["ddns"] = {"ok": False, "at": now, "message": "Couldn't find out this connection's public address. Trying again later."}
            return
        if self.stuck == conf and not force:
            return
        if now < self.retry_at and not (force or changed):
            return  # the provider asked us to wait
        if force or changed or not self.sent or self.sent[0] != ip or now - self.sent[1] >= REFRESH:
            result = update(conf["provider"], ip, conf["hostname"], conf["username"], conf["token"], conf["url"])
            self.state["ddns"] = {"ok": result.ok, "at": now, "message": result.message, "ip": ip}
            self.sent = (ip, now) if result.ok else None
            self.stuck = None if result.ok or result.retry else conf
            if result.ok or not result.retry:
                self.backoff = self.retry_at = 0.0
            else:  # providers flag names that keep retrying fast, so wait longer each time
                self.backoff = min(REFRESH, self.backoff * 2 if self.backoff else RETRY)
                self.retry_at = now + self.backoff
            if not result.ok:
                log.warning("Dynamic DNS update failed: %s", result.message)

    def _router_ip(self) -> Optional[str]:
        for gateway in self.gateways():
            try:
                found = gateway.external_ip()
            except GatewayError:
                continue
            if found:
                return found
        return None

    def _open(self, conf: dict, now: float) -> None:
        internal, external = conf["target_port"], conf["router_port"]
        if self.mapped and (self.mapped[1], self.mapped[3]) != (internal, external):
            self._close()
        def candidates() -> Iterator[Any]:
            if self.mapped:
                yield self.mapped[0]  # renewing: the router that has it already
            yield from self.gateways()
        reasons = []
        for gateway in candidates():
            try:
                port, lease = gateway.add(internal, external, LEASE)
            except GatewayError as exc:
                reasons.append(f"{gateway.label}: {exc}")
                continue
            try:
                wan = gateway.external_ip()
            except GatewayError:
                wan = None
            was_permanent = bool(self.mapped) and self.mapped[4] == 0
            if self.mapped and self.mapped[0] is not gateway:
                self._retire()  # renewed somewhere else: the old one comes off, unless it's this same forward
            self.mapped = (gateway, internal, port, external, lease)
            mine = self._record(gateway, internal, port)
            self.stale = [(r, g) for r, g in self.stale if not _same_forward(r, mine)]  # replaced by this one
            self._save()
            self.renew_at = now + (max(60, lease // 2) if lease else LEASE)
            message = f"Port {port} on the router leads here ({gateway.label})."
            if not lease:
                # Never quietly: a permanent forward outlives a crash (until the next start takes it off).
                message += (" The router only takes permanent forwards, so if the server stops without "
                            "taking it off, it stays open until KaraQueue starts again.")
                if not was_permanent:
                    log.warning("The router only takes permanent port forwards. Port %s stays open if the server "
                                "stops without taking it off, until KaraQueue starts again.", port)
            self.state["port"] = {"ok": True, "method": gateway.label, "external_port": port, "internal_port": internal,
                                  "lease": lease, "permanent": not lease, "message": message}
            self.state["router_ip"] = wan
            return
        if self.mapped:  # a renewal that failed everywhere: the old forward may still be there
            self._retire()
        self.renew_at = now + SWEEP
        why = "; ".join(reasons) or "no router answered (UPnP, NAT-PMP or PCP)"
        self.state["port"] = {"ok": False, "external_port": external, "internal_port": internal,
                              "message": f"The router didn't open the port: {why}."}
        log.warning("Couldn't open port %s on the router: %s", external, why)

    def _record(self, gateway: Any, internal: int, external: int) -> dict:
        return {"method": gateway.label, "internal": internal, "external": external, "memo": gateway.memo()}

    def _save(self) -> None:
        if self.store is None:
            return
        records = ([self._record(*self.mapped[:3])] if self.mapped else []) + [r for r, _ in self.stale]
        self.store.save_setting(FORWARDS_KEY, records or None)

    def _retire(self) -> None:
        """Stop using the current forward, and take it off the router now or, failing that, later."""
        gateway, internal, external = self.mapped[:3]
        self.stale.append((self._record(gateway, internal, external), gateway))
        self.mapped = None
        self._save()

    def _close(self) -> None:
        if self.mapped:
            gateway, internal, external = self.mapped[:3]
            try:
                gateway.remove(internal, external)
                self.mapped = None
                self._save()
            except GatewayError as exc:
                log.warning("Couldn't remove the router's port forward: %s. Trying again later.", exc)
                self._retire()
        self.state.pop("port", None)

    def _sweep(self) -> None:
        """Take off forwards that should be gone: ones a removal failed for, a renewal lost, or an
        earlier run left behind (it crashed, or the setting was turned off while it was down).
        Each is checked to still be ours first, where the router can tell."""
        if not self.stale:
            return
        found: Optional[list] = None  # routers, looked for at most once a sweep
        left = []
        for record, gateway in self.stale:
            if self.mapped and _same_forward(record, self._record(*self.mapped[:3])):
                continue  # it's the one in use now
            if gateway is None:
                if found is None:
                    found = list(self.gateways())
                gateway = next((g for g in found if g.label == record["method"]), None)
            try:
                if gateway is None:
                    raise GatewayError(f"no {record['method']} router answered")
                gateway.forget(record["internal"], record["external"], record["memo"])
            except GatewayError as exc:
                log.warning("Couldn't take an old forward of port %s off the router yet: %s", record["external"], exc)
                left.append((record, gateway))
        if len(left) != len(self.stale):
            self.stale = left
            self._save()

    def wait_time(self) -> Optional[float]:
        conf = self.conf or {}
        waits = []
        if conf.get("provider") in PROVIDERS:
            waits.append(60 * (self.get("ddns_minutes") or 5))
        if conf.get("forward"):
            waits.append(max(5.0, self.renew_at - self.clock()))
        if self.stale:
            waits.append(SWEEP)
        return min(waits) if waits else None

    def status(self) -> dict:
        # No lock: a check can take seconds of network, and the admin page shouldn't wait on it.
        state, conf = dict(self.state), self.conf or self.settings()
        port = (state.get("port") or {}).get("external_port") or conf["router_port"]
        host = conf["hostname"]
        on = conf["provider"] in PROVIDERS or conf["forward"]
        # 443 on the router only makes sense forwarded to an https server (Caddy, say).
        url = None
        if host and on:
            url = f"https://{host}" if port == 443 else f"http://{host}" + ("" if port == 80 else f":{port}")
        plain_auth = (conf["provider"] == "custom" and conf["url"].lower().startswith("http://")
                      and bool(conf["username"] or conf["token"]))
        return {**state, "on": on, "provider": conf["provider"], "listen_port": self.listen_port,
                "target_port": conf["target_port"], "url": url, "plain_http": on and port != 443,
                "plain_auth": plain_auth, "tunnels": TUNNEL_DOCS}

    # -- the thread ---------------------------------------------------------------

    def poke(self, look: bool = False) -> None:
        """Wake the thread to work out when to look next from the settings, and look now if `look`."""
        self.look_now = self.look_now or look
        self.wake.set()

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="public-access", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        due = True
        while not self.stopping.is_set():
            if due or self.look_now:
                self.look_now = False
                self.look()
            due = not self.wake.wait(self.wait_time())
            self.wake.clear()

    def stop(self) -> None:
        """Stop looking, and take the port forward off the router."""
        self.stopping.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        with self.lock:
            self._close()


def listen_port(env=os.environ) -> int:
    try:
        return int(env.get("KARAQUEUE_PORT") or 8000)
    except ValueError:
        return 8000
