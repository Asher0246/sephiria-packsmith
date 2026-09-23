"""Opt-in build sharing. No network activity until consent has been saved."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = "https://asher0627.site/packsmith/v1/builds"
POLICY_VERSION = 1
MAX_BYTES = 2_000_000
MAX_PENDING = 50


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def pick(value, fields):
    return {key: value[key] for key in fields.split() if key in value}


def make_sample(job, event="solve", outcome=None):
    """Explicit field selection; never upload raw requests, errors or snapshots."""
    from .result_cache import _catalog_fingerprint, _code_fingerprint
    raw = job.request_payload or {}
    items = raw.get("artifacts", []) + raw.get("tablets", [])
    ids = {item["instanceId"]: f"i{index}" for index, item in enumerate(items)}
    custom = raw.get("customTabletTypes", [])
    custom_ids = {item["id"]: f"custom-tablet-{index}" for index, item in enumerate(custom)}
    def item_row(item):
        row = pick(item, "typeId weight baseLevel minLevel exactLevel fixedCell fixedRotation preferredRotation specialPriority")
        row["instanceId"] = ids.get(item.get("instanceId"))
        row["typeId"] = custom_ids.get(row.get("typeId"), row.get("typeId"))
        if "specialTargetInstanceId" in item:
            row["specialTargetInstanceId"] = ids.get(item["specialTargetInstanceId"])
        return row
    request = {
        "grid": pick(raw.get("grid", {}), "cellCount rows cols doubleLevelCells"),
        "artifacts": [item_row(item) for item in raw.get("artifacts", [])],
        "tablets": [item_row(item) for item in raw.get("tablets", [])],
        "options": pick(raw.get("options", {}), "timeLimitMs workerCount fastMode gpuAcceleration"),
        "customTabletTypes": [dict(
            pick(item, "rotatable custom cellCount candidates queryRotations conditionRotations"),
            id=custom_ids[item["id"]], name="自定义石板",
        ) for item in custom],
    }
    request["grid"]["cellLevelBonuses"] = [
        pick(item, "cell bonus") for item in raw.get("grid", {}).get("cellLevelBonuses", [])
    ]
    result = job.result or {}
    solution = pick(result, "solutionStatus secondaryStatus specialStatus tertiaryStatus emptyCellStatus primaryObjective secondaryObjective specialObjective tertiaryObjective emptyCellObjective primaryBestBound relativeGap cellEffects cellMultipliers disabledCells unlockedCells buildMs solveMs fromCache")
    solution["placements"] = [dict(
        pick(item, "kind cell rotation"), instanceId=ids.get(item.get("instanceId")),
    ) for item in result.get("placements", [])]
    solution["artifacts"] = [dict(
        pick(item, "cell baseLevel rawBonus multiplier disabled level cap active weight contribution"),
        instanceId=ids.get(item.get("instanceId")),
    ) for item in result.get("artifacts", [])]
    solution["specialDetails"] = [dict(
        pick(item, "condition rawScore maxScore completion weight weightedScore satisfied"),
        instanceId=ids.get(item.get("instanceId")),
    ) for item in result.get("specialDetails", [])]
    solution["diagnostics"] = {key: value for key, value in pick(
        result.get("diagnostics", {}),
        "tabletCandidates rawTabletCandidates fixedOccupancyPrunedCandidates levelPrunedArtifactCells artifactPlacementVariables tabletPlacementVariables phase1Variables refinementVariables finalVariables levelTransformGroups initialHintVariables artifacts tablets workerCount gpuRequested gpuUsed gpuMs gpuEvaluations gpuCacheHit replacedCpuResult",
    ).items() if type(value) in (int, float, bool)}
    source = job.game_source or {}
    sample = {
        "schemaVersion": 1, "policyVersion": POLICY_VERSION,
        "sampleId": str(uuid.uuid4()), "solveId": job.id, "event": event,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "catalogHash": _catalog_fingerprint(), "solverHash": _code_fingerprint(),
        "source": "game" if source else "manual",
        "request": request, "solution": solution,
        "jobStatus": "FAILED" if job.error else "FINISHED",
        "initialLayout": [dict(pick(item, "kind cell rotation"),
            instanceId=ids.get(item.get("solverInstanceId"))) for item in source.get("items", [])],
    }
    if outcome is not None:
        sample["application"] = pick(outcome, "ok moves rotations rolledBack")
    return sample


class Sharing:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "sharing.json"
        self.queue = directory / "upload-queue.sqlite3"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        self.generation = 0
        self.last_error = False
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.choice = saved["enabled"] if saved.get("policyVersion") == POLICY_VERSION and type(saved.get("enabled")) is bool else None
        except (OSError, ValueError, KeyError):
            self.choice = None
        if self.choice is not True and self.queue.exists():
            with self.connect() as db:
                db.execute("DELETE FROM pending")

    @contextmanager
    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.queue)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                yield db
        finally:
            db.close()

    def status(self):
        with self.lock:
            pending = 0
            if self.queue.exists():
                with self.connect() as db:
                    pending = db.execute("SELECT count(*) FROM pending").fetchone()[0]
            return {"enabled": self.choice is True, "needsConsent": self.choice is None,
                    "pending": pending, "uploadFailed": self.last_error, "endpoint": ENDPOINT}

    def configure(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"enabled": enabled, "policyVersion": POLICY_VERSION}), encoding="utf-8")
            temporary.replace(self.path)
            self.choice = enabled
            self.generation += 1
            if not enabled and self.queue.exists():
                with self.connect() as db:
                    db.execute("DELETE FROM pending")
            self.last_error = False
        self.wake.set()
        return self.status()

    def ticket(self):
        with self.lock:
            return self.generation if self.choice is True else None

    def enqueue(self, job, ticket, event="solve", outcome=None):
        try:
            with self.lock:
                if ticket is None or ticket != self.generation or self.choice is not True:
                    return
            sample = make_sample(job, event, outcome)
            body = json.dumps(sample, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(body.encode("utf-8")) > MAX_BYTES:
                return
            with self.lock:
                if self.choice is not True or ticket != self.generation:
                    return
                with self.connect() as db:
                    db.execute("INSERT OR IGNORE INTO pending VALUES (?, ?)", (sample["sampleId"], body))
                    db.execute("DELETE FROM pending WHERE rowid NOT IN (SELECT rowid FROM pending ORDER BY rowid DESC LIMIT ?)", (MAX_PENDING,))
            self.wake.set()
        except Exception:
            self.last_error = True  # Sharing must never fail a solve or print sensitive input.

    def send_one(self):
        # Hold consent lock during the bounded request: once disabling returns,
        # no queued or in-flight upload can start from the previous consent.
        with self.lock:
            if self.choice is not True or not self.queue.exists():
                return False
            with self.connect() as db:
                row = db.execute("SELECT id, body FROM pending ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return False
            request = Request(ENDPOINT, data=row[1].encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=5) as response:
                if response.status != 201 or response.url != ENDPOINT:
                    raise ValueError("Unexpected upload response")
                ack = json.loads(response.read(1024))
                if ack.get("sampleId") != row[0]:
                    raise ValueError("Missing upload acknowledgement")
            with self.connect() as db:
                db.execute("DELETE FROM pending WHERE id = ?", (row[0],))
            self.last_error = False
            return True

    def start(self):
        def run():
            delay = 1
            while not self.closed.is_set():
                self.wake.wait(delay)
                self.wake.clear()
                if self.closed.is_set():
                    break
                try:
                    sent = self.send_one()
                    delay = 1 if sent else 60
                except Exception:
                    self.last_error = True
                    delay = min(300, max(30, delay * 2))
        self.thread = threading.Thread(target=run, name="build-upload", daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=6)
