"""Testes da convenção compartilhada e do bloco de geometria derivada.

Na Fase 4b o fiscal sugeriu rotação com a frente para o lado errado e um
guarda-roupa que passava da parede do quarto. Os testes daqui guardam as
duas correções: a convenção é um texto só, igual nas duas instruções, e a
geometria que o código calcula chega certa aos dois agentes, sem nenhum
número dos parâmetros de circulação.

Os testes usam só as fixtures de `tests/fixtures/p2/`.
"""

import inspect as signatures
import json
from pathlib import Path

import pytest
import yaml

from floorplan_guardrails import furnisher_prompt, inspector_prompt
from floorplan_guardrails.convention_prompt import CONVENTION
from floorplan_guardrails.derived_geometry import (
    PROPOSAL_TITLE,
    TITLE,
    geometry_block,
    plan_geometry,
    proposal_geometry,
)
from floorplan_guardrails.furnisher import Feedback
from floorplan_guardrails.furnisher import build_message as furnisher_message
from floorplan_guardrails.furniture import (
    FurnishedPlan,
    FurnishingProposal,
    FurnishingRequest,
    Placement,
    Profile,
    load_catalog,
)
from floorplan_guardrails.furniture_rules import load_furniture_rules
from floorplan_guardrails.inspection import door_faces, inspect
from floorplan_guardrails.inspector import build_message as inspector_message
from floorplan_guardrails.inspector import numbers_in, parse
from floorplan_guardrails.schema import FloorPlan
from floorplan_guardrails.validator import WALL_NAMES

FIXTURES = Path(__file__).parent / "fixtures" / "p2"
CATALOG = load_catalog(FIXTURES / "catalog.yaml")
RULES = load_furniture_rules(CATALOG, FIXTURES / "furniture_rules.yaml")
ODD_RULES_PATH = FIXTURES / "furniture_rules_odd.yaml"


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def plan() -> FloorPlan:
    return FloorPlan.model_validate(read("plan.json"))


def request() -> FurnishingRequest:
    return FurnishingRequest.model_validate(read("request.json"))


def proposal(**changes: dict) -> FurnishingProposal:
    """A proposta de referência, com os móveis indicados mudados."""
    data = read("proposal.json")
    for placement in data["placements"]:
        placement.update(changes.get(placement["id"], {}))

    return FurnishingProposal.model_validate(data)


def cramped() -> FurnishingProposal:
    """A cama diante da porta do quarto e o guarda-roupa colado nela."""
    return proposal(m2={"y": 3.35}, m4={"x": 2.8})


def only(placement: Placement) -> FurnishingProposal:
    return FurnishingProposal(placements=[placement], omissions=[], design_notes="")


def numbers_of(data: object) -> list[float]:
    """Todos os números de uma estrutura lida do YAML, em qualquer nível."""
    if isinstance(data, bool):
        return []
    if isinstance(data, int | float):
        return [float(data)]
    if isinstance(data, dict):
        return [n for value in data.values() for n in numbers_of(value)]
    if isinstance(data, list):
        return [n for value in data for n in numbers_of(value)]
    return []


def odd_values() -> list[float]:
    raw = yaml.safe_load(ODD_RULES_PATH.read_text(encoding="utf-8"))
    return sorted(set(numbers_of(raw)))


def leaked(text: str, values: list[float]) -> list[str]:
    """Os números do texto que coincidem com algum dos valores dados.

    Compara número com número, e não trecho de texto: "0.8" é pedaço de
    "10.85", e a busca por texto acusaria coincidências que não existem.
    """
    return [
        written
        for written in numbers_in(text)
        if any(abs(parse(written) - value) < 1e-9 for value in values)
    ]


def geometry_parts(message: str) -> list[str]:
    """Os pedaços de uma mensagem que são geometria calculada."""
    return [
        part
        for part in message.split("\n\n")
        if part.startswith(TITLE) or part.startswith(PROPOSAL_TITLE)
    ]


# --- a convenção, uma fonte só ----------------------------------------------


@pytest.mark.parametrize(
    "instructions",
    [furnisher_prompt.INSTRUCTIONS, inspector_prompt.INSTRUCTIONS],
    ids=["mobiliador", "fiscal"],
)
def test_both_instructions_carry_the_same_convention(instructions: str) -> None:
    assert instructions.count(CONVENTION) == 1


