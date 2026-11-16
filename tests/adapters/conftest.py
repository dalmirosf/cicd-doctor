"""Fixtures shared by the GitHub adapter tests: a key pair, a clock, FakeGitHub and the client."""

from collections.abc import AsyncIterator

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp
from tests.github_fake import APP_ID, FakeGitHub


@pytest.fixture(scope="session")
def app_key() -> rsa.RSAPrivateKey:
    """The app's key pair, generated for this test session only (never written to disk)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def github(clock: FakeClock, app_key: rsa.RSAPrivateKey) -> FakeGitHub:
    fake = FakeGitHub(clock, app_key.public_key())
    fake.install()
    return fake


@pytest.fixture
async def http(clock: FakeClock, github: FakeGitHub) -> AsyncIterator[GitHubHttp]:
    async with GitHubHttp(clock, transport=github.transport()) as client:
        yield client


@pytest.fixture
def auth(http: GitHubHttp, clock: FakeClock, app_key: rsa.RSAPrivateKey) -> GitHubAppAuth:
    return GitHubAppAuth(http, app_id=APP_ID, private_key=app_key, clock=clock)
