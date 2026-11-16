"""Postgres ``FailureStore`` (SPEC §6, D-69, D-70).

SQLAlchemy 2 Core (async) on psycopg 3 with explicit SQL, like the queue (D-28). Times come from
the injected ``Clock`` and travel as parameters (D-48), so a ``FakeClock`` drives the cache expiry.

* ``save_run_analysis`` writes the run, the attempt, the failures and their diagnoses in one
  transaction that also sets ``run_attempts.analyzed_at``. The first writer wins; a second call
  returns what is stored. ``run_analysis`` rebuilds the record from the rows.
* A diagnosis is one row per source: always the ``rules`` row, and the ``llm`` (or ``cache``) row
  only when that is what is published. Exactly one row per failure is ``published`` (partial
  unique index), and the LLM usage sits on that row.
* ``publication_lock`` is a session-level advisory lock on a pool connection held for the whole
  critical section; the database drops it if the connection dies.
* ``similar`` arrives with the embeddings (P2).
"""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import Any, Self
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from cicd_doctor.adapters.postgres.engine import make_engine, transient_db_errors
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import (
    Category,
    Diagnosis,
    DiagnosisUsage,
    Evidence,
    FailedJob,
    Failure,
    FailureStatus,
    JobRef,
    LogExcerpt,
    Publication,
    PublicationKey,
    PublicationKind,
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    Signature,
    SimilarFailure,
    StoredFailure,
    Suggestion,
    WorkflowRun,
)
from cicd_doctor.core.ports import Clock

logger = logging.getLogger(__name__)

DEFAULT_LOCK_WAIT = timedelta(seconds=30)
_LOCK_NOT_AVAILABLE = "55P03"  # lock_timeout expired

# --- runs, attempts and the checkpoint ----------------------------------------------------

_UPSERT_RUN = text(
    """
    INSERT INTO workflow_runs (id, repository_id, workflow_id, workflow_name, head_sha,
                               head_branch, head_repo_full_name, event, is_fork, pr_number,
                               created_at)
    VALUES (:id, :repository_id, :workflow_id, :workflow_name, :head_sha, :head_branch,
            :head_repo_full_name, :event, :is_fork, :pr_number, :now)
    ON CONFLICT (id) DO UPDATE
    SET repository_id = EXCLUDED.repository_id, workflow_id = EXCLUDED.workflow_id,
        workflow_name = EXCLUDED.workflow_name, head_sha = EXCLUDED.head_sha,
        head_branch = EXCLUDED.head_branch, head_repo_full_name = EXCLUDED.head_repo_full_name,
        event = EXCLUDED.event, is_fork = EXCLUDED.is_fork, pr_number = EXCLUDED.pr_number
    """
)
# An analysed attempt is final: nothing rewrites it, and ``analyzed_at`` is never touched here.
_UPSERT_ATTEMPT = text(
    """
    INSERT INTO run_attempts (run_id, attempt, conclusion, completed_at)
    VALUES (:run_id, :attempt, :conclusion, :completed_at)
    ON CONFLICT (run_id, attempt) DO UPDATE
    SET conclusion = EXCLUDED.conclusion, completed_at = EXCLUDED.completed_at
    WHERE run_attempts.analyzed_at IS NULL
    """
)
# The checkpoint: one writer flips ``analyzed_at`` from NULL; a concurrent one waits for its
# commit, re-checks the condition and updates nothing.
_CLAIM_ATTEMPT = text(
    """
    UPDATE run_attempts SET analyzed_at = :now
    WHERE run_id = :run_id AND attempt = :attempt AND analyzed_at IS NULL
    """
)
_UPSERT_FAILURE = text(
    """
    INSERT INTO failures (id, repository_id, run_id, run_attempt, job_id, job_name, step_name,
                          step_number, signature_hash, signature_text, excerpt, excerpt_tokens,
                          truncated, embedding, status, fix_commit_sha, created_at, conclusion,
                          runner_os, started_at, completed_at, step_started_at,
                          step_completed_at)
    VALUES (:id, :repository_id, :run_id, :run_attempt, :job_id, :job_name, :step_name,
            :step_number, :signature_hash, :signature_text, CAST(:excerpt AS jsonb),
            :excerpt_tokens, :truncated, CAST(:embedding AS vector),
            CAST(:status AS failure_status), :fix_commit_sha, :now, :conclusion, :runner_os,
            :started_at, :completed_at, :step_started_at, :step_completed_at)
    ON CONFLICT (id) DO UPDATE
    SET repository_id = EXCLUDED.repository_id, run_id = EXCLUDED.run_id,
        run_attempt = EXCLUDED.run_attempt, job_id = EXCLUDED.job_id,
        job_name = EXCLUDED.job_name, step_name = EXCLUDED.step_name,
        step_number = EXCLUDED.step_number, signature_hash = EXCLUDED.signature_hash,
        signature_text = EXCLUDED.signature_text, excerpt = EXCLUDED.excerpt,
        excerpt_tokens = EXCLUDED.excerpt_tokens, truncated = EXCLUDED.truncated,
        embedding = EXCLUDED.embedding, status = EXCLUDED.status,
        fix_commit_sha = EXCLUDED.fix_commit_sha, conclusion = EXCLUDED.conclusion,
        runner_os = EXCLUDED.runner_os, started_at = EXCLUDED.started_at,
        completed_at = EXCLUDED.completed_at, step_started_at = EXCLUDED.step_started_at,
        step_completed_at = EXCLUDED.step_completed_at
    """
)
_INSERT_DIAGNOSIS = text(
    """
    INSERT INTO diagnoses (failure_id, source, published, category, probable_cause, evidence,
                           evidence_verified, suggested_fix, suggestion, confidence, rule_ids,
                           model, prompt_version, input_tokens, output_tokens, cost_usd,
                           latency_ms, llm_calls, llm_failed_calls, fallback_reason, created_at)
    VALUES (:failure_id, CAST(:source AS diagnosis_source), :published,
            CAST(:category AS failure_category), :probable_cause, CAST(:evidence AS jsonb),
            :evidence_verified, :suggested_fix, CAST(:suggestion AS jsonb), :confidence,
            CAST(:rule_ids AS text[]), :model, :prompt_version, :input_tokens, :output_tokens,
            :cost_usd, :latency_ms, :llm_calls, :llm_failed_calls, :fallback_reason, :now)
    """
)

