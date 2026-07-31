"""Regression guard for the clock-injection rule (CLAUDE.md; build prompt
24, defect #1): every wall-clock read under ``orchestrator/`` must be
reachable only through the ``now or datetime.now(...)`` fallback idiom,
never bare — otherwise a module quietly stops being hermetic, and a test
pinned to a fixed timestamp starts failing the moment real time drifts past
whatever window the code assumes (exactly what happened to
``JsonAttentionStore``: see ``tests/test_attention.py``).

A grep-based AST check: walk every module's syntax tree, and flag any
``datetime.now(...)`` call that is not the right-hand side of an ``or``
boolean expression.
"""

from __future__ import annotations

import ast
from pathlib import Path

ORCHESTRATOR_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "attune" / "orchestrator"
)


def _is_datetime_now_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "datetime"
    )


def _bare_datetime_now_lines(tree: ast.AST) -> list[int]:
    """Line numbers of ``datetime.now(...)`` calls that are NOT part of the
    injectable ``now or datetime.now(...)`` idiom."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values:
                if _is_datetime_now_call(value):
                    allowed.add(id(value))

    return [
        node.lineno
        for node in ast.walk(tree)
        if _is_datetime_now_call(node) and id(node) not in allowed
    ]


def test_no_bare_wall_clock_reads_under_orchestrator():
    violations: dict[str, list[int]] = {}
    for path in sorted(ORCHESTRATOR_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        lines = _bare_datetime_now_lines(tree)
        if lines:
            violations[path.name] = lines

    assert not violations, (
        "bare datetime.now(...) call(s) found outside the injectable "
        "`now or datetime.now(...)` idiom (thread an explicit `now` "
        f"parameter instead): {violations}"
    )
