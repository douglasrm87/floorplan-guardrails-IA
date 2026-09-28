"""Desenho da planta mobiliada, por cima do desenho do P1.

A planta sai de `draw_plan`, com os cômodos em violação já marcados; este
módulo acrescenta os móveis, as faixas medidas e o quadrado de giro.

Convenções de desenho
---------------------
Móvel é um retângulo cinza com o id e o nome curto, e um triângulo pequeno
junto ao lado da frente, apontando para fora. Faixa é um retângulo
tracejado com a profundidade **exigida**: o que o código pediu, não o que
sobrou. O quadrado de giro é o maior quadrado livre que a varredura achou,
com o círculo inscrito.

As faixas e o quadrado saem das mesmas funções que as regras usam
(`door_faces`, `use_zones`, `largest_free_square`), então a figura mostra
exatamente o que foi medido.

Móvel citado em violação sai com a **marcação tripla do P1**: camada de
alerta com transparência, traço grosso vermelho com hachura e a contagem
escrita. Faixa que falhou sai vermelha e com traço mais grosso. A cor
sozinha nunca carrega a informação, pelo mesmo motivo explicado em
`renderer`.
"""

from collections.abc import Sequence
from typing import Literal

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Circle, Polygon, Rectangle
from matplotlib.text import Text

from floorplan_guardrails.furniture import (
    Catalog,
    Footprint,
    FurnishedPlan,
    Placement,
    Profile,
    footprint,
    side_direction,
)
from floorplan_guardrails.furniture_rules import FurnitureRules
from floorplan_guardrails.geometry import TOLERANCE, span_x, span_y
from floorplan_guardrails.inspection import (
    FLOAT_MARGIN,
    FurnitureViolation,
    clearance_box,
    door_faces,
    item_fails,
    largest_free_square,
    room_furniture,
    turning_rooms,
    use_zones,
    zones_by_placement,
)
from floorplan_guardrails.renderer import (
    BROKEN_EDGE,
    BROKEN_FILL,
    BROKEN_FILL_ALPHA,
    BROKEN_HATCH,
    BROKEN_WALL_WIDTH,
    INK,
    INK_SOFT,
    SURFACE,
    draw_plan,
    plan_bounds,
)
from floorplan_guardrails.schema import Room
from floorplan_guardrails.validator import number

#: Preenchimento e traço de um móvel sem problema.
FURNITURE_FILL = "#d9d7d2"
FURNITURE_EDGE = INK_SOFT
FURNITURE_WIDTH = 0.8

#: Marca da frente do móvel.
FRONT_MARK = INK_SOFT

#: Traço das faixas e do quadrado de giro que passaram.
ZONE_EDGE = INK_SOFT
ZONE_WIDTH = 0.8
BROKEN_ZONE_WIDTH = 1.6
ZONE_STYLE = (0, (4, 3))

#: Como cada estado de rodada aparece no título.
RoundStatus = Literal["approved", "rejected", "declined", "not_converged"]

ROUND_STATES: dict[str, str] = {
    "approved": "aprovada",
    "rejected": "reprovada",
    "declined": "desistência",
    "not_converged": "sem acordo",
}

#: Abaixo desta medida, em metros, o rótulo do móvel leva só o id.
SMALL_FURNITURE = 0.5

# As camadas do P1 vão de 1 (piso) a 5 (rótulos). Faixas e móveis ficam entre
# as paredes e as portas. O quadrado de giro fica acima do rótulo do cômodo,
# que é movido para dentro dele; os rótulos dos móveis, por cima de tudo.
ZONE_LAYER = 2.5
FURNITURE_LAYER = 3.2
TURNING_LAYER = 5.2
LABEL_LAYER = 5.5


def cited_placements(violations: Sequence[FurnitureViolation]) -> dict[str, int]:
    """Quantas violações citam cada móvel."""
    counts: dict[str, int] = {}

    for violation in violations:
        for placement_id in violation.placement_ids:
            counts[placement_id] = counts.get(placement_id, 0) + 1

    return counts


def counted(count: int) -> str:
    """A contagem por extenso, como no P1."""
    return "1 violação" if count == 1 else f"{count} violações"


def short_name(name: str) -> str:
    """O nome do catálogo sem o complemento entre parênteses."""
    return name.split(" (")[0]


def front_mark(box: Footprint, placement: Placement) -> list[tuple[float, float]]:
    """Os três pontos do triângulo que aponta para a frente do móvel."""
    direction = side_direction("front", placement.rotation)
    size = min(0.12, 0.3 * (box.x1 - box.x0), 0.3 * (box.y1 - box.y0))
    middle_x = (box.x0 + box.x1) / 2
    middle_y = (box.y0 + box.y1) / 2

    if direction == "south":
        return [
            (middle_x - size, box.y0 + size),
            (middle_x + size, box.y0 + size),
            (middle_x, box.y0),
        ]
    if direction == "north":
        return [
            (middle_x - size, box.y1 - size),
            (middle_x + size, box.y1 - size),
            (middle_x, box.y1),
        ]
    if direction == "east":
        return [
            (box.x1 - size, middle_y - size),
            (box.x1 - size, middle_y + size),
            (box.x1, middle_y),
        ]
    return [
        (box.x0 + size, middle_y - size),
        (box.x0 + size, middle_y + size),
        (box.x0, middle_y),
    ]


