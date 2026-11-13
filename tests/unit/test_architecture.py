"""Architecture rule (SPEC D-02): ``core`` must not depend on adapters, apps or IO libraries.

The check is static: it parses every module under ``core/`` and looks at its import statements,
without importing anything. Dynamic imports (``importlib.import_module``) are not detected.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import cicd_doctor

PACKAGE = "cicd_doctor"
FORBIDDEN_INTERNAL = (
    f"{PACKAGE}.adapters",
    f"{PACKAGE}.app",
    f"{PACKAGE}.api",
    f"{PACKAGE}.worker",
)
# IO / framework libraries that ``core`` must stay free of (D-02, D-50). Pydantic is allowed
# (D-03); pydantic_settings is not, because it reads the environment and ``.env``.
FORBIDDEN_EXTERNAL = frozenset(
    {
        "fastapi",
        "starlette",
        "uvicorn",
        "httpx",
        "sqlalchemy",
        "psycopg",
        "alembic",
        "pydantic_settings",
        "openai",
        "fastembed",
    }
)


def _module_package(path: Path, src_root: Path) -> str:
    """Dotted name of the package that contains ``path`` (needed to resolve relative imports)."""
    parts = path.relative_to(src_root).with_suffix("").parts
    return ".".join(parts if path.name == "__init__.py" else parts[:-1]).removesuffix(".__init__")


def _imported_names(tree: ast.AST, package: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            yield node.lineno, base
            # ``from cicd_doctor import adapters`` imports the submodule ``cicd_doctor.adapters``
            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}"


def _is_forbidden(name: str) -> bool:
    if name.split(".")[0] in FORBIDDEN_EXTERNAL:
        return True
    return any(name == banned or name.startswith(f"{banned}.") for banned in FORBIDDEN_INTERNAL)


def find_violations(src_root: Path) -> list[str]:
    """Return one message per forbidden import found under ``<src_root>/cicd_doctor/core``."""
    core_dir = src_root / PACKAGE / "core"
    violations: list[str] = []
    for path in sorted(core_dir.rglob("*.py")):
        package = _module_package(path, src_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seen: set[tuple[int, str]] = set()
        for lineno, name in _imported_names(tree, package):
            if _is_forbidden(name) and (lineno, name) not in seen:
                seen.add((lineno, name))
                violations.append(
                    f"{path.relative_to(src_root).as_posix()}:{lineno} imports {name}"
                )
    return violations


def test_core_does_not_import_adapters_or_io_libraries() -> None:
    src_root = Path(cicd_doctor.__file__).resolve().parents[1]
    assert any((src_root / PACKAGE / "core").rglob("*.py")), "no core modules found: bad path?"
    assert find_violations(src_root) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import cicd_doctor.adapters.memory", "imports cicd_doctor.adapters.memory"),
        ("from cicd_doctor.adapters import memory", "imports cicd_doctor.adapters"),
        ("from cicd_doctor import adapters", "imports cicd_doctor.adapters"),
        ("from ..adapters import memory", "imports cicd_doctor.adapters"),
        ("from cicd_doctor.api.main import app", "imports cicd_doctor.api.main"),
        ("from cicd_doctor.worker import main", "imports cicd_doctor.worker"),
        ("import fastapi", "imports fastapi"),
        ("from httpx import Client", "imports httpx"),
        ("from sqlalchemy.ext.asyncio import AsyncSession", "imports sqlalchemy.ext.asyncio"),
        ("from alembic import op", "imports alembic"),
        ("from pydantic_settings import BaseSettings", "imports pydantic_settings"),
    ],
)
def test_checker_detects_forbidden_imports(tmp_path: Path, source: str, expected: str) -> None:
    core = tmp_path / PACKAGE / "core"
    core.mkdir(parents=True)
    (tmp_path / PACKAGE / "__init__.py").write_text("")
    (core / "__init__.py").write_text("")
    (core / "bad.py").write_text(source + "\n")

    violations = find_violations(tmp_path)

    assert any(expected in violation for violation in violations), violations
    assert all(violation.startswith("cicd_doctor/core/bad.py:1") for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        "from cicd_doctor.core.models import Category",
        "from . import models",
        "from .models import Category",
        "import pydantic",
        "from collections.abc import Sequence",
    ],
)
def test_checker_allows_legitimate_imports(tmp_path: Path, source: str) -> None:
    core = tmp_path / PACKAGE / "core"
    core.mkdir(parents=True)
    (tmp_path / PACKAGE / "__init__.py").write_text("")
    (core / "__init__.py").write_text("")
    (core / "models.py").write_text("")
    (core / "ok.py").write_text(source + "\n")

    assert find_violations(tmp_path) == []
