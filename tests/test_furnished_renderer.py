"""Testes do desenho da planta mobiliada.

Como no desenho do P1, o que se afirma é o que o desenho promete: uma marca
para cada móvel, o móvel com problema distinguível por mais de um meio, as
faixas que falharam sempre à vista e as rodadas lado a lado na mesma escala.

Cada marca leva um `gid` (`furniture:m2`, `zone:use:m2:left`, `turning:r3`),
e é por ele que os testes a encontram.

Os testes usam só as fixtures de `tests/fixtures/p2/`, nunca `config/`.
"""

import json
from pathlib import Path
from typing import get_args

import pytest
from matplotlib.colors import to_rgba

from floorplan_guardrails.furnished_renderer import (
    BROKEN_ZONE_WIDTH,
    ROUND_STATES,
    draw_furnished,
    draw_negotiation,
    move_room_labels,
    room_label,
    round_title,
)
from floorplan_guardrails.furniture import (
    FurnishedPlan,
    FurnishingRequest,
    Profile,
    footprint,
    load_catalog,
)
from floorplan_guardrails.furniture_rules import load_furniture_rules
from floorplan_guardrails.inspection import FurnitureViolation, inspect
from floorplan_guardrails.negotiation import RoundStatus
from floorplan_guardrails.renderer import BROKEN_EDGE, draw_plan, save_png

FIXTURES = Path(__file__).parent / "fixtures" / "p2"
CATALOG = load_catalog(FIXTURES / "catalog.yaml")
RULES = load_furniture_rules(CATALOG, FIXTURES / "furniture_rules.yaml")
WHEELCHAIR = Profile(accessible=True)


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def furnished(**changes: dict) -> FurnishedPlan:
    """A proposta de referência, com os móveis indicados mudados."""
    proposal = read("proposal.json")
    for placement in proposal["placements"]:
        placement.update(changes.get(placement["id"], {}))

    return FurnishedPlan(plan=read("plan.json"), proposal=proposal)


def cramped() -> FurnishedPlan:
    """A cama a 0,35 m da porta do quarto e o guarda-roupa a 0,20 m da cama."""
    return furnished(m2={"y": 3.35}, m4={"x": 2.8})


def violations_of(
    plan: FurnishedPlan, profile: Profile | None = None
) -> list[FurnitureViolation]:
    return inspect(
        plan,
        CATALOG,
        RULES,
        profile or Profile(),
        FurnishingRequest.model_validate(read("request.json")),
    )


def draw(plan: FurnishedPlan, profile: Profile | None = None, show_zones: bool = False):
    profile = profile or Profile()
    return draw_furnished(
        plan,
        CATALOG,
        RULES,
        profile,
        violations_of(plan, profile),
        show_zones=show_zones,
    )


def marks(ax, prefix: str) -> dict:
    """As marcas cujo gid começa com `prefix`, indexadas pelo gid."""
    artists = [*ax.patches, *ax.texts]
    return {
        artist.get_gid(): artist
        for artist in artists
        if (artist.get_gid() or "").startswith(prefix)
    }


def is_red(patch) -> bool:
    return tuple(patch.get_edgecolor()) == to_rgba(BROKEN_EDGE)


# --- os móveis ---------------------------------------------------------------


def test_every_placement_gets_a_body_a_front_mark_and_a_label() -> None:
    plan = furnished()
    ax = draw(plan)

    ids = [p.id for p in plan.proposal.placements]
    assert sorted(marks(ax, "furniture:")) == sorted(f"furniture:{i}" for i in ids)
    assert sorted(marks(ax, "front:")) == sorted(f"front:{i}" for i in ids)
    assert sorted(marks(ax, "label:")) == sorted(f"label:{i}" for i in ids)


def test_the_body_is_drawn_on_the_footprint() -> None:
    plan = furnished()
    wardrobe = next(p for p in plan.proposal.placements if p.id == "m4")

    body = marks(draw(plan), "furniture:")["furniture:m4"]

    box = footprint(wardrobe, CATALOG)
    assert body.get_xy() == pytest.approx((box.x0, box.y0))
    assert body.get_width() == pytest.approx(box.x1 - box.x0)
    assert body.get_height() == pytest.approx(box.y1 - box.y0)


def test_the_front_mark_points_where_the_front_is() -> None:
    """O guarda-roupa está a 270 graus: a frente aponta para o oeste."""
    plan = furnished()
    wardrobe = next(p for p in plan.proposal.placements if p.id == "m4")

    front = marks(draw(plan), "front:")["front:m4"]

    tip_x = min(x for x, _ in front.get_xy())
    assert tip_x == pytest.approx(footprint(wardrobe, CATALOG).x0)


