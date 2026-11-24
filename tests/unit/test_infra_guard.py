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
# An ingress rule may be open to the world only for the ALB listener (D-121); today none is:
# CloudFront reaches the ALB through its managed prefix list.
ALB_INGRESS_PREFIX = "alb_"
OPEN_WORLD = ("0.0.0.0/0", "::/0")


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
        label = re.search(r'"(\w+)"\s*\{$', header)
        allowed = label is not None and label.group(1).startswith(ALB_INGRESS_PREFIX)
        if any(world in body for world in OPEN_WORLD) and not allowed:
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
        'resource "aws_vpc_security_group_ingress_rule" "alb_http" {\n  cidr_ipv4 = "0.0.0.0/0"\n}',
        'cidr_block = "10.20.0.0/16"',
    ],
)
def test_the_scanner_lets_a_harmless_snippet_pass(snippet: str) -> None:
    assert violations(snippet) == []
