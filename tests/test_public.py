import base64
import email.message
import io
import pathlib
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

import pytest
from fastapi.testclient import TestClient

from karaqueue import public
from karaqueue import settings as settings_module
from karaqueue.app import create_app
from karaqueue.public import GatewayError, PublicAccess, nat_problem
from karaqueue.store import Error, Store

TOKEN = "s3cret-token-0123456789"


class Reply:
    def __init__(self, status, body):
        self.status, self.body = status, body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.body[:n] if n >= 0 else self.body


@pytest.fixture
def web(monkeypatch):
    """Every HTTP request the code makes, answered from `replies` (status, body) in order."""
    sent, replies = [], []

    def urlopen(req, timeout=None):
        sent.append(req)
        status, body = replies.pop(0) if replies else (200, "")
        if isinstance(status, Exception):
            raise status
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "no", {}, io.BytesIO(body.encode()))
        return Reply(status, body)
    monkeypatch.setattr(public, "open_url", urlopen)
    return sent, replies


class FakeSocket:
    """A UDP socket that sends nowhere and receives `packets`, (data, (sender, port)), in order."""

    def __init__(self, packets=(), fail_send=False):
        self.packets, self.sent, self.fail_send = list(packets), [], fail_send

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        pass

    def sendto(self, data, address):
        if self.fail_send:
            raise OSError(101, "Network is unreachable")
        self.sent.append((data, address))

    def recvfrom(self, size):
        if self.packets:
            return self.packets.pop(0)
        raise socket.timeout()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Nothing in these tests may reach the LAN: a test that forgets to fake a socket fails."""
    def refuse():
        raise AssertionError("a test opened a real UDP socket")
    monkeypatch.setattr(public, "udp_socket", refuse)
    monkeypatch.setattr(public, "find_gateways", lambda: iter([]))


def query(req):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))


def basic(req):
    scheme, _, value = req.get_header("Authorization", "").partition(" ")
    assert scheme == "Basic"
    return base64.b64decode(value).decode()


# ---- what each provider is sent, and how its answer is read ----

def test_duckdns(web):
    sent, replies = web
    replies.append((200, "OK"))
    assert public.update("duckdns", "203.0.113.7", "MyParty.duckdns.org", token=TOKEN).ok
    req = sent[-1]
    assert req.full_url.startswith("https://www.duckdns.org/update?")
    assert query(req) == {"domains": "myparty", "token": TOKEN, "ip": "203.0.113.7"}
    assert req.get_header("User-agent").startswith("KaraQueue/")
    replies.append((200, "KO"))
    result = public.update("duckdns", "203.0.113.7", "myparty", token=TOKEN)
    assert not result.ok and not result.retry and TOKEN not in result.message


@pytest.mark.parametrize("provider,base", [("noip", "https://dynupdate.no-ip.com/nic/update"),
                                           ("dynu", "https://api.dynu.com/nic/update")])
def test_noip_and_dynu_speak_dyndns2(web, provider, base):
    sent, replies = web
    replies.append((200, "good 203.0.113.7"))
    result = public.update(provider, "203.0.113.7", "party.ddns.net", "me@example.com", TOKEN)
    assert result == (True, "Updated", True)
    req = sent[-1]
    assert req.full_url.split("?")[0] == base
    assert query(req) == {"hostname": "party.ddns.net", "myip": "203.0.113.7"}
    assert basic(req) == "me@example.com:" + TOKEN
    assert TOKEN not in req.full_url
    assert "KaraQueue" in req.get_header("User-agent")  # No-IP blocks updaters without one

    replies.append((200, "nochg 203.0.113.7"))
    assert public.update(provider, "203.0.113.7", "party.ddns.net", "me", TOKEN).message == "Up to date"
    for word, retry in (("badauth", False), ("nohost", False), ("abuse", False), ("911", True)):
        replies.append((200, word))
        result = public.update(provider, "203.0.113.7", "party.ddns.net", "me", TOKEN)
        assert not result.ok and result.retry is retry, word
    replies.append((401, ""))
    assert public.update(provider, "203.0.113.7", "party.ddns.net", "me", TOKEN).retry is False


def test_freedns(web):
    sent, replies = web
    replies.append((200, "Updated 1 host(s) party.mooo.com to 203.0.113.7 in 0.2 seconds"))
    assert public.update("freedns", "203.0.113.7", token=TOKEN).ok
    assert sent[-1].full_url == f"https://freedns.afraid.org/dynamic/update.php?{TOKEN}&address=203.0.113.7"
    replies.append((200, "ERROR: Address 203.0.113.7 has not changed."))
    assert public.update("freedns", "203.0.113.7", token=TOKEN).message == "Up to date"
    replies.append((200, "ERROR: Unable to locate this record"))
    assert public.update("freedns", "203.0.113.7", token=TOKEN).retry is False


def test_custom_update_url(web):
    sent, replies = web
    replies.append((200, "anything"))
    url = "https://dns.example.net/update?host={hostname}&ip={ip}&key=abc"
    assert public.update("custom", "203.0.113.7", "party.example.net", url=url).ok
    assert sent[-1].full_url == "https://dns.example.net/update?host=party.example.net&ip=203.0.113.7&key=abc"
    assert sent[-1].get_header("Authorization") is None
    replies.append((200, "good"))
    public.update("custom", "203.0.113.7", "h", "me", TOKEN, url)
    assert basic(sent[-1]) == "me:" + TOKEN  # with a username, the token is its password
    replies.append((403, "denied"))
    assert public.update("custom", "203.0.113.7", url=url).retry is False


def test_missing_settings_and_unreachable_providers(web):
    sent, replies = web
    result = public.update("noip", "203.0.113.7", "party.ddns.net", token=TOKEN)
    assert not result.ok and "Username" in result.message and not sent
    replies.append((urllib.error.URLError("https://www.duckdns.org/update?token=" + TOKEN), ""))
    result = public.update("duckdns", "203.0.113.7", "party", token=TOKEN)
    assert not result.ok and result.retry and TOKEN not in result.message


def test_public_ip_tries_the_next_service(web):
    sent, replies = web
    replies.extend([(OSError("down"), ""), (200, "203.0.113.7\n")])
    assert public.public_ip() == "203.0.113.7"
    assert [r.full_url for r in sent] == list(public.IP_SERVICES)
    replies.extend([(200, "<html>"), (200, "not an address")])
    assert public.public_ip() is None


# ---- carrier-grade NAT ----

def test_nat_problem_spots_carrier_grade_and_double_nat():
    assert "carrier-grade" in nat_problem("100.64.3.4", "203.0.113.7")
    assert "carrier-grade" in nat_problem("100.127.255.254", None)
    assert "another router" in nat_problem("192.168.1.20", "203.0.113.7")
    assert "the internet sees 203.0.113.7" in nat_problem("198.51.100.4", "203.0.113.7")
    assert nat_problem("203.0.113.7", "203.0.113.7") is None
    assert nat_problem("100.128.0.1", "100.128.0.1") is None  # just past the shared range
    assert nat_problem(None, "203.0.113.7") is None  # no router told us: no telling
    assert nat_problem("garbage", "203.0.113.7") is None


# ---- talking to the router ----

def test_ssdp_only_follows_a_location_on_the_device_that_answered():
    reply = b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\nLOCATION: http://192.168.1.1:5000/rootDesc.xml\r\n\r\n"
    assert public.ssdp_location(reply, "192.168.1.1") == "http://192.168.1.1:5000/rootDesc.xml"
    assert public.ssdp_location(reply, "192.168.1.66") is None
    assert b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1" in public.ssdp_search(public.IGD_TYPES[1])


def test_upnp_description_and_soap():
    description = """<?xml version="1.0"?>
    <root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device><serviceList>
      <service><serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType><controlURL>/a</controlURL></service>
      <service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType><controlURL>/ctl/IPConn</controlURL></service>
    </serviceList></device></deviceList></device></deviceList></device></root>"""
    assert public.wan_service(description, "http://192.168.1.1:5000/rootDesc.xml") == \
        ("http://192.168.1.1:5000/ctl/IPConn", "urn:schemas-upnp-org:service:WANIPConnection:1")
    body = public.soap_envelope("urn:x:WANIPConnection:1", "AddPortMapping", (("NewExternalPort", 8000), ("NewProtocol", "TCP")))
    assert b'<u:AddPortMapping xmlns:u="urn:x:WANIPConnection:1"><NewExternalPort>8000</NewExternalPort>' in body
    assert public.soap_fields('<s:Envelope xmlns:s="x"><s:Body><u:R xmlns:u="y"><NewExternalIPAddress>203.0.113.7'
                              '</NewExternalIPAddress></u:R></s:Body></s:Envelope>')["NewExternalIPAddress"] == "203.0.113.7"


def test_upnp_falls_back_to_a_permanent_forward(web):
    sent, replies = web
    fault = ("<s:Envelope xmlns:s='x'><s:Body><s:Fault><detail><UPnPError><errorCode>725</errorCode>"
             "<errorDescription>OnlyPermanentLeasesSupported</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>")
    replies.extend([(500, fault), (200, "<r/>")])
    upnp = public.UPnP("http://192.168.1.1:5000/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1", "192.168.1.20")
    assert upnp.add(8000, 8080, 3600) == (8080, 0)
    assert sent[-1].get_header("Soapaction") == '"urn:schemas-upnp-org:service:WANIPConnection:1#AddPortMapping"'
    assert b"<NewInternalClient>192.168.1.20</NewInternalClient>" in sent[-1].data
    assert b"<NewLeaseDuration>0</NewLeaseDuration>" in sent[-1].data
    replies.append((500, fault.replace("725", "718")))
    with pytest.raises(GatewayError, match="another device"):
        upnp.add(8000, 8080, 3600)


def test_natpmp_messages():
    assert public.natpmp_request(0) == b"\x00\x00"
    assert public.natpmp_request(2, 8000, 8080, 3600) == struct.pack("!BBHHHI", 0, 2, 0, 8000, 8080, 3600)
    reply = struct.pack("!BBHI", 0, 128, 0, 99) + socket.inet_aton("203.0.113.7")
    assert public.natpmp_reply(reply, 0) == {"ip": "203.0.113.7"}
    mapped = struct.pack("!BBHIHHI", 0, 130, 0, 99, 8000, 8080, 7200)
    assert public.natpmp_reply(mapped, 2) == {"internal": 8000, "external": 8080, "lifetime": 7200}
    with pytest.raises(GatewayError, match="doesn't allow"):
        public.natpmp_reply(struct.pack("!BBHIHHI", 0, 130, 2, 99, 8000, 0, 0), 2)


def test_pcp_messages():
    nonce = bytes(range(12))
    req = public.pcp_map_request("192.168.1.20", 8000, 8080, 3600, nonce)
    assert len(req) == 60 and req[:2] == b"\x02\x01" and req[8:24] == b"\0" * 10 + b"\xff\xff" + bytes([192, 168, 1, 20])
    assert req[24:36] == nonce and req[36] == 6 and struct.unpack("!HH", req[40:44]) == (8000, 8080)
    reply = (struct.pack("!BBBBII12x", 2, 0x81, 0, 0, 3600, 5) +
             struct.pack("!12sB3xHH16s", nonce, 6, 8000, 8080, public._v6("203.0.113.7")))
    assert public.pcp_map_reply(reply, nonce) == {"internal": 8000, "external": 8080, "lifetime": 3600, "ip": "203.0.113.7"}
    with pytest.raises(GatewayError):
        public.pcp_map_reply(reply, bytes(12))  # not our request


# ---- the updater ----

class FakeRouter:
    label = "Fake"

    def __init__(self, wan="203.0.113.7", refuse=None, lease=None):
        self.wan, self.refuse, self.lease, self.forwards, self.calls = wan, refuse, lease, {}, []
        self.down = False  # True: it answers nothing

    def _answer(self):
        if self.down:
            raise GatewayError("the router didn't answer")

    def external_ip(self):
        self._answer()
        return self.wan

    def add(self, internal, external, lease):
        self.calls.append(("add", internal, external))
        self._answer()
        if self.refuse:
            raise GatewayError(self.refuse)
        self.forwards[external] = internal
        return external, lease if self.lease is None else self.lease

    def remove(self, internal, external):
        self.calls.append(("remove", internal, external))
        self._answer()
        self.forwards.pop(external, None)

    def memo(self):
        return {"client": "192.168.1.20"}

    def forget(self, internal, external, memo):
        self.calls.append(("forget", internal, external))
        self._answer()
        assert memo == {"client": "192.168.1.20"}
        self.forwards.pop(external, None)


def updater(values, router=None, ip="203.0.113.7", store=None):
    settings = {"ddns_provider": "off", "ddns_hostname": "", "ddns_username": "", "ddns_token": "", "ddns_url": "",
                "ddns_minutes": 5, "port_forward": "off", "router_port": 0, "forward_to_port": 0, **values}
    pa = PublicAccess(settings.get, 8000, store)
    pa.lookup_ip = lambda: pa.ip
    pa.ip = ip
    pa.gateways = lambda: iter([router] if router else [])
    pa.now = 1000.0
    pa.clock = lambda: pa.now
    return pa, settings


def test_updater_does_nothing_when_off(web):
    pa, _ = updater({})
    pa.lookup_ip = lambda: pytest.fail("looked up the address with everything off")
    pa.check()
    assert pa.status()["on"] is False and pa.wait_time() is None and not web[0]


def test_updater_tells_the_provider_only_when_the_address_changes(web, caplog):
    sent, replies = web
    router = FakeRouter()
    pa, settings = updater({"ddns_provider": "duckdns", "ddns_hostname": "party.duckdns.org", "ddns_token": TOKEN}, router)
    replies.append((200, "OK"))
    pa.check()
    assert len(sent) == 1 and pa.status()["ddns"]["ok"] and pa.status()["url"] == "http://party.duckdns.org:8000"
    assert pa.status()["router_ip"] == "203.0.113.7" and pa.status()["warning"] is None
    pa.now += 300
    pa.check()
    assert len(sent) == 1  # same address: nothing sent
    pa.ip, pa.now = "203.0.113.8", pa.now + 300
    replies.append((200, "OK"))
    pa.check()
    assert query(sent[-1])["ip"] == "203.0.113.8" and len(sent) == 2
    pa.now += public.REFRESH
    replies.append((200, "OK"))
    pa.check()
    assert len(sent) == 3  # once a day anyway
    assert pa.wait_time() == 300

    # Turned down: not asked again until the settings change (or the admin asks).
    replies.append((200, "KO"))
    pa.check(force=True)
    pa.ip = "203.0.113.9"
    pa.check()
    assert len(sent) == 4 and not pa.status()["ddns"]["ok"]
    settings["ddns_token"] = TOKEN + "x"
    replies.append((200, "OK"))
    pa.check()
    assert len(sent) == 5 and pa.status()["ddns"]["ok"]
    assert TOKEN not in caplog.text and TOKEN not in str(pa.status())


def test_updater_warns_behind_carrier_grade_nat(web):
    pa, _ = updater({"ddns_provider": "duckdns", "ddns_hostname": "party", "ddns_token": TOKEN},
                    FakeRouter(wan="100.72.1.2"))
    web[1].append((200, "OK"))
    pa.check()
    assert "carrier-grade" in pa.status()["warning"]


def test_port_forward_opens_renews_moves_and_comes_off(web):
    router = FakeRouter()
    pa, settings = updater({"port_forward": "on"}, router)
    pa.check()
    assert router.forwards == {8000: 8000}
    port = pa.status()["port"]
    assert port["ok"] and port["external_port"] == 8000 and port["method"] == "Fake"
    pa.now += public.LEASE / 2
    pa.check()
    assert router.calls.count(("add", 8000, 8000)) == 2  # renewed at half time
    settings["router_port"] = 8443
    pa.check()
    assert router.forwards == {8443: 8000} and ("remove", 8000, 8000) in router.calls
    pa.stop()
    assert router.forwards == {} and "port" not in pa.status()


def test_port_forward_refused_explains_why(web):
    pa, settings = updater({"port_forward": "on"}, FakeRouter(refuse="the router doesn't allow it"))
    pa.check()
    port = pa.status()["port"]
    assert not port["ok"] and "doesn't allow it" in port["message"] and port["external_port"] == 8000
    pa, _ = updater({"port_forward": "on"})
    pa.check()
    assert "no router answered" in pa.status()["port"]["message"]


# ---- plain http, and forwarding to an https server (M1) ----

def test_the_forward_can_lead_to_an_https_server_instead():
    router = FakeRouter()
    pa, settings = updater({"port_forward": "on", "ddns_hostname": "party.duckdns.org"}, router)
    pa.check()
    status = pa.status()
    assert router.forwards == {8000: 8000}
    assert status["url"] == "http://party.duckdns.org:8000" and status["plain_http"] is True

    settings["forward_to_port"] = 443  # Caddy on this machine; the router port follows it
    pa.check()
    status = pa.status()
    assert router.forwards == {443: 443} and status["port"]["internal_port"] == 443
    assert status["url"] == "https://party.duckdns.org" and status["plain_http"] is False
    settings["router_port"] = 8443
    pa.check()
    assert router.forwards == {8443: 443} and pa.status()["plain_http"] is True


def test_forward_to_port_is_set_in_the_environment_only(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "public-test")
    monkeypatch.setenv("KARAQUEUE_FORWARD_TO_PORT", "443")
    client = TestClient(create_app(Store()))
    client.headers["Authorization"] = "Bearer " + client.post("/api/admin/login", json={"password": "public-test"}).json()["token"]
    assert client.put("/api/admin/settings/forward_to_port", json={"value": 22}).status_code == 409
    assert client.app.state.public.settings()["target_port"] == 443


def test_the_plain_http_warning_is_on_the_admin_page_and_in_the_docs():
    # The docs live in the repo, not in the package: find them from this test file, so it also works
    # when karaqueue is installed into site-packages rather than editable.
    root = pathlib.Path(__file__).resolve().parents[1]
    page = (pathlib.Path(public.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
    docs = (root / "docs" / "public-access.md").read_text(encoding="utf-8")
    assert "passwords and sign-ins travel unencrypted" in page and "p.plain_http" in page
    assert "passwords and sign-ins travel unencrypted" in docs
    assert "Don't sign in as admin from outside your network until you have https" in docs


# ---- forwards left on the router (M2) ----

def test_a_failed_renewal_keeps_the_forward_to_take_off_later():
    router = FakeRouter()
    pa, settings = updater({"port_forward": "on"}, router)
    pa.check()
    router.down = True
    pa.now += public.LEASE / 2
    pa.check()  # the renewal fails: the forward is still on the router, and not forgotten
    assert not pa.status()["port"]["ok"] and router.forwards == {8000: 8000}
    assert [r["external"] for r, _ in pa.stale] == [8000] and pa.wait_time() <= public.SWEEP

    settings["port_forward"] = "off"
    pa.check()  # still unreachable: kept, and the thread still wakes to try again
    assert pa.stale and pa.wait_time() == public.SWEEP
    router.down = False
    pa.check()
    assert router.forwards == {} and pa.stale == [] and pa.wait_time() is None


def test_a_renewal_that_works_again_keeps_the_forward():
    router = FakeRouter()
    pa, _ = updater({"port_forward": "on"}, router)
    pa.check()
    router.down = True
    pa.now += public.LEASE / 2
    pa.check()
    router.down = False
    pa.now += public.SWEEP
    pa.check()  # back: the forward is open again, and the old record of it isn't taken off
    assert router.forwards == {8000: 8000} and pa.stale == [] and pa.status()["port"]["ok"]
    assert router.calls.count(("forget", 8000, 8000)) == 1  # only the try while it didn't answer


def test_a_failed_removal_is_tried_again():
    router = FakeRouter()
    pa, settings = updater({"port_forward": "on"}, router)
    pa.check()
    router.down = True
    settings["port_forward"] = "off"
    pa.check()
    assert router.forwards == {8000: 8000} and pa.stale
    router.down = False
    pa.now += public.SWEEP
    pa.check()
    assert router.forwards == {} and not pa.stale


def test_forwards_left_by_a_crash_come_off_at_the_next_start():
    store, router = Store(), FakeRouter()
    pa, _ = updater({"port_forward": "on"}, router, store=store)
    pa.check()
    assert store.load_settings()[public.FORWARDS_KEY] == [
        {"method": "Fake", "internal": 8000, "external": 8000, "memo": {"client": "192.168.1.20"}}]
    # The server is killed here: no stop(). It starts again with the setting turned off.
    pa, _ = updater({}, router, store=store)
    pa.check()
    assert ("forget", 8000, 8000) in router.calls and router.forwards == {}
    assert public.FORWARDS_KEY not in store.load_settings()

    # Killed with the forward on, and started again with it on: kept, not taken off and put back.
    pa, _ = updater({"port_forward": "on"}, router, store=store)
    pa.check()
    router.calls.clear()
    pa, _ = updater({"port_forward": "on"}, router, store=store)
    pa.check()
    assert router.calls == [("add", 8000, 8000)] and router.forwards == {8000: 8000}
    assert len(store.load_settings()[public.FORWARDS_KEY]) == 1
    pa.stop()
    assert router.forwards == {} and public.FORWARDS_KEY not in store.load_settings()


def test_a_forward_from_a_restart_on_another_port_comes_off():
    store, router = Store(), FakeRouter()
    pa, _ = updater({"port_forward": "on"}, router, store=store)
    pa.check()
    pa, _ = updater({"port_forward": "on", "router_port": 9000}, router, store=store)
    pa.check()
    assert router.forwards == {9000: 8000}
    assert [r["external"] for r in store.load_settings()[public.FORWARDS_KEY]] == [9000]


def test_nothing_saved_and_everything_off_looks_for_no_router():
    store = Store()
    store.save_setting(public.FORWARDS_KEY, [{"method": "Fake", "internal": "x"}, "junk"])  # hand-edited
    pa, _ = updater({}, store=store)
    pa.gateways = lambda: pytest.fail("looked for a router with nothing to do")
    pa.check()
    assert pa.wait_time() is None


def test_a_permanent_forward_says_so_and_comes_off_at_stop(web, caplog):
    sent, replies = web
    fault = ("<s:Envelope xmlns:s='x'><s:Body><s:Fault><detail><UPnPError><errorCode>725</errorCode>"
             "</UPnPError></detail></s:Fault></s:Body></s:Envelope>")
    ip = "<r><NewExternalIPAddress>203.0.113.7</NewExternalIPAddress></r>"
    upnp = public.UPnP("http://192.168.1.1:5000/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1", "192.168.1.20")
    pa, _ = updater({"port_forward": "on"}, upnp)
    replies.extend([(500, fault), (200, "<r/>"), (200, ip)])
    pa.check()
    port = pa.status()["port"]
    assert port["ok"] and port["permanent"] and "only takes permanent forwards" in port["message"]
    assert "permanent port forwards" in caplog.text
    pa.stop()
    assert b"DeletePortMapping" in sent[-1].data and b"<NewExternalPort>8000</NewExternalPort>" in sent[-1].data


def upnp_entry(client="192.168.1.20", port=8000, description="KaraQueue"):
    return (200, f"<r><NewInternalPort>{port}</NewInternalPort><NewInternalClient>{client}</NewInternalClient>"
                 f"<NewPortMappingDescription>{description}</NewPortMappingDescription></r>")


def test_upnp_only_forgets_a_forward_that_is_still_ours(web):
    sent, replies = web
    upnp = public.UPnP("http://192.168.1.1:5000/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1", "192.168.1.30")
    memo = {"client": "192.168.1.20"}  # this machine's address when it made the forward
    for entry in (upnp_entry(client="192.168.1.99"), upnp_entry(description="Game console"), upnp_entry(port=22)):
        replies.append(entry)
        upnp.forget(8000, 8000, memo)
        assert b"GetSpecificPortMappingEntry" in sent[-1].data  # looked, and left it alone
    replies.extend([upnp_entry(), (200, "<r/>")])
    upnp.forget(8000, 8000, memo)
    assert b"DeletePortMapping" in sent[-1].data
    replies.append((500, "<e><errorCode>714</errorCode><errorDescription>NoSuchEntryInArray</errorDescription></e>"))
    upnp.forget(8000, 8000, memo)  # gone already: fine
    assert b"GetSpecificPortMappingEntry" in sent[-1].data


def test_pcp_and_natpmp_forget_with_what_they_saved(monkeypatch):
    sock = FakeSocket()
    monkeypatch.setattr(public, "udp_socket", lambda: sock)
    pcp = public.Pcp("192.168.1.1", "192.168.1.20")
    old = bytes(range(12))
    reply = (struct.pack("!BBBBII12x", 2, 0x81, 0, 0, 0, 5) +
             struct.pack("!12sB3xHH16s", old, 6, 8000, 8000, public._v6("203.0.113.7")))
    sock.packets.append((reply, ("192.168.1.1", 5351)))
    pcp.forget(8000, 8000, {"nonce": old.hex()})
    assert sock.sent[-1][0][24:36] == old and struct.unpack("!I", sock.sent[-1][0][4:8]) == (0,)
    assert public.Pcp("192.168.1.1", "192.168.1.20").memo()["nonce"] != pcp.memo()["nonce"]

    natpmp = public.NatPmp("192.168.1.1")
    sock.packets.append((struct.pack("!BBHIHHI", 0, 130, 0, 9, 8000, 0, 0), ("192.168.1.1", 5351)))
    natpmp.forget(8000, 8000, natpmp.memo())
    assert sock.sent[-1][0] == public.natpmp_request(2, 8000, 0, 0)


# ---- talking to the router: hostile and odd devices on the network ----

DESCRIPTION_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">{base}<device><serviceList>
  <service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType><controlURL>{control}</controlURL></service>
</serviceList></device></root>"""


