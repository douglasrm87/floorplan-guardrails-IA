"""Testes do fiscal com modelo.

Nenhum teste chama o Azure. O fiscal com modelo roda de verdade, com o
agente, a ferramenta e o middleware do Microsoft Agent Framework, mas sobre
um cliente de chat roteirizado: devolve as respostas combinadas, em ordem, e
guarda cada pedido que recebeu. O framework executa a ferramenta entre uma
resposta e outra, como faria com o modelo.

Os testes usam só as fixtures de `tests/fixtures/p2/`. A exceção é o teste
do princípio 2, que lê o arquivo real de parâmetros, como o do mobiliador.
"""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from floorplan_guardrails.furnisher import Feedback, FurnishingAttempt
from floorplan_guardrails.furniture import (
    Catalog,
    FurnishedPlan,
    FurnishingProposal,
    FurnishingRequest,
    Profile,
    load_catalog,
)
from floorplan_guardrails.furniture_rules import (
    DEFAULT_FURNITURE_RULES_PATH,
    load_furniture_rules,
)
from floorplan_guardrails.inspection import FurnitureViolation, inspect
from floorplan_guardrails.inspector import (
    SUMMARY_REF,
    Consultation,
    InspectorError,
    ModelInspector,
    Replacement,
    assemble_review,
    build_message,
    finding_ids,
    inspector_from_env,
    lookup,
    numbers_in,
    parameter_ids,
    parameter_of,
    rejected_summary,
    report_model,
    unsupported_numbers,
)
from floorplan_guardrails.inspector_prompt import INSTRUCTIONS
from floorplan_guardrails.negotiation import Negotiation, negotiate
from floorplan_guardrails.negotiation_log import NegotiationLog, load_negotiation
from floorplan_guardrails.rules import load_rules
from floorplan_guardrails.runlog import read_run
from floorplan_guardrails.schema import FloorPlan

REPO_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "p2"
CATALOG = load_catalog(FIXTURES / "catalog.yaml")
RULES = load_furniture_rules(CATALOG, FIXTURES / "furniture_rules.yaml")
PLAN_RULES = load_rules(FIXTURES / "plan_rules.yaml")


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def plan() -> FloorPlan:
    return FloorPlan.model_validate(read("plan.json"))


def request() -> FurnishingRequest:
    return FurnishingRequest.model_validate(read("request.json"))


def proposal(**changes: dict) -> FurnishingProposal:
    """A proposta de referência, aprovada, com os móveis indicados mudados."""
    data = read("proposal.json")
    for placement in data["placements"]:
        placement.update(changes.get(placement["id"], {}))

    return FurnishingProposal.model_validate(data)


def cramped() -> FurnishingProposal:
    """Três violações de circulação, nesta ordem.

    f1: a cama (m2) deixa 0,35 m diante da porta do quarto; exigidos 0,80 m.
    f2: a cama tem 0,20 m à direita, até o guarda-roupa; exigidos 0,50 m.
    f3: o guarda-roupa (m4) tem 0,20 m na frente, até a cama; exigidos 0,50 m.
    """
    return proposal(m2={"y": 3.35}, m4={"x": 2.8})


def without_nightstand() -> FurnishingProposal:
    """Só uma violação de integridade: a mesa de cabeceira pedida não veio."""
    data = proposal().model_dump()
    data["placements"] = [p for p in data["placements"] if p["id"] != "m3"]

    return FurnishingProposal.model_validate(data)


def furnished(item: FurnishingProposal | None = None) -> FurnishedPlan:
    return FurnishedPlan(plan=plan(), proposal=item or cramped())


def violations_of(item: FurnishingProposal | None = None) -> list[FurnitureViolation]:
    return inspect(furnished(item), CATALOG, RULES, Profile(), request())


# --- o cliente roteirizado -----------------------------------------------------


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """Cliente de chat de mentira: entrega as respostas combinadas, em ordem.

    Herda a camada que executa ferramentas, a mesma do cliente do Azure, e
    por isso a ferramenta e o middleware do fiscal rodam de verdade. Guarda
    as mensagens e as opções de cada pedido.
    """

    def __init__(self, replies: Sequence[ChatResponse] = ()) -> None:
        super().__init__()
        self.replies = list(replies)
        self.requests: list[tuple[list[Message], dict]] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.requests.append((list(messages), dict(options)))
        reply = self.replies.pop(0)

        async def respond() -> ChatResponse:
            return reply

        return respond()


