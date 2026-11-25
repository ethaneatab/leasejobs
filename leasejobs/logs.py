"""Structured logs: one JSON object per line on stderr, with fixed keys first."""
from __future__ import annotations

import json
import os
import sys
import time


class JsonLog:
    def __init__(self, component: str, **static):
        self.component = component
        self.static = static

    def bind(self, **more) -> "JsonLog":
        return JsonLog(self.component, **{**self.static, **more})

    def _emit(self, level: str, msg: str, fields: dict) -> None:
        rec = {"ts": round(time.time(), 3), "level": level, "component": self.component,
               "pid": os.getpid(), "msg": msg, **self.static, **fields}
        sys.stderr.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
        sys.stderr.flush()

    def info(self, msg: str, **fields) -> None:
        self._emit("info", msg, fields)

    def warn(self, msg: str, **fields) -> None:
        self._emit("warn", msg, fields)

    def error(self, msg: str, **fields) -> None:
        self._emit("error", msg, fields)
