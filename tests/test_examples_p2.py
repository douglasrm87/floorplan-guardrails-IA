"""Teste de sanidade dos exemplos do P2, em `examples/p2/`.

Exceção documentada à regra da suíte
------------------------------------
Os outros testes usam só as fixtures de `tests/`, nunca `config/`. Este usa
a configuração real de propósito: `config/rules.yaml`,
`config/furniture_catalog.yaml` e `config/furniture_rules.yaml`.

O notebook do P2 depende de duas coisas que só valem com esses arquivos: toda
planta de exemplo é aceita pelo P1, porque o mobiliador recusa planta
reprovada na entrada; e `layout_ruim.json` reprova, porque é a reprovação
garantida do nível 2. As negociações gravadas em `replays/` também são
conferidas: o notebook as desenha quando a conexão cai. Se alguém mudar um
valor em `config/` e um exemplo deixar de servir, é este teste que avisa
antes da oficina.
"""

import json
from pathlib import Path

import pytest
import yaml

from floorplan_guardrails.furniture import (
    FurnishedPlan,
    FurnishingProposal,
    FurnishingRequest,
    Profile,
    load_catalog,
)
from floorplan_guardrails.furniture_rules import load_furniture_rules
from floorplan_guardrails.inspection import INTEGRITY_RULES_P2, inspect
from floorplan_guardrails.negotiation_log import load_negotiation
from floorplan_guardrails.rules import load_rules
from floorplan_guardrails.schema import FloorPlan
from floorplan_guardrails.validator import validate

REPO_ROOT = Path(__file__).parents[1]
EXAMPLES = REPO_ROOT / "examples" / "p2"
CONFIG = REPO_ROOT / "config"

PLANS = sorted((EXAMPLES / "plans").glob("*.json"))


def read_plan(path: Path) -> FloorPlan:
    return FloorPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))


def read_request(name: str) -> FurnishingRequest:
    path = EXAMPLES / "requests" / f"{name}.yaml"
    return FurnishingRequest.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def test_the_examples_folder_has_plans() -> None:
    """Uma pasta lida vazia faria o teste abaixo passar sem conferir nada."""
    assert len(PLANS) >= 3


@pytest.mark.parametrize("path", PLANS, ids=lambda path: path.stem)
def test_every_example_plan_is_approved_by_the_real_rules(path: Path) -> None:
    violations = validate(read_plan(path), load_rules(CONFIG / "rules.yaml"))

    assert violations == [], "\n".join(violation.message for violation in violations)


def test_the_bad_layout_fails_only_on_circulation() -> None:
    """`layout_ruim.json` reprova pela porta e pela faixa de uso, nunca por desenho.

    Uma violação de integridade faria a negociação pular o fiscal, e o nível
    2 do notebook mostraria uma lista de erros de desenho em vez de parecer.
    """
    catalog = load_catalog(CONFIG / "furniture_catalog.yaml")
    rules = load_furniture_rules(catalog, CONFIG / "furniture_rules.yaml")
    proposal = FurnishingProposal.model_validate_json(
        (EXAMPLES / "proposals" / "layout_ruim.json").read_text(encoding="utf-8")
    )
    furnished = FurnishedPlan(
        plan=read_plan(EXAMPLES / "plans" / "casa_4_comodos.json"),
        proposal=proposal,
    )

    violations = inspect(
        furnished, catalog, rules, Profile(), read_request("casa_4_comodos")
    )
    rule_ids = {violation.rule_id for violation in violations}

    assert {"door_clearance", "use_zone"} <= rule_ids
    assert not rule_ids & INTEGRITY_RULES_P2


#: Cada gravação de `replays/` e o desfecho que o notebook conta que ela tem.
REPLAYS = {
    "aprovada": "approved",
    "desistencia": "declined",
    "sugestao-errada": "not_converged",
}


def test_the_replays_folder_has_exactly_the_recorded_negotiations() -> None:
    names = {path.stem for path in (EXAMPLES / "replays").glob("*.jsonl")}

    assert names == set(REPLAYS)


@pytest.mark.parametrize(("name", "status"), REPLAYS.items())
def test_each_replay_loads_with_the_outcome_the_notebook_tells(
    name: str, status: str
) -> None:
    negotiation = load_negotiation(EXAMPLES / "replays" / f"{name}.jsonl")

    assert negotiation.status == status
    assert validate(negotiation.plan, load_rules(CONFIG / "rules.yaml")) == []
