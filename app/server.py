from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .catalog import ROOT, artifact_types, public_catalog, tablet_types
from .custom_tablets import compose_custom_tablet, parse_custom_tablet_types
from .game_bridge import (
    GameApplyError,
    GameBridgeError,
    apply_game_arrangement,
    inventory_to_solve_payload,
    prepare_apply_command,
    read_game_inventory,
)
from .models import RequestError, parse_request
from .repair import repair_layout
from .result_cache import ResultCache, default_cache
from .solver import StopController, solve
from .sharing import Sharing

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 8_000_000
RUNTIME_FILE_NAME = "runtime.json"
RECORDED_BUILDS_FILE_NAME = "recorded_builds.jsonl"
DEFAULT_AUTO_ORGANIZE_TIME_LIMIT_MS = 30_000
DEFAULT_AUTO_ORGANIZE_WAIT_GRACE_S = 5.0
APPLICABLE_SOLUTION_STATUSES = frozenset({"OPTIMAL", "FEASIBLE", "STOPPED"})
_RECORDED_BUILDS_LOCK = threading.Lock()


@dataclass
class Job:
    id: str
    status: str = "QUEUED"
    result: dict | None = None
    error: dict | None = None
    game_source: dict | None = None
    request_payload: dict | None = None
    record_requested: bool = False
    recording_succeeded: bool | None = None
    recording_error: str | None = None
    sharing_ticket: int | None = None
    controller: StopController = field(default_factory=StopController)


