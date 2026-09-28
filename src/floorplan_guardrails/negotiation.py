"""A negociação: o mobiliador propõe, o código confere, o parecer volta.

Fica em Python comum, fora do framework de agentes, como o laço do P1. É
controle determinístico, e quem lê o código vê onde termina a parte que
adivinha e começa a parte que verifica.

Uma rodada
----------
1. O mobiliador propõe a disposição inteira.
2. Se ele declarou alguma omissão, a negociação acaba em `declined`: a
   proposta e os motivos ficam no histórico, sem verificação nem parecer.
3. `inspect` confere a proposta. Sem violações, `approved`, e acabou.
4. Com violações, sai o parecer. Se todas forem de integridade (móvel fora
   do cômodo, sobreposição, pedido não atendido), o parecer é a lista de
   mensagens, sem chamar o fiscal: não há o que explicar além do que a
   mensagem já diz. Se houver alguma de circulação, o fiscal redige.
5. Sobrando rodadas, a proposta e o parecer voltam ao mobiliador; na última,
   o estado é `not_converged`.

Antes da primeira rodada
------------------------
Com o perfil acessível, há plantas que nenhuma disposição de móveis salva:
um banheiro de 1,20 m de largura não comporta o giro de cadeira de rodas nem
vazio. Nesse caso a negociação nem começa. O código confere cada cômodo em
que o giro é verificado e, se algum não comporta o giro vazio, termina em
`infeasible`, sem chamar modelo nenhum, com uma mensagem por cômodo. Quem
resolve isso é a etapa de geração da planta, não o mobiliador.

Quem decide
-----------
O estado de cada rodada é calculado aqui, a partir de `inspect`. O fiscal só
escreve, e o mobiliador não tem como contestar: ele reposiciona ou desiste.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from floorplan_guardrails import validator
from floorplan_guardrails.furnisher import Feedback, Furnisher
from floorplan_guardrails.furniture import (
    Catalog,
    FurnishedPlan,
    FurnishingProposal,
    FurnishingRequest,
    Profile,
)
from floorplan_guardrails.furniture_rules import FurnitureRules
from floorplan_guardrails.geometry import TOLERANCE
from floorplan_guardrails.inspection import (
    INTEGRITY_RULES_P2,
    FurnitureViolation,
    inspect,
    require_approved_plan,
    turning_rooms,
)
from floorplan_guardrails.inspector import (
    APPROVED_SUMMARY,
    Inspector,
    Review,
    messages_review,
)
from floorplan_guardrails.rules import Ruleset
from floorplan_guardrails.schema import FloorPlan
from floorplan_guardrails.validator import room_noun

# O registro importa `Round` e `Negotiation` deste módulo para reconstruir
# execuções gravadas. Importá-lo aqui de verdade seria circular; o nome só
# serve para a anotação de tipo de `negotiate`.
if TYPE_CHECKING:
    from floorplan_guardrails.negotiation_log import NegotiationLog

#: Quantas rodadas a negociação dá antes de desistir. Revisto na Fase 5,
#: com a latência medida.
DEFAULT_MAX_ROUNDS = 3

#: Aprovada nesta rodada.
APPROVED = "approved"

#: Reprovada, mas ainda há rodadas pela frente.
REJECTED = "rejected"

#: O mobiliador declarou que algum item não cabe.
DECLINED = "declined"

#: Reprovada e o limite de rodadas acabou.
NOT_CONVERGED = "not_converged"

#: A planta não comporta o giro nem vazia: a negociação nem começa.
INFEASIBLE = "infeasible"

#: O estado de uma rodada.
RoundStatus = Literal["approved", "rejected", "declined", "not_converged", "infeasible"]

#: O estado com que a negociação termina: `rejected` nunca é o último.
FinalStatus = Literal["approved", "declined", "not_converged", "infeasible"]


@dataclass(frozen=True)
class Round:
    """Uma rodada: a proposta, o que o código achou e o parecer.

    `review` fica vazio (`None`) numa desistência, em que nada é conferido,
    e numa planta inviável, em que o modelo não chega a ser chamado: a
    rodada guarda então uma proposta vazia e as violações do giro.
    """

    proposal: FurnishingProposal
    violations: list[FurnitureViolation]
    review: Review | None
    status: RoundStatus
    furnisher_input_tokens: int = 0
    furnisher_output_tokens: int = 0
    furnisher_latency_ms: int = 0

    @property
    def inspector_input_tokens(self) -> int:
        return self.review.input_tokens if self.review else 0

    @property
    def inspector_output_tokens(self) -> int:
        return self.review.output_tokens if self.review else 0

    @property
    def inspector_latency_ms(self) -> int:
        return self.review.latency_ms if self.review else 0


#: Uma rodada no formato que `draw_negotiation` desenha.
RoundView = tuple[FurnishedPlan, list[FurnitureViolation], RoundStatus]


@dataclass(frozen=True)
class Negotiation:
    """Uma negociação inteira, da planta de entrada ao desfecho."""

    run_id: str
    plan: FloorPlan
    request: FurnishingRequest
    profile: Profile
    deployment: str
    status: FinalStatus
    rounds: list[Round]

    @property
    def approved(self) -> bool:
        return self.status == APPROVED

    @property
    def proposal(self) -> FurnishingProposal:
        """A última proposta, aprovada ou não."""
        return self.rounds[-1].proposal

    @property
    def violations(self) -> list[FurnitureViolation]:
        """As violações que sobraram no fim."""
        return self.rounds[-1].violations

    @property
    def history(self) -> list[RoundView]:
        """O histórico no formato que o renderizador desenha lado a lado."""
        return [
            (
                FurnishedPlan(plan=self.plan, proposal=item.proposal),
                item.violations,
                item.status,
            )
            for item in self.rounds
        ]

    @property
    def input_tokens(self) -> int:
        """Tokens de entrada dos dois agentes, somados em todas as rodadas."""
        return sum(
            item.furnisher_input_tokens + item.inspector_input_tokens
            for item in self.rounds
        )

    @property
    def output_tokens(self) -> int:
        """Tokens de saída dos dois agentes, somados em todas as rodadas."""
        return sum(
            item.furnisher_output_tokens + item.inspector_output_tokens
            for item in self.rounds
        )

    @property
    def latency_ms(self) -> int:
        """Tempo de espera pelos dois agentes, somado em todas as rodadas."""
        return sum(
            item.furnisher_latency_ms + item.inspector_latency_ms
            for item in self.rounds
        )


#: A proposta de uma rodada em que o mobiliador não foi chamado.
EMPTY_PROPOSAL = FurnishingProposal(placements=[], omissions=[], design_notes="")


def infeasible_rooms(
    plan: FloorPlan, rules: FurnitureRules, profile: Profile
) -> list[FurnitureViolation]:
    """Os cômodos que não comportam o giro de cadeira de rodas nem vazios.

    Só vale com o perfil acessível e nos tipos de cômodo do parâmetro, como
    a verificação de giro. Vazio, o maior quadrado que cabe num retângulo tem
    o lado igual à menor dimensão dele; se isso fica abaixo do diâmetro de
    giro, nenhuma disposição de móveis resolve.
    """
    parameter = rules.turning_diameter
    violations = []

    for room in turning_rooms(plan, rules, profile):
        side = min(room.width, room.depth)

        if side >= parameter.value - TOLERANCE:
            continue

        measured = validator.number(side)
        required = validator.number(parameter.value)
        violations.append(
            FurnitureViolation(
                rule_id="turning_space",
                room_ids=[room.id],
                placement_ids=[],
                measured=side,
                required=parameter.value,
                unit="m",
                message=(
                    f"O {room_noun(room)} não tem espaço de giro para cadeira de "
                    "rodas nem vazio: o maior quadrado que cabe nele tem "
                    f"{measured} m de lado, e são exigidos {required} m. Nenhuma "
                    "disposição de móveis resolve isso: a planta precisa voltar "
                    "à etapa de geração."
                ),
                source=parameter.source,
            )
        )

    return violations


def only_integrity(violations: Sequence[FurnitureViolation]) -> bool:
    """Se todas as violações são do grupo integridade."""
    return all(violation.rule_id in INTEGRITY_RULES_P2 for violation in violations)


async def negotiate(
    plan: FloorPlan,
    request: FurnishingRequest,
    profile: Profile,
    furnisher: Furnisher,
    inspector: Inspector,
    catalog: Catalog,
    rules: FurnitureRules,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    log: "NegotiationLog | None" = None,
    deployment: str = "",
    plan_rules: Ruleset | None = None,
) -> Negotiation:
    """Negocia até a aprovação, a desistência ou o fim das rodadas.

    Antes da primeira rodada, confere uma vez que a planta foi aprovada no
    P1 (`plan_rules`; sem ele, `config/rules.yaml`). Planta reprovada levanta
    `InvalidInputPlan` e o mobiliador nem é chamado.

    Com o perfil acessível, confere também que cada cômodo comporta o giro
    vazio. Se algum não comporta, devolve a negociação em `infeasible`, com
    uma única rodada sem proposta, e nenhum modelo é chamado.

    Devolve a negociação inteira, com uma `Round` por rodada, para que o
    desenho mostre o caminho e não só o destino.
    """
    if max_rounds < 1:
        raise ValueError("a negociação precisa de ao menos uma rodada")

    require_approved_plan(plan, plan_rules)

    blocked = infeasible_rooms(plan, rules, profile)
    if blocked:
        current = Round(
            proposal=EMPTY_PROPOSAL,
            violations=blocked,
            review=None,
            status=INFEASIBLE,
        )
        if log is not None:
            log.record(
                number=1,
                result=current,
                plan=plan,
                request=request,
                profile=profile,
                deployment=deployment,
            )
        return Negotiation(
            run_id=log.run_id if log is not None else "",
            plan=plan,
            request=request,
            profile=profile,
            deployment=deployment,
            status=INFEASIBLE,
            rounds=[current],
        )

    rounds: list[Round] = []
    previous: FurnishingProposal | None = None
    feedback: Sequence[Feedback] = ()

    for number in range(1, max_rounds + 1):
        attempt = await furnisher.propose(
            plan, catalog, request, profile, previous, feedback
        )
        proposal = attempt.proposal

        if proposal.omissions:
            violations: list[FurnitureViolation] = []
            review = None
            status = DECLINED
        else:
            furnished = FurnishedPlan(plan=plan, proposal=proposal)
            violations = inspect(furnished, catalog, rules, profile, request)

            if not violations:
                review = Review(summary=APPROVED_SUMMARY)
                status = APPROVED
            else:
                if only_integrity(violations):
                    review = messages_review(violations)
                else:
                    review = await inspector.review(furnished, violations, profile)
                status = NOT_CONVERGED if number == max_rounds else REJECTED

        current = Round(
            proposal=proposal,
            violations=violations,
            review=review,
            status=status,
            furnisher_input_tokens=attempt.input_tokens,
            furnisher_output_tokens=attempt.output_tokens,
            furnisher_latency_ms=attempt.latency_ms,
        )
        rounds.append(current)

        if log is not None:
            log.record(
                number=number,
                result=current,
                plan=plan,
                request=request,
                profile=profile,
                deployment=deployment,
            )

        if status != REJECTED:
            break

        previous = proposal
        feedback = review.feedback

    return Negotiation(
        run_id=log.run_id if log is not None else "",
        plan=plan,
        request=request,
        profile=profile,
        deployment=deployment,
        status=rounds[-1].status,
        rounds=rounds,
    )
