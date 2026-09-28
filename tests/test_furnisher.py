"""Testes do mobiliador.

Nenhum teste aqui fala com o Azure: o que se verifica é o que o mobiliador
monta antes de falar, o contrato que a negociação vai usar e o que acontece
quando a configuração está errada.

O teste que mais importa é o primeiro: a extensão do princípio 2 ao P2. Ele
lê o arquivo real de parâmetros, e não uma lista copiada dele, para que
continue valendo quando os valores mudarem.
"""

import asyncio
import inspect as signatures
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from floorplan_guardrails.furnisher import (
    AzureFurnisher,
    Feedback,
    Furnisher,
    FurnisherError,
    FurnishingAttempt,
    build_message,
    furnisher_from_env,
    profile_in_words,
    requested_catalog,
)
from floorplan_guardrails.furnisher_prompt import INSTRUCTIONS
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
from floorplan_guardrails.inspection import inspect
from floorplan_guardrails.schema import FloorPlan

REPO_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "p2"
CATALOG = load_catalog(FIXTURES / "catalog.yaml")

ENV_NAMES = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
)


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def plan() -> FloorPlan:
    return FloorPlan.model_validate(read("plan.json"))


def request(notes: str = "") -> FurnishingRequest:
    return FurnishingRequest(items=read("request.json")["items"], notes=notes)


def proposal() -> FurnishingProposal:
    return FurnishingProposal.model_validate(read("proposal.json"))


def numbers_in(data: object) -> list[float]:
    """Todos os números de uma estrutura lida do YAML, em qualquer nível."""
    if isinstance(data, bool):
        return []
    if isinstance(data, int | float):
        return [float(data)]
    if isinstance(data, dict):
        return [n for value in data.values() for n in numbers_in(value)]
    if isinstance(data, list):
        return [n for value in data for n in numbers_in(value)]
    return []


def renderings(value: float) -> set[str]:
    """Os jeitos plausíveis de um número aparecer escrito num texto."""
    written = {f"{value:g}", f"{value:.1f}", f"{value:.2f}"}
    return written | {text.replace(".", ",") for text in written}


def section(message: str, title: str) -> str:
    """O bloco da mensagem que começa pelo título dado."""
    return next(part for part in message.split("\n\n") if part.startswith(title))


# --- o princípio 2, estendido ------------------------------------------------


def test_instructions_carry_no_circulation_value() -> None:
    """O mobiliador não pode saber os números que o fiscal cobra.

    Se um valor de `config/furniture_rules.yaml` vazasse para as instruções,
    a primeira proposta já respeitaria as faixas e a oficina perderia a
    reprovação. O teste lê o arquivo real e varre todos os números dele, em
    qualquer nível, e não só os campos que existem hoje.
    """
    raw = yaml.safe_load(
        (REPO_ROOT / DEFAULT_FURNITURE_RULES_PATH).read_text(encoding="utf-8")
    )
    values = sorted(set(numbers_in(raw)))

    # O arquivo real tem ao menos a porta, uma faixa por item e o giro; um
    # arquivo lido vazio faria o teste passar sem conferir nada.
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


def test_the_real_parameters_file_is_the_one_scanned() -> None:
    """O arquivo varrido acima é o mesmo que a verificação carrega."""
    catalog = load_catalog(REPO_ROOT / "config" / "furniture_catalog.yaml")
    rules = load_furniture_rules(catalog, REPO_ROOT / DEFAULT_FURNITURE_RULES_PATH)
    raw = yaml.safe_load(
        (REPO_ROOT / DEFAULT_FURNITURE_RULES_PATH).read_text(encoding="utf-8")
    )

    scanned = set(numbers_in(raw))

    assert rules.door_clearance_depth.value in scanned
    assert rules.turning_diameter.value in scanned
    assert set(rules.use_zone_depth.values.values()) <= scanned


def test_instructions_say_how_to_answer_a_report() -> None:
    assert "proposta INTEIRA corrigida" in INSTRUCTIONS


def test_instructions_state_the_rotation_convention() -> None:
    """A frase da seção 4.1, que o modelo precisa para posicionar."""
    assert (
        "A frente aponta para o sul em 0, para o leste em 90, para o norte em "
        "180 e para o oeste em 270." in INSTRUCTIONS
    )
    assert "canto inferior esquerdo do retângulo" in INSTRUCTIONS


# --- o perfil ------------------------------------------------------------------


@pytest.mark.parametrize("accessible", [True, False])
def test_the_profile_is_words_without_numbers(accessible: bool) -> None:
    sentence = profile_in_words(Profile(accessible=accessible))

    assert sentence
    assert not any(character.isdigit() for character in sentence)


