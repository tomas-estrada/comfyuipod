"""Cliente de la API oficial de Wikimedia Commons (sin credenciales).

Usa consultas de categorías (con recursión limitada), búsqueda geogrática
(`list=geosearch`) y búsqueda textual en el namespace 6 (archivos), resuelve
redirecciones de categorías, pagina con `continue` y conserva identificadores.
`imageinfo` entrega url, size, sha1, mime, timestamp y extmetadata (licencia,
autor, DateTimeOriginal, GPS...). La fecha de captura (DateTimeOriginal) nunca
se confunde con la de subida (timestamp).
"""

from __future__ import annotations

import json
import re
from typing import Any

from comfy_studio.atlas.acquire.http import HttpClient

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_HOSTS = ["commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org"]

#: licencias aceptadas para redistribución y transformación en el atlas público.
_LICENSE_BAD = re.compile(
    r"\b(NC|ND)\b|NO DERIV|NON ?COMMERCIAL|NO COMERCIAL|ALL RIGHTS|TODOS LOS DERECHOS",
    re.IGNORECASE,
)


def license_verdict(short_name: str | None) -> tuple[bool, str]:
    """(permitida, licencia canónica) a partir de LicenseShortName."""
    raw = (short_name or "").strip()
    if not raw or raw.upper() in ("UNKNOWN", "NONE"):
        return False, raw or "UNKNOWN"
    u = re.sub(r"[-_]", " ", raw.upper())
    u = re.sub(r"\s+", " ", u).strip()
    if _LICENSE_BAD.search(u):
        return False, raw
    m = re.match(r"^CC BY SA ([1-4]\.0)", u) or re.match(r"^CC BY ([1-4]\.0)", u)
    if m:
        ver = m.group(1)
        return True, ("CC BY-SA " if "SA" in m.group(0) else "CC BY ") + ver
    if re.match(r"^CC0", u):
        return True, "CC0"
    if "PUBLIC DOMAIN" in u:
        return True, "Public domain"
    if re.match(r"^(CC PDM|OGL|OPEN DATA)", u):
        return True, u.title()
    return False, raw


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    import html as _html

    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