def test_the_control_url_has_to_be_on_the_device_that_answered():
    location = "http://192.168.1.1:5000/rootDesc.xml"
    good = DESCRIPTION_XML.format(base="", control="/ctl")
    assert public.wan_service(good, location)[0] == "http://192.168.1.1:5000/ctl"
    for base, control in (("<URLBase>http://203.0.113.9/</URLBase>", "/ctl"),
                          ("", "http://169.254.169.254/latest"),
                          ("", "https://192.168.1.1/ctl"),
                          ("<URLBase>http://192.168.1.66:5000/</URLBase>", "ctl")):
        assert public.wan_service(DESCRIPTION_XML.format(base=base, control=control), location) is None, (base, control)


def test_xml_with_a_document_type_is_turned_away():
    bomb = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><r>&a;</r>'
    with pytest.raises(public.ET.ParseError):
        public.soap_fields(bomb)
    with pytest.raises(public.ET.ParseError):
        public.wan_service(bomb.replace("<!DOCTYPE", "<!doctype"), "http://192.168.1.1/")


def test_upnp_discovery_passes_over_foreign_and_hostile_devices(web, monkeypatch):
    sent, replies = web
    def answer(sender, location):
        return (f"HTTP/1.1 200 OK\r\nLOCATION: {location}\r\nST: x\r\n\r\n".encode(), (sender, 1900))
    sock = FakeSocket([
        answer("192.168.1.66", "http://192.168.1.1:5000/rootDesc.xml"),  # someone else speaking for the router
        answer("192.168.1.66", "http://192.168.1.66/desc.xml"),  # its own, sending us elsewhere
        (b"\x00\xffgarbage", ("192.168.1.67", 1900)),
        answer("192.168.1.1", "http://192.168.1.1:5000/rootDesc.xml"),
    ])
    monkeypatch.setattr(public, "udp_socket", lambda: sock)
    monkeypatch.setattr(public, "local_ip_for", lambda host: "192.168.1.20")
    replies.extend([(200, DESCRIPTION_XML.format(base="<URLBase>http://203.0.113.9/</URLBase>", control="/ctl")),
                    (200, DESCRIPTION_XML.format(base="", control="/ctl/IPConn"))])
    found = public.UPnP.discover(timeout=1)
    assert found.control == "http://192.168.1.1:5000/ctl/IPConn" and found.local_ip == "192.168.1.20"
    assert [r.full_url for r in sent] == ["http://192.168.1.66/desc.xml", "http://192.168.1.1:5000/rootDesc.xml"]
    assert all(address == public.SSDP for _, address in sock.sent)