def test_an_approved_layout_has_no_red_mark() -> None:
    ax = draw(furnished(), show_zones=True)

    assert not any(is_red(patch) for patch in marks(ax, "furniture:").values())
    assert not any(is_red(patch) for patch in marks(ax, "zone:").values())


def test_the_furniture_in_trouble_is_marked_in_red() -> None:
    bodies = marks(draw(cramped()), "furniture:")

    red = {gid for gid, patch in bodies.items() if is_red(patch)}
    assert red == {"furniture:m2", "furniture:m4"}


def test_the_furniture_in_trouble_is_readable_without_colour() -> None:
    """A garantia de acessibilidade do P1, estendida aos móveis.

    Quem não separa o vermelho do cinza precisa da hachura no corpo do móvel
    e da contagem escrita no rótulo.
    """
    ax = draw(cramped())

    bodies = marks(ax, "furniture:")
    hatched = {gid for gid, patch in bodies.items() if patch.get_hatch()}
    assert hatched == {"furniture:m2", "furniture:m4"}

    labels = {gid: text.get_text() for gid, text in marks(ax, "label:").items()}
    assert "3 violações" in labels["label:m2"]  # a porta e as duas faixas
    assert "2 violações" in labels["label:m4"]
    assert not any("violaç" in labels[f"label:{i}"] for i in ("m1", "m3", "m5"))


def test_a_small_piece_is_labelled_by_its_id_only() -> None:
    ax = draw(furnished())

    labels = {gid: text.get_text() for gid, text in marks(ax, "label:").items()}

    assert labels["label:m3"] == "m3"  # a mesa de cabeceira tem 0,45 x 0,40 m
    assert labels["label:m2"] == "m2\nCama de casal"


def test_the_room_label_moves_off_the_furniture() -> None:
    """O P1 escreve o nome no centro do quarto, que cai em cima da cama."""
    plan = furnished()
    bed = footprint(next(p for p in plan.proposal.placements if p.id == "m2"), CATALOG)

    ax = draw(plan)

    room_label = next(t for t in ax.texts if t.get_text().startswith("Quarto"))
    x, y = room_label.get_position()
    assert not (bed.x0 < x < bed.x1 and bed.y0 < y < bed.y1)


def test_every_room_label_of_the_p1_is_found() -> None:
    """O alarme para uma mudança no P1.

    `room_label` acha o texto que `draw_plan` escreve pelo conteúdo (nome na
    primeira linha) e pela posição (centro do cômodo). Se o P1 mudar isso, o
    desenho não quebra, só deixa o rótulo onde está; é este teste que avisa.
    """
    plan = cramped()
    ax = draw_plan(plan.plan, violations_of(plan))

    for room in plan.plan.rooms:
        label = room_label(ax, room)
        assert label is not None, f"rótulo do {room.id} não encontrado"
        assert label.get_text().startswith(f"{room.name}\n")

    bedroom = next(room for room in plan.plan.rooms if room.id == "r3")
    assert room_label(ax, bedroom).get_text().endswith("3 violações")


def test_the_room_violation_count_moves_with_the_label() -> None:
    """Nome, área e contagem são um texto só no P1, e vão juntos."""
    plan = cramped()

    ax = draw(plan)

    moved = [t for t in ax.texts if t.get_text().startswith("Quarto\n")]
    assert len(moved) == 1
    assert moved[0].get_text() == "Quarto\n12,00 m²\n3 violações"
    assert moved[0].get_position() != pytest.approx((2.0, 4.5))  # o centro


def test_a_missing_room_label_does_not_break_the_drawing() -> None:
    plan = furnished()
    ax = draw_plan(plan.plan)
    for text in list(ax.texts):
        text.remove()

    move_room_labels(ax, plan, CATALOG)

    assert len(ax.texts) == 0


# --- as faixas ---------------------------------------------------------------


def test_failed_zones_are_drawn_even_without_show_zones() -> None:
    zones = marks(draw(cramped()), "zone:")

    assert set(zones) == {
        "zone:door:r3:south",
        "zone:use:m2:right",
        "zone:use:m4:front",
    }
    for patch in zones.values():
        assert is_red(patch)
        assert patch.get_linewidth() == pytest.approx(BROKEN_ZONE_WIDTH)
        assert patch.get_linestyle() != "solid"


