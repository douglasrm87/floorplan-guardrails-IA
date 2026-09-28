"""O fiscal: quem redige o parecer sobre as verificações que falharam.

O parecer explica; não decide. Quando o fiscal é chamado, o veredito já foi
dado por `inspect`, e nada do que ele escrever muda o estado da rodada. O
fiscal não consegue aprovar nem reprovar.

O contrato
----------
A negociação conversa com o fiscal por um `Protocol`, `Inspector`, pelo
mesmo motivo do `Furnisher`: o laço precisa rodar em teste sem chave e sem
rede. Qualquer objeto com o método `review` serve.

A versão determinística
-----------------------
`MessageInspector` não chama modelo nenhum: devolve, para cada violação, a
própria `message` e uma sugestão vazia. É o parecer mínimo que o mobiliador
pode receber, porque a `message` já é autossuficiente e traz os números
medidos ao lado dos exigidos. A mesma função, `messages_review`, é usada pela
negociação quando só há violações de integridade, e será o que sobra de um
achado do fiscal com modelo que se perder no caminho.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from floorplan_guardrails.furnisher import Feedback
from floorplan_guardrails.furniture import FurnishedPlan, Profile
from floorplan_guardrails.inspection import FurnitureViolation

#: O parecer de uma proposta aprovada, escrito pelo código, sem modelo.
APPROVED_SUMMARY = "Aprovado: nenhuma verificação falhou."


@dataclass(frozen=True)
class Review:
    """O parecer de uma rodada, do jeito que a negociação o guarda.

    `feedback` tem um item por violação, na ordem de `inspect`, e é o que
    volta ao mobiliador. Tokens, latência e chamadas de ferramenta ficam em
    zero quando o parecer foi escrito pelo código.
    """

    feedback: list[Feedback] = field(default_factory=list)
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tool_calls: int = 0


class Inspector(Protocol):
    """O contrato que a negociação usa para pedir o parecer."""

    async def review(
        self,
        furnished: FurnishedPlan,
        violations: Sequence[FurnitureViolation],
        profile: Profile,
    ) -> Review:
        """Redige o parecer sobre as violações que `inspect` encontrou."""
        ...


def messages_review(violations: Sequence[FurnitureViolation]) -> Review:
    """O parecer feito só com as mensagens das violações, sem sugestão."""
    return Review(
        feedback=[
            Feedback(message=violation.message, suggestion="")
            for violation in violations
        ],
    )


class MessageInspector:
    """Fiscal sem modelo: o parecer é a lista de mensagens das violações."""

    async def review(
        self,
        furnished: FurnishedPlan,
        violations: Sequence[FurnitureViolation],
        profile: Profile,
    ) -> Review:
        return messages_review(violations)
