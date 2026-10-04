import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from haui.config import Config
from haui.pve import PVEClient
from haui.server import App, Backend, make_server
from haui.settings import ClusterSettings, format_fingerprint, normalize_host, validate


class ValidateTest(unittest.TestCase):
    def test_normalize_host(self):
        self.assertEqual(normalize_host(" 192.168.2.57 "), "192.168.2.57")
        self.assertEqual(normalize_host("https://pve1:8006/"), "pve1")
        self.assertEqual(normalize_host("pve1:9000"), "pve1:9000")
        self.assertEqual(normalize_host("fe80::1"), "[fe80::1]")
        self.assertEqual(normalize_host("http://127.0.0.1:5000"), "http://127.0.0.1:5000")
        for bad in ["", "a b", "x/y", "root@pve1", "https://pve1/api", "pve1:99999", "pve1?x=1"]:
            with self.assertRaises(ValueError, msg=bad):
                normalize_host(bad)

    def test_fingerprint(self):
        hexstr = "ab" * 32
        self.assertEqual(format_fingerprint(hexstr), ":".join(["AB"] * 32))
        self.assertEqual(format_fingerprint(":".join(["ab"] * 32)), ":".join(["AB"] * 32))
        with self.assertRaises(ValueError):
            format_fingerprint("ab:cd")

    def test_validate(self):
        s = validate({"hosts": ["10.0.0.1", "10.0.0.1:8006", "10.0.0.2"], "tls": "insecure",
                      "fingerprints": ["ab" * 32]})
        self.assertEqual(s.hosts, ["10.0.0.1", "10.0.0.2"])   # de-duplicated
        self.assertEqual(s.fingerprints, [])                   # only kept in pin mode
        with self.assertRaises(ValueError):
            validate({"hosts": ["10.0.0.1"], "tls": "maybe"})
        with self.assertRaises(ValueError):
            validate({"hosts": [f"10.0.0.{i}" for i in range(20)]})

    def test_from_config(self):
        self.assertEqual(ClusterSettings.from_config(Config(hosts=["a"], verify_tls=False)).tls, "insecure")
        self.assertEqual(ClusterSettings.from_config(Config(hosts=["a"], fingerprints=["ab" * 32])).tls, "pin")


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
class ProbeTLSTest(unittest.TestCase):
    """Probe a real HTTPS server with a self-signed certificate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cert, key = os.path.join(self.tmp, "c.pem"), os.path.join(self.tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", key, "-out", cert, "-subj", "/CN=localhost"],
                       check=True, capture_output=True)
        app = App(Backend(Config(hosts=[], settings_file=None)))
        self.httpd = make_server(app, ("127.0.0.1", 0), cert, key)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def test_untrusted_then_pinned(self):
        (res,) = PVEClient([self.host], timeout=3).probe()
        self.assertFalse(res["ok"])
        self.assertTrue(res["untrusted"])
        self.assertRegex(res["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        # Pinning that fingerprint gets past TLS (this server is not PVE, so it still fails, differently).
        (pinned,) = PVEClient([self.host], fingerprints=[res["fingerprint"]], timeout=3).probe()
        self.assertFalse(pinned["untrusted"])
        self.assertNotIn("certificate", pinned["error"])
        (other,) = PVEClient([self.host], fingerprints=["00" * 32], timeout=3).probe()
        self.assertTrue(other["untrusted"])


if __name__ == "__main__":
    unittest.main()