_FAILURE_COLUMNS = """
    f.id, f.repository_id, f.run_id, f.run_attempt, f.job_id, f.job_name, f.step_name,
    f.step_number, f.signature_hash, f.signature_text, f.excerpt, f.embedding::text AS embedding,
    f.status, f.fix_commit_sha, f.conclusion, f.runner_os, f.started_at, f.completed_at,
    f.step_started_at, f.step_completed_at,
    p.installation_id, p.full_name AS repo_full_name,
    r.workflow_id, r.workflow_name, r.head_sha, r.head_branch, r.head_repo_full_name, r.event,
    r.is_fork, r.pr_number
"""
_FAILURE_JOINS = """
    FROM failures f
    JOIN workflow_runs r ON r.id = f.run_id
    JOIN repositories p ON p.id = f.repository_id
"""
_ANALYSIS_HEAD = text(
    """
    SELECT r.id, r.repository_id, r.workflow_id, r.workflow_name, r.head_sha, r.head_branch,
           r.head_repo_full_name, r.event, r.is_fork, r.pr_number,
           a.attempt, a.conclusion, a.completed_at
    FROM run_attempts a JOIN workflow_runs r ON r.id = a.run_id
    WHERE a.run_id = :run_id AND a.attempt = :attempt AND a.analyzed_at IS NOT NULL
    """
)
_ANALYSIS_FAILURES = text(
    f"""
    SELECT {_FAILURE_COLUMNS} {_FAILURE_JOINS}
    WHERE f.run_id = :run_id AND f.run_attempt = :attempt
    ORDER BY f.job_id
    """
)
_DIAGNOSIS_COLUMNS = """
    d.source, d.published, d.category, d.probable_cause, d.evidence, d.evidence_verified,
    d.suggested_fix, d.suggestion, d.confidence, d.rule_ids, d.model, d.prompt_version,
    d.input_tokens, d.output_tokens, d.cost_usd, d.latency_ms, d.llm_calls, d.llm_failed_calls,
    d.fallback_reason
"""
_ANALYSIS_DIAGNOSES = text(
    f"""
    SELECT d.failure_id, {_DIAGNOSIS_COLUMNS}
    FROM diagnoses d JOIN failures f ON f.id = d.failure_id
    WHERE f.run_id = :run_id AND f.run_attempt = :attempt
    ORDER BY d.created_at, d.published
    """
)

