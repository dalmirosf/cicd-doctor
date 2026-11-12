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


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])

    assert exit_info.value.code == 2
