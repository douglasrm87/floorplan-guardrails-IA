"""Registro das negociações, em JSONL, e o modo replay.

Um arquivo por execução, uma linha por rodada, em `runs/p2/`, que o git
ignora. O nome do arquivo segue o padrão do P1 (`new_run_id`), e a leitura
das linhas é a mesma (`read_run`).

Cada linha é um JSON completo com a rodada inteira: a proposta, as
violações, o parecer, os tokens e a latência de cada agente e o estado. A
planta de entrada só vai na primeira linha, porque é a mesma em todas.

O modo replay
-------------
`load_negotiation` reconstrói a `Negotiation` a partir do arquivo, sem rede.
É o plano de contingência da oficina: se a API cair ou ficar lenta, o
notebook desenha uma execução gravada, com o mesmo `draw_negotiation`.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from floorplan_guardrails.furnisher import Feedback
from floorplan_guardrails.furniture import (
    FurnishingProposal,
    FurnishingRequest,
    Profile,
)
from floorplan_guardrails.inspection import FurnitureViolation
from floorplan_guardrails.inspector import Replacement, Review
from floorplan_guardrails.negotiation import Negotiation, Round
from floorplan_guardrails.runlog import new_run_id, read_run
from floorplan_guardrails.schema import FloorPlan

#: Onde os registros do P2 ficam, a partir da raiz do repositório.
DEFAULT_RUNS_DIR = Path("runs/p2")


@dataclass(frozen=True)
class NegotiationLog:
    """O arquivo JSONL de uma negociação."""

    run_id: str
    path: Path

    @classmethod
    def create(
        cls,
        directory: Path | str = DEFAULT_RUNS_DIR,
        run_id: str | None = None,
    ) -> "NegotiationLog":
        """Abre um registro novo, criando a pasta se for preciso."""
        run_id = run_id or new_run_id()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        return cls(run_id=run_id, path=directory / f"{run_id}.jsonl")

    def record(
        self,
        *,
        number: int,
        result: Round,
        plan: FloorPlan,
        request: FurnishingRequest,
        profile: Profile,
        deployment: str,
    ) -> dict:
        """Acrescenta a linha de uma rodada e devolve o que foi gravado."""
        line: dict = {
            "run_id": self.run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "round": number,
        }

        if number == 1:
            line["plan"] = plan.model_dump()

        line |= {
            "request": request.model_dump(),
            "profile": profile.model_dump(),
            "deployment": deployment,
            "proposal": result.proposal.model_dump(),
            "violations": [violation.model_dump() for violation in result.violations],
            "report": report(result.review),
            "furnisher": {
                "input_tokens": result.furnisher_input_tokens,
                "output_tokens": result.furnisher_output_tokens,
                "latency_ms": result.furnisher_latency_ms,
            },
            "inspector": {
                "input_tokens": result.inspector_input_tokens,
                "output_tokens": result.inspector_output_tokens,
                "latency_ms": result.inspector_latency_ms,
            },
            "status": result.status,
        }

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

        return line


def report(review: Review | None) -> dict | None:
    """O parecer como vai para a linha.

    Tokens e latência do fiscal ficam fora daqui, no bloco `inspector`, ao
    lado dos do mobiliador, para que as duas medidas se leiam do mesmo jeito.
    """
    if review is None:
        return None

    return {
        "feedback": [
            {"message": item.message, "suggestion": item.suggestion}
            for item in review.feedback
        ],
        "summary": review.summary,
        "tool_calls": review.tool_calls,
        "invalid_params": review.invalid_params,
        "replacements": [asdict(item) for item in review.replacements],
        "duplicate_refs": review.duplicate_refs,
        "missing_refs": review.missing_refs,
    }


def review_from(line: dict) -> Review | None:
    """O parecer de uma linha gravada, de volta como `Review`.

    Os registros do que o código corrigiu no texto do fiscal entraram depois
    das primeiras gravações; numa linha antiga, ficam vazios.
    """
    data = line["report"]
    if data is None:
        return None

    return Review(
        feedback=[Feedback(**item) for item in data["feedback"]],
        summary=data["summary"],
        input_tokens=line["inspector"]["input_tokens"],
        output_tokens=line["inspector"]["output_tokens"],
        latency_ms=line["inspector"]["latency_ms"],
        tool_calls=data["tool_calls"],
        invalid_params=data.get("invalid_params", []),
        replacements=[Replacement(**item) for item in data.get("replacements", [])],
        duplicate_refs=data.get("duplicate_refs", []),
        missing_refs=data.get("missing_refs", []),
    )


def round_from(line: dict) -> Round:
    """Uma linha gravada, de volta como `Round`."""
    return Round(
        proposal=FurnishingProposal.model_validate(line["proposal"]),
        violations=[
            FurnitureViolation.model_validate(violation)
            for violation in line["violations"]
        ],
        review=review_from(line),
        status=line["status"],
        furnisher_input_tokens=line["furnisher"]["input_tokens"],
        furnisher_output_tokens=line["furnisher"]["output_tokens"],
        furnisher_latency_ms=line["furnisher"]["latency_ms"],
    )


def load_negotiation(path: Path | str) -> Negotiation:
    """Reconstrói uma negociação gravada, sem chamar o modelo.

    A planta vem da primeira linha; o pedido, o perfil e o deployment também,
    porque não mudam de uma rodada para outra. O estado final é o da última.
    """
    lines = read_run(path)

    if not lines:
        raise ValueError(f"O registro {path} está vazio: não há rodada para ler.")

    first = lines[0]
    if "plan" not in first:
        raise ValueError(
            f"A primeira linha do registro {path} não traz a planta de entrada. "
            "O arquivo pode ter sido cortado no começo."
        )

    return Negotiation(
        run_id=first["run_id"],
        plan=FloorPlan.model_validate(first["plan"]),
        request=FurnishingRequest.model_validate(first["request"]),
        profile=Profile.model_validate(first["profile"]),
        deployment=first["deployment"],
        status=lines[-1]["status"],
        rounds=[round_from(line) for line in lines],
    )