def test_the_failed_door_zone_has_the_required_depth() -> None:
    zone = marks(draw(cramped()), "zone:door:")["zone:door:r3:south"]

    assert zone.get_xy() == pytest.approx((1.0, 3.0))
    assert zone.get_width() == pytest.approx(0.9)
    assert zone.get_height() == pytest.approx(0.8)


def test_show_zones_draws_every_door_face_and_use_side() -> None:
    zones = marks(draw(furnished(), show_zones=True), "zone:")

    assert len([gid for gid in zones if gid.startswith("zone:door:")]) == 7
    assert sorted(gid for gid in zones if gid.startswith("zone:use:")) == [
        "zone:use:m1:front",
        "zone:use:m2:left",
        "zone:use:m2:right",
        "zone:use:m4:front",
        "zone:use:m5:front",
    ]


def test_a_short_side_of_an_any_item_that_passes_is_not_red() -> None:
    """A cama de solteiro encostada na parede tem um lado livre: está aprovada."""
    plan = furnished(m2={"item_id": "bed_single", "x": 0.0})

    ax = draw_furnished(plan, CATALOG, RULES, Profile(), [], show_zones=True)

    left = marks(ax, "zone:use:m2:")["zone:use:m2:left"]
    assert not is_red(left)
    assert "zone:use:m2:left" not in marks(
        draw_furnished(plan, CATALOG, RULES, Profile(), []), "zone:"
    )


# --- o giro ------------------------------------------------------------------


def test_turning_is_not_drawn_without_the_profile() -> None:
    assert marks(draw(furnished()), "turning") == {}


def test_turning_is_drawn_with_square_and_circle_in_each_checked_room() -> None:
    ax = draw(furnished(), WHEELCHAIR)

    squares = marks(ax, "turning:")
    circles = marks(ax, "turning-circle:")
    assert set(squares) == {"turning:r1", "turning:r3", "turning:r4"}
    assert set(circles) == {f"turning-circle:{r}" for r in ("r1", "r3", "r4")}

    bedroom = squares["turning:r3"]
    assert bedroom.get_width() == pytest.approx(1.20)
    assert circles["turning-circle:r3"].get_radius() == pytest.approx(0.60)
    assert is_red(bedroom)
    assert not is_red(squares["turning:r4"])


def test_the_turning_square_says_its_side() -> None:
    labels = marks(draw(furnished(), WHEELCHAIR), "turning-label:")

    assert labels["turning-label:r3"].get_text() == "giro 1,20 m"


# --- a negociação ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "count", "expected"),
    [
        ("rejected", 3, "Rodada 1: reprovada, 3 violações"),
        ("rejected", 1, "Rodada 1: reprovada, 1 violação"),
        ("approved", 0, "Rodada 1: aprovada"),
        ("declined", 0, "Rodada 1: desistência"),
        ("not_converged", 2, "Rodada 1: sem acordo, 2 violações"),
        ("infeasible", 1, "Planta inviável, 1 violação"),
    ],
)
def test_round_title(status: str, count: int, expected: str) -> None:
    violations = violations_of(cramped())[:count]

    assert round_title(1, status, violations) == expected


def test_every_round_state_has_a_word_in_the_title() -> None:
    # Os estados são definidos em `negotiation`; o desenho só os traduz.
    assert set(ROUND_STATES) == set(get_args(RoundStatus))


def test_negotiation_draws_one_panel_per_round_on_one_scale() -> None:
    first = cramped()
    history = [
        (first, violations_of(first), "rejected"),
        (furnished(), [], "approved"),
    ]

    figure = draw_negotiation(history, CATALOG, RULES, Profile())

    assert len(figure.axes) == 2
    assert figure.axes[0].get_title() == "Rodada 1: reprovada, 3 violações"
    assert figure.axes[1].get_title() == "Rodada 2: aprovada"
    assert len({(ax.get_xlim(), ax.get_ylim()) for ax in figure.axes}) == 1


def test_negotiation_without_rounds_is_refused() -> None:
    with pytest.raises(ValueError, match="não há rodadas"):
        draw_negotiation([], CATALOG, RULES, Profile())


def test_the_p1_png_export_serves_the_furnished_drawing(tmp_path: Path) -> None:
    ax = draw(cramped(), WHEELCHAIR, show_zones=True)

    path = save_png(ax.figure, tmp_path / "mobiliada.png")

    assert path.read_bytes().startswith(b"\x89PNG")
