"""Librería de assets / continuidad del proyecto (M4-SCR-004, F4).

Lo que separa un ESTUDIO de un generador de clips: el mismo personaje, coche o
voz debe sobrevivir de la escena 1 a la 7 y de un reel al siguiente. Sin esto
hay "deriva" (el personaje cambia de cara entre tomas).

La idea: cada asset lógico (p.ej. "protagonista", "coche_rojo") se construye UNA
vez (una LoRA, una malla .glb, un embedding de cara, una muestra de voz) y se
registra bajo un id estable del proyecto. Cada escena posterior RESUELVE ese id
en vez de regenerar — misma identidad, coste pagado una sola vez.

Store puro y serializable (persistible en el SQLite del proyecto, reanudable sin
GPU). No genera nada: cataloga lo que los motores produjeron y lo entrega por id.

Tipos de asset (kind):
    character | object | voice | style | mesh3d | lora | audio | other
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AssetKind(StrEnum):
    CHARACTER = "character"
    OBJECT = "object"
    VOICE = "voice"
    STYLE = "style"
    MESH3D = "mesh3d"
    LORA = "lora"
    AUDIO = "audio"
    OTHER = "other"


class AssetError(RuntimeError):
    """Error de resolución o registro en la librería de assets."""


@dataclass(slots=True)
class Asset:
    """Un asset lógico reusable del proyecto."""

    asset_id: str  # id estable, p.ej. "protagonista"
    kind: AssetKind
    path: str  # ruta en /workspace del artefacto producido
    produced_by: str  # motor que lo creó, p.ej. "comfyui", "blender"
    meta: dict[str, Any] = field(default_factory=dict)
    created_ts: float = field(default_factory=lambda: time.time())
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": str(self.kind),
            "path": self.path,
            "produced_by": self.produced_by,
            "meta": dict(self.meta),
            "created_ts": self.created_ts,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Asset:
        return cls(
            asset_id=str(data["asset_id"]),
            kind=AssetKind(data["kind"]),
            path=str(data["path"]),
            produced_by=str(data.get("produced_by", "unknown")),
            meta=dict(data.get("meta", {})),
            created_ts=float(data.get("created_ts", time.time())),
            version=int(data.get("version", 1)),
        )


class AssetLibrary:
    """Librería por proyecto: registra una vez, resuelve en cada escena."""

    def __init__(self) -> None:
        self._assets: dict[str, Asset] = {}

    def register(
        self,
        asset_id: str,
        kind: AssetKind,
        path: str,
        produced_by: str,
        meta: dict[str, Any] | None = None,
    ) -> Asset:
        """Registra un asset nuevo, o crea una nueva VERSIÓN si el id ya existe.

        Versionar (no sobrescribir en silencio) preserva continuidad: una escena
        ya renderizada puede seguir apuntando a su versión mientras otra usa la
        nueva.
        """
        if not asset_id.strip():
            raise AssetError("asset_id no puede estar vacío")
        existing = self._assets.get(asset_id)
        version = (existing.version + 1) if existing else 1
        asset = Asset(
            asset_id=asset_id,
            kind=kind,
            path=path,
            produced_by=produced_by,
            meta=meta or {},
            version=version,
        )
        self._assets[asset_id] = asset
        return asset

    def resolve(self, asset_id: str) -> Asset:
        """Devuelve el asset por id para reuso en una escena. Falla si falta."""
        asset = self._assets.get(asset_id)
        if asset is None:
            raise AssetError(
                f"asset '{asset_id}' no está en la librería; "
                "constrúyelo antes de referenciarlo en una escena"
            )
        return asset

    def has(self, asset_id: str) -> bool:
        return asset_id in self._assets

    def by_kind(self, kind: AssetKind) -> list[Asset]:
        return [a for a in self._assets.values() if a.kind == kind]

    def ids(self) -> list[str]:
        return sorted(self._assets)

    # -- persistencia --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"assets": [self._assets[i].to_dict() for i in self.ids()]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AssetLibrary:
        lib = cls()
        for raw in data.get("assets", []):
            asset = Asset.from_dict(raw)
            lib._assets[asset.asset_id] = asset
        return lib
