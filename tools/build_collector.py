"""Small WSGI intake service. Run behind HTTPS using gunicorn, never publicly serve DB."""
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

MAX_BYTES = 2_000_000
MAX_ROWS = 200_000
MAX_DB_BYTES = 2_000_000_000
DB_PATH = Path(os.environ.get("PACKSMITH_COLLECTOR_DB", "/var/lib/packsmith-collector/builds.sqlite3"))
_lock = threading.Lock()
_minute = 0
_requests = 0


def validate(sample):
    if not isinstance(sample, dict) or sample.get("schemaVersion") != 1 or sample.get("policyVersion") != 1:
        raise ValueError("schema")
    if set(sample) - set("schemaVersion policyVersion sampleId solveId event createdAt catalogHash solverHash source request solution jobStatus initialLayout application".split()):
        raise ValueError("fields")
    if not re.fullmatch(r"[0-9a-f-]{36}", str(sample.get("sampleId", ""))):
        raise ValueError("sampleId")
    if not re.fullmatch(r"[0-9a-f]{32}", str(sample.get("solveId", ""))):
        raise ValueError("solveId")
    for key in ("catalogHash", "solverHash"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(sample.get(key, ""))):
            raise ValueError(key)
    if sample.get("event") not in ("solve", "apply") or sample.get("source") not in ("game", "manual"):
        raise ValueError("event")
    request = sample.get("request")
    if not isinstance(request, dict) or not isinstance(request.get("grid"), dict):
        raise ValueError("request")
    count = request["grid"].get("cellCount")
    if type(count) is not int or not 1 <= count <= 60:
        raise ValueError("cellCount")
    items = []
    for key in ("artifacts", "tablets", "customTabletTypes"):
        rows = request.get(key, [])
        if not isinstance(rows, list) or len(rows) > 60 or not all(isinstance(row, dict) for row in rows):
            raise ValueError(key)
        if key != "customTabletTypes":
            items.extend(rows)
    if not items or len(items) > count:
        raise ValueError("items")
    ids = [item.get("instanceId") for item in items]
    if any(not isinstance(value, str) or not re.fullmatch(r"i\d{1,2}", value) for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("instanceId")
    if not isinstance(sample.get("solution"), dict) or not isinstance(sample.get("initialLayout"), list):
        raise ValueError("solution")
    # Intake validation only. These records are NOT trusted training labels;
    # replay and independently validate against the archived rules before training.


def application(environ, start_response):
    global _minute, _requests
    def reply(status, payload):
        data = json.dumps(payload, separators=(",", ":")).encode()
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(data))), ("Cache-Control", "no-store")])
        return [data]
    path = environ.get("PATH_INFO")
    if path == "/packsmith/health" and environ.get("REQUEST_METHOD") == "GET":
        return reply("200 OK", {"ok": True})
    if path != "/packsmith/v1/builds" or environ.get("REQUEST_METHOD") != "POST":
        return reply("404 Not Found", {"error": "not_found"})
    with _lock:
        minute = int(time.monotonic() // 60)
        if minute != _minute:
            _minute, _requests = minute, 0
        _requests += 1
        if _requests > 120:
            return reply("429 Too Many Requests", {"error": "rate_limit"})
    try:
        length = int(environ.get("CONTENT_LENGTH", "0"))
    except ValueError:
        length = 0
    if not 0 < length <= MAX_BYTES:
        return reply("413 Content Too Large", {"error": "size"})
    if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
        return reply("415 Unsupported Media Type", {"error": "content_type"})
    try:
        body = environ["wsgi.input"].read(length)
        if len(body) != length:
            raise ValueError("length")
        sample = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        validate(sample)
    except (ValueError, TypeError, RecursionError):
        return reply("400 Bad Request", {"error": "invalid_sample"})
    try:
        with _lock:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(DB_PATH, timeout=10)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS samples (id TEXT PRIMARY KEY, received_at INTEGER NOT NULL, payload TEXT NOT NULL, validation TEXT NOT NULL DEFAULT 'pending')")
                exists = db.execute("SELECT 1 FROM samples WHERE id = ?", (sample["sampleId"],)).fetchone()
                if not exists:
                    if DB_PATH.stat().st_size >= MAX_DB_BYTES or db.execute("SELECT count(*) FROM samples").fetchone()[0] >= MAX_ROWS:
                        return reply("503 Service Unavailable", {"error": "storage_limit"})
                    db.execute("INSERT INTO samples (id, received_at, payload) VALUES (?, ?, ?)",
                               (sample["sampleId"], int(time.time()), body.decode("utf-8")))
        return reply("201 Created", {"sampleId": sample["sampleId"]})
    except (OSError, sqlite3.Error):
        return reply("503 Service Unavailable", {"error": "storage"})
