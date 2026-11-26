"""A fake external integration ("ledger-api") that enforces fencing tokens.

`python -m leasejobs.integration --port 8915`

POST /<integration>/ledger/<key>   body: JSON, header X-Fencing-Token: <n>
  - a token lower than the highest one seen for <key> is refused with 409;
  - no header at all is accepted (that's what a client without fencing does).
GET  /<integration>/ledger/<key>   the accepted writes, the refused ones, the current value
POST /admin/mode                   {"mode": "ok|fail500|fail400|slow", "count": n, "delay_ms": ms}
POST /admin/reset                  forget everything
GET  /healthz, /metrics

State is in memory on purpose: this stands in for someone else's system.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest

REG = CollectorRegistry()
REQS = Counter("leasejobs_integration_requests_total", "Requests to the fake integration",
               ["integration", "outcome"], registry=REG)


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.keys: dict[str, dict] = {}
        self.mode = {"mode": "ok", "count": 0, "delay_ms": 0}

    def reset(self):
        with self.lock:
            self.keys.clear()
            self.mode = {"mode": "ok", "count": 0, "delay_ms": 0}

    def _key(self, key: str) -> dict:
        return self.keys.setdefault(key, {"max_token": None, "accepted": [], "rejected": [], "current": None})

    # @learn:integration-fence
    def write(self, key: str, token: int | None, body: dict, integration: str = "") -> tuple[int, dict]:
        with self.lock:
            k = self._key(key)
            entry = {"token": token, "at": round(time.time(), 3), "integration": integration, **body}
            if token is not None and k["max_token"] is not None and token < k["max_token"]:
                k["rejected"].append(entry)
                return 409, {"error": "stale fencing token", "got": token, "max_seen": k["max_token"]}
            if token is not None:
                k["max_token"] = token if k["max_token"] is None else max(k["max_token"], token)
            k["accepted"].append(entry)
            k["current"] = entry
            return 200, {"ok": True, "key": key, "token": token, "max_seen": k["max_token"]}
    # @learn:end integration-fence

    def take_fault(self) -> dict | None:
        with self.lock:
            m = self.mode
            if m["mode"] == "ok" or m["count"] == 0:
                return None
            if m["count"] > 0:
                m["count"] -= 1
            return dict(m)


LEDGER = Ledger()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep stderr for real problems
        pass

    def _send(self, code: int, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if self.path == "/healthz":
            return self._send(200, {"status": "alive"})
        if self.path == "/metrics":
            return self._send(200, generate_latest(REG), CONTENT_TYPE_LATEST)
        if len(parts) == 3 and parts[1] == "ledger":
            with LEDGER.lock:
                return self._send(200, LEDGER.keys.get(parts[2], {"max_token": None, "accepted": [],
                                                                   "rejected": [], "current": None}))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if self.path == "/admin/reset":
            LEDGER.reset()
            return self._send(200, {"ok": True})
        if self.path == "/admin/mode":
            b = self._body()
            if b.get("mode") not in ("ok", "fail500", "fail400", "slow"):
                return self._send(400, {"error": "mode must be ok|fail500|fail400|slow"})
            with LEDGER.lock:
                LEDGER.mode = {"mode": b["mode"], "count": int(b.get("count", -1)),
                               "delay_ms": int(b.get("delay_ms", 0))}
            return self._send(200, LEDGER.mode)
        if len(parts) == 3 and parts[1] == "ledger":
            integration, key = parts[0], parts[2]
            body = self._body()
            fault = LEDGER.take_fault()
            if fault and fault["mode"] == "slow":
                time.sleep(fault["delay_ms"] / 1000)
            elif fault and fault["mode"] == "fail500":
                REQS.labels(integration, "injected_500").inc()
                return self._send(503, {"error": "injected upstream failure"})
            elif fault and fault["mode"] == "fail400":
                REQS.labels(integration, "injected_400").inc()
                return self._send(400, {"error": "injected permanent rejection"})
            raw = self.headers.get("x-fencing-token")
            code, resp = LEDGER.write(key, int(raw) if raw is not None else None, body, integration)
            REQS.labels(integration, "accepted" if code == 200 else "stale_token").inc()
            return self._send(code, resp)
        return self._send(404, {"error": "not found"})


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8915)
    args = ap.parse_args()
    srv = serve(args.port)
    print(json.dumps({"msg": "integration listening", "port": args.port}), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