def test_upnp_discovery_without_a_network_finds_nothing(monkeypatch):
    monkeypatch.setattr(public, "udp_socket", lambda: FakeSocket(fail_send=True))
    assert public.UPnP.discover(timeout=0.1) is None


def test_udp_exchange_passes_over_foreign_and_odd_packets(monkeypatch):
    good = struct.pack("!BBHIHHI", 0, 130, 0, 9, 8000, 8000, 3600)
    sock = FakeSocket([
        (good, ("192.168.1.66", 5351)),  # not the router
        (b"\x00", ("192.168.1.1", 5351)),  # too short
        (struct.pack("!BBHIHHI", 0, 130, 0, 9, 9999, 9999, 3600), ("192.168.1.1", 5351)),  # about another port
        (good, ("192.168.1.1", 5351)),
    ])
    monkeypatch.setattr(public, "udp_socket", lambda: sock)
    assert public.NatPmp("192.168.1.1").add(8000, 8000, 3600) == (8000, 3600)
    sock.packets = [(b"\x00", ("192.168.1.1", 5351))]
    with pytest.raises(GatewayError, match="odd"):
        public.NatPmp("192.168.1.1").add(8000, 8000, 3600)
    sock.packets = []
    with pytest.raises(GatewayError, match="didn't answer"):
        public.NatPmp("192.168.1.1").add(8000, 8000, 3600)