def tool_calls(*param_ids: str) -> ChatResponse:
    """Uma resposta do modelo que só pede a ferramenta, uma vez por id."""
    return ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id=f"c{index}",
                        name="consultar_parametro",
                        arguments=json.dumps({"param_id": param_id}),
                    )
                    for index, param_id in enumerate(param_ids)
                ],
            )
        ]
    )


def answer(findings: list[dict], summary: str = "Afaste os móveis.") -> ChatResponse:
    """A resposta final do modelo, no formato do parecer."""
    text = json.dumps({"findings": findings, "summary": summary}, ensure_ascii=False)
    return ChatResponse(messages=[Message(role="assistant", contents=[text])])


def finding(ref: str, explanation: str = "", suggestion: str = "") -> dict:
    return {
        "ref": ref,
        "explanation": explanation or f"Explicação de {ref}.",
        "suggestion": suggestion,
    }


def tool_results(client: ScriptedChatClient) -> list[str]:
    """O que a ferramenta devolveu ao modelo, lido do pedido seguinte."""
    messages, _ = client.requests[-1]
    return [
        str(content.result)
        for message in messages
        for content in message.contents
        if content.type == "function_result"
    ]


def report(findings: list[dict], summary: str = "Afaste os móveis."):
    """A resposta do fiscal já validada no formato de três achados."""
    return report_model(finding_ids(3)).model_validate(
        {"findings": findings, "summary": summary}
    )


# --- o princípio 2, estendido ------------------------------------------------


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


def renderings(value: float) -> set[str]:
    """Os jeitos plausíveis de um número aparecer escrito num texto."""
    written = {f"{value:g}", f"{value:.1f}", f"{value:.2f}"}
    return written | {text.replace(".", ",") for text in written}


def test_instructions_carry_no_circulation_value() -> None:
    """O fiscal cita números que recebe, e nunca os traz de casa.

    Mesmo teste do mobiliador: lê o arquivo real e varre todos os números
    dele, em qualquer nível.
    """
    raw = yaml.safe_load(
        (REPO_ROOT / DEFAULT_FURNITURE_RULES_PATH).read_text(encoding="utf-8")
    )
    values = sorted(set(numbers_of(raw)))
    assert len(values) >= 3

    leaked = [
        written
        for value in values
        for written in renderings(value)
        if written in INSTRUCTIONS
    ]

    assert not leaked, "valor de circulação vazou para as instruções: " + ", ".join(
        leaked
    )


def test_instructions_forbid_approving_and_ask_for_the_given_numbers_only() -> None:
    assert "Você não aprova" in INSTRUCTIONS
    assert "use só os números que vieram na mensagem" in INSTRUCTIONS
    assert "consultar_parametro" in INSTRUCTIONS


# --- o formato da resposta ---------------------------------------------------


