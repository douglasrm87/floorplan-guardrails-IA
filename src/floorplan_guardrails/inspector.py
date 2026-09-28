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
negociação quando só há violações de integridade.

O fiscal com modelo
-------------------
`ModelInspector` é um agente do Microsoft Agent Framework, pela mesma rota
do mobiliador, com uma ferramenta, `consultar_parametro`, e saída
estruturada estrita na mesma chamada. O que ele escreve passa por três
proteções antes de chegar ao mobiliador:

1. O `ref` de cada achado só pode ser um dos ids daquela chamada (`f1`,
   `f2`, ...). O formato da resposta é montado a cada chamada, com a lista
   fechada de ids, e o modo estrito recusa qualquer outro.
2. Todo número da explicação e do resumo precisa ser um número que o código
   forneceu: o medido, o exigido ou o que a ferramenta devolveu. Número
   inventado derruba o texto inteiro, que é trocado pela `message`.
3. Violação sem achado recebe a `message`; `ref` repetido vale o primeiro.

Medido, exigido, unidade e fonte nunca saem do texto do modelo: o parecer
tem um item por violação, na mesma ordem, e esses campos são lidos da
própria violação.
"""

import json
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, Protocol

from agent_framework import (
    Agent,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    tool,
)
from agent_framework.openai import OpenAIChatClient, OpenAIChatOptions
from pydantic import BaseModel, ConfigDict, WithJsonSchema, create_model

from floorplan_guardrails.derived_geometry import geometry_block
from floorplan_guardrails.furnisher import (
    DEFAULT_REASONING_EFFORT,
    ENV_NAMES,
    FORBIDDEN_ENV,
    Feedback,
    profile_in_words,
)
from floorplan_guardrails.furniture import (
    Catalog,
    FurnishedPlan,
    FurnishingProposal,
    Profile,
)
from floorplan_guardrails.furniture_rules import FurnitureRules
from floorplan_guardrails.generator import GeneratorError, openai_base_url
from floorplan_guardrails.inspection import FurnitureViolation
from floorplan_guardrails.inspector_prompt import INSTRUCTIONS
from floorplan_guardrails.validator import number

#: O parecer de uma proposta aprovada, escrito pelo código, sem modelo.
APPROVED_SUMMARY = "Aprovado: nenhuma verificação falhou."

#: Quanto um número do texto pode se afastar do número fornecido.
NUMBER_TOLERANCE = 0.005

#: Onde um `Replacement` do resumo é registrado, no lugar do id do achado.
SUMMARY_REF = "summary"


class InspectorError(Exception):
    """Falha ao falar com o modelo, com mensagem pronta para o aluno ler."""


@dataclass(frozen=True)
class Replacement:
    """Um texto do fiscal trocado pelo código, e por quê.

    `ref` é o id do achado, ou `summary` quando o trocado foi o resumo.
    `numbers` são os números do texto original que não batiam com nada do
    que foi fornecido, como estavam escritos.
    """

    ref: str
    original: str
    numbers: list[str]


@dataclass(frozen=True)
class Review:
    """O parecer de uma rodada, do jeito que a negociação o guarda.

    `feedback` tem um item por violação, na ordem de `inspect`, e é o que
    volta ao mobiliador. Tokens, latência e chamadas de ferramenta ficam em
    zero quando o parecer foi escrito pelo código.

    Os quatro últimos campos registram o que o código corrigiu no texto do
    fiscal: parâmetros pedidos à ferramenta que não existem, textos trocados
    por número inventado, `ref` repetido e violação que ficou sem achado.
    """

    feedback: list[Feedback] = field(default_factory=list)
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tool_calls: int = 0
    invalid_params: list[str] = field(default_factory=list)
    replacements: list[Replacement] = field(default_factory=list)
    duplicate_refs: list[str] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)


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


def rejected_summary(count: int) -> str:
    """O resumo escrito pelo código quando o do fiscal é descartado."""
    if count == 1:
        return "Reprovado: 1 verificação falhou."

    return f"Reprovado: {count} verificações falharam."


# --- a ferramenta --------------------------------------------------------------


def parameter_ids(rules: FurnitureRules) -> list[str]:
    """Os ids que `consultar_parametro` aceita.

    A faixa de uso tem um valor por item, e por isso um id por item:
    `use_zone_depth.wardrobe`, `use_zone_depth.sofa`, ...
    """
    return [
        "door_clearance_depth",
        *(f"use_zone_depth.{item_id}" for item_id in rules.use_zone_depth.values),
        "turning_diameter",
    ]


def lookup(rules: FurnitureRules, param_id: str) -> dict | None:
    """O parâmetro como a ferramenta o devolve, ou `None` se o id não existe."""
    if param_id == "door_clearance_depth":
        parameter = rules.door_clearance_depth
        value = parameter.value
    elif param_id == "turning_diameter":
        parameter = rules.turning_diameter
        value = parameter.value
    elif param_id.startswith("use_zone_depth."):
        parameter = rules.use_zone_depth
        value = parameter.values.get(param_id.removeprefix("use_zone_depth."))
        if value is None:
            return None
    else:
        return None

    return {
        "value": value,
        "unit": parameter.unit,
        "source": parameter.source,
        "description": parameter.description,
    }


def unknown_parameter(param_id: str, valid_ids: Sequence[str]) -> str:
    """A resposta da ferramenta a um id que não existe, em português."""
    return (
        f'O parâmetro "{param_id}" não existe. Os ids válidos são: '
        + ", ".join(valid_ids)
        + "."
    )


class Consultation(FunctionMiddleware):
    """A ferramenta de uma chamada ao fiscal, com o registro do que devolveu.

    É criada de novo a cada chamada, e guarda cada id pedido, os inválidos e
    os números que a ferramenta devolveu, que são os únicos que o fiscal pode
    citar além do medido e do exigido.

    Por que também é middleware
    ---------------------------
    O `param_id` da ferramenta é um `Literal` dos ids existentes, para que o
    modelo veja a lista no formato da ferramenta. Mas o framework confere o
    argumento contra essa lista antes de chamar a função, e um id inválido
    viraria um erro genérico em inglês, sem passar por aqui. Como
    middleware, esta classe vê a chamada antes da conferência: um id que não
    existe recebe a resposta em português, com os ids válidos, e fica
    registrado.
    """

    def __init__(self, rules: FurnitureRules) -> None:
        self.rules = rules
        self.valid_ids = parameter_ids(rules)
        self.requested: list[str] = []
        self.invalid: list[str] = []
        self.numbers: list[float] = []

    def consult(self, param_id: str) -> dict | str:
        """Responde a um pedido e o registra, válido ou não."""
        self.requested.append(param_id)
        found = lookup(self.rules, param_id)

        if found is None:
            self.invalid.append(param_id)
            return unknown_parameter(param_id, self.valid_ids)

        # A fonte pode trazer número (o da norma, por exemplo), e citar a
        # fonte é justamente o que se pede ao fiscal.
        self.numbers.append(float(found["value"]))
        for text in (found["source"], found["description"]):
            self.numbers.extend(parse(written) for written in numbers_in(text))

        return found

    def tool(self) -> FunctionTool:
        """A ferramenta `consultar_parametro`, com a lista fechada de ids."""
        ParamId = Literal[tuple(self.valid_ids)]  # noqa: N806

        @tool(
            name="consultar_parametro",
            description=(
                "Consulta um parâmetro de circulação e devolve o valor, a "
                "unidade, a fonte e a descrição."
            ),
        )
        def consultar_parametro(
            param_id: Annotated[ParamId, "O id do parâmetro."],
        ) -> dict | str:
            return self.consult(param_id)

        return consultar_parametro

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        arguments = context.arguments
        if isinstance(arguments, BaseModel):
            arguments = arguments.model_dump()

        param_id = str((arguments or {}).get("param_id", ""))

        if param_id in self.valid_ids:
            await call_next()
        else:
            context.result = self.consult(param_id)


# --- a mensagem ----------------------------------------------------------------


def finding_ids(count: int) -> list[str]:
    """Os ids das violações de uma chamada: f1, f2, ..."""
    return [f"f{index}" for index in range(1, count + 1)]


def parameter_of(
    violation: FurnitureViolation, proposal: FurnishingProposal
) -> str | None:
    """O id do parâmetro que exige o que a violação cobra.

    As violações de integridade não têm parâmetro: são regras fixas no
    código. A da faixa de uso depende do item, que é o do primeiro móvel
    citado, o dono da faixa.
    """
    if violation.rule_id == "door_clearance":
        return "door_clearance_depth"
    if violation.rule_id == "turning_space":
        return "turning_diameter"
    if violation.rule_id == "use_zone" and violation.placement_ids:
        owner = violation.placement_ids[0]
        for placement in proposal.placements:
            if placement.id == owner:
                return f"use_zone_depth.{placement.item_id}"

    return None


def build_message(
    furnished: FurnishedPlan,
    violations: Sequence[FurnitureViolation],
    profile: Profile,
    catalog: Catalog,
) -> str:
    """Monta a mensagem enviada ao fiscal.

    Cada violação começa pelo seu id, sozinho numa linha. No experimento da
    Fase 0, com o id seguido de um rótulo na mesma linha, o modelo copiou o
    rótulo para o `ref`.

    Depois da planta vai a geometria calculada pelo código: limites dos
    cômodos, vão das portas, retângulo e frente de cada móvel. Na Fase 4b,
    sem ela, o fiscal sugeriu posições fora do cômodo e rotações com a
    frente para o lado errado.

    A fonte do parâmetro não vai na mensagem: o fiscal a consulta pela
    ferramenta.
    """
    blocks = []

    for ref, violation in zip(finding_ids(len(violations)), violations, strict=True):
        lines = [
            ref,
            f"Regra: {violation.rule_id}",
            f"Mensagem: {violation.message}",
        ]
        if violation.measured is not None:
            lines.append(f"Medido: {number(violation.measured)}")
        if violation.required is not None:
            lines.append(f"Exigido: {number(violation.required)}")
        lines.append(f"Unidade: {violation.unit}")
        if violation.placement_ids:
            lines.append("Móveis: " + ", ".join(violation.placement_ids))
        if violation.room_ids:
            lines.append("Cômodos: " + ", ".join(violation.room_ids))

        parameter = parameter_of(violation, furnished.proposal)
        if parameter is not None:
            lines.append(f"Parâmetro: {parameter}")

        blocks.append("\n".join(lines))

    return "\n\n".join(
        [
            "Verificações que falharam:",
            *blocks,
            "Planta com os móveis:\n"
            + json.dumps(furnished.model_dump(), ensure_ascii=False, indent=2),
            geometry_block(furnished.plan, catalog, furnished.proposal),
            f"Perfil do morador:\n{profile_in_words(profile)}",
        ]
    )


# --- o formato da resposta -----------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def report_model(refs: Sequence[str]) -> type[BaseModel]:
    """O formato `InspectionReport` de uma chamada, com `ref` preso aos ids dela.

    Montado a cada chamada porque a lista muda com o número de violações. O
    `enum` vai escrito à mão no JSON Schema: com um id só, o Pydantic
    escreveria `const`, e o teste do modo estrito pede o `enum` sempre.
    """
    ref_type = Annotated[
        Literal[tuple(refs)],
        WithJsonSchema({"type": "string", "enum": list(refs)}),
    ]
    finding = create_model(
        "Finding",
        __base__=_Strict,
        ref=(ref_type, ...),
        explanation=(str, ...),
        suggestion=(str, ...),
    )

    return create_model(
        "InspectionReport",
        __base__=_Strict,
        findings=(list[finding], ...),
        summary=(str, ...),
    )


# --- a verificação dos números -------------------------------------------------

# Um número solto no texto, com vírgula ou ponto decimal. Dígito colado numa
# letra não conta: m1, r3 e f2 são ids, não números.
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> list[str]:
    """Os números de um texto, como estão escritos."""
    return _NUMBER.findall(text)


def parse(written: str) -> float:
    """Um número escrito com vírgula ou ponto decimal."""
    return float(written.replace(",", "."))


def unsupported_numbers(text: str, allowed: Iterable[float]) -> list[str]:
    """Os números do texto que não batem com nenhum dos permitidos.

    Bater é ficar a até `NUMBER_TOLERANCE` de algum deles. A folga de 1e-9
    absorve o erro de ponto flutuante: 0,805 - 0,80 dá 0,00500000000000004.
    """
    allowed = list(allowed)

    return [
        written
        for written in numbers_in(text)
        if not any(
            abs(parse(written) - value) <= NUMBER_TOLERANCE + 1e-9 for value in allowed
        )
    ]


def given_numbers(violation: FurnitureViolation) -> list[float]:
    """O medido e o exigido de uma violação, os que existirem."""
    return [
        value for value in (violation.measured, violation.required) if value is not None
    ]


def assemble_review(
    violations: Sequence[FurnitureViolation],
    report: Any,
    tool_numbers: Sequence[float] = (),
) -> Review:
    """O parecer final, montado pelo código a partir da resposta do fiscal.

    `report` é a resposta no formato de `report_model`. O parecer tem um item
    por violação, na ordem de `inspect`:

    - achado com número que não foi fornecido: a explicação vira a `message`
      da violação, e a sugestão fica;
    - violação sem achado: a `message`, com sugestão vazia;
    - `ref` repetido: vale o primeiro achado.

    O resumo passa pela mesma verificação, contra os números de todas as
    violações; se cair, vira o resumo escrito pelo código.
    """
    refs = finding_ids(len(violations))
    first: dict[str, Any] = {}
    duplicates: list[str] = []

    for finding in report.findings:
        if finding.ref in first:
            duplicates.append(finding.ref)
        else:
            first[finding.ref] = finding

    feedback: list[Feedback] = []
    replacements: list[Replacement] = []
    missing: list[str] = []

    for ref, violation in zip(refs, violations, strict=True):
        finding = first.get(ref)

        if finding is None:
            missing.append(ref)
            feedback.append(Feedback(message=violation.message, suggestion=""))
            continue

        wrong = unsupported_numbers(
            finding.explanation, [*given_numbers(violation), *tool_numbers]
        )
        if wrong:
            replacements.append(Replacement(ref, finding.explanation, wrong))
            explanation = violation.message
        else:
            explanation = finding.explanation

        feedback.append(Feedback(message=explanation, suggestion=finding.suggestion))

    everything = [
        value for violation in violations for value in given_numbers(violation)
    ]
    summary = report.summary
    wrong = unsupported_numbers(summary, [*everything, *tool_numbers])
    if wrong:
        replacements.append(Replacement(SUMMARY_REF, summary, wrong))
        summary = rejected_summary(len(violations))

    return Review(
        feedback=feedback,
        summary=summary,
        replacements=replacements,
        duplicate_refs=duplicates,
        missing_refs=missing,
    )


# --- o agente ------------------------------------------------------------------


class ModelInspector:
    """Fiscal com modelo: um agente com a ferramenta e saída estrita.

    Recebe o cliente de chat pronto, para que os testes possam trocá-lo por
    um roteirizado; `inspector_from_env` monta o do Azure. O catálogo vem no
    construtor, e não em `review`, para que o `Protocol` continue o mesmo:
    só o fiscal com modelo precisa dele, para calcular a geometria. O agente é
    recriado a cada chamada, porque o formato da resposta e o registro da
    ferramenta são daquela chamada; o custo é desprezível perto da espera
    pelo modelo.
    """

    def __init__(
        self,
        client: Any,
        rules: FurnitureRules,
        catalog: Catalog,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> None:
        self.client = client
        self.rules = rules
        self.catalog = catalog
        self.reasoning_effort = reasoning_effort

    async def review(
        self,
        furnished: FurnishedPlan,
        violations: Sequence[FurnitureViolation],
        profile: Profile,
    ) -> Review:
        report_type = report_model(finding_ids(len(violations)))
        consultation = Consultation(self.rules)
        agent = Agent(
            name="fiscal",
            instructions=INSTRUCTIONS,
            client=self.client,
            tools=[consultation.tool()],
            middleware=[consultation],
            default_options=OpenAIChatOptions(
                response_format=report_type,
                reasoning={"effort": self.reasoning_effort},
            ),
        )

        message = build_message(furnished, violations, profile, self.catalog)
        started = time.monotonic()

        try:
            reply = await agent.run(message)
        except Exception as exc:
            raise InspectorError(
                "A chamada ao fiscal falhou. Confira o endpoint, a chave e o "
                f"nome do deployment.\n\n{type(exc).__name__}: {exc}"
            ) from exc

        latency_ms = round((time.monotonic() - started) * 1000)

        # O framework só lê a resposta no formato pedido quando `value` é
        # acessado, e um texto fora do formato levanta ali.
        try:
            report = reply.value
        except ValueError:
            report = None

        if not isinstance(report, report_type):
            raise InspectorError(
                "O fiscal respondeu fora do formato esperado. "
                "Confira se o deployment suporta saída estruturada estrita."
            )

        usage = reply.usage_details or {}
        review = assemble_review(violations, report, consultation.numbers)

        return replace(
            review,
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
            latency_ms=latency_ms,
            tool_calls=len(consultation.requested),
            invalid_params=list(consultation.invalid),
        )


def inspector_from_env(
    rules: FurnitureRules,
    catalog: Catalog,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> ModelInspector:
    """Monta o fiscal com modelo a partir das variáveis de ambiente do P1.

    As mesmas variáveis e a mesma rota do mobiliador, com as mesmas
    mensagens para o que faltar ou sobrar.
    """
    missing = [name for name in ENV_NAMES if not os.environ.get(name)]

    if missing:
        raise InspectorError(
            "Faltam variáveis de ambiente para falar com o Azure: "
            + ", ".join(missing)
            + ". Rode antes a célula de configuração do notebook, ou copie "
            "o arquivo .env.example para .env e preencha."
        )

    if FORBIDDEN_ENV in os.environ:
        raise InspectorError(
            f"A variável {FORBIDDEN_ENV} está definida, e a rota usada aqui "
            "recusa esse parâmetro. Apague a variável (no notebook, a célula de "
            "configuração já faz isso) e tente de novo."
        )

    try:
        base_url = openai_base_url(os.environ["AZURE_OPENAI_ENDPOINT"])
    except GeneratorError as exc:
        raise InspectorError(str(exc)) from exc

    client = OpenAIChatClient(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        base_url=base_url,
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
    )

    return ModelInspector(client, rules, catalog, reasoning_effort)
