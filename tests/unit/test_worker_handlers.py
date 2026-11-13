from typing import Any

import pytest
from pydantic import SecretStr

from cicd_doctor.adapters.memory import InMemoryInstallationStore
from cicd_doctor.app import container
from cicd_doctor.app.settings import Settings
from cicd_doctor.core.errors import PermanentError
from cicd_doctor.core.models import (
    Installation,
    InstallationAction,
    InstallationEvent,
    Job,
    JobKind,
    JobStatus,
)
from cicd_doctor.worker.handlers import sync_installation_handler
from tests.factories import T0


def job(payload: dict[str, Any]) -> Job:
    return Job(
        id=1,
        kind=JobKind.SYNC_INSTALLATION,
        payload=payload,
        dedupe_key=None,
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=5,
        run_after=T0,
        locked_until=None,
        locked_by="w",
        last_error=None,
    )


async def test_sync_installation_applies_the_event() -> None:
    store = InMemoryInstallationStore()
    event = InstallationEvent(
        action=InstallationAction.CREATED,
        installation=Installation(id=10, account_login="acme", account_type="Organization"),
        occurred_at=T0,
    )

    await sync_installation_handler(store)(job(event.model_dump(mode="json")))

    assert await store.installation(10) == event.installation


async def test_a_malformed_payload_is_a_permanent_error() -> None:
    with pytest.raises(PermanentError, match="invalid sync_installation payload"):
        await sync_installation_handler(InMemoryInstallationStore())(job({"action": "nope"}))


def test_only_sync_installation_has_a_real_handler_so_far() -> None:
    registry = container.handlers(InMemoryInstallationStore())

    assert set(registry) == set(JobKind)
    real = {
        kind for kind, handler in registry.items() if handler is not container.placeholder_handler
    }
    assert real == {JobKind.SYNC_INSTALLATION}


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        ("now", None, (b"now",)),
        ("now", "before", (b"now", b"before")),
        (None, None, ()),
    ],
)
def test_webhook_secrets_list_the_current_secret_first(
    current: str | None, previous: str | None, expected: tuple[bytes, ...]
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+psycopg://u@127.0.0.1/db"),
        github_webhook_secret=SecretStr(current) if current else None,
        github_webhook_secret_previous=SecretStr(previous) if previous else None,
    )

    assert container.webhook_secrets(settings) == expected
