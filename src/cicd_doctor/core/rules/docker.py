"""Docker build and registry rules (SPEC §10)."""

from cicd_doctor.core.models import Category
from cicd_doctor.core.rules.engine import Rule, compile_all

DOCKER_RULES: tuple[Rule, ...] = (
    Rule(
        id="DOCKER-COPY",
        ecosystem="docker",
        patterns=compile_all(r"failed to compute cache key: .*not found", r"COPY failed"),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.85,
        priority=2,
        cause="A `COPY` in the Dockerfile refers to a file that is not in the build context.",
        fix=(
            "Check the path in the `COPY` instruction, the build context directory, and "
            "`.dockerignore` (an ignored file is not in the context)."
        ),
    ),
    Rule(
        id="DOCKER-BASE",
        ecosystem="docker",
        patterns=compile_all(
            r"manifest unknown", r"pull access denied for", r"failed to resolve source metadata"
        ),
        category=Category.DEPENDENCY,
        confidence=0.80,
        priority=2,
        cause="The base image or tag in `FROM` cannot be pulled: it does not exist or is private.",
        fix=(
            "Check the image name and tag in `FROM`; for a private image, log in to its "
            "registry (`docker/login-action`) before the build."
        ),
    ),
    Rule(
        id="DOCKER-AUTH",
        ecosystem="docker",
        patterns=compile_all(
            r"denied: requested access to the resource is denied",
            r"unauthorized: authentication required",
        ),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.85,
        priority=2,
        cause="The container registry refused the credentials (or there were none).",
        fix=(
            "Log in to the registry before pushing or pulling, and check that the token has "
            "the needed scope (for GHCR, `packages: write` in the workflow permissions)."
        ),
    ),
    Rule(
        id="DOCKER-HUB-LIMIT",
        ecosystem="docker",
        patterns=compile_all(r"toomanyrequests: You have reached your pull rate limit"),
        category=Category.NETWORK,
        confidence=0.90,
        priority=3,
        cause="Docker Hub's anonymous pull rate limit was reached.",
        fix="Log in to Docker Hub before pulling, or pull the image from a mirror or GHCR.",
    ),
    Rule(
        id="DOCKER-RUN-FAIL",
        ecosystem="docker",
        patterns=compile_all(
            r"failed to solve: process .* did not complete successfully: exit code: (?P<code>\d+)"
        ),
        category=Category.INFRA_OR_DEPLOY,
        confidence=0.50,
        priority=1,
        cause="A `RUN` instruction of the Docker build failed with exit code {code}.",
        fix="Look at the output of that `RUN` step above the error to find what failed in it.",
    ),
)