def draw_zone(ax: Axes, box: Footprint, failed: bool, gid: str) -> None:
    """Uma faixa tracejada: neutra se passou, vermelha e grossa se falhou."""
    patch = Rectangle(
        (box.x0, box.y0),
        box.x1 - box.x0,
        box.y1 - box.y0,
        facecolor="none",
        edgecolor=BROKEN_EDGE if failed else ZONE_EDGE,
        linewidth=BROKEN_ZONE_WIDTH if failed else ZONE_WIDTH,
        linestyle=ZONE_STYLE,
        zorder=ZONE_LAYER,
    )
    patch.set_gid(gid)
    ax.add_patch(patch)


def draw_furniture(
    ax: Axes, placement: Placement, catalog: Catalog, broken: int
) -> None:
    """Um móvel: corpo, marca da frente e rótulo, com a marcação tripla se citado."""
    item = catalog[placement.item_id]
    box = footprint(placement, catalog)
    width = box.x1 - box.x0
    height = box.y1 - box.y0

    body = Rectangle(
        (box.x0, box.y0),
        width,
        height,
        facecolor=FURNITURE_FILL,
        edgecolor=BROKEN_EDGE if broken else FURNITURE_EDGE,
        linewidth=BROKEN_WALL_WIDTH if broken else FURNITURE_WIDTH,
        hatch=BROKEN_HATCH if broken else None,
        zorder=FURNITURE_LAYER,
    )
    body.set_gid(f"furniture:{placement.id}")
    ax.add_patch(body)

    if broken:
        alert = Rectangle(
            (box.x0, box.y0),
            width,
            height,
            facecolor=BROKEN_FILL,
            alpha=BROKEN_FILL_ALPHA,
            edgecolor="none",
            zorder=FURNITURE_LAYER + 0.1,
        )
        alert.set_gid(f"alert:{placement.id}")
        ax.add_patch(alert)

    front = Polygon(
        front_mark(box, placement),
        closed=True,
        facecolor=FRONT_MARK,
        edgecolor="none",
        zorder=FURNITURE_LAYER + 0.2,
    )
    front.set_gid(f"front:{placement.id}")
    ax.add_patch(front)

    # Num móvel pequeno, como a mesa de cabeceira, o nome não cabe: fica o id.
    lines = [placement.id]
    if min(width, height) >= SMALL_FURNITURE:
        lines.append(short_name(item.name))
    if broken:
        lines.append(counted(broken))

    label = ax.text(
        (box.x0 + box.x1) / 2,
        (box.y0 + box.y1) / 2,
        "\n".join(lines),
        ha="center",
        va="center",
        fontsize=6,
        # Como no P1: o texto usa tinta de texto, nunca a cor do alerta.
        color=INK,
        linespacing=1.3,
        zorder=LABEL_LAYER,
        bbox=(
            {
                "facecolor": SURFACE,
                "edgecolor": "none",
                "boxstyle": "round,pad=0.25",
                "alpha": 0.92,
            }
            if broken
            else None
        ),
    )
    label.set_gid(f"label:{placement.id}")


def draw_turning(ax: Axes, square: Footprint, required: float, room_id: str) -> None:
    """O maior quadrado livre de um cômodo, com o círculo inscrito."""
    side = square.x1 - square.x0
    if side <= 0:
        return

    failed = side < required - TOLERANCE
    edge = BROKEN_EDGE if failed else ZONE_EDGE
    width = BROKEN_ZONE_WIDTH if failed else ZONE_WIDTH

    outline = Rectangle(
        (square.x0, square.y0),
        side,
        side,
        facecolor="none",
        edgecolor=edge,
        linewidth=width,
        linestyle=ZONE_STYLE,
        zorder=TURNING_LAYER,
    )
    outline.set_gid(f"turning:{room_id}")
    ax.add_patch(outline)

    circle = Circle(
        ((square.x0 + square.x1) / 2, (square.y0 + square.y1) / 2),
        side / 2,
        facecolor="none",
        edgecolor=edge,
        linewidth=width,
        zorder=TURNING_LAYER,
    )
    circle.set_gid(f"turning-circle:{room_id}")
    ax.add_patch(circle)

    caption = ax.text(
        (square.x0 + square.x1) / 2,
        square.y1 - 0.05,
        f"giro {number(side)} m",
        ha="center",
        va="top",
        fontsize=6,
        color=INK,
        zorder=LABEL_LAYER,
    )
    caption.set_gid(f"turning-label:{room_id}")


