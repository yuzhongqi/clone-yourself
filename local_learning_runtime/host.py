"""One user-local, headless service. CLI, MCP and hooks are thin IPC clients."""

import json
import os
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .core import Store, RuntimeErrorBase, canonical, digest, require, require_private_location
from . import __version__

MAX_FRAME = 256_000


def endpoint(home: Path) -> Path:
    require(os.name == "posix", "Shared Host currently supports macOS/Linux; Windows needs a named-pipe adapter")
    home = home.expanduser().resolve()
    require_private_location(home)
    base = home / "ipc"
    if len(str(base / "agent.sock").encode()) >= 100:
        base = Path(tempfile.gettempdir()) / ("llr-" + str(os.getuid()) + "-" + digest(str(home).encode())[:12])
    require(not base.is_symlink(), "IPC directory cannot be a symlink")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(base.stat().st_uid == os.getuid(), "IPC directory has a different owner")
    base.chmod(0o700)
    return base / "agent.sock"


def raw_call(home: Path, request: Dict[str, Any], timeout: float = 120) -> Dict[str, Any]:
    message = canonical(request).encode() + b"\n"
    require(len(message) <= MAX_FRAME, "IPC request exceeds 256000 bytes")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(endpoint(home)))
        connection.sendall(message)
        with connection.makefile("rb") as stream:
            response = stream.readline(8 * 1024 * 1024 + 1)
    require(len(response) <= 8 * 1024 * 1024 and response.endswith(b"\n"), "Invalid/oversized IPC response")
    body = json.loads(response)
    if body.get("error"):
        raise RuntimeErrorBase(body["error"])
    return body["result"]


