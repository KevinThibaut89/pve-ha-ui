"""``python -m haui`` — run the HA UI server."""

from __future__ import annotations

import argparse
import logging

from . import __version__
from .config import load
from .server import App, Backend, make_server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="haui", description="Simple web UI for Proxmox VE High Availability")
    ap.add_argument("--config", default="/etc/haui/haui.toml", help="config file (default: %(default)s)")
    ap.add_argument("--listen", help="address:port to listen on (overrides config)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load(args.config)
    if args.listen:
        cfg.listen = args.listen
    backend = Backend(cfg)

    tls = bool(cfg.tls_cert and cfg.tls_key)
    httpd = make_server(App(backend, secure_cookies=tls), cfg.listen_addr, cfg.tls_cert, cfg.tls_key)
    host, port = cfg.listen_addr
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    logging.info("HA UI %s listening on %s://%s:%d", __version__, "https" if tls else "http", shown, port)
    if backend.configured:
        logging.info("cluster: %s (tls: %s, from %s)", ", ".join(backend.settings.hosts),
                     backend.settings.tls, backend.settings_source)
    else:
        logging.warning("no Proxmox node configured — open the UI to enter the cluster address")
    if not tls:
        logging.warning("serving plain HTTP — set tls_cert/tls_key so Proxmox passwords are not sent in clear text")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
