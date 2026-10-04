import subprocess
import unittest
from unittest import mock

from haui.maintenance import AUTHORIZED_KEYS_COMMAND, MaintenanceError, SSHMaintenance


def done(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.m = SSHMaintenance("/etc/haui/id_ed25519", known_hosts="/var/lib/haui/known_hosts")

    def test_argv(self):
        argv = self.m.argv("10.0.0.1", "pve2", True)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("UserKnownHostsFile=/var/lib/haui/known_hosts", argv)
        self.assertEqual(argv[-2:], ["root@10.0.0.1", "node-maintenance enable pve2"])
        self.assertEqual(self.m.argv("h", "pve2", False)[-1], "node-maintenance disable pve2")

    def test_rejects_bad_node_names(self):
        for bad in ["pve1; reboot", "-oProxyCommand=x", "a b", "", "$(id)"]:
            with self.assertRaises(MaintenanceError):
                self.m.set(bad, True, ["10.0.0.1"])

    @mock.patch("haui.maintenance.subprocess.run")
    def test_tries_next_host_when_ssh_fails(self, run):
        run.side_effect = [done(255, err="ssh: connect: No route to host"), done(0)]
        self.assertEqual(self.m.set("pve2", True, ["10.0.0.1", "10.0.0.2"]), "10.0.0.2")
        self.assertEqual(run.call_count, 2)

    @mock.patch("haui.maintenance.subprocess.run")
    def test_stops_on_ha_manager_error(self, run):
        run.return_value = done(1, err="node 'pve9' not in cluster")
        with self.assertRaises(MaintenanceError) as ctx:
            self.m.set("pve9", True, ["10.0.0.1", "10.0.0.2"])
        self.assertIn("not in cluster", str(ctx.exception))
        self.assertEqual(run.call_count, 1)

    def test_forced_command_only_allows_maintenance(self):
        """Run the authorized_keys command through bash with a stubbed ha-manager."""
        script = AUTHORIZED_KEYS_COMMAND.replace("/usr/sbin/ha-manager", "echo RAN")

        def run(cmd):
            return subprocess.run(["bash", "-c", script], env={"SSH_ORIGINAL_COMMAND": cmd, "PATH": "/usr/bin:/bin"},
                                  capture_output=True, text=True)

        ok = run("node-maintenance enable pve2")
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(ok.stdout.strip(), "RAN crm-command node-maintenance enable pve2")
        for evil in ["ls /", "node-maintenance enable pve2; id", "node-maintenance enable $(id)",
                     "node-maintenance enable *", "migrate vm:100 pve1", ""]:
            r = run(evil)
            self.assertNotEqual(r.returncode, 0, evil)
            self.assertNotIn("RAN", r.stdout, evil)


if __name__ == "__main__":
    unittest.main()
