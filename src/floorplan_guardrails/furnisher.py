"""O mobiliador: o agente que posiciona os móveis na planta aprovada.

Um agente do Microsoft Agent Framework, no mesmo deployment e pela mesma rota
do gerador do P1, responde no formato `FurnishingProposal`, em saída
estruturada estrita. Devolve id de catálogo, posição e rotação; as dimensões
vêm do catálogo, e quem mede e decide é o código.

O que as instruções NÃO contêm
------------------------------
Nenhum valor de `config/furniture_rules.yaml`: nem a profundidade livre
diante das portas, nem a faixa de uso de cada móvel, nem o diâmetro de giro.
O mobiliador aprende o que a circulação exige só pelo parecer. O teste
`test_instructions_carry_no_circulation_value` lê o arquivo real e falha se
qualquer número de lá aparecer nas instruções.

A mensagem de cada chamada
--------------------------
Montada por `build_message`, uma função pura: a planta, o catálogo filtrado
aos itens pedidos, o pedido, o perfil em palavras e, a partir da segunda
rodada, a proposta anterior e o parecer. O perfil nunca leva número: "o
morador usa cadeira de rodas", e não o diâmetro que isso exige.

Conexão
-------
A do P1, sem mudança: `openai_base_url` e as mesmas três variáveis de
ambiente. `AZURE_OPENAI_API_VERSION` precisa não existir, porque o cliente a
lê sozinho e a rota v1 recusa o parâmetro com HTTP 400.
"""

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient, OpenAIChatOptions

from floorplan_guardrails.furnisher_prompt import INSTRUCTIONS
from floorplan_guardrails.furniture import (
    Catalog,
    FurnishingProposal,
    FurnishingRequest,
    Profile,
)
from floorplan_guardrails.generator import GeneratorError, openai_base_url
from floorplan_guardrails.schema import FloorPlan

#: Esforço de raciocínio padrão, o mesmo do gerador do P1.
DEFAULT_REASONING_EFFORT = "low"

#: As variáveis de ambiente que o mobiliador lê, as mesmas do P1.
ENV_NAMES = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
)

#: A variável que precisa não existir.
FORBIDDEN_ENV = "AZURE_OPENAI_API_VERSION"


class FurnisherError(Exception):
    """Falha ao falar com o modelo, com mensagem pronta para o aluno ler."""


@dataclass(frozen=True)
class Feedback:
    """Um item do parecer, do jeito que o mobiliador o recebe.

    `message` é o problema; `suggestion`, o que fazer, e pode vir vazia
    quando o parecer é só a lista de violações de integridade.
    """

    message: str
    suggestion: str


@dataclass(frozen=True)
class FurnishingAttempt:
    """O que uma chamada ao mobiliador produziu."""

    proposal: FurnishingProposal
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Furnisher(Protocol):
    """O contrato que a negociação usa.

    Existe para que a negociação possa ser testada com um mobiliador
    roteirizado, sem chave e sem rede, como o `Generator` do P1.
    """

    async def propose(
        self,
        plan: FloorPlan,
        catalog: Catalog,
        request: FurnishingRequest,
        profile: Profile,
        previous: FurnishingProposal | None = None,
        feedback: Sequence[Feedback] = (),
    ) -> FurnishingAttempt:
        """Propõe a disposição, corrigindo a anterior se houver parecer."""
        ...


# --- a mensagem ----------------------------------------------------------------


def requested_catalog(catalog: Catalog, request: FurnishingRequest) -> Catalog:
    """O catálogo só com os itens pedidos, na ordem em que aparecem no pedido.

    Item pedido que não existe no catálogo fica de fora: a verificação de
    integridade vai apontá-lo, com o id, no parecer.
    """
    wanted: Catalog = {}

    for item_ids in request.items.values():
        for item_id in item_ids:
            if item_id in catalog and item_id not in wanted:
                wanted[item_id] = catalog[item_id]

    return wanted


