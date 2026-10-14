"""One approved gateway HTTP request in an interruptible private subprocess."""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict

from .core import RuntimeErrorBase, canonical, require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeErrorBase("Gateway redirect refused")


def send_gateway(endpoint: str, headers: Dict[str, str], body: bytes,
                 timeout: float) -> Dict[str, Any]:
    require(timeout > 0, "Mission deadline reached before model request")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen([sys.executable, "-m", "local_learning_runtime.gateway_transport"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    payload = canonical({"endpoint": endpoint, "headers": headers, "body": body.decode()}).encode()
    try:
        stdout, _ = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise RuntimeErrorBase("Gateway wall-clock deadline exceeded; billing outcome unknown, no retry") from exc
    require(process.returncode == 0, "Gateway transport failed; billed request requires review")
    require(len(stdout) <= 32_000, "Gateway response oversized")
    return json.loads(stdout)


def main():
    request = json.loads(sys.stdin.buffer.read(64_001))
    raw = request["body"].encode()
    req = urllib.request.Request(request["endpoint"], raw, request["headers"])
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
        data = response.read(32_001)
    require(len(data) <= 32_000, "Gateway response oversized")
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeErrorBase, OSError, ValueError, KeyError):
        # Do not print authorization headers, raw request or endpoint response.
        print("Gateway transport rejected", file=sys.stderr)
        raise SystemExit(1)
