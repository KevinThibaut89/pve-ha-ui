"""A fake Proxmox VE 8 cluster for the tests (and local UI development).

``FakeCluster`` answers the same API paths, with the same response shapes, as
PVE 8 and simulates the HA stack just enough: migrations take a few seconds,
maintenance mode drains a node and brings guests back afterwards, and a guest
in ``error`` only recovers via ``disabled``. ``serve()`` puts it behind a real
HTTP server speaking ``/api2/json`` with ticket cookies and CSRF tokens, so the
production client is exercised end to end.

    python3 -m tests.fake_pve --port 8006     # then point haui at http://127.0.0.1:8006
"""

from __future__ import annotations

import random
import re
import threading
import time
from typing import Any, Callable

from haui.pve import PVEError

GiB = 1024 ** 3
HA_STATES = {"started", "stopped", "disabled", "ignored"}

NAMES = [
    "dns", "pihole", "traefik", "nginx", "homeassistant", "mqtt", "zigbee2mqtt", "nodered",
    "grafana", "prometheus", "loki", "influxdb", "postgres", "mariadb", "redis", "minio",
    "nextcloud", "immich", "jellyfin", "plex", "sonarr", "radarr", "prowlarr", "qbittorrent",
    "paperless", "vaultwarden", "gitea", "drone", "runner", "k3s-master", "k3s-worker",
    "unifi", "omada", "wireguard", "tailscale", "frigate", "scrypted", "uptime-kuma",
    "authentik", "keycloak", "mail", "backup", "syncthing", "photoprism", "bookstack",
]