class AppState:
    def __init__(self, token: str, result_cache: ResultCache | None = None) -> None:
        self.token = token
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.result_cache = result_cache or default_cache()
        self.sharing = Sharing(packsmith_data_dir())
        self.gpu_search = None
        self.gpu_lock = threading.Lock()

    def _gpu_seed(self, request, artifact_map, tablet_map):
        # CuPy and CUDA stay fully optional and are imported only after the
        # user enables the experimental option.
        with self.gpu_lock:
            if self.gpu_search is None:
                from tools.gpu_search import GpuSearch
                self.gpu_search = GpuSearch()
            return self.gpu_search.search(
                request, artifact_map, tablet_map, count=4096, steps=512, seed=1,
            )

    @staticmethod
    def _quality(result: dict) -> tuple:
        if not result.get("placements"):
            return (-1, -1, -1, -1, -10**18, -10**18)
        return (
            result.get("specialObjective") or 0,
            result.get("primaryObjective") or 0,
            result.get("secondaryObjective") or 0,
            sum(item.get("active") is True for item in result.get("artifacts", [])),
            -(result.get("tertiaryObjective") or 0),
            result.get("emptyCellObjective") or 0,
        )

    def _solve(self, request, artifact_map, tablet_map, controller):
        controller = controller or StopController()
        if not request.gpu_acceleration:
            return repair_layout(
                request, artifact_map, tablet_map,
                solve(request, artifact_map, tablet_map, controller),
            )

        started = time.perf_counter()
        try:
            seed = self._gpu_seed(request, artifact_map, tablet_map)
        except Exception as exc:
            result = repair_layout(
                request, artifact_map, tablet_map,
                solve(request, artifact_map, tablet_map, controller),
            )
            result.setdefault("diagnostics", {}).update({
                "gpuRequested": True, "gpuUsed": False,
                "gpuFallbackReason": str(exc)[:300],
            })
            return result

        seed_result = repair_layout(request, artifact_map, tablet_map, seed["result"])
        elapsed_ms = (time.perf_counter() - started) * 1000
        remaining_ms = request.time_limit_ms - elapsed_ms
        if controller.stopped or remaining_ms < 1:
            result = seed_result
            if controller.stopped:
                result["solutionStatus"] = "STOPPED"
                result["message"] = "求解已停止，返回 GPU 已找到的排布"
        else:
            cpu_request = replace(request, time_limit_ms=max(1, int(remaining_ms)))
            result = repair_layout(
                cpu_request, artifact_map, tablet_map,
                solve(
                    cpu_request, artifact_map, tablet_map, controller,
                    initial_placements=seed["placements"],
                ),
            )
            if self._quality(seed_result) > self._quality(result):
                cpu_diagnostics = result.get("diagnostics", {})
                seed_result["buildMs"] = result.get("buildMs", 0)
                seed_result["solveMs"] = result.get("solveMs", 0)
                seed_result.setdefault("diagnostics", {}).update({
                    "replacedCpuResult": True,
                    "discardedCpuQuality": self._quality(result),
                    "initialHintVariables": cpu_diagnostics.get("initialHintVariables", 0),
                })
                result = seed_result
        result.setdefault("diagnostics", {}).update({
            "gpuRequested": True, "gpuUsed": True,
            "gpuMs": round(seed["elapsedMs"], 2),
            "gpuEvaluations": seed["evaluations"],
        })
        return result

    def create_job(self, payload: dict) -> Job:
        artifact_map = {item.id: item for item in artifact_types()}
        tablet_map = {item.id: item for item in tablet_types()}
        custom_tablets = parse_custom_tablet_types(payload)
        tablet_map.update(custom_tablets)
        request = parse_request(payload, set(artifact_map), set(tablet_map))
        expected_size = f"{request.rows}x{request.cols}"
        for tablet in custom_tablets.values():
            if tablet.cell_count != request.cell_count or expected_size not in (tablet.candidates or {}):
                raise RequestError(f"自定义石板 {tablet.name} 仅适用于创建时的背包格数")
        game_source = payload.get("gameSource")
        # Local build recording follows the sharing consent: without consent the
        # tool records nothing at all, so the two can never drift apart.
        sharing_ticket = self.sharing.ticket()
        job = Job(
            uuid.uuid4().hex,
            game_source=game_source if isinstance(game_source, dict) else None,
            request_payload={
                key: value for key, value in payload.items()
                if key not in ("gameSource", "recordBuild")
            },
            record_requested=sharing_ticket is not None,
            sharing_ticket=sharing_ticket,
        )

        cache_key = artifact_ids = tablet_ids = None
        try:
            cache_key, artifact_ids, tablet_ids = self.result_cache.key(request, payload)
            cached = self.result_cache.lookup(
                cache_key, request, artifact_map, tablet_map, artifact_ids, tablet_ids,
            )
        except Exception as exc:  # Boundary: a cache failure must never block solving.
            print(f"result cache skipped: {exc!r}")
            cached = None
        if cached is not None:
            job.result = cached
            if request.gpu_acceleration:
                job.result.setdefault("diagnostics", {}).update({
                    "gpuRequested": True, "gpuUsed": False, "gpuCacheHit": True,
                })
            self.sharing.enqueue(job, job.sharing_ticket)
            if job.record_requested:
                record_solve(job, "FINISHED")
            job.status = "FINISHED"
            with self.lock:
                self.jobs[job.id] = job
            return job
        with self.lock:
            self.jobs[job.id] = job

        def run() -> None:
            job.status = "RUNNING"
            terminal_status = "FINISHED"
            try:
                job.result = self._solve(request, artifact_map, tablet_map, job.controller)
                if cache_key is not None:
                    try:
                        self.result_cache.store(cache_key, artifact_ids, tablet_ids, job.result)
                    except Exception as exc:
                        print(f"result cache store skipped: {exc!r}")
            except Exception as exc:  # Boundary: return a stable API error, keep traceback in console.
                terminal_status = "FAILED"
                job.error = {"code": "INTERNAL_SOLVE_FAILURE", "message": str(exc)}
                import traceback
                traceback.print_exc()
            if job.record_requested:
                record_solve(job, terminal_status)
            self.sharing.enqueue(job, job.sharing_ticket)
            job.status = terminal_status

        threading.Thread(target=run, name=f"solve-{job.id[:8]}", daemon=True).start()
        return job

    def wait_for_job(self, job: Job, timeout_s: float) -> Job:
        deadline = time.monotonic() + max(0.001, timeout_s)
        while time.monotonic() < deadline:
            with self.lock:
                if job.status in ("FINISHED", "FAILED"):
                    return job
            time.sleep(0.05)
        job.controller.stop()
        while time.monotonic() < deadline + DEFAULT_AUTO_ORGANIZE_WAIT_GRACE_S:
            with self.lock:
                if job.status in ("FINISHED", "FAILED"):
                    return job
            time.sleep(0.05)
        return job


def packsmith_data_dir() -> Path:
    override = os.environ.get("SEPHIRIA_CACHE_DIR")
    if override:
        return Path(override)
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SephiriaPacksmith"
    return Path.home() / ".sephiria-packsmith"


def runtime_info_path() -> Path:
    return packsmith_data_dir() / RUNTIME_FILE_NAME


def recorded_builds_path() -> Path:
    return packsmith_data_dir() / RECORDED_BUILDS_FILE_NAME