def ensure_host(home: Path) -> Dict[str, Any]:
    import fcntl
    home = home.expanduser().resolve()
    require_private_location(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / "host-start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return raw_call(home, {"method": "health"}, timeout=1)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        sock = endpoint(home)
        if sock.exists():
            sock.unlink()
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        with (home / "host.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "local_learning_runtime", "--home", str(home), "host-serve"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeErrorBase("Shared Host failed to start; inspect private host.log")
            try:
                return raw_call(home, {"method": "health"}, timeout=1)
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                time.sleep(0.05)
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeErrorBase("Shared Host did not become responsive within 10 seconds")


def call(home: Path, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    ensure_host(home)
    return raw_call(home, {"method": method, "payload": payload or {}})


def dispatch_request(home: Path, request: Dict[str, Any]) -> Any:
    store = Store(home)
    try:
        payload = request.get("payload", {})
        if request["method"] == "command":
            from .cli import parser, dispatch
            try:
                args = parser().parse_args(payload["argv"])
            except SystemExit as exc:
                raise RuntimeErrorBase("Invalid proxied command arguments") from exc
            require(args.home.expanduser().resolve() == home.expanduser().resolve(), "IPC home mismatch")
            require(args.command not in ("host-serve", "host-stop", "host-status", "mcp"),
                    "Invalid proxied lifecycle command")
            if args.command == "hook":
                from .clients import hook
                return hook(store, args.client, args.event, payload["hook_input"])
            if args.command == "notify":
                from .clients import codex_notify
                return codex_notify(store, json.loads(args.payload))
            return dispatch(store, args)
        if request["method"] == "mcp":
            from .mcp import respond
            return respond(store, payload)
        raise RuntimeErrorBase("Unknown Host method")
    finally:
        store.close()


class AgentServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, home: Path, idle_seconds: float = 600):
        self.home = home.expanduser().resolve()
        self.idle_seconds = idle_seconds
        self.last_used = time.monotonic()
        self.stopping = threading.Event()
        self.working = threading.Event()
        self.mission_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(16)
        self.active_lock = threading.Lock()
        self.active_requests = 0
        self.socket_path = endpoint(home)
        super().__init__(str(self.socket_path), AgentHandler)
        self.socket_path.chmod(0o600)
        self.worker = threading.Thread(target=self.learning_worker, daemon=True)
        self.worker.start()

    def learning_worker(self) -> None:
        from .missions import tick
        while not self.stopping.wait(0.5):
            if not (self.home / "runtime.sqlite").exists():
                self.stopping.set()
                self.shutdown()
                return
            store = Store(self.home)
            row = None
            try:
                row = store.db.execute("SELECT id FROM missions WHERE state='RUNNING' "
                                       "ORDER BY updated_at LIMIT 1").fetchone()
                if row is not None:
                    self.working.set()
                    with self.mission_lock:
                        tick(store, row["id"])
                    self.last_used = time.monotonic()
                elif time.monotonic() - self.last_used > self.idle_seconds:
                    self.stopping.set()
                    self.shutdown()
                    return
            except (RuntimeErrorBase, OSError, ValueError, sqlite3.Error) as exc:
                # Only the exception type is logged; private content is kept in task records.
                print("Learning worker blocked: " + type(exc).__name__, file=sys.stderr, flush=True)
                if row is not None:
                    with store.transaction():
                        store.db.execute("UPDATE missions SET state='WAITING_USER' WHERE id=?", (row["id"],))
            finally:
                self.working.clear()
                store.close()

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()
        path = self.socket_path
        if path.exists():
            path.unlink()


class AgentHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        shutdown_after_response = False
        if not server.slots.acquire(blocking=False):
            self.wfile.write(canonical({"error": "Host busy; retry a non-side-effect read later"}).encode() + b"\n")
            return
        with server.active_lock:
            server.active_requests += 1
        try:
            self.connection.settimeout(120)
            raw = self.rfile.readline(MAX_FRAME + 1)
            require(len(raw) <= MAX_FRAME and raw.endswith(b"\n"), "Malformed/oversized IPC request")
            request = json.loads(raw)
            server.last_used = time.monotonic()
            if request.get("method") == "health":
                result = {"status": "READY", "pid": os.getpid(), "state_home": str(server.home),
                          "version": __version__, "mission_active": server.working.is_set(),
                          "worker_alive": server.worker.is_alive(),
                          "model_calls_by_health": 0}
            elif request.get("method") == "shutdown":
                require(not server.working.is_set(), "Pause active learning before stopping Host")
                with server.active_lock:
                    require(server.active_requests == 1, "Wait for active client operations before stopping Host")
                result = {"status": "STOPPING", "pid": os.getpid()}
                shutdown_after_response = True
            else:
                payload = request.get("payload", {})
                argv = payload.get("argv", [])
                if request["method"] == "command" and "mission-tick" in argv:
                    with server.mission_lock:
                        result = dispatch_request(server.home, request)
                else:
                    result = dispatch_request(server.home, request)
            response = {"result": result}
        except (RuntimeErrorBase, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            response = {"error": str(exc)}
        finally:
            with server.active_lock:
                server.active_requests -= 1
            server.slots.release()
        raw_response = canonical(response).encode() + b"\n"
        if len(raw_response) > 8 * 1024 * 1024:
            raw_response = canonical({"error": "Host response too large; request paged memory"}).encode() + b"\n"
        self.wfile.write(raw_response)
        self.wfile.flush()
        if shutdown_after_response:
            threading.Thread(target=server.shutdown, daemon=True).start()


def serve_host(home: Path) -> None:
    import fcntl
    home = home.expanduser().resolve()
    require_private_location(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / "host-owner.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeErrorBase("A Host already owns this state directory") from exc
        store = Store(home)
        store.close()
        path = endpoint(home)
        if path.exists():
            path.unlink()
        server = AgentServer(home)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()


def stop_host(home: Path) -> Dict[str, Any]:
    import fcntl
    result = raw_call(home, {"method": "shutdown"})
    deadline = time.monotonic() + 5
    with (home.expanduser().resolve() / "host-owner.lock").open("a") as owner:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.02)
            else:
                fcntl.flock(owner, fcntl.LOCK_UN)
                return result
    raise RuntimeErrorBase("Host shutdown did not complete within 5 seconds")