# --- cache and open failures --------------------------------------------------------------

_CACHED_DIAGNOSIS = text(
    f"""
    SELECT {_DIAGNOSIS_COLUMNS}
    FROM diagnoses d JOIN failures f ON f.id = d.failure_id
    WHERE f.repository_id = :repo_id AND f.signature_hash = :signature_hash
      AND d.source = 'llm' AND d.created_at >= :oldest_allowed
    ORDER BY d.created_at DESC
    LIMIT 1
    """
)
_OPEN_FAILURES = text(
    f"""
    SELECT {_FAILURE_COLUMNS} {_FAILURE_JOINS}
    WHERE f.repository_id = :repo_id AND r.workflow_id = :workflow_id AND f.status = 'open'
      AND r.head_repo_full_name || ':' || COALESCE(r.head_branch, '') = :branch_key
    ORDER BY f.created_at, f.id
    """
)
_RESOLVE = text(
    """
    UPDATE failures
    SET status = CAST(:status AS failure_status), fix_commit_sha = :fix_commit_sha,
        resolved_at = :resolved_at
    WHERE id = ANY(CAST(:ids AS uuid[]))
    """
)

# --- publications (idempotent outputs) ----------------------------------------------------

_GET_CHECK_PUBLICATION = text(
    """
    SELECT check_run_id AS external_id, run_id, last_attempt AS attempt
    FROM check_publications WHERE repository_id = :repo_id AND run_id = :run_id
    """
)
_GET_COMMENT_PUBLICATION = text(
    """
    SELECT comment_id AS external_id, last_run_id AS run_id, last_attempt AS attempt
    FROM comment_publications
    WHERE repository_id = :repo_id AND pr_number = :pr_number AND workflow_id = :workflow_id
    """
)
# Newer-attempt guard (SPEC §6): the row is replaced only by the same or a newer attempt, and
# RETURNING says whether that happened. A check run has one run id, so its order is the attempt.
_SAVE_CHECK_PUBLICATION = text(
    """
    INSERT INTO check_publications (repository_id, run_id, check_run_id, last_attempt, updated_at)
    VALUES (:repo_id, :run_id, :external_id, :attempt, :now)
    ON CONFLICT (repository_id, run_id) DO UPDATE
    SET check_run_id = EXCLUDED.check_run_id, last_attempt = EXCLUDED.last_attempt,
        updated_at = EXCLUDED.updated_at
    WHERE check_publications.last_attempt <= EXCLUDED.last_attempt
    RETURNING 1
    """
)
_SAVE_COMMENT_PUBLICATION = text(
    """
    INSERT INTO comment_publications (repository_id, pr_number, workflow_id, comment_id,
                                      last_run_id, last_attempt, updated_at)
    VALUES (:repo_id, :pr_number, :workflow_id, :external_id, :run_id, :attempt, :now)
    ON CONFLICT (repository_id, pr_number, workflow_id) DO UPDATE
    SET comment_id = EXCLUDED.comment_id, last_run_id = EXCLUDED.last_run_id,
        last_attempt = EXCLUDED.last_attempt, updated_at = EXCLUDED.updated_at
    WHERE (comment_publications.last_run_id, comment_publications.last_attempt)
          <= (EXCLUDED.last_run_id, EXCLUDED.last_attempt)
    RETURNING 1
    """
)

# lock_timeout bounds the wait of pg_advisory_lock; ``true`` keeps the setting local to the
# transaction, so it never leaks to the next user of the pooled connection.
_SET_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :milliseconds, true)")
_LOCK = text("SELECT pg_advisory_lock(hashtextextended(CAST(:key AS text), 0))")
_UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(CAST(:key AS text), 0))")


