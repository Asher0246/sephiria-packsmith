"""Disk cache for proven solve results.

Only results the solver proved (OPTIMAL layouts and INFEASIBLE proofs) are
cached; they remain valid for any time limit.  The cache key covers every
input that shapes the model - grid, per-instance settings (normalized so the
instance order and generated ids do not matter), custom tablet definitions,
catalog data and solver code - while time limit and worker count are
deliberately excluded.  A cached layout is re-validated against the new
request before it is returned, and any load or validation failure silently
falls back to a fresh solve: the cache is an accelerator, never a dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path

from .models import ArtifactInstance, SolveRequest, TabletInstance
from .validation import validate_result

CACHE_VERSION = 1
CACHE_FILE_NAME = "solve-results.json"
CACHEABLE_STATUSES = ("OPTIMAL", "INFEASIBLE")
_ID_FIELDS = ("instanceId", "specialTargetInstanceId")

_CODE_FILES = ("solver.py", "models.py", "validation.py", "custom_tablets.py", "catalog.py")


def _optional_int(value: int | None) -> tuple:
    return (0,) if value is None else (1, value)


@lru_cache(maxsize=1)
def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    package = Path(__file__).resolve().parent
    for name in _CODE_FILES:
        path = package / name
        if path.exists():
            digest.update(name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _catalog_fingerprint() -> str:
    from .catalog import artifact_types, tablet_types

    payload = json.dumps(
        {"artifacts": [asdict(item) for item in artifact_types()],
         "tablets": [asdict(item) for item in tablet_types()]},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=list,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_row(item: ArtifactInstance, target: ArtifactInstance | None) -> tuple:
    target_row = (0,) if target is None else (1,) + (
        target.type_id, target.weight, target.base_level, target.fixed_cell,
        _optional_int(target.min_level), _optional_int(target.exact_level),
    )
    return (
        item.type_id, item.weight, item.base_level, item.fixed_cell,
        _optional_int(item.min_level), _optional_int(item.exact_level),
        item.special_priority, target_row,
    )


def _tablet_row(item: TabletInstance) -> tuple:
    return (item.type_id, item.fixed_cell, item.fixed_rotation, item.preferred_rotation)


def _canonical_artifacts(request: SolveRequest) -> tuple[list[tuple], list[str]]:
    by_id = {item.instance_id: item for item in request.artifacts}
    ordered = sorted(
        request.artifacts,
        key=lambda item: (
            _artifact_row(item, by_id.get(item.special_target_instance_id)
                          if item.special_priority else None),
            item.instance_id,
        ),
    )
    rows = [
        _artifact_row(item, by_id.get(item.special_target_instance_id)
                      if item.special_priority else None)
        for item in ordered
    ]
    return rows, [item.instance_id for item in ordered]


def _canonical_tablets(request: SolveRequest) -> tuple[list[tuple], list[str]]:
    ordered = sorted(request.tablets, key=lambda item: (_tablet_row(item), item.instance_id))
    return ([_tablet_row(item) for item in ordered],
            [item.instance_id for item in ordered])


def _remap_ids(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {
            key: (mapping.get(entry, entry)
                  if key in _ID_FIELDS and isinstance(entry, str)
                  else _remap_ids(entry, mapping))
            for key, entry in value.items()
        }
    if isinstance(value, list):
        return [_remap_ids(entry, mapping) for entry in value]
    return value


class ResultCache:
    def __init__(self, directory: Path | str, max_entries: int = 200) -> None:
        self.directory = Path(directory)
        self.max_entries = max_entries
        self.path = self.directory / CACHE_FILE_NAME
        self._lock = threading.Lock()
        self._entries: dict[str, dict] | None = None

    # -- key -------------------------------------------------------------

    def key(self, request: SolveRequest, payload: dict) -> tuple[str, list[str], list[str]]:
        """Return (key, canonical artifact ids, canonical tablet ids).

        Time limit and worker count are intentionally absent: a proven result
        does not depend on them, and re-running the same build with a longer
        limit is the most common repeat request.
        """
        artifact_rows, artifact_ids = _canonical_artifacts(request)
        tablet_rows, tablet_ids = _canonical_tablets(request)
        fingerprint = json.dumps({
            "version": CACHE_VERSION,
            "code": _code_fingerprint(),
            "catalog": _catalog_fingerprint(),
            "grid": {
                "rows": request.rows,
                "cols": request.cols,
                "cellCount": request.cell_count,
                "doubleLevelCells": sorted(request.double_level_cells),
            },
            "artifacts": [list(row) for row in artifact_rows],
            "tablets": [list(row) for row in tablet_rows],
            "customTabletTypes": payload.get("customTabletTypes") or [],
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return digest, artifact_ids, tablet_ids

    # -- lookup / store ---------------------------------------------------

    def lookup(
        self, key: str, request: SolveRequest,
        artifacts_by_id: dict, tablets_by_id: dict,
        artifact_ids: list[str], tablet_ids: list[str],
    ) -> dict | None:
        entry = self._loaded().get(key)
        if entry is None:
            return None
        if (len(entry.get("artifactIds", [])) != len(artifact_ids)
                or len(entry.get("tabletIds", [])) != len(tablet_ids)):
            self._evict(key)
            return None
        mapping = dict(zip(entry["artifactIds"], artifact_ids))
        mapping.update(zip(entry["tabletIds"], tablet_ids))
        result = _remap_ids(entry.get("result"), mapping)
        if not isinstance(result, dict) or result.get("solutionStatus") not in CACHEABLE_STATUSES:
            self._evict(key)
            return None
        if validate_result(request, artifacts_by_id, tablets_by_id, result):
            self._evict(key)
            return None
        result["fromCache"] = True
        return result

    def store(self, key: str, artifact_ids: list[str], tablet_ids: list[str],
              result: dict) -> None:
        if not isinstance(result, dict) or result.get("solutionStatus") not in CACHEABLE_STATUSES:
            return
        with self._lock:
            self._load()
            self._entries[key] = {
                "created": time.time(),
                "artifactIds": list(artifact_ids),
                "tabletIds": list(tablet_ids),
                "result": result,
            }
            if len(self._entries) > self.max_entries:
                for stale in sorted(self._entries,
                                    key=lambda item: self._entries[item]["created"])[:len(self._entries) - self.max_entries]:
                    del self._entries[stale]
            self._save()

    def _evict(self, key: str) -> None:
        with self._lock:
            if self._entries is not None and self._entries.pop(key, None) is not None:
                self._save()

    # -- persistence ------------------------------------------------------

    def _loaded(self) -> dict[str, dict]:
        with self._lock:
            self._load()
            return self._entries

    def _load(self) -> None:
        if self._entries is not None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries") if isinstance(raw, dict) else None
            self._entries = {key: entry for key, entry in entries.items()
                             if isinstance(entry, dict)} if isinstance(entries, dict) else {}
        except (OSError, ValueError):
            self._entries = {}

    def _save(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(
                json.dumps({"version": CACHE_VERSION, "entries": self._entries},
                           ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temp, self.path)
        except OSError:
            pass  # Best effort only: solve results stay correct without the cache.


_DEFAULT: ResultCache | None = None
_DEFAULT_LOCK = threading.Lock()


def default_cache() -> ResultCache:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            override = os.environ.get("SEPHIRIA_CACHE_DIR")
            if override:
                directory = Path(override)
            elif os.environ.get("LOCALAPPDATA"):
                directory = Path(os.environ["LOCALAPPDATA"]) / "SephiriaPacksmith"
            else:
                directory = Path.home() / ".sephiria-packsmith"
            _DEFAULT = ResultCache(directory)
        return _DEFAULT
