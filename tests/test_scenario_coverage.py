"""Every check in scenarios/checks has a deep or bench test that calls it."""

import ast
import re
from pathlib import Path

SCENARIOS = Path(__file__).parent.parent / "scenarios"
SUBJECTS = ("role", "device")


def test_every_check_has_a_test() -> None:
    """A check is a public `async def` taking `(ctx, role|device, ...)` and returning None."""
    layers = (SCENARIOS / "deep", SCENARIOS / "bench")
    tests = "\n".join(path.read_text() for layer in layers for path in layer.glob("*.py"))
    untested = [name for name in _checks() if not re.search(rf"\b{name}\b", tests)]
    assert not untested, f"checks without a deep or bench test: {untested}"


def _checks() -> list[str]:
    """Return the names of the check functions in scenarios/checks, context.py excluded."""
    names: list[str] = []
    for path in sorted((SCENARIOS / "checks").glob("*.py")):
        if path.name == "context.py":
            continue
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
                continue
            params = [arg.arg for arg in node.args.args[:2]]
            returns_none = isinstance(node.returns, ast.Constant) and node.returns.value is None
            if len(params) == 2 and params[0] == "ctx" and params[1] in SUBJECTS and returns_none:
                names.append(node.name)
    return names
