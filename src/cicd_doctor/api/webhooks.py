"""``POST /webhooks/github`` (FR-01, FR-02, SPEC §7.1 step 1, §8, D-56).

The request path only checks, records and enqueues: no GitHub or LLM call (NFR-01). Checks run
in this order, and nothing is written before the last one:

1. body size (25 MB)                → 413
2. ``X-Hub-Signature-256``          → 401 (503 when no secret is configured)
3. headers and JSON                 → 400
4. ``ping``                         → 200, nothing stored
5. routing: no job for this event   → 204, nothing stored; a handled event missing a field → 400
6. delivery + job, one transaction  → 202, or 200 for a duplicate; 503 if the database is down
   or does not answer within ``enqueue_timeout`` (5 s, below GitHub's 10 s)
"""

import asyncio
import json
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from cicd_doctor.adapters.github.webhooks import (
    InvalidPayloadError,
    installation_id,
    route_event,
    verify_signature,
)
from cicd_doctor.api.deps import get_dependencies
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import Delivery

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 25 * 1024 * 1024  # SPEC §4.4

router = APIRouter()


class WebhookResponse(BaseModel):
    status: Literal["accepted", "duplicate", "pong"]


def _status(status: Literal["accepted", "duplicate", "pong"], code: int) -> JSONResponse:
    return JSONResponse(WebhookResponse(status=status).model_dump(), status_code=code)


def _error(code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=code)


@router.post(
    "/webhooks/github",
    status_code=202,
    response_model=WebhookResponse,
    responses={
        200: {"description": "Duplicate delivery or ping: nothing new was stored"},
        204: {"description": "Event not handled: nothing stored"},
        400: {"description": "Missing headers, invalid JSON or invalid payload"},
        401: {"description": "Missing or invalid X-Hub-Signature-256"},
        413: {"description": "Body larger than 25 MB"},
        503: {"description": "Database unavailable or webhook secret not configured"},
    },
)
async def github_webhook(request: Request) -> Response:
    deps = get_dependencies(request)

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _error(413, "payload too large")
    body = await _read_body(request, MAX_BODY_BYTES)
    if body is None:
        return _error(413, "payload too large")

    if not deps.webhook_secrets:
        logger.error("webhook refused: GITHUB_WEBHOOK_SECRET is not configured")
        return _error(503, "webhook secret not configured")
    if not verify_signature(body, request.headers.get("x-hub-signature-256"), deps.webhook_secrets):
        # Nothing from an unverified request is logged: its headers are attacker-controlled.
        logger.warning("webhook refused: missing or invalid signature")
        return _error(401, "invalid signature")

    event = request.headers.get("x-github-event")
    if not event:
        return _error(400, "missing X-GitHub-Event")
    try:
        delivery_id = UUID(request.headers.get("x-github-delivery") or "")
    except ValueError:
        return _error(400, "missing or invalid X-GitHub-Delivery")
    try:
        payload = json.loads(body)
    except ValueError:
        return _error(400, "body is not valid JSON")
    if not isinstance(payload, dict):
        return _error(400, "body is not a JSON object")
    action = payload.get("action") if isinstance(payload.get("action"), str) else None
    label = f"{event}.{action}" if action else event

    if event == "ping":
        logger.info("webhook %s %s: pong", delivery_id, label)
        return _status("pong", 200)

    try:
        job = route_event(
            event, payload, received_at=deps.clock.now(), max_attempts=deps.max_attempts
        )
    except InvalidPayloadError as exc:
        logger.warning("webhook %s %s rejected: %s", delivery_id, label, exc)
        return _error(400, str(exc))
    if job is None:
        logger.info("webhook %s %s: not handled", delivery_id, label)
        return Response(status_code=204)

    delivery = Delivery(
        delivery_id=delivery_id,
        event=event,
        action=action,
        installation_id=installation_id(payload),
    )
    try:
        async with asyncio.timeout(deps.enqueue_timeout.total_seconds()):
            created = await deps.queue.enqueue(job, delivery)
    except (TransientError, TimeoutError) as exc:
        # If the commit went through after all, a redelivery is a harmless duplicate (D-06).
        logger.error("webhook %s %s not stored: %s", delivery_id, label, type(exc).__name__)
        return _error(503, "database unavailable, redeliver later")
    if not created:
        logger.info("webhook %s %s: duplicate", delivery_id, label)
        return _status("duplicate", 200)
    logger.info("webhook %s %s: enqueued %s", delivery_id, label, job.kind)
    return _status("accepted", 202)


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The raw body, or None as soon as it grows past ``limit`` (covers chunked uploads)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
