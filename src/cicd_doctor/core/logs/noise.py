"""Noise filters: log lines that never help a diagnosis (SPEC §7.3 step 4).

A noise line is dropped from the excerpt, unless it is also an error line. Add a pattern here
together with a line it removes in ``tests/unit/test_logs_clean.py`` (``test_noise_lines``).
"""

import re

# Matched with ``search`` against the cleaned line (timestamp and ANSI codes already removed).
NOISE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        # pip / uv
        r"^\s*Requirement already satisfied\b",
        r"^\s*Downloading\b",
        r"^\s*Collecting\b",
        r"^\s*Using cached\b",
        r"^\s*Obtaining dependency information\b",
        r"^\s*(Resolved|Prepared|Installed|Uninstalled|Audited) \d+ packages? in\b",
        r"^\s*[+-] [A-Za-z0-9_.\-\[\]]+==\S+(?: \(from \S+\))?\s*$",  # uv: " + requests==2.32.3"
        # npm
        r"^\s*added \d+ packages?\b",
        r"^\s*found 0 vulnerabilities\b",
        # docker pull
        r"^\s*[0-9a-f]{12}: (Pulling fs layer|Waiting|Verifying Checksum|Download complete"
        r"|Pull complete|Already exists|Extracting|Downloading)\b",
        # docker buildx layer progress: "#4 sha256:<64 hex> 3.15MB / 29.13MB 0.1s"
        r"^#\d+ sha256:[0-9a-f]{64} [\d.]+[kMG]?B / [\d.]+[kMG]?B [\d.]+s$",
        # progress bars: a long run of bar characters
        r"[━█▇▆▅▄▃▂▁■░▒▓]{8,}",
        r"\[[=#>\- ]{20,}\]",
        # runner setup boilerplate ("Set up job")
        r"^Current runner version\b",
        r"^Runner (name|group name|Image|Image Provisioner)\b",
        r"^Requested labels:",
        r"^Job defined at:",
        r"^Waiting for a runner to pick up this job",
        r"^Prepare workflow directory$",
        r"^Prepare all required actions$",
        r"^Getting action download info$",
        r"^Download (immutable )?action repository\b",
        r"^Complete job name:",
        r"^Secret source:",
        r"^Machine name:",
        r"^Cleaning up orphan processes$",
        r"^Temporarily overriding HOME=",
        r"^Adding repository directory to the temporary git global config",
        r"^Evaluate and set (job outputs|environment url)$",
        # actions/checkout post-job cleanup
        r"^Post job cleanup\.$",
        r"^\[command\]/usr/bin/git (version|config --local|submodule foreach)\b",
        r"^git version \d",
    )
)

# ``##[group]`` sections whose whole content is runner boilerplate (title and body dropped).
NOISE_GROUPS: frozenset[str] = frozenset(
    {
        "Operating System",
        "Runner Image",
        "Runner Image Provisioner",
        "GITHUB_TOKEN Permissions",
        "Fetching the repository",
        "Determining the checkout info",
        "Setting up auth",
        "Getting Git version info",
        "Initializing the repository",
        "Disabling automatic garbage collection",
        "Checking out the ref",
        "Removing auth",
    }
)

_NOISE = re.compile("|".join(f"(?:{pattern.pattern})" for pattern in NOISE_PATTERNS))


def is_noise(text: str) -> bool:
    """True when the cleaned line matches a noise pattern (callers keep error lines anyway)."""
    return _NOISE.search(text) is not None
