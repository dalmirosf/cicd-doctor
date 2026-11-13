"""Helpers for webhook tests: load the synthetic payloads and sign them like GitHub does."""

import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

FIXTURES = Path(__file__).parent / "fixtures" / "webhooks"
SECRET = b"test-webhook-secret"  # not a real secret: only these tests use it


def load(name: str) -> dict[str, Any]:
    """A fresh copy of ``tests/fixtures/webhooks/<name>.json``, safe to modify."""
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def headers(
    event: str, body: bytes, *, delivery: str | None = None, secret: bytes = SECRET
) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid4()),
        "X-Hub-Signature-256": sign(body, secret),
        "Content-Type": "application/json",
    }
