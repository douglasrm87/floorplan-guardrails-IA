"""Testes da negociação, do registro e do replay.

Nenhum teste chama o modelo. O mobiliador é roteirizado, como o gerador dos
testes do laço do P1: entrega as propostas combinadas, em ordem, e guarda o
que recebeu em cada chamada. O fiscal é o `MessageInspector`, embrulhado num
espião que conta as chamadas.

Os testes usam só as fixtures de `tests/fixtures/p2/`, nunca `config/`.
"""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from floorplan_guardrails.furnished_renderer import draw_negotiation
from floorplan_guardrails.furnisher import Feedback, FurnishingAttempt
from floorplan_guardrails.furniture import (
    Catalog,
    FurnishedPlan,
    FurnishingProposal,
    FurnishingRequest,
    Profile,
    load_catalog,
)
from floorplan_guardrails.furniture_rules import load_furniture_rules
from floorplan_guardrails.inspection import (
    INTEGRITY_RULES_P2,
    FurnitureViolation,
    InvalidInputPlan,
    inspect,
)
from floorplan_guardrails.inspector import (
    APPROVED_SUMMARY,
    MessageInspector,
    Review,
)
from floorplan_guardrails.negotiation import (
    DEFAULT_MAX_ROUNDS,
    Negotiation,
    negotiate,
)
from floorplan_guardrails.negotiation_log import NegotiationLog, load_negotiation
from floorplan_guardrails.rules import load_rules
from floorplan_guardrails.runlog import read_run
from floorplan_guardrails.schema import FloorPlan

FIXTURES = Path(__file__).parent / "fixtures" / "p2"
CATALOG = load_catalog(FIXTURES / "catalog.yaml")
RULES = load_furniture_rules(CATALOG, FIXTURES / "furniture_rules.yaml")
PLAN_RULES = load_rules(FIXTURES / "plan_rules.yaml")


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def plan() -> FloorPlan:
    return FloorPlan.model_validate(read("plan.json"))


def request() -> FurnishingRequest:
    return FurnishingRequest.model_validate(read("request.json"))


def proposal(**changes: dict) -> FurnishingProposal:
    """A proposta de referência, aprovada, com os móveis indicados mudados."""
    data = read("proposal.json")
    for placement in data["placements"]:
        placement.update(changes.get(placement["id"], {}))

    return FurnishingProposal.model_validate(data)


def cramped() -> FurnishingProposal:
    """Só violações de circulação: a cama diante da porta, o guarda-roupa
    colado nela."""
    return proposal(m2={"y": 3.35}, m4={"x": 2.8})


def without_nightstand() -> FurnishingProposal:
    """Só uma violação de integridade: a mesa de cabeceira pedida não veio."""
    data = proposal().model_dump()
    data["placements"] = [p for p in data["placements"] if p["id"] != "m3"]

    return FurnishingProposal.model_validate(data)


def declining() -> FurnishingProposal:
    """O mobiliador desiste do guarda-roupa."""
    data = proposal().model_dump()
    data["placements"] = [p for p in data["placements"] if p["id"] != "m4"]
    data["omissions"] = [
        {
            "room_id": "r3",
            "item_id": "wardrobe",
            "reason": "O guarda-roupa não cabe no quarto ao lado da cama.",
        }
    ]

    return FurnishingProposal.model_validate(data)


def violations_of(item: FurnishingProposal) -> list[FurnitureViolation]:
    furnished = FurnishedPlan(plan=plan(), proposal=item)
    return inspect(furnished, CATALOG, RULES, Profile(), request())


class ScriptedFurnisher:
    """Mobiliador de mentira: entrega as propostas combinadas, em ordem.

    Guarda o que recebeu em cada chamada, que é como os testes provam que o
    parecer chega mesmo ao mobiliador. Depois da última proposta, repete-a.
    """

    def __init__(self, proposals: Sequence[FurnishingProposal]) -> None:
        self.proposals = list(proposals)
        self.calls: list[tuple[FurnishingProposal | None, list[Feedback]]] = []

    async def propose(
        self,
        plan: FloorPlan,
        catalog: Catalog,
        request: FurnishingRequest,
        profile: Profile,
        previous: FurnishingProposal | None = None,
        feedback: Sequence[Feedback] = (),
    ) -> FurnishingAttempt:
        self.calls.append((previous, list(feedback)))
        item = self.proposals[min(len(self.calls) - 1, len(self.proposals) - 1)]

        return FurnishingAttempt(
            proposal=item, input_tokens=10, output_tokens=20, latency_ms=30
        )


class SpyInspector:
    """O `MessageInspector`, contando as chamadas e as violações recebidas."""

    def __init__(self) -> None:
        self.calls: list[list[FurnitureViolation]] = []

    async def review(
        self,
        furnished: FurnishedPlan,
        violations: Sequence[FurnitureViolation],
        profile: Profile,
    ) -> Review:
        self.calls.append(list(violations))
        return await MessageInspector().review(furnished, violations, profile)


