"""Testes das contas da medição do P2.

As negociações aqui são linhas de JSONL fabricadas à mão, no formato que
`NegotiationLog.record` grava. O relatório é feito de funções puras
justamente para isso: conferir as contas sem gastar uma chamada ao modelo.
"""

from eval.report_p2 import (
    Execution,
    Target,
    build_report,
    p90,
    suggestions,
    targets_in,
    unstructured_suggestions,
    violation_counts,
)


def placement(placement_id: str, x: float, y: float, rotation: int = 0) -> dict:
    return {
        "id": placement_id,
        "room_id": "r1",
        "item_id": "sofa",
        "x": x,
        "y": y,
        "rotation": rotation,
    }


def violation(rule_id: str, *placement_ids: str) -> dict:
    return {"rule_id": rule_id, "placement_ids": list(placement_ids)}


def line(
    number: int,
    placements: list[dict],
    violations: list[dict],
    status: str,
    suggestion: str = "",
    furnisher_ms: int = 10_000,
    inspector_ms: int = 0,
) -> dict:
    report = None
    if violations:
        report = {
            "feedback": [{"message": "m", "suggestion": suggestion}],
            "summary": "",
            "tool_calls": 1,
            "invalid_params": [],
            "replacements": [],
        }

    return {
        "round": number,
        "proposal": {"placements": placements, "omissions": []},
        "violations": violations,
        "report": report,
        "furnisher": {
            "input_tokens": 100,
            "output_tokens": 50,
            "latency_ms": furnisher_ms,
        },
        "inspector": {
            "input_tokens": 200 if inspector_ms else 0,
            "output_tokens": 20 if inspector_ms else 0,
            "latency_ms": inspector_ms,
        },
        "status": status,
    }


def execution(lines: list[dict], plan: str = "casa", profile: str = "padrão"):
    return Execution(plan=plan, profile=profile, repetition=1, run_id="x", lines=lines)


# --- sugestões -------------------------------------------------------------------


def test_a_final_position_is_read_from_the_suggestion() -> None:
    text = (
        "Encoste o guarda-roupa na parede leste: m4 em x = 3,40, y = 3.00, rotação 270."
    )

    assert targets_in(text) == [Target("m4", 3.4, 3.0, 270)]


def test_the_para_variant_and_repeated_targets() -> None:
    text = (
        "Mover a cama m3 para m3 em x = 2.30, y = 4.10, rotação 0 "
        "(junto com mover o guarda-roupa m4 para x = 2.40, y = 3.00, rotação 90), "
        "ou seja, m3 em x = 2.30, y = 4.10, rotação 0."
    )

    assert targets_in(text) == [
        Target("m3", 2.3, 4.1, 0),
        Target("m4", 2.4, 3.0, 90),
    ]


def test_a_suggestion_without_position_is_not_a_target() -> None:
    assert targets_in("Afaste a cama da porta.") == []


def test_an_adopted_suggestion_that_breaks_something_new() -> None:
    """A proposta seguinte segue a sugestão, e o móvel movido cai noutra regra."""
    run = execution(
        [
            line(
                1,
                [placement("m1", 1.0, 0.0)],
                [violation("use_zone", "m1")],
                "rejected",
                suggestion="m1 em x = 1.00, y = 0.40, rotação 0",
                inspector_ms=5_000,
            ),
            line(
                2,
                [placement("m1", 1.0, 0.4)],
                [violation("door_clearance", "m1")],
                "not_converged",
            ),
        ]
    )

    [found] = suggestions(run)

    assert found.adopted
    assert found.cited_next
    assert found.new_rule_next


def test_a_suggestion_ignored_by_the_furnisher_is_not_adopted() -> None:
    run = execution(
        [
            line(
                1,
                [placement("m1", 1.0, 0.0)],
                [violation("use_zone", "m1")],
                "rejected",
                suggestion="m1 em x = 1.00, y = 0.40, rotação 0",
            ),
            line(2, [placement("m1", 1.0, 0.3, rotation=180)], [], "approved"),
        ]
    )

    [found] = suggestions(run)

    assert not found.adopted
    assert not found.cited_next


def test_a_suggestion_in_the_last_round_is_left_out() -> None:
    run = execution(
        [
            line(
                1,
                [placement("m1", 1.0, 0.0)],
                [violation("use_zone", "m1")],
                "not_converged",
                suggestion="m1 em x = 1.00, y = 0.40, rotação 0",
            )
        ]
    )

    assert suggestions(run) == []
    assert unstructured_suggestions(run) == 0


# --- números -------------------------------------------------------------------


def test_p90_is_a_value_that_occurred() -> None:
    assert p90([5, 1, 4, 2, 3, 6, 7, 8, 9, 10]) == 9
    assert p90([3]) == 3


def test_violations_per_round_are_named_by_rule() -> None:
    run = execution(
        [
            line(
                1,
                [],
                [
                    violation("use_zone"),
                    violation("use_zone"),
                    violation("door_clearance"),
                ],
                "rejected",
            ),
            line(2, [], [], "approved"),
        ]
    )

    assert violation_counts(run) == "3 (door_clearance, use_zone ×2) → 0"


def test_the_report_has_every_section() -> None:
    runs = [
        execution([line(1, [], [], "approved")], plan="a"),
        execution(
            [line(1, [], [violation("use_zone")], "not_converged", inspector_ms=4_000)],
            plan="b",
            profile="acessível",
        ),
    ]

    report = build_report(
        runs, deployment="gpt-5-mini", max_rounds=3, reasoning_effort="low"
    )

    for title in (
        "## Execução a execução",
        "## Estado final por planta",
        "## Estado final por perfil",
        "## Latência",
        "## Tokens",
        "## Sugestões do fiscal",
    ):
        assert title in report
    assert "| total | 2 | 1/2 (50%) | 0/2 (0%) | 1/2 (50%) | 0/2 (0%) |" in report
