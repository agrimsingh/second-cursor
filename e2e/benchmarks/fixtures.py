"""Local benchmark fixtures and independent result predicates. No engine imports."""
import copy
import http.server
import json
import math
import pathlib
import threading
import time


def judge(task, events):
    """Judge actual event payloads, never a page's claimed success or an adapter's answer."""
    kinds = [event.get("kind") for event in events]
    if task == "browser_work_order":
        expected = {"kind": "work_order", "name": "Ada Lovelace", "route": "priority", "confirmed": True}
        return len(events) == 1 and all(events[0].get(k) == v for k, v in expected.items())
    if task == "transient_dialog":
        return kinds == ["dialog_cancelled", "draft_deleted"]
    if task == "canvas_route":
        if kinds != ["canvas_route"]:
            return False
        event = events[0]
        points = event.get("points", [])
        if event.get("trusted") is not True or len(points) < 7:
            return False
        if not all(isinstance(p, dict) and all(isinstance(p.get(k), (int, float))
                   and not isinstance(p.get(k), bool) and math.isfinite(p[k]) for k in ("x", "y")) for p in points):
            return False
        near = lambda p, x: math.hypot(p["x"] - x, p["y"] - 120) <= 30
        def crosses_gate(a, b, x):
            low, high = 0, 1
            for axis, center, radius in (("x", x, 25), ("y", 120, 30)):
                delta = b[axis] - a[axis]
                minimum, maximum = center - radius, center + radius
                if delta == 0:
                    if not minimum <= a[axis] <= maximum:
                        return False
                else:
                    first, second = (minimum - a[axis]) / delta, (maximum - a[axis]) / delta
                    low, high = max(low, min(first, second)), min(high, max(first, second))
                    if low > high:
                        return False
            return True
        # Input may be coalesced: a real pointer segment crossing a gate is evidence even
        # without a sample at the gate center. Match the visible fixture's geometry.
        gates = [any(crosses_gate(a, b, x) for a, b in zip(points, points[1:])) for x in (240, 400)]
        return (near(points[0], 80) and near(points[-1], 560)
                and all(gates)
                and all(0 <= p["x"] <= 640 and 0 <= p["y"] <= 240 for p in points))
    raise ValueError(f"Unknown task: {task}")


class FixtureServer:
    """One trial's event ledger lives in Python, separate from the browser's status text."""
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.httpd = None

    def __enter__(self):
        owner = self
        html = pathlib.Path(__file__).with_name("fixture.html").read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.split("?", 1)[0] not in ("/", "/fixture.html"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if self.path != "/events" or not 0 < size <= 131072:
                        self.send_error(400)
                        return
                    event = json.loads(self.rfile.read(size))
                    if not isinstance(event, dict):
                        raise ValueError("event must be an object")
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                with owner.lock:
                    owner.events.append({**event, "received_monotonic": time.monotonic()})
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/fixture.html"
        return self

    def read(self):
        with self.lock:
            return copy.deepcopy(self.events)

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