def run(
    furnisher: ScriptedFurnisher,
    inspector: SpyInspector | None = None,
    **kwargs,
) -> Negotiation:
    return asyncio.run(
        negotiate(
            plan(),
            request(),
            Profile(),
            furnisher,
            inspector or SpyInspector(),
            CATALOG,
            RULES,
            plan_rules=PLAN_RULES,
            **kwargs,
        )
    )


def statuses(negotiation: Negotiation) -> list[str]:
    return [item.status for item in negotiation.rounds]


# --- o parecer determinístico ------------------------------------------------


def test_the_message_inspector_returns_each_message_without_suggestion() -> None:
    violations = violations_of(cramped())

    review = asyncio.run(
        MessageInspector().review(
            FurnishedPlan(plan=plan(), proposal=cramped()), violations, Profile()
        )
    )

    assert review.feedback == [
        Feedback(message=violation.message, suggestion="") for violation in violations
    ]
    assert (review.input_tokens, review.output_tokens, review.latency_ms) == (0, 0, 0)
    assert review.tool_calls == 0


# --- desfechos ---------------------------------------------------------------


def test_a_layout_that_passes_at_once_is_approved() -> None:
    furnisher = ScriptedFurnisher([proposal()])
    inspector = SpyInspector()

    negotiation = run(furnisher, inspector)

    assert negotiation.status == "approved"
    assert negotiation.approved
    assert statuses(negotiation) == ["approved"]
    assert negotiation.violations == []
    assert negotiation.rounds[0].review == Review(summary=APPROVED_SUMMARY)
    assert inspector.calls == []


def test_an_omission_ends_the_negotiation_as_declined() -> None:
    furnisher = ScriptedFurnisher([declining(), proposal()])
    inspector = SpyInspector()

    negotiation = run(furnisher, inspector)

    assert negotiation.status == "declined"
    assert statuses(negotiation) == ["declined"]
    assert len(furnisher.calls) == 1
    assert inspector.calls == []

    only = negotiation.rounds[0]
    assert only.violations == []
    assert only.review is None
    assert only.proposal.omissions[0].reason.startswith("O guarda-roupa não cabe")


def test_a_layout_that_never_passes_ends_as_not_converged() -> None:
    furnisher = ScriptedFurnisher([cramped()])
    inspector = SpyInspector()

    negotiation = run(furnisher, inspector, max_rounds=2)

    assert negotiation.status == "not_converged"
    assert statuses(negotiation) == ["rejected", "not_converged"]
    assert len(furnisher.calls) == 2
    # O parecer da última rodada também é redigido, para quem lê o histórico.
    assert len(inspector.calls) == 2
    assert negotiation.violations == violations_of(cramped())


def test_a_rejected_round_is_followed_by_an_approved_one() -> None:
    furnisher = ScriptedFurnisher([cramped(), proposal()])
    inspector = SpyInspector()

    negotiation = run(furnisher, inspector)

    assert negotiation.status == "approved"
    assert statuses(negotiation) == ["rejected", "approved"]
    assert inspector.calls == [violations_of(cramped())]

    first_call, second_call = furnisher.calls
    assert first_call == (None, [])
    assert second_call == (cramped(), negotiation.rounds[0].review.feedback)
    assert [item.message for item in second_call[1]] == [
        violation.message for violation in violations_of(cramped())
    ]


def test_a_round_with_only_integrity_violations_uses_the_messages() -> None:
    furnisher = ScriptedFurnisher([without_nightstand(), proposal()])
    inspector = SpyInspector()

    negotiation = run(furnisher, inspector)

    first = negotiation.rounds[0]
    assert first.status == "rejected"
    assert first.violations
    assert {violation.rule_id for violation in first.violations} <= INTEGRITY_RULES_P2
    assert inspector.calls == []
    assert first.review.feedback == [
        Feedback(message=violation.message, suggestion="")
        for violation in first.violations
    ]
    assert furnisher.calls[1][1] == first.review.feedback
    assert negotiation.status == "approved"


def test_the_default_limit_is_three_rounds() -> None:
    furnisher = ScriptedFurnisher([cramped()])

    negotiation = run(furnisher)

    assert DEFAULT_MAX_ROUNDS == 3
    assert len(negotiation.rounds) == DEFAULT_MAX_ROUNDS
    assert statuses(negotiation) == ["rejected", "rejected", "not_converged"]


@pytest.mark.parametrize("max_rounds", [0, -1])
def test_a_negotiation_without_rounds_is_refused(max_rounds: int) -> None:
    furnisher = ScriptedFurnisher([proposal()])

    with pytest.raises(ValueError, match="ao menos uma rodada"):
        run(furnisher, max_rounds=max_rounds)

    assert furnisher.calls == []


def test_a_single_round_can_end_as_not_converged() -> None:
    negotiation = run(ScriptedFurnisher([cramped()]), max_rounds=1)

    assert statuses(negotiation) == ["not_converged"]


# --- pré-condição ------------------------------------------------------------


