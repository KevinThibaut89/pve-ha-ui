import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from haui.config import Config
from haui.server import App, Backend, make_server
from tests.fake_pve import FakeCluster, serve


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def call(self, method, path, body=None, xrw=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if xrw:
            headers["X-Requested-With"] = "haui"
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";", 1)[0]
            self.set_cookie = sc
        conn.close()
        return resp.status, json.loads(raw) if raw[:1] == b"{" else raw

    def login(self, user="root", password="demo"):
        return self.call("POST", "/api/login", {"username": user, "password": password, "realm": "pam"})


def start(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


class Harness(unittest.TestCase):
    """haui (real Backend) -> HTTP -> fake PVE cluster."""

    hosts_configured = True

    def setUp(self):
        self.cluster = FakeCluster(speed=50)  # simulated tasks finish in ~60 ms
        self.pve = start(serve(self.cluster))
        self.pve_url = f"http://127.0.0.1:{self.pve.server_address[1]}"
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(hosts=[self.pve_url] if self.hosts_configured else [],
                          settings_file=os.path.join(self.tmp, "settings.json"),
                          ssh_key="/dev/null")
        self.backend = Backend(self.cfg)
        # Maintenance goes over SSH in production; drive the fake directly here.
        self.backend.ssh.set = lambda node, enable, hosts: self.cluster.maintenance(node, enable)
        self.app = App(self.backend)
        self.httpd = start(make_server(self.app, ("127.0.0.1", 0)))
        self.c = Client(self.httpd.server_address[1])

    def tearDown(self):
        for srv in (self.httpd, self.pve):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(self.tmp)


class ServerTest(Harness):

    def first(self, pred):
        _, body = self.c.call("GET", "/api/state")
        return next(g for g in body["data"]["guests"] if pred(g))

    def test_static_and_csp(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.c.port)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"HA Manager", resp.read())
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
        conn.request("GET", "/../server.py")
        r2 = conn.getresponse()
        r2.read()
        self.assertEqual(r2.status, 404)

    def test_requires_login(self):
        self.assertEqual(self.c.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.c.call("GET", "/api/session")[0], 401)

    def test_login_wrong_password(self):
        status, body = self.c.call("POST", "/api/login", {"username": "root", "password": ""})
        self.assertEqual(status, 400)

    def test_login_and_state(self):
        status, body = self.c.login()
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"user": "root@pam", "can_manage": True, "can_admin": True})
        self.assertIn("HttpOnly", self.c.set_cookie)
        self.assertIn("SameSite=Strict", self.c.set_cookie)
        status, body = self.c.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["me"]["user"], "root@pam")
        self.assertTrue(body["data"]["features"]["maintenance"])

    def test_writes_need_xrw_header(self):
        self.c.login()
        g = self.first(lambda g: not g["ha"])
        status, _ = self.c.call("POST", "/api/ha/resources", {"sid": g["sid"]}, xrw=False)
        self.assertEqual(status, 403)

    def test_tfa_flow(self):
        status, body = self.c.login(user="tfa-user")
        self.assertEqual(body["data"], {"need_tfa": True})
        self.assertEqual(self.c.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.c.call("POST", "/api/login/tfa", {"code": "000000"})[0], 401)
        status, body = self.c.call("POST", "/api/login/tfa", {"code": "123 456"})
        self.assertEqual(status, 200)
        self.assertEqual(self.c.call("GET", "/api/state")[0], 200)

    def test_read_only_user(self):
        _, body = self.c.login(user="viewer")
        self.assertFalse(body["data"]["can_manage"])
        g = self.first(lambda g: not g["ha"])
        status, body = self.c.call("POST", "/api/ha/resources", {"sid": g["sid"]})
        self.assertEqual(status, 403)
        self.assertIn("Sys.Console", body["error"])

    def test_add_update_remove(self):
        self.c.login()
        g = self.first(lambda g: not g["ha"] and g["status"] == "stopped")
        self.assertEqual(self.c.call("POST", "/api/ha/resources", {"sid": g["sid"], "state": "stopped", "group": "core"})[0], 200)
        g = self.first(lambda x: x["sid"] == g["sid"])
        self.assertEqual((g["ha"]["state"], g["ha"]["group"]), ("stopped", "core"))
        self.assertEqual(self.c.call("PUT", f"/api/ha/resources/{g['sid']}", {"group": ""})[0], 200)
        self.assertEqual(self.first(lambda x: x["sid"] == g["sid"])["ha"]["group"], "")
        self.assertEqual(self.c.call("DELETE", f"/api/ha/resources/{g['sid']}")[0], 200)
        self.assertIsNone(self.first(lambda x: x["sid"] == g["sid"])["ha"])

    def test_validation(self):
        self.c.login()
        for body in [{"sid": "vm:1; rm"}, {"sid": "../x"}, {"sid": "vm:100", "state": "on"},
                     {"sid": "vm:100", "group": "bad group"}, {"sid": "vm:100", "max_restart": 99}]:
            self.assertEqual(self.c.call("POST", "/api/ha/resources", body)[0], 400, body)
        self.assertEqual(self.c.call("PUT", "/api/ha/resources/vm:100", {})[0], 400)

    def test_move(self):
        self.c.login()
        g = self.first(lambda g: g["ha"] and g["ha"]["crm_state"] == "started")
        target = next(n for n in ["pve1", "pve2", "pve3", "pve4", "pve5"] if n != g["node"])
        status, body = self.c.call("POST", f"/api/ha/resources/{g['sid']}/move", {"node": target, "mode": "migrate"})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["upid"].startswith("UPID:"))
        self.assertEqual(self.c.call("POST", f"/api/ha/resources/{g['sid']}/move", {"node": target, "mode": "teleport"})[0], 400)
        time.sleep(0.2)
        self.assertEqual(self.first(lambda x: x["sid"] == g["sid"])["node"], target)

    def test_move_error_from_proxmox(self):
        self.c.login()
        g = self.first(lambda g: g["ha"] and g["ha"]["crm_state"] == "error")
        status, body = self.c.call("POST", f"/api/ha/resources/{g['sid']}/move", {"node": "pve1"})
        self.assertEqual(status, 502)
        self.assertTrue(body["error"].startswith("Proxmox:"))

    def test_maintenance(self):
        self.c.login()
        self.assertEqual(self.c.call("POST", "/api/nodes/pve9/maintenance", {"enable": True})[0], 400)
        self.assertEqual(self.c.call("POST", "/api/nodes/pve2/maintenance", {"enable": "yes"})[0], 400)
        self.assertEqual(self.c.call("POST", "/api/nodes/pve2/maintenance", {"enable": True})[0], 200)
        time.sleep(0.2)
        _, body = self.c.call("GET", "/api/state")
        s = body["data"]
        self.assertEqual(s["summary"]["maintenance"], ["pve2"])
        movable = [g for g in s["guests"] if g["ha"] and g["node"] == "pve2"
                   and g["ha"]["state"] != "ignored" and not g["ha"]["problem"]]
        self.assertEqual(movable, [])
        self.assertEqual(self.c.call("POST", "/api/nodes/pve2/maintenance", {"enable": False})[0], 200)

    def test_maintenance_needs_quorum(self):
        self.c.login()
        for n in ("pve1", "pve2", "pve3"):
            self.cluster.nodes[n]["online"] = False
        self.assertEqual(self.c.call("POST", "/api/nodes/pve4/maintenance", {"enable": True})[0], 409)

    def test_groups(self):
        self.c.login()
        body = {"group": "edge", "nodes": {"pve4": 2, "pve5": 0}, "restricted": True, "comment": "x"}
        self.assertEqual(self.c.call("POST", "/api/ha/groups", body)[0], 200)
        self.assertEqual(self.cluster.groups["edge"]["nodes"], "pve4:2,pve5")
        self.assertEqual(self.c.call("PUT", "/api/ha/groups/edge", {"nodes": {"pve1": 1}})[0], 200)
        self.assertEqual(self.cluster.groups["edge"]["nodes"], "pve1:1")
        self.assertEqual(self.cluster.groups["edge"]["comment"], "")
        self.assertEqual(self.c.call("POST", "/api/ha/groups", {"group": "e", "nodes": {}})[0], 400)
        status, body = self.c.call("DELETE", "/api/ha/groups/databases")
        self.assertEqual(status, 502)  # in use
        self.assertIn("is used by", body["error"])
        self.assertEqual(self.c.call("DELETE", "/api/ha/groups/edge")[0], 200)

    def test_logout(self):
        self.c.login()
        self.assertEqual(self.c.call("POST", "/api/logout")[0], 200)
        self.assertEqual(self.c.call("GET", "/api/state")[0], 401)

    def test_login_rate_limit(self):
        app = self.app
        for _ in range(app.ratelimit.limit):
            app.ratelimit.fail("127.0.0.1")
        self.assertEqual(self.c.login()[0], 429)