class CommonsClient:
    """API de Commons con paginación `continue` y consultas por lotes."""

    def __init__(self, http: HttpClient, *, thumb_width: int = 3072, max_pages: int = 40) -> None:
        self.http = http
        self.thumb_width = thumb_width
        self.max_pages = max_pages

    def api_all(
        self, base_params: dict[str, Any], *, max_pages: int | None = None
    ) -> dict[str, Any]:
        """Ejecuta la query acumulando todas las páginas (listas) vía continue."""
        params = dict(base_params)
        params.update({"action": "query", "format": "json", "formatversion": "2"})
        out: dict[str, Any] = {"query": {}}
        pages = 0
        cap = max_pages if max_pages is not None else self.max_pages
        while True:
            data = self.http.get_json(COMMONS_API, params=params)
            q = data.get("query", {})
            for key, val in q.items():
                if isinstance(val, list):
                    out["query"].setdefault(key, [])
                    seen = {json.dumps(x, sort_keys=True, default=str) for x in out["query"][key]}
                    for item in val:
                        k = json.dumps(item, sort_keys=True, default=str)
                        if k not in seen:
                            seen.add(k)
                            out["query"][key].append(item)
                elif isinstance(val, dict):
                    out["query"].setdefault(key, {}).update(val)
            cont = data.get("continue")
            if not cont or pages >= cap:
                break
            pages += 1
            params.update({k: v for k, v in cont.items() if k != "continue"})
        return out

    # -- consultas -------------------------------------------------------------

    def category_files(
        self,
        category: str,
        *,
        max_depth: int = 2,
        max_items: int = 1500,
        max_subcats: int = 12,
        max_pages: int = 6,
    ) -> list[str]:
        """Archivos de una categoría + subcategorías (recursión limitada)."""
        seen: set[str] = set()
        titles: list[str] = []
        subcats_walked = 0

        def walk(cat: str, depth: int) -> None:
            nonlocal subcats_walked
            if depth > max_depth or len(titles) >= max_items or subcats_walked >= max_subcats:
                return
            norm = cat if cat.lower().startswith("category:") else f"Category:{cat}"
            try:
                data = self.api_all(
                    {
                        "list": "categorymembers",
                        "cmtitle": norm,
                        "cmtype": "file|subcat",
                        "cmlimit": "500",
                    },
                    max_pages=max_pages,
                )
            except Exception:  # noqa: BLE001
                return
            for item in data.get("query", {}).get("categorymembers", []):
                title = item.get("title", "")
                if item.get("type") == "subcat" or "Category:" in title:
                    if depth + 1 <= max_depth and subcats_walked < max_subcats:
                        subcats_walked += 1
                        walk(title, depth + 1)
                elif title.startswith("File:") and title not in seen:
                    seen.add(title)
                    titles.append(title)
                    if len(titles) >= max_items:
                        return

        walk(category, 0)
        return titles

    def geosearch(
        self, lat: float, lon: float, radius_m: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        data = self.api_all(
            {
                "list": "geosearch",
                "gscoord": f"{lat}|{lon}",
                "gsradius": str(radius_m),
                "gsnamespace": "6",
                "gsrlimit": str(min(limit, 500)),
            }
        )
        return [
            {"title": x.get("title", ""), "lat": x.get("lat"), "lon": x.get("lon")}
            for x in data.get("query", {}).get("geosearch", [])
            if x.get("title", "").startswith("File:")
        ]

    def search_files(self, text: str, *, limit: int = 50) -> list[str]:
        data = self.api_all(
            {
                "list": "search",
                "srsearch": text,
                "srnamespace": "6",
                "srlimit": str(min(limit, 500)),
            }
        )
        return [x.get("title", "") for x in data.get("query", {}).get("search", [])
                if x.get("title", "").startswith("File:")]

    def imageinfo(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        """imageinfo en lotes de 50: url, size, sha1, mime, timestamp, extmetadata
        y coordenadas (prop=coordinates), más thumburl a thumb_width."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(titles), 50):
            batch = titles[i : i + 50]
            try:
                data = self.http.get_json(
                    COMMONS_API,
                    params={
                        "action": "query",
                        "format": "json",
                        "formatversion": "2",
                        "titles": "|".join(batch),
                        "prop": "imageinfo|coordinates",
                        "iiprop": "url|size|sha1|timestamp|mime|extmetadata",
                        "iiurlwidth": str(self.thumb_width),
                        "colimit": "50",
                    },
                )
            except Exception:  # noqa: BLE001
                continue
            for page in data.get("query", {}).get("pages", []):
                title = page.get("title", "")
                infos = page.get("imageinfo") or []
                if not infos:
                    continue
                ii = infos[0]
                coords = (page.get("coordinates") or [{}])[0]
                em = ii.get("extmetadata", {})

                em_local = em

                def meta(key: str, _em: dict = em_local) -> str:
                    return strip_html(_em.get(key, {}).get("value"))

                gps_lat = _parse_float(meta("GPSLatitude")) or coords.get("lat")
                gps_lon = _parse_float(meta("GPSLongitude")) or coords.get("lon")
                out[title] = {
                    "title": title,
                    "url": ii.get("url"),
                    "descriptionurl": ii.get("descriptionurl"),
                    "thumburl": ii.get("thumburl"),
                    "thumbwidth": ii.get("thumbwidth"),
                    "thumbheight": ii.get("thumbheight"),
                    "width": ii.get("width"),
                    "height": ii.get("height"),
                    "size": ii.get("size"),
                    "sha1": ii.get("sha1"),
                    "mime": ii.get("mime"),
                    "timestamp": ii.get("timestamp"),  # subida/última revisión
                    "license_short": meta("LicenseShortName"),
                    "license_url": strip_html(em.get("LicenseUrl", {}).get("value")),
                    "artist": meta("Artist"),
                    "credit": meta("Credit"),
                    "capture_date": meta("DateTimeOriginal"),
                    "description": meta("ImageDescription"),
                    "gps_lat": gps_lat,
                    "gps_lon": gps_lon,
                }
        return out


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def page_url(title: str) -> str:
    return "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")
