"""ComfyUI community index downloader with on-disk cache.

Stdlib-only: urllib.request, json, pathlib, os, time, socket, dataclasses.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comfy_studio.config import SecretRedactor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDEX_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/"
    "custom-node-list.json"
)
CACHE_DIRNAME = ".cache/template_index"
CACHE_TTL_S = 24 * 3600
DEFAULT_TIMEOUT_S = 10.0
CACHE_FILENAME = "custom-node-list.json"


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def redact_url(url: str) -> str:
    """Strip credentials and sensitive query params from a URL.

    Pure function; never raises. Bad URL in -> the input back, redacted as far
    as possible.
    """
    if not isinstance(url, str) or not url:
        return url

    redactor = SecretRedactor()
    out = redactor.redact(url)

    # Strip userinfo: scheme://user:pass@host -> scheme://***@host
    # Use a simple approach: find ://, then find @ before /
    try:
        scheme_rest = out.split("://", 1)
        if len(scheme_rest) == 2:
            scheme, rest = scheme_rest
            # Find the first @ that appears before the first /
            slash_idx = rest.find("/")
            at_idx = rest.find("@")
            if 0 < at_idx < (slash_idx if slash_idx != -1 else len(rest)):
                userinfo = rest[:at_idx]
                host_part = rest[at_idx + 1 :]
                # Check if userinfo contains a colon (user:pass)
                if ":" in userinfo:
                    out = f"{scheme}://***@{host_part}"
                else:
                    # Just user, no pass: still redact
                    out = f"{scheme}://***@{host_part}"
    except Exception:
        pass

    # Redact sensitive query params
    sensitive = {
        "token",
        "key",
        "secret",
        "password",
        "auth",
        "access_token",
        "api_key",
        "sig",
        "signature",
    }
    if "?" in out:
        parts = out.split("?", 1)
        path_part = parts[0]
        query_part = parts[1]
        params = query_part.split("&")
        redacted_params = []
        for param in params:
            if "=" in param:
                k, v = param.split("=", 1)
                if k.lower() in sensitive:
                    redacted_params.append(f"{k}=***")
                else:
                    redacted_params.append(param)
            else:
                redacted_params.append(param)
        out = path_part + "?" + "&".join(redacted_params)

    # Run through SecretRedactor again for generic token patterns
    out = redactor.redact(out)
    return out


# ---------------------------------------------------------------------------
# Cache dir resolution
# ---------------------------------------------------------------------------


def resolve_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the cache directory under COMFY_DATA_DIR."""
    environment = dict(os.environ) if env is None else dict(env)
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip() or "/workspace/comfyui-data"
    return Path(comfy_data) / CACHE_DIRNAME


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CommunityIndexEntry:
    """One entry from the community index."""

    title: str
    author: str
    reference: str
    files: tuple[str, ...]
    install_type: str
    description: str

    @property
    def entry_id(self) -> str:
        """Stable id: 'community:' + repo slug from reference or files[0]."""
        candidates: list[str] = []
        if self.reference:
            candidates.append(self.reference)
        if self.files:
            candidates.append(self.files[0])
        for url in candidates:
            slug = _slug_from_url(url)
            if slug:
                return f"community:{slug}"
        return ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "reference": self.reference,
            "files": list(self.files),
            "install_type": self.install_type,
            "description": self.description,
            "entry_id": self.entry_id,
        }


@dataclass(slots=True)
class IndexResult:
    """Result of a fetch operation."""

    entries: list[CommunityIndexEntry] = field(default_factory=list)
    source: str = "unavailable"  # 'cache' | 'network' | 'unavailable'
    cause: str = ""
    fetched_at_s: float = 0.0
    age_s: float | None = None
    entry_count: int = 0

    def __post_init__(self):
        self.entry_count = len(self.entries)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.to_public_dict() for e in self.entries],
            "source": self.source,
            "cause": self.cause,
            "fetched_at_s": self.fetched_at_s,
            "age_s": self.age_s,
            "entry_count": self.entry_count,
        }


# ---------------------------------------------------------------------------
# URL slug extraction
# ---------------------------------------------------------------------------


