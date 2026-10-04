"""Node maintenance mode over SSH.

PVE 8 has no API endpoint for ``ha-manager crm-command node-maintenance``, so
we SSH to a node with a key whose ``authorized_keys`` entry is locked to that
one command (see README / ``AUTHORIZED_KEYS_COMMAND``).
"""

from __future__ import annotations

import re
import subprocess

NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")

# The forced command for /etc/pve/priv/authorized_keys. It only lets
# "node-maintenance enable|disable <node>" through, rejecting any character a
# node name cannot contain (so nothing can be globbed or smuggled in).
AUTHORIZED_KEYS_COMMAND = (
    "case $SSH_ORIGINAL_COMMAND in "
    "*[!A-Za-z0-9.\\ -]*) echo denied >&2; exit 1;; "
    "node-maintenance\\ enable\\ *|node-maintenance\\ disable\\ *) "
    "exec /usr/sbin/ha-manager crm-command $SSH_ORIGINAL_COMMAND;; "
    "*) echo denied >&2; exit 1;; esac"
)


class MaintenanceError(Exception):
    pass


class SSHMaintenance:
    def __init__(self, key: str, user: str = "root", known_hosts: str | None = None, timeout: int = 15):
        self.key = key
        self.user = user
        self.known_hosts = known_hosts
        self.timeout = timeout

    def argv(self, host: str, node: str, enable: bool) -> list[str]:
        opts = [
            "-i", self.key,
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        action = "enable" if enable else "disable"
        return ["ssh", *opts, f"{self.user}@{host}", f"node-maintenance {action} {node}"]

    def set(self, node: str, enable: bool, hosts: list[str]) -> str:
        """Run the command on the first host that answers; return its host."""
        if not NODE_RE.match(node):
            raise MaintenanceError(f"invalid node name {node!r}")
        if not hosts:
            raise MaintenanceError("no node reachable over SSH")
        errors = []
        for host in hosts:
            try:
                res = subprocess.run(
                    self.argv(host, node, enable),
                    capture_output=True, text=True, timeout=self.timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                errors.append(f"{host}: {e}")
                continue
            if res.returncode == 0:
                return host
            msg = (res.stderr or res.stdout).strip().splitlines()
            errors.append(f"{host}: {msg[-1] if msg else 'exit ' + str(res.returncode)}")
            # 255 = ssh itself failed (unreachable, auth); anything else came from
            # ha-manager on a reachable node, so trying another node won't help.
            if res.returncode != 255:
                break
        raise MaintenanceError("; ".join(errors))