def test_replies_about_another_port_or_protocol_dont_count():
    with pytest.raises(public.OddReply):
        public.natpmp_reply(struct.pack("!BBHIHHI", 0, 130, 0, 9, 9999, 8000, 3600), 2, 8000)
    nonce = bytes(range(12))
    udp = (struct.pack("!BBBBII12x", 2, 0x81, 0, 0, 3600, 5) +
           struct.pack("!12sB3xHH16s", nonce, 17, 8000, 8080, public._v6("203.0.113.7")))
    with pytest.raises(public.OddReply):
        public.pcp_map_reply(udp, nonce, 8000)


def test_router_error_text_cant_forge_log_lines(web, caplog):
    sent, replies = web
    replies.append((500, "<e><errorCode>501</errorCode><errorDescription>Bad&#13;\nWARNING\tforged line x"
                         "</errorDescription></e>"))
    upnp = public.UPnP("http://192.168.1.1:5000/ctl", "urn:x:WANIPConnection:1", "192.168.1.20")
    with pytest.raises(GatewayError) as caught:
        upnp.external_ip()
    assert str(caught.value) == "the router said no (501 Bad WARNING forged line x)" and caught.value.code == "501"
    replies.append((500, "<e><errorDescription>" + "x" * 5000 + "</errorDescription></e>"))
    with pytest.raises(GatewayError) as caught:
        upnp.external_ip()
    assert len(str(caught.value)) < 200