def _write_build_record(record: dict) -> tuple[bool, str | None]:
    try:
        path = recorded_builds_path()
        with _RECORDED_BUILDS_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True, None
    except Exception as exc:  # Recording is optional and must not block solving or applying.
        print(f"build recording skipped: {exc!r}")
        return False, str(exc)


def record_solve(job: Job, job_status: str) -> None:
    job.recording_succeeded, job.recording_error = _write_build_record({
        "schemaVersion": 2,
        "event": "solve",
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "solveId": job.id,
        "request": job.request_payload,
        "gameSourceAtSolve": job.game_source,
        "jobStatus": job_status,
        "solution": job.result,
        "error": job.error,
    })


def record_apply_attempt(
    job: Job,
    *,
    pre_apply_inventory: dict | None,
    pre_apply_read_error: dict | None,
    command: dict | None,
    outcome: dict,
) -> tuple[bool, str | None]:
    record = {
        "schemaVersion": 2,
        "event": "apply",
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "solveId": job.id,
        "request": job.request_payload,
        "gameSourceAtSolve": job.game_source,
        "preApplyInventory": pre_apply_inventory,
        "preApplyReadError": pre_apply_read_error,
        "solution": job.result,
        "applyCommand": command,
        "outcome": outcome,
    }
    return _write_build_record(record)


def write_runtime_info(port: int, token: str) -> None:
    directory = packsmith_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": port,
        "token": token,
        "url": f"http://127.0.0.1:{port}",
    }
    runtime_info_path().write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def remove_runtime_info() -> None:
    try:
        runtime_info_path().unlink(missing_ok=True)
    except OSError:
        pass


def _auto_organize_options(body: dict) -> dict:
    options = body.get("options") if isinstance(body.get("options"), dict) else body
    if not isinstance(options, dict):
        options = {}
    time_limit_ms = options.get("timeLimitMs", DEFAULT_AUTO_ORGANIZE_TIME_LIMIT_MS)
    if not isinstance(time_limit_ms, int) or isinstance(time_limit_ms, bool):
        try:
            time_limit_ms = int(time_limit_ms)
        except (TypeError, ValueError):
            time_limit_ms = DEFAULT_AUTO_ORGANIZE_TIME_LIMIT_MS
    time_limit_ms = max(1000, min(600_000, time_limit_ms))
    worker_count = options.get("workerCount", 0)
    if not isinstance(worker_count, int) or isinstance(worker_count, bool):
        try:
            worker_count = int(worker_count)
        except (TypeError, ValueError):
            worker_count = 0
    worker_count = max(0, min(64, worker_count))
    fast_mode = options.get("fastMode", True)
    if not isinstance(fast_mode, bool):
        fast_mode = bool(fast_mode)
    return {
        "timeLimitMs": time_limit_ms,
        "workerCount": worker_count,
        "fastMode": fast_mode,
    }


