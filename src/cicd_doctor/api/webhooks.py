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
from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from cicd_doctor.adapters.github.webhooks import (
    InvalidPayloadError,
    installation_id,
    route_event,
    verify_signature,
)
from cicd_doctor.adapters.structured_log import get_logger
from cicd_doctor.api.deps import get_dependencies
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import Delivery

logger = get_logger(__name__)

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
    try:
        return await _handle(request)
    finally:
        # The correlation fields (D-117) belong to this request and to nothing after it.
        structlog.contextvars.clear_contextvars()


def _correlation(payload: dict[str, Any]) -> dict[str, Any]:
    """The ids of a verified payload worth a log field: only ints and the repository name."""
    fields: dict[str, Any] = {}
    installation = payload.get("installation")
    if isinstance(installation, dict) and type(installation.get("id")) is int:
        fields["installation_id"] = installation["id"]
    repository = payload.get("repository")
    if isinstance(repository, dict) and isinstance(repository.get("full_name"), str):
        fields["repository"] = repository["full_name"]
    run = payload.get("workflow_run")
    if isinstance(run, dict) and type(run.get("id")) is int:
        fields["run_id"] = run["id"]
    return fields


async def _handle(request: Request) -> Response:
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
    # Only a verified request reaches this point: nothing before it is bound or logged.
    structlog.contextvars.bind_contextvars(
        delivery_id=str(delivery_id), github_event=label, **_correlation(payload)
    )

    if event == "ping":
        logger.info("webhook pong")
        return _status("pong", 200)

    try:
        job = route_event(
            event, payload, received_at=deps.clock.now(), max_attempts=deps.max_attempts
        )
    except InvalidPayloadError as exc:
        logger.warning("webhook rejected", reason=str(exc))
        return _error(400, str(exc))
    if job is None:
        logger.info("webhook not handled")
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
        logger.error("webhook not stored", error=type(exc).__name__)
        return _error(503, "database unavailable, redeliver later")
    if not created:
        logger.info("webhook duplicate")
        return _status("duplicate", 200)
    logger.info("webhook enqueued", job_kind=str(job.kind))
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
