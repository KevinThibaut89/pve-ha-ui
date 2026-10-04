"""Minimal Proxmox VE API client (standard library only).

Talks to ``/api2/json`` with ticket auth, sends the CSRF token on writes, and
fails over to the next configured host when one is unreachable — which is
exactly when an HA tool is needed most.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any


class PVEError(Exception):
    """An error answered by the Proxmox API (or no host reachable)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Auth:
    ticket: str
    csrf: str
    username: str


@dataclass
class _Host:
    scheme: str
    host: str
    port: int

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"


def parse_host(spec: str) -> _Host:
    """Accept ``pve1``, ``10.0.0.5:8006`` or ``https://pve1:8006``."""
    if "://" not in spec:
        spec = "https://" + spec
    u = urllib.parse.urlsplit(spec)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"bad host: {spec!r}")
    return _Host(u.scheme, u.hostname, u.port or 8006)


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").strip().lower()


class PVEClient:
    def __init__(
        self,
        hosts: list[str],
        *,
        verify_tls: bool = True,
        ca_file: str | None = None,
        fingerprints: list[str] | None = None,
        timeout: float = 8.0,
    ):
        if not hosts:
            raise ValueError("at least one Proxmox host is required")
        self.hosts = [parse_host(h) for h in hosts]
        self.timeout = timeout
        self.fingerprints = {_norm_fp(f) for f in (fingerprints or [])}
        self._preferred = 0
        self._lock = threading.Lock()

        if self.fingerprints:
            # Pinned certificates: the fingerprint check replaces CA validation.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif verify_tls:
            # System CAs (e.g. ACME certs) plus the cluster CA if given.
            ctx = ssl.create_default_context()
            if ca_file:
                ctx.load_verify_locations(cafile=ca_file)
        else:
            ctx = ssl._create_unverified_context()
        self._ssl = ctx

    # ------------------------------------------------------------ transport
    def _connect(self, h: _Host) -> http.client.HTTPConnection:
        if h.scheme == "http":
            conn = http.client.HTTPConnection(h.host, h.port, timeout=self.timeout)
            conn.connect()
            return conn
        conn = http.client.HTTPSConnection(h.host, h.port, timeout=self.timeout, context=self._ssl)
        conn.connect()
        if self.fingerprints:
            der = conn.sock.getpeercert(binary_form=True)
            if hashlib.sha256(der).hexdigest() not in self.fingerprints:
                conn.close()
                raise ssl.SSLError(f"certificate fingerprint of {h.label} is not pinned")
        return conn

    def _order(self) -> list[int]:
        n = len(self.hosts)
        with self._lock:
            start = self._preferred
        return [(start + i) % n for i in range(n)]

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        auth: Auth | None = None,
    ) -> Any:
        """Call ``/api2/json{path}`` and return the ``data`` member."""
        method = method.upper()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = "/api2/json" + path
        body = None
        headers = {"Accept": "application/json"}
        if params:
            enc = urllib.parse.urlencode(
                {k: (int(v) if isinstance(v, bool) else v) for k, v in params.items()}
            )
            if method in ("GET", "DELETE"):
                url += "?" + enc
            else:
                body = enc.encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        if auth:
            # Sent raw, as proxmoxer does; tickets never contain ";" or ",".
            headers["Cookie"] = "PVEAuthCookie=" + auth.ticket
            if method != "GET":
                headers["CSRFPreventionToken"] = auth.csrf

        errors = []
        for idx in self._order():
            h = self.hosts[idx]
            try:
                conn = self._connect(h)
                try:
                    conn.request(method, url, body=body, headers=headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                finally:
                    conn.close()
            except (OSError, socket.timeout, http.client.HTTPException) as e:
                errors.append(f"{h.label}: {e}")
                continue
            with self._lock:
                self._preferred = idx
            return self._decode(resp.status, resp.reason, raw)
        raise PVEError(503, "no Proxmox host reachable (" + "; ".join(errors) + ")")

    @staticmethod
    def _decode(status: int, reason: str, raw: bytes) -> Any:
        try:
            payload = json.loads(raw.decode() or "{}")
        except ValueError:
            payload = {}
        if status >= 400:
            msg = reason or "error"
            errs = payload.get("errors") if isinstance(payload, dict) else None
            if errs:
                msg += ": " + "; ".join(f"{k}: {v}" for k, v in errs.items())
            elif isinstance(payload, dict) and payload.get("message"):
                msg = str(payload["message"]).strip()
            raise PVEError(status, msg)
        return payload.get("data") if isinstance(payload, dict) else None

    # ---------------------------------------------------------------- probe
    def _peer_fingerprint(self, h: _Host) -> str:
        """SHA-256 of the certificate the host presents (no trust decision)."""
        ctx = ssl._create_unverified_context()
        with socket.create_connection((h.host, h.port), timeout=self.timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=h.host) as tls:
                der = tls.getpeercert(binary_form=True)
        hexstr = hashlib.sha256(der).hexdigest().upper()
        return ":".join(hexstr[i:i + 2] for i in range(0, 64, 2))

    def probe_host(self, h: _Host) -> dict[str, Any]:
        """Check one host without credentials: reachable, TLS trusted, really PVE?"""
        res: dict[str, Any] = {"host": h.label if h.scheme == "https" else f"http://{h.label}",
                               "ok": False, "error": None, "fingerprint": None, "untrusted": False}
        if h.scheme == "https":
            try:
                res["fingerprint"] = self._peer_fingerprint(h)
            except (OSError, ssl.SSLError) as e:
                res["error"] = f"unreachable ({e})"
                return res
        try:
            conn = self._connect(h)
            try:
                conn.request("GET", "/api2/json/access/domains", headers={"Accept": "application/json"})
                resp = conn.getresponse()
                raw = resp.read()
            finally:
                conn.close()
            data = self._decode(resp.status, resp.reason, raw)
            if not isinstance(data, list):
                raise PVEError(502, "answers, but is not a Proxmox VE API")
            res["ok"] = True
        except ssl.SSLCertVerificationError as e:
            res["error"] = f"certificate not trusted ({e.verify_message})"
            res["untrusted"] = True
        except ssl.SSLError as e:
            res["error"] = str(e)
            res["untrusted"] = "not pinned" in str(e)
        except (OSError, socket.timeout, http.client.HTTPException) as e:
            res["error"] = f"unreachable ({e})"
        except PVEError as e:
            res["error"] = e.message
        return res

    def probe(self) -> list[dict[str, Any]]:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(self.hosts))) as pool:
            return list(pool.map(self.probe_host, self.hosts))

    # ----------------------------------------------------------------- auth
    def login(self, username: str, password: str) -> dict[str, Any]:
        """Return the raw ticket response (may carry ``NeedTFA``)."""
        return self.request("POST", "/access/ticket", {"username": username, "password": password})

    def complete_tfa(self, username: str, challenge: str, response: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/access/ticket",
            {"username": username, "tfa-challenge": challenge, "password": response},
        )

    def renew(self, auth: Auth) -> Auth:
        data = self.request("POST", "/access/ticket", {"username": auth.username, "password": auth.ticket})
        return Auth(data["ticket"], data["CSRFPreventionToken"], data["username"])
