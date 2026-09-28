"""As contas da rodada de medição do P2.

Funções puras sobre negociações já gravadas: cada execução é a lista de
linhas do seu JSONL em `runs/p2/`, mais o que o registro não guarda (qual
planta de exemplo, qual perfil, qual repetição). Nada aqui chama o modelo,
então o relatório pode ser refeito a partir dos registros quantas vezes for
preciso.

Sugestão adotada
----------------
O fiscal escreve a sugestão como posição e rotação finais ("m4 em x = 2.40,
y = 3.00, rotação 90"). Uma sugestão é **adotada** quando a proposta da
rodada seguinte põe aquele móvel exatamente ali, com aquela rotação. Uma
adotada **causou violação** quando o móvel movido aparece citado numa
violação da rodada seguinte. Sugestão sem posição escrita, ou dada na última
rodada, não entra na conta de adoção.

O giro de cadeira de rodas não cita móvel (o problema é o cômodo inteiro),
então uma sugestão que tira o giro do cômodo não aparece como causadora.
"""

import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from floorplan_guardrails.validator import number

#: Folga para dizer que a proposta seguiu a posição sugerida, em metros.
POSITION_TOLERANCE = 0.005

#: Os estados com que uma negociação termina, na ordem do relatório.
FINAL_STATES = ("approved", "declined", "not_converged", "infeasible")