def test_the_lan_address_isnt_shown_from_inside_a_container(monkeypatch):
    pa, _ = updater({"port_forward": "on"}, FakeRouter())
    monkeypatch.setattr(public, "in_container", lambda: True)
    pa.check()
    assert pa.status()["lan_ip"] is None


# ---- dynamic DNS: redirects, backoff, plain http ----

def test_redirects_are_never_followed(monkeypatch):
    seen = []

    class Redirecting(urllib.request.BaseHandler):
        handler_order = 100  # before the real https handler

        def https_open(self, req):
            seen.append(req)
            headers = email.message.Message()
            headers["Location"] = "http://evil.example/steal"
            res = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, code=302)
            res.msg = "Found"
            return res

    monkeypatch.setattr(public, "OPENER", urllib.request.build_opener(public.NoRedirect, Redirecting))
    result = public.update("noip", "203.0.113.7", "party.ddns.net", "me", TOKEN)
    assert len(seen) == 1 and not result.ok and "(302)" in result.message
    assert public.NoRedirect().redirect_request(seen[0], None, 302, "Found", {}, "http://evil.example/") is None
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in public.OPENER.handlers)


def test_a_provider_in_trouble_is_asked_less_and_less_often(web):
    sent, replies = web
    pa, _ = updater({"ddns_provider": "noip", "ddns_hostname": "party.ddns.net", "ddns_username": "me",
                     "ddns_token": TOKEN})
    start = pa.now
    replies.extend([(200, "911"), (200, "911")])
    pa.check()
    assert len(sent) == 1
    pa.now = start + 300  # the next look, 5 minutes on: not yet
    pa.check()
    assert len(sent) == 1
    pa.now = start + 1800  # 30 minutes after the 911
    pa.check()
    assert len(sent) == 2
    pa.now = start + 1800 + 3000  # then twice as long, an hour: not yet
    pa.check()
    assert len(sent) == 2
    replies.append((200, "good 203.0.113.7"))
    pa.now = start + 1800 + 3600
    pa.check()
    assert len(sent) == 3 and pa.status()["ddns"]["ok"] and pa.retry_at == 0
    replies.append((200, "911"))
    pa.check(force=True)  # Check now always asks
    assert len(sent) == 4


