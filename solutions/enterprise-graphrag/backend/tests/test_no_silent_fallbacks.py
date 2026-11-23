"""AST static guardrails enforcing fail-loud, authentic-measurement, and ISO GQL contracts."""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _all_app_files() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def test_zero_silent_exception_swallowing_in_backend_app() -> None:
    """No bare `except:` and no `except ...: pass` or silent `return None` across backend/app."""
    for path in _all_app_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None, f"Bare except prohibited in {path.relative_to(APP_DIR)}:{node.lineno}"
                if len(node.body) == 1:
                    stmt = node.body[0]
                    assert not isinstance(
                        stmt, ast.Pass
                    ), f"Silent except:pass in {path.relative_to(APP_DIR)}:{node.lineno}"
                    if (
                        isinstance(stmt, ast.Return)
                        and isinstance(stmt.value, ast.Constant)
                        and stmt.value.value is None
                    ):
                        raise AssertionError(
                            f"Silent except:return None in {path.relative_to(APP_DIR)}:{node.lineno}"
                        )


def test_zero_fake_sleep_pacing_in_engines_and_replay() -> None:
    """Engines and Replay must report real timings — never fake asyncio.sleep/time.sleep pacing."""
    for path in sorted((APP_DIR / "engines").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        assert "asyncio.sleep" not in src, f"Fake asyncio.sleep found in {path.name}"
        assert "time.sleep" not in src, f"Fake time.sleep found in {path.name}"


def test_no_cypher_syntax_in_gql_builder() -> None:
    src = (APP_DIR / "engines" / "gql_builder.py").read_text(encoding="utf-8")
    assert "MERGE (" not in src
    assert "CREATE (" not in src
    assert "DETACH DELETE" not in src


def test_no_unsafe_yaml_load_in_backend_app() -> None:
    """Enforce yaml.safe_load only (zero insecure yaml.load deserialization)."""
    for path in _all_app_files():
        src = path.read_text(encoding="utf-8")
        assert "yaml.load(" not in src, f"Unsafe yaml.load() found in {path.relative_to(APP_DIR)}"