def _slug_from_url(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL, or empty string."""
    if not url:
        return ""
    # Strip trailing slashes and fragments
    clean = url.split("#")[0].rstrip("/")
    # Try to find github.com/owner/repo pattern
    parts = clean.split("github.com/")
    if len(parts) != 2:
        return ""
    after = parts[1]
    # Take owner/repo (first two path components)
    segments = [s for s in after.split("/") if s]
    if len(segments) >= 2:
        return f"{segments[0]}/{segments[1]}"
    return ""


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


class CommunityIndexDownloader:
    """Download and cache the ComfyUI community index."""

    def __init__(
        self,
        *,
        url: str = INDEX_URL,
        cache_dir: Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = CACHE_TTL_S,
        opener: Callable[[str, float], bytes] | None = None,
        env: Mapping[str, str] | None = None,
    ):
        self.url = url
        self.timeout_s = timeout_s
        self.ttl_s = ttl_s
        self.env = env
        self._opener = opener
        self._cache_dir = cache_dir

    def _get_cache_dir(self) -> Path:
        if self._cache_dir is not None:
            return self._cache_dir
        return resolve_cache_dir(self.env)

    def _cache_path(self) -> Path:
        return self._get_cache_dir() / CACHE_FILENAME

    def _read_cache(self) -> IndexResult | None:
        """Read and return cache content, or None if not fresh/missing."""
        path = self._cache_path()
        if not path.is_file():
            return None
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            age = time.time() - mtime
            if age > self.ttl_s:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = _parse_index(data)
            return IndexResult(
                entries=entries,
                source="cache",
                cause="",
                fetched_at_s=mtime,
                age_s=age,
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _download(self) -> tuple[bytes | None, str]:
        """Download the index. Returns (bytes, "") or (None, motivo redactado).

        El motivo viaja redactado desde aquí porque un cliente HTTP real puede
        hacer eco de la URL completa —con su token— en el mensaje de error.
        """
        if self._opener is not None:
            try:
                return self._opener(self.url, self.timeout_s), ""
            except Exception as exc:
                return None, redact_url(f"{type(exc).__name__}: {exc}")
        try:
            req = urllib.request.Request(self.url)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read(), ""
        except Exception as exc:
            return None, redact_url(f"{type(exc).__name__}: {exc}")

    def _write_cache(self, data: bytes) -> None:
        """Atomically write cache file."""
        cache_dir = self._get_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _parse_bytes(self, data: bytes) -> list[CommunityIndexEntry]:
        """Parse raw bytes into entries. Raises on malformed data."""
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("index is not a JSON object")
        nodes = parsed.get("custom_nodes")
        if not isinstance(nodes, list):
            raise ValueError("'custom_nodes' is not a list")
        return _parse_index(parsed)

    def fetch(self, *, force: bool = False) -> IndexResult:
        """Fetch the index. NEVER RAISES."""
        # a) Fresh cache?
        if not force:
            cached = self._read_cache()
            if cached is not None:
                return cached

        # b) Download
        raw, failure_reason = self._download()
        if raw is not None:
            try:
                entries = self._parse_bytes(raw)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
                # El motivo puede arrastrar la URL: se redacta en origen.
                failure_reason = redact_url(f"{type(exc).__name__}: {exc}")
            else:
                # Escribir la caché es una optimización, no una condición de
                # éxito: si el disco no deja (solo lectura, cuota), el índice
                # recién descargado sigue siendo válido y se devuelve igual.
                try:
                    self._write_cache(raw)
                except OSError:
                    pass
                now = time.time()
                return IndexResult(
                    entries=entries,
                    source="network",
                    cause="",
                    fetched_at_s=now,
                    age_s=0.0,
                )

        # c) Fallback to stale cache
        path = self._cache_path()
        if path.is_file():
            try:
                stat = path.stat()
                mtime = stat.st_mtime
                age = time.time() - mtime
                data = json.loads(path.read_text(encoding="utf-8"))
                entries = _parse_index(data)
                return IndexResult(
                    entries=entries,
                    source="cache",
                    cause=f"stale cache used after: {failure_reason}",
                    fetched_at_s=mtime,
                    age_s=age,
                )
            except Exception:
                pass

        # d) No cache at all
        return IndexResult(
            entries=[],
            source="unavailable",
            cause=failure_reason,
            fetched_at_s=0.0,
            age_s=None,
        )

    def cached_age_s(self) -> float | None:
        """Return cache age in seconds, or None if no cache file."""
        path = self._cache_path()
        if not path.is_file():
            return None
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return None


# ---------------------------------------------------------------------------
# Internal parser
# ---------------------------------------------------------------------------


def _parse_index(data: dict) -> list[CommunityIndexEntry]:
    """Parse the top-level index dict into a list of entries."""
    nodes = data.get("custom_nodes")
    if not isinstance(nodes, list):
        raise ValueError("'custom_nodes' is not a list")
    entries: list[CommunityIndexEntry] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        title = node.get("title")
        if not title or not isinstance(title, str):
            continue  # skip entries without title
        files_raw = node.get("files", [])
        if not isinstance(files_raw, list):
            files_raw = []
        files_tuple = tuple(str(f) for f in files_raw if isinstance(f, str))
        entries.append(
            CommunityIndexEntry(
                title=title,
                author=str(node.get("author", "")),
                reference=str(node.get("reference", "")),
                files=files_tuple,
                install_type=str(node.get("install_type", "")),
                description=str(node.get("description", "")),
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Tokenization and node attribution
# ---------------------------------------------------------------------------


def tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, splitting on non-alphanumerics and on
    lowercase→uppercase transitions that signal a new word.

    Accents are stripped so that e.g. 'animación' and 'animacion' match.

    Examples
    --------
    >>> tokenize("ComfyUI-IPAdapter")
    {'comfyui', 'ipadapter'}
    >>> tokenize("IPAdapterApply")
    {'ipadapter', 'apply'}
    >>> tokenize("animación")
    {'animacion'}
    """
    # 1. Normalize to NFKD and strip combining marks (accents).
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    # 2. Replace non-alphanumeric runs with a single space.
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", stripped)
    # 3. Split before an uppercase letter that is followed by a lowercase letter
    #    and is itself preceded by a lowercase letter or digit.  This splits
    #    "IPAdapterApply" into "IPAdapter" + "Apply" but keeps "ComfyUI"
    #    together because the 'U' is followed by 'I' (not lowercase).
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])", " ", cleaned)
    # 4. Lowercase and collect contiguous alphanumeric runs.
    return {w for w in re.findall(r"[a-z0-9]+", split.lower()) if w}