class FakeCluster:
    def __init__(self, seed: int = 7, nodes: int = 5, guests: int = 80, speed: float = 1.0):
        rnd = random.Random(seed)
        self._rnd = rnd
        self._lock = threading.RLock()
        self._speed = speed
        self._pending: list[tuple[float, Callable[[], None]]] = []
        self._upid = 0

        self.cluster_name = "homelab"
        self.nodes = {}
        for i in range(1, nodes + 1):
            big = i <= 2
            self.nodes[f"pve{i}"] = {
                "ip": f"192.168.2.{50 + i}",
                "online": True,
                "mode": "online",           # or "maintenance"
                "maxcpu": 32 if big else 16,
                "maxmem": (128 if big else 64) * GiB,
                "uptime": rnd.randint(3, 90) * 86400,
            }
        node_names = list(self.nodes)

        self.groups = {
            "databases": {"nodes": "pve1:2,pve2:1", "restricted": 0, "nofailback": 0, "comment": "Prefer the big boxes"},
            "media": {"nodes": "pve3,pve4,pve5", "restricted": 1, "nofailback": 0, "comment": "GPU nodes only"},
            "core": {"nodes": ",".join(f"{n}:1" for n in node_names), "restricted": 0, "nofailback": 1, "comment": ""},
        }

        self.guests: dict[int, dict] = {}
        self.ha: dict[str, dict] = {}
        self.crm: dict[str, str] = {}
        self.maint_origin: dict[str, str] = {}
        for i in range(guests):
            vmid = 100 + i
            kind = "lxc" if rnd.random() < 0.55 else "qemu"
            base = NAMES[i % len(NAMES)]
            name = base if i < len(NAMES) else f"{base}-{i // len(NAMES) + 1}"
            running = rnd.random() < 0.85
            maxmem = rnd.choice([512, 1024, 2048, 4096, 8192]) * 1024 ** 2
            self.guests[vmid] = {
                "vmid": vmid,
                "type": kind,
                "name": name,
                "node": rnd.choice(node_names),
                "status": "running" if running else "stopped",
                "maxmem": maxmem,
                "mem": int(maxmem * rnd.uniform(0.2, 0.8)) if running else 0,
                "cpu": rnd.uniform(0.0, 0.3) if running else 0.0,
                "tags": rnd.choice(["", "prod", "prod;critical", "lab", "media"]),
                "template": 1 if i == guests - 1 else 0,
            }
            if rnd.random() < 0.55 and i != guests - 1:
                sid = self._guest_sid(self.guests[vmid])
                group = rnd.choice(["", "", "core", "databases", "media"])
                if group == "media":
                    self.guests[vmid]["node"] = rnd.choice(["pve3", "pve4", "pve5"])
                self.ha[sid] = {
                    "sid": sid, "type": sid[:2], "state": "started" if running else "stopped",
                    "group": group, "max_restart": 1, "max_relocate": 1, "comment": "",
                }
                self.crm[sid] = "started" if running else "stopped"

        # A few interesting cases to look at.
        ha_sids = sorted(self.ha)
        if len(ha_sids) > 3:
            self.crm[ha_sids[1]] = "error"
            self.ha[ha_sids[2]]["state"] = "ignored"
            self.crm[ha_sids[2]] = "ignored"
        self.tasks: list[dict] = []

    # ---------------------------------------------------------------- helpers
    def _guest_sid(self, g: dict) -> str:
        return ("ct:" if g["type"] == "lxc" else "vm:") + str(g["vmid"])

    def _guest(self, sid: str) -> dict:
        m = re.fullmatch(r"(vm|ct):(\d+)", sid)
        g = self.guests.get(int(m.group(2))) if m else None
        if not g or self._guest_sid(g) != sid:
            raise PVEError(500, f"no such resource '{sid}'")
        return g

    def _after(self, seconds: float, fn: Callable[[], None]) -> None:
        self._pending.append((time.monotonic() + seconds / self._speed, fn))

    def _tick(self) -> None:
        now = time.monotonic()
        due = sorted((p for p in self._pending if p[0] <= now), key=lambda p: p[0])
        self._pending = [p for p in self._pending if p[0] > now]
        for _, fn in due:
            fn()
        for g in self.guests.values():  # a little life in the graphs
            if g["status"] == "running":
                g["cpu"] = min(1.0, max(0.0, g["cpu"] + self._rnd.uniform(-0.03, 0.03)))

    def _task(self, node: str, kind: str, ident: str, user: str, duration: float) -> str:
        self._upid += 1
        start = int(time.time())
        upid = f"UPID:{node}:{self._upid:08X}:{start:08X}:{kind}:{ident}:{user}:"
        task = {"upid": upid, "node": node, "type": kind, "id": ident, "user": user,
                "starttime": start, "status": None, "endtime": None}
        self.tasks.append(task)

        def done() -> None:
            task["endtime"] = int(time.time())
            task["status"] = "OK"
        self._after(duration, done)
        return upid

    def _usable_nodes(self) -> list[str]:
        return [n for n, v in self.nodes.items() if v["online"] and v["mode"] == "online"]

    def _ha_load(self, node: str) -> int:
        return sum(1 for sid in self.ha if self._guest(sid)["node"] == node)

    def _pick_target(self, sid: str, exclude: str) -> str | None:
        group = self.groups.get(self.ha[sid].get("group") or "")
        usable = [n for n in self._usable_nodes() if n != exclude]
        if group:
            prio = {}
            for part in group["nodes"].split(","):
                n, _, p = part.partition(":")
                prio[n] = int(p or 0)
            members = [n for n in usable if n in prio]
            if members:
                best = max(prio[n] for n in members)
                usable = [n for n in members if prio[n] == best]
            elif group["restricted"]:
                return None
        return min(usable, key=self._ha_load) if usable else None

    def _start_move(self, sid: str, target: str, mode: str, user: str) -> str:
        g = self._guest(sid)
        source = g["node"]
        was_running = g["status"] == "running"
        kind = "hamigrate" if mode == "migrate" else "harelocate"
        self.crm[sid] = mode
        if mode == "relocate" and was_running:
            g["status"] = "stopped"
        upid = self._task(source, kind, str(g["vmid"]), user, 3.0)

        def finish() -> None:
            g["node"] = target
            if was_running:
                g["status"] = "running"
            if self.crm.get(sid) in ("migrate", "relocate"):
                self.crm[sid] = "started" if g["status"] == "running" else "stopped"
        self._after(3.0, finish)
        return upid

    def _apply_state(self, sid: str, state: str, user: str) -> None:
        g = self._guest(sid)
        crm = self.crm.get(sid)
        if crm == "error" and state != "disabled":
            return  # PVE: an errored service only leaves 'error' via 'disabled'
        if state == "ignored":
            self.crm[sid] = "ignored"
            return
        if state in ("stopped", "disabled"):
            if g["status"] == "running":
                self.crm[sid] = "request_stop"
                self._task(g["node"], "hastop", str(g["vmid"]), user, 2.0)

                def stopped() -> None:
                    g["status"], g["mem"], g["cpu"] = "stopped", 0, 0.0
                    self.crm[sid] = state if state == "disabled" else "stopped"
                self._after(2.0, stopped)
            else:
                self.crm[sid] = "disabled" if state == "disabled" else "stopped"
        elif state == "started":
            if g["status"] != "running":
                self.crm[sid] = "request_start"
                self._task(g["node"], "hastart", str(g["vmid"]), user, 2.0)

                def started() -> None:
                    g["status"] = "running"
                    g["mem"] = int(g["maxmem"] * 0.4)
                    g["cpu"] = 0.05
                    if self.crm.get(sid) == "request_start":
                        self.crm[sid] = "started"
                self._after(2.0, started)
            else:
                self.crm[sid] = "started"

    # ------------------------------------------------------- public "API"
    def request(self, method: str, path: str, params: dict | None = None, auth: Any = None) -> Any:
        with self._lock:
            self._tick()
            return self._route(method.upper(), path, dict(params or {}), getattr(auth, "username", "root@pam"))

    def maintenance(self, node: str, enable: bool, user: str = "root@pam") -> None:
        with self._lock:
            self._tick()
            if node not in self.nodes:
                raise PVEError(400, f"unknown node '{node}'")
            n = self.nodes[node]
            if enable:
                if n["mode"] == "maintenance":
                    return
                n["mode"] = "maintenance"
                for sid, conf in self.ha.items():
                    if self._guest(sid)["node"] != node or conf["state"] == "ignored":
                        continue
                    if self.crm.get(sid) == "error":
                        continue
                    target = self._pick_target(sid, exclude=node)
                    if target:
                        self.maint_origin[sid] = node
                        self._start_move(sid, target, "migrate", user)
            else:
                n["mode"] = "online"
                for sid, origin in list(self.maint_origin.items()):
                    if origin != node:
                        continue
                    del self.maint_origin[sid]
                    if sid in self.ha and self._guest(sid)["node"] != node:
                        self._start_move(sid, node, "migrate", user)

    # ------------------------------------------------------------- routing
    def _route(self, m: str, path: str, p: dict, user: str) -> Any:
        if m == "POST" and path == "/access/ticket":
            return self._login(p)
        if m == "GET" and path == "/access/domains":
            return [{"realm": "pam", "type": "pam", "comment": "Linux PAM standard authentication"},
                    {"realm": "pve", "type": "pve", "comment": "Proxmox VE authentication server"}]
        if m == "GET" and path == "/access/permissions":
            privs = {"Sys.Audit": 1, "VM.Audit": 1}
            if not user.startswith("viewer"):
                privs.update({"Sys.Console": 1, "Sys.Modify": 1, "VM.Migrate": 1})
            return {"/": privs}
        if m == "GET" and path == "/version":
            return {"version": "8.4.1", "release": "8.4", "repoid": "demo"}
        if m == "GET" and path == "/cluster/status":
            return self._cluster_status()
        if m == "GET" and path == "/cluster/resources":
            return self._resources(p.get("type"))
        if m == "GET" and path == "/cluster/tasks":
            return [dict(t) for t in self.tasks[-50:]]
        if m == "GET" and path == "/cluster/ha/status/current":
            return self._ha_current()
        if m == "GET" and path == "/cluster/ha/status/manager_status":
            return self._ha_manager_status()

        if path == "/cluster/ha/resources":
            if m == "GET":
                return [dict(v) for v in self.ha.values()]
            if m == "POST":
                return self._ha_create(p, user)
        mo = re.fullmatch(r"/cluster/ha/resources/([^/]+)(?:/(migrate|relocate))?", path)
        if mo:
            sid, action = mo.group(1), mo.group(2)
            if sid not in self.ha:
                raise PVEError(500, f"service '{sid}' does not exist")
            if action and m == "POST":
                return self._ha_move(sid, p.get("node", ""), action, user)
            if not action and m == "GET":
                return dict(self.ha[sid])
            if not action and m == "PUT":
                return self._ha_update(sid, p, user)
            if not action and m == "DELETE":
                del self.ha[sid]
                self.crm.pop(sid, None)
                self.maint_origin.pop(sid, None)
                return None

        if path == "/cluster/ha/groups":
            if m == "GET":
                return [{"group": k, "type": "group", **v} for k, v in self.groups.items()]
            if m == "POST":
                return self._group_save(p.get("group", ""), p, create=True)
        mo = re.fullmatch(r"/cluster/ha/groups/([^/]+)", path)
        if mo:
            name = mo.group(1)
            if name not in self.groups:
                raise PVEError(500, f"no such ha group '{name}'")
            if m == "GET":
                return {"group": name, "type": "group", **self.groups[name]}
            if m == "PUT":
                return self._group_save(name, p, create=False)
            if m == "DELETE":
                used = [s for s, c in self.ha.items() if c.get("group") == name]
                if used:
                    raise PVEError(500, f"ha group '{name}' is used by {', '.join(used)}")
                del self.groups[name]
                return None
        raise PVEError(501, f"Method '{m} {path}' not implemented")

    def _login(self, p: dict) -> dict:
        user = p.get("username", "")
        if "@" not in user or not p.get("password"):
            raise PVEError(401, "authentication failure")
        if p.get("password", "").startswith("PVE:"):  # ticket renewal
            return {"ticket": p["password"], "CSRFPreventionToken": "demo-csrf", "username": user}
        if user.startswith("tfa") and "tfa-challenge" not in p:
            return {"ticket": f"PVE:{user}:!tfa!DEMO", "CSRFPreventionToken": "demo-csrf",
                    "username": user, "NeedTFA": 1}
        if "tfa-challenge" in p and p.get("password") != "totp:123456":
            raise PVEError(401, "authentication failure")
        return {"ticket": f"PVE:{user}:DEMO", "CSRFPreventionToken": "demo-csrf", "username": user}

    def _cluster_status(self) -> list[dict]:
        out = [{"type": "cluster", "id": "cluster", "name": self.cluster_name, "nodes": len(self.nodes),
                "quorate": int(sum(v["online"] for v in self.nodes.values()) * 2 > len(self.nodes)),
                "version": 12}]
        for i, (name, v) in enumerate(self.nodes.items(), 1):
            out.append({"type": "node", "id": f"node/{name}", "name": name, "ip": v["ip"],
                        "online": int(v["online"]), "nodeid": i, "local": int(i == 1), "level": ""})
        return out

    def _resources(self, kind: str | None) -> list[dict]:
        out = []
        if kind in (None, "node"):
            for name, v in self.nodes.items():
                on = [g for g in self.guests.values() if g["node"] == name and g["status"] == "running"]
                cpu = min(0.98, 0.03 + sum(g["cpu"] * 2 for g in on) / v["maxcpu"])
                mem = int(4 * GiB + sum(g["mem"] for g in on))
                out.append({"id": f"node/{name}", "type": "node", "node": name,
                            "status": "online" if v["online"] else "offline",
                            "cpu": cpu if v["online"] else 0, "maxcpu": v["maxcpu"],
                            "mem": mem if v["online"] else 0, "maxmem": v["maxmem"],
                            "uptime": v["uptime"] if v["online"] else 0})
        if kind in (None, "vm"):
            for g in self.guests.values():
                sid = self._guest_sid(g)
                r = {"id": f"{g['type']}/{g['vmid']}", "type": g["type"], "vmid": g["vmid"],
                     "name": g["name"], "node": g["node"], "status": g["status"],
                     "maxmem": g["maxmem"], "mem": g["mem"], "cpu": g["cpu"], "maxcpu": 2,
                     "template": g["template"], "tags": g["tags"]}
                if sid in self.ha:
                    r["hastate"] = self.crm.get(sid, "started")
                out.append(r)
        return out

    def _ha_current(self) -> list[dict]:
        stamp = time.strftime("%a %b %d %H:%M:%S %Y")
        master = next((n for n in self._usable_nodes()), next(iter(self.nodes)))
        out = [{"id": "quorum", "type": "quorum", "node": master, "status": "OK", "quorate": 1},
               {"id": "master", "type": "master", "node": master, "status": f"{master} (active, {stamp})"}]
        for name, v in self.nodes.items():
            hosts_ha = any(self._guest(s)["node"] == name for s in self.ha)
            mode = "maintenance mode" if v["mode"] == "maintenance" else ("active" if hosts_ha else "idle")
            if not v["online"]:
                mode = "old timestamp - dead?"
            out.append({"id": f"lrm:{name}", "type": "lrm", "node": name, "status": f"{name} ({mode}, {stamp})"})
        for sid, conf in sorted(self.ha.items()):
            if conf["state"] == "ignored":
                continue
            g = self._guest(sid)
            st = self.crm.get(sid, "started")
            out.append({"id": f"service:{sid}", "type": "service", "sid": sid, "node": g["node"],
                        "state": st, "crm_state": st, "request_state": conf["state"],
                        "max_restart": conf["max_restart"], "max_relocate": conf["max_relocate"],
                        "status": f"{sid} ({g['node']}, {st})"})
        return out

    def _ha_manager_status(self) -> dict:
        return {
            "manager_status": {
                "master_node": next(iter(self._usable_nodes()), None),
                "node_status": {n: ("maintenance" if v["mode"] == "maintenance" else
                                    ("online" if v["online"] else "unknown"))
                                for n, v in self.nodes.items()},
                "service_status": {s: {"node": self._guest(s)["node"], "state": self.crm.get(s, "started")}
                                   for s in self.ha if self.ha[s]["state"] != "ignored"},
            },
        }

    def _check_common(self, p: dict) -> None:
        if "state" in p and p["state"] not in HA_STATES:
            raise PVEError(400, f"Parameter verification failed. state: value '{p['state']}' does not have a value in the enumeration")
        if p.get("group") and p["group"] not in self.groups:
            raise PVEError(500, f"ha group '{p['group']}' does not exist")

    def _ha_create(self, p: dict, user: str) -> None:
        sid = p.get("sid", "")
        self._guest(sid)
        if sid in self.ha:
            raise PVEError(500, f"resource ID '{sid}' already defined")
        self._check_common(p)
        state = p.get("state", "started")
        self.ha[sid] = {"sid": sid, "type": sid[:2], "state": state, "group": p.get("group", ""),
                        "max_restart": int(p.get("max_restart", 1)),
                        "max_relocate": int(p.get("max_relocate", 1)), "comment": p.get("comment", "")}
        self.crm[sid] = "started" if self._guest(sid)["status"] == "running" else "stopped"
        self._apply_state(sid, state, user)

    def _ha_update(self, sid: str, p: dict, user: str) -> None:
        self._check_common(p)
        conf = self.ha[sid]
        for key in [k for k in (p.pop("delete", "") or "").split(",") if k]:
            conf[key] = "" if key in ("group", "comment") else 1
        for key in ("group", "comment", "max_restart", "max_relocate"):
            if key in p:
                conf[key] = p[key] if key in ("group", "comment") else int(p[key])
        if "state" in p:
            conf["state"] = p["state"]
            self._apply_state(sid, p["state"], user)

    def _ha_move(self, sid: str, node: str, action: str, user: str) -> str:
        if node not in self.nodes:
            raise PVEError(400, f"no such node '{node}'")
        if not self.nodes[node]["online"]:
            raise PVEError(500, f"node '{node}' is not online")
        if self.ha[sid]["state"] in ("disabled", "ignored") or self.crm.get(sid) == "error":
            raise PVEError(500, f"service '{sid}' is in state '{self.crm.get(sid)}' and cannot be moved")
        if self._guest(sid)["node"] == node:
            raise PVEError(500, f"service '{sid}' is already on node '{node}'")
        return self._start_move(sid, node, action, user)

    def _group_save(self, name: str, p: dict, create: bool) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]+", name or ""):
            raise PVEError(400, "Parameter verification failed. group: invalid format")
        if create and name in self.groups:
            raise PVEError(500, f"ha group '{name}' already defined")
        g = self.groups.setdefault(name, {"nodes": "", "restricted": 0, "nofailback": 0, "comment": ""})
        for key in [k for k in (p.pop("delete", "") or "").split(",") if k]:
            g[key] = "" if key == "comment" else 0
        if create and not p.get("nodes"):
            del self.groups[name]
            raise PVEError(400, "Parameter verification failed. nodes: property is missing")
        for key in ("nodes", "comment"):
            if key in p:
                g[key] = p[key]
        for key in ("restricted", "nofailback"):
            if key in p:
                g[key] = int(p[key])