@pytest.mark.parametrize(
    "instructions",
    [furnisher_prompt.INSTRUCTIONS, inspector_prompt.INSTRUCTIONS],
    ids=["mobiliador", "fiscal"],
)
def test_the_convention_is_not_written_twice(instructions: str) -> None:
    """Uma cópia à mão ao lado do texto compartilhado divergiria dele um dia."""
    for sentence in (
        "A frente aponta para o sul em 0, para o leste em 90, para o norte em "
        "180 e para o oeste em 270.",
        "A frente (front) do móvel é o lado sul",
        "canto inferior esquerdo do retângulo",
    ):
        assert sentence in CONVENTION
        assert instructions.count(sentence) == 1


def test_the_inspector_is_told_to_keep_suggestions_inside_the_room() -> None:
    text = inspector_prompt.INSTRUCTIONS

    assert "fica inteiro dentro dos limites do cômodo" in text
    assert "A rotação sugerida é a da convenção" in text
    assert "a posição e a rotação finais do móvel" in text


# --- o bloco: cômodos ------------------------------------------------------------


def test_each_room_has_its_limits() -> None:
    lines = plan_geometry(plan()).splitlines()

    assert lines[:6] == [
        TITLE,
        "Cômodos:",
        "- Sala (r1): x de 0.00 a 4.00, y de 0.00 a 3.00.",
        "- Cozinha (r2): x de 4.00 a 7.00, y de 0.00 a 3.00.",
        "- Quarto (r3): x de 0.00 a 4.00, y de 3.00 a 6.00.",
        "- Banheiro (r4): x de 4.00 a 7.00, y de 3.00 a 6.00.",
    ]


# --- o bloco: portas ------------------------------------------------------------


def door_lines() -> list[str]:
    lines = plan_geometry(plan()).splitlines()
    return lines[lines.index("Portas:") + 1 :]


def test_an_internal_door_in_a_horizontal_wall_opens_to_both_rooms() -> None:
    """A porta na parede norte da sala é a porta na parede sul do quarto."""
    assert (
        "- Porta na parede norte do cômodo Sala (r1): vão em y = 3.00, de x = 1.00 "
        "a x = 1.90. A face voltada para o sul abre para Sala (r1); a face "
        "voltada para o norte abre para Quarto (r3)."
    ) in door_lines()


def test_an_internal_door_in_a_vertical_wall_opens_to_both_rooms() -> None:
    assert (
        "- Porta na parede leste do cômodo Sala (r1): vão em x = 4.00, de y = 1.00 "
        "a y = 1.90. A face voltada para o oeste abre para Sala (r1); a face "
        "voltada para o leste abre para Cozinha (r2)."
    ) in door_lines()


def test_the_entrance_opens_to_the_exterior_on_one_face() -> None:
    assert (
        "- Porta na parede oeste do cômodo Sala (r1): vão em x = 0.00, de y = 0.50 "
        "a y = 1.40. A face voltada para o leste abre para Sala (r1); a face "
        "voltada para o oeste abre para o exterior."
    ) in door_lines()


def test_the_door_faces_agree_with_what_the_inspection_measures() -> None:
    """Cada face que `inspect` mede aparece no bloco, voltada para o mesmo lado.

    A face medida para dentro do cômodo aponta na direção em que a faixa
    diante da porta entra nele.
    """
    furnished = FurnishedPlan(plan=plan(), proposal=proposal())
    lines = door_lines()
    faces = door_faces(furnished, CATALOG, RULES)

    assert len(faces) == 7  # 3 portas internas com duas faces e a entrada

    for face in faces:
        said = (
            f"face voltada para o {WALL_NAMES[face.normal]} abre para "
            f"{face.room.name} ({face.room.id})"
        )
        assert any(said in line for line in lines), said


# --- o bloco: móveis ------------------------------------------------------------


@pytest.mark.parametrize(
    ("rotation", "span_x", "span_y", "front"),
    [
        (0, "x de 1.00 a 2.60", "y de 3.00 a 3.60", "sul"),
        (90, "x de 1.00 a 1.60", "y de 3.00 a 4.60", "leste"),
        (180, "x de 1.00 a 2.60", "y de 3.00 a 3.60", "norte"),
        (270, "x de 1.00 a 1.60", "y de 3.00 a 4.60", "oeste"),
    ],
)
def test_the_footprint_and_the_front_follow_the_rotation(
    rotation: int, span_x: str, span_y: str, front: str
) -> None:
    """O guarda-roupa tem 1,60 m de largura e 0,60 m de profundidade.

    Em 90 e 270 ele fica deitado de lado, e a frente gira no sentido
    anti-horário: sul, leste, norte, oeste.
    """
    wardrobe = Placement(
        id="m4", room_id="r3", item_id="wardrobe", x=1.0, y=3.0, rotation=rotation
    )

    lines = proposal_geometry(plan(), CATALOG, only(wardrobe)).splitlines()

    assert lines == [
        PROPOSAL_TITLE,
        f"- m4, Guarda-roupa (wardrobe), no cômodo Quarto (r3), rotação {rotation}: "
        f"ocupa {span_x}, {span_y}; frente para o {front}. "
        "Catálogo: width 1.60, depth 0.60.",
    ]