def attribute_nodes(pack_title: str, installed_nodes: Iterable[str]) -> list[str]:
    """Return the installed node names that share at least one token with `pack_title`.

    The result is sorted and de-duplicated. Pure function; no I/O.
    """
    title_tokens = tokenize(pack_title)
    if not title_tokens:
        return []
    matched: list[str] = []
    seen: set[str] = set()
    for node_name in installed_nodes:
        node_tokens = tokenize(node_name)
        if title_tokens & node_tokens and node_name not in seen:
            seen.add(node_name)
            matched.append(node_name)
    matched.sort()
    return matched


# ---------------------------------------------------------------------------
# Search result and search engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchResult:
    """One ranked result from a community template search."""

    template: DiscoveredTemplate
    relevance: float = 0.0          # 0..1 lexical overlap intent <-> title+description
    coverage: int = 0               # number of installed nodes attributed to this pack
    matched_nodes: list[str] = field(default_factory=list)  # those nodes, sorted
    installed_pack: bool = False    # True when coverage > 0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "template": self.template.to_public_dict(),
            "relevance": self.relevance,
            "coverage": self.coverage,
            "matched_nodes": list(self.matched_nodes),
            "installed_pack": self.installed_pack,
            # Procedencia también en la raíz: quién consume el resultado no tiene
            # que entrar en `template` para saber si el índice era real y fresco.
            "provenance": self.template.provenance,
        }


@dataclass(slots=True)
class DiscoveredTemplate:
    """Una plantilla candidata hallada en una fuente comunitaria (no curada).

    Forma mínima para poder buscar/cruzar igual que una curada: id, texto para
    casar la intención, nodos requeridos (para el cruce de capacidades) y la
    procedencia. El workflow JSON en sí se trae solo si el agente la elige.
    """

    template_id: str
    name: str
    category: str
    text: str  # resumen/descripción para el match léxico
    required_nodes: list[str] = field(default_factory=list)
    source: str = "community"
    provenance: str = ""  # 'community:cache' | 'community:network' | 'community:unavailable' | ''

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "category": self.category,
            "text": self.text,
            "required_nodes": list(self.required_nodes),
            "source": self.source,
            "provenance": self.provenance,
        }


