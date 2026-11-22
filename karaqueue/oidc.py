"""Optional host sign-in through an OpenID Connect provider."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .store import Error

ALGORITHMS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
COOKIE = "kq_oidc"
SESSION_COOKIE = "kq_oidc_session"
SECONDS = 600


def _url(value: str) -> str:
    parts = urlsplit(value)
    # HTTP is useful for a local test provider, but never send credentials over it on a network.
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (parts.scheme != "https" and not (parts.scheme == "http" and local)) or not parts.netloc:
        raise ValueError("OIDC needs https")
    if parts.username or parts.password or parts.fragment:
        raise ValueError("Invalid OIDC URL")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Discovery already gives the endpoints. Don't forward a client secret through a redirect.
        return None


def fetch(url: str, data=None, headers=None) -> dict:
    request = Request(_url(url), data=data, headers={"Accept": "application/json", **(headers or {})})
    with build_opener(_NoRedirect()).open(request, timeout=10) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("OIDC response too large")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("Invalid OIDC response")
    return result


class OIDC:
    def __init__(self, get):
        self.get = get
        self.pending = {}
        self.lock = threading.Lock()

    def enabled(self) -> bool:
        return bool(self.get("oidc_issuer"))

    def only(self) -> bool:
        return self.enabled() and self.get("oidc_only") == "on"

    def public(self) -> dict:
        return {"oidc": {"name": self.get("oidc_name"), "only": self.only()}} if self.enabled() else {}

    def _config(self) -> tuple:
        if not self.enabled():
            raise Error("SSO isn't enabled on this server", 404)
        return tuple(self.get("oidc_" + key) for key in
                     ("issuer", "client_id", "client_secret", "scopes", "default_role", "admin_group", "groups_claim"))

    def _discovery(self, issuer: str) -> dict:
        _url(issuer)
        if urlsplit(issuer).query:
            raise ValueError("Invalid issuer")
        metadata = fetch(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if metadata["issuer"] != issuer:
            raise ValueError("Wrong issuer")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            _url(metadata[key])
        return metadata

    def start(self, redirect_uri: str, target: str) -> tuple:
        config = self._config()
        try:
            # Lazy import: the core server needs no crypto dependency when SSO is off.
            import jwt

            if "RS256" not in jwt.algorithms.get_default_algorithms():
                raise ImportError("Missing crypto backend")

            issuer, client_id, _, scopes, *_ = config
            if not client_id or "openid" not in scopes.split():
                raise ValueError("Missing client or scope")
            metadata = self._discovery(issuer)
            state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            with self.lock:
                now = time.time()
                self.pending = {key: value for key, value in self.pending.items() if now - value[0] < SECONDS}
                if len(self.pending) >= 1000:
                    raise Error("Sign-in is busy. Try again in a few minutes.", 503)
                self.pending[state] = (now, browser, nonce, verifier, redirect_uri, target, config, metadata)
            query = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                               "scope": scopes, "state": state, "nonce": nonce,
                               "code_challenge": challenge, "code_challenge_method": "S256"})
            endpoint = metadata["authorization_endpoint"]
            return endpoint + ("&" if "?" in endpoint else "?") + query, browser
        except ImportError:
            raise Error("SSO isn't installed. Ask the server admin to install karaqueue[oidc].", 503)
        except Error:
            raise
        except Exception:
            # Provider errors can contain tokens or client secrets. Never include them in a response or log.
            raise Error("Couldn't reach sign-in. Ask an admin to check the SSO settings.", 503) from None

    def finish(self, state: str, browser: str, code: str) -> tuple:
        config = self._config()
        with self.lock:
            pending = self.pending.pop(state, None)
        if not pending or not browser or not secrets.compare_digest(browser.encode(), pending[1].encode()):
            raise Error("That sign-in link isn't valid. Start signing in again.", 401)
        created, _, nonce, verifier, redirect_uri, target, old_config, metadata = pending
        if time.time() - created >= SECONDS or old_config != config or not code or len(code) > 8192:
            raise Error("That sign-in has expired. Start signing in again.", 401)
        try:
            import jwt

            issuer, client_id, secret, _, default_role, admin_group, groups_claim = config
            data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                    "code_verifier": verifier}
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
            if secret and "client_secret_basic" in methods:
                # OAuth client credentials are form-encoded before HTTP Basic encoding.
                credentials = urlencode({"": client_id})[1:] + ":" + urlencode({"": secret})[1:]
                headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
            elif secret and "client_secret_post" in methods:
                data.update(client_id=client_id, client_secret=secret)
            elif not secret and "none" in methods:
                data["client_id"] = client_id
            else:
                raise ValueError("Unsupported client authentication")
            tokens = fetch(metadata["token_endpoint"], urlencode(data).encode(), headers)
            encoded = tokens["id_token"]
            header = jwt.get_unverified_header(encoded)
            alg = header.get("alg")
            if alg not in ALGORITHMS or alg not in metadata.get("id_token_signing_alg_values_supported", ["RS256"]):
                raise ValueError("Unsupported signing algorithm")
            keys = fetch(metadata["jwks_uri"])["keys"]
            keys = [key for key in keys if key.get("kid") == header.get("kid") and key.get("use", "sig") == "sig"
                    and key.get("alg", alg) == alg and "verify" in key.get("key_ops", ["verify"])]
            if len(keys) != 1 or header.get("crit"):
                raise ValueError("Invalid signing key")
            key = jwt.PyJWK.from_dict(keys[0], algorithm=alg)
            claims = jwt.decode(encoded, key.key, algorithms=[alg], issuer=issuer, audience=client_id,
                                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255:
                raise ValueError("Invalid subject")
            if not isinstance(claims["nonce"], str) or not secrets.compare_digest(claims["nonce"].encode(), nonce.encode()):
                raise ValueError("Invalid nonce")
            audiences = claims["aud"]
            if (isinstance(audiences, list) and len(audiences) > 1 and "azp" not in claims
                    or claims.get("azp", client_id) != client_id):
                raise ValueError("Invalid authorized party")
            if "at_hash" in claims:
                digest = getattr(hashlib, "sha" + alg[-3:])(tokens["access_token"].encode("ascii")).digest()
                expected = base64.urlsafe_b64encode(digest[:len(digest) // 2]).rstrip(b"=").decode()
                if claims["at_hash"] != expected:
                    raise ValueError("Invalid access token hash")
            groups = claims.get(groups_claim, [])
            role = "admin" if admin_group and isinstance(groups, list) and admin_group in groups else default_role
            name = claims.get("preferred_username") or claims.get("name") or "Host"
            return issuer, claims["sub"], name if isinstance(name, str) else "Host", role, target
        except Exception:
            raise Error("Couldn't verify that sign-in. Start signing in again.", 401) from None
