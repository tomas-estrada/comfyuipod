"""Cliente HTTP stdlib endurecido para adquisición (sin dependencias nuevas).

Propiedades de seguridad/operación:
- Allowlist estricta de hosts + https obligatorio; los redirects a hosts fuera
  de la allowlist se rechazan (anti-SSRF/redirección a redes internas).
- User-Agent identificable configurable.
- Rate limit por host (intervalo mínimo entre peticiones).
- Backoff exponencial con tope, respeto de Retry-After (tope), máximo de
  reintentos y circuit breaker por host: un 429/403 abre el circuito y suspende
  esa fuente durante el cooldown sin destruir lo ya descargado.
- Descarga reanudable con Range (.part), verificación de hash sha256/sha1,
  MIME por firma y tamaño máximo antes de promover el archivo a su destino.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class HttpError(Exception):
    """Error HTTP con status (None si fue de red)."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RateLimitedError(HttpError):
    def __init__(self, url: str, retry_after_s: float | None) -> None:
        super().__init__(f"429 rate limited: {url}", status=429, url=url)
        self.retry_after_s = retry_after_s


class ForbiddenError(HttpError):
    def __init__(self, url: str, detail: str = "") -> None:
        super().__init__(f"403 forbidden: {url} {detail}", status=403, url=url)


class CircuitOpenError(HttpError):
    def __init__(self, host: str, retry_in_s: float) -> None:
        super().__init__(
            f"circuit abierto para {host}; reintentar en {retry_in_s:.0f}s", url=host
        )
        self.host = host
        self.retry_in_s = retry_in_s


class FileTooLargeError(HttpError):
    def __init__(self, url: str, max_bytes: int) -> None:
        super().__init__(f"descarga excede el máximo permitido ({max_bytes} bytes): {url}")
        self.max_bytes = max_bytes


