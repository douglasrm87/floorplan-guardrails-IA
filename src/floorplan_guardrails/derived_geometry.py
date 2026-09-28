"""A geometria que o código calcula e entrega aos dois agentes.

A planta em JSON já contém tudo, mas em forma que pede conta: o cômodo vem
como canto mais largura, a porta como offset ao longo de uma parede, o
móvel como canto mais rotação. Na Fase 4b o fiscal errou justamente essas
contas, e sugeriu um guarda-roupa até x = 4,50 num quarto que termina em
x = 4,00. Este módulo faz as contas uma vez, em código, e escreve o
resultado em texto para a mensagem do mobiliador e para a do fiscal.

O que entra
-----------
- os limites de cada cômodo, em x e em y;
- o vão de cada porta, em coordenadas absolutas, com o cômodo para onde
  cada face abre;
- para cada móvel posicionado, o retângulo que ele ocupa, a direção da
  frente e as medidas do catálogo.

O que não entra
---------------
Nada que dependa de `config/furniture_rules.yaml`: nem faixa de uso, nem
profundidade livre diante de porta, nem quadrado de giro. Esses retângulos
existem em `inspection`, mas cada um tem uma profundidade que vem dos
parâmetros, e o mobiliador não pode aprender a exigência por aqui. Por isso
nenhuma função daqui recebe os parâmetros: só a planta, o catálogo e a
proposta.
"""

from floorplan_guardrails.furniture import (
    Catalog,
    FurnishingProposal,
    Placement,
    footprint,
    side_direction,
)
from floorplan_guardrails.geometry import OPPOSITE, span_x, span_y
from floorplan_guardrails.renderer import opening_endpoints
from floorplan_guardrails.schema import Door, FloorPlan, Room
from floorplan_guardrails.validator import WALL_NAMES, rooms_by_id

#: O título do bloco, que também serve para achá-lo na mensagem.
TITLE = "Geometria calculada pelo código, em metros, nas coordenadas da casa:"

#: O título da parte dos móveis.
PROPOSAL_TITLE = "Móveis da proposta, calculados pelo código, em metros:"


def coordinate(value: float) -> str:
    """Uma coordenada com ponto decimal, como o modelo a escreve no JSON."""
    return f"{value:.2f}"


def room_label(room: Room) -> str:
    """O cômodo pelo nome e pelo id, como aparece no bloco."""
    return f"{room.name} ({room.id})"


def room_line(room: Room) -> str:
    """Os limites de um cômodo."""
    horizontal = span_x(room)
    vertical = span_y(room)

    return (
        f"- {room_label(room)}: x de {coordinate(horizontal.start)} a "
        f"{coordinate(horizontal.end)}, y de {coordinate(vertical.start)} a "
        f"{coordinate(vertical.end)}."
    )


def door_line(door: Door, index: dict[str, Room]) -> str | None:
    """O vão de uma porta e o cômodo para onde cada face abre.

    A porta fica na parede `wall` do cômodo dono. A face voltada para dentro
    do dono aponta para a direção oposta à parede; a outra face aponta para
    a própria parede e abre para o vizinho, ou para o exterior. Porta de
    cômodo que não existe fica de fora: numa planta aprovada, não acontece.
    """
    owner = index.get(door.room_id)
    if owner is None:
        return None

    start, end = opening_endpoints(owner, door.wall, door.offset, door.width)

    if door.wall in ("north", "south"):
        opening = (
            f"vão em y = {coordinate(start[1])}, "
            f"de x = {coordinate(start[0])} a x = {coordinate(end[0])}"
        )
    else:
        opening = (
            f"vão em x = {coordinate(start[0])}, "
            f"de y = {coordinate(start[1])} a y = {coordinate(end[1])}"
        )

    neighbour = index.get(door.to)
    other_side = room_label(neighbour) if neighbour is not None else "o exterior"
    inward = WALL_NAMES[OPPOSITE[door.wall]]
    outward = WALL_NAMES[door.wall]

    return (
        f"- Porta na parede {WALL_NAMES[door.wall]} do cômodo {room_label(owner)}: "
        f"{opening}. A face voltada para o {inward} abre para {room_label(owner)}; "
        f"a face voltada para o {outward} abre para {other_side}."
    )


def placement_line(
    placement: Placement, catalog: Catalog, index: dict[str, Room]
) -> str:
    """O retângulo que um móvel ocupa, a direção da frente e as medidas.

    Móvel de item ou cômodo que não existe continua na lista, com o que dá
    para dizer dele: a verificação de integridade vai apontá-lo no parecer.
    """
    room = index.get(placement.room_id)
    where = room_label(room) if room is not None else placement.room_id
    item = catalog.get(placement.item_id)

    if item is None:
        return (
            f"- {placement.id}, item {placement.item_id}, que não existe no "
            f"catálogo, no cômodo {where}: sem retângulo calculado."
        )

    box = footprint(placement, catalog)
    front = WALL_NAMES[side_direction("front", placement.rotation)]

    return (
        f"- {placement.id}, {item.name} ({placement.item_id}), no cômodo {where}, "
        f"rotação {placement.rotation}: ocupa x de {coordinate(box.x0)} a "
        f"{coordinate(box.x1)}, y de {coordinate(box.y0)} a {coordinate(box.y1)}; "
        f"frente para o {front}. Catálogo: width {coordinate(item.width)}, "
        f"depth {coordinate(item.depth)}."
    )


def plan_geometry(plan: FloorPlan) -> str:
    """Os limites dos cômodos e o vão das portas, com o título do bloco."""
    index = rooms_by_id(plan)
    lines = [TITLE, "Cômodos:"]
    lines.extend(room_line(room) for room in plan.rooms)

    lines.append("Portas:")
    for door in plan.doors:
        line = door_line(door, index)
        if line is not None:
            lines.append(line)

    return "\n".join(lines)


def proposal_geometry(
    plan: FloorPlan, catalog: Catalog, proposal: FurnishingProposal
) -> str:
    """O retângulo, a frente e as medidas de cada móvel da proposta."""
    index = rooms_by_id(plan)
    lines = [PROPOSAL_TITLE]

    if proposal.placements:
        lines.extend(
            placement_line(placement, catalog, index)
            for placement in proposal.placements
        )
    else:
        lines.append("- nenhum.")

    return "\n".join(lines)


def geometry_block(
    plan: FloorPlan, catalog: Catalog, proposal: FurnishingProposal
) -> str:
    """O bloco inteiro, cômodos, portas e móveis, como o fiscal o recebe.

    O mobiliador recebe as duas partes separadas: `plan_geometry` logo depois
    da planta, e `proposal_geometry` logo depois da proposta anterior, a
    partir da segunda rodada. Nenhuma das partes tem linha em branco, para
    que cada uma fique num pedaço só da mensagem.
    """
    return plan_geometry(plan) + "\n" + proposal_geometry(plan, catalog, proposal)
