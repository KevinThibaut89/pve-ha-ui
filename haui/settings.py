"""Cluster connection settings that can be changed from the web UI.

The config file provides the defaults; whatever is saved from the Settings
panel lives in a small JSON file in the service's state directory (the config
file itself is read-only under the systemd sandbox) and takes precedence.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Config

TLS_MODES = ("verify", "pin", "insecure")
MAX_HOSTS = 16
_HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]+$")


@dataclass
class ClusterSettings:
    hosts: list[str] = field(default_factory=list)
    tls: str = "verify"                 # verify | pin | insecure
    fingerprints: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Config) -> "ClusterSettings":
        tls = "pin" if cfg.fingerprints else ("verify" if cfg.verify_tls else "insecure")
        return cls(list(cfg.hosts), tls, [format_fingerprint(f) for f in cfg.fingerprints])

    def client_kwargs(self, ca_file: str | None) -> dict[str, Any]:
        return {
            "verify_tls": self.tls != "insecure",
            "ca_file": ca_file if self.tls == "verify" else None,
            "fingerprints": self.fingerprints if self.tls == "pin" else [],
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def format_fingerprint(fp: str) -> str:
    """Any SHA-256 spelling -> 'AB:CD:…' (what `openssl x509 -fingerprint` prints)."""
    hexstr = re.sub(r"[^0-9A-Fa-f]", "", fp or "").upper()
    if len(hexstr) != 64:
        raise ValueError(f"not a SHA-256 fingerprint: {fp!r}")
    return ":".join(hexstr[i:i + 2] for i in range(0, 64, 2))


def normalize_host(spec: str) -> str:
    """Accept '10.0.0.5', 'pve1:8006' or 'https://pve1:8006'; reject anything else."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty host")
    try:  # bare IPv6 address
        if ipaddress.ip_address(spec).version == 6:
            spec = f"[{spec}]"
    except ValueError:
        pass
    probe = spec if "://" in spec else "https://" + spec
    u = urllib.parse.urlsplit(probe)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password \
            or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError(f"{spec!r} is not a host or IP address")
    if not _HOST_RE.match(u.netloc):
        raise ValueError(f"{spec!r} is not a host or IP address")
    try:
        port = u.port
    except ValueError:
        raise ValueError(f"{spec!r} has an invalid port") from None
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    if u.scheme == "http":  # unusual: keep it fully explicit
        return f"http://{host}:{port or 8006}"
    return host if port in (None, 8006) else f"{host}:{port}"


def validate(body: Any) -> ClusterSettings:
    if not isinstance(body, dict):
        raise ValueError("expected an object")
    raw_hosts = body.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ValueError("add at least one Proxmox node IP or hostname")
    hosts: list[str] = []
    for h in raw_hosts:
        n = normalize_host(str(h))
        if n not in hosts:
            hosts.append(n)
    if len(hosts) > MAX_HOSTS:
        raise ValueError(f"at most {MAX_HOSTS} hosts")
    tls = body.get("tls", "verify")
    if tls not in TLS_MODES:
        raise ValueError(f"tls must be one of {', '.join(TLS_MODES)}")
    fps = [format_fingerprint(str(f)) for f in (body.get("fingerprints") or [])]
    fps = list(dict.fromkeys(fps))
    if tls == "pin" and not fps:
        raise ValueError("pin mode needs at least one certificate fingerprint — use Test, then Trust")
    return ClusterSettings(hosts, tls, fps if tls == "pin" else [])


def load_overrides(path: str | None) -> ClusterSettings | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f))


def save(path: str, settings: ClusterSettings) -> None:
    """Atomic write, readable by the service user only."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".settings.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
