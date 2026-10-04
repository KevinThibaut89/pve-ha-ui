"""Configuration (TOML). Every key is optional.

With no ``hosts`` the UI starts in setup mode and asks for the cluster address.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older Pythons, dev only
    tomllib = None  # type: ignore[assignment]


@dataclass
class Config:
    hosts: list[str] = field(default_factory=list)
    verify_tls: bool = True
    ca_file: str | None = None
    fingerprints: list[str] = field(default_factory=list)
    listen: str = "0.0.0.0:8443"
    tls_cert: str | None = None
    tls_key: str | None = None
    # Node maintenance (runs `ha-manager crm-command node-maintenance` over SSH).
    ssh_key: str | None = None
    ssh_user: str = "root"
    ssh_known_hosts: str | None = None
    ssh_hosts: list[str] = field(default_factory=list)  # default: node IPs from /cluster/status
    # Where the Settings panel saves the cluster address/TLS choice (overrides the above).
    settings_file: str | None = "/var/lib/haui/settings.json"

    @property
    def listen_addr(self) -> tuple[str, int]:
        host, _, port = self.listen.rpartition(":")
        return (host.strip("[]") or "0.0.0.0", int(port))

    @property
    def maintenance_enabled(self) -> bool:
        return bool(self.ssh_key)


def load(path: str) -> Config:
    if tomllib is None:
        raise SystemExit("reading the config file needs Python 3.11+ (tomllib)")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    maint = raw.pop("maintenance", {}) or {}
    cfg = Config()
    for key, value in raw.items():
        if not hasattr(cfg, key):
            raise SystemExit(f"{path}: unknown setting {key!r}")
        setattr(cfg, key, value)
    for key, value in maint.items():
        attr = key if key.startswith("ssh_") else "ssh_" + key
        if not hasattr(cfg, attr):
            raise SystemExit(f"{path}: unknown [maintenance] setting {key!r}")
        setattr(cfg, attr, value)
    if bool(cfg.tls_cert) != bool(cfg.tls_key):
        raise SystemExit(f"{path}: set both tls_cert and tls_key, or neither")
    return cfg