def test_the_two_profiles_read_differently() -> None:
    assert profile_in_words(Profile(accessible=True)) == (
        "O morador usa cadeira de rodas."
    )
    assert profile_in_words(Profile(accessible=False)) != profile_in_words(
        Profile(accessible=True)
    )


# --- a mensagem da rodada 1 -----------------------------------------------------


def test_the_catalog_is_filtered_to_the_requested_items() -> None:
    filtered = requested_catalog(CATALOG, request())

    # O pedido tem sofá, cama de casal, mesa de cabeceira, guarda-roupa e
    # vaso; a cama de solteiro está no catálogo e não foi pedida.
    assert list(filtered) == ["sofa", "bed_double", "nightstand", "wardrobe", "toilet"]
    assert "bed_single" in CATALOG


def test_a_repeated_item_appears_once_in_the_catalog() -> None:
    doubled = FurnishingRequest(items={"r3": ["nightstand", "nightstand"]})

    assert list(requested_catalog(CATALOG, doubled)) == ["nightstand"]


def test_an_unknown_item_stays_in_the_request_but_not_in_the_catalog() -> None:
    odd = FurnishingRequest(items={"r1": ["sofa", "piano"]})

    message = build_message(plan(), CATALOG, odd, Profile())

    assert '"piano"' not in section(message, "Catálogo")
    assert '"piano"' in section(message, "Pedido")


def test_the_first_message_carries_plan_catalog_request_and_profile() -> None:
    message = build_message(plan(), CATALOG, request(), Profile(accessible=True))

    assert json.loads(section(message, "Planta").split("\n", 1)[1]) == (
        plan().model_dump()
    )

    catalog = json.loads(section(message, "Catálogo").split("\n", 1)[1])
    assert list(catalog) == ["sofa", "bed_double", "nightstand", "wardrobe", "toilet"]
    assert catalog["wardrobe"] == {
        "name": "Guarda-roupa",
        "width": 1.6,
        "depth": 0.6,
        "use_sides": ["front"],
        "use_mode": "all",
        "room_types": ["bedroom"],
    }

    assert json.loads(section(message, "Pedido").split("\n", 1)[1]) == (request().items)
    assert section(message, "Perfil") == (
        "Perfil do morador:\nO morador usa cadeira de rodas."
    )


def test_the_first_message_has_no_previous_proposal_nor_report() -> None:
    message = build_message(plan(), CATALOG, request(), Profile())

    assert "Proposta que você fez antes" not in message
    assert "Parecer" not in message
    assert "Devolva a proposta inteira corrigida." not in message


def test_the_catalog_source_does_not_go_to_the_model() -> None:
    """A fonte é para o aluno conferir, não para o modelo posicionar."""
    message = build_message(plan(), CATALOG, request(), Profile())

    assert "Catálogo de teste." not in message
    assert "blocks_use_zones" not in message


def test_the_notes_go_only_when_there_are_notes() -> None:
    with_notes = build_message(
        plan(), CATALOG, request("Home office no quarto."), Profile()
    )
    without = build_message(plan(), CATALOG, request("   "), Profile())

    assert "Observações do pedido:\nHome office no quarto." in with_notes
    assert "Observações" not in without


# --- a mensagem da rodada 2 -----------------------------------------------------


def test_a_later_message_carries_the_previous_proposal_and_the_report() -> None:
    feedback = [
        Feedback(
            message="A cama bloqueia a porta do quarto.",
            suggestion="Mova a cama 0,40 m para o norte.",
        ),
        Feedback(message="O sofá passa da parede.", suggestion=""),
    ]

    message = build_message(plan(), CATALOG, request(), Profile(), proposal(), feedback)

    previous = section(message, "Proposta que você fez antes").split("\n", 1)[1]
    assert json.loads(previous) == proposal().model_dump()

    report = section(message, "O verificador reprovou")
    assert report.splitlines()[1:] == [
        "- A cama bloqueia a porta do quarto.",
        "  Sugestão: Mova a cama 0,40 m para o norte.",
        "- O sofá passa da parede.",
    ]
    assert message.endswith("Devolva a proposta inteira corrigida.")


def test_the_later_message_still_carries_the_first_round_context() -> None:
    """O modelo não guarda memória entre rodadas: tudo vai de novo."""
    first = build_message(plan(), CATALOG, request(), Profile())
    later = build_message(
        plan(),
        CATALOG,
        request(),
        Profile(),
        proposal(),
        [Feedback(message="Problema.", suggestion="")],
    )

    assert later.startswith(first)


