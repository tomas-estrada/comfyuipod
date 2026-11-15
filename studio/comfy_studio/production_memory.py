"""Project-scoped audiovisual production memory backed by ProjectStore metadata.

This model stores editable scene, recipe, preference and decision notes. It is
separate from spatial-anchor history and orchestration checkpoints. ProjectStore
and its existing SQLite database remain the sole persistence authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any

from comfy_studio.config import SecretRedactor
from comfy_studio.projects import ProjectStore

MEMORY_SCHEMA = "comfy_studio.production_memory.v1"
MEMORY_META_KEY = "production_memory_v1"


class MemoryError(ValueError):
    """Base class for project-memory contract failures."""


class MemoryValidationError(MemoryError):
    """Memory input or persisted metadata is malformed."""


class MemoryCapacityError(MemoryError):
    """A project memory limit has been reached."""


class MemoryConflictError(MemoryError):
    """An existing logical memory needs an explicit correction."""


class MemoryNotFoundError(MemoryError):
    """The requested project memory does not exist."""


class MemoryKind(StrEnum):
    SCENE = "scene"
    RECIPE = "recipe"
    PREFERENCE = "preference"
    DECISION = "decision"


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    max_records: int = 128
    max_versions_per_record: int = 8
    max_key_chars: int = 128
    max_content_bytes: int = 8192
    max_tags: int = 16
    max_tag_chars: int = 64
    max_source_refs: int = 32
    max_source_ref_chars: int = 128
    max_reason_chars: int = 500
    max_author_chars: int = 100
    max_query_chars: int = 256
    max_state_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_records",
            "max_versions_per_record",
            "max_key_chars",
            "max_content_bytes",
            "max_tags",
            "max_tag_chars",
            "max_source_refs",
            "max_source_ref_chars",
            "max_reason_chars",
            "max_author_chars",
            "max_query_chars",
            "max_state_bytes",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} debe ser un entero positivo")


DEFAULT_LIMITS = MemoryLimits()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _now_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if len(token) > 1
    }


@dataclass(slots=True)
class ProductionMemory:
    """A bounded versioned memory service using ProjectStore's SQLite meta API."""

    store: ProjectStore
    limits: MemoryLimits = DEFAULT_LIMITS
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def _require_project(self, project_id: str) -> str:
        if self.store.get_project(project_id) is None:
            raise LookupError(f"Proyecto {project_id!r} no encontrado")
        return self.store._validate_project_id(project_id)

    def _load(self, project_id: str) -> dict[str, Any]:
        state = self.store.get_meta(project_id, MEMORY_META_KEY)
        if state is None:
            return {"schema": MEMORY_SCHEMA, "revision": 0, "records": []}
        if (
            not isinstance(state, dict)
            or state.get("schema") != MEMORY_SCHEMA
            or not isinstance(state.get("revision"), int)
            or isinstance(state.get("revision"), bool)
            or not isinstance(state.get("records"), list)
        ):
            raise MemoryValidationError("metadato production_memory_v1 inválido")
        try:
            state_bytes = len(_canonical_json(state).encode("utf-8"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise MemoryValidationError(
                "metadato production_memory_v1 no es JSON válido"
            ) from exc
        if state_bytes > self.limits.max_state_bytes:
            raise MemoryCapacityError("production_memory_v1 excede max_state_bytes")
        if len(state["records"]) > self.limits.max_records:
            raise MemoryCapacityError("production_memory_v1 excede max_records")
        seen_ids: set[str] = set()
        seen_keys: set[tuple[str, str]] = set()
        for record in state["records"]:
            if not isinstance(record, dict):
                raise MemoryValidationError("record de memoria debe ser objeto")
            memory_id = record.get("memory_id")
            kind = self._normalize_kind(record.get("kind"))
            key = self._normalize_key(record.get("key"))
            if not isinstance(memory_id, str) or memory_id != self._memory_id(
                project_id, kind, key
            ):
                raise MemoryValidationError("memory_id no coincide con su proyecto/kind/key")
            if memory_id in seen_ids or (kind, key) in seen_keys:
                raise MemoryValidationError("memoria duplicada en el metadato")
            seen_ids.add(memory_id)
            seen_keys.add((kind, key))
            versions = record.get("versions")
            if (
                not isinstance(versions, list)
                or not versions
                or len(versions) > self.limits.max_versions_per_record
            ):
                raise MemoryValidationError("historial de versiones inválido o fuera de límite")
            previous_version = 0
            for version in versions:
                if not isinstance(version, dict):
                    raise MemoryValidationError("versión de memoria debe ser objeto")
                version_number = version.get("version")
                if (
                    not isinstance(version_number, int)
                    or isinstance(version_number, bool)
                    or version_number <= previous_version
                ):
                    raise MemoryValidationError("número de versión inválido")
                previous_version = version_number
                self._normalize_content(version.get("content"))
                self._normalize_tags(version.get("tags") or [])
                self._normalize_source_refs(version.get("source_refs") or [])
                created_at = version.get("created_at")
                if not isinstance(created_at, str):
                    raise MemoryValidationError("created_at de versión inválido")
                try:
                    datetime.fromisoformat(created_at)
                except ValueError as exc:
                    raise MemoryValidationError("created_at de versión inválido") from exc
        return state

    def _save(self, project_id: str, state: dict[str, Any]) -> None:
        state["revision"] = int(state.get("revision", 0)) + 1
        encoded = _canonical_json(state)
        if len(encoded.encode("utf-8")) > self.limits.max_state_bytes:
            state["revision"] -= 1
            raise MemoryCapacityError("production_memory_v1 excede max_state_bytes")
        self.store.set_meta(project_id, MEMORY_META_KEY, state)

    def _normalize_kind(self, kind: MemoryKind | str) -> str:
        try:
            return MemoryKind(kind).value
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(item.value for item in MemoryKind)
            raise MemoryValidationError(f"kind debe ser uno de: {allowed}") from exc

    def _normalize_content(self, content: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(content, dict) or not content:
            raise MemoryValidationError("content debe ser un objeto JSON no vacío")
        try:
            serialized = _canonical_json(content)
        except (TypeError, ValueError, RecursionError) as exc:
            raise MemoryValidationError("content debe ser JSON finito y serializable") from exc
        if len(serialized.encode("utf-8")) > self.limits.max_content_bytes:
            raise MemoryValidationError("content excede max_content_bytes")
        return json.loads(serialized)

    def _normalize_tags(self, tags: Iterable[str]) -> list[str]:
        if not isinstance(tags, Iterable) or isinstance(tags, (str, bytes)):
            raise MemoryValidationError("tags debe ser una lista de textos")
        values = sorted({str(tag).strip().casefold() for tag in tags if str(tag).strip()})
        if len(values) > self.limits.max_tags:
            raise MemoryValidationError("tags excede max_tags")
        if any(len(tag) > self.limits.max_tag_chars for tag in values):
            raise MemoryValidationError("tag excede max_tag_chars")
        return values

    def _normalize_source_refs(self, refs: Iterable[str]) -> list[str]:
        if not isinstance(refs, Iterable) or isinstance(refs, (str, bytes)):
            raise MemoryValidationError("source_refs debe ser una lista de IDs")
        values = list(dict.fromkeys(str(ref).strip() for ref in refs))
        if len(values) > self.limits.max_source_refs:
            raise MemoryValidationError("source_refs excede max_source_refs")
        if any(not ref or len(ref) > self.limits.max_source_ref_chars for ref in values):
            raise MemoryValidationError("source_ref vacío o excede max_source_ref_chars")
        return values

    def _normalize_key(self, key: str) -> str:
        if not isinstance(key, str):
            raise MemoryValidationError("key debe ser texto")
        normalized = key.strip()
        if not normalized or len(normalized) > self.limits.max_key_chars:
            raise MemoryValidationError(
                f"key debe tener entre 1 y {self.limits.max_key_chars} caracteres"
            )
        return normalized

    @staticmethod
    def _memory_id(project_id: str, kind: str, key: str) -> str:
        material = f"{project_id}\0{kind}\0{key}".encode()
        return f"mem_{hashlib.sha256(material).hexdigest()[:24]}"

    @staticmethod
    def _latest(record: dict[str, Any]) -> dict[str, Any]:
        versions = record.get("versions")
        if not isinstance(versions, list) or not versions:
            raise MemoryValidationError("record sin versiones")
        return versions[-1]

    @staticmethod
    def _public_record(project_id: str, record: dict[str, Any]) -> dict[str, Any]:
        latest = ProductionMemory._latest(record)
        return {
            "project_id": project_id,
            "memory_id": record["memory_id"],
            "kind": record["kind"],
            "key": record["key"],
            "version": latest["version"],
            "content": latest["content"],
            "tags": list(latest.get("tags") or []),
            "source_refs": list(latest.get("source_refs") or []),
            "recorded_by": latest.get("recorded_by"),
            "correction_reason": latest.get("correction_reason"),
            "created_at": latest["created_at"],
            "evidence_claim": False,
        }

    def remember(
        self,
        project_id: str,
        *,
        kind: MemoryKind | str,
        key: str,
        content: dict[str, Any],
        tags: Iterable[str] = (),
        source_refs: Iterable[str] = (),
        recorded_by: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a logical memory; identical retries return its current version."""
        pid = self._require_project(project_id)
        normalized_kind = self._normalize_kind(kind)
        normalized_key = self._normalize_key(key)
        normalized_content = self._normalize_content(content)
        normalized_tags = self._normalize_tags(tags)
        normalized_refs = self._normalize_source_refs(source_refs)
        author = str(recorded_by).strip() if recorded_by is not None else None
        if author is not None and (not author or len(author) > self.limits.max_author_chars):
            raise MemoryValidationError("recorded_by vacío o demasiado largo")
        memory_id = self._memory_id(pid, normalized_kind, normalized_key)
        with self._lock:
            state = self._load(pid)
            existing = next(
                (row for row in state["records"] if row.get("memory_id") == memory_id),
                None,
            )
            if existing is not None:
                current = self._latest(existing)
                same = (
                    current.get("content") == normalized_content
                    and current.get("tags", []) == normalized_tags
                    and current.get("source_refs", []) == normalized_refs
                )
                if same:
                    return self._public_record(pid, existing)
                raise MemoryConflictError(
                    "La memoria ya existe con otro contenido; usa correct() para versionarla"
                )
            if len(state["records"]) >= self.limits.max_records:
                raise MemoryCapacityError("límite de memorias del proyecto excedido (max_records)")
            created_at = _now_iso(now)
            record = {
                "memory_id": memory_id,
                "kind": normalized_kind,
                "key": normalized_key,
                "versions": [
                    {
                        "version": 1,
                        "content": normalized_content,
                        "tags": normalized_tags,
                        "source_refs": normalized_refs,
                        "recorded_by": author,
                        "correction_reason": None,
                        "created_at": created_at,
                    }
                ],
            }
            state["records"].append(record)
            self._save(pid, state)
            return self._public_record(pid, record)

    def correct(
        self,
        project_id: str,
        memory_id: str,
        *,
        content: dict[str, Any],
        reason: str,
        tags: Iterable[str] | None = None,
        source_refs: Iterable[str] | None = None,
        recorded_by: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append a correction version, retaining bounded history and reason."""
        pid = self._require_project(project_id)
        if not isinstance(reason, str) or not reason.strip():
            raise MemoryValidationError("reason es obligatorio para corregir memoria")
        if len(reason.strip()) > self.limits.max_reason_chars:
            raise MemoryValidationError("reason excede max_reason_chars")
        normalized_content = self._normalize_content(content)
        with self._lock:
            state = self._load(pid)
            record = next(
                (row for row in state["records"] if row.get("memory_id") == memory_id),
                None,
            )
            if record is None:
                raise MemoryNotFoundError(f"memory_id {memory_id!r} not found")
            current = self._latest(record)
            next_tags = (
                self._normalize_tags(tags) if tags is not None else list(current.get("tags") or [])
            )
            next_refs = (
                self._normalize_source_refs(source_refs)
                if source_refs is not None
                else list(current.get("source_refs") or [])
            )
            reason = reason.strip()
            if (
                current.get("content") == normalized_content
                and current.get("tags", []) == next_tags
                and current.get("source_refs", []) == next_refs
                and current.get("correction_reason") == reason
            ):
                return self._public_record(pid, record)
            author = str(recorded_by).strip() if recorded_by is not None else None
            if author is not None and (not author or len(author) > self.limits.max_author_chars):
                raise MemoryValidationError("recorded_by vacío o demasiado largo")
            next_version = int(current["version"]) + 1
            versions = list(record["versions"])
            versions.append(
                {
                    "version": next_version,
                    "content": normalized_content,
                    "tags": next_tags,
                    "source_refs": next_refs,
                    "recorded_by": author,
                    "correction_reason": reason,
                    "created_at": _now_iso(now),
                }
            )
            record["versions"] = versions[-self.limits.max_versions_per_record :]
            self._save(pid, state)
            return self._public_record(pid, record)

    def get_history(self, project_id: str, memory_id: str) -> list[dict[str, Any]]:
        pid = self._require_project(project_id)
        state = self._load(pid)
        record = next(
            (row for row in state["records"] if row.get("memory_id") == memory_id),
            None,
        )
        if record is None:
            raise MemoryNotFoundError(f"memory_id {memory_id!r} not found")
        return [
            {
                "memory_id": memory_id,
                "kind": record["kind"],
                "key": record["key"],
                **dict(version),
            }
            for version in record["versions"]
        ]

    def list_records(
        self,
        project_id: str,
        *,
        kind: MemoryKind | str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        pid = self._require_project(project_id)
        selected_kind = self._normalize_kind(kind) if kind is not None else None
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
        ):
            raise MemoryValidationError("limit debe ser entero no negativo")
        state = self._load(pid)
        rows = [
            self._public_record(pid, row)
            for row in state["records"]
            if selected_kind is None or row.get("kind") == selected_kind
        ]
        rows.sort(key=lambda row: row["memory_id"])
        return rows[:limit] if limit is not None else rows

    def retrieve(
        self,
        project_id: str,
        query: str,
        *,
        kinds: Iterable[MemoryKind | str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Rank same-project memories by deterministic token overlap."""
        if not isinstance(query, str) or not query.strip():
            raise MemoryValidationError("query no puede estar vacío")
        if len(query) > self.limits.max_query_chars:
            raise MemoryValidationError("query excede max_query_chars")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= self.limits.max_records
        ):
            raise MemoryValidationError("limit fuera de rango")
        selected_kinds = (
            {self._normalize_kind(kind) for kind in kinds} if kinds is not None else None
        )
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        records = self.list_records(project_id)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for row in records:
            if selected_kinds is not None and row["kind"] not in selected_kinds:
                continue
            searchable = _canonical_json(
                {
                    "key": row["key"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "tags": row["tags"],
                }
            )
            overlap = len(query_tokens & _tokens(searchable))
            if overlap:
                scored.append((overlap, row["memory_id"], row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [row for _, _, row in scored[:limit]]

    def prune(self, project_id: str, *, older_than: datetime) -> int:
        """Remove superseded versions older than the cutoff; current stays intact."""
        pid = self._require_project(project_id)
        if not isinstance(older_than, datetime):
            raise MemoryValidationError("older_than debe ser datetime")
        cutoff = _now_iso(older_than)
        with self._lock:
            state = self._load(pid)
            removed = 0
            for record in state["records"]:
                versions = record["versions"]
                current = versions[-1]
                kept = [
                    version
                    for version in versions[:-1]
                    if _now_iso(datetime.fromisoformat(version["created_at"])) >= cutoff
                ]
                removed += len(versions) - 1 - len(kept)
                record["versions"] = [*kept, current]
            if removed:
                self._save(pid, state)
            return removed

    def delete(self, project_id: str, memory_id: str) -> bool:
        """Delete a memory and its entire retained correction history."""
        pid = self._require_project(project_id)
        with self._lock:
            state = self._load(pid)
            remaining = [
                row for row in state["records"] if row.get("memory_id") != memory_id
            ]
            if len(remaining) == len(state["records"]):
                return False
            state["records"] = remaining
            self._save(pid, state)
            return True

    def export(
        self,
        project_id: str,
        *,
        redactor: SecretRedactor | None = None,
    ) -> dict[str, Any]:
        """Export all retained project memory after recursive secret redaction."""
        pid = self._require_project(project_id)
        state = self._load(pid)
        safe_redactor = redactor or SecretRedactor.from_env()

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                return safe_redactor.redact(value)
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {
                    safe_redactor.redact(str(key)): redact(item)
                    for key, item in value.items()
                }
            return value

        return redact(
            {
                "schema": state["schema"],
                "project_id": pid,
                "revision": state["revision"],
                "records": state["records"],
            }
        )


__all__ = [
    "DEFAULT_LIMITS",
    "MEMORY_META_KEY",
    "MEMORY_SCHEMA",
    "MemoryCapacityError",
    "MemoryConflictError",
    "MemoryError",
    "MemoryKind",
    "MemoryLimits",
    "MemoryNotFoundError",
    "MemoryValidationError",
    "ProductionMemory",
]
