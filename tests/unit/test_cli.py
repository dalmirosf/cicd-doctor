import asyncio
from typing import Any

import pytest

from cicd_doctor.app import cli


def test_api_command_starts_uvicorn_on_loopback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert cli.main(["api"]) == 0

    assert calls == [
        ("cicd_doctor.api.main:app", {"host": "127.0.0.1", "port": 8000, "reload": False})
    ]


def test_api_command_accepts_host_port_and_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))

    cli.main(["api", "--host", "0.0.0.0", "--port", "9000", "--reload"])

    assert calls[0][1] == {"host": "0.0.0.0", "port": 9000, "reload": True}


def test_worker_command_serves_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "cicd_doctor.app.container.serve_worker", lambda: calls.append("serve_worker")
    )

    assert cli.main(["worker"]) == 0

    assert calls == ["serve_worker"]


def test_the_worker_uses_a_selector_event_loop() -> None:
    """psycopg's async mode refuses Windows' default Proactor loop (D-52)."""
    from cicd_doctor.app.container import event_loop_factory

    loop = event_loop_factory()()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])

    assert exit_info.value.code == 2