# ------------------------------------------------------------- HTTP wrapper
import json  # noqa: E402
import urllib.parse  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

PUBLIC = {("POST", "/access/ticket"), ("GET", "/access/domains")}


class _Handler(BaseHTTPRequestHandler):
    cluster: FakeCluster

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: dict, reason: str | None = None) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status, reason)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if not url.path.startswith("/api2/json/"):
            return self._send(404, {"data": None})
        path = url.path[len("/api2/json"):]
        params = dict(urllib.parse.parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            params.update(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        method = self.command
        auth = None
        if (method, path) not in PUBLIC:
            cookie = self.headers.get("Cookie") or ""
            ticket = cookie.partition("PVEAuthCookie=")[2].split(";")[0]
            if not ticket.startswith("PVE:") or "!tfa!" in ticket:
                return self._send(401, {"data": None}, "No ticket")
            if method != "GET" and self.headers.get("CSRFPreventionToken") != "demo-csrf":
                return self._send(401, {"data": None}, "Permission check failed (invalid csrf token)")
            auth = type("A", (), {"username": ticket.split(":")[1]})()
        try:
            data = self.cluster.request(method, path, params, auth)
        except PVEError as e:
            return self._send(e.status, {"data": None}, e.message)
        self._send(200, {"data": data})

    do_GET = do_POST = do_PUT = do_DELETE = _handle


def serve(cluster: FakeCluster | None = None, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Return a started-on-demand HTTP server (call serve_forever in a thread)."""
    handler = type("H", (_Handler,), {"cluster": cluster or FakeCluster()})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="fake Proxmox VE 8 API for development")
    ap.add_argument("--port", type=int, default=8006)
    a = ap.parse_args()
    srv = serve(port=a.port)
    print(f"fake PVE API on http://127.0.0.1:{a.port}")
    srv.serve_forever()
