"""Checks on the shell installer that can run without a Proxmox node."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from haui.maintenance import AUTHORIZED_KEYS_COMMAND

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = (ROOT / "lxc" / "create-haui-lxc.sh").read_text()


class InstallerTest(unittest.TestCase):
    def test_forced_command_matches_python(self):
        m = re.search(r"^FORCED_CMD='(.*)'$", INSTALLER, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), AUTHORIZED_KEYS_COMMAND)

    @unittest.skipUnless(shutil.which("perl"), "needs perl")
    def test_node_list_parsing(self):
        m = re.search(r"perl -MJSON::PP -e '(.*?)'\)", INSTALLER, re.S)
        self.assertIsNotNone(m)
        status = [
            {"type": "cluster", "name": "homelab", "quorate": 1},
            {"type": "node", "name": "pve1", "ip": "192.168.2.51", "online": 1},
            {"type": "node", "name": "pve2", "ip": "192.168.2.52", "online": 0},
            {"type": "node", "name": "pve3"},
        ]
        out = subprocess.run(["perl", "-MJSON::PP", "-e", m.group(1)], input=json.dumps(status),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.splitlines(), ["pve1 192.168.2.51", "pve2 192.168.2.52"])

    def test_scripts_parse(self):
        for script in ("create-haui-lxc.sh", "haui-update"):
            subprocess.run(["bash", "-n", str(ROOT / "lxc" / script)], check=True)

    def test_no_python_on_the_host(self):
        # PVE nodes are not guaranteed to have python3; the installer must not need it.
        host_side = re.sub(r'pct exec "\$CTID" -- bash -c "[^"]*(?:\\.[^"]*)*"', "", INSTALLER)
        self.assertNotIn("python3 -c", host_side)


if __name__ == "__main__":
    unittest.main()
