"""Redaction corpus (SPEC §11.1): planted fake secrets and look-alikes that must survive.

The only fake secrets of the repository live here (D-59). They are assembled at import time from
fragments and a seeded random generator, so no secret-shaped literal sits in the source and
GitHub's secret scanning has nothing to flag. Every value is random and invalid.

The hypothesis strategies at the bottom generate the same shapes for the property tests.
"""

import random
import string
from dataclasses import dataclass

from hypothesis import strategies as st

ALNUM = string.ascii_letters + string.digits
UPPER_ALNUM = string.ascii_uppercase + string.digits
URLSAFE = ALNUM + "-_"
B64 = ALNUM + "+/"


@dataclass(frozen=True)
class Planted:
    kind: str  # the placeholder kind expected in the output
    line: str  # a log line with the secret in context
    secret: str  # the part that must not survive redaction


def _random(rng: random.Random, alphabet: str, length: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


def _github_token(rng: random.Random, kind: str) -> str:
    return "gh" + kind + "_" + _random(rng, ALNUM, 36)


def planted_secrets(seed: int = 20261113) -> list[Planted]:
    """At least two contexts for every secret kind of SPEC §9.2."""
    rng = random.Random(seed)

    def r(alphabet: str, length: int) -> str:
        return _random(rng, alphabet, length)

    gho, ghp, ghs, ghu = (_github_token(rng, kind) for kind in ("o", "p", "s", "u"))
    pat = "github" + "_pat_" + r(ALNUM + "_", 82)
    akia = "AK" + "IA" + r(UPPER_ALNUM, 16)
    asia = "AS" + "IA" + r(UPPER_ALNUM, 16)
    aws_secret_1, aws_secret_2 = r(B64, 40), r(B64, 40)
    aiza = "AI" + "za" + r(URLSAFE, 35)
    ya29 = "ya" + "29." + r(URLSAFE, 60)
    openai = "sk-" + "proj-" + r(URLSAFE, 48)
    openai_legacy = "sk-" + r(ALNUM, 48)
    anthropic = "sk-" + "ant-api03-" + r(URLSAFE, 60)
    slack_bot = "xo" + "xb-" + r(string.digits, 12) + "-" + r(ALNUM, 24)
    slack_user = "xo" + "xp-" + r(string.digits, 12) + "-" + r(ALNUM, 24)
    stripe = "sk" + "_live_" + r(ALNUM, 24)
    stripe_restricted = "rk" + "_live_" + r(ALNUM, 24)
    jwt = "ey" + "J" + r(URLSAFE, 30) + "." + "ey" + "J" + r(URLSAFE, 60) + "." + r(URLSAFE, 43)
    pem_body = r(B64, 64)
    url_password_1, url_password_2 = r(ALNUM, 16), r(ALNUM, 24)
    bearer, basic = r(URLSAFE + ".", 40), r(B64, 28) + "=="
    db_password = r(ALNUM + "#$%!", 16)
    client_secret = r(ALNUM, 32)
    api_key = r(ALNUM, 20)
    hexes = "0123456789abcdef"
    codecov = "-".join(
        [r(hexes, 8), r(hexes, 4), "4" + r(hexes, 3), "a" + r(hexes, 3), r(hexes, 12)]
    )  # Codecov upload tokens are UUIDs
    begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    return [
        Planted("github_token", f"remote: Using token {ghp} for acme/widgets", ghp),
        Planted("github_token", f"GH_TOKEN={gho}", gho),
        Planted("github_token", f"echo '{ghu}' | gh auth login --with-token", ghu),
        Planted(
            "url_credentials",
            f"git clone https://x-access-token:{ghs}@github.com/acme/widgets.git",
            ghs,
        ),
        Planted("github_pat", f"Authenticating with {pat}", pat),
        Planted("github_pat", f'GITHUB_PAT: "{pat}"', pat),
        Planted("aws_access_key_id", f"AWS_ACCESS_KEY_ID={akia}", akia),
        Planted("aws_access_key_id", f"Found credentials ({asia}) in environment", asia),
        Planted("aws_secret_access_key", f"aws_secret_access_key = {aws_secret_1}", aws_secret_1),
        Planted("aws_secret_access_key", f'AWS_SECRET_ACCESS_KEY: "{aws_secret_2}"', aws_secret_2),
        Planted("google_api_key", f"GET https://maps.example.com/api?key={aiza}&q=x", aiza),
        Planted("google_api_key", f"GOOGLE_API_KEY={aiza}", aiza),
        Planted("google_oauth_token", f"curl -H 'X-Token: {ya29}'", ya29),
        Planted("google_oauth_token", f"gcloud token: {ya29}", ya29),
        Planted(
            "private_key",
            f'  "private_key": "{begin}\\n{pem_body}\\n{end}\\n",',
            pem_body,
        ),
        Planted("private_key", f"key={begin} {pem_body} {end}", pem_body),
        Planted("openai_api_key", f"OPENAI_API_KEY={openai}", openai),
        Planted("openai_api_key", f"Incorrect API key provided: {openai_legacy}.", openai_legacy),
        Planted("anthropic_api_key", f"ANTHROPIC_API_KEY={anthropic}", anthropic),
        Planted("anthropic_api_key", f"x-api-key: {anthropic}", anthropic),
        Planted("slack_token", f"posting to Slack with {slack_bot}", slack_bot),
        Planted("slack_token", f"SLACK_USER_TOKEN: {slack_user}", slack_user),
        Planted("stripe_key", f"STRIPE_KEY={stripe}", stripe),
        Planted("stripe_key", f"using restricted key {stripe_restricted}", stripe_restricted),
        Planted("jwt", f"id_token={jwt}", jwt),
        Planted("jwt", f"OIDC token received: {jwt}", jwt),
        Planted(
            "url_credentials",
            f"pulling from https://deploy:{url_password_1}@registry.example.com/v2/",
            url_password_1,
        ),
        Planted(
            "url_credentials",
            f"DATABASE_URL=postgresql://app:{url_password_2}@db.internal:5432/app",
            url_password_2,
        ),
        Planted("authorization", f"curl -H 'Authorization: Bearer {bearer}' https://x", bearer),
        Planted("authorization", f"> authorization: Basic {basic}", basic),
        Planted("secret", f"DB_PASSWORD={db_password}", db_password),
        Planted("secret", f"client_secret: {client_secret}", client_secret),
        Planted("secret", f'{{"api_key": "{api_key}"}}', api_key),
        Planted("secret", f"CODECOV_TOKEN={codecov}", codecov),
    ]


def planted_pem_block(seed: int = 7) -> tuple[list[str], list[str]]:
    """A multi-line PEM key as it appears in a log: (lines, the body lines that must go)."""
    rng = random.Random(seed)
    body = [_random(rng, B64, 64) for _ in range(6)] + [_random(rng, B64, 20) + "=="]
    lines = [
        "Writing deploy key to ~/.ssh/id_rsa",
        "-----BEGIN RSA PRIVATE KEY-----",
        *body,
        "-----END RSA PRIVATE KEY-----",
        "Done",
    ]
    return lines, body


def pem_edge_cases(seed: int = 11) -> tuple[list[str], list[str]]:
    """Two PEM shapes: a key whose ``END`` line never comes, and one with text around its
    ``BEGIN`` and ``END`` markers. Returns (unterminated lines, surrounded lines)."""
    rng = random.Random(seed)
    unterminated = ["-----BEGIN OPENSSH PRIVATE KEY-----", _random(rng, B64, 24), "Done."]
    surrounded = [
        "key: -----BEGIN EC PRIVATE KEY-----",
        _random(rng, B64, 44),
        "-----END EC PRIVATE KEY----- (written)",
    ]
    return unterminated, surrounded


def generic_threshold_cases(seed: int = 13) -> list[tuple[str, bool]]:
    """(value, redacted?) after ``password=``: the entropy threshold depends on the length."""
    rng = random.Random(seed)
    return [
        ("change" + "me1", True),  # 9 chars, close to the maximum entropy for its length
        (_random(rng, ALNUM, 12), True),
        ("aaaabbbb", False),
        ("x" * 19, False),
        ("x" * 20, True),  # >= 20 chars: redacted whatever the entropy
    ]


# Fixed snippets with secrets, mixed into the idempotence property test.
SECRET_SNIPPETS: tuple[str, ...] = (
    "https://user:" + "s3cret" + "pass@host/path",
    "Authorization: " + "Bearer abc.def",
    "-----BEGIN PRIVATE KEY-----",
    "-----END PRIVATE KEY-----",
)


# Lines that look like secrets but are not: they must come out unchanged (SPEC §9.2).
LOOKALIKES: tuple[str, ...] = (
    "HEAD is now at 5f1c2a9e4b7d3c6a8f0e1d2b3c4a5f6e7d8c9b0a Merge pull request #12 from x/y",
    "commit 3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c (HEAD -> main, origin/main)",
    "Download action repository 'actions/checkout@v5' (SHA:08c6903cd8c0fde910a37f88322edcfb5dd907a8)",  # noqa: E501
    "GITHUB_SHA=5f1c2a9e4b7d3c6a8f0e1d2b3c4a5f6e7d8c9b0a",
    "request-id: 123e4567-e89b-12d3-a456-426614174000",
    "uuid=550e8400-e29b-41d4-a716-446655440000 run_id=19000000001",
    "Created container 6f0a1b2c-3d4e-4f5a-8b9c-0d1e2f3a4b5c",
    "#4 [1/4] FROM docker.io/library/python:3.13-slim@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419",  # noqa: E501
    "trace_id 4bf92f3577b34da6a3ce929d0e0e4736 span_id 00f067aa0ba902b7",
    "  token: ***",
    "GITHUB_TOKEN: ***",
    "password: ***",
    # The checkout step of every real job prints this line, with GitHub's mask as the value.
    "[command]/usr/bin/git config --file /home/runner/work/_temp/git-credentials-3f1ba908-846b-41f2-8715-6d760b8d8500.config http.https://github.com/.extraheader AUTHORIZATION: basic ***",  # noqa: E501
    "Authorization: Bearer ***",
    "AWS_SECRET_ACCESS_KEY: ***",
    "git clone https://x-access-token:***@github.com/acme/widgets.git",
    "max_tokens: 800",
    "tokenizer: o200k_base",
    "secret_name: deploy-key",
    "Set password for user deploy",
    "password=${DB_PASSWORD}",
    "PWD=/home/runner/work/widgets/widgets",
    "Downloading https://files.pythonhosted.org/packages/ab/cd/requests-2.32.3-py3-none-any.whl",
    "risk-assessment-report-generator-v2 passed",
    "Cache restored from key: setup-uv-1-x86_64-unknown-linux-gnu-3.13.7-9f8e7d6c5b4a",
    "##[error]Process completed with exit code 1.",
)


# --- hypothesis strategies: one per secret shape --------------------------------------------


def _chars(alphabet: str, min_size: int, max_size: int) -> st.SearchStrategy[str]:
    return st.text(alphabet=alphabet, min_size=min_size, max_size=max_size)


SECRET_SHAPES: dict[str, st.SearchStrategy[str]] = {
    "github_token": st.builds(
        lambda kind, body: f"gh{kind}_{body}", st.sampled_from("pousr"), _chars(ALNUM, 36, 60)
    ),
    "github_pat": _chars(ALNUM + "_", 22, 90).map(lambda body: "github" + "_pat_" + body),
    "aws_access_key_id": st.builds(
        lambda prefix, body: prefix + body,
        st.sampled_from(["AK" + "IA", "AS" + "IA"]),
        _chars(UPPER_ALNUM, 16, 16),
    ),
    "google_api_key": _chars(URLSAFE, 35, 45).map(lambda body: "AI" + "za" + body),
    "google_oauth_token": _chars(URLSAFE, 10, 80).map(lambda body: "ya" + "29." + body),
    "openai_api_key": _chars(URLSAFE, 20, 80).map(lambda body: "sk-" + body),
    "anthropic_api_key": _chars(URLSAFE, 20, 80).map(lambda body: "sk-" + "ant-" + body),
    "slack_token": st.builds(
        lambda kind, body: f"xox{kind}-{body}", st.sampled_from("abpr"), _chars(ALNUM + "-", 10, 60)
    ),
    "stripe_key": st.builds(
        lambda kind, body: f"{kind}k_live_{body}", st.sampled_from("sr"), _chars(ALNUM, 16, 40)
    ),
    "jwt": st.builds(
        lambda a, b, c: f"eyJ{a}.{b}.{c}",
        _chars(URLSAFE, 1, 40),
        _chars(URLSAFE, 1, 80),
        _chars(URLSAFE, 1, 60),
    ),
}

# Values whose context (a key name or a header) is what makes them secret.
SECRET_KEYS = (
    "token",
    "GITHUB_TOKEN",
    "secret",
    "password",
    "DB_PASSWORD",
    "passwd",
    "pwd",
    "api_key",
    "apiKey",
    "api-key",
    "access_key",
    "client_secret",
    "clientSecret",
)
SEPARATORS = ("=", ": ", " = ", '="', "='", '": "')
GENERIC_VALUES = _chars(ALNUM + "+_.-", 20, 60)  # >= 20 chars: redacted whatever the entropy

# Surrounding words: none of them can merge with a secret into a longer token.
WORDS = st.lists(
    st.sampled_from(
        ["Run", "uv", "sync", "error:", "##[group]", "found", "export", "echo", "->", "(", ")"]
    ),
    max_size=4,
).map(" ".join)
SPACERS = st.sampled_from([" ", "\t", " (", " '", ' "', " [", "; "])
