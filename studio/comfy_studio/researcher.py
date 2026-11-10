"""Investigador autónomo y aprendizaje selectivo (plan Ultra, W23 / A26).

W23 pide un investigador que formule hipótesis medibles, compare recetas con
holdouts/controles, acumule una frontera Pareto y solo promueva cambios con
evidencia. El invariante (A26): hipótesis/intervención/métrica/criterio/budget
fijados ANTES de resultados; Pareto por resultado aceptado; una propuesta LoRA
incluye datos/licencias/holdout/rollback y el training NO se ejecuta ahora; no
promoción desde mocks ni reset de budget; una hipótesis negativa termina.

Este módulo implementa el CONTRATO:

- `Hypothesis`: preregistro (intervención, métrica, criterio, budget) fijado
  antes de ver resultados. No se puede alterar el criterio tras los resultados.
- `Experiment`: una corrida con sus medidas (seeds/escenas), marca `simulated`.
- `pareto_front`: frontera de Pareto sobre resultados ACEPTADOS (no simulados),
  con identidad/factualidad como restricciones duras, no solo score.
- `can_promote`: solo promueve una propuesta (LoRA/preset) con evidencia real
  (no simulada) que cumple el criterio preregistrado; exige rollback declarado.

The SQLite store persists preregistrations and measurements transactionally. It
does not execute models or train adapters; those operations require their own
engine and hardware evidence (A26).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ResearchError(ValueError):
    """Contrato de investigación inválido."""


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_git_revision(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(slots=True)
class Hypothesis:
    """Preregistro: fijado ANTES de ver resultados (A26)."""

    hyp_id: str
    intervention: str
    metric: str          # p. ej. "vbench", "clip_sim"
    higher_is_better: bool
    criterion: float     # umbral que la métrica debe cruzar
    budget_runs: int     # nº máximo de corridas antes de concluir
    #: una vez registrada con resultados, el criterio queda CONGELADO.
    _locked: bool = False
    _locked_values: tuple[str, str, bool, float, int] | None = field(
        default=None, init=False, repr=False
    )

    def validate(self) -> None:
        if not self.hyp_id.strip() or not self.intervention.strip() or not self.metric.strip():
            raise ResearchError("hipótesis requiere id, intervención y métrica")
        if not math.isfinite(self.criterion):
            raise ResearchError("criterio debe ser un número finito")
        if self.budget_runs <= 0:
            raise ResearchError("budget_runs debe ser positivo")

    def _values(self) -> tuple[str, str, bool, float, int]:
        return (
            self.intervention,
            self.metric,
            self.higher_is_better,
            self.criterion,
            self.budget_runs,
        )

    def assert_unchanged(self) -> None:
        if self._locked and self._locked_values != self._values():
            raise ResearchError(
                f"{self.hyp_id}: el preregistro está congelado; no se cambia tras ver resultados"
            )

    def lock(self) -> None:
        self.validate()
        if self._locked_values is None:
            self._locked_values = self._values()
        self._locked = True

    def set_criterion(self, value: float) -> None:
        if self._locked:
            raise ResearchError(
                f"{self.hyp_id}: el criterio esta congelado; no se ajusta tras ver resultados"
            )
        if not math.isfinite(value):
            raise ResearchError("criterio debe ser un número finito")
        self.criterion = value
        self.validate()


@dataclass(slots=True)
class Experiment:
    """Una corrida con sus medidas; marca si es simulada (no promovible)."""

    run_id: str
    hyp_id: str
    seed: int
    scores: dict[str, float]          # métrica -> valor
    constraints_ok: bool = True        # identidad/factualidad respetadas
    cost: float = 0.0
    simulated: bool = True             # solo un caller explícito puede declarar real (A26)
    dataset_sha256s: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.run_id.strip() or not self.hyp_id.strip():
            raise ResearchError("experimento requiere run_id e hyp_id")
        if self.seed < 0:
            raise ResearchError("seed no puede ser negativo")
        if not self.scores or any(not math.isfinite(value) for value in self.scores.values()):
            raise ResearchError("scores deben contener solo medidas finitas")
        if not math.isfinite(self.cost) or self.cost < 0:
            raise ResearchError("cost debe ser finito y no negativo")
        if len(self.dataset_sha256s) != len(set(self.dataset_sha256s)):
            raise ResearchError("dataset_sha256s contiene duplicados")
        if any(not _is_sha256(digest) for digest in self.dataset_sha256s):
            raise ResearchError("dataset_sha256s requiere hashes SHA-256 válidos")

    def meets(self, hyp: Hypothesis) -> bool:
        val = self.scores.get(hyp.metric)
        if val is None:
            return False
        return val >= hyp.criterion if hyp.higher_is_better else val <= hyp.criterion


@dataclass(slots=True)
class Registry:
    """Registro de experimentos de una hipótesis, con budget acotado."""

    hypothesis: Hypothesis
    runs: list[Experiment] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.hypothesis.validate()
        seen: set[str] = set()
        for run in self.runs:
            run.validate()
            if run.hyp_id != self.hypothesis.hyp_id or run.run_id in seen:
                raise ResearchError("registro contiene experimento duplicado o de otra hipótesis")
            seen.add(run.run_id)
        if len(self.runs) > self.hypothesis.budget_runs:
            raise ResearchError("registro supera el budget preregistrado")
        if self.runs:
            self.hypothesis.lock()

    def _validate_frozen_preregistration(self) -> None:
        self.hypothesis.assert_unchanged()

    def add_run(self, exp: Experiment) -> None:
        self.hypothesis.validate()
        self._validate_frozen_preregistration()
        exp.validate()
        if exp.hyp_id != self.hypothesis.hyp_id:
            raise ResearchError("experimento de otra hipotesis")
        if any(existing.run_id == exp.run_id for existing in self.runs):
            raise ResearchError(f"run_id duplicado: {exp.run_id}")
        if len(self.runs) >= self.hypothesis.budget_runs:
            raise ResearchError(
                f"budget de corridas agotado ({self.hypothesis.budget_runs}); "
                "una hipotesis negativa termina, no busca una muestra bonita"
            )
        self.runs.append(exp)
        self.hypothesis.lock()  # al primer resultado, el criterio se congela

    def real_runs(self) -> list[Experiment]:
        return [r for r in self.runs if not r.simulated]

    def accepted(self) -> list[Experiment]:
        """Corridas reales que cumplen criterio Y respetan las restricciones."""
        self._validate_frozen_preregistration()
        return [
            r for r in self.real_runs()
            if r.constraints_ok and r.meets(self.hypothesis)
        ]


def _hypothesis_payload(hypothesis: Hypothesis) -> dict[str, Any]:
    return {
        "hyp_id": hypothesis.hyp_id,
        "intervention": hypothesis.intervention,
        "metric": hypothesis.metric,
        "higher_is_better": hypothesis.higher_is_better,
        "criterion": hypothesis.criterion,
        "budget_runs": hypothesis.budget_runs,
    }


def _experiment_payload(experiment: Experiment) -> dict[str, Any]:
    return {
        "run_id": experiment.run_id,
        "hyp_id": experiment.hyp_id,
        "seed": experiment.seed,
        "scores": experiment.scores,
        "constraints_ok": experiment.constraints_ok,
        "cost": experiment.cost,
        "simulated": experiment.simulated,
        "dataset_sha256s": experiment.dataset_sha256s,
    }


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteResearchStore:
    """Durable preregistration and experiment registry with budget-safe writes."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS hypotheses (
                    hyp_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    run_id TEXT PRIMARY KEY,
                    hyp_id TEXT NOT NULL REFERENCES hypotheses(hyp_id),
                    payload TEXT NOT NULL,
                    payload_sha256 TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS experiments_by_hypothesis
                    ON experiments(hyp_id, created_at, run_id);
                CREATE TABLE IF NOT EXISTS adapter_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    hyp_id TEXT NOT NULL REFERENCES hypotheses(hyp_id),
                    run_id TEXT NOT NULL REFERENCES experiments(run_id),
                    payload TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS adapter_proposals_by_hypothesis
                    ON adapter_proposals(hyp_id, created_at, proposal_id);
                """
            )
            experiment_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(experiments)")
            }
            if "payload_sha256" not in experiment_columns:
                db.execute("ALTER TABLE experiments ADD COLUMN payload_sha256 TEXT")
                legacy_runs = db.execute("SELECT run_id,payload FROM experiments").fetchall()
                for run in legacy_runs:
                    digest = hashlib.sha256(run["payload"].encode("utf-8")).hexdigest()
                    db.execute(
                        "UPDATE experiments SET payload_sha256=? WHERE run_id=?",
                        (digest, run["run_id"]),
                    )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def register(self, hypothesis: Hypothesis) -> None:
        """Persist a hypothesis before any result; duplicate IDs never overwrite."""
        hypothesis.validate()
        payload = _canonical_json(_hypothesis_payload(hypothesis))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        try:
            with closing(self._connect()) as db:
                db.execute(
                    "INSERT INTO hypotheses(hyp_id,payload,payload_sha256) VALUES(?,?,?)",
                    (hypothesis.hyp_id, payload, digest),
                )
        except sqlite3.IntegrityError as exc:
            raise ResearchError(f"hipótesis ya preregistrada: {hypothesis.hyp_id}") from exc

    def record(self, experiment: Experiment) -> None:
        """Atomically enforce the immutable preregistration and run budget."""
        experiment.validate()
        payload = _canonical_json(_experiment_payload(experiment))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload,payload_sha256 FROM hypotheses WHERE hyp_id=?",
                (experiment.hyp_id,),
            ).fetchone()
            if row is None:
                raise ResearchError("hipótesis debe preregistrarse antes de medir")
            if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row[
                "payload_sha256"
            ]:
                raise ResearchError("preregistro corrupto: hash no coincide")
            hypothesis = Hypothesis(**json.loads(row["payload"]))
            count = db.execute(
                "SELECT COUNT(*) FROM experiments WHERE hyp_id=?",
                (experiment.hyp_id,),
            ).fetchone()[0]
            if count >= hypothesis.budget_runs:
                raise ResearchError(
                    f"budget de corridas agotado ({hypothesis.budget_runs}); "
                    "una hipótesis negativa termina"
                )
            db.execute(
                "INSERT INTO experiments(run_id,hyp_id,payload,payload_sha256) "
                "VALUES(?,?,?,?)",
                (
                    experiment.run_id,
                    experiment.hyp_id,
                    payload,
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                ),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
            raise ResearchError(f"run_id duplicado: {experiment.run_id}") from exc
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def get_registry(self, hyp_id: str) -> Registry:
        """Rehydrate an immutable hypothesis and all of its persisted runs."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT payload,payload_sha256 FROM hypotheses WHERE hyp_id=?", (hyp_id,)
            ).fetchone()
            if row is None:
                raise ResearchError(f"hipótesis desconocida: {hyp_id}")
            if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row[
                "payload_sha256"
            ]:
                raise ResearchError("preregistro corrupto: hash no coincide")
            hypothesis = Hypothesis(**json.loads(row["payload"]))
            stored_runs = db.execute(
                "SELECT run_id,payload,payload_sha256 FROM experiments "
                "WHERE hyp_id=? ORDER BY created_at,run_id",
                (hyp_id,),
            ).fetchall()
            runs = []
            for run in stored_runs:
                if hashlib.sha256(run["payload"].encode("utf-8")).hexdigest() != run[
                    "payload_sha256"
                ]:
                    raise ResearchError(f"medición corrupta: hash no coincide ({run['run_id']})")
                runs.append(Experiment(**json.loads(run["payload"])))
        return Registry(hypothesis=hypothesis, runs=runs)

    def pareto(self, hyp_id: str) -> list[Experiment]:
        """Return only accepted real runs on the preregistered primary metric."""
        registry = self.get_registry(hyp_id)
        accepted = registry.accepted()
        return pareto_front(
            accepted,
            metric=registry.hypothesis.metric,
            higher_is_better=registry.hypothesis.higher_is_better,
        )

    def propose_adapter(self, proposal: AdapterProposal) -> None:
        """Persist an authorized, holdout-backed adapter proposal; never trains it."""
        proposal.validate()
        payload = _canonical_json(proposal.to_dict())
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT h.payload,h.payload_sha256,e.payload AS run_payload, "
                "e.payload_sha256 AS run_payload_sha256 "
                "FROM hypotheses h JOIN experiments e ON e.hyp_id=h.hyp_id "
                "WHERE h.hyp_id=? AND e.run_id=?",
                (proposal.hyp_id, proposal.run_id),
            ).fetchone()
            if row is None:
                raise ResearchError("la propuesta requiere hipótesis y corrida registradas")
            if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row[
                "payload_sha256"
            ]:
                raise ResearchError("preregistro corrupto: hash no coincide")
            if hashlib.sha256(row["run_payload"].encode("utf-8")).hexdigest() != row[
                "run_payload_sha256"
            ]:
                raise ResearchError("medición corrupta: hash no coincide")
            hypothesis = Hypothesis(**json.loads(row["payload"]))
            experiment = Experiment(**json.loads(row["run_payload"]))
            if proposal.hyp_id != hypothesis.hyp_id:
                raise ResearchError("la propuesta refiere otra hipótesis")
            if (
                experiment.simulated
                or not experiment.constraints_ok
                or not experiment.meets(hypothesis)
            ):
                raise ResearchError("la evidencia no es aceptable para proponer un adaptador")
            test_hashes = {
                dataset.sha256 for dataset in proposal.datasets if dataset.split == "test"
            }
            if not experiment.dataset_sha256s or not set(experiment.dataset_sha256s) <= test_hashes:
                raise ResearchError("la corrida ganadora no acredita el holdout declarado")
            db.execute(
                "INSERT INTO adapter_proposals "
                "(proposal_id,hyp_id,run_id,payload,payload_sha256) VALUES(?,?,?,?,?)",
                (proposal.proposal_id, proposal.hyp_id, proposal.run_id, payload, digest),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
            raise ResearchError(
                f"propuesta duplicada o corrida inválida: {proposal.proposal_id}"
            ) from exc
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def get_adapter_proposal(self, proposal_id: str) -> AdapterProposal:
        """Rehydrate a durable proposal only after verifying its stored hash."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT payload,payload_sha256 FROM adapter_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise ResearchError(f"propuesta desconocida: {proposal_id}")
        if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row[
            "payload_sha256"
        ]:
            raise ResearchError("propuesta corrupta: hash no coincide")
        return AdapterProposal.from_dict(json.loads(row["payload"]))


def pareto_front(
    experiments: list[Experiment], *, metric: str, higher_is_better: bool
) -> list[Experiment]:
    """Frontera de Pareto (métrica vs. coste) sobre corridas con restricciones ok.

    Solo entran corridas REALES con `constraints_ok` (identidad/factualidad como
    restricción dura, no solo score). Menor coste y mejor métrica dominan.
    """
    pool = [e for e in experiments if not e.simulated and e.constraints_ok and metric in e.scores]
    front: list[Experiment] = []
    for cand in pool:
        dominated = False
        for other in pool:
            if other is cand:
                continue
            better_metric = (
                other.scores[metric] >= cand.scores[metric]
                if higher_is_better
                else other.scores[metric] <= cand.scores[metric]
            )
            better_cost = other.cost <= cand.cost
            strictly = (
                (other.scores[metric] != cand.scores[metric]) or (other.cost != cand.cost)
            )
            if better_metric and better_cost and strictly:
                dominated = True
                break
        if not dominated:
            front.append(cand)
    return front


@dataclass(slots=True)
class PromotionProposal:
    """Propuesta de promoción (LoRA/preset) con rollback declarado (A26)."""

    name: str
    kind: str              # "lora" | "preset"
    datasets: list[str]
    licenses: list[str]
    hyperparameters: dict[str, Any]
    holdout: str
    rollback: str          # cómo revertir si falla en producción


@dataclass(slots=True)
class AdapterDataset:
    """Muestra concreta, autorizada y asignada a un split antes del experimento."""

    sample_id: str
    sha256: str
    license: str
    split: str
    authorized: bool
    authorization_reference: str


@dataclass(slots=True)
class AdapterProposal:
    """Propuesta reproducible que prepara promoción por PR, sin ejecutar training."""

    proposal_id: str
    hyp_id: str
    run_id: str
    name: str
    kind: str
    base_model: str
    base_model_revision: str
    code_revision: str
    datasets: list[AdapterDataset]
    hyperparameters: dict[str, Any]
    rollback_reference: str
    training_executed: bool = False
    artifact_sha256: str | None = None

    def validate(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.proposal_id, self.hyp_id, self.run_id, self.name,
                self.base_model, self.base_model_revision, self.rollback_reference,
            )
        ):
            raise ResearchError("propuesta requiere identidad, modelo base y rollback")
        if self.kind not in {"lora", "adapter", "preset"}:
            raise ResearchError("kind debe ser lora, adapter o preset")
        if not _is_git_revision(self.base_model_revision):
            raise ResearchError("base_model_revision debe ser una revisión fijada")
        if not _is_git_revision(self.code_revision):
            raise ResearchError("code_revision debe ser un commit SHA fijado")
        if self.training_executed:
            raise ResearchError("A26 prohíbe ejecutar training durante la propuesta")
        if self.artifact_sha256 is not None and not _is_sha256(self.artifact_sha256):
            raise ResearchError("artifact_sha256 debe ser SHA-256 válido")
        if not isinstance(self.hyperparameters, dict) or not self.hyperparameters:
            raise ResearchError("la propuesta requiere hyperparameters como objeto JSON")
        try:
            json.dumps(self.hyperparameters, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ResearchError("hyperparameters deben ser JSON finito") from exc
        if not self.datasets:
            raise ResearchError("la propuesta requiere dataset y holdout")
        sample_ids: set[str] = set()
        sample_hashes: set[str] = set()
        splits: set[str] = set()
        for sample in self.datasets:
            if not all(
                isinstance(value, str)
                for value in (sample.sample_id, sample.sha256, sample.license, sample.split)
            ):
                raise ResearchError("campos de muestra deben ser texto")
            if not sample.sample_id.strip() or not _is_sha256(sample.sha256):
                raise ResearchError("cada muestra requiere id y SHA-256")
            if not sample.license.strip() or sample.license.casefold() == "unknown":
                raise ResearchError("cada muestra requiere licencia conocida")
            if sample.authorized is not True:
                raise ResearchError("dataset sin autorización no puede usarse en la propuesta")
            if (
                not isinstance(sample.authorization_reference, str)
                or not sample.authorization_reference.strip()
            ):
                raise ResearchError("cada muestra requiere referencia de autorización")
            if sample.split not in {"train", "validation", "test"}:
                raise ResearchError("split debe ser train, validation o test")
            if sample.sample_id in sample_ids or sample.sha256 in sample_hashes:
                raise ResearchError("muestras duplicadas o filtradas entre splits")
            sample_ids.add(sample.sample_id)
            sample_hashes.add(sample.sha256)
            splits.add(sample.split)
        if splits != {"train", "validation", "test"}:
            raise ResearchError("se requieren splits train, validation y test")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "comfyuipod.adapter-proposal",
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "hyp_id": self.hyp_id,
            "run_id": self.run_id,
            "name": self.name,
            "kind": self.kind,
            "base_model": self.base_model,
            "base_model_revision": self.base_model_revision,
            "code_revision": self.code_revision,
            "datasets": [
                {
                    "sample_id": sample.sample_id,
                    "sha256": sample.sha256,
                    "license": sample.license,
                    "split": sample.split,
                    "authorized": sample.authorized,
                    "authorization_reference": sample.authorization_reference,
                }
                for sample in self.datasets
            ],
            "hyperparameters": self.hyperparameters,
            "rollback_reference": self.rollback_reference,
            "training_executed": self.training_executed,
            "artifact_sha256": self.artifact_sha256,
            "status": "proposed",
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AdapterProposal:
        data = dict(value)
        if data.pop("schema", None) != "comfyuipod.adapter-proposal":
            raise ResearchError("schema de propuesta de adaptador desconocido")
        if data.pop("schema_version", None) != 1:
            raise ResearchError("schema_version de propuesta no soportado")
        data.pop("status", None)
        data["datasets"] = [AdapterDataset(**sample) for sample in data["datasets"]]
        proposal = cls(**data)
        proposal.validate()
        return proposal


def can_promote(proposal: PromotionProposal, winning: Experiment, hyp: Hypothesis) -> bool:
    """¿Se puede promover? Solo con evidencia REAL que cumple el criterio (A26)."""
    if winning.simulated:
        raise ResearchError("no se promueve desde una corrida simulada (A26)")
    if not winning.constraints_ok:
        raise ResearchError("la corrida ganadora viola identidad/factualidad")
    if not proposal.rollback:
        raise ResearchError("la propuesta debe declarar un rollback")
    if not proposal.holdout:
        raise ResearchError("la propuesta debe declarar un holdout")
    return winning.meets(hyp)


__all__ = [
    "AdapterDataset",
    "AdapterProposal",
    "Experiment",
    "Hypothesis",
    "PromotionProposal",
    "Registry",
    "ResearchError",
    "can_promote",
    "pareto_front",
]
