"""Terraform and AWS rules (SPEC §10; the cloud family is AWS since D-46)."""

from cicd_doctor.core.models import Category
from cicd_doctor.core.rules.engine import Rule, compile_all

TERRAFORM_RULES: tuple[Rule, ...] = (
    Rule(
        id="TF-LOCK",
        ecosystem="terraform",
        patterns=compile_all(r"Error acquiring the state lock"),
        category=Category.INFRA_OR_DEPLOY,
        confidence=0.90,
        priority=3,
        cause="The Terraform state is locked by another run (or by one that crashed).",
        fix=(
            "Wait for the other run to finish; if none is running, release the stale lock with "
            "`terraform force-unlock <lock id>`, and serialize runs with a `concurrency` group."
        ),
    ),
    Rule(
        id="TF-CONFIG",
        ecosystem="terraform",
        patterns=compile_all(
            r"Error: (Unsupported argument|Missing required argument|Invalid reference"
            r"|Reference to undeclared)"
        ),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.85,
        priority=2,
        cause="The Terraform configuration has an unknown or missing argument or reference.",
        fix="Run `terraform validate` locally and fix the argument or reference it points to.",
    ),
    Rule(
        id="TF-INIT",
        ecosystem="terraform",
        patterns=compile_all(
            r"Error: Failed to query available provider packages",
            r"Inconsistent dependency lock file",
        ),
        category=Category.DEPENDENCY,
        confidence=0.80,
        priority=2,
        cause="`terraform init` could not install the providers the configuration requires.",
        fix=(
            "Check the provider `source` and version constraints, then refresh the lock file "
            "with `terraform init -upgrade` and commit `.terraform.lock.hcl`."
        ),
    ),
    Rule(
        id="AWS-ACCESS-DENIED",
        ecosystem="aws",
        patterns=compile_all(
            r"\bAccessDenied",
            r"\bUnauthorizedOperation\b",
            r"is not authorized to perform: (?P<action>\S+)",
        ),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.85,
        priority=2,
        cause="The AWS identity used by the job lacks a permission (`{action}`).",
        fix="Grant the missing IAM permission to the role or user the workflow uses.",
        defaults={"action": "the requested action"},
    ),
    Rule(
        id="AWS-CREDENTIALS",
        ecosystem="aws",
        patterns=compile_all(
            r"Unable to locate credentials",
            r"Could not load credentials",
            r"The security token included in the request is invalid",
            r"\bExpiredToken\b",
        ),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.85,
        priority=2,
        cause="The job has no valid AWS credentials (missing, invalid or expired).",
        fix=(
            "Configure credentials before the AWS step, preferably with "
            "`aws-actions/configure-aws-credentials` and GitHub OIDC (`id-token: write`)."
        ),
    ),
    Rule(
        id="AWS-OIDC",
        ecosystem="aws",
        patterns=compile_all(
            r"Could not assume role with OIDC",
            r"Not authorized to perform sts:AssumeRoleWithWebIdentity",
        ),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.90,
        priority=3,
        cause="The workflow could not assume its AWS role through GitHub OIDC.",
        fix=(
            "Check the role's trust policy (audience `sts.amazonaws.com` and the `sub` condition "
            "for this repository and branch) and that the job has `id-token: write`."
        ),
    ),
    Rule(
        id="AWS-CONFLICT",
        ecosystem="aws",
        patterns=compile_all(
            r"\bAlreadyExists\b",
            r"\bEntityAlreadyExists\b",
            r"\bBucketAlreadyOwnedByYou\b",
            r"\bResourceAlreadyExistsException\b",
        ),
        category=Category.INFRA_OR_DEPLOY,
        confidence=0.75,
        priority=2,
        cause="The deployment tried to create an AWS resource that already exists.",
        fix=(
            "Import the existing resource into the Terraform state, or give the new one a "
            "different name."
        ),
    ),
)
