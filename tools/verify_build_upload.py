"""Explicit deployment smoke test: uploads one SYNTHETIC build, never user history.

Run from the repository root: python -m tools.verify_build_upload
"""
import json
import os
import tempfile
from pathlib import Path

from app.catalog import artifact_types
from app.result_cache import ResultCache
from app.server import AppState
from app.sharing import Sharing


def main():
    with tempfile.TemporaryDirectory(prefix="packsmith-upload-check-") as temp:
        root = Path(temp)
        os.environ["SEPHIRIA_CACHE_DIR"] = str(root)
        state = AppState("synthetic-test", ResultCache(root / "cache"))
        state.sharing = Sharing(root)
        state.sharing.configure(True)
        job = state.create_job({
            "grid": {"cellCount": 30},
            "artifacts": [{"instanceId": "synthetic-a", "typeId": artifact_types()[0].id}],
            "tablets": [], "options": {"timeLimitMs": 1000},
        })
        state.wait_for_job(job, 10)
        assert job.status == "FINISHED", job.error
        assert state.sharing.status()["pending"] == 1
        with state.sharing.connect() as db:
            sample_id, body = db.execute("SELECT id, body FROM pending").fetchone()
        assert state.sharing.send_one()
        assert state.sharing.status()["pending"] == 0
        # Simulate a lost acknowledgement: replay the identical delivery ID.
        with state.sharing.connect() as db:
            db.execute("INSERT INTO pending VALUES (?, ?)", (sample_id, body))
        assert state.sharing.send_one()
        print(json.dumps({"ok": True, "syntheticSampleId": sample_id, "duplicateAcknowledged": True}))


if __name__ == "__main__":
    main()
