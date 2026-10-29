"""Gate tests only ask for roles a gate run over their area or journey boots."""

import ast
from pathlib import Path

import pytest

from labctl import areas

GATE = Path(__file__).parent.parent / "scenarios" / "gate"


@pytest.mark.parametrize("path", sorted(GATE.glob("test_*.py")), ids=lambda path: path.name)
def test_roles_within_area(path: Path) -> None:
    """Area roles stay within `gate_roles` of the area, a journey's on its domain's `<kind>-1`."""
    tree = ast.parse(path.read_text())
    module_marks = next(
        node.value.elts
        for node in tree.body
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "pytestmark"
    )
    area = _marker_suffix(module_marks, "area_")
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
    for marks in [module_marks, *(node.decorator_list for node in functions)]:
        roles = [role for mark in marks if _mark_name(mark) == "roles" for role in _roles(mark)]
        if not roles:
            continue
        domain = _marker_suffix(marks, "provider_")
        allowed = _domain_roles(domain) if domain else areas.gate_roles((area,))
        assert set(roles) <= set(allowed), f"{path.name}: {roles} outside {allowed}"


def _mark_name(mark: ast.expr) -> str:
    """Return the name of a `pytest.mark.<name>` or `pytest.mark.<name>(...)` expression."""
    node = mark.func if isinstance(mark, ast.Call) else mark
    return node.attr if isinstance(node, ast.Attribute) else ""


def _marker_suffix(marks: list[ast.expr], prefix: str) -> str | None:
    """Return the suffix of the first mark named `<prefix><suffix>`, None without one."""
    names = (_mark_name(mark) for mark in marks)
    return next((name.removeprefix(prefix) for name in names if name.startswith(prefix)), None)


def _roles(mark: ast.expr) -> list[str]:
    """Return the roles of a `roles(...)` mark: literal strings or `*AREA_ROLES["<area>"]`."""
    assert isinstance(mark, ast.Call)
    roles: list[str] = []
    for arg in mark.args:
        if isinstance(arg, ast.Starred):
            roles += areas.AREA_ROLES[ast.literal_eval(arg.value.slice)]
        else:
            roles.append(ast.literal_eval(arg))
    return roles


def _domain_roles(domain: str) -> tuple[str, ...]:
    """Return the first role of the player kind behind a provider domain."""
    return tuple(f"{kind}-1" for kind, name in areas.PLAYER_DOMAINS.items() if name == domain)