class HashMismatchError(HttpError):
    def __init__(self, url: str, expected: str, actual: str) -> None:
        super().__init__(f"hash no coincide para {url}: esperado {expected}, obtenido {actual}")
        self.expected = expected
        self.actual = actual


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self._allowed = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        host = host_of(newurl)
        if host not in self._allowed:
            raise ForbiddenError(newurl, "redirect a host fuera de la allowlist")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    """Cliente con allowlist, rate limit, backoff, Retry-After y circuit breaker."""

    def __init__(
        self,
        user_agent: str,
        allowed_hosts: list[str],
        *,
        timeout_s: float = 30.0,
        min_interval_s: float = 0.35,
        max_retries: int = 4,
        backoff_base_s: float = 2.0,
        max_retry_after_s: float = 120.0,
        circuit_failures: int = 3,
        circuit_cooldown_s: float = 300.0,
        max_response_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.user_agent = user_agent
        self.allowed_hosts = frozenset(allowed_hosts)
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.max_retry_after_s = max_retry_after_s
        self.circuit_failures = circuit_failures
        self.circuit_cooldown_s = circuit_cooldown_s
        self.max_response_bytes = max_response_bytes
        self._last_request: dict[str, float] = {}
        self._circuit: dict[str, dict[str, float]] = {}
        self._throttle_lock = threading.Lock()
        self._opener = urllib.request.build_opener(
            _AllowlistRedirectHandler(self.allowed_hosts)
        )

    # -- guards ---------------------------------------------------------------

    def _validate_url(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https":
            raise HttpError(f"sólo se permiten URLs https: {url}", url=url)
        host = parts.hostname or ""
        if host not in self.allowed_hosts:
            raise ForbiddenError(url, "host fuera de la allowlist")
        return url

    def _throttle(self, host: str) -> None:
        with self._throttle_lock:
            last = self._last_request.get(host, 0.0)
            wait = self.min_interval_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()

    def _check_circuit(self, host: str) -> None:
        state = self._circuit.get(host)
        if state and state.get("open_until", 0.0) > time.monotonic():
            raise CircuitOpenError(host, state["open_until"] - time.monotonic())

    def _record_failure(self, host: str) -> None:
        state = self._circuit.setdefault(host, {"failures": 0.0, "open_until": 0.0})
        state["failures"] = state.get("failures", 0.0) + 1
        if state["failures"] >= self.circuit_failures:
            state["open_until"] = time.monotonic() + self.circuit_cooldown_s

    def _record_success(self, host: str) -> None:
        self._circuit[host] = {"failures": 0.0, "open_until": 0.0}

    # -- peticiones -------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> bytes:
        self._validate_url(url)
        if params:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + sep + urllib.parse.urlencode(params)
        host = host_of(url)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._check_circuit(host)
            self._throttle(host)
            req = urllib.request.Request(
                url,
                method=method,
                headers={"User-Agent": self.user_agent, **(headers or {})},
            )
            try:
                with self._opener.open(req, timeout=timeout_s or self.timeout_s) as resp:
                    data = resp.read(self.max_response_bytes + 1)
                    if len(data) > self.max_response_bytes:
                        raise FileTooLargeError(url, self.max_response_bytes)
                    self._record_success(host)
                    return data
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read(4096)
                except Exception:  # noqa: BLE001
                    pass
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait_s = min(
                        float(retry_after) if retry_after else self.backoff_base_s * (2**attempt),
                        self.max_retry_after_s,
                    )
                    last_error = RateLimitedError(url, wait_s)
                elif exc.code == 403:
                    last_error = ForbiddenError(url, body.decode("utf-8", "replace")[:200])
                else:
                    last_error = HttpError(
                        f"HTTP {exc.code} en {url}: {body.decode('utf-8', 'replace')[:200]}",
                        status=exc.code,
                        url=url,
                    )
                self._record_failure(host)
                if exc.code == 403:
                    break  # prohibido: no reintentar, el circuito ya quedó abierto
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = HttpError(f"error de red en {url}: {exc}", url=url)
                self._record_failure(host)
            if attempt < self.max_retries:
                if isinstance(last_error, RateLimitedError) and last_error.retry_after_s:
                    time.sleep(last_error.retry_after_s)
                else:
                    time.sleep(min(self.backoff_base_s * (2**attempt), 30.0))
        raise last_error if last_error else HttpError(f"falló {url}")

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        data = self.request("GET", url, params=params, headers={"Accept": "application/json"})
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"respuesta no JSON de {url}: {exc}", url=url) from exc

    # -- descarga reanudable ------------------------------------------------------

    def download(
        self,
        url: str,
        dest: Path,
        *,
        sha256: str | None = None,
        sha1: str | None = None,
        max_bytes: int | None = None,
        resume: bool = True,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Descarga a `dest` (vía `dest.part`) con reanudación y verificación.

        Devuelve {'bytes', 'sha256', 'resumed', 'from_cache', 'url'}.
        Lanza FileTooLargeError/HashMismatchError y deja el .part para reanudar.
        """
        self._validate_url(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        existing = part.stat().st_size if (resume and part.is_file()) else 0
        if dest.is_file() and not part.is_file():
            # ya promovido: verificar hash si se dio; siempre devolver sha256 real
            digest256 = _hash_file(dest, "sha256")
            expected = sha256 or sha1
            if expected:
                algo = "sha256" if sha256 else "sha1"
                actual = _hash_file(dest, algo)
                if actual != expected.lower():
                    dest.unlink(missing_ok=True)
                    raise HashMismatchError(url, expected, actual)
            return {
                "bytes": dest.stat().st_size,
                "sha256": digest256,
                "resumed": False,
                "from_cache": True,
                "url": url,
            }
        headers: dict[str, str] = {}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        host = host_of(url)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._check_circuit(host)
            self._throttle(host)
            req = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent, **headers}
            )
            try:
                with self._opener.open(req, timeout=timeout_s or self.timeout_s) as resp:
                    status = getattr(resp, "status", 200)
                    mode = "ab" if (existing and status == 206) else "wb"
                    if existing and status == 200:
                        existing = 0
                        mode = "wb"
                    h256 = hashlib.sha256()
                    h1 = hashlib.sha1()
                    if existing:
                        # re-sembrar los hashes con el contenido ya descargado
                        with part.open("rb") as prev:
                            for chunk in iter(lambda: prev.read(1024 * 1024), b""):
                                h256.update(chunk)
                                h1.update(chunk)
                    total = existing
                    with part.open(mode) as fh:
                        while True:
                            chunk = resp.read(1024 * 512)
                            if not chunk:
                                break
                            total += len(chunk)
                            if max_bytes is not None and total > max_bytes:
                                fh.close()
                                part.unlink(missing_ok=True)
                                raise FileTooLargeError(url, max_bytes)
                            fh.write(chunk)
                            h256.update(chunk)
                            h1.update(chunk)
                    self._record_success(host)
                    digest256 = h256.hexdigest()
                    digest1 = h1.hexdigest()
                    if sha256 and digest256 != sha256.lower():
                        raise HashMismatchError(url, sha256, digest256)
                    if sha1 and not sha256 and digest1 != sha1.lower():
                        raise HashMismatchError(url, sha1, digest1)
                    part.replace(dest)
                    return {
                        "bytes": total,
                        "sha256": digest256,
                        "resumed": existing > 0,
                        "from_cache": False,
                        "url": url,
                    }
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and dest.is_file():
                    # el .part ya tenía el archivo completo
                    self._record_success(host)
                    actual = _hash_file(dest, "sha256")
                    return {"bytes": dest.stat().st_size, "sha256": actual,
                            "resumed": True, "from_cache": True, "url": url}
                last_error = HttpError(
                    f"HTTP {exc.code} descargando {url}", status=exc.code, url=url
                )
                self._record_failure(host)
                if exc.code == 403:
                    raise ForbiddenError(url) from exc
                if exc.code == 429:
                    ra = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(min(float(ra) if ra else self.backoff_base_s * (2**attempt),
                                   self.max_retry_after_s))
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = HttpError(f"error de red descargando {url}: {exc}", url=url)
                self._record_failure(host)
            if attempt < self.max_retries:
                time.sleep(min(self.backoff_base_s * (2**attempt), 30.0))
        raise last_error if last_error else HttpError(f"falló la descarga de {url}")


def _hash_file(path: Path, algo: str, *, return_sha1: bool = False) -> Any:
    if algo == "sha256":
        h = hashlib.sha256()
    else:
        h = hashlib.sha1()
    h1 = hashlib.sha1() if return_sha1 else None
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            if h1 is not None:
                h1.update(chunk)
    if return_sha1:
        return h.hexdigest(), h1.hexdigest()
    return h.hexdigest()