class PostgresFailureStore:
    """Implements ``FailureStore``. Create it with ``PostgresFailureStore.connect(...)``."""

    def __init__(
        self, engine: AsyncEngine, clock: Clock, *, lock_wait: timedelta = DEFAULT_LOCK_WAIT
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._lock_wait = lock_wait

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        database_url: str,
        clock: Clock,
        *,
        pool_size: int = 5,
        lock_wait: timedelta = DEFAULT_LOCK_WAIT,
    ) -> AsyncIterator[Self]:
        """``database_url`` is a SQLAlchemy URL (``postgresql+psycopg://...``)."""
        engine = make_engine(database_url, pool_size=pool_size)
        try:
            yield cls(engine, clock, lock_wait=lock_wait)
        finally:
            await engine.dispose()

    # --- row by row -------------------------------------------------------------------------

    async def save_run(self, run: WorkflowRun, attempt: RunAttempt) -> None:
        now = self._clock.now()
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await _save_run(conn, run, attempt, now)

    async def save_failure(self, failure: Failure) -> None:
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await conn.execute(_UPSERT_FAILURE, _failure_params(failure, self._clock.now()))

    async def save_diagnosis(self, failure_id: UUID, diagnosis: Diagnosis) -> None:
        params = _diagnosis_params(failure_id, diagnosis, False, None, self._clock.now())
        try:
            with transient_db_errors():
                async with self._engine.begin() as conn:
                    await conn.execute(_INSERT_DIAGNOSIS, params)
        except sa_exc.IntegrityError as exc:  # the only constraint a new row can break is the FK
            raise KeyError(f"unknown failure {failure_id}") from exc

    # --- the checkpoint ---------------------------------------------------------------------

    async def save_run_analysis(self, record: RunAnalysisRecord) -> RunAnalysisRecord:
        now = self._clock.now()
        run_id, attempt = record.run.id, record.attempt.attempt
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await _save_run(conn, record.run, record.attempt, now)
                claimed = await conn.execute(
                    _CLAIM_ATTEMPT, {"run_id": run_id, "attempt": attempt, "now": now}
                )
                if claimed.rowcount == 1:  # the first writer; the others only read
                    await self._insert_items(conn, record.items, now)
                stored = await _load_analysis(conn, run_id, attempt)
        if stored is None:  # pragma: no cover  (the claim above makes it impossible)
            raise RuntimeError(f"run {run_id} attempt {attempt} has no analysis after saving it")
        return stored

    async def run_analysis(self, run_id: int, attempt: int) -> RunAnalysisRecord | None:
        with transient_db_errors():
            async with self._engine.connect() as conn:
                return await _load_analysis(conn, run_id, attempt)

    async def _insert_items(
        self, conn: AsyncConnection, items: Sequence[StoredFailure], now: datetime
    ) -> None:
        if not items:
            return
        await conn.execute(_UPSERT_FAILURE, [_failure_params(item.failure, now) for item in items])
        await conn.execute(
            _INSERT_DIAGNOSIS,
            [
                _diagnosis_params(
                    item.failure.id,
                    diagnosis,
                    published,
                    item.usage if published else None,
                    now,
                )
                for item in items
                for diagnosis, published in item.diagnosis_rows()
            ],
        )

    # --- cache, similarity, open failures ---------------------------------------------------

    async def cached_diagnosis(
        self, repo_id: int, signature_hash: str, max_age: timedelta
    ) -> Diagnosis | None:
        params = {
            "repo_id": repo_id,
            "signature_hash": signature_hash,
            "oldest_allowed": self._clock.now() - max_age,
        }
        with transient_db_errors():
            async with self._engine.connect() as conn:
                row = (await conn.execute(_CACHED_DIAGNOSIS, params)).first()
        return None if row is None else _to_diagnosis(row._mapping)

    async def similar(
        self, repo_id: int, embedding: Sequence[float], k: int, min_score: float
    ) -> list[SimilarFailure]:
        raise NotImplementedError("P2")

    async def open_failures(self, repo_id: int, workflow_id: int, branch_key: str) -> list[Failure]:
        params = {"repo_id": repo_id, "workflow_id": workflow_id, "branch_key": branch_key}
        with transient_db_errors():
            async with self._engine.connect() as conn:
                rows = (await conn.execute(_OPEN_FAILURES, params)).mappings().all()
        return [_to_failure(row) for row in rows]

    async def resolve(
        self,
        failure_ids: Sequence[UUID],
        status: FailureStatus,
        fix_commit_sha: str | None,
    ) -> None:
        ids = list(dict.fromkeys(failure_ids))
        if not ids:
            return
        params = {
            "ids": ids,
            "status": status.value,
            "fix_commit_sha": fix_commit_sha,
            "resolved_at": None if status is FailureStatus.OPEN else self._clock.now(),
        }
        with transient_db_errors():
            async with self._engine.begin() as conn:
                result = await conn.execute(_RESOLVE, params)
                if result.rowcount != len(ids):  # raising rolls the whole update back
                    raise KeyError("unknown failure among " + ", ".join(map(str, ids)))

    # --- publications -----------------------------------------------------------------------

    async def publication(self, key: PublicationKey) -> Publication | None:
        if key.kind is PublicationKind.CHECK:
            statement, params = (
                _GET_CHECK_PUBLICATION,
                {"repo_id": key.repo_id, "run_id": key.run_id},
            )
        else:
            statement, params = (
                _GET_COMMENT_PUBLICATION,
                {
                    "repo_id": key.repo_id,
                    "pr_number": key.pr_number,
                    "workflow_id": key.workflow_id,
                },
            )
        with transient_db_errors():
            async with self._engine.connect() as conn:
                row = (await conn.execute(statement, params)).mappings().first()
        if row is None:
            return None
        return Publication(
            key=key,
            external_id=row["external_id"],
            run_id=row["run_id"],
            attempt=row["attempt"],
        )

    async def save_publication(self, pub: Publication) -> bool:
        key = pub.key
        params: dict[str, Any] = {
            "repo_id": key.repo_id,
            "external_id": pub.external_id,
            "attempt": pub.attempt,
            "now": self._clock.now(),
        }
        if key.kind is PublicationKind.CHECK:
            statement = _SAVE_CHECK_PUBLICATION
            params["run_id"] = key.run_id
        else:
            statement = _SAVE_COMMENT_PUBLICATION
            params |= {
                "run_id": pub.run_id,
                "pr_number": key.pr_number,
                "workflow_id": key.workflow_id,
            }
        with transient_db_errors():
            async with self._engine.begin() as conn:
                return (await conn.execute(statement, params)).first() is not None

    @asynccontextmanager
    async def publication_lock(self, key: PublicationKey) -> AsyncIterator[None]:
        lock_key = _lock_key(key)
        conn = await self._acquire(lock_key)
        try:
            yield
        finally:
            await self._release(conn, lock_key)

    async def _acquire(self, lock_key: str) -> AsyncConnection:
        """A pooled connection that holds the advisory lock, waiting at most ``lock_wait``."""
        milliseconds = max(1, round(self._lock_wait.total_seconds() * 1000))
        with transient_db_errors():
            conn = await self._engine.connect()
            try:
                await conn.execute(_SET_LOCK_TIMEOUT, {"milliseconds": str(milliseconds)})
                await conn.execute(_LOCK, {"key": lock_key})
                await conn.commit()  # a session lock survives the commit; the timeout is reset
            except BaseException as exc:
                # A statement that was cut short may still be waiting inside the server: drop
                # the whole session instead of returning it to the pool.
                await _discard(conn)
                orig = exc.orig if isinstance(exc, sa_exc.DBAPIError) else None
                if getattr(orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
                    raise TransientError(
                        f"publication lock busy: waited {self._lock_wait.total_seconds():g} s"
                    ) from exc
                raise
        return conn

    async def _release(self, conn: AsyncConnection, lock_key: str) -> None:
        """Unlock and return the connection. A connection that died took its lock with it."""
        released = False
        try:
            released = bool((await conn.execute(_UNLOCK, {"key": lock_key})).scalar())
            await conn.commit()
        except Exception as exc:  # a dead connection: the server has already dropped the lock
            logger.warning("could not release the publication lock (%s)", type(exc).__name__)
        finally:
            if not released:
                await _discard(conn)
            else:
                await conn.close()


async def _discard(conn: AsyncConnection) -> None:
    """Close the session for good: the server drops every lock it still holds. It never raises,
    so it cannot hide the error that made the caller discard the connection."""
    with suppress(Exception):
        await conn.invalidate()
    with suppress(Exception):
        await conn.close()


# --- helpers ------------------------------------------------------------------------------


def _lock_key(key: PublicationKey) -> str:
    """The stable text that is hashed into the advisory lock id."""
    parts = (key.kind.value, key.repo_id, key.run_id, key.pr_number, key.workflow_id)
    return "cicd-doctor:publication:" + ":".join(
        "-" if part is None else str(part) for part in parts
    )


async def _save_run(
    conn: AsyncConnection, run: WorkflowRun, attempt: RunAttempt, now: datetime
) -> None:
    await conn.execute(
        _UPSERT_RUN,
        {
            "id": run.id,
            "repository_id": run.repo_id,
            "workflow_id": run.workflow_id,
            "workflow_name": run.workflow_name,
            "head_sha": run.head_sha,
            "head_branch": run.head_branch,
            "head_repo_full_name": run.head_repo_full_name,
            "event": run.event,
            "is_fork": run.is_fork,
            "pr_number": run.pr_number,
            "now": now,
        },
    )
    await conn.execute(
        _UPSERT_ATTEMPT,
        {
            "run_id": attempt.run_id,
            "attempt": attempt.attempt,
            "conclusion": attempt.conclusion,
            "completed_at": attempt.completed_at,
        },
    )


def _failure_params(failure: Failure, now: datetime) -> dict[str, Any]:
    job = failure.job
    run = job.job.run
    return {
        "id": failure.id,
        "repository_id": run.repo_id,
        "run_id": run.run_id,
        "run_attempt": run.run_attempt,
        "job_id": job.job.job_id,
        "job_name": job.job.job_name,
        "step_name": job.step_name,
        "step_number": job.step_number,
        "signature_hash": failure.signature.hash,
        "signature_text": failure.signature.text,
        # The job (and its run) is the failure's own: the excerpt keeps only what is its own.
        "excerpt": json.dumps(failure.excerpt.model_dump(mode="json", exclude={"job"})),
        "excerpt_tokens": failure.excerpt.token_count,
        "truncated": failure.excerpt.truncated,
        "embedding": _vector_text(failure.embedding),
        "status": failure.status.value,
        "fix_commit_sha": failure.fix_commit_sha,
        "now": now,
        "conclusion": job.conclusion,
        "runner_os": job.runner_os,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "step_started_at": job.step_started_at,
        "step_completed_at": job.step_completed_at,
    }


def _diagnosis_params(
    failure_id: UUID,
    diagnosis: Diagnosis,
    published: bool,
    usage: DiagnosisUsage | None,
    now: datetime,
) -> dict[str, Any]:
    # ``model`` is the model of the answer. A rules row has none of its own: the model of the
    # failed calls, if any, belongs to the usage and travels in this column (see _to_diagnosis).
    model = diagnosis.model if usage is None or diagnosis.source != "rules" else usage.model
    suggestion = diagnosis.suggestion
    return {
        "failure_id": failure_id,
        "source": diagnosis.source,
        "published": published,
        "category": diagnosis.category.value,
        "probable_cause": diagnosis.probable_cause,
        "evidence": json.dumps([item.model_dump(mode="json") for item in diagnosis.evidence]),
        "evidence_verified": diagnosis.evidence_verified,
        "suggested_fix": diagnosis.suggested_fix,
        "suggestion": None if suggestion is None else suggestion.model_dump_json(),
        "confidence": diagnosis.confidence,
        "rule_ids": list(diagnosis.rule_ids),
        "model": model,
        "prompt_version": diagnosis.prompt_version,
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "cost_usd": None if usage is None else usage.cost_usd,
        "latency_ms": None if usage is None else usage.latency_ms,
        "llm_calls": None if usage is None else usage.calls,
        "llm_failed_calls": None if usage is None else usage.failed_calls,
        "fallback_reason": diagnosis.fallback_reason,
        "now": now,
    }


def _vector_text(embedding: Sequence[float] | None) -> str | None:
    """pgvector text input: ``[1,2,3]``. pgvector keeps 32-bit floats."""
    return None if embedding is None else "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _parse_vector(value: str | None) -> list[float] | None:
    return None if value is None else [float(x) for x in value.strip("[]").split(",")]


async def _load_analysis(
    conn: AsyncConnection, run_id: int, attempt: int
) -> RunAnalysisRecord | None:
    params = {"run_id": run_id, "attempt": attempt}
    head = (await conn.execute(_ANALYSIS_HEAD, params)).mappings().first()
    if head is None:
        return None
    failures = (await conn.execute(_ANALYSIS_FAILURES, params)).mappings().all()
    by_failure: dict[UUID, list[RowMapping]] = {}
    for row in (await conn.execute(_ANALYSIS_DIAGNOSES, params)).mappings():
        by_failure.setdefault(row["failure_id"], []).append(row)
    return RunAnalysisRecord(
        run=WorkflowRun(
            id=head["id"],
            repo_id=head["repository_id"],
            workflow_id=head["workflow_id"],
            workflow_name=head["workflow_name"],
            head_sha=head["head_sha"],
            head_branch=head["head_branch"],
            head_repo_full_name=head["head_repo_full_name"],
            event=head["event"],
            is_fork=head["is_fork"],
            pr_number=head["pr_number"],
        ),
        attempt=RunAttempt(
            run_id=head["id"],
            attempt=head["attempt"],
            conclusion=head["conclusion"],
            completed_at=head["completed_at"],
        ),
        items=[_to_stored_failure(row, by_failure.get(row["id"], [])) for row in failures],
    )


def _to_stored_failure(failure_row: RowMapping, diagnosis_rows: list[RowMapping]) -> StoredFailure:
    published_row = next((row for row in diagnosis_rows if row["published"]), None)
    if published_row is None:  # a half-written failure: the checkpoint never leaves one
        raise RuntimeError(f"failure {failure_row['id']} has no published diagnosis")
    published = _to_diagnosis(published_row)
    rules_row = next((row for row in diagnosis_rows if not row["published"]), None)
    # When the rules diagnosis is the published one, it is the same row plus its fallback reason.
    rules = (
        published.model_copy(update={"fallback_reason": None})
        if rules_row is None
        else _to_diagnosis(rules_row)
    )
    usage = (
        None
        if published_row["llm_calls"] is None
        else DiagnosisUsage(
            model=published_row["model"],
            calls=published_row["llm_calls"],
            input_tokens=published_row["input_tokens"],
            output_tokens=published_row["output_tokens"],
            latency_ms=published_row["latency_ms"],
            cost_usd=published_row["cost_usd"],
            failed_calls=published_row["llm_failed_calls"],
        )
    )
    return StoredFailure(
        failure=_to_failure(failure_row), published=published, rules=rules, usage=usage
    )


def _to_diagnosis(row: RowMapping) -> Diagnosis:
    suggestion = row["suggestion"]
    return Diagnosis(
        category=Category(row["category"]),
        probable_cause=row["probable_cause"],
        evidence=[Evidence.model_validate(item) for item in row["evidence"]],
        suggested_fix=row["suggested_fix"],
        suggestion=None if suggestion is None else Suggestion.model_validate(suggestion),
        confidence=row["confidence"],
        source=row["source"],
        rule_ids=list(row["rule_ids"]),
        # A rules row keeps the model of its failed calls in the column, not in the diagnosis.
        model=None if row["source"] == "rules" else row["model"],
        prompt_version=row["prompt_version"],
        fallback_reason=row["fallback_reason"],
        evidence_verified=row["evidence_verified"],
    )


def _to_failure(row: RowMapping) -> Failure:
    run = RunRef(
        installation_id=row["installation_id"],
        repo_id=row["repository_id"],
        repo_full_name=row["repo_full_name"],
        run_id=row["run_id"],
        run_attempt=row["run_attempt"],
        workflow_id=row["workflow_id"],
        workflow_name=row["workflow_name"],
        head_sha=row["head_sha"],
        head_branch=row["head_branch"],
        head_repo_full_name=row["head_repo_full_name"],
        event=row["event"],
        is_fork=row["is_fork"],
        pr_number=row["pr_number"],
    )
    job = JobRef(run=run, job_id=row["job_id"], job_name=row["job_name"])
    return Failure(
        id=row["id"],
        job=FailedJob(
            job=job,
            conclusion=row["conclusion"],
            step_name=row["step_name"],
            step_number=row["step_number"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            runner_os=row["runner_os"],
            step_started_at=row["step_started_at"],
            step_completed_at=row["step_completed_at"],
        ),
        excerpt=LogExcerpt.model_validate({**row["excerpt"], "job": job}),
        signature=Signature(text=row["signature_text"], hash=row["signature_hash"]),
        embedding=_parse_vector(row["embedding"]),
        status=FailureStatus(row["status"]),
        fix_commit_sha=row["fix_commit_sha"],
    )
