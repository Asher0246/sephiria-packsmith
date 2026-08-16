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
from dataclasses import dataclass, field
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
from .result_cache import ResultCache, default_cache
from .solver import StopController, solve

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 8_000_000
RUNTIME_FILE_NAME = "runtime.json"
DEFAULT_AUTO_ORGANIZE_TIME_LIMIT_MS = 30_000
DEFAULT_AUTO_ORGANIZE_WAIT_GRACE_S = 5.0
APPLICABLE_SOLUTION_STATUSES = frozenset({"OPTIMAL", "FEASIBLE", "STOPPED"})


@dataclass
class Job:
    id: str
    status: str = "QUEUED"
    result: dict | None = None
    error: dict | None = None
    game_source: dict | None = None
    controller: StopController = field(default_factory=StopController)


class AppState:
    def __init__(self, token: str, result_cache: ResultCache | None = None) -> None:
        self.token = token
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.result_cache = result_cache or default_cache()

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
        job = Job(uuid.uuid4().hex, game_source=game_source if isinstance(game_source, dict) else None)

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
            job.status = "FINISHED"
            job.result = cached
            with self.lock:
                self.jobs[job.id] = job
            return job
        with self.lock:
            self.jobs[job.id] = job

        def run() -> None:
            job.status = "RUNNING"
            try:
                job.result = solve(request, artifact_map, tablet_map, job.controller)
                job.status = "FINISHED"
                if cache_key is not None:
                    try:
                        self.result_cache.store(cache_key, artifact_ids, tablet_ids, job.result)
                    except Exception as exc:
                        print(f"result cache store skipped: {exc!r}")
            except Exception as exc:  # Boundary: return a stable API error, keep traceback in console.
                job.status = "FAILED"
                job.error = {"code": "INTERNAL_SOLVE_FAILURE", "message": str(exc)}
                import traceback
                traceback.print_exc()

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
    applied = apply_game_arrangement(command)
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
                    self._json(HTTPStatus.OK, {"solveId": job.id, "jobStatus": job.status, "result": job.result, "error": job.error})
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
            if path not in ("/api/solve", "/api/custom-tablet/compose", "/api/apply-arrangement", "/api/auto-organize"):
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": "接口不存在"}})
                return
            if not self._require_api_auth():
                return
            payload = self._read_json(optional=(path == "/api/auto-organize"))
            if payload is None:
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
                try:
                    command = prepare_apply_command(job.game_source, job.result)
                    applied = apply_game_arrangement(command)
                except GameApplyError as exc:
                    conflict_codes = {
                        "INVENTORY_CHANGED", "INVALID_APPLY_PLAN",
                        "INVALID_GAME_SNAPSHOT", "NO_GAME_SNAPSHOT", "NO_FEASIBLE_RESULT",
                    }
                    status = HTTPStatus.CONFLICT if exc.code in conflict_codes else HTTPStatus.SERVICE_UNAVAILABLE
                    self._json(status, {"error": {"code": exc.code, "message": str(exc)}})
                    return
                self._json(HTTPStatus.OK, applied)
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
    return server, token


def main() -> None:
    parser = argparse.ArgumentParser(description="Sephiria Packsmith 背包构筑求解器")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server, token = create_server(args.port, args.token)
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
        remove_runtime_info()
        server.server_close()


if __name__ == "__main__":
    main()