class SettingsTest(Harness):
    def test_needs_login_once_configured(self):
        self.assertEqual(self.c.call("GET", "/api/settings")[0], 401)
        self.assertEqual(self.c.call("PUT", "/api/settings", {"hosts": [self.pve_url]})[0], 401)

    def test_get(self):
        self.c.login()
        status, body = self.c.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual((d["hosts"], d["tls"], d["setup"], d["source"], d["can_edit"]),
                         ([self.pve_url], "verify", False, "config", True))

    def test_viewer_is_read_only(self):
        self.c.login(user="viewer")
        status, body = self.c.call("GET", "/api/settings")
        self.assertFalse(body["data"]["can_edit"])
        self.assertEqual(self.c.call("PUT", "/api/settings", {"hosts": [self.pve_url]})[0], 403)
        self.assertEqual(self.c.call("POST", "/api/settings/test", {"hosts": [self.pve_url]})[0], 403)

    def test_test_endpoint(self):
        self.c.login()
        dead = "http://127.0.0.1:9"
        status, body = self.c.call("POST", "/api/settings/test", {"hosts": [self.pve_url, dead]})
        self.assertEqual(status, 200)
        ok, bad = body["data"]["results"]
        self.assertTrue(ok["ok"])
        self.assertFalse(bad["ok"])
        self.assertIn("unreachable", bad["error"])

    def test_save_validates_and_requires_a_live_node(self):
        self.c.login()
        self.assertEqual(self.c.call("PUT", "/api/settings", {"hosts": []})[0], 400)
        self.assertEqual(self.c.call("PUT", "/api/settings", {"hosts": ["a b"]})[0], 400)
        self.assertEqual(self.c.call("PUT", "/api/settings", {"hosts": ["10.0.0.1"], "tls": "pin"})[0], 400)
        status, body = self.c.call("PUT", "/api/settings", {"hosts": ["http://127.0.0.1:9"]})
        self.assertEqual(status, 400)
        self.assertIn("none of these nodes", body["error"])
        self.assertFalse(os.path.exists(self.cfg.settings_file))

    def test_save_persists_and_signs_out_others(self):
        self.c.login()
        other = Client(self.c.port)
        other.login()
        hosts = ["http://127.0.0.1:9", self.pve_url]
        status, body = self.c.call("PUT", "/api/settings", {"hosts": hosts, "tls": "verify"})
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["relogin"])           # same cluster: ticket still valid
        self.assertEqual(self.c.call("GET", "/api/state")[0], 200)
        self.assertEqual(other.call("GET", "/api/state")[0], 401)
        with open(self.cfg.settings_file) as f:
            self.assertEqual(json.load(f)["hosts"], hosts)
        # A restart picks the saved target over the config file.
        again = Backend(Config(hosts=["10.9.9.9"], settings_file=self.cfg.settings_file))
        self.assertEqual((again.settings.hosts, again.settings_source), (hosts, "ui"))


class SetupModeTest(Harness):
    hosts_configured = False

    def test_first_run(self):
        status, body = self.c.call("GET", "/api/realms")
        self.assertTrue(body["data"]["setup"])
        self.assertEqual(self.c.login()[0], 503)
        status, body = self.c.call("GET", "/api/settings")      # no login needed yet
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["setup"])
        status, body = self.c.call("PUT", "/api/settings", {"hosts": [self.pve_url]})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["relogin"])
        self.assertFalse(self.c.call("GET", "/api/realms")[1]["data"]["setup"])
        # Configured now: settings are admin-only again.
        self.assertEqual(self.c.call("PUT", "/api/settings", {"hosts": [self.pve_url]})[0], 401)
        self.assertEqual(self.c.login()[0], 200)
        self.assertEqual(self.c.call("GET", "/api/state")[0], 200)


if __name__ == "__main__":
    unittest.main()