def test_the_inspection_messages_reach_the_furnisher_word_for_word() -> None:
    """A mensagem da verificação vai literal: é por ela que o modelo aprende."""
    data = read("proposal.json")
    data["placements"][1]["y"] = 3.35  # a cama encosta na porta do quarto
    cramped = FurnishingProposal.model_validate(data)
    rules = load_furniture_rules(CATALOG, FIXTURES / "furniture_rules.yaml")
    violations = inspect(
        FurnishedPlan(plan=plan(), proposal=cramped),
        CATALOG,
        rules,
        Profile(),
        request(),
    )
    assert violations

    message = build_message(
        plan(),
        CATALOG,
        request(),
        Profile(),
        cramped,
        [Feedback(message=v.message, suggestion="") for v in violations],
    )

    for violation in violations:
        assert f"- {violation.message}" in message


# --- o contrato ----------------------------------------------------------------


class ScriptedFurnisher:
    """Mobiliador de mentira: entrega as propostas combinadas, em ordem.

    Monta a mesma mensagem que o mobiliador de verdade montaria e a guarda,
    que é como os testes da negociação vão provar que o parecer chega.
    """

    def __init__(self, proposals: Sequence[FurnishingProposal]) -> None:
        self.proposals = list(proposals)
        self.messages: list[str] = []

    async def propose(
        self,
        plan: FloorPlan,
        catalog: Catalog,
        request: FurnishingRequest,
        profile: Profile,
        previous: FurnishingProposal | None = None,
        feedback: Sequence[Feedback] = (),
    ) -> FurnishingAttempt:
        self.messages.append(
            build_message(plan, catalog, request, profile, previous, feedback)
        )
        chosen = self.proposals[min(len(self.messages), len(self.proposals)) - 1]

        return FurnishingAttempt(
            proposal=chosen, input_tokens=10, output_tokens=20, latency_ms=30
        )


async def two_rounds(furnisher: Furnisher) -> list[FurnishingAttempt]:
    """Duas rodadas pelo contrato, sem saber qual mobiliador está do outro lado."""
    first = await furnisher.propose(plan(), CATALOG, request(), Profile())
    second = await furnisher.propose(
        plan(),
        CATALOG,
        request(),
        Profile(),
        first.proposal,
        [Feedback(message="A cama bloqueia a porta.", suggestion="Mova a cama.")],
    )
    return [first, second]


def test_a_scripted_furnisher_runs_through_the_protocol() -> None:
    corrected = proposal().model_copy(update={"design_notes": "Corrigida."})
    scripted = ScriptedFurnisher([proposal(), corrected])

    attempts = asyncio.run(two_rounds(scripted))

    assert [a.proposal.design_notes for a in attempts] == [
        proposal().design_notes,
        "Corrigida.",
    ]
    assert "Parecer" not in scripted.messages[0]
    second = scripted.messages[1]
    assert "- A cama bloqueia a porta.\n  Sugestão: Mova a cama." in second


def test_the_azure_furnisher_has_the_protocol_signature() -> None:
    """O mobiliador de verdade e o roteirizado são trocáveis na negociação."""
    expected = signatures.signature(Furnisher.propose)

    assert signatures.signature(AzureFurnisher.propose) == expected
    assert signatures.signature(ScriptedFurnisher.propose) == expected


# --- configuração ----------------------------------------------------------------


def set_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://recurso.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "chave-de-mentira")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "um-deployment")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)


def test_missing_environment_variables_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(FurnisherError) as caught:
        furnisher_from_env()

    for name in ENV_NAMES:
        assert name in str(caught.value)


def test_an_api_version_in_the_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

    with pytest.raises(FurnisherError, match="Apague a variável"):
        furnisher_from_env()


def test_the_furnisher_is_built_from_the_environment_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Montar o agente não fala com a rede: a chamada só acontece em propose."""
    set_environment(monkeypatch)

    furnisher = furnisher_from_env()

    assert furnisher.deployment == "um-deployment"
    assert furnisher.reasoning_effort == "low"
    options = furnisher._agent.default_options
    assert options["response_format"] is FurnishingProposal
    assert options["reasoning"] == {"effort": "low"}
    assert options["instructions"] == INSTRUCTIONS


def test_the_reasoning_effort_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_environment(monkeypatch)

    furnisher = furnisher_from_env(reasoning_effort="medium")

    assert furnisher._agent.default_options["reasoning"] == {"effort": "medium"}


def test_the_endpoint_goes_through_the_p1_normalization() -> None:
    """A mesma regra do P1: qualquer URL do portal vira a base v1."""
    furnisher = AzureFurnisher(
        "https://recurso.openai.azure.com/openai/deployments/x?api-version=1",
        "chave-de-mentira",
        "um-deployment",
    )

    assert str(furnisher._agent.client.base_url).startswith(
        "https://recurso.openai.azure.com/openai/v1"
    )


def test_an_unrecognized_endpoint_is_a_furnisher_error() -> None:
    with pytest.raises(FurnisherError, match="Ponto de extremidade"):
        AzureFurnisher("https://exemplo.com/", "chave-de-mentira", "um-deployment")
