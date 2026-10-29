"""Every check in scenarios/checks is a `@check` and has a test of its own that calls it."""

import ast
from pathlib import Path

SCENARIOS = Path(__file__).parent.parent / "scenarios"
# public `(ctx, ...) -> None` coroutines in scenarios/checks that are helpers, not checks
HELPERS = ("stop",)


def test_every_check_has_a_test() -> None:
    """Every check is used by name in a test, an import alone does not count."""
    # gate journeys only chain checks; music and smoke checks cannot run in a deep run
    layers = (SCENARIOS / "deep", SCENARIOS / "bench", SCENARIOS / "music", SCENARIOS / "smoke")
    used = {
        node.id
        for layer in layers
        for path in layer.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Name)
    }
    untested = [node.name for node in _checks() if node.name not in used]
    assert not untested, f"checks without a deep, bench, music or smoke test: {untested}"


def test_every_check_is_decorated() -> None:
    """Every check carries `@check`, so its checkpoints are named `<check>.<step>`."""
    bare = [
        node.name
        for node in _checks()
        if not any(ast.unparse(decorator) == "check" for decorator in node.decorator_list)
    ]
    assert not bare, f"checks without @check: {bare}"


def _checks() -> list[ast.AsyncFunctionDef]:
    """Return the public `(ctx, ...) -> None` coroutines of scenarios/checks that are checks."""
    checks: list[ast.AsyncFunctionDef] = []
    for path in sorted((SCENARIOS / "checks").glob("*.py")):
        if path.name == "context.py":
            continue
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
                continue
            params = [arg.arg for arg in node.args.args]
            returns_none = isinstance(node.returns, ast.Constant) and node.returns.value is None
            if params[:1] == ["ctx"] and returns_none and node.name not in HELPERS:
                checks.append(node)
    return checks
