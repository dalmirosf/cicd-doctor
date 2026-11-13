from fastapi.testclient import TestClient

from cicd_doctor.api.main import create_app
from tests.webhooks import memory_dependencies


def test_health_returns_ok() -> None:
    client = TestClient(create_app(memory_dependencies()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_is_404() -> None:
    client = TestClient(create_app(memory_dependencies()))

    assert client.get("/nope").status_code == 404


def test_ready_answers_200_when_the_database_answers() -> None:
    client = TestClient(create_app(memory_dependencies(ready=True)))

    response = client.get("/ready")

    assert (response.status_code, response.json()) == (200, {"status": "ready"})


def test_ready_answers_503_when_the_database_does_not() -> None:
    client = TestClient(create_app(memory_dependencies(ready=False)))

    response = client.get("/ready")

    assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