class CommunityTemplateSearch:
    """Search the community index for templates relevant to a query.

    IMPORTANT LIMITATION: the public index (custom-node-list.json) contains NO
    node-class ids and NO workflow ids.  Each entry only has author/title/reference/
    files/install_type/description.  The endpoints node_list.json,
    workflow_templates.json and templates.json all return HTTP 404.

    Because the index does not declare a pack's required node classes, the
    **only honest way** to estimate node coverage is to attribute the pod's
    *real* installed node names to each pack, using token overlap between the
    pack's ``title`` (from the JSON) and the node class names (from the pod's
    ``/object_info``, passed in by the caller).

    Example: pack title ``'ComfyUI-IPAdapter'`` + installed node
    ``'IPAdapterApply'`` share the token ``'ipadapter'``, so
    ``'IPAdapterApply'`` is attributed to that pack.

    ``DiscoveredTemplate.required_nodes`` therefore contains exactly those
    attributed nodes.  For a pack with no attributed nodes the list is empty --
    that is the honest truth (we do not know its real node list), NOT a
    fabricated empty set.

    Ranking: node coverage DESC first, then lexical relevance to the intent DESC.
    """

    def __init__(self, downloader: CommunityIndexDownloader | None = None) -> None:
        self._downloader = downloader

    def _get_downloader(self) -> CommunityIndexDownloader:
        if self._downloader is not None:
            return self._downloader
        return CommunityIndexDownloader()

    @staticmethod
    def _lexical_relevance(query: str, title: str, description: str) -> float:
        """Overlap of query tokens with title+description tokens, normalised."""
        query_tokens = tokenize(query)
        if not query_tokens:
            return 0.0
        hay_tokens = tokenize(title + " " + description)
        if not hay_tokens:
            return 0.0
        return len(query_tokens & hay_tokens) / len(query_tokens)

    def search(
        self,
        query: str,
        installed_nodes: set[str] | Iterable[str] | None = None,
        *,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search the community index for templates relevant to ``query``.

        Never raises.  If the index is unavailable returns an empty list.
        The ``output_kind`` field is NOT part of the public index and is
        therefore ignored (no fabricated filter).
        """
        # Resolve installed nodes to a set for O(1) lookups. Un valor absurdo
        # (no iterable) degrada a "sin info de pod", nunca lanza.
        try:
            nodes_set: set[str] = set(installed_nodes) if installed_nodes else set()
        except TypeError:
            nodes_set = set()

        # Fetch the index (NEVER raises).
        downloader = self._get_downloader()
        index_result = downloader.fetch()
        if not index_result.entries:
            return []

        # Map the fetch source to a provenance label visible to callers.
        if index_result.source == "network":
            provenance = "community:network"
        elif index_result.source == "cache":
            provenance = "community:cache"
        else:
            provenance = "community:unavailable"

        # Score every entry.
        results: list[SearchResult] = []
        for entry in index_result.entries:
            relevance = self._lexical_relevance(query, entry.title, entry.description)
            matched = attribute_nodes(entry.title, nodes_set) if nodes_set else []
            coverage = len(matched)
            results.append(
                SearchResult(
                    template=DiscoveredTemplate(
                        template_id=entry.entry_id,
                        name=entry.title,
                        category=entry.install_type,
                        text=entry.title + ". " + entry.description,
                        required_nodes=list(matched),
                        source="community",
                        provenance=provenance,
                    ),
                    relevance=relevance,
                    coverage=coverage,
                    matched_nodes=matched,
                    installed_pack=coverage > 0,
                )
            )

        # Honest contract: return entries with coverage>0 OR relevance>0,
        # ordered by (coverage desc, relevance desc).  Capped at limit.
        filtered = [r for r in results if r.coverage > 0 or r.relevance > 0]
        filtered.sort(key=lambda r: (r.coverage, r.relevance), reverse=True)
        return filtered[:limit]

    def search_templates(
        self,
        query: str,
        installed_nodes: set[str] | Iterable[str] | None = None,
        *,
        limit: int = 10,
    ) -> list[DiscoveredTemplate]:
        """Convenience wrapper returning just the DiscoveredTemplate list."""
        return [
            r.template
            for r in self.search(query, installed_nodes=installed_nodes, limit=limit)
        ]

    def discover(
        self,
        query: str,
        *,
        output_kind: str,
        limit: int,
        installed_nodes: set[str] | Iterable[str] | None = None,
    ) -> list[DiscoveredTemplate]:
        """Satisfy the ``DiscoveryProvider`` protocol.

        ``output_kind`` is accepted but the public index has no output_kind
        field, so it is intentionally ignored (no fabricated filter).

        ``installed_nodes`` is forwarded to ``search_templates`` so that
        community packs can attribute their required node classes from the
        pod's actual installed nodes.
        """
        return self.search_templates(
            query, installed_nodes=installed_nodes, limit=limit
        )