def test_a_plain_http_update_url_with_a_password_is_flagged():
    pa, settings = updater({"ddns_provider": "custom", "ddns_url": "http://dns.example.net/u?ip={ip}", "ddns_token": "x"})
    assert pa.status()["plain_auth"] is True
    settings["ddns_url"] = "https://dns.example.net/u?ip={ip}"
    assert pa.status()["plain_auth"] is False


def test_hostname_labels_are_63_characters_at_most():
    assert settings_module._coerce("ddns_hostname", "a" * 63 + ".duckdns.org") == "a" * 63 + ".duckdns.org"
    with pytest.raises(Error):
        settings_module._coerce("ddns_hostname", "a" * 64 + ".duckdns.org")


# ---- the admin page ----

def test_admin_page_keeps_the_token_secret_and_checks_on_save(web, monkeypatch, caplog):
    sent, replies = web
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "public-test")
    client = TestClient(create_app(Store()))
    client.headers["Authorization"] = "Bearer " + client.post("/api/admin/login", json={"password": "public-test"}).json()["token"]
    pa = client.app.state.public
    pa.lookup_ip = lambda: "203.0.113.7"
    pa.gateways = lambda: iter([])

    state = client.get("/api/admin").json()
    assert state["public"]["on"] is False and not sent
    assert client.put("/api/admin/settings/ddns_hostname", json={"value": "https://x.duckdns.org"}).status_code == 400
    assert client.put("/api/admin/settings/ddns_url", json={"value": "ftp://x"}).status_code == 400
    client.put("/api/admin/settings/ddns_hostname", json={"value": " Party.DuckDNS.org "})
    client.put("/api/admin/settings/ddns_token", json={"value": TOKEN})
    replies.append((200, "OK"))
    state = client.put("/api/admin/settings/ddns_provider", json={"value": "duckdns"}).json()
    assert query(sent[-1]) == {"domains": "party", "token": TOKEN, "ip": "203.0.113.7"}
    assert state["public"]["ddns"]["ok"] and state["public"]["public_ip"] == "203.0.113.7"
    assert state["public"]["url"] == "http://party.duckdns.org:8000"
    token_setting = next(s for s in state["settings"] if s["key"] == "ddns_token")
    assert token_setting["secret"] and token_setting["value"] is True
    assert TOKEN not in client.get("/api/admin").text and TOKEN not in caplog.text

    replies.append((200, "OK"))
    assert client.post("/api/admin/public/check").json()["public"]["ddns"]["ok"] and len(sent) == 2
    assert TestClient(create_app(Store())).post("/api/admin/public/check").status_code == 401


