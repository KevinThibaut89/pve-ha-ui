import unittest

from tests.fake_pve import FakeCluster
from haui.state import _lrm_mode, build_state


def state_of(cluster):
    return build_state(lambda m, p, q=None: cluster.request(m, p, q))


class StateTest(unittest.TestCase):
    def test_lrm_mode_parsing(self):
        self.assertEqual(_lrm_mode("pve1 (active, Sat Oct  4 12:00:00 2026)"), "active")
        self.assertEqual(_lrm_mode("pve1 (idle, Sat Oct  4 12:00:00 2026)"), "idle")
        self.assertEqual(_lrm_mode("pve1 (maintenance mode, Sat Oct  4 12:00:00 2026)"), "maintenance")
        self.assertEqual(_lrm_mode("pve1 (old timestamp - dead?, Sat Oct  4 2026)"), "dead")
        self.assertEqual(_lrm_mode(None), "unknown")

    def test_shape(self):
        s = state_of(FakeCluster())
        self.assertEqual(s["cluster"]["name"], "homelab")
        self.assertTrue(s["cluster"]["quorate"])
        self.assertEqual(len(s["nodes"]), 5)
        self.assertEqual(s["summary"]["guests"], 79)  # the template is skipped
        self.assertEqual(s["summary"]["ha_problems"], 1)
        ha = [g for g in s["guests"] if g["ha"]]
        self.assertEqual(len(ha), s["summary"]["ha"])
        ignored = [g for g in ha if g["ha"]["state"] == "ignored"]
        self.assertTrue(ignored and all(g["ha"]["crm_state"] == "ignored" for g in ignored))
        groups = {g["group"]: g for g in s["groups"]}
        self.assertEqual(groups["databases"]["nodes"], {"pve1": 2, "pve2": 1})
        self.assertTrue(groups["media"]["restricted"])

    def test_maintenance_reflected(self):
        c = FakeCluster()
        c.maintenance("pve3", True)
        s = state_of(c)
        pve3 = next(n for n in s["nodes"] if n["name"] == "pve3")
        self.assertTrue(pve3["maintenance"])
        self.assertEqual(s["summary"]["maintenance"], ["pve3"])
        self.assertGreater(s["summary"]["ha_moving"], 0)

    def test_orphans(self):
        c = FakeCluster()
        c.ha["vm:999"] = {"sid": "vm:999", "type": "vm", "state": "started", "group": ""}
        orig = c._guest

        def guest(sid):
            if sid == "vm:999":
                return {"node": "pve1", "vmid": 999, "type": "qemu", "status": "stopped"}
            return orig(sid)
        c._guest = guest
        self.assertIn("vm:999", state_of(c)["orphans"])


if __name__ == "__main__":
    unittest.main()