def room_label(ax: Axes, room: Room) -> Text | None:
    """O rótulo que `draw_plan` escreveu para o cômodo, se estiver no eixo.

    O P1 escreve um texto por cômodo, no centro do retângulo, com o nome na
    primeira linha, a área na segunda e, se houver, a contagem de violações
    na terceira. É por esse conteúdo e por essa posição que o texto é achado,
    e não pela ordem em `ax.texts`. Se o P1 mudar esse formato, a função
    devolve `None`; o teste `test_every_room_label_of_the_p1_is_found`
    quebra, e o desenho continua saindo, só com o rótulo no lugar original.
    """
    horizontal = span_x(room)
    vertical = span_y(room)
    middle_x = (horizontal.start + horizontal.end) / 2
    middle_y = (vertical.start + vertical.end) / 2

    for text in ax.texts:
        name = text.get_text().split("\n")[0]
        x, y = text.get_position()
        if (
            name == room.name
            and abs(x - middle_x) < FLOAT_MARGIN
            and abs(y - middle_y) < FLOAT_MARGIN
        ):
            return text

    return None


def move_room_labels(ax: Axes, furnished: FurnishedPlan, catalog: Catalog) -> None:
    """Tira o rótulo do cômodo de cima dos móveis.

    O P1 escreve o nome, a área e a contagem de violações no centro de cada
    cômodo, onde num quarto costuma estar a cama. Os três estão num único
    texto, que vai inteiro para o centro do maior quadrado livre do cômodo.
    """
    for room in furnished.plan.rooms:
        furniture = room_furniture(furnished, catalog, room.id)
        label = room_label(ax, room)
        if not furniture or label is None:
            continue

        square = largest_free_square(room, [box for _, box in furniture])
        if square.x1 - square.x0 > 0:
            label.set_position(
                ((square.x0 + square.x1) / 2, (square.y0 + square.y1) / 2)
            )


def draw_furnished(
    furnished: FurnishedPlan,
    catalog: Catalog,
    rules: FurnitureRules,
    profile: Profile,
    violations: Sequence[FurnitureViolation] = (),
    ax: Axes | None = None,
    title: str | None = None,
    show_zones: bool = False,
    bounds: tuple[float, float, float, float] | None = None,
) -> Axes:
    """Desenha a planta mobiliada e devolve o eixo em que ela ficou.

    Com `show_zones`, desenha todas as faixas de porta e de uso; sem ele,
    só as que falharam. O quadrado de giro aparece sempre que o giro é
    verificado, isto é, com perfil acessível e nos tipos de cômodo do
    parâmetro.
    """
    ax = draw_plan(furnished.plan, violations, ax=ax, title=title, bounds=bounds)
    move_room_labels(ax, furnished, catalog)

    for face in door_faces(furnished, catalog, rules):
        if show_zones or not face.ok:
            draw_zone(
                ax,
                clearance_box(face.base, face.normal, face.required),
                failed=not face.ok,
                gid=f"zone:door:{face.room.id}:{face.wall}",
            )

    for placement, zones in zones_by_placement(use_zones(furnished, catalog, rules)):
        fails = item_fails(zones, catalog[placement.item_id].use_mode)

        for zone in zones:
            # Num móvel "any" aprovado, um lado curto não é falha.
            failed = fails and not zone.ok
            if show_zones or failed:
                draw_zone(
                    ax,
                    clearance_box(zone.base, zone.normal, zone.required),
                    failed=failed,
                    gid=f"zone:use:{placement.id}:{zone.side}",
                )

    counts = cited_placements(violations)

    for placement in furnished.proposal.placements:
        if placement.item_id in catalog:
            draw_furniture(ax, placement, catalog, counts.get(placement.id, 0))

    for room in turning_rooms(furnished.plan, rules, profile):
        furniture = room_furniture(furnished, catalog, room.id)
        square = largest_free_square(room, [box for _, box in furniture])
        draw_turning(ax, square, rules.turning_diameter.value, room.id)

    return ax


def round_title(
    round_number: int,
    status: RoundStatus,
    violations: Sequence[FurnitureViolation],
) -> str:
    """O título de uma rodada: "Rodada 2: reprovada, 3 violações"."""
    title = f"Rodada {round_number}: {ROUND_STATES[status]}"

    if violations:
        title += f", {counted(len(violations))}"

    return title


def draw_negotiation(
    history: Sequence[tuple[FurnishedPlan, Sequence[FurnitureViolation], RoundStatus]],
    catalog: Catalog,
    rules: FurnitureRules,
    profile: Profile,
) -> Figure:
    """Desenha as rodadas lado a lado, na mesma escala.

    Recebe uma tripla (planta mobiliada, violações, estado) por rodada, na
    ordem em que a negociação as produziu.
    """
    if not history:
        raise ValueError("não há rodadas para desenhar")

    bounds = plan_bounds([furnished.plan for furnished, _, _ in history])

    figure, axes = plt.subplots(
        1,
        len(history),
        figsize=(5 * len(history), 4.5),
        squeeze=False,
    )
    figure.set_facecolor(SURFACE)

    for column, (furnished, violations, status) in enumerate(history):
        draw_furnished(
            furnished,
            catalog,
            rules,
            profile,
            violations,
            ax=axes[0][column],
            title=round_title(column + 1, status, violations),
            bounds=bounds,
        )

    figure.tight_layout()
    return figure