def catalog_json(catalog: Catalog) -> str:
    """O catálogo como o mobiliador o lê.

    Vão o nome, as dimensões, os lados de uso com o modo (`all`: todos
    precisam ficar livres; `any`: basta um) e os tipos de cômodo permitidos.
    A fonte da dimensão e `blocks_use_zones` ficam de fora: a primeira não
    ajuda a posicionar, e o segundo é detalhe da verificação.
    """
    data = {
        item_id: {
            "name": item.name,
            "width": item.width,
            "depth": item.depth,
            "use_sides": item.use_sides,
            "use_mode": item.use_mode,
            "room_types": item.room_types,
        }
        for item_id, item in catalog.items()
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def profile_in_words(profile: Profile) -> str:
    """O perfil do morador numa frase, sem nenhum número."""
    if profile.accessible:
        return "O morador usa cadeira de rodas."

    return "O morador não usa cadeira de rodas."


def build_message(
    plan: FloorPlan,
    catalog: Catalog,
    request: FurnishingRequest,
    profile: Profile,
    previous: FurnishingProposal | None = None,
    feedback: Sequence[Feedback] = (),
) -> str:
    """Monta a mensagem enviada ao mobiliador.

    Na primeira rodada vão a planta, o catálogo dos itens pedidos, o pedido
    e o perfil. Nas seguintes vão junto a proposta anterior e o parecer, que
    é o único caminho pelo qual as exigências de circulação chegam ao modelo.
    """
    parts = [
        "Planta aprovada, em que os móveis serão posicionados:\n"
        + json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
        "Catálogo dos itens pedidos, com as dimensões sem rotação:\n"
        + catalog_json(requested_catalog(catalog, request)),
        "Pedido de móveis por cômodo, do id do cômodo à lista de itens; "
        "item repetido significa mais de uma unidade:\n"
        + json.dumps(request.items, ensure_ascii=False, indent=2),
    ]

    if request.notes.strip():
        parts.append(f"Observações do pedido:\n{request.notes.strip()}")

    parts.append(f"Perfil do morador:\n{profile_in_words(profile)}")

    if previous is not None:
        parts.append(
            "Proposta que você fez antes:\n"
            + json.dumps(previous.model_dump(), ensure_ascii=False, indent=2)
        )

    if feedback:
        lines = []
        for item in feedback:
            lines.append(f"- {item.message}")
            if item.suggestion.strip():
                lines.append(f"  Sugestão: {item.suggestion.strip()}")

        parts.append(
            "O verificador reprovou essa proposta. Parecer:\n"
            + "\n".join(lines)
            + "\n\nDevolva a proposta inteira corrigida."
        )

    return "\n\n".join(parts)


# --- o agente ------------------------------------------------------------------


class AzureFurnisher:
    """Mobiliador ligado a um deployment do Azure OpenAI."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> None:
        self.deployment = deployment
        self.reasoning_effort = reasoning_effort

        try:
            base_url = openai_base_url(endpoint)
        except GeneratorError as exc:
            # A mesma frase do P1, que diz o que copiar do portal.
            raise FurnisherError(str(exc)) from exc

        self._agent = Agent(
            name="mobiliador",
            instructions=INSTRUCTIONS,
            client=OpenAIChatClient(
                api_key=api_key,
                base_url=base_url,
                model=deployment,
            ),
            default_options=OpenAIChatOptions(
                response_format=FurnishingProposal,
                reasoning={"effort": reasoning_effort},
            ),
        )

    async def propose(
        self,
        plan: FloorPlan,
        catalog: Catalog,
        request: FurnishingRequest,
        profile: Profile,
        previous: FurnishingProposal | None = None,
        feedback: Sequence[Feedback] = (),
    ) -> FurnishingAttempt:
        message = build_message(plan, catalog, request, profile, previous, feedback)
        started = time.monotonic()

        try:
            reply = await self._agent.run(message)
        except Exception as exc:
            raise FurnisherError(
                "A chamada ao mobiliador falhou. Confira o endpoint, a chave e o "
                f"nome do deployment.\n\n{type(exc).__name__}: {exc}"
            ) from exc

        latency_ms = round((time.monotonic() - started) * 1000)
        proposal = reply.value

        if not isinstance(proposal, FurnishingProposal):
            raise FurnisherError(
                "O mobiliador respondeu fora do formato esperado. "
                "Confira se o deployment suporta saída estruturada estrita."
            )

        usage = reply.usage_details or {}

        return FurnishingAttempt(
            proposal=proposal,
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
            latency_ms=latency_ms,
        )


def furnisher_from_env(
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> AzureFurnisher:
    """Monta o mobiliador a partir das variáveis de ambiente.

    Falta alguma, a mensagem diz qual. Sobra `AZURE_OPENAI_API_VERSION`, a
    mensagem diz para apagá-la: com ela, toda chamada falharia com um HTTP
    400 que não explica nada.
    """
    missing = [name for name in ENV_NAMES if not os.environ.get(name)]

    if missing:
        raise FurnisherError(
            "Faltam variáveis de ambiente para falar com o Azure: "
            + ", ".join(missing)
            + ". Rode antes a célula de configuração do notebook, ou copie "
            "o arquivo .env.example para .env e preencha."
        )

    if FORBIDDEN_ENV in os.environ:
        raise FurnisherError(
            f"A variável {FORBIDDEN_ENV} está definida, e a rota usada aqui "
            "recusa esse parâmetro. Apague a variável (no notebook, a célula de "
            "configuração já faz isso) e tente de novo."
        )

    return AzureFurnisher(
        endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        reasoning_effort=reasoning_effort,
    )
