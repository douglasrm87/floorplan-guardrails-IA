"""Roda a medição do P2 e escreve o relatório.

    uv run --env-file .env python -m eval.run_p2
    uv run --env-file .env python -m eval.run_p2 --repetitions 1
    uv run python -m eval.run_p2 --report eval/results/p2-20260928T120000Z

São as plantas de `examples/p2/plans/`, cada uma com o seu pedido de
`examples/p2/requests/`, nos dois perfis (padrão e acessível), repetidas. Com
4 plantas e 2 repetições, são 16 negociações, com o fiscal com modelo.

Roda em série, uma negociação de cada vez, pelo mesmo motivo do `eval.run`
do P1: disparar tudo junto contra um deployment de conta de estudante é o
caminho mais curto para tomar throttling. A ordem intercala plantas e perfis
dentro de cada repetição, para que uma lentidão passageira do deployment não
caia inteira sobre uma planta só.

Os registros vão para `runs/p2/`, um JSONL por negociação, como no notebook.
O manifesto (qual planta, perfil e repetição cada registro é) e o relatório
vão para `eval/results/p2-<data>/`. Com `--report`, o relatório é refeito a
partir de um manifesto já gravado, sem chamar o modelo.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from eval.report_p2 import Execution, build_report
from floorplan_guardrails.furnisher import (
    DEFAULT_REASONING_EFFORT,
    FurnisherError,
    furnisher_from_env,
)
from floorplan_guardrails.furniture import FurnishingRequest, Profile, load_catalog
from floorplan_guardrails.furniture_rules import load_furniture_rules
from floorplan_guardrails.inspector import InspectorError, inspector_from_env
from floorplan_guardrails.negotiation import DEFAULT_MAX_ROUNDS, negotiate
from floorplan_guardrails.negotiation_log import NegotiationLog
from floorplan_guardrails.rules import load_rules
from floorplan_guardrails.runlog import read_run
from floorplan_guardrails.schema import FloorPlan

RAIZ = Path(__file__).resolve().parents[1]
EXAMPLES = RAIZ / "examples" / "p2"
CONFIG = RAIZ / "config"
RUNS = RAIZ / "runs" / "p2"
RESULTS = RAIZ / "eval" / "results"

#: Os dois perfis medidos, com o nome que aparece no relatório.
PROFILES = {"padrão": Profile(accessible=False), "acessível": Profile(accessible=True)}


def load_examples(
    folder: Path = EXAMPLES,
) -> list[tuple[str, FloorPlan, FurnishingRequest]]:
    """Cada planta de exemplo com o pedido de mesmo nome."""
    examples = []

    for path in sorted((folder / "plans").glob("*.json")):
        plan = FloorPlan.model_validate_json(path.read_text(encoding="utf-8"))
        request_path = folder / "requests" / f"{path.stem}.yaml"
        request = FurnishingRequest.model_validate(
            yaml.safe_load(request_path.read_text(encoding="utf-8"))
        )
        examples.append((path.stem, plan, request))

    return examples


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument(
        "--report",
        type=Path,
        help="refaz o relatório a partir do manifesto desta pasta, sem chamar o modelo",
    )

    return parser.parse_args(argv)


def write_report(destination: Path, manifest: dict) -> Path:
    """Lê os registros do manifesto e escreve `relatorio.md` na pasta."""
    executions = [
        Execution(
            plan=item["plan"],
            profile=item["profile"],
            repetition=item["repetition"],
            run_id=item["run_id"],
            lines=read_run(RUNS / f"{item['run_id']}.jsonl"),
        )
        for item in manifest["executions"]
        if item["error"] is None
    ]
    failures = [
        (item["plan"], item["profile"], item["repetition"], item["error"])
        for item in manifest["executions"]
        if item["error"] is not None
    ]

    report = build_report(
        executions,
        deployment=manifest["deployment"],
        max_rounds=manifest["max_rounds"],
        reasoning_effort=manifest["reasoning_effort"],
        failures=failures,
    )
    path = destination / "relatorio.md"
    path.write_text(report, encoding="utf-8")

    return path


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.report is not None:
        manifest = json.loads(
            (args.report / "manifest.json").read_text(encoding="utf-8")
        )
        print(f"relatório: {write_report(args.report, manifest)}")
        return 0

    catalog = load_catalog(CONFIG / "furniture_catalog.yaml")
    rules = load_furniture_rules(catalog, CONFIG / "furniture_rules.yaml")
    plan_rules = load_rules(CONFIG / "rules.yaml")

    try:
        furnisher = furnisher_from_env(reasoning_effort=args.reasoning_effort)
        inspector = inspector_from_env(rules, catalog, args.reasoning_effort)
    except (FurnisherError, InspectorError) as erro:
        print(erro, file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = RESULTS / f"p2-{stamp}"
    destination.mkdir(parents=True, exist_ok=True)

    manifest = {
        "deployment": furnisher.deployment,
        "max_rounds": args.max_rounds,
        "reasoning_effort": args.reasoning_effort,
        "executions": [],
    }

    examples = load_examples()
    schedule = [
        (repetition, name, plan, request, label, profile)
        for repetition in range(1, args.repetitions + 1)
        for name, plan, request in examples
        for label, profile in PROFILES.items()
    ]

    for position, (repetition, name, plan, request, label, profile) in enumerate(
        schedule, 1
    ):
        print(
            f"[{position}/{len(schedule)}] {name}, {label}, rep. {repetition} ...",
            flush=True,
        )
        log = NegotiationLog.create(RUNS)
        error = None

        try:
            negotiation = await negotiate(
                plan,
                request,
                profile,
                furnisher,
                inspector,
                catalog,
                rules,
                max_rounds=args.max_rounds,
                log=log,
                deployment=furnisher.deployment,
                plan_rules=plan_rules,
            )
        except (FurnisherError, InspectorError) as erro:
            # Uma negociação que estoura não pode derrubar a rodada inteira.
            error = str(erro).splitlines()[0]
            print(f"    falhou: {error}", file=sys.stderr)
        else:
            print(
                f"    {negotiation.status} em {len(negotiation.rounds)} rodadas, "
                f"{negotiation.latency_ms / 1000:.1f} s",
                flush=True,
            )

        manifest["executions"].append(
            {
                "plan": name,
                "profile": label,
                "repetition": repetition,
                "run_id": log.run_id,
                "error": error,
            }
        )
        # Gravado a cada negociação: se a rodada cair no meio, o que já
        # rodou continua aproveitável com --report.
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if all(item["error"] is not None for item in manifest["executions"]):
        print("nenhuma negociação concluiu", file=sys.stderr)
        return 1

    print(f"\nrelatório: {write_report(destination, manifest)}")
    print(f"registros: {RUNS}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
