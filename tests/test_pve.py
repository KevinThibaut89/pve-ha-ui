import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from haui.pve import Auth, PVEClient, PVEError, parse_host


class FakePVE(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self, status, payload, reason=None):
        raw = json.dumps(payload).encode()
        self.send_response(status, reason)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        FakePVE.seen.append({"method": self.command, "path": self.path, "body": body,
                             "cookie": self.headers.get("Cookie"),
                             "csrf": self.headers.get("CSRFPreventionToken")})
        if self.path.startswith("/api2/json/access/ticket"):
            return self._reply(200, {"data": {"ticket": "PVE:root@pam:X", "CSRFPreventionToken": "tok",
                                              "username": "root@pam"}})
        if self.path.startswith("/api2/json/fail"):
            return self._reply(500, {"data": None, "errors": {"sid": "invalid"}}, "Parameter verification failed")
        return self._reply(200, {"data": {"ok": True}})

    do_GET = do_POST = do_PUT = do_DELETE = _handle


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PVEClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakePVE)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakePVE.seen.clear()
        self.url = f"http://127.0.0.1:{self.port}"

    def test_parse_host(self):
        h = parse_host("pve1")
        self.assertEqual((h.scheme, h.host, h.port), ("https", "pve1", 8006))
        h = parse_host("http://10.0.0.5:9000")
        self.assertEqual((h.scheme, h.host, h.port), ("http", "10.0.0.5", 9000))

    def test_failover_to_next_host(self):
        dead = f"http://127.0.0.1:{free_port()}"
        c = PVEClient([dead, self.url], timeout=2)
        self.assertEqual(c.request("GET", "/version"), {"ok": True})
        # The working host becomes preferred: the dead one is not tried again.
        FakePVE.seen.clear()
        c.request("GET", "/version")
        self.assertEqual(c._preferred, 1)

    def test_all_hosts_down(self):
        c = PVEClient([f"http://127.0.0.1:{free_port()}"], timeout=1)
        with self.assertRaises(PVEError) as ctx:
            c.request("GET", "/version")
        self.assertEqual(ctx.exception.status, 503)

    def test_auth_cookie_and_csrf(self):
        c = PVEClient([self.url])
        auth = Auth("PVE:root@pam:X", "tok", "root@pam")
        c.request("GET", "/cluster/status", auth=auth)
        c.request("PUT", "/cluster/ha/resources/vm:100", {"state": "stopped"}, auth=auth)
        get, put = FakePVE.seen
        self.assertEqual(get["cookie"], "PVEAuthCookie=PVE:root@pam:X")
        self.assertIsNone(get["csrf"])
        self.assertEqual(put["csrf"], "tok")
        self.assertEqual(put["body"], "state=stopped")

    def test_get_params_in_query(self):
        PVEClient([self.url]).request("GET", "/cluster/resources", {"type": "vm", "x": None})
        self.assertEqual(FakePVE.seen[0]["path"], "/api2/json/cluster/resources?type=vm")

    def test_error_message(self):
        with self.assertRaises(PVEError) as ctx:
            PVEClient([self.url]).request("POST", "/fail")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("sid: invalid", ctx.exception.message)

    def test_login(self):
        data = PVEClient([self.url]).login("root@pam", "secret")
        self.assertEqual(data["CSRFPreventionToken"], "tok")
        self.assertIn("password=secret", FakePVE.seen[0]["body"])


if __name__ == "__main__":
    unittest.main()
