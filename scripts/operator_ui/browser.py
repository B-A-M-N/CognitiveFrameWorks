"""Minimal localhost-only browser cockpit; no external assets or network access."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PAGE = """<!doctype html><meta charset=utf-8><title>Cognitive Cockpit</title>
<style>body{font:16px system-ui;background:#10131a;color:#e8edf5;max-width:1000px;margin:2rem auto}section{background:#1a202b;padding:1rem;border-radius:.5rem;margin:1rem 0}</style>
<h1>Cognitive Cockpit</h1><p id=health>Connecting…</p><section id=tasks></section><section id=profiles></section>
<script>async function tick(){let s=await (await fetch('/api/snapshot')).json();health.textContent=`Connected · events through ${s.health.operator_event_high_watermark}`;tasks.innerHTML='<h2>Agent activity</h2>'+s.tasks.map(t=>`<p><b>${t.status}</b> ${t.task_id} · ${t.provenance}</p>`).join('');profiles.innerHTML='<h2>Shared learning</h2>'+s.profiles.map(p=>`<p>${p.status} · ${p.profile_id}</p>`).join('')}tick();setInterval(tick,2000)</script>"""


def make_handler(service: Any):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode(); content_type = "text/html; charset=utf-8"
            elif self.path == "/api/snapshot":
                body = json.dumps(service.snapshot(), sort_keys=True).encode(); content_type = "application/json"
            else:
                self.send_error(404); return
            self.send_response(200); self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    return Handler


def run(service: Any, *, host: str = "127.0.0.1", port: int = 8765) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("operator browser binds to localhost only")
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"Cognitive Cockpit: http://{host}:{port}/ (q to stop)")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); service.close()
    return 0