def test_an_unknown_item_stays_in_the_list_without_a_footprint() -> None:
    ghost = Placement(id="m9", room_id="r3", item_id="piano", x=1, y=3, rotation=0)

    lines = proposal_geometry(plan(), CATALOG, only(ghost)).splitlines()

    assert lines[1] == (
        "- m9, item piano, que não existe no catálogo, no cômodo Quarto (r3): "
        "sem retângulo calculado."
    )


def test_an_empty_proposal_says_so() -> None:
    empty = FurnishingProposal(placements=[], omissions=[], design_notes="")

    assert proposal_geometry(plan(), CATALOG, empty).splitlines()[1] == "- nenhum."


# --- nenhum número dos parâmetros -----------------------------------------------


def test_the_odd_parameters_really_differ_from_the_geometry() -> None:
    """A fixture de valores ímpares só serve se nada coincidir por acaso."""
    values = odd_values()
    rules = load_furniture_rules(CATALOG, ODD_RULES_PATH)

    # Os três parâmetros e uma faixa por item do catálogo com lado de uso.
    assert rules.door_clearance_depth.value in values
    assert rules.turning_diameter.value in values
    assert set(rules.use_zone_depth.values.values()) <= set(values)
    assert len(values) >= 3


def test_the_geometry_takes_no_parameters() -> None:
    """Sem os parâmetros na assinatura, o bloco não tem de onde tirar um valor."""
    for function in (plan_geometry, proposal_geometry, geometry_block):
        assert set(signatures.signature(function).parameters) <= {
            "plan",
            "catalog",
            "proposal",
        }


def test_no_parameter_value_reaches_the_geometry_block() -> None:
    values = odd_values()
    block = geometry_block(plan(), CATALOG, cramped())

    assert len(numbers_in(block)) > 50
    assert not leaked(block, values)


def test_no_parameter_value_reaches_the_geometry_of_either_message() -> None:
    """As mensagens inteiras, montadas com violações medidas pelos parâmetros.

    O pedaço das verificações traz o exigido, e deve; os pedaços de
    geometria, nunca. A primeira conferência prova que os parâmetros de
    valores ímpares estavam em jogo.
    """
    values = odd_values()
    rules = load_furniture_rules(CATALOG, ODD_RULES_PATH)
    furnished = FurnishedPlan(plan=plan(), proposal=cramped())
    violations = inspect(furnished, CATALOG, rules, Profile(accessible=True), request())

    to_inspector = inspector_message(
        furnished, violations, Profile(accessible=True), CATALOG
    )
    to_furnisher = furnisher_message(
        plan(),
        CATALOG,
        request(),
        Profile(accessible=True),
        cramped(),
        [Feedback(message=v.message, suggestion="") for v in violations],
    )

    assert leaked(to_inspector, values)
    assert leaked(to_furnisher, values)

    for message in (to_inspector, to_furnisher):
        geometry = "\n".join(geometry_parts(message))
        assert TITLE in geometry
        assert PROPOSAL_TITLE in geometry
        assert not leaked(geometry, values), geometry


# --- onde o bloco entra ---------------------------------------------------------


def test_the_inspector_gets_the_whole_block_after_the_plan() -> None:
    furnished = FurnishedPlan(plan=plan(), proposal=cramped())
    violations = inspect(furnished, CATALOG, RULES, Profile(), request())

    message = inspector_message(furnished, violations, Profile(), CATALOG)

    block = geometry_block(plan(), CATALOG, cramped())
    assert block in message
    assert message.index('"placements"') < message.index(TITLE)


def test_the_furnisher_gets_rooms_first_and_the_previous_furniture_later() -> None:
    first = furnisher_message(plan(), CATALOG, request(), Profile())
    later = furnisher_message(
        plan(),
        CATALOG,
        request(),
        Profile(),
        cramped(),
        [Feedback(message="Problema.", suggestion="")],
    )

    assert plan_geometry(plan()) in first
    assert PROPOSAL_TITLE not in first

    geometry = proposal_geometry(plan(), CATALOG, cramped())
    assert geometry in later
    assert later.index("Proposta que você fez antes") < later.index(geometry)
    assert later.index(geometry) < later.index("O verificador reprovou")
