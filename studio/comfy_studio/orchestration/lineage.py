"""Lineage y DAG con invalidación selectiva (plan Ultra, W04 / acceptance A05).

El objetivo que la política fija con un ejemplo concreto: «Cambiar un título no
debe regenerar actores; cambiar iluminación no debe recalcular transcripción».
Un render de producción es caro; recomputar todo ante cualquier edición
desperdicia GPU y rompe la continuidad de lo ya aprobado.

Este módulo modela la producción como un **DAG de nodos** (un nodo por capa de
cada shot, más nodos de entrada como `brief`, `asr`, `geometry`, `lighting`),
cada uno con:

- un conjunto de **entradas con hash de contenido** (`inputs`): el valor del que
  depende su resultado;
- las **aristas** `depends_on` hacia otros nodos;
- un **fingerprint** = hash(kind + inputs + fingerprints de sus dependencias).

Cuando una entrada cambia, se recalcula el fingerprint de su nodo; si cambió, el
nodo y SOLO sus descendientes transitivos quedan **stale** (a recomputar). Los
nodos no afectados conservan su artifact: no se regeneran.

Es puro cálculo determinista sobre el estado — no toca GPU ni motores. Se valida
en CPU (A05); la ejecución real de los nodos stale la hace el executor con el
lease de W03.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _hash_inputs(kind: str, inputs: dict[str, Any], dep_fingerprints: list[str]) -> str:
    """Fingerprint determinista de un nodo.

    Depende de su tipo, de sus entradas (ordenadas y serializadas de forma
    estable) y de los fingerprints de sus dependencias, de modo que un cambio
    aguas arriba se propaga hacia abajo pero no lateralmente.
    """
    payload = {
        "kind": kind,
        "inputs": {k: inputs[k] for k in sorted(inputs)},
        "deps": sorted(dep_fingerprints),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class LineageNode:
    """Un nodo del DAG de producción con su procedencia."""

    node_id: str
    kind: str
    inputs: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    #: fingerprint del último resultado computado (None = nunca computado/stale).
    fingerprint: str | None = None
    #: puntero al artifact producido (ruta), si se computó.
    artifact: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "inputs": dict(self.inputs),
            "depends_on": list(self.depends_on),
            "fingerprint": self.fingerprint,
            "artifact": self.artifact,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LineageNode:
        return cls(
            node_id=data["node_id"],
            kind=data["kind"],
            inputs=dict(data.get("inputs", {})),
            depends_on=list(data.get("depends_on", [])),
            fingerprint=data.get("fingerprint"),
            artifact=data.get("artifact"),
        )


class LineageGraph:
    """DAG de producción con invalidación selectiva por fingerprint."""

    def __init__(self) -> None:
        self._nodes: dict[str, LineageNode] = {}

    # -- construcción -----------------------------------------------------

    def add(
        self,
        node_id: str,
        kind: str,
        *,
        inputs: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
    ) -> LineageNode:
        node = LineageNode(
            node_id=node_id,
            kind=kind,
            inputs=dict(inputs or {}),
            depends_on=list(depends_on or []),
        )
        self._nodes[node_id] = node
        return node

    def get(self, node_id: str) -> LineageNode | None:
        return self._nodes.get(node_id)

    def nodes(self) -> list[LineageNode]:
        return list(self._nodes.values())

    # -- topología --------------------------------------------------------

    def _dependents(self, node_id: str) -> list[str]:
        """Nodos que dependen DIRECTAMENTE de `node_id`."""
        return [n.node_id for n in self._nodes.values() if node_id in n.depends_on]

    def descendants(self, node_id: str) -> set[str]:
        """Todos los descendientes transitivos de `node_id` (sin incluirlo)."""
        seen: set[str] = set()
        stack = list(self._dependents(node_id))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._dependents(current))
        return seen

    def topo_order(self) -> list[str]:
        """Orden topológico (dependencias antes que dependientes).

        Lanza `ValueError` si hay un ciclo: un DAG de producción no puede tener
        dependencias circulares.
        """
        indegree = {nid: 0 for nid in self._nodes}
        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep in indegree:
                    indegree[node.node_id] += 1
        ready = [nid for nid, d in indegree.items() if d == 0]
        order: list[str] = []
        while ready:
            ready.sort()  # determinista
            nid = ready.pop(0)
            order.append(nid)
            for dependent in self._dependents(nid):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if len(order) != len(self._nodes):
            raise ValueError("el DAG de producción tiene un ciclo")
        return order

    # -- fingerprints e invalidación -------------------------------------

    def compute_fingerprints(self) -> dict[str, str]:
        """Recalcula el fingerprint de cada nodo en orden topológico.

        Marca cada nodo con su fingerprint actual y devuelve el mapa. No decide
        qué está stale; eso lo hace `plan_recompute` comparando con el estado
        previo.
        """
        result: dict[str, str] = {}
        for nid in self.topo_order():
            node = self._nodes[nid]
            dep_fps = [result[d] for d in node.depends_on if d in result]
            fp = _hash_inputs(node.kind, node.inputs, dep_fps)
            node.fingerprint = fp
            result[nid] = fp
        return result

    def set_computed(self, node_id: str, artifact: str) -> None:
        """Registra que un nodo se computó: fija artifact (y su fingerprint)."""
        node = self._nodes[node_id]
        if node.fingerprint is None:
            self.compute_fingerprints()
        node.artifact = artifact

    def update_inputs(self, node_id: str, **inputs: Any) -> None:
        """Cambia una o más entradas de un nodo (p. ej. el título, la luz)."""
        self._nodes[node_id].inputs.update(inputs)

    def plan_recompute(self, previous: dict[str, str]) -> set[str]:
        """Qué nodos hay que recomputar tras un cambio.

        Compara los fingerprints actuales (recalculados) con los `previous`:
        un nodo es stale si su fingerprint cambió. Por construcción del hash
        (incluye los fingerprints de las dependencias), un cambio aguas arriba
        invalida a sus descendientes y SOLO a ellos; los nodos de otras ramas
        conservan su fingerprint y su artifact.
        """
        current = self.compute_fingerprints()
        stale: set[str] = set()
        for nid, fp in current.items():
            if previous.get(nid) != fp:
                stale.add(nid)
        return stale

    def invalidate_artifacts(self, stale: set[str]) -> None:
        """Borra el puntero de artifact de los nodos stale (hay que recomputar).

        Los nodos NO stale conservan su artifact: no se regeneran.
        """
        for nid in stale:
            node = self._nodes.get(nid)
            if node is not None:
                node.artifact = None

    # -- serialización ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": [n.to_dict() for n in self._nodes.values()]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LineageGraph:
        graph = cls()
        for nd in data.get("nodes", []):
            node = LineageNode.from_dict(nd)
            graph._nodes[node.node_id] = node
        return graph


__all__ = ["LineageGraph", "LineageNode"]
