"""A Navan-like corporate flight search with server-owned results and an independent answer judge.

The page reaches results only through its own search form: a trip-type menu whose open state lays a transparent
backdrop over the form, a destination autocomplete that needs a picked suggestion, a date picker with Confirm, and a
search that answers after a spinner. Fares come from the server per query, so a wrong trip type, airport or date
yields different flights. The expected answer never reaches the browser.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import pathlib
import random
import re
import threading
import urllib.parse

FLIGHT_TASKS = ("flight_policy_search",)
FIXTURE_VERSION = "flight-search/1"
ORIGIN = "SIN"
TODAY = "2026-10-13"
POLICY_CAP = 420
TARGET = {"trip": "oneway", "from": "SIN", "to": "MAA", "depart": "2026-10-28", "return": None}
AIRPORTS = [
    ("MAA", "Chennai International Airport", "Chennai, India"), ("IXM", "Madurai Airport", "Madurai, India"),
    ("MST", "Maastricht Aachen Airport", "Maastricht, Netherlands"), ("MAD", "Adolfo Suárez Madrid–Barajas", "Madrid, Spain"),
    ("MAN", "Manchester Airport", "Manchester, United Kingdom"), ("BLR", "Kempegowda International Airport", "Bengaluru, India"),
    ("BOM", "Chhatrapati Shivaji Maharaj International Airport", "Mumbai, India"),
    ("DEL", "Indira Gandhi International Airport", "Delhi, India"), ("COK", "Cochin International Airport", "Kochi, India"),
    ("TRZ", "Tiruchirappalli International Airport", "Tiruchirappalli, India"), ("CMB", "Bandaranaike International Airport", "Colombo, Sri Lanka"),
    ("HYD", "Rajiv Gandhi International Airport", "Hyderabad, India"), ("KUL", "Kuala Lumpur International Airport", "Kuala Lumpur, Malaysia"),
]
NONSTOP = [("Singapore Airlines", "SQ"), ("IndiGo", "6E"), ("Air India", "AI"), ("Scoot", "TR")]
CONNECTING = [("Malaysia Airlines", "MH", "KUL"), ("Batik Air", "OD", "KUL"), ("Thai Airways", "TG", "BKK"),
              ("SriLankan Airlines", "UL", "CMB"), ("Emirates", "EK", "DXB"), ("Cathay Pacific", "CX", "HKG"),
              ("IndiGo", "6E", "BLR"), ("Air India", "AI", "DEL"), ("Vistara", "UK", "BOM")]
MAX_BODY = 4096


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def clock(minutes):
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}" + (" +1" if minutes >= 24 * 60 else "")


def flights(seed, query):
    """Every result for one query. The target query has exactly three nonstop flights within the policy cap."""
    rng = random.Random(f"{FIXTURE_VERSION}:{seed}:{canonical(query)}")
    target = query == TARGET
    numbers, rows = set(), []

    def number(code):
        while True:
            value = f"{code} {rng.randint(100, 1999)}"
            if value not in numbers:
                numbers.add(value)
                return value

    nonstop_fares = ([rng.randint(285, POLICY_CAP) for _ in range(3)] + [rng.randint(POLICY_CAP + 15, 690) for _ in range(5)]
                     if target else [rng.randint(260, 720) for _ in range(8)])
    rng.shuffle(nonstop_fares)
    multiplier = 1.8 if query["trip"] == "roundtrip" else 1
    for i, fare in enumerate(nonstop_fares):
        airline, code = NONSTOP[i % len(NONSTOP)]
        depart = rng.randrange(5 * 60, 23 * 60, 5)
        duration = 4 * 60 + rng.choice([0, 5, 10, 15])
        rows.append({"airline": airline, "flight": number(code), "depart": depart, "duration": duration, "stops": 0,
                     "via": [], "fare": round(fare * multiplier)})
    total = 140 + rng.randint(0, 4)
    while len(rows) < total:
        stops = 1 if rng.random() < 0.76 else 2
        legs = rng.sample(CONNECTING, stops)
        depart = rng.randrange(0, 24 * 60, 5)
        duration = rng.randrange(6 * 60, 22 * 60, 5) + (stops - 1) * 180
        fare = rng.randint(240, 980) if stops == 1 else rng.randint(230, 760)
        flight = " / ".join(number(code) for _, code, _ in legs) + " / " + number(legs[-1][1])
        rows.append({"airline": " + ".join(dict.fromkeys(name for name, _, _ in legs)), "flight": flight,
                     "depart": depart, "duration": duration, "stops": stops, "via": [hub for _, _, hub in legs],
                     "fare": round(fare * multiplier)})
    rng.shuffle(rows)
    for row in rows:
        row["arrive"] = row["depart"] + row["duration"] - 150
        row["in_policy"] = row["fare"] <= POLICY_CAP * multiplier
        row["depart_time"], row["arrive_time"] = clock(row["depart"]), clock(row["arrive"])
    return rows


def expected(seed):
    return sorted(row["flight"] for row in flights(seed, TARGET) if row["stops"] == 0 and row["in_policy"])


def answer_flights(text):
    """Flight numbers on the reply's last ANSWER: line, or None when there is no such line."""
    lines = [line for line in (text or "").splitlines() if re.search(r"ANSWER\s*:", line, re.I)]
    if not lines:
        return None
    listed = re.split(r"ANSWER\s*:", lines[-1], flags=re.I)[-1]
    return sorted({f"{a.upper()} {b}" for a, b in re.findall(r"\b([A-Z0-9]{2})\s*-?\s*(\d{2,4})\b", listed, re.I)})