def test_a_failing_check_doesnt_turn_saving_a_setting_into_a_500(monkeypatch, caplog):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "public-test")
    client = TestClient(create_app(Store()), raise_server_exceptions=False)
    client.headers["Authorization"] = "Bearer " + client.post("/api/admin/login", json={"password": "public-test"}).json()["token"]
    pa = client.app.state.public
    pa.lookup_ip = lambda: "203.0.113.7"

    def broken():
        raise OSError(101, "Network is unreachable")
        yield
    pa.gateways = broken
    res = client.put("/api/admin/settings/port_forward", json={"value": "on"})
    assert res.status_code == 200 and "went wrong" in res.json()["public"]["error"]
    assert "OSError" in caplog.text and "Traceback" not in caplog.text
    assert client.post("/api/admin/public/check").status_code == 200
    pa.gateways = lambda: iter([FakeRouter()])
    assert "error" not in client.post("/api/admin/public/check").json()["public"]


def test_a_secret_saved_value_that_is_ignored_isnt_logged(caplog):
    store = Store()
    store.save_setting("ddns_url", "ftp://dns.example.net/update?key=" + TOKEN)  # written by hand: fails the rules
    create_app(store)
    assert "Ignoring ddns_url saved on the admin page (hidden)" in caplog.text and TOKEN not in caplog.text


