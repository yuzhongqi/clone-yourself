"""Local deterministic minimization. Unstructured text never proves a predicate."""

import hashlib
import hmac
import re
from typing import Any, Dict, List, Tuple

from .core import canonical, require

SENSITIVE_KEY = re.compile(
    r"token|secret|password|receipt|signature|authorization|cookie|email|"
    r"order.?id|transaction.?id|device.?id|serial|account.?id|user.?id|display.?name", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
ASSIGNMENT = re.compile(
    r"(?im)(\b(?:[A-Za-z_.]*(?:token|secret|password|receipt|signature|authorization|"
    r"cookie|order.?id|transaction.?id|device.?id|serial|email))\b\s*[=:]\s*)"
    r'("[^"\n]*"|\'[^\'\n]*\'|[^\s,;]+)')


def marker(value: Any, key: bytes) -> str:
    return "[private:" + hmac.new(key, canonical(value).encode(), hashlib.sha256).hexdigest()[:16] + "]"


def redact_text(text: str, key: bytes) -> str:
    text = JWT.sub(lambda m: marker(m.group(), key), text)
    text = BEARER.sub(lambda m: marker(m.group(), key), text)
    text = EMAIL.sub(lambda m: marker(m.group(), key), text)
    return ASSIGNMENT.sub(lambda m: m.group(1) + marker(m.group(2), key), text)


def sanitize(value: Any, key: bytes) -> Tuple[Any, List[str]]:
    changed: List[str] = []

    def walk(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            result: Dict[str, Any] = {}
            for name, child in item.items():
                require(isinstance(name, str), "Privacy input keys must be strings")
                child_path = path + "/" + name.replace("~", "~0").replace("/", "~1")
                if SENSITIVE_KEY.search(name):
                    changed.append(child_path)
                    result[name] = marker(child, key)
                else:
                    result[name] = walk(child, child_path)
            return result
        if isinstance(item, list):
            return [walk(child, path + "/" + str(index)) for index, child in enumerate(item)]
        if isinstance(item, str):
            result = redact_text(item, key)
            if result != item:
                changed.append(path)
            return result
        require(type(item) in (int, float, bool) or item is None, "Unsupported privacy value")
        return item

    result = walk(value, "")
    require(len(canonical(result).encode()) <= 64_000, "Sanitized payload exceeds 64000 bytes")
    return result, changed