#: "m4 em x = 2.40, y = 3.00, rotação 90", e a variante "m4 para x = ...".
TARGET = re.compile(
    r"\b(m\d+)\s+(?:em|para)\s+x\s*=\s*(-?\d+(?:[.,]\d+)?)\s*,\s*"
    r"y\s*=\s*(-?\d+(?:[.,]\d+)?)\s*,?\s*(?:e\s+)?rota[çc][ãa]o\s*(?:de\s+)?(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Execution:
    """Uma negociação da rodada de medição."""

    plan: str
    profile: str
    repetition: int
    run_id: str
    lines: list[dict]

    @property
    def status(self) -> str:
        return self.lines[-1]["status"]


@dataclass(frozen=True)
class Target:
    """Uma posição final sugerida pelo fiscal."""

    placement_id: str
    x: float
    y: float
    rotation: int


@dataclass(frozen=True)
class Suggestion:
    """Uma sugestão com posição escrita, e o que a rodada seguinte fez com ela."""

    run_id: str
    round: int
    target: Target
    adopted: bool
    cited_next: bool
    new_rule_next: bool


def decimal(text: str) -> float:
    return float(text.replace(",", "."))


def targets_in(text: str) -> list[Target]:
    """As posições finais escritas numa sugestão, sem repetição."""
    found: list[Target] = []

    for placement_id, x, y, rotation in TARGET.findall(text):
        target = Target(placement_id, decimal(x), decimal(y), int(rotation))
        if target not in found:
            found.append(target)

    return found


def round_latency_ms(line: dict) -> int:
    """A espera de uma rodada: o mobiliador e, se foi chamado, o fiscal."""
    return line["furnisher"]["latency_ms"] + line["inspector"]["latency_ms"]


def total_latency_ms(execution: Execution) -> int:
    return sum(round_latency_ms(line) for line in execution.lines)


def inspector_called(line: dict) -> bool:
    """O fiscal com modelo só é chamado quando há violação de circulação."""
    return line["inspector"]["latency_ms"] > 0


def rules_cited(line: dict, placement_id: str) -> set[str]:
    """As regras que, naquela rodada, citam o móvel."""
    return {
        violation["rule_id"]
        for violation in line["violations"]
        if placement_id in violation.get("placement_ids", [])
    }


def adopted(target: Target, line: dict) -> bool:
    """Se a proposta daquela rodada pôs o móvel na posição sugerida."""
    for placement in line["proposal"]["placements"]:
        if placement["id"] != target.placement_id:
            continue
        return (
            abs(placement["x"] - target.x) <= POSITION_TOLERANCE
            and abs(placement["y"] - target.y) <= POSITION_TOLERANCE
            and placement["rotation"] == target.rotation
        )

    return False


def suggestions(execution: Execution) -> list[Suggestion]:
    """As sugestões com posição escrita que ainda tiveram rodada seguinte."""
    found = []

    for current, following in pairwise(execution.lines):
        report = current["report"]
        if report is None:
            continue

        targets: list[Target] = []
        for item in report["feedback"]:
            for target in targets_in(item["suggestion"]):
                if target not in targets:
                    targets.append(target)

        for target in targets:
            was = adopted(target, following)
            before = rules_cited(current, target.placement_id)
            after = rules_cited(following, target.placement_id)
            found.append(
                Suggestion(
                    run_id=execution.run_id,
                    round=current["round"],
                    target=target,
                    adopted=was,
                    cited_next=was and bool(after),
                    new_rule_next=was and bool(after - before),
                )
            )

    return found


def unstructured_suggestions(execution: Execution) -> int:
    """Sugestões não vazias em que nenhuma posição final foi escrita."""
    return sum(
        1
        for line in execution.lines
        if line["report"] is not None
        for item in line["report"]["feedback"]
        if item["suggestion"].strip() and not targets_in(item["suggestion"])
    )


# --- números agregados ----------------------------------------------------------


def p90(values: Sequence[float]) -> float:
    """O percentil 90 pelo posto mais próximo: um valor que de fato ocorreu."""
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def seconds(values_ms: Sequence[int], statistic: Callable) -> str:
    if not values_ms:
        return "—"
    return f"{number(statistic(values_ms) / 1000, 1)} s"


def median(values: Sequence[float]) -> float:
    return statistics.median(values)


def percentage(part: int, whole: int) -> str:
    if whole == 0:
        return "—"
    return f"{part}/{whole} ({number(100 * part / whole, 0)}%)"


def profile_label(profile: dict) -> str:
    return "acessível" if profile.get("accessible") else "padrão"


def violation_counts(execution: Execution) -> str:
    """As violações de cada rodada, por regra: "3 (door_clearance ×2, use_zone)"."""
    parts = []

    for line in execution.lines:
        if line["status"] == "declined":
            parts.append("desistiu")
            continue
        if line["status"] == "infeasible":
            parts.append("inviável")
            continue

        rules = Counter(violation["rule_id"] for violation in line["violations"])
        if not rules:
            parts.append("0")
            continue

        named = ", ".join(
            rule if count == 1 else f"{rule} ×{count}"
            for rule, count in sorted(rules.items())
        )
        parts.append(f"{sum(rules.values())} ({named})")

    return " → ".join(parts)


def execution_table(executions: Sequence[Execution]) -> list[str]:
    lines = [
        "| # | planta | perfil | rep. | estado final | violações por rodada | "
        "latência total | ferramenta | trocas | ids inválidos | run_id |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for position, execution in enumerate(executions, 1):
        reports = [line["report"] for line in execution.lines if line["report"]]
        tools = sum(report["tool_calls"] for report in reports)
        swaps = sum(len(report.get("replacements", [])) for report in reports)
        invalid = sum(len(report.get("invalid_params", [])) for report in reports)
        lines.append(
            f"| {position} | {execution.plan} | {execution.profile} | "
            f"{execution.repetition} | {execution.status} | "
            f"{violation_counts(execution)} | "
            f"{seconds([total_latency_ms(execution)], median)} | {tools} | "
            f"{swaps} | {invalid} | `{execution.run_id}` |"
        )

    return lines


def latency_table(executions: Sequence[Execution]) -> list[str]:
    """Rodada a rodada e por agente, em milissegundos gravados."""
    rows = [line for execution in executions for line in execution.lines]
    by_round: dict[int, list[int]] = {}
    for line in rows:
        by_round.setdefault(line["round"], []).append(round_latency_ms(line))

    furnisher = [line["furnisher"]["latency_ms"] for line in rows]
    inspector = [
        line["inspector"]["latency_ms"] for line in rows if inspector_called(line)
    ]
    everything = [round_latency_ms(line) for line in rows]
    totals = [total_latency_ms(execution) for execution in executions]

    def row(label: str, values: Sequence[int]) -> str:
        return (
            f"| {label} | {len(values)} | {seconds(values, median)} | "
            f"{seconds(values, p90)} |"
        )

    lines = ["| Medida | n | mediana | p90 |", "|---|---|---|---|"]
    lines.append(row("Negociação inteira", totals))
    lines.append(row("Rodada, qualquer uma", everything))
    for round_number in sorted(by_round):
        lines.append(row(f"Rodada {round_number}", by_round[round_number]))
    lines.append(row("Mobiliador, por chamada", furnisher))
    lines.append(row("Fiscal, por chamada", inspector))

    return lines


def token_table(executions: Sequence[Execution]) -> list[str]:
    rows = [line for execution in executions for line in execution.lines]
    called = [line for line in rows if inspector_called(line)]

    def middle(values: Iterable[int]) -> str:
        values = list(values)
        return number(median(values), 0) if values else "—"

    return [
        "| Agente | chamadas | entrada (mediana) | saída (mediana) |",
        "|---|---|---|---|",
        f"| Mobiliador | {len(rows)} | "
        f"{middle(line['furnisher']['input_tokens'] for line in rows)} | "
        f"{middle(line['furnisher']['output_tokens'] for line in rows)} |",
        f"| Fiscal | {len(called)} | "
        f"{middle(line['inspector']['input_tokens'] for line in called)} | "
        f"{middle(line['inspector']['output_tokens'] for line in called)} |",
    ]


def rate_table(
    executions: Sequence[Execution], key: Callable[[Execution], str], title: str
) -> list[str]:
    lines = [
        f"| {title} | n | approved | declined | not_converged | infeasible |",
        "|---|---|---|---|---|---|",
    ]
    groups: dict[str, list[Execution]] = {}
    for execution in executions:
        groups.setdefault(key(execution), []).append(execution)
    groups["total"] = list(executions)

    for name, group in groups.items():
        states = Counter(execution.status for execution in group)
        cells = " | ".join(
            percentage(states[state], len(group)) for state in FINAL_STATES
        )
        lines.append(f"| {name} | {len(group)} | {cells} |")

    return lines


def suggestion_table(executions: Sequence[Execution]) -> list[str]:
    found = [item for execution in executions for item in suggestions(execution)]
    taken = [item for item in found if item.adopted]
    cited = [item for item in taken if item.cited_next]
    new = [item for item in taken if item.new_rule_next]
    loose = sum(unstructured_suggestions(execution) for execution in executions)

    lines = [
        "| Medida | Valor |",
        "|---|---|",
        f"| Sugestões com posição escrita e rodada seguinte | {len(found)} |",
        f"| Adotadas ao pé da letra | {percentage(len(taken), len(found))} |",
        "| Adotadas cujo móvel foi citado na rodada seguinte | "
        f"{percentage(len(cited), len(taken))} |",
        "| ...por uma regra que não o citava antes | "
        f"{percentage(len(new), len(taken))} |",
        f"| Sugestões sem posição escrita (fora da conta) | {loose} |",
    ]

    if cited:
        lines.extend(["", "Adotadas e citadas na rodada seguinte:", ""])
        for item in cited:
            target = item.target
            lines.append(
                f"- `{item.run_id}`, rodada {item.round}: {target.placement_id} em "
                f"x = {target.x:.2f}, y = {target.y:.2f}, rotação {target.rotation}"
                + (" (regra nova)" if item.new_rule_next else " (mesma regra)")
            )

    return lines


def build_report(
    executions: Sequence[Execution],
    *,
    deployment: str,
    max_rounds: int,
    reasoning_effort: str,
    failures: Sequence[tuple[str, str, int, str]] = (),
) -> str:
    """Monta o relatório inteiro em Markdown."""
    if not executions:
        raise ValueError("não há execuções para relatar")

    header = [
        "# Medição do P2",
        "",
        f"- Data: {datetime.now(UTC).strftime('%d/%m/%Y %H:%M UTC')}",
        f"- Deployment: `{deployment}`",
        f"- Esforço de raciocínio: `{reasoning_effort}`",
        f"- Limite de rodadas: {max_rounds}",
        "- Configuração: `config/rules.yaml`, `config/furniture_catalog.yaml`, "
        "`config/furniture_rules.yaml`",
        "- Plantas e pedidos: `examples/p2/`",
        "",
        "Uma rodada só não permite concluir nada: o ruído entre repetições foi "
        "o que mais pesou na avaliação do P1.",
        "",
    ]

    sections = [
        ("## Execução a execução", execution_table(executions)),
        (
            "## Estado final por planta",
            rate_table(executions, lambda e: e.plan, "Planta"),
        ),
        (
            "## Estado final por perfil",
            rate_table(executions, lambda e: e.profile, "Perfil"),
        ),
        ("## Latência", latency_table(executions)),
        ("## Tokens", token_table(executions)),
        ("## Sugestões do fiscal", suggestion_table(executions)),
    ]

    lines = list(header)
    for title, table in sections:
        lines.extend([title, "", *table, ""])

    if failures:
        lines.extend(["## Falhas", ""])
        lines.extend(
            f"- {plan}, {profile}, repetição {repetition}: {reason}"
            for plan, profile, repetition, reason in failures
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