def test_guesses_from_elsewhere_dont_lock_the_admin_out():
    from karaqueue.guards import Logins
    logins = Logins(per_address=10, per_name=3)
    for address in ("a", "b", "c"):
        logins.failed([("address", address), ("name", "")])
    logins.check([("address", "home"), ("name", "")], spare_clean=True)  # never got it wrong itself
    with pytest.raises(Error):
        logins.check([("address", "home"), ("name", "")])  # without it, locked as before
    logins.failed([("address", "home"), ("name", "")])
    with pytest.raises(Error):  # one guess only, then it's locked like the rest
        logins.check([("address", "home"), ("name", "")], spare_clean=True)


def test_only_the_admin_password_is_spared_a_lock_from_elsewhere(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "correct horse")
    app = create_app(Store())
    client = TestClient(app)
    root = {"Authorization": "Bearer " + client.post("/api/admin/login", json={"password": "correct horse"}).json()["token"]}
    client.post("/api/admin/users", headers=root, json={"name": "Kit", "role": "host", "password": "long enough"})
    for n in range(50):  # the per-name limit, from addresses that never get near their own
        guesser = TestClient(app, client=(f"203.0.113.{n}", 50000))
        assert guesser.post("/api/admin/login", json={"name": "Kit", "password": "nope nope"}).status_code == 401
        assert guesser.post("/api/admin/login", json={"password": "nope nope"}).status_code == 401
    home = TestClient(app, client=("198.51.100.7", 50000))
    # A named account stays locked, even for an address that never got it wrong,
    assert home.post("/api/admin/login", json={"name": "Kit", "password": "long enough"}).status_code == 429
    # while the admin password still lets that address in.
    assert home.post("/api/admin/login", json={"password": "correct horse"}).status_code == 200


def test_the_updater_runs_with_the_server_and_stops_with_it():
    with TestClient(create_app(Store())) as client:
        pa = client.app.state.public
        assert pa.thread.is_alive()
    assert not pa.thread.is_alive()
