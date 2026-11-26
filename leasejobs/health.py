"""A tiny health + metrics HTTP server for the non-API processes."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


def serve_health(port: int, info: Callable[[], dict], ready: Callable[[], bool] = lambda: True,
                 extra_post: Callable | None = None) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj, ctype="application/json"):
            data = obj if isinstance(obj, bytes) else json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self.send(200, {"status": "alive", **info()})
            if self.path == "/readyz":
                ok = ready()
                return self.send(200 if ok else 503, {"status": "ready" if ok else "not ready", **info()})
            if self.path == "/metrics":
                return self.send(200, generate_latest(), CONTENT_TYPE_LATEST)
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            if extra_post and extra_post(self):
                return
            return self.send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="health", daemon=True).start()
    return srv
