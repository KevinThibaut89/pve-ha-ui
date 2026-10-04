"""Build the single ``/api/state`` document the UI renders from.

``api`` is any callable ``api(method, path, params=None)`` returning the PVE
``data`` member (the PVE client, or the fake cluster in the tests).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

Api = Callable[..., Any]

PROBLEM_STATES = {"error", "fence", "recovery", "freeze"}
MOVING_STATES = {"migrate", "relocate", "request_stop", "request_start"}
TASK_TYPES = {"hamigrate", "harelocate", "hastart", "hastop", "qmigrate", "vzmigrate", "haupdate"}

_LRM_RE = re.compile(r"\(([^,]+),")


def _lrm_mode(text: str | None) -> str:
    """'pve1 (maintenance mode, Sat Oct  4 ...)' -> 'maintenance'."""
    m = _LRM_RE.search(text or "")
    if not m:
        return "unknown"
    mode = m.group(1).strip()
    if mode.endswith(" mode"):
        mode = mode[: -len(" mode")]
    if mode.startswith("old timestamp"):
        return "dead"
    return mode


def _guest_sid(res: dict) -> str:
    return ("ct:" if res.get("type") == "lxc" else "vm:") + str(res["vmid"])


def build_state(api: Api) -> dict[str, Any]:
    calls = {
        "status": ("/cluster/status", None),
        "vms": ("/cluster/resources", {"type": "vm"}),
        "nodes": ("/cluster/resources", {"type": "node"}),
        "ha_res": ("/cluster/ha/resources", None),
        "ha_cur": ("/cluster/ha/status/current", None),
        "ha_mgr": ("/cluster/ha/status/manager_status", None),
        "groups": ("/cluster/ha/groups", None),
        "tasks": ("/cluster/tasks", None),
    }
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futs = {k: pool.submit(api, "GET", path, params) for k, (path, params) in calls.items()}
        raw = {}
        for k, fut in futs.items():
            try:
                raw[k] = fut.result()
            except Exception:  # noqa: BLE001 - one failing call must not blank the page
                if k in ("status", "vms"):
                    raise
                raw[k] = None

    # ---------------------------------------------------------------- cluster
    cluster = {"name": None, "quorate": False, "nodes": 0}
    node_info: dict[str, dict] = {}
    for item in raw["status"] or []:
        if item.get("type") == "cluster":
            cluster = {
                "name": item.get("name"),
                "quorate": bool(item.get("quorate")),
                "nodes": item.get("nodes", 0),
            }
        elif item.get("type") == "node":
            node_info[item["name"]] = item
    if not cluster["name"] and len(node_info) == 1:  # standalone node
        cluster.update(quorate=True, nodes=1)

    # --------------------------------------------------------------- HA status
    lrm: dict[str, str] = {}
    services: dict[str, dict] = {}
    manager = {"master": None, "status": "unknown", "quorum": None}
    for item in raw["ha_cur"] or []:
        t = item.get("type")
        if t == "lrm":
            lrm[item["node"]] = _lrm_mode(item.get("status"))
        elif t == "service":
            services[item["sid"]] = item
        elif t == "master":
            manager["master"] = item.get("node")
            manager["status"] = _lrm_mode(item.get("status"))
        elif t == "quorum":
            manager["quorum"] = item.get("status")

    mstat = (raw["ha_mgr"] or {}).get("manager_status") or {}
    crm_node_status: dict[str, str] = mstat.get("node_status") or {}
    service_status: dict[str, dict] = mstat.get("service_status") or {}
    if not manager["master"]:
        manager["master"] = mstat.get("master_node")

    ha_conf = {r["sid"]: r for r in (raw["ha_res"] or [])}

    # ------------------------------------------------------------------ guests
    guests = []
    for res in raw["vms"] or []:
        if res.get("template"):
            continue
        sid = _guest_sid(res)
        conf = ha_conf.get(sid)
        ha = None
        if conf:
            svc = services.get(sid, {})
            crm = svc.get("state") or service_status.get(sid, {}).get("state")
            if not crm:
                # PVE leaves ignored services out of the status entirely.
                crm = "ignored" if conf.get("state") == "ignored" else "unknown"
            ha = {
                "state": conf.get("state", "started"),
                "group": conf.get("group") or "",
                "max_restart": conf.get("max_restart", 1),
                "max_relocate": conf.get("max_relocate", 1),
                "comment": conf.get("comment", ""),
                "crm_state": crm,
                "problem": crm in PROBLEM_STATES,
                "moving": crm in MOVING_STATES,
            }
        guests.append({
            "sid": sid,
            "vmid": res["vmid"],
            "type": "ct" if res.get("type") == "lxc" else "vm",
            "name": res.get("name") or f"{res.get('type')}-{res['vmid']}",
            "node": res.get("node"),
            "status": res.get("status", "unknown"),
            "cpu": res.get("cpu", 0),
            "mem": res.get("mem", 0),
            "maxmem": res.get("maxmem", 0),
            "tags": [t for t in (res.get("tags") or "").split(";") if t],
            "ha": ha,
        })
    guests.sort(key=lambda g: g["vmid"])

    # Resources configured for HA whose guest is gone (deleted VM, stale config).
    known = {g["sid"] for g in guests}
    orphans = sorted(sid for sid in ha_conf if sid not in known)

    # ------------------------------------------------------------------- nodes
    load = {n["node"]: n for n in (raw["nodes"] or [])}
    names = sorted(set(node_info) | set(load))
    nodes = []
    for name in names:
        info, ld = node_info.get(name, {}), load.get(name, {})
        online = bool(info.get("online", ld.get("status") == "online"))
        crm = crm_node_status.get(name)
        lrm_mode = lrm.get(name, "unknown")
        maintenance = crm == "maintenance" or lrm_mode == "maintenance"
        on_node = [g for g in guests if g["node"] == name]
        nodes.append({
            "name": name,
            "ip": info.get("ip"),
            "online": online,
            "lrm": lrm_mode,
            "crm": crm or ("online" if online else "unknown"),
            "maintenance": maintenance,
            "cpu": ld.get("cpu", 0),
            "maxcpu": ld.get("maxcpu", 0),
            "mem": ld.get("mem", 0),
            "maxmem": ld.get("maxmem", 0),
            "uptime": ld.get("uptime", 0),
            "guests": len(on_node),
            "ha_guests": sum(1 for g in on_node if g["ha"]),
            "running": sum(1 for g in on_node if g["status"] == "running"),
        })

    # ------------------------------------------------------------------ groups
    groups = []
    for g in raw["groups"] or []:
        prio = {}
        for part in (g.get("nodes") or "").split(","):
            if not part.strip():
                continue
            node, _, p = part.strip().partition(":")
            prio[node] = int(p) if p.isdigit() else 0
        groups.append({
            "group": g["group"],
            "nodes": prio,
            "restricted": bool(g.get("restricted")),
            "nofailback": bool(g.get("nofailback")),
            "comment": g.get("comment", ""),
            "members": sum(1 for x in guests if x["ha"] and x["ha"]["group"] == g["group"]),
        })
    groups.sort(key=lambda g: g["group"])

    # ------------------------------------------------------------------- tasks
    tasks = [
        {
            "upid": t.get("upid"),
            "node": t.get("node"),
            "type": t.get("type"),
            "id": t.get("id"),
            "user": t.get("user"),
            "starttime": t.get("starttime"),
            "endtime": t.get("endtime"),
            "status": t.get("status") or ("running" if not t.get("endtime") else "unknown"),
        }
        for t in (raw["tasks"] or [])
        if t.get("type") in TASK_TYPES
    ]
    tasks.sort(key=lambda t: t["starttime"] or 0, reverse=True)

    ha = [g for g in guests if g["ha"]]
    return {
        "cluster": cluster,
        "manager": manager,
        "nodes": nodes,
        "guests": guests,
        "groups": groups,
        "tasks": tasks[:50],
        "orphans": orphans,
        "summary": {
            "nodes_online": sum(1 for n in nodes if n["online"]),
            "nodes_total": len(nodes),
            "guests": len(guests),
            "ha": len(ha),
            "ha_problems": sum(1 for g in ha if g["ha"]["problem"]),
            "ha_moving": sum(1 for g in ha if g["ha"]["moving"]),
            "maintenance": [n["name"] for n in nodes if n["maintenance"]],
        },
    }