class FlightFixture:
    def __init__(self, seed, artifact_dir):
        if type(seed) is not int or seed < 0:
            raise ValueError("A nonnegative integer seed is required")
        self.seed, self.artifact_dir = seed, pathlib.Path(artifact_dir)
        self.events, self.lock = [], threading.Lock()
        self.httpd = self.thread = self.url = self.origin = None
        self._ledger = self.artifact_dir / "flight-ledger.jsonl"
        self.prompt = (
            "Open the provided URL. In this corporate travel tool, find one-way flights from Singapore (SIN) to "
            "Chennai (MAA) departing Wednesday, October 28, 2026 that are within the company's travel policy, and "
            "report the nonstop ones: for each give the flight number, departure time and fare. Do not select or "
            "hold any fare. End your reply with one line: ANSWER: followed by those flight numbers, comma-separated.")

    def case_metadata(self):
        fingerprint = hashlib.sha256(canonical({"version": FIXTURE_VERSION, "seed": self.seed}).encode()).hexdigest()
        return {"task_id": FLIGHT_TASKS[0], "seed": self.seed, "case_id": fingerprint[:20], "fixture_version": FIXTURE_VERSION}

    def _event(self, kind, **fields):
        with self.lock:
            event = {"seq": len(self.events) + 1, "kind": kind, **fields}
            self.events.append(event)
            with self._ledger.open("a") as stream:
                stream.write(canonical(event) + "\n")
        return event

    def read(self):
        with self.lock:
            return [json.loads(line) for line in self._ledger.read_text().splitlines() if line.strip()]

    def search(self, request):
        keys = {"trip", "from", "to", "depart", "return"}
        if not isinstance(request, dict) or set(request) != keys:
            return 400, {"error": "Choose a trip type, destination and dates."}
        query = {key: request[key] for key in keys}
        valid_date = lambda value: isinstance(value, str) and re.fullmatch(r"2026-1[0-2]-\d{2}", value) and value >= TODAY
        if (query["trip"] not in {"oneway", "roundtrip"} or query["from"] != ORIGIN
                or query["to"] not in {code for code, _, _ in AIRPORTS} or not valid_date(query["depart"])
                or (query["trip"] == "roundtrip") != valid_date(query["return"])
                or (query["trip"] == "oneway" and query["return"] is not None)):
            self._event("search", query=query, outcome="invalid")
            return 400, {"error": "Choose a trip type, destination and dates."}
        rows = flights(self.seed, query)
        self._event("search", query=query, outcome="ok", results=len(rows), target=query == TARGET)
        public = [{k: row[k] for k in ("airline", "flight", "depart_time", "arrive_time", "duration", "stops", "via",
                                       "fare", "in_policy")} for row in rows]
        return 200, {"results": public, "policy_cap": round(POLICY_CAP * (1.8 if query["trip"] == "roundtrip" else 1))}

    def judge(self, events, answer):
        searches = [e for e in events if e.get("kind") == "search" and e.get("outcome") == "ok"]
        listed = answer_flights(answer)
        return {
            "searched_one_way_sin_maa_oct_28": any(e.get("target") is True for e in searches),
            "no_fare_selected": not any(e.get("kind") == "select" for e in events),
            "answer_line_present": listed is not None,
            "answer_is_exactly_the_nonstop_in_policy_flights": listed == expected(self.seed),
        }

    def diagnostics(self, events, answer):
        return {"searches": [e.get("query") for e in events if e.get("kind") == "search"],
                "selections": sum(e.get("kind") == "select" for e in events),
                "answer_flights": answer_flights(answer), "expected_flights": expected(self.seed)}

    def __enter__(self):
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if self._ledger.exists():
            raise ValueError("Flight evidence directory already holds a case; use a fresh trial directory")
        self._ledger.touch()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else canonical(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def same_origin(self, mutation=False):
                if self.headers.get("Host") != urllib.parse.urlsplit(owner.origin).netloc:
                    return False
                return not mutation or self.headers.get("Origin") == owner.origin

            def do_GET(self):
                if not self.same_origin() or urllib.parse.urlsplit(self.path).path != "/fixture.html":
                    return self.reply(404, {"error": "not_found"})
                owner._event("page_load")
                html = pathlib.Path(__file__).with_name("flight.html").read_text()
                airports = canonical([{"code": c, "name": n, "city": city} for c, n, city in AIRPORTS])
                html = html.replace("__AIRPORTS__", airports).replace("__TODAY__", TODAY)
                return self.reply(200, html.encode(), "text/html; charset=utf-8")

            def do_POST(self):
                if not self.same_origin(mutation=True):
                    return self.reply(403, {"error": "same_origin_required"})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BODY:
                        raise ValueError("body size")
                    request = json.loads(self.rfile.read(size))
                except ValueError:
                    return self.reply(400, {"error": "invalid_json"})
                if self.path == "/api/search":
                    return self.reply(*owner.search(request))
                if self.path == "/api/select":
                    owner._event("select", request=request)
                    return self.reply(200, {"ok": True})
                return self.reply(404, {"error": "not_found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = self.origin + "/fixture.html"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=3)
