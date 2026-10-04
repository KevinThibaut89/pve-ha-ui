"""HTTP(S) server: static UI + a small JSON API in front of Proxmox."""

from __future__ import annotations

import json
import logging
import re
import secrets
import ssl
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import Config
from .maintenance import NODE_RE, MaintenanceError, SSHMaintenance
from .pve import Auth, PVEClient, PVEError
from .settings import ClusterSettings, load_overrides, validate as validate_settings
from .settings import save as save_settings
from .state import build_state

log = logging.getLogger("haui")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

SID_RE = re.compile(r"^(vm|ct):\d{3,9}$")
GROUP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
HA_STATES = ("started", "stopped", "disabled", "ignored")
SESSION_COOKIE = "haui_session"
SESSION_TTL = 2 * 3600          # PVE tickets live 2h
RENEW_AFTER = 30 * 60           # renew the PVE ticket every 30 min of use
MAX_BODY = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------------ backend
class Backend:
    """Talks to Proxmox (API + SSH for maintenance); its target is changeable at runtime."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.settings_source = "config"
        settings = ClusterSettings.from_config(cfg)
        try:
            saved = load_overrides(cfg.settings_file)
        except (OSError, ValueError) as e:
            log.error("ignoring saved settings in %s: %s", cfg.settings_file, e)
            saved = None
        if saved:
            settings = saved
            self.settings_source = "ui"
        self._apply(settings)
        self.ssh = (SSHMaintenance(cfg.ssh_key, cfg.ssh_user, cfg.ssh_known_hosts)
                    if cfg.maintenance_enabled else None)

    # ------------------------------------------------------------- target
    def _client(self, settings: ClusterSettings) -> PVEClient:
        return PVEClient(settings.hosts, **settings.client_kwargs(self.cfg.ca_file))

    def _apply(self, settings: ClusterSettings) -> None:
        self.settings = settings
        self.pve = self._client(settings) if settings.hosts else None

    @property
    def configured(self) -> bool:
        return self.pve is not None

    def _pve(self) -> PVEClient:
        pve = self.pve
        if pve is None:
            raise PVEError(503, "no Proxmox node configured yet — open Settings")
        return pve

    def probe(self, settings: ClusterSettings) -> list[dict]:
        return self._client(settings).probe()

    def save_settings(self, settings: ClusterSettings) -> None:
        if self.cfg.settings_file:
            save_settings(self.cfg.settings_file, settings)
        self._apply(settings)
        self.settings_source = "ui"

    # ---------------------------------------------------------------- API
    @property
    def maintenance_enabled(self) -> bool:
        return self.ssh is not None

    def login(self, username: str, password: str) -> dict:
        return self._pve().login(username, password)

    def complete_tfa(self, username: str, challenge: str, response: str) -> dict:
        return self._pve().complete_tfa(username, challenge, response)

    def renew(self, auth: Auth) -> Auth:
        return self._pve().renew(auth)

    def api(self, auth: Auth | None, method: str, path: str, params: dict | None = None) -> Any:
        return self._pve().request(method, path, params, auth)

    def maintenance(self, auth: Auth, node: str, enable: bool, node_ips: list[str]) -> None:
        assert self.ssh is not None
        self.ssh.set(node, enable, self.cfg.ssh_hosts or node_ips)


# ------------------------------------------------------------------ sessions
@dataclass
class Session:
    id: str
    auth: Auth | None = None
    tfa: tuple[str, str] | None = None   # (username, challenge ticket)
    can_manage: bool = False   # Sys.Console on /: change HA
    can_admin: bool = False    # Sys.Modify on /: change the cluster connection
    created: float = field(default_factory=time.time)
    renewed: float = field(default_factory=time.time)


class Sessions:
    def __init__(self) -> None:
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def new(self) -> Session:
        s = Session(secrets.token_urlsafe(32))
        with self._lock:
            self._gc()
            self._items[s.id] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            self._gc()
            return self._items.get(sid)

    def drop_all(self, keep: str | None = None) -> None:
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if k == keep}

    def drop(self, sid: str | None) -> None:
        with self._lock:
            self._items.pop(sid or "", None)

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, s in self._items.items() if now - s.renewed > SESSION_TTL]:
            del self._items[k]


class RateLimit:
    """At most ``limit`` failed logins per client IP per ``window`` seconds."""

    def __init__(self, limit: int = 8, window: float = 300):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            now = time.time()
            hits = [t for t in self._fails.get(ip, []) if now - t < self.window]
            self._fails[ip] = hits
            return len(hits) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())


# ------------------------------------------------------------------- the app
class App:
    def __init__(self, backend: Backend, secure_cookies: bool = False):
        self.backend = backend
        self.sessions = Sessions()
        self.ratelimit = RateLimit()
        self.secure_cookies = secure_cookies
        self.routes: list[tuple[str, re.Pattern, Callable, bool]] = []
        r = self._route
        r("GET", r"/api/session", self.h_session, False)
        r("GET", r"/api/realms", self.h_realms, False)
        r("POST", r"/api/login", self.h_login, False)
        r("POST", r"/api/login/tfa", self.h_login_tfa, False)
        r("POST", r"/api/logout", self.h_logout, False)
        r("GET", r"/api/state", self.h_state, True)
        r("POST", r"/api/ha/resources", self.h_ha_create, True)
        r("PUT", r"/api/ha/resources/(?P<sid>[^/]+)", self.h_ha_update, True)
        r("DELETE", r"/api/ha/resources/(?P<sid>[^/]+)", self.h_ha_delete, True)
        r("POST", r"/api/ha/resources/(?P<sid>[^/]+)/move", self.h_ha_move, True)
        r("POST", r"/api/ha/groups", self.h_group_create, True)
        r("PUT", r"/api/ha/groups/(?P<group>[^/]+)", self.h_group_update, True)
        r("DELETE", r"/api/ha/groups/(?P<group>[^/]+)", self.h_group_delete, True)
        r("POST", r"/api/nodes/(?P<node>[^/]+)/maintenance", self.h_maintenance, True)
        # Settings check their own access: open in setup mode, admins otherwise.
        r("GET", r"/api/settings", self.h_settings_get, False)
        r("POST", r"/api/settings/test", self.h_settings_test, False)
        r("PUT", r"/api/settings", self.h_settings_save, False)

    def _route(self, method: str, pattern: str, fn: Callable, auth: bool) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn, auth))

    # ----------------------------------------------------------- helpers
    def _api(self, s: Session, method: str, path: str, params: dict | None = None) -> Any:
        if time.time() - s.renewed > RENEW_AFTER:
            try:
                s.auth = self.backend.renew(s.auth)
                s.renewed = time.time()
            except PVEError as e:
                if e.status == 401:
                    raise ApiError(401, "session expired, please log in again") from e
                raise
        return self.backend.api(s.auth, method, path, params)

    @staticmethod
    def _require_manage(s: Session) -> None:
        if not s.can_manage:
            raise ApiError(403, "your Proxmox user lacks Sys.Console on / (needed to change HA)")

    @staticmethod
    def _sid(sid: str) -> str:
        if not SID_RE.match(sid):
            raise ApiError(400, f"invalid resource id {sid!r} (expected vm:100 or ct:100)")
        return sid

    @staticmethod
    def _group(name: str) -> str:
        if not GROUP_RE.match(name or ""):
            raise ApiError(400, "group names start with a letter and use letters, digits, - or _")
        return name

    @staticmethod
    def _state(value: Any) -> str:
        if value not in HA_STATES:
            raise ApiError(400, f"state must be one of {', '.join(HA_STATES)}")
        return value

    @staticmethod
    def _int(value: Any, name: str, lo: int = 0, hi: int = 10) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ApiError(400, f"{name} must be a number") from None
        if not lo <= v <= hi:
            raise ApiError(400, f"{name} must be between {lo} and {hi}")
        return v

    def _start_session(self, s: Session, data: dict) -> dict:
        s.auth = Auth(data["ticket"], data["CSRFPreventionToken"], data["username"])
        s.tfa = None
        s.renewed = time.time()
        try:
            perms = (self.backend.api(s.auth, "GET", "/access/permissions", {"path": "/"}) or {}).get("/") or {}
        except PVEError:
            perms = {}
        s.can_manage = bool(perms.get("Sys.Console"))
        s.can_admin = bool(perms.get("Sys.Modify"))
        return self._me(s)

    @staticmethod
    def _me(s: Session) -> dict:
        return {"user": s.auth.username, "can_manage": s.can_manage, "can_admin": s.can_admin}

    # ---------------------------------------------------------- handlers
    def h_session(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        if not s or not s.auth:
            raise ApiError(401, "not logged in")
        return self._me(s)

    def h_realms(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        if not self.backend.configured:
            return {"realms": [], "setup": True}
        try:
            realms = self.backend.api(None, "GET", "/access/domains") or []
        except PVEError:
            realms = [{"realm": "pam", "comment": "Linux PAM"}, {"realm": "pve", "comment": "Proxmox VE"}]
        return {"realms": [{"realm": r["realm"], "comment": r.get("comment") or r["realm"]} for r in realms],
                "setup": False}

    def h_login(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        ip = req.client_address[0]
        if self.ratelimit.blocked(ip):
            raise ApiError(429, "too many failed logins — wait a few minutes")
        user = str(body.get("username", "")).strip()
        realm = str(body.get("realm", "pam")).strip() or "pam"
        password = str(body.get("password", ""))
        if not user or not password:
            raise ApiError(400, "username and password are required")
        if "@" not in user:
            user = f"{user}@{realm}"
        try:
            data = self.backend.login(user, password)
        except PVEError as e:
            if e.status == 401:
                self.ratelimit.fail(ip)
                raise ApiError(401, "wrong username or password") from e
            if e.status == 503:
                raise ApiError(503, f"can't reach the Proxmox cluster: {e.message}. An administrator can "
                                    "change the address in /var/lib/haui/settings.json or /etc/haui/haui.toml.") from e
            raise
        if req.session_id:
            self.sessions.drop(req.session_id)
        s = self.sessions.new()
        req.set_cookie = s.id
        if data.get("NeedTFA"):
            s.tfa = (data["username"], data["ticket"])
            return {"need_tfa": True}
        return self._start_session(s, data)

    def h_login_tfa(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        ip = req.client_address[0]
        if not s or not s.tfa:
            raise ApiError(401, "log in with your password first")
        if self.ratelimit.blocked(ip):
            raise ApiError(429, "too many failed logins — wait a few minutes")
        code = re.sub(r"\s+", "", str(body.get("code", "")))
        kind = "recovery" if body.get("recovery") else "totp"
        if not code:
            raise ApiError(400, "enter your code")
        try:
            data = self.backend.complete_tfa(s.tfa[0], s.tfa[1], f"{kind}:{code}")
        except PVEError as e:
            if e.status == 401:
                self.ratelimit.fail(ip)
                raise ApiError(401, "that code was not accepted") from e
            raise
        return self._start_session(s, data)

    def h_logout(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        self.sessions.drop(req.session_id)
        req.set_cookie = ""
        return {"ok": True}

    def h_state(self, req: "Handler", s: Session, body: dict, **_: str) -> Any:
        state = build_state(lambda m, p, q=None: self._api(s, m, p, q))
        state["me"] = self._me(s)
        state["features"] = {"maintenance": self.backend.maintenance_enabled}
        state["version"] = __version__
        state["time"] = int(time.time())
        return state

    def h_ha_create(self, req: "Handler", s: Session, body: dict, **_: str) -> Any:
        self._require_manage(s)
        params = {"sid": self._sid(str(body.get("sid", ""))), "state": self._state(body.get("state", "started"))}
        if body.get("group"):
            params["group"] = self._group(str(body["group"]))
        for key in ("max_restart", "max_relocate"):
            if key in body:
                params[key] = self._int(body[key], key)
        if body.get("comment"):
            params["comment"] = str(body["comment"])[:200]
        return self._api(s, "POST", "/cluster/ha/resources", params)

    def h_ha_update(self, req: "Handler", s: Session, body: dict, sid: str) -> Any:
        self._require_manage(s)
        sid = self._sid(sid)
        params: dict[str, Any] = {}
        delete = []
        if "state" in body:
            params["state"] = self._state(body["state"])
        if "group" in body:
            if body["group"]:
                params["group"] = self._group(str(body["group"]))
            else:
                delete.append("group")
        if "comment" in body:
            if body["comment"]:
                params["comment"] = str(body["comment"])[:200]
            else:
                delete.append("comment")
        for key in ("max_restart", "max_relocate"):
            if key in body:
                params[key] = self._int(body[key], key)
        if delete:
            params["delete"] = ",".join(delete)
        if not params:
            raise ApiError(400, "nothing to change")
        return self._api(s, "PUT", f"/cluster/ha/resources/{sid}", params)

    def h_ha_delete(self, req: "Handler", s: Session, body: dict, sid: str) -> Any:
        self._require_manage(s)
        return self._api(s, "DELETE", f"/cluster/ha/resources/{self._sid(sid)}")

    def h_ha_move(self, req: "Handler", s: Session, body: dict, sid: str) -> Any:
        self._require_manage(s)
        sid = self._sid(sid)
        node = str(body.get("node", ""))
        mode = body.get("mode", "migrate")
        if not NODE_RE.match(node):
            raise ApiError(400, "pick a target node")
        if mode not in ("migrate", "relocate"):
            raise ApiError(400, "mode must be migrate or relocate")
        return {"upid": self._api(s, "POST", f"/cluster/ha/resources/{sid}/{mode}", {"node": node})}

    def _group_params(self, body: dict) -> dict:
        nodes = body.get("nodes")
        if not isinstance(nodes, dict) or not nodes:
            raise ApiError(400, "pick at least one node")
        parts = []
        for node, prio in nodes.items():
            if not NODE_RE.match(str(node)):
                raise ApiError(400, f"invalid node {node!r}")
            p = self._int(prio or 0, "priority", 0, 1000)
            parts.append(f"{node}:{p}" if p else str(node))
        params: dict[str, Any] = {
            "nodes": ",".join(parts),
            "restricted": int(bool(body.get("restricted"))),
            "nofailback": int(bool(body.get("nofailback"))),
        }
        if body.get("comment"):
            params["comment"] = str(body["comment"])[:200]
        return params

    def h_group_create(self, req: "Handler", s: Session, body: dict, **_: str) -> Any:
        self._require_manage(s)
        params = self._group_params(body)
        params["group"] = self._group(str(body.get("group", "")))
        return self._api(s, "POST", "/cluster/ha/groups", params)

    def h_group_update(self, req: "Handler", s: Session, body: dict, group: str) -> Any:
        self._require_manage(s)
        params = self._group_params(body)
        if not body.get("comment"):
            params["delete"] = "comment"
        return self._api(s, "PUT", f"/cluster/ha/groups/{self._group(group)}", params)

    def h_group_delete(self, req: "Handler", s: Session, body: dict, group: str) -> Any:
        self._require_manage(s)
        return self._api(s, "DELETE", f"/cluster/ha/groups/{self._group(group)}")

    def h_maintenance(self, req: "Handler", s: Session, body: dict, node: str) -> Any:
        self._require_manage(s)
        if not self.backend.maintenance_enabled:
            raise ApiError(501, "maintenance mode is not set up (no SSH key configured — see README)")
        if not isinstance(body.get("enable"), bool):
            raise ApiError(400, "'enable' must be true or false")
        status = self._api(s, "GET", "/cluster/status") or []
        if any(i.get("type") == "cluster" and not i.get("quorate") for i in status):
            raise ApiError(409, "the cluster is not quorate — maintenance changes are not possible")
        members = [i for i in status if i.get("type") == "node"]
        if node not in {m["name"] for m in members}:
            raise ApiError(400, f"{node!r} is not a node of this cluster")
        ips = [m["ip"] for m in members if m.get("online") and m.get("ip")]
        # Prefer running the command on a node other than the one being drained.
        ips.sort(key=lambda ip: any(m["name"] == node and m.get("ip") == ip for m in members))
        try:
            self.backend.maintenance(s.auth, node, body["enable"], ips)
        except MaintenanceError as e:
            raise ApiError(502, f"ha-manager over SSH failed: {e}") from e
        log.info("%s %s maintenance on %s", s.auth.username, "enabled" if body["enable"] else "disabled", node)
        return {"ok": True}


    # ---------------------------------------------------------- settings
    def _settings_access(self, s: Session | None, write: bool) -> None:
        if not self.backend.configured:
            return  # first run: nothing to protect yet, and nobody can log in
        if not s or not s.auth:
            raise ApiError(401, "not logged in")
        if write and not s.can_admin:
            raise ApiError(403, "only Proxmox administrators (Sys.Modify on /) can change the cluster connection")

    @staticmethod
    def _parse_settings(body: dict) -> ClusterSettings:
        try:
            return validate_settings(body)
        except ValueError as e:
            raise ApiError(400, str(e)) from None

    def h_settings_get(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        self._settings_access(s, write=False)
        b = self.backend
        return {
            **b.settings.to_dict(),
            "setup": not b.configured,
            "source": b.settings_source,
            "ca_file": bool(b.cfg.ca_file),
            "can_edit": not b.configured or bool(s and s.can_admin),
        }

    def h_settings_test(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        self._settings_access(s, write=True)
        settings = self._parse_settings(body)
        return {"hosts": settings.hosts, "results": self.backend.probe(settings)}

    def h_settings_save(self, req: "Handler", s: Session | None, body: dict, **_: str) -> Any:
        self._settings_access(s, write=True)
        settings = self._parse_settings(body)
        results = self.backend.probe(settings)
        if not any(r["ok"] for r in results):
            raise ApiError(400, "none of these nodes answered as Proxmox — " + "; ".join(
                f"{r['host']}: {r['error']}" for r in results))
        try:
            self.backend.save_settings(settings)
        except OSError as e:
            raise ApiError(500, f"could not save the settings: {e}") from e
        who = s.auth.username if s and s.auth else "setup"
        log.info("%s set the cluster to %s (tls: %s)", who, ", ".join(settings.hosts), settings.tls)
        # Tickets belong to a cluster: sign everyone else out, and keep the
        # current session only if its ticket still works against the new target.
        keep = None
        if s and s.auth:
            try:
                self.backend.api(s.auth, "GET", "/access/permissions", {"path": "/"})
                keep = s.id
            except PVEError:
                pass
        self.sessions.drop_all(keep=keep)
        return {"hosts": settings.hosts, "results": results, "relogin": keep is None}


# ------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "haui/" + __version__
    sys_version = ""
    app: App  # set by make_server

    session_id: str | None = None
    set_cookie: str | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # route to logging
        log.debug("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _cookie(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return None

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            if method == "GET" and path in STATIC_FILES:
                return self._static(*STATIC_FILES[path])
            return self._json(404, {"error": "not found"})

        self.session_id = self._cookie()
        self.set_cookie = None
        try:
            if method != "GET" and self.headers.get("X-Requested-With") != "haui":
                raise ApiError(403, "missing X-Requested-With header")
            for m, rx, fn, needs_auth in self.app.routes:
                mo = rx.match(path)
                if mo and m == method:
                    break
            else:
                raise ApiError(404, "no such endpoint")
            s = self.app.sessions.get(self.session_id)
            if needs_auth and (not s or not s.auth):
                raise ApiError(401, "not logged in")
            data = fn(self, s, self._body(), **mo.groupdict())
            self._json(200, {"data": data})
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except PVEError as e:
            status = 401 if e.status == 401 else (503 if e.status == 503 else 502)
            if e.status == 401:
                self.app.sessions.drop(self.session_id)
            self._json(status, {"error": f"Proxmox: {e.message}"})
        except Exception:  # noqa: BLE001
            log.exception("unhandled error on %s %s", method, path)
            self._json(500, {"error": "internal error — see the haui log"})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "request too large")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError(400, "invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "expected a JSON object")
        return data

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        if self.set_cookie is not None:
            attrs = "Path=/; HttpOnly; SameSite=Strict"
            if self.app.secure_cookies:
                attrs += "; Secure"
            if self.set_cookie:
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}={self.set_cookie}; {attrs}; Max-Age={SESSION_TTL}")
            else:
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; {attrs}; Max-Age=0")
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, name: str, ctype: str) -> None:
        try:
            raw = (STATIC_DIR / name).read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    ssl_ctx: ssl.SSLContext | None = None

    def finish_request(self, request: Any, client_address: Any) -> None:
        # TLS handshake per connection, inside the worker thread, so a slow or
        # broken client can never stall the accept loop.
        if self.ssl_ctx is None:
            return super().finish_request(request, client_address)
        request.settimeout(15)
        try:
            tls = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            tls.settimeout(60)
            super().finish_request(tls, client_address)
        finally:
            tls.close()


def make_server(app: App, addr: tuple[str, int], cert: str | None = None, key: str | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = _Server(addr, handler)
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        httpd.ssl_ctx = ctx
    return httpd