def object_schemas(node: object) -> list[dict]:
    """Percorre o JSON Schema e devolve todo subesquema de objeto."""
    found: list[dict] = []

    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(object_schemas(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(object_schemas(value))

    return found


@pytest.mark.parametrize("count", [1, 3])
def test_the_report_schema_is_strict_and_closes_ref_on_the_call_ids(
    count: int,
) -> None:
    """O `ref` só aceita os ids daquela chamada, e o modo estrito é atendido.

    Com um id só, o Pydantic escreveria `const`; o esquema precisa do `enum`.
    """
    schema = report_model(finding_ids(count)).model_json_schema()
    objects = object_schemas(schema)

    assert {obj["title"] for obj in objects} == {"InspectionReport", "Finding"}
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj["title"]
        assert set(obj.get("required", [])) == set(obj["properties"]), obj["title"]

    ref = next(obj for obj in objects if obj["title"] == "Finding")["properties"]["ref"]
    assert ref["type"] == "string"
    assert ref["enum"] == finding_ids(count)
    assert "const" not in ref


def test_a_ref_outside_the_call_is_refused_by_the_format() -> None:
    model = report_model(finding_ids(2))

    with pytest.raises(ValueError):
        model.model_validate(
            {"findings": [finding("f3")], "summary": "Resumo."},
        )
    with pytest.raises(ValueError):
        model.model_validate(
            {"findings": [finding("f1 (door_clearance)")], "summary": "Resumo."},
        )


def test_the_format_goes_with_the_request_to_the_model() -> None:
    client = ScriptedChatClient([answer([finding("f1"), finding("f2"), finding("f3")])])

    asyncio.run(
        ModelInspector(client, RULES).review(furnished(), violations_of(), Profile())
    )

    _, options = client.requests[0]
    schema = options["response_format"].model_json_schema()
    assert schema["$defs"]["Finding"]["properties"]["ref"]["enum"] == [
        "f1",
        "f2",
        "f3",
    ]


# --- a mensagem ----------------------------------------------------------------


def test_each_id_stands_alone_on_its_own_line() -> None:
    """No experimento da Fase 0, id e rótulo na mesma linha foram copiados
    juntos para o `ref`."""
    violations = violations_of()
    message = build_message(furnished(), violations, Profile())
    lines = message.splitlines()

    for ref in finding_ids(len(violations)):
        assert lines.count(ref) == 1
        assert [line for line in lines if ref in line.split()] == [ref]


def test_the_message_carries_each_violation_without_the_source() -> None:
    violations = violations_of()
    message = build_message(furnished(), violations, Profile(accessible=True))

    first = message.split("\n\n")[1]
    assert first.splitlines() == [
        "f1",
        "Regra: door_clearance",
        f"Mensagem: {violations[0].message}",
        "Medido: 0,35",
        "Exigido: 0,80",
        "Unidade: m",
        "Móveis: m2",
        "Cômodos: r3",
        "Parâmetro: door_clearance_depth",
    ]
    assert "Parâmetro de teste." not in message
    assert "O morador usa cadeira de rodas." in message
    assert '"placements"' in message


def test_each_circulation_rule_points_to_its_parameter() -> None:
    violations = violations_of()
    item = cramped()

    assert [parameter_of(violation, item) for violation in violations] == [
        "door_clearance_depth",
        "use_zone_depth.bed_double",
        "use_zone_depth.wardrobe",
    ]
    assert all(parameter_of(v, item) in parameter_ids(RULES) for v in violations)


def test_an_integrity_violation_has_no_parameter() -> None:
    (violation,) = violations_of(without_nightstand())

    assert parameter_of(violation, without_nightstand()) is None


# --- a ferramenta --------------------------------------------------------------


def test_the_tool_returns_value_unit_source_and_description() -> None:
    assert lookup(RULES, "use_zone_depth.wardrobe") == {
        "value": 0.5,
        "unit": "m",
        "source": "Parâmetro de teste.",
        "description": "Profundidade da faixa livre diante de cada lado de uso.",
    }
    assert lookup(RULES, "turning_diameter")["value"] == 1.5


def test_the_tool_lists_the_valid_ids_in_its_schema() -> None:
    schema = Consultation(RULES).tool().parameters()

    assert schema["properties"]["param_id"]["enum"] == parameter_ids(RULES)


def test_an_unknown_id_gets_the_valid_ids_in_portuguese() -> None:
    consultation = Consultation(RULES)

    answer_text = consultation.consult("use_zone_depth.fogao")

    assert answer_text.startswith('O parâmetro "use_zone_depth.fogao" não existe.')
    for param_id in parameter_ids(RULES):
        assert param_id in answer_text
    assert consultation.invalid == ["use_zone_depth.fogao"]


def test_an_invalid_id_reaches_the_model_as_text_and_is_logged(
    tmp_path: Path,
) -> None:
    """O caminho inteiro: o modelo pede um id que não existe, a ferramenta
    responde em português sem exceção, o fiscal segue, e o registro guarda
    o id pedido."""
    client = ScriptedChatClient(
        [
            tool_calls("door_clearance", "door_clearance_depth"),
            answer([finding("f1"), finding("f2"), finding("f3")]),
        ]
    )
    log = NegotiationLog.create(tmp_path, run_id="teste")

    negotiation = run_with(ModelInspector(client, RULES), [cramped()], log=log)

    invalid, valid = tool_results(client)
    assert invalid.startswith('O parâmetro "door_clearance" não existe.')
    assert "use_zone_depth.wardrobe" in invalid
    assert json.loads(valid)["value"] == 0.8

    review = negotiation.rounds[0].review
    assert review.tool_calls == 2
    assert review.invalid_params == ["door_clearance"]

    (line,) = read_run(log.path)
    assert line["report"]["invalid_params"] == ["door_clearance"]
    assert line["report"]["tool_calls"] == 2


# --- os números no texto -------------------------------------------------------


def test_ids_are_not_numbers() -> None:
    assert numbers_in("A cama (m2) no quarto (r3), achado f1.") == []
    assert numbers_in("0,35 m, depois 0.80 m, depois 12.") == ["0,35", "0.80", "12"]


@pytest.mark.parametrize(
    ("written", "ok"),
    [
        ("0,35", True),
        ("0.35", True),
        ("0,80", True),
        ("0.8", True),
        ("0,355", True),
        ("0,345", True),
        ("0,356", False),
        ("0,344", False),
        ("0,40", False),
    ],
)
def test_numbers_match_with_comma_or_dot_within_the_tolerance(
    written: str, ok: bool
) -> None:
    text = f"A cama deixa {written} m livres."

    assert (unsupported_numbers(text, [0.35, 0.8]) == []) is ok


def test_an_explanation_with_the_given_numbers_is_kept() -> None:
    violations = violations_of()
    text = (
        "A cama deixa só 0,35 m diante da porta, e a regra pede 0.80 m "
        "(Parâmetro de teste.)."
    )

    review = assemble_review(
        violations,
        report([finding("f1", text), finding("f2"), finding("f3")]),
    )

    assert review.feedback[0].message == text
    assert review.replacements == []


def test_an_explanation_with_another_number_falls_back_to_the_message() -> None:
    violations = violations_of()
    text = "A cama deixa 0,35 m; afaste-a 0,45 m da porta."

    review = assemble_review(
        violations,
        report(
            [
                finding("f1", text, "Mova a cama (m2) 0,45 m para o norte."),
                finding("f2"),
                finding("f3"),
            ]
        ),
    )

    assert review.feedback[0] == Feedback(
        message=violations[0].message,
        suggestion="Mova a cama (m2) 0,45 m para o norte.",
    )
    assert review.replacements == [Replacement("f1", text, ["0,45"])]


def test_the_numbers_of_one_violation_do_not_cover_another() -> None:
    """f2 exige 0,50 m; citar isso na explicação de f1 é número de fora."""
    violations = violations_of()
    text = "A cama deixa 0,35 m, e a faixa de uso pede 0,50 m."

    review = assemble_review(
        violations, report([finding("f1", text), finding("f2"), finding("f3")])
    )

    assert review.feedback[0].message == violations[0].message
    assert review.replacements[0].numbers == ["0,50"]


def test_a_number_returned_by_the_tool_is_allowed() -> None:
    violations = violations_of()
    text = "A faixa de uso da cama é de 0,50 m, segundo o parâmetro."

    review = assemble_review(
        violations,
        report([finding("f1", text), finding("f2"), finding("f3")]),
        tool_numbers=[0.5],
    )

    assert review.feedback[0].message == text


def test_numbers_in_the_source_count_as_returned_by_the_tool() -> None:
    """Citar a fonte é o que se pede; "NBR 9050" não pode derrubar o texto."""
    rules = RULES.model_copy(
        update={
            "turning_diameter": RULES.turning_diameter.model_copy(
                update={"source": "ABNT NBR 9050, por fonte secundária."}
            )
        }
    )
    consultation = Consultation(rules)

    consultation.consult("turning_diameter")

    assert 9050.0 in consultation.numbers
    assert 1.5 in consultation.numbers


def test_numbers_are_allowed_in_the_suggestion() -> None:
    violations = violations_of()
    suggestion = "Mova a cama (m2) 0,45 m para o norte e gire o guarda-roupa 90 graus."

    review = assemble_review(
        violations,
        report(
            [
                finding("f1", "A cama deixa só 0,35 m.", suggestion),
                finding("f2"),
                finding("f3"),
            ]
        ),
    )

    assert review.feedback[0].suggestion == suggestion
    assert review.replacements == []


def test_the_summary_is_checked_like_the_explanation() -> None:
    violations = violations_of()
    findings = [finding("f1"), finding("f2"), finding("f3")]

    kept = assemble_review(
        violations, report(findings, "Libere 0,80 m na porta e 0,50 m nas faixas.")
    )
    dropped = assemble_review(violations, report(findings, "Afaste tudo 1,20 m."))

    assert kept.summary == "Libere 0,80 m na porta e 0,50 m nas faixas."
    assert dropped.summary == rejected_summary(3)
    assert dropped.replacements == [
        Replacement(SUMMARY_REF, "Afaste tudo 1,20 m.", ["1,20"])
    ]


# --- o pós-processamento -------------------------------------------------------


def test_a_violation_without_finding_gets_its_message() -> None:
    violations = violations_of()

    review = assemble_review(violations, report([finding("f1"), finding("f3")]))

    assert review.feedback[1] == Feedback(message=violations[1].message, suggestion="")
    assert review.missing_refs == ["f2"]
    assert len(review.feedback) == len(violations)


def test_a_repeated_ref_keeps_the_first_finding() -> None:
    violations = violations_of()

    review = assemble_review(
        violations,
        report(
            [
                finding("f2", "Primeira.", "Mova m4."),
                finding("f1"),
                finding("f2", "Segunda.", "Mova m2."),
                finding("f3"),
            ]
        ),
    )

    assert review.feedback[1] == Feedback(message="Primeira.", suggestion="Mova m4.")
    assert review.duplicate_refs == ["f2"]


def test_the_review_follows_the_order_of_the_violations() -> None:
    """Medido, exigido, unidade e fonte do parecer são os da violação de
    mesma posição: o texto do modelo não tem esses campos."""
    violations = violations_of()

    review = assemble_review(
        violations,
        report([finding("f3", "Terceira."), finding("f1", "Primeira."), finding("f2")]),
    )

    assert [item.message for item in review.feedback] == [
        "Primeira.",
        "Explicação de f2.",
        "Terceira.",
    ]


# --- o fiscal inteiro ----------------------------------------------------------


def test_the_model_inspector_reports_tokens_latency_and_tool_calls() -> None:
    client = ScriptedChatClient(
        [
            tool_calls("door_clearance_depth", "use_zone_depth.bed_double"),
            answer(
                [
                    finding("f1", "Só 0,35 m livres; o parâmetro pede 0,80 m."),
                    finding("f2", suggestion="Mova m4 para o leste."),
                ],
                "Afaste a cama da porta.",
            ),
        ]
    )

    review = asyncio.run(
        ModelInspector(client, RULES).review(furnished(), violations_of(), Profile())
    )

    assert review.tool_calls == 2
    assert review.invalid_params == []
    assert review.missing_refs == ["f3"]
    assert review.summary == "Afaste a cama da porta."
    assert review.feedback[0].message == "Só 0,35 m livres; o parâmetro pede 0,80 m."
    assert review.feedback[1].suggestion == "Mova m4 para o leste."
    assert review.latency_ms >= 0
    assert len(client.requests) == 2


def test_a_reply_outside_the_format_is_an_inspector_error() -> None:
    client = ScriptedChatClient(
        [ChatResponse(messages=[Message(role="assistant", contents=["não é JSON"])])]
    )

    with pytest.raises(InspectorError):
        asyncio.run(
            ModelInspector(client, RULES).review(
                furnished(), violations_of(), Profile()
            )
        )


def test_missing_variables_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(InspectorError, match="AZURE_OPENAI_API_KEY"):
        inspector_from_env(RULES)


# --- na negociação -------------------------------------------------------------


class ScriptedFurnisher:
    """Mobiliador de mentira: entrega as propostas combinadas, em ordem."""

    def __init__(self, proposals: Sequence[FurnishingProposal]) -> None:
        self.proposals = list(proposals)
        self.calls: list[list[Feedback]] = []

    async def propose(
        self,
        plan: FloorPlan,
        catalog: Catalog,
        request: FurnishingRequest,
        profile: Profile,
        previous: FurnishingProposal | None = None,
        feedback: Sequence[Feedback] = (),
    ) -> FurnishingAttempt:
        self.calls.append(list(feedback))
        item = self.proposals[min(len(self.calls) - 1, len(self.proposals) - 1)]

        return FurnishingAttempt(
            proposal=item, input_tokens=10, output_tokens=20, latency_ms=30
        )


def run_with(
    inspector: ModelInspector,
    proposals: Sequence[FurnishingProposal],
    **kwargs,
) -> Negotiation:
    return asyncio.run(
        negotiate(
            plan(),
            request(),
            Profile(),
            ScriptedFurnisher(proposals),
            inspector,
            CATALOG,
            RULES,
            plan_rules=PLAN_RULES,
            max_rounds=kwargs.pop("max_rounds", 1),
            **kwargs,
        )
    )


def test_an_approved_proposal_does_not_call_the_model() -> None:
    client = ScriptedChatClient()

    negotiation = run_with(ModelInspector(client, RULES), [proposal()])

    assert negotiation.status == "approved"
    assert client.requests == []


def test_a_round_with_only_integrity_violations_does_not_call_the_model() -> None:
    client = ScriptedChatClient()

    negotiation = run_with(
        ModelInspector(client, RULES), [without_nightstand(), proposal()], max_rounds=2
    )

    assert [item.status for item in negotiation.rounds] == ["rejected", "approved"]
    assert client.requests == []
    assert negotiation.rounds[0].review.tool_calls == 0


def test_the_suggestion_reaches_the_furnisher() -> None:
    client = ScriptedChatClient(
        [
            answer(
                [
                    finding("f1", suggestion="Mova a cama (m2) para o norte."),
                    finding("f2"),
                    finding("f3"),
                ]
            )
        ]
    )
    furnisher = ScriptedFurnisher([cramped(), proposal()])

    asyncio.run(
        negotiate(
            plan(),
            request(),
            Profile(),
            furnisher,
            ModelInspector(client, RULES),
            CATALOG,
            RULES,
            plan_rules=PLAN_RULES,
        )
    )

    assert furnisher.calls[1][0] == Feedback(
        message="Explicação de f1.", suggestion="Mova a cama (m2) para o norte."
    )


def test_replay_keeps_what_the_code_corrected(tmp_path: Path) -> None:
    client = ScriptedChatClient(
        [
            tool_calls("door_clearance", "door_clearance_depth"),
            answer(
                [
                    finding("f1", "Afaste 0,45 m."),
                    finding("f1"),
                    finding("f3"),
                ],
                "Resumo com 9 itens.",
            ),
        ]
    )
    log = NegotiationLog.create(tmp_path, run_id="teste")

    negotiation = run_with(ModelInspector(client, RULES), [cramped()], log=log)
    replayed = load_negotiation(log.path)

    review = replayed.rounds[0].review
    assert replayed == negotiation
    assert review.invalid_params == ["door_clearance"]
    assert [item.ref for item in review.replacements] == ["f1", SUMMARY_REF]
    assert review.duplicate_refs == ["f1"]
    assert review.missing_refs == ["f2"]


def test_a_line_recorded_before_the_corrections_still_loads(tmp_path: Path) -> None:
    """Os registros da 4a não têm os campos novos do parecer."""
    log = NegotiationLog.create(tmp_path, run_id="antigo")
    run_with(
        ModelInspector(ScriptedChatClient([answer([])]), RULES), [cramped()], log=log
    )

    line = read_run(log.path)[0]
    for key in ("invalid_params", "replacements", "duplicate_refs", "missing_refs"):
        del line["report"][key]
    log.path.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")

    review = load_negotiation(log.path).rounds[0].review

    assert review.replacements == []
    assert review.missing_refs == []
