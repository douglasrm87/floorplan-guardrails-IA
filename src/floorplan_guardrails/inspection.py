"""As verificações do P2: a autoridade sobre o que é uma disposição aceitável.

Recebe a planta mobiliada, o catálogo, os parâmetros de circulação, o
perfil do morador e o pedido; devolve a lista de violações. Lista vazia
significa aprovada. É o validador do P1 aplicado aos móveis, com as mesmas
duas decisões:

**Roda tudo, sempre.** Nenhuma regra interrompe as outras: um móvel fora do
cômodo continua sendo medido contra os vizinhos, porque o mobiliador precisa
do relatório inteiro numa passada só.

**A mensagem é o produto.** Português simples, autossuficiente, com o nome e
o id de cada móvel e os números medidos ao lado dos exigidos.

Dois grupos
-----------
O grupo **integridade** tem parâmetros fixos no código: a proposta fecha como
desenho? O grupo **circulação** lê os números de `config/furniture_rules.yaml`:
dá para passar diante de cada porta, usar cada móvel e, quando o morador usa
cadeira de rodas, girar dentro do cômodo?

Toda a circulação se reduz a uma conta, `free_depth`: quanto espaço livre
existe à frente de um segmento até o primeiro obstáculo. As funções que medem
as faixas (`door_faces`, `use_zones`, `largest_free_square`) são as mesmas que
o desenho usa, para que a figura mostre exatamente o que foi medido.

A pré-condição
--------------
O P2 só trabalha sobre planta aprovada pelo validador do P1. Planta
reprovada na entrada é erro de uso, não violação: `require_approved_plan`
levanta `InvalidInputPlan` e nada mais roda.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from floorplan_guardrails.furniture import (
    Catalog,
    Direction,
    Footprint,
    FurnishedPlan,
    FurnishingRequest,
    Placement,
    Profile,
    Segment,
    Side,
    footprint,
    side_segment,
)
from floorplan_guardrails.furniture_rules import FurnitureRules
from floorplan_guardrails.geometry import (
    OPPOSITE,
    TOLERANCE,
    Interval,
    intersect,
    interval,
    span_x,
    span_y,
)
from floorplan_guardrails.renderer import opening_endpoints
from floorplan_guardrails.rules import Ruleset, load_rules
from floorplan_guardrails.schema import FloorPlan, Room, Wall
from floorplan_guardrails.validator import (
    ROOM_TYPE_NAMES,
    WALL_NAMES,
    Violation,
    number,
    room_noun,
    rooms_by_id,
    validate,
)


class FurnitureViolation(Violation):
    """Uma regra do P2 que a proposta não cumpre.

    É uma `Violation` do P1 com dois campos a mais, e por isso o desenho do
    P1 já sabe marcar os cômodos que ela cita.
    """

    placement_ids: list[str]
    source: str | None


class InvalidInputPlan(Exception):
    """A planta de entrada não foi aprovada pelo validador do P1."""


# --- pré-condição ---------------------------------------------------------------


def require_approved_plan(plan: FloorPlan, rules: Ruleset | None = None) -> None:
    """Confere que a planta passa no validador do P1 sem nenhuma violação.

    Sem `rules`, usa `config/rules.yaml`, que é o que o notebook e a
    negociação usam. Levanta `InvalidInputPlan` com o relatório do P1 se a
    planta tiver qualquer violação.
    """
    if rules is None:
        rules = load_rules()

    violations = validate(plan, rules)

    if not violations:
        return

    count = len(violations)
    counted = "1 violação" if count == 1 else f"{count} violações"
    report = "\n".join(f"  - {violation.message}" for violation in violations)
    raise InvalidInputPlan(
        "O mobiliador só trabalha sobre planta aprovada no P1, e esta tem "
        f"{counted}:\n\n{report}\n\n"
        "Corrija a planta, ou escolha outra, antes de mobiliar."
    )


# --- profundidade livre ---------------------------------------------------------


def _along_and_away(box: Footprint, normal: Direction) -> tuple[Interval, Interval]:
    """Um retângulo visto a partir de uma direção.

    Devolve dois intervalos: a extensão do retângulo **ao longo** da base e a
    extensão **na direção** da normal. A segunda é escrita de modo que andar
    na direção da normal sempre aumenta o número, o que deixa uma única conta
    servir para as quatro direções.
    """
    if normal == "north":
        return box.span_x, Interval(box.y0, box.y1)
    if normal == "south":
        return box.span_x, Interval(-box.y1, -box.y0)
    if normal == "east":
        return box.span_y, Interval(box.x0, box.x1)
    return box.span_y, Interval(-box.x1, -box.x0)


def _away(line: float, normal: Direction) -> float:
    """A coordenada da base na mesma escala de `_along_and_away`."""
    return line if normal in ("north", "east") else -line


def room_box(room: Room) -> Footprint:
    """O retângulo do cômodo, no mesmo formato da pegada de um móvel."""
    horizontal = span_x(room)
    vertical = span_y(room)
    return Footprint(horizontal.start, vertical.start, horizontal.end, vertical.end)


def free_depth(
    base: Segment,
    normal: Direction,
    room: Room,
    obstacles: Sequence[Footprint],
) -> float:
    """Quanto espaço livre existe à frente de um segmento, até o primeiro obstáculo.

    Parte da `base` e anda na direção `normal`. Conta como obstáculo todo
    retângulo que esteja à frente da base e cuja projeção sobre ela se
    sobreponha à base por mais que a tolerância: um móvel ao lado, que só
    encosta na ponta da faixa, não a bloqueia. A parede do cômodo é o limite
    final.

    Devolve 0 se já houver obstáculo encostado na base, ou se a base estiver
    fora do cômodo.
    """
    start = _away(base.line, normal)

    _, room_away = _along_and_away(room_box(room), normal)
    depth = room_away.end - start

    for obstacle in obstacles:
        along, away = _along_and_away(obstacle, normal)

        if intersect(along, base.span) is None:
            continue
        if away.end <= start + TOLERANCE:
            # O obstáculo está atrás da base, como o próprio corpo do móvel.
            continue

        depth = min(depth, away.start - start)

    return max(depth, 0.0)


# --- integridade ----------------------------------------------------------------


def furniture_noun(placement: Placement, catalog: Catalog) -> str:
    """Como um móvel aparece numa frase, sem artigo.

    Leva o nome do catálogo, que é o que o aluno reconhece, e o id, que é
    como o modelo encontra o móvel para corrigir. Se o item não existe no
    catálogo, resta o id.
    """
    item = catalog.get(placement.item_id)

    if item is None:
        return f"móvel {placement.id}"

    return f'móvel "{item.name}" ({placement.id})'


def item_name(item_id: str, catalog: Catalog) -> str:
    """O nome de um item para a frase, ou o próprio id se ele não existe."""
    item = catalog.get(item_id)
    return f'"{item.name}"' if item is not None else f"'{item_id}'"


def violation(
    rule_id: str,
    message: str,
    placement_ids: list[str],
    room_ids: list[str],
    measured: float | None = None,
    required: float | None = None,
    unit: str = "",
) -> FurnitureViolation:
    """Uma violação do grupo integridade, que não tem fonte externa."""
    return FurnitureViolation(
        rule_id=rule_id,
        room_ids=room_ids,
        placement_ids=placement_ids,
        measured=measured,
        required=required,
        unit=unit,
        message=message,
        source=None,
    )


def check_unique_placement_ids(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    placements = furnished.proposal.placements
    counts = Counter(placement.id for placement in placements)
    violations = []

    for placement_id, count in counts.items():
        if count < 2:
            continue

        room_ids = sorted(
            {
                placement.room_id
                for placement in placements
                if placement.id == placement_id
            }
        )
        violations.append(
            violation(
                "unique_placement_ids",
                f"O id '{placement_id}' aparece em {count} móveis. Cada móvel "
                "posicionado precisa de um id só dele.",
                placement_ids=[placement_id],
                room_ids=room_ids,
                measured=float(count),
                required=1.0,
                unit="móveis",
            )
        )

    return violations


def check_known_room(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    index = rooms_by_id(furnished.plan)

    return [
        violation(
            "known_room",
            f"O {furniture_noun(placement, catalog)} está no cômodo "
            f"'{placement.room_id}', que não existe na planta.",
            placement_ids=[placement.id],
            room_ids=[placement.room_id],
        )
        for placement in furnished.proposal.placements
        if placement.room_id not in index
    ]


def check_known_item(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    return [
        violation(
            "known_item",
            f"O móvel {placement.id} usa o item '{placement.item_id}', que não "
            "existe no catálogo.",
            placement_ids=[placement.id],
            room_ids=[placement.room_id],
        )
        for placement in furnished.proposal.placements
        if placement.item_id not in catalog
    ]


def check_item_allowed_in_room(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    index = rooms_by_id(furnished.plan)
    violations = []

    for placement in furnished.proposal.placements:
        room = index.get(placement.room_id)
        item = catalog.get(placement.item_id)

        if room is None or item is None or room.type in item.room_types:
            continue

        violations.append(
            violation(
                "item_allowed_in_room",
                f"O {furniture_noun(placement, catalog)} não pode ficar no "
                f"{room_noun(room)}. Esse item só vai em "
                + ", ".join(ROOM_TYPE_NAMES[kind] for kind in item.room_types)
                + ".",
                placement_ids=[placement.id],
                room_ids=[room.id],
            )
        )

    return violations


def overhang(box: Footprint, room: Room) -> float:
    """O quanto a pegada passa para fora do cômodo, no pior lado, em metros."""
    limits = room_box(room)

    return max(
        limits.x0 - box.x0,
        box.x1 - limits.x1,
        limits.y0 - box.y0,
        box.y1 - limits.y1,
        0.0,
    )


def check_inside_room(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    index = rooms_by_id(furnished.plan)
    violations = []

    for placement in furnished.proposal.placements:
        room = index.get(placement.room_id)

        if room is None or placement.item_id not in catalog:
            continue

        outside = overhang(footprint(placement, catalog), room)

        if outside <= TOLERANCE:
            continue

        violations.append(
            violation(
                "inside_room",
                f"O {furniture_noun(placement, catalog)} passa {number(outside)} m "
                f"para fora do {room_noun(room)}. Todo móvel precisa ficar "
                "inteiro dentro do seu cômodo.",
                placement_ids=[placement.id],
                room_ids=[room.id],
                measured=outside,
                required=0.0,
                unit="m",
            )
        )

    return violations


def footprint_overlap(first: Footprint, second: Footprint) -> float:
    """Área em que duas pegadas se sobrepõem, em metros quadrados.

    Móveis que apenas se encostam não se sobrepõem: devolve 0,0.
    """
    horizontal = intersect(first.span_x, second.span_x)
    vertical = intersect(first.span_y, second.span_y)

    if horizontal is None or vertical is None:
        return 0.0

    return horizontal.length * vertical.length


def check_no_furniture_overlap(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    placed = [
        placement
        for placement in furnished.proposal.placements
        if placement.item_id in catalog
    ]
    violations = []

    for i, first in enumerate(placed):
        for second in placed[i + 1 :]:
            shared = footprint_overlap(
                footprint(first, catalog), footprint(second, catalog)
            )

            if shared <= 0:
                continue

            violations.append(
                violation(
                    "no_furniture_overlap",
                    f"O {furniture_noun(first, catalog)} e o "
                    f"{furniture_noun(second, catalog)} ocupam {number(shared)} m² "
                    "do mesmo lugar. Móveis podem se encostar, mas não se "
                    "sobrepor.",
                    placement_ids=[first.id, second.id],
                    room_ids=sorted({first.room_id, second.room_id}),
                    measured=shared,
                    required=0.0,
                    unit="m²",
                )
            )

    return violations


def check_request_matches(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    """Por cômodo, posicionados e omitidos formam exatamente o que foi pedido."""
    proposal = furnished.proposal
    index = rooms_by_id(furnished.plan)

    asked = Counter(
        (room_id, item_id)
        for room_id, item_ids in request.items.items()
        for item_id in item_ids
    )
    delivered = Counter(
        (placement.room_id, placement.item_id) for placement in proposal.placements
    )
    delivered.update(
        (omission.room_id, omission.item_id) for omission in proposal.omissions
    )

    violations = []

    for room_id, item_id in sorted(asked.keys() | delivered.keys()):
        wanted = asked[(room_id, item_id)]
        got = delivered[(room_id, item_id)]

        if wanted == got:
            continue

        room = index.get(room_id)
        where = room_noun(room if room is not None else room_id)
        name = item_name(item_id, catalog)
        placement_ids = [
            placement.id
            for placement in proposal.placements
            if placement.room_id == room_id and placement.item_id == item_id
        ]

        if got < wanted:
            message = (
                f"O pedido tem {wanted} {name} no {where}, e a proposta posiciona "
                f"ou declara omissão de só {got}. Todo item pedido precisa ser "
                "posicionado ou declarado como omissão."
            )
        else:
            message = (
                f"A proposta põe {got} {name} no {where}, e o pedido tem {wanted}. "
                "Não posicione nem declare item que não foi pedido."
            )

        violations.append(
            violation(
                "request_matches",
                message,
                placement_ids=placement_ids,
                room_ids=[room_id],
                measured=float(got),
                required=float(wanted),
                unit="itens",
            )
        )

    return violations


# --- circulação: as medidas ---------------------------------------------------
#
# As funções desta seção só medem: dizem onde fica cada faixa, quanto ela
# precisa e quanto tem. Quem transforma a medida em violação são as regras
# da seção seguinte, e quem a transforma em desenho é `furnished_renderer`.

#: Passo da varredura que procura o quadrado de giro, em metros.
GRID_STEP = 0.05

#: Margem para o erro de ponto flutuante, em metros.
#:
#: Não é a `TOLERANCE`. A tolerância de 1 cm diz quando duas medidas são a
#: mesma medida, e as regras a usam para aprovar ou reprovar. Esta margem só
#: diz quando uma conta deu zero: 0.1 + 0.2 vale 0.30000000000000004, e um
#: quadrado que encosta num móvel pode sair "entrando" 2e-16 m nele. Usar a
#: tolerância no lugar deixaria o quadrado de giro entrar 1 cm no móvel, e o
#: lado medido sairia 1 cm maior do que o espaço de verdade.
FLOAT_MARGIN = 1e-9

#: Como cada lado do móvel aparece numa frase.
SIDE_PHRASES: dict[Side, str] = {
    "front": "na frente",
    "back": "atrás",
    "left": "à esquerda",
    "right": "à direita",
}


def clearance_box(base: Segment, normal: Direction, depth: float) -> Footprint:
    """O retângulo de uma faixa: a base, estendida `depth` metros para a normal."""
    span = base.span

    if normal == "north":
        return Footprint(span.start, base.line, span.end, base.line + depth)
    if normal == "south":
        return Footprint(span.start, base.line - depth, span.end, base.line)
    if normal == "east":
        return Footprint(base.line, span.start, base.line + depth, span.end)
    return Footprint(base.line - depth, span.start, base.line, span.end)


def room_furniture(
    furnished: FurnishedPlan, catalog: Catalog, room_id: str
) -> list[tuple[Placement, Footprint]]:
    """Os móveis de um cômodo, cada um com a sua pegada.

    Móvel com item fora do catálogo não tem pegada e fica de fora: esse
    problema já é uma violação de integridade.
    """
    return [
        (placement, footprint(placement, catalog))
        for placement in furnished.proposal.placements
        if placement.room_id == room_id and placement.item_id in catalog
    ]


def measure(
    base: Segment,
    normal: Direction,
    room: Room,
    candidates: Sequence[tuple[Placement, Footprint]],
) -> tuple[float, tuple[Placement, ...]]:
    """A profundidade livre à frente da base, e quem a limita.

    Quem limita é o obstáculo que, sozinho, deixa a mesma profundidade que
    todos juntos. Se a profundidade é a da parede, ninguém limita, e a tupla
    sai vazia.
    """
    depth = free_depth(base, normal, room, [box for _, box in candidates])
    wall = free_depth(base, normal, room, [])

    if depth >= wall - TOLERANCE:
        return depth, ()

    limiting = tuple(
        placement
        for placement, box in candidates
        if abs(free_depth(base, normal, room, [box]) - depth) <= TOLERANCE
    )
    return depth, limiting


@dataclass(frozen=True)
class Clearance:
    """Uma faixa medida: onde começa, para onde vai, quanto precisa e quanto tem."""

    room: Room
    base: Segment
    normal: Direction
    required: float
    measured: float
    blockers: tuple[Placement, ...]

    @property
    def ok(self) -> bool:
        return self.measured >= self.required - TOLERANCE

    @property
    def blocker_ids(self) -> list[str]:
        return [placement.id for placement in self.blockers]


@dataclass(frozen=True)
class DoorFace(Clearance):
    """O vão de uma porta visto de dentro de um dos cômodos que ela liga.

    `wall` é a parede em que o vão fica, do ponto de vista desse cômodo: a
    porta na parede norte da sala é a porta na parede sul do quarto de cima.
    """

    wall: Wall


@dataclass(frozen=True)
class SideZone(Clearance):
    """A faixa de uso de um lado de um móvel."""

    placement: Placement
    side: Side


def door_faces(
    furnished: FurnishedPlan, catalog: Catalog, rules: FurnitureRules
) -> list[DoorFace]:
    """As duas faces de cada porta interna, e a face de dentro da porta de entrada.

    A base é o vão da porta. A faixa entra no cômodo, na direção oposta à
    parede, e os obstáculos são todos os móveis daquele cômodo.
    """
    index = rooms_by_id(furnished.plan)
    required = rules.door_clearance_depth.value
    faces = []

    for door in furnished.plan.doors:
        owner = index.get(door.room_id)
        if owner is None:
            continue

        start, end = opening_endpoints(owner, door.wall, door.offset, door.width)
        if door.wall in ("north", "south"):
            base = Segment(start[1], interval(start[0], end[0]))
        else:
            base = Segment(start[0], interval(start[1], end[1]))

        sides = [(owner, door.wall)]
        neighbour = index.get(door.to)
        if neighbour is not None:
            sides.append((neighbour, OPPOSITE[door.wall]))

        for room, wall in sides:
            normal = OPPOSITE[wall]
            measured, blockers = measure(
                base, normal, room, room_furniture(furnished, catalog, room.id)
            )
            faces.append(
                DoorFace(
                    room=room,
                    base=base,
                    normal=normal,
                    required=required,
                    measured=measured,
                    blockers=blockers,
                    wall=wall,
                )
            )

    return faces


def use_zones(
    furnished: FurnishedPlan, catalog: Catalog, rules: FurnitureRules
) -> list[SideZone]:
    """A faixa de cada lado de uso de cada móvel.

    Os obstáculos são os outros móveis cujo corpo bloqueia faixas: a mesa de
    cabeceira encostada na cama não tira o lado da cama de uso.
    """
    index = rooms_by_id(furnished.plan)
    placements = furnished.proposal.placements
    blocking = [
        (placement, footprint(placement, catalog))
        for placement in placements
        if placement.item_id in catalog and catalog[placement.item_id].blocks_use_zones
    ]
    zones = []

    for placement in placements:
        room = index.get(placement.room_id)
        item = catalog.get(placement.item_id)

        if room is None or item is None or not item.use_sides:
            continue

        others = [(other, box) for other, box in blocking if other is not placement]
        required = rules.use_zone_depth.values[placement.item_id]

        for side in item.use_sides:
            base, normal = side_segment(placement, catalog, side)
            measured, blockers = measure(base, normal, room, others)
            zones.append(
                SideZone(
                    room=room,
                    base=base,
                    normal=normal,
                    required=required,
                    measured=measured,
                    blockers=blockers,
                    placement=placement,
                    side=side,
                )
            )

    return zones


def item_fails(zones: Sequence[SideZone], mode: str) -> bool:
    """Se um móvel reprova, a partir das faixas dos seus lados.

    Em `all`, basta um lado sem espaço; em `any`, todos precisam estar sem.
    """
    if not zones:
        return False
    if mode == "any":
        return all(not zone.ok for zone in zones)
    return any(not zone.ok for zone in zones)


def zones_by_placement(
    zones: Sequence[SideZone],
) -> list[tuple[Placement, list[SideZone]]]:
    """As faixas agrupadas por móvel, na ordem em que os móveis aparecem."""
    groups: list[tuple[Placement, list[SideZone]]] = []

    for zone in zones:
        if groups and groups[-1][0] is zone.placement:
            groups[-1][1].append(zone)
        else:
            groups.append((zone.placement, [zone]))

    return groups


def _positions(start: float, end: float, side: float) -> list[float]:
    """Onde um quadrado de lado `side` pode começar, num eixo do cômodo.

    A grade de 5 cm a partir do canto do cômodo, mais a posição rente à
    parede do fim, que a grade pode não alcançar.
    """
    last = end - side
    positions = []
    step = 0

    while start + step * GRID_STEP <= last + TOLERANCE / 10:
        positions.append(round(start + step * GRID_STEP, 6))
        step += 1

    if last >= start - TOLERANCE / 10:
        positions.append(last)

    return positions


def touches_inside(first: Footprint, second: Footprint) -> bool:
    """Se dois retângulos têm área em comum, e não só uma borda.

    Compara com `FLOAT_MARGIN`, e não com `TOLERANCE`: ver a constante.
    """
    horizontal = min(first.x1, second.x1) - max(first.x0, second.x0)
    vertical = min(first.y1, second.y1) - max(first.y0, second.y0)
    return horizontal > FLOAT_MARGIN and vertical > FLOAT_MARGIN


def _free_square(
    room: Room, obstacles: Sequence[Footprint], side: float
) -> Footprint | None:
    """O primeiro quadrado de lado `side` que cabe livre no cômodo, se houver."""
    limits = room_box(room)

    for x in _positions(limits.x0, limits.x1, side):
        for y in _positions(limits.y0, limits.y1, side):
            square = Footprint(x, y, x + side, y + side)
            if not any(touches_inside(square, box) for box in obstacles):
                return square

    return None


def largest_free_square(room: Room, obstacles: Sequence[Footprint]) -> Footprint:
    """O maior quadrado livre de móveis dentro do cômodo.

    Busca binária no lado, em centímetros: se um quadrado cabe, qualquer
    menor também cabe. Para cada lado, varre as posições em grade de 5 cm. É
    uma aproximação conservadora: um quadrado que só caberia fora da grade
    não é encontrado.

    Se nem 1 cm cabe, devolve um quadrado de lado zero no canto do cômodo.
    """
    limits = room_box(room)
    low = 0
    high = round(min(limits.x1 - limits.x0, limits.y1 - limits.y0) * 100)
    best = Footprint(limits.x0, limits.y0, limits.x0, limits.y0)

    while low < high:
        middle = (low + high + 1) // 2
        square = _free_square(room, obstacles, middle / 100)

        if square is None:
            high = middle - 1
        else:
            low = middle
            best = square

    return best


def turning_rooms(
    plan: FloorPlan, rules: FurnitureRules, profile: Profile
) -> list[Room]:
    """Os cômodos em que o giro de cadeira de rodas é verificado.

    Nenhum, se o morador não usa cadeira. Se usa, os cômodos dos tipos
    listados no parâmetro.
    """
    if not profile.accessible:
        return []

    kinds = rules.turning_diameter.room_types
    return [room for room in rooms_by_id(plan).values() if room.type in kinds]


# --- circulação: as regras -----------------------------------------------------


def furniture_list(placements: Sequence[Placement], catalog: Catalog) -> str:
    """Vários móveis numa frase: "o móvel A, o móvel B e o móvel C"."""
    nouns = [f"o {furniture_noun(placement, catalog)}" for placement in placements]

    if len(nouns) == 1:
        return nouns[0]

    return ", ".join(nouns[:-1]) + " e " + nouns[-1]


def until(clearance: Clearance, catalog: Catalog) -> str:
    """Até onde a faixa vai: até um móvel, até vários ou até a parede."""
    if not clearance.blockers:
        return "até a parede"

    return "até " + furniture_list(clearance.blockers, catalog)


def circulation_violation(
    rule_id: str,
    message: str,
    placement_ids: list[str],
    room_ids: list[str],
    measured: float,
    required: float,
    source: str,
) -> FurnitureViolation:
    """Uma violação do grupo circulação, com a fonte do parâmetro exigido."""
    return FurnitureViolation(
        rule_id=rule_id,
        room_ids=room_ids,
        placement_ids=placement_ids,
        measured=measured,
        required=required,
        unit="m",
        message=message,
        source=source,
    )


def check_door_clearance(
    furnished: FurnishedPlan,
    catalog: Catalog,
    rules: FurnitureRules,
    profile: Profile,
) -> list[FurnitureViolation]:
    violations = []

    for face in door_faces(furnished, catalog, rules):
        if face.ok:
            continue

        door = f"da porta na parede {WALL_NAMES[face.wall]} do {room_noun(face.room)}"
        measured = number(face.measured)
        required = number(face.required)

        if not face.blockers:
            message = (
                f"Diante {door}, a parede oposta fica a só {measured} m; são "
                f"exigidos {required} m."
            )
        else:
            who = furniture_list(face.blockers, catalog)
            verb = "deixa" if len(face.blockers) == 1 else "deixam"
            message = (
                f"{who[0].upper()}{who[1:]} {verb} só {measured} m livres diante "
                f"{door}; são exigidos {required} m."
            )

        violations.append(
            circulation_violation(
                "door_clearance",
                message,
                placement_ids=face.blocker_ids,
                room_ids=[face.room.id],
                measured=face.measured,
                required=face.required,
                source=rules.door_clearance_depth.source,
            )
        )

    return violations


def describe_side(zone: SideZone, catalog: Catalog) -> str:
    """Um lado medido: "0,20 m livres na frente (lado oeste), até a parede"."""
    return (
        f"{number(zone.measured)} m livres {SIDE_PHRASES[zone.side]} "
        f"(lado {WALL_NAMES[zone.normal]}), {until(zone, catalog)}"
    )


def describe_sides(zones: Sequence[SideZone], catalog: Catalog) -> str:
    """Vários lados medidos numa frase, separados por ", e "."""
    return ", e ".join(describe_side(zone, catalog) for zone in zones)


def check_use_zone(
    furnished: FurnishedPlan,
    catalog: Catalog,
    rules: FurnitureRules,
    profile: Profile,
) -> list[FurnitureViolation]:
    violations = []

    for placement, zones in zones_by_placement(use_zones(furnished, catalog, rules)):
        mode = catalog[placement.item_id].use_mode

        if not item_fails(zones, mode):
            continue

        noun = furniture_noun(placement, catalog)
        required = number(zones[0].required)

        if mode == "any":
            measured = max(zone.measured for zone in zones)
            sides = describe_sides(zones, catalog)
            message = (
                f"O {noun} precisa de ao menos um lado livre, e nenhum está: tem "
                f"{sides}; são exigidos {required} m em ao menos um deles."
            )
        else:
            measured = min(zone.measured for zone in zones)
            short = [zone for zone in zones if not zone.ok]
            sides = describe_sides(short, catalog)
            each = " de cada lado" if len(short) > 1 else ""
            message = f"O {noun} tem {sides}; são exigidos {required} m{each}."

        # Só quem prende um lado sem espaço é citado.
        placement_ids = [placement.id]
        for zone in zones:
            if zone.ok:
                continue
            for blocker_id in zone.blocker_ids:
                if blocker_id not in placement_ids:
                    placement_ids.append(blocker_id)

        violations.append(
            circulation_violation(
                "use_zone",
                message,
                placement_ids=placement_ids,
                room_ids=[zones[0].room.id],
                measured=measured,
                required=zones[0].required,
                source=rules.use_zone_depth.source,
            )
        )

    return violations


def check_turning_space(
    furnished: FurnishedPlan,
    catalog: Catalog,
    rules: FurnitureRules,
    profile: Profile,
) -> list[FurnitureViolation]:
    parameter = rules.turning_diameter
    violations = []

    for room in turning_rooms(furnished.plan, rules, profile):
        furniture = room_furniture(furnished, catalog, room.id)
        square = largest_free_square(room, [box for _, box in furniture])
        side = round(square.x1 - square.x0, 2)

        if side >= parameter.value - TOLERANCE:
            continue

        room_name = room_noun(room)
        violations.append(
            circulation_violation(
                "turning_space",
                f"O {room_name} não tem espaço de giro para cadeira de rodas: o "
                f"maior quadrado livre tem {number(side)} m de lado, e são "
                f"exigidos {number(parameter.value)} m.",
                # O problema é a disposição do cômodo inteiro, não de um móvel.
                placement_ids=[],
                room_ids=[room.id],
                measured=side,
                required=parameter.value,
                source=parameter.source,
            )
        )

    return violations


# --- entrada pública ------------------------------------------------------------

#: As regras de integridade: a proposta fecha como desenho?
INTEGRITY_RULES_P2 = frozenset(
    {
        "unique_placement_ids",
        "known_room",
        "known_item",
        "item_allowed_in_room",
        "inside_room",
        "no_furniture_overlap",
        "request_matches",
    }
)

INTEGRITY_CHECKS = (
    check_unique_placement_ids,
    check_known_room,
    check_known_item,
    check_item_allowed_in_room,
    check_inside_room,
    check_no_furniture_overlap,
    check_request_matches,
)


def precheck(
    furnished: FurnishedPlan, catalog: Catalog, request: FurnishingRequest
) -> list[FurnitureViolation]:
    """Confere a proposta contra todas as regras de integridade.

    Devolve a lista de violações, vazia se a proposta fecha como desenho.
    Não confere a planta de entrada: isso é `require_approved_plan`, que roda
    uma vez antes de tudo.
    """
    violations: list[FurnitureViolation] = []

    for check in INTEGRITY_CHECKS:
        violations.extend(check(furnished, catalog, request))

    return violations


#: As regras de circulação: dá para passar, usar e girar?
CIRCULATION_RULES = frozenset({"door_clearance", "use_zone", "turning_space"})

CIRCULATION_CHECKS = (
    check_door_clearance,
    check_use_zone,
    check_turning_space,
)


def inspect(
    furnished: FurnishedPlan,
    catalog: Catalog,
    rules: FurnitureRules,
    profile: Profile,
    request: FurnishingRequest,
) -> list[FurnitureViolation]:
    """Confere a proposta contra todas as regras, de integridade e de circulação.

    Roda tudo, sempre, como o `validate` do P1: um móvel fora do cômodo
    continua sendo medido contra as portas e os vizinhos. Devolve a lista de
    violações, vazia se a proposta está aprovada.
    """
    violations = precheck(furnished, catalog, request)

    for check in CIRCULATION_CHECKS:
        violations.extend(check(furnished, catalog, rules, profile))

    return violations
