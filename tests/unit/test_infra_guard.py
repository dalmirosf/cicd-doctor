"""Offline guard over ``infra/**/*.tf`` (P5 d, D-121, D-122, D-124, D-125).

Nobody runs ``terraform plan`` without the owner, so this reads the code as text and refuses the
things that would cost money, leak a secret or open a door: a secret value in Terraform, an access
key, a real account id, a public database, a world-open ingress, a NAT gateway. It does not replace
``terraform validate``; it keeps what ``validate`` cannot see.
"""

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra"
# No ingress rule is open to the world (D-121, D-120): CloudFront reaches the ALB through its
# managed prefix list, and everything else accepts only a security group.
OPEN_WORLD = ("0.0.0.0/0", "::/0")
# A name that carries a secret never goes in a task definition `environment` (only in `secrets`).
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|^DATABASE_URL$")


def strip_comments(text: str) -> str:
    """Drop ``#`` and ``//`` comments (a comment may name what the code must not contain)."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", "//")):
            continue
        lines.append(re.sub(r"\s#\s.*$", "", line))
    return "\n".join(lines)


def terraform_files() -> list[Path]:
    return sorted(INFRA.rglob("*.tf"))


def roots() -> list[Path]:
    """Folders that declare a ``terraform {`` block: the root modules."""
    found = {
        p.parent
        for p in terraform_files()
        if re.search(r"^terraform\s*\{", strip_comments(p.read_text(encoding="utf-8")), re.M)
    }
    return sorted(found)


def blocks(text: str, opener: re.Pattern[str]) -> list[tuple[str, str]]:
    """Return ``(header line, body)`` of every brace block whose header line matches ``opener``."""
    out: list[tuple[str, str]] = []
    for match in opener.finditer(text):
        depth, i = 0, text.index("{", match.start())
        start = i
        while i < len(text):
            depth += text[i] == "{"
            depth -= text[i] == "}"
            i += 1
            if depth == 0:
                break
        out.append((match.group(0).strip(), text[start:i]))
    return out


def violations(text: str, name: str = "x.tf") -> list[str]:
    """Every dangerous thing found in one Terraform file, as readable lines."""
    code = strip_comments(text)
    found: list[str] = []
    checks = {
        "a secret value in Terraform (D-122)": r'resource\s+"aws_secretsmanager_secret_version"',
        "an IAM access key": r'resource\s+"aws_iam_access_key"|\b\w*access_key\w*\s*=',
        "a customer KMS key (costs money, D-125)": r'resource\s+"aws_kms_key"',
        "a NAT gateway (D-121)": r'resource\s+"aws_nat_gateway"|\bnat_gateway_id\b',
        "a public database (D-121)": r"publicly_accessible\s*=\s*true",
        "a 12-digit id (use aws_caller_identity)": r"(?<![\d.])\d{12}(?![\d.])",
    }
    for what, pattern in checks.items():
        if re.search(pattern, code):
            found.append(f"{name}: {what}")
    ingress_headers = re.compile(
        r'resource\s+"aws_vpc_security_group_ingress_rule"\s+"(?P<n>\w+)"\s*\{|^\s*ingress\s*\{',
        re.M,
    )
    for header, body in blocks(code, ingress_headers):
        if any(world in body for world in OPEN_WORLD):
            found.append(f"{name}: an ingress open to the world ({header})")
    for header, body in blocks(
        code, re.compile(r'resource\s+"aws_security_group_rule"\s+"\w+"\s*\{')
    ):
        if "type" in body and '"ingress"' in body and any(w in body for w in OPEN_WORLD):
            found.append(f"{name}: an ingress open to the world ({header})")
    return found


def test_there_is_terraform_to_scan() -> None:
    assert {p.name for p in roots()} == {"bootstrap", "persistent", "ephemeral"}


def test_no_dangerous_construct_in_any_terraform_file() -> None:
    found = [
        v
        for p in terraform_files()
        for v in violations(p.read_text(encoding="utf-8"), str(p.relative_to(INFRA)))
    ]
    assert found == []


@pytest.mark.parametrize("root", roots(), ids=lambda p: p.name)
def test_every_root_pins_terraform_and_the_provider(root: Path) -> None:
    code = strip_comments(
        "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.glob("*.tf")))
    )
    assert re.search(r'required_version\s*=\s*">= 1\.10"', code)
    assert re.search(r'source\s*=\s*"hashicorp/aws"\s*\n\s*version\s*=\s*"~> 6\.74"', code)
    # Committed next to the code: the lock file and the example of the variables.
    assert (root / ".terraform.lock.hcl").is_file()
    assert (root / "terraform.tfvars.example").is_file()


@pytest.mark.parametrize(
    "root", [r for r in roots() if r.name != "bootstrap"], ids=lambda p: p.name
)
def test_remote_layers_use_native_s3_locking(root: Path) -> None:
    code = strip_comments(
        "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.glob("*.tf")))
    )
    assert 'backend "s3"' in code
    assert re.search(r"use_lockfile\s*=\s*true", code)
    assert "dynamodb_table" not in code


# Positive controls: the scanner must catch what it claims to catch.
@pytest.mark.parametrize(
    "snippet",
    [
        'resource "aws_secretsmanager_secret_version" "v" {\n  secret_id = "x"\n}',
        'resource "aws_iam_access_key" "k" {\n  user = "u"\n}',
        'resource "aws_nat_gateway" "n" {\n}',
        'resource "aws_kms_key" "k" {\n}',
        "publicly_accessible = true",
        'role = "arn:aws:iam::123456789012:role/x"',
        'resource "aws_vpc_security_group_ingress_rule" "db_open" {\n  cidr_ipv4 = "0.0.0.0/0"\n}',
        'resource "aws_vpc_security_group_ingress_rule" "alb_http" {\n  cidr_ipv4 = "0.0.0.0/0"\n}',
        'resource "aws_security_group" "g" {\n  ingress {\n    cidr_blocks = ["::/0"]\n  }\n}',
        'resource "aws_security_group_rule" "r" {\n  type = "ingress"\n  cidr_blocks = ["::/0"]\n}',
    ],
)
def test_the_scanner_catches_a_dangerous_snippet(snippet: str) -> None:
    assert violations(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "# aws_nat_gateway is forbidden, publicly_accessible = true too",
        'resource "aws_vpc_security_group_egress_rule" "out" {\n  cidr_ipv4 = "0.0.0.0/0"\n}',
        'cidr_block = "10.20.0.0/16"',
    ],
)
def test_the_scanner_lets_a_harmless_snippet_pass(snippet: str) -> None:
    assert violations(snippet) == []


# ---- P5 e: services, ALB, CloudFront, OIDC (D-120, D-123) --------------------------------------


def code_of(root: str) -> str:
    folder = INFRA / "terraform" / root
    return strip_comments(
        "\n".join(p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.tf")))
    )


def resources_block(code: str, header: str) -> str:
    found = blocks(code, re.compile(header))
    assert len(found) == 1, header
    return found[0][1]


def test_the_alb_accepts_only_the_cloudfront_prefix_list() -> None:
    code = code_of("ephemeral")
    assert "com.amazonaws.global.cloudfront.origin-facing" in code
    rules = blocks(
        code,
        re.compile(r'resource\s+"aws_vpc_security_group_ingress_rule"\s+"alb_\w+"\s*\{'),
    )
    assert rules
    for header, body in rules:
        assert "prefix_list_id" in body, header
        assert "cidr_ipv4" not in body, header
        assert "cidr_ipv6" not in body, header


def test_one_task_per_service_and_none_until_the_migration_ran() -> None:
    code = code_of("ephemeral")
    counts = re.findall(r"desired_count\s*=\s*([^\n]+)", code)
    assert counts == ["var.services_enabled ? 1 : 0"] * 2  # api and worker, never more than one
    assert re.search(r'variable\s+"services_enabled"\s*\{[^}]*default\s*=\s*false', code, re.S)
    assert len(re.findall(r'resource\s+"aws_ecs_service"', code)) == 2
    assert len(re.findall(r"^\s*assign_public_ip\s*=\s*true", code, re.M)) == 2  # no NAT


def test_task_definitions_carry_no_secret_in_environment() -> None:
    code = code_of("ephemeral")
    maps = blocks(code, re.compile(r"\b\w+_environment\s*=\s*(?=\{|merge)"))
    assert len(maps) == 3  # app, migrate, and the worker's merge
    names: list[str] = []
    for _header, body in maps:
        names += re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=", body, re.M)
    assert {"APP_ENV", "LOG_FORMAT", "DB_HOST", "GITHUB_APP_ID"} <= set(names)
    assert [n for n in names if SECRET_NAME.search(n)] == []
    # The secrets come from Secrets Manager by ARN, injected by ECS, never from a literal.
    assert "secretsmanager_secret_version" not in code
    assert re.search(r'name\s*=\s*"DB_PASSWORD"', code)
    assert ':password::"' in code  # one JSON key of the RDS-managed secret
    for name in (
        "GITHUB_PRIVATE_KEY",
        "GITHUB_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        assert re.search(rf'^\s*{name}\s*=\s*"[a-z-]+"', code, re.M), name
    assert not re.search(r"\bDATABASE_URL\b", code)


def test_secret_names_of_the_services_exist_in_the_persistent_layer() -> None:
    persistent = code_of("persistent")
    ephemeral = code_of("ephemeral")
    secrets = resources_block(ephemeral, r"\bsecret_names\s*=\s*(?=\{)")
    for env, suffix in re.findall(r'^\s*([A-Z_]+)\s*=\s*"([a-z-]+)"', secrets, re.M):
        assert suffix.replace("-", "_") in persistent, suffix
        assert f'env_var     = "{env}"' in persistent, env


def test_cloudfront_is_the_cheap_http_only_entry() -> None:
    code = code_of("ephemeral")
    assert re.search(r'price_class\s*=\s*"PriceClass_100"', code)
    assert re.search(r'origin_protocol_policy\s*=\s*"http-only"', code)
    assert re.search(r'viewer_protocol_policy\s*=\s*"https-only"', code)
    assert "Managed-CachingDisabled" in code
    assert "Managed-AllViewer" in code
    assert '"POST"' in code
    for absent in ("web_acl_id", "logging_config", "aws_wafv2", "aws_acm_certificate"):
        assert absent not in code


def test_the_oidc_trust_is_pinned_to_one_repository_and_branch() -> None:
    code = code_of("persistent")
    assert re.search(r'url\s*=\s*"https://token\.actions\.githubusercontent\.com"', code)
    body = resources_block(code, r'data\s+"aws_iam_policy_document"\s+"deploy_trust"\s*\{')
    assert "sts:AssumeRoleWithWebIdentity" in body
    by_var: dict[str, str] = {}
    for _h, condition in blocks(body, re.compile(r"condition\s*\{")):
        match = re.search(r'variable\s*=\s*"([^"]+)"', condition)
        assert match is not None
        by_var[match.group(1)] = condition
    aud = by_var["token.actions.githubusercontent.com:aud"]
    sub = by_var["token.actions.githubusercontent.com:sub"]
    assert '"sts.amazonaws.com"' in aud
    for condition in (aud, sub):
        assert re.search(r'test\s*=\s*"StringEquals"', condition)  # exact match, never StringLike
        assert "*" not in condition
    assert re.search(r"values\s*=\s*\[local\.github_sub\]", sub)
    pinned = re.search(r'github_sub\s*=\s*"([^"]+)"', code)
    assert pinned is not None
    assert pinned.group(1) == (
        "repo:${var.github_owner}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}"
        ":ref:refs/heads/${var.github_deploy_branch}"
    )
    # The variables that feed the subject reject a wildcard (an anchored allow-list regex).
    for name in (
        "github_owner",
        "github_owner_id",
        "github_repo",
        "github_repo_id",
        "github_deploy_branch",
    ):
        variable = resources_block(code, rf'variable\s+"{name}"\s*\{{')
        assert re.search(r'regex\("\^\[[^*\]]+\]\+\$"', variable), name


def test_the_deploy_policy_is_least_privilege() -> None:
    code = code_of("persistent")
    policy = resources_block(code, r'data\s+"aws_iam_policy_document"\s+"deploy"\s*\{')
    statements = [b for _h, b in blocks(policy, re.compile(r"\bstatement\s*\{"))]
    assert len(statements) >= 7
    unscopable = {
        "ecr:GetAuthorizationToken",
        "ecs:RegisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
    }
    saw_pass_role = False
    for st in statements:
        actions_text = re.search(r"actions\s*=\s*\[(.*?)\]", st, re.S)
        resources_text = re.search(r"resources\s*=\s*\[(.*?)\]\s*(?:\n|$)", st, re.S)
        assert actions_text is not None, st
        assert resources_text is not None, st
        actions = re.findall(r'"([^"]+)"', actions_text.group(1))
        assert not any(a == "*" or a.endswith(":*") for a in actions), st
        if "iam:PassRole" in actions:
            saw_pass_role = True
            assert re.search(r'variable\s*=\s*"iam:PassedToService"', st)
            assert '"ecs-tasks.amazonaws.com"' in st
            assert '"*"' not in resources_text.group(1)
        if '"*"' in resources_text.group(1):
            assert set(actions) <= unscopable, actions  # only what AWS cannot scope
    assert saw_pass_role
    assert "aws_iam_user" not in code
    assert "aws_iam_access_key" not in code


# ---- review of P5 e: ordering, secret split, egress, workflows ----------------------------------


def test_the_api_service_waits_for_the_rule_that_attaches_its_target_group() -> None:
    code = code_of("ephemeral")
    service = resources_block(code, r'resource\s+"aws_ecs_service"\s+"api"\s*\{')
    depends = re.search(r"depends_on\s*=\s*\[(.*?)\]", service, re.S)
    assert depends is not None
    # The target group joins the ALB through the listener rule, not through the listener.
    assert "aws_lb_listener_rule.from_our_cloudfront" in depends.group(1)
    assert "aws_lb_listener.http" in depends.group(1)


def test_the_internet_facing_api_gets_only_the_webhook_secret() -> None:
    code = code_of("ephemeral")
    api_task = resources_block(code, r'resource\s+"aws_ecs_task_definition"\s+"api"\s*\{')
    worker_task = resources_block(code, r'resource\s+"aws_ecs_task_definition"\s+"worker"\s*\{')
    assert "local.api_secrets" in api_task
    assert "local.api_environment" in api_task
    assert "local.worker_secrets" in worker_task
    assert "local.worker_secrets" not in api_task
    envs = re.search(r"api_secret_envs\s*=\s*\[(.*?)\]", code, re.S)
    assert envs is not None
    assert re.findall(r'"([A-Z_]+)"', envs.group(1)) == ["GITHUB_WEBHOOK_SECRET"]
    api_env = resources_block(code, r"\bapi_environment\s*=\s*(?=\{)")
    for forbidden in ("GITHUB_APP_ID", "OPENAI", "LANGFUSE", "LLM_"):
        assert forbidden not in api_env
    worker_env = resources_block(code, r"\bworker_environment\s*=\s*(?=merge)")
    for needed in ("GITHUB_APP_ID", "OPENAI_MODEL", "LANGFUSE_HOST"):
        assert needed in worker_env
    # The worker starts with every secret container.
    assert re.search(r"worker_secrets\s*=\s*concat\(values\(local\.secret_entries\)", code)


def test_the_execution_role_reads_every_secret_a_task_starts_with() -> None:
    persistent = code_of("persistent")
    ephemeral = code_of("ephemeral")
    execution = resources_block(persistent, r'data\s+"aws_iam_policy_document"\s+"execution"\s*\{')
    # Every container of secrets.tf, nothing wider.
    assert re.search(
        r"resources\s*=\s*\[for s in aws_secretsmanager_secret\.app : s\.arn\]", execution
    )
    containers = re.findall(r'^\s*env_var\s*=\s*"([A-Z_]+)"', persistent, re.M)
    in_services = re.search(r"secret_names\s*=\s*(\{.*?\n  \})", ephemeral, re.S)
    assert in_services is not None
    assert sorted(containers) == sorted(
        re.findall(r"^\s*([A-Z_]+)\s*=", in_services.group(1), re.M)
    )
    # ... and the RDS password secret, attached by the ephemeral layer.
    db_policy = resources_block(
        ephemeral, r'data\s+"aws_iam_policy_document"\s+"read_db_secret"\s*\{'
    )
    assert "master_user_secret[0].secret_arn" in db_policy


def test_task_egress_is_https_and_postgres_only() -> None:
    code = code_of("ephemeral")
    rules = blocks(
        code,
        re.compile(r'resource\s+"aws_vpc_security_group_egress_rule"\s+"(api|worker)_\w+"\s*\{'),
    )
    assert len(rules) == 4
    for header, body in rules:
        assert '"-1"' not in body, header
        if "0.0.0.0/0" in body:
            assert re.search(r"from_port\s*=\s*443", body), header
            assert re.search(r"to_port\s*=\s*443", body), header
        else:
            assert "referenced_security_group_id" in body, header
            assert re.search(r"to_port\s*=\s*5432", body), header


def test_the_default_security_group_is_emptied() -> None:
    code = code_of("ephemeral")
    default = resources_block(code, r'resource\s+"aws_default_security_group"\s+"default"\s*\{')
    assert "ingress" not in default
    assert "egress" not in default


def test_no_workflow_combines_pull_request_target_with_an_oidc_token() -> None:
    # A pull_request_target run takes the base branch's sub (refs/heads/main), which the deploy
    # role trusts (D-123): such a workflow must never be able to ask for an OIDC token.
    workflows = sorted((INFRA.parent / ".github" / "workflows").glob("*.y*ml"))
    assert workflows
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        assert not (_mentions_pull_request_target(text) and _mentions_id_token(text)), path.name


def _mentions_pull_request_target(text: str) -> bool:
    return bool(
        re.search(r"^\s*(on:.*|-\s*)?pull_request_target\b|^\s+pull_request_target\s*:", text, re.M)
    )


def _mentions_id_token(text: str) -> bool:
    return bool(re.search(r"^\s*id-token\s*:\s*write", text, re.M))


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        (
            "on:\n  pull_request_target:\n    branches: [main]\npermissions:\n  id-token: write\n",
            True,
        ),
        ("on: [pull_request_target]\njobs:\n  a:\n    permissions:\n      id-token: write\n", True),
        ("on:\n  push:\n    branches: [main]\npermissions:\n  id-token: write\n", False),
        ("on:\n  pull_request_target:\npermissions:\n  contents: read\n", False),
    ],
)
def test_the_workflow_scanner_catches_the_dangerous_combination(text: str, bad: bool) -> None:
    assert (_mentions_pull_request_target(text) and _mentions_id_token(text)) is bad
