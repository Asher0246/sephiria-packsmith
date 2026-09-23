import io
import json
import uuid
from types import SimpleNamespace

from app.sharing import Sharing, make_sample
from tools import build_collector


def job():
    return SimpleNamespace(id=uuid.uuid4().hex, error=None, game_source={
        "items": [{"solverInstanceId": "secret-game-id", "cell": 2, "kind": "artifact", "instanceId": 12345}],
    }, request_payload={
        "grid": {"cellCount": 30, "cellLevelBonuses": [{"cell": 2, "bonus": 3, "secret": "private-path"}]}, "token": "secret-token", "path": "private-path",
        "artifacts": [{"instanceId": "secret-game-id", "typeId": "artifact-test", "baseLevel": 2}],
        "tablets": [], "customTabletTypes": [], "options": {"timeLimitMs": 15000},
    }, result={"solutionStatus": "FEASIBLE", "message": "private-error", "placements": [
        {"instanceId": "secret-game-id", "kind": "artifact", "cell": 0},
    ]})


def test_consent_queue_privacy_and_retry(tmp_path, monkeypatch):
    sharing = Sharing(tmp_path)
    assert sharing.status()["needsConsent"]
    sharing.enqueue(job(), sharing.ticket())
    assert not sharing.queue.exists()
    sharing.configure(True)
    ticket = sharing.ticket()
    sharing.enqueue(job(), ticket)
    assert sharing.status()["pending"] == 1
    with sharing.connect() as db:
        body = db.execute("SELECT body FROM pending").fetchone()[0]
    for private in ("secret-game-id", "secret-token", "private-path", "private-error", "12345"):
        assert private not in body
    sample = json.loads(body)
    assert sample["initialLayout"][0]["instanceId"] == "i0"
    assert sample["request"]["grid"]["cellCount"] == 30
    assert sample["request"]["grid"]["cellLevelBonuses"] == [{"cell": 2, "bonus": 3}]
    assert Sharing(tmp_path).status()["enabled"]

    def unavailable(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr("app.sharing.urlopen", unavailable)
    try:
        sharing.send_one()
    except OSError:
        pass
    assert sharing.status()["pending"] == 1
    sharing.configure(False)
    assert sharing.status()["pending"] == 0
    sharing.configure(True)
    sharing.enqueue(job(), ticket)  # Revoked jobs must not upload after re-enabling.
    assert sharing.status()["pending"] == 0


def test_receiver_validation_and_idempotent_ack(tmp_path, monkeypatch):
    monkeypatch.setattr(build_collector, "DB_PATH", tmp_path / "received.sqlite3")
    sample = make_sample(job())
    def post(value):
        body = json.dumps(value).encode()
        statuses = []
        output = build_collector.application({"PATH_INFO": "/packsmith/v1/builds", "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)},
            lambda status, headers: statuses.append(status))
        return statuses[0], json.loads(b"".join(output))
    assert post(sample) == ("201 Created", {"sampleId": sample["sampleId"]})
    assert post(sample)[0] == "201 Created"
    import sqlite3
    with sqlite3.connect(build_collector.DB_PATH) as db:
        assert db.execute("SELECT count(*) FROM samples").fetchone()[0] == 1
        assert db.execute("SELECT validation FROM samples").fetchone()[0] == "pending"
    sample["request"]["grid"]["cellCount"] = 300
    assert post(sample)[0] == "400 Bad Request"
