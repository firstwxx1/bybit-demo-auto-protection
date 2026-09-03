#!/usr/bin/env python3
"""Loopback-only n8n boundary for the credential-free risk reporter."""
from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTER = ROOT / "live_reporter.py"
TIMEOUT = float(os.getenv("REPORT_HTTP_TIMEOUT_SECONDS", "120"))


def response_payload(body: dict) -> dict:
    configured_mode = os.getenv("OKX_TRADING_MODE", "").strip().lower()
    if (body.get("paper_only") is not True or body.get("trading_mode") != "demo"
            or configured_mode != "demo"):
        return {"exitCode": 1, "stdout": "", "stderr": "BLOCKED: demo paper-only contract required"}
    # The process decides whether active close is allowed. The request cannot enable it.
    env = os.environ.copy()
    env["OKX_TRADING_MODE"] = "demo"
    env["PROTECTION_EXECUTION_ENABLED"] = "false"
    env["ACTIVE_CLOSE_EXECUTION_ENABLED"] = env.get("ACTIVE_CLOSE_EXECUTION_ENABLED", "false")
    command = ["python3", str(REPORTER)]
    if body.get("request_active_close") is True:
        command.append("--active-close-execution")
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                   capture_output=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"exitCode": 124, "stdout": exc.stdout or "", "stderr": "BLOCKED: reporter timeout"}
    return {"exitCode": completed.returncode, "stdout": completed.stdout,
            "stderr": completed.stderr}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, {"status": "ok", "paper_only": True, "loopback_only": True})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/report":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            payload = response_payload(body)
            self.send_json(200, payload)
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"exitCode": 1, "stdout": "", "stderr": "BLOCKED: invalid JSON"})


if __name__ == "__main__":
    port = int(os.getenv("REPORT_HTTP_PORT", "38635"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