def test_a_plan_rejected_by_the_p1_is_refused_before_any_round() -> None:
    data = read("plan.json")
    bedroom = next(room for room in data["rooms"] if room["id"] == "r3")
    bedroom["width"] = 2.2
    bedroom["depth"] = 2.2
    furnisher = ScriptedFurnisher([proposal()])

    with pytest.raises(InvalidInputPlan, match="planta aprovada no P1"):
        asyncio.run(
            negotiate(
                FloorPlan.model_validate(data),
                request(),
                Profile(),
                furnisher,
                SpyInspector(),
                CATALOG,
                RULES,
                plan_rules=PLAN_RULES,
            )
        )

    assert furnisher.calls == []


# --- somas e histórico -------------------------------------------------------


def test_tokens_and_latency_add_up_across_rounds() -> None:
    negotiation = run(ScriptedFurnisher([cramped(), proposal()]))

    assert negotiation.input_tokens == 20
    assert negotiation.output_tokens == 40
    assert negotiation.latency_ms == 60


def test_the_history_has_the_shape_the_renderer_draws() -> None:
    negotiation = run(ScriptedFurnisher([cramped(), proposal()]))

    (first_plan, first_violations, first_status), second = negotiation.history

    assert first_plan == FurnishedPlan(plan=plan(), proposal=cramped())
    assert first_violations == violations_of(cramped())
    assert first_status == "rejected"
    assert second[2] == "approved"


# --- registro e replay -------------------------------------------------------


def logged_run(tmp_path: Path) -> tuple[Negotiation, NegotiationLog]:
    """Uma negociação rejeitada e depois aprovada, gravada em `tmp_path`."""
    log = NegotiationLog.create(tmp_path, run_id="teste")
    negotiation = run(
        ScriptedFurnisher([cramped(), proposal()]),
        log=log,
        deployment="gpt-teste",
    )

    return negotiation, log


def test_creating_a_log_makes_the_folder(tmp_path: Path) -> None:
    log = NegotiationLog.create(tmp_path / "runs" / "p2")

    assert log.path.parent.is_dir()
    assert log.path.name == f"{log.run_id}.jsonl"


def test_each_round_is_one_line(tmp_path: Path) -> None:
    negotiation, log = logged_run(tmp_path)

    lines = read_run(log.path)

    assert negotiation.run_id == "teste"
    assert [line["round"] for line in lines] == [1, 2]
    assert [line["status"] for line in lines] == ["rejected", "approved"]
    assert {line["run_id"] for line in lines} == {"teste"}
    assert {line["deployment"] for line in lines} == {"gpt-teste"}


def test_the_plan_goes_only_in_the_first_line(tmp_path: Path) -> None:
    _, log = logged_run(tmp_path)

    first, second = read_run(log.path)

    assert FloorPlan.model_validate(first["plan"]) == plan()
    assert "plan" not in second


def test_a_line_carries_tokens_and_latency_per_agent(tmp_path: Path) -> None:
    _, log = logged_run(tmp_path)

    first = read_run(log.path)[0]

    assert first["furnisher"] == {
        "input_tokens": 10,
        "output_tokens": 20,
        "latency_ms": 30,
    }
    # O fiscal desta suíte é o `MessageInspector`, que não gasta nada.
    assert first["inspector"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
    }
    feedback = first["report"]["feedback"]
    assert [item["message"] for item in feedback] == [
        violation["message"] for violation in first["violations"]
    ]


def test_replay_rebuilds_a_recorded_negotiation(tmp_path: Path) -> None:
    negotiation, log = logged_run(tmp_path)

    replayed = load_negotiation(log.path)

    assert replayed == negotiation
    assert isinstance(replayed.violations, list)
    assert all(
        isinstance(violation, FurnitureViolation)
        for item in replayed.rounds
        for violation in item.violations
    )


def test_replay_rebuilds_a_declined_negotiation(tmp_path: Path) -> None:
    log = NegotiationLog.create(tmp_path)
    negotiation = run(ScriptedFurnisher([declining()]), log=log)

    replayed = load_negotiation(log.path)

    assert replayed == negotiation
    assert replayed.rounds[0].review is None


def test_a_replayed_negotiation_is_drawn_like_a_live_one(tmp_path: Path) -> None:
    _, log = logged_run(tmp_path)
    replayed = load_negotiation(log.path)

    figure = draw_negotiation(replayed.history, CATALOG, RULES, replayed.profile)

    assert [ax.get_title() for ax in figure.axes] == [
        "Rodada 1: reprovada, 3 violações",
        "Rodada 2: aprovada",
    ]


def test_an_empty_log_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "vazio.jsonl"
    path.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="está vazio"):
        load_negotiation(path)


def test_a_log_cut_at_the_start_is_refused(tmp_path: Path) -> None:
    _, log = logged_run(tmp_path)
    second = log.path.read_text(encoding="utf-8").splitlines()[1]
    cut = tmp_path / "cortado.jsonl"
    cut.write_text(second + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="não traz a planta"):
        load_negotiation(cut)
