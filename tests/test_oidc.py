"""A local provider exercises discovery, PKCE, signed ID tokens and the browser handoff."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from karaqueue import oidc
from karaqueue.app import create_app
from karaqueue.settings import Settings, hash_password, password_matches
from karaqueue.store import SCHEMA, Error, Store


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def provider(monkeypatch):
    jwt = pytest.importorskip("jwt", reason="Install the dev or oidc extra to test signed OIDC tokens")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": "test-key", "alg": "RS256"}
    p = SimpleNamespace(claims={}, nonce="", challenge="", calls=[], wrong_signature=False,
                        metadata={}, keys=[jwk], alg="RS256", key=key, other_key=other_key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            p.calls.append(self.path)
            if self.path == "/.well-known/openid-configuration":
                self.reply({"issuer": p.issuer, "authorization_endpoint": p.issuer + "/authorize",
                            "token_endpoint": p.issuer + "/token", "jwks_uri": p.issuer + "/keys",
                            "id_token_signing_alg_values_supported": ["RS256"], **p.metadata})
            elif self.path == "/keys":
                self.reply({"keys": p.keys})
            else:
                self.send_error(404)

        def do_POST(self):
            p.calls.append(self.path)
            data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            assert self.path == "/token" and data["grant_type"] == ["authorization_code"]
            assert data["code"] == ["test-code"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            assert challenge == p.challenge
            assert data["redirect_uri"] == [p.redirect_uri]
            if p.metadata.get("token_endpoint_auth_methods_supported") == ["client_secret_post"]:
                assert data["client_id"] == ["karaqueue"] and data["client_secret"] == ["test-secret"]
            elif p.metadata.get("token_endpoint_auth_methods_supported") == ["none"]:
                assert data["client_id"] == ["karaqueue"] and "client_secret" not in data
            else:
                assert self.headers["Authorization"] == "Basic " + base64.b64encode(b"karaqueue:test-secret").decode()
            claims = {"iss": p.issuer, "sub": "subject-1", "aud": "karaqueue", "exp": int(time.time()) + 300,
                      "iat": int(time.time()), "nonce": p.nonce, "preferred_username": "Alex", **p.claims}
            token = jwt.encode(claims, other_key if p.wrong_signature else key,
                               algorithm=p.alg, headers={"kid": "test-key"})
            self.reply({"id_token": token, "access_token": "test-access"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    p.issuer = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("KARAQUEUE_OIDC_ISSUER", p.issuer)
    monkeypatch.setenv("KARAQUEUE_OIDC_CLIENT_ID", "karaqueue")
    monkeypatch.setenv("KARAQUEUE_OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("KARAQUEUE_NEW_ROOMS", "accounts")
    try:
        yield p
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def start(client, provider, target="home"):
    response = client.get("/auth/oidc/start", params={"target": target}, follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["response_type"] == ["code"] and params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid profile email"]
    provider.nonce, provider.challenge = params["nonce"][0], params["code_challenge"][0]
    provider.redirect_uri = params["redirect_uri"][0]
    return params["state"][0]


def callback(client, state):
    return client.get("/auth/oidc/callback", params={"state": state, "code": "test-code"}, follow_redirects=False)


def sign_in(client, provider, target="home"):
    response = callback(client, start(client, provider, target))
    assert response.status_code == 303
    assert response.headers["location"] == ("/admin" if target == "admin" else "/") + "#oidc=1"
    assert "no-store" in response.headers["cache-control"]
    handoff = client.post("/auth/oidc/session", headers={"X-KaraQueue-OIDC": "1"})
    assert handoff.status_code == 200
    assert "no-store" in handoff.headers["cache-control"]
    assert client.post("/auth/oidc/session", headers={"X-KaraQueue-OIDC": "1"}).status_code == 401
    return handoff.json()


def test_unset_has_no_sign_in_and_no_public_configuration(monkeypatch):
    monkeypatch.delenv("KARAQUEUE_OIDC_ISSUER", raising=False)
    client = TestClient(create_app(Store()))
    assert "oidc" not in client.get("/api/config").json()
    assert "oidc" not in client.get("/api/admin/status").json()
    assert client.get("/auth/oidc/start").status_code == 404
    assert client.get("/auth/oidc/callback").status_code == 404
    assert client.post("/auth/oidc/session").status_code == 404


def test_missing_optional_dependency_explains_how_to_enable_sso(monkeypatch):
    monkeypatch.setenv("KARAQUEUE_OIDC_ISSUER", "https://login.example.com")
    monkeypatch.setitem(sys.modules, "jwt", None)
    client = TestClient(create_app(Store()))
    response = client.get("/auth/oidc/start")
    assert response.status_code == 503
    assert "karaqueue[oidc]" in response.json()["detail"]


def test_happy_path_uses_normal_host_sessions_and_keeps_guests_public(provider):
    store = Store()
    client = TestClient(create_app(store))
    assert client.post("/api/rooms", json={"name": "Friday"}).status_code == 401
    who = sign_in(client, provider)
    assert who["role"] == "host" and who["name"] == "Alex"
    assert store.staff_session(who["token"])["user_id"] == who["id"]
    host = bearer(who["token"])
    assert client.get("/api/admin", headers=host).status_code == 403
    room = client.post("/api/rooms", headers=host, json={"name": "Friday"}).json()
    code = room["code"]
    guest = TestClient(client.app)
    assert guest.get(f"/r/{code}").status_code == 200
    assert guest.get(f"/r/{code}/screen").status_code == 200
    assert guest.post(f"/api/rooms/{code}/singers", json={"name": "Sam"}).status_code == 200
    for _ in range(10):
        latest = sign_in(client, provider)
    assert store.staff_session(who["token"]) is None  # the same ten-device cap
    store.end_staff_sessions(who["id"])
    assert store.staff_session(latest["token"]) is None
    assert set(provider.calls) == {"/.well-known/openid-configuration", "/token", "/keys"}


@pytest.mark.parametrize("claim,value", [("nonce", "wrong"), ("aud", "someone-else"), ("exp", 1),
                                         ("iss", "https://other.example"), ("sub", ""), ("azp", "someone-else"),
                                         ("aud", ["karaqueue", "another-client"]), ("at_hash", "wrong")])
def test_invalid_claims_are_refused(provider, claim, value):
    client = TestClient(create_app(Store()))
    provider.claims[claim] = value
    assert callback(client, start(client, provider)).status_code == 401
    assert client.post("/auth/oidc/session", headers={"X-KaraQueue-OIDC": "1"}).status_code == 401


def test_wrong_signature_is_refused(provider):
    provider.wrong_signature = True
    client = TestClient(create_app(Store()))
    assert callback(client, start(client, provider)).status_code == 401


@pytest.mark.parametrize("change", [{"use": "enc"}, {"kid": "another-key"}, {"alg": "RS512"},
                                   {"key_ops": ["sign"]}])
def test_only_the_matching_signing_key_is_used(provider, change):
    provider.keys[0].update(change)
    client = TestClient(create_app(Store()))
    assert callback(client, start(client, provider)).status_code == 401


def test_discovery_must_match_the_configured_issuer(provider):
    provider.metadata["issuer"] = "https://other.example.com"
    client = TestClient(create_app(Store()))
    response = client.get("/auth/oidc/start", follow_redirects=False)
    assert response.status_code == 503
    assert provider.calls == ["/.well-known/openid-configuration"]


def test_bad_state_browser_binding_and_replay_are_refused(provider):
    client = TestClient(create_app(Store()))
    state = start(client, provider)
    assert callback(client, "wrong-state").status_code == 401
    assert "/token" not in provider.calls
    # Knowing the state isn't enough: it must be the browser that started signing in.
    assert callback(TestClient(client.app), state).status_code == 401
    assert callback(client, state).status_code == 401
    state = start(client, provider)
    assert callback(client, state).status_code == 303
    assert callback(client, state).status_code == 401
    assert client.post("/auth/oidc/session").status_code == 403  # no cross-site form handoff
    assert TestClient(client.app).post("/auth/oidc/session", headers={"X-KaraQueue-OIDC": "1"}).status_code == 401


def test_expired_or_reconfigured_transactions_are_refused(provider, monkeypatch):
    app = create_app(Store())
    client = TestClient(app)
    state = start(client, provider)
    monkeypatch.setattr(oidc, "SECONDS", -1)
    assert callback(client, state).status_code == 401
    monkeypatch.setattr(oidc, "SECONDS", 600)
    state = start(client, provider)
    app.state.settings.set("oidc_client_id", "new-client")
    assert callback(client, state).status_code == 401
    assert "/token" not in provider.calls


def test_same_subject_finds_the_account_not_a_matching_name_and_disabled_stays_refused(provider):
    store = Store()
    password = hash_password("a local password")
    local = store.add_user("Alex", "admin", password)
    client = TestClient(create_app(store))
    who = sign_in(client, provider)
    assert who["id"] != local and who["role"] == "host" and who["name"] != "Alex"
    assert password_matches("a local password", store.user(local)["password"])
    provider.claims["preferred_username"] = "New name"
    again = sign_in(client, provider)
    assert again["id"] == who["id"] and again["name"] == who["name"]
    store.update_user(who["id"], disabled=1)
    assert callback(client, start(client, provider)).status_code == 403
    assert client.get("/api/admin/me", headers=bearer(who["token"])).status_code == 401


def test_admin_group_maps_and_remaps_existing_accounts(provider, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_OIDC_ADMIN_GROUP", "karaqueue-admins")
    monkeypatch.setenv("KARAQUEUE_OIDC_GROUPS_CLAIM", "roles")
    store = Store()
    client = TestClient(create_app(store))
    provider.claims["roles"] = ["karaqueue-admins"]
    who = sign_in(client, provider, "admin")
    assert who["role"] == "admin"
    assert client.get("/api/admin", headers=bearer(who["token"])).status_code == 200
    provider.claims["roles"] = "karaqueue-admins"  # not an array: don't accidentally match a substring
    again = sign_in(client, provider)
    assert again["id"] == who["id"] and again["role"] == "host"
    assert client.get("/api/admin", headers=bearer(who["token"])).status_code == 403


def test_default_none_refuses_unmapped_sign_in(provider, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_OIDC_DEFAULT_ROLE", "none")
    store = Store()
    client = TestClient(create_app(store))
    assert callback(client, start(client, provider)).status_code == 403
    assert store.users() == []


@pytest.mark.parametrize("initial_role", ["admin", "host"])
def test_denied_remap_revokes_access_and_can_be_granted_again(provider, monkeypatch, tmp_path, initial_role):
    monkeypatch.setenv("KARAQUEUE_OIDC_ADMIN_GROUP", "karaqueue-admins")
    path = str(tmp_path / "accounts.db")
    store = Store(path)
    client = TestClient(create_app(store))
    provider.claims["groups"] = ["karaqueue-admins"] if initial_role == "admin" else []
    who = sign_in(client, provider)
    assert who["role"] == initial_role
    second = sign_in(client, provider)
    invite = store.add_invite(who["name"], who["id"], role="admin")
    reset = store.add_invite("admin", None, user_id=who["id"])
    client.app.state.settings.set("oidc_default_role", "none")
    provider.claims["groups"] = []
    assert callback(client, start(client, provider)).status_code == 403
    assert client.post("/auth/oidc/session", headers={"X-KaraQueue-OIDC": "1"}).status_code == 401
    for token in (who["token"], second["token"]):
        assert store.staff_session(token) is None
        assert client.get("/api/admin/me", headers=bearer(token)).status_code == 401
        assert client.get("/api/admin", headers=bearer(token)).status_code == 401
        assert client.post("/api/rooms", headers=bearer(token), json={"name": "Friday"}).status_code == 401
    assert store.invite(invite) is None and store.invite(reset) is None
    assert store.user(who["id"])["role"] == "none"
    assert not store.user(who["id"])["disabled"]
    # A restart must not restore access, even if a local password or session is issued later.
    store.db.close()
    store = Store(path)
    client = TestClient(create_app(store))
    store.update_user(who["id"], password=hash_password("a local password"))
    assert client.post("/api/admin/login", json={"name": who["name"], "password": "a local password"}).status_code == 403
    token = store.start_staff_session(who["id"])
    assert client.post("/api/rooms", headers=bearer(token), json={"name": "Friday"}).status_code == 401
    provider.claims["groups"] = ["karaqueue-admins"]
    again = sign_in(client, provider)
    assert again["id"] == who["id"] and again["role"] == "admin"
    store.update_user(who["id"], disabled=1)
    assert callback(client, start(client, provider)).status_code == 403
    store.db.close()


def test_existing_users_gain_none_role_without_losing_links_sessions_or_invites(tmp_path):
    path = str(tmp_path / "accounts.db")
    # Seed the previous schema, including rows that refer to the table being upgraded.
    db = sqlite3.connect(path)
    db.executescript(SCHEMA.replace("'admin', 'host', 'none'", "'admin', 'host'"))
    user_id, token, invite, now = 1, "old-token", "old-invite", time.time()
    db.execute("INSERT INTO users (id, name, role, password, created_at) VALUES (1, 'Alex', 'admin', 'unused', ?)", (now,))
    db.execute("INSERT INTO oidc_accounts VALUES ('https://login.example.com', 'subject', 1)")
    db.execute("INSERT INTO staff_sessions (token_hash, user_id, created_at, seen_at) VALUES (?, 1, ?, ?)",
               (hashlib.sha256(token.encode()).hexdigest(), now, now))
    db.execute("INSERT INTO invites (token_hash, role, made_by, made_by_id, created_at, expires_at) "
               "VALUES (?, 'host', 'Alex', 1, ?, ?)", (hashlib.sha256(invite.encode()).hexdigest(), now, now + 3600))
    db.commit()
    db.close()
    store = Store(path)
    assert store.staff_session(token)["user_id"] == user_id
    assert store.invite(invite)["role"] == "host"
    with pytest.raises(Error):
        store.oidc_user("https://login.example.com", "subject", "Alex", "none", "unused")
    assert store.user(user_id)["role"] == "none"
    assert store.staff_session(token) is None and store.invite(invite) is None
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.oidc_user("https://login.example.com", "subject", "Alex", "host", "unused") == user_id
    store.delete_user(user_id)
    assert store.db.execute("SELECT * FROM oidc_accounts").fetchall() == []
    store.db.close()


@pytest.mark.parametrize("role", ["admin", "host"])
def test_denied_linked_account_cannot_use_staff_sessions_or_passwords(role):
    store = Store()
    cfg = Settings(store, env={})
    password = hash_password("a local password")
    user_id = store.oidc_user("https://login.example.com", "subject", "Alex", role, password)
    tokens = [store.start_staff_session(user_id) for _ in range(2)]
    invite = store.add_invite("Alex", user_id, role="admin")
    reset = store.add_invite("admin", None, user_id=user_id)
    with pytest.raises(Error) as denied:
        store.oidc_user("https://login.example.com", "subject", "Alex", "none", password)
    assert denied.value.status == 403
    assert store.user(user_id)["role"] == "none" and not store.user(user_id)["disabled"]
    assert store.invite(invite) is None and store.invite(reset) is None
    for token in tokens:
        assert store.staff_session(token) is None
        for required in ("admin", "host"):
            with pytest.raises(Error):
                cfg.require(token, required)
    with pytest.raises(Error):
        cfg.log_in("Alex", "a local password")
    token = store.start_staff_session(user_id)
    assert cfg.whoami(token) is None  # Even an otherwise valid session cannot authorize role=none.
    assert store.oidc_user("https://login.example.com", "subject", "Alex", role, password) == user_id
    assert cfg.whoami(cfg.log_in("Alex", "a local password"))["role"] == role


@pytest.mark.parametrize("method", ["client_secret_post", "none"])
def test_provider_client_auth_methods(provider, monkeypatch, method):
    provider.metadata["token_endpoint_auth_methods_supported"] = [method]
    if method == "none":
        monkeypatch.setenv("KARAQUEUE_OIDC_CLIENT_SECRET", "")
    assert sign_in(TestClient(create_app(Store())), provider)["role"] == "host"


def test_public_url_drives_the_redirect_and_secure_cookies(provider, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_PUBLIC_URL", "https://karaoke.example.com")
    client = TestClient(create_app(Store()), base_url="https://karaoke.example.com")
    state = start(client, provider)
    assert provider.redirect_uri == "https://karaoke.example.com/auth/oidc/callback"
    cookie = client.cookies.get(oidc.COOKIE)
    assert cookie
    response = callback(client, state)
    assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    assert response.status_code == 303
    assert client.post("/auth/oidc/session", headers={"X-KaraQueue-OIDC": "1"}).status_code == 200
    assert client.get("/auth/oidc/start?target=https://elsewhere.example", follow_redirects=False).status_code == 400


def test_oidc_only_keeps_guest_passwords_and_disabled_oidc_keeps_staff_passwords(provider, monkeypatch):
    monkeypatch.setenv("KARAQUEUE_OIDC_ONLY", "on")
    monkeypatch.setenv("KARAQUEUE_ADMIN_PASSWORD", "local admin password")
    app = create_app(Store())
    client = TestClient(app)
    assert client.get("/api/config").json()["oidc"] == {"name": "Sign in with SSO", "only": True}
    assert client.post("/api/admin/login", json={"password": "local admin password"}).status_code == 403
    assert client.post("/api/guests", json={"name": "Sam", "password": "guest password"}).status_code == 200
    assert client.post("/api/guests/login", json={"name": "Sam", "password": "guest password"}).status_code == 200
    app.state.settings.set("oidc_issuer", "")
    assert "oidc" not in client.get("/api/config").json()
    assert client.post("/api/admin/login", json={"password": "local admin password"}).status_code == 200


def test_settings_hide_client_secret_and_allow_saved_values():
    cfg = Settings(Store(), env={"KARAQUEUE_OIDC_CLIENT_SECRET": "test-secret"})
    cfg.set("oidc_name", "Sign in for karaoke")
    assert cfg.get("oidc_name") == "Sign in for karaoke"
    secret = next(row for row in cfg.describe() if row["key"] == "oidc_client_secret")
    assert secret["secret"] and secret["value"] is True
    assert "test-secret" not in json.dumps(cfg.describe())


def test_existing_database_gains_links_and_subjects_are_scoped_to_issuers(tmp_path):
    path = str(tmp_path / "accounts.db")
    store = Store(path)
    password = hash_password("unused random password")
    local = store.add_user("Alex", "host", password)
    store.db.execute("DROP TABLE oidc_accounts")  # a database from before OIDC
    store.db.close()
    store = Store(path)
    linked = store.oidc_user("https://first.example", "subject-1", "Alex", "host", password)
    other = store.oidc_user("https://second.example", "subject-1", "Alex", "host", password)
    assert len({local, linked, other}) == 3
    token = store.start_staff_session(linked)
    store.db.close()
    store = Store(path)
    assert store.oidc_user("https://first.example", "subject-1", "New name", "admin", password) == linked
    assert store.staff_session(token)["user_id"] == linked
    store.delete_user(linked)
    assert store.staff_session(token) is None
    assert store.oidc_user("https://first.example", "subject-1", "Alex", "host", password) != other
    store.db.close()