def auto_organize(state: AppState, body: dict | None = None) -> dict:
    body = body or {}
    options = _auto_organize_options(body)
    inventory = read_game_inventory()
    payload = inventory_to_solve_payload(
        inventory,
        fast_mode=options["fastMode"],
        time_limit_ms=options["timeLimitMs"],
        worker_count=options["workerCount"],
    )
    job = state.create_job(payload)
    wait_timeout_s = options["timeLimitMs"] / 1000 + DEFAULT_AUTO_ORGANIZE_WAIT_GRACE_S
    job = state.wait_for_job(job, wait_timeout_s)
    if job.status == "FAILED":
        message = "求解失败"
        if isinstance(job.error, dict):
            message = str(job.error.get("message") or message)
        raise RequestError(message)
    result = job.result
    if not isinstance(result, dict):
        raise RequestError("求解未返回有效结果")
    solution_status = result.get("solutionStatus")
    if solution_status not in APPLICABLE_SOLUTION_STATUSES:
        raise RequestError(str(result.get("message") or "没有满足全部约束的排布"))
    command = prepare_apply_command(job.game_source, result)
    sharing_ticket = state.sharing.ticket()
    try:
        applied = apply_game_arrangement(command)
    except GameApplyError:
        state.sharing.enqueue(job, sharing_ticket, "apply", {"ok": False})
        raise
    state.sharing.enqueue(job, sharing_ticket, "apply", applied)
    return {
        "ok": True,
        "message": str(result.get("message") or "已应用到游戏"),
        "solutionStatus": solution_status,
        "solveId": job.id,
        "solveMs": result.get("solveMs"),
        "relativeGap": result.get("relativeGap"),
        "moves": applied.get("moves"),
        "rotations": applied.get("rotations"),
        "inventoryFingerprint": applied.get("inventoryFingerprint"),
    }


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SephiriaPacksmith/0.1"

        def log_message(self, fmt: str, *args) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            parsed = urlparse(self.path)
            query_token = parse_qs(parsed.query).get("token", [None])[0]
            return secrets.compare_digest(self.headers.get("X-Sephiria-Token", ""), state.token) or (
                isinstance(query_token, str) and secrets.compare_digest(query_token, state.token)
            )

        def _require_api_auth(self) -> bool:
            if not self._authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": {"code": "FORBIDDEN", "message": "访问令牌无效"}})
                return False
            return True

        def _read_json(self, *, optional: bool = False) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1 if not optional else 0
            if optional and length <= 0:
                return {}
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": {"code": "UNSUPPORTED_MEDIA_TYPE", "message": "请求必须使用 application/json"}})
                return None
            if not 0 < length <= MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"code": "BODY_TOO_LARGE", "message": "请求体大小无效"}})
                return None
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "INVALID_JSON", "message": "JSON 格式无效"}})
                return None
            if not isinstance(value, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "INVALID_REQUEST", "message": "请求体必须是对象"}})
                return None
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                if not self._require_api_auth():
                    return
                if path == "/api/catalog":
                    self._json(HTTPStatus.OK, public_catalog())
                    return
                if path == "/api/sharing":
                    self._json(HTTPStatus.OK, state.sharing.status())
                    return
                if path == "/api/game-inventory":
                    try:
                        inventory = read_game_inventory()
                    except GameBridgeError as exc:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                            "error": {"code": "GAME_BRIDGE_UNAVAILABLE", "message": str(exc)},
                        })
                        return
                    self._json(HTTPStatus.OK, inventory)
                    return
                if path.startswith("/api/solve/"):
                    job_id = path.rsplit("/", 1)[-1]
                    with state.lock:
                        job = state.jobs.get(job_id)
                    if not job:
                        self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": "求解任务不存在"}})
                        return
                    self._json(HTTPStatus.OK, {
                        "solveId": job.id,
                        "jobStatus": job.status,
                        "result": job.result,
                        "error": job.error,
                        "recordedBuild": job.recording_succeeded,
                        "recordingError": job.recording_error,
                    })
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": "接口不存在"}})
                return
            self._serve_static(path)

        def _serve_static(self, path: str) -> None:
            if path == "/":
                path = "/index.html"
            if path.startswith("/images/"):
                root = ROOT / "assets" / "images"
                relative = path.removeprefix("/images/")
            else:
                root = STATIC
                relative = path.lstrip("/")
            target = (root / relative).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/api/solve", "/api/custom-tablet/compose", "/api/apply-arrangement", "/api/auto-organize", "/api/sharing"):
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": "接口不存在"}})
                return
            if not self._require_api_auth():
                return
            payload = self._read_json(optional=(path == "/api/auto-organize"))
            if payload is None:
                return
            if path == "/api/sharing":
                try:
                    response = state.sharing.configure(payload.get("enabled"))
                except (ValueError, OSError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": {"message": "无法保存数据分享设置"}})
                    return
                self._json(HTTPStatus.OK, response)
                return
            if path == "/api/auto-organize":
                try:
                    response = auto_organize(state, payload)
                except GameBridgeError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                        "error": {"code": "GAME_BRIDGE_UNAVAILABLE", "message": str(exc)},
                    })
                    return
                except GameApplyError as exc:
                    conflict_codes = {
                        "INVENTORY_CHANGED", "INVALID_APPLY_PLAN",
                        "INVALID_GAME_SNAPSHOT", "NO_GAME_SNAPSHOT", "NO_FEASIBLE_RESULT",
                        "GAME_VERSION_CHANGED",
                    }
                    status = HTTPStatus.CONFLICT if exc.code in conflict_codes else HTTPStatus.SERVICE_UNAVAILABLE
                    self._json(status, {"error": {"code": exc.code, "message": str(exc)}})
                    return
                except RequestError as exc:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": {"code": "AUTO_ORGANIZE_FAILED", "message": str(exc)},
                    })
                    return
                self._json(HTTPStatus.OK, response)
                return
            if path == "/api/apply-arrangement":
                solve_id = payload.get("solveId")
                sharing_ticket = state.sharing.ticket()
                record_build = sharing_ticket is not None
                with state.lock:
                    job = state.jobs.get(solve_id) if isinstance(solve_id, str) else None
                if not job:
                    self._json(HTTPStatus.NOT_FOUND, {
                        "error": {"code": "NOT_FOUND", "message": "求解任务不存在"},
                    })
                    return
                if job.status != "FINISHED" or not job.result:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": {"code": "SOLVE_NOT_FINISHED", "message": "求解任务尚未完成"},
                    })
                    return
                if job.result.get("solutionStatus") not in ("OPTIMAL", "FEASIBLE", "STOPPED"):
                    self._json(HTTPStatus.CONFLICT, {
                        "error": {"code": "NO_FEASIBLE_RESULT", "message": "求解任务没有可应用的排布"},
                    })
                    return
                pre_apply_inventory = None
                pre_apply_read_error = None
                if record_build:
                    try:
                        pre_apply_inventory = read_game_inventory()
                    except Exception as exc:  # Extra context is best-effort; applying should still proceed.
                        pre_apply_read_error = {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                command = None
                try:
                    command = prepare_apply_command(job.game_source, job.result)
                    applied = apply_game_arrangement(command)
                except GameApplyError as exc:
                    recorded = False
                    state.sharing.enqueue(job, sharing_ticket, "apply", {"ok": False})
                    recording_error = None
                    if record_build:
                        recorded, recording_error = record_apply_attempt(
                            job,
                            pre_apply_inventory=pre_apply_inventory,
                            pre_apply_read_error=pre_apply_read_error,
                            command=command,
                            outcome={"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                        )
                    conflict_codes = {
                        "INVENTORY_CHANGED", "INVALID_APPLY_PLAN",
                        "INVALID_GAME_SNAPSHOT", "NO_GAME_SNAPSHOT", "NO_FEASIBLE_RESULT",
                    }
                    status = HTTPStatus.CONFLICT if exc.code in conflict_codes else HTTPStatus.SERVICE_UNAVAILABLE
                    error_message = str(exc)
                    if recording_error:
                        error_message += f"；实际构筑记录失败：{recording_error}"
                    self._json(status, {
                        "error": {"code": exc.code, "message": error_message},
                        "recordedBuild": recorded,
                    })
                    return
                response = dict(applied)
                state.sharing.enqueue(job, sharing_ticket, "apply", applied)
                if record_build:
                    recorded, recording_error = record_apply_attempt(
                        job,
                        pre_apply_inventory=pre_apply_inventory,
                        pre_apply_read_error=pre_apply_read_error,
                        command=command,
                        outcome={"ok": True, "response": applied},
                    )
                    response["recordedBuild"] = recorded
                    if recording_error:
                        response["recordingError"] = recording_error
                self._json(HTTPStatus.OK, response)
                return
            if path == "/api/custom-tablet/compose":
                try:
                    tablet_map = {item.id: item for item in tablet_types()}
                    custom = compose_custom_tablet(payload, tablet_map)
                except RequestError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {
                        "error": {"code": "INVALID_CUSTOM_TABLET", "message": str(exc)},
                    })
                    return
                self._json(HTTPStatus.OK, custom)
                return
            try:
                job = state.create_job(payload)
            except RequestError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "INVALID_REQUEST", "message": str(exc)}})
                return
            self._json(HTTPStatus.ACCEPTED, {"solveId": job.id, "jobStatus": job.status})

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/solve/") or not self._require_api_auth():
                return
            job_id = path.rsplit("/", 1)[-1]
            with state.lock:
                job = state.jobs.get(job_id)
            if not job:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": "求解任务不存在"}})
                return
            job.controller.stop()
            self._json(HTTPStatus.OK, {"solveId": job.id, "jobStatus": job.status, "stopRequested": True})

    return Handler


def create_server(port: int = 0, token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(24)
    state = AppState(token)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    server.sharing = state.sharing
    return server, token


def main() -> None:
    parser = argparse.ArgumentParser(description="Sephiria Packsmith 背包构筑求解器")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server, token = create_server(args.port, args.token)
    server.sharing.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"
    try:
        write_runtime_info(port, token)
    except OSError as exc:
        print(f"runtime info skipped: {exc!r}")
    print(json.dumps({"event": "READY", "url": url, "runtime": str(runtime_info_path())}, ensure_ascii=False), flush=True)
    if not args.no_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在停止服务...")
    finally:
        server.sharing.close()
        remove_runtime_info()
        server.server_close()


if __name__ == "__main__":
    main()
