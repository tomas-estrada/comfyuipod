"""Esquemas Pydantic y composici\u00f3n determinista del estudio de anuncios.

El guion se compone con un **composer determinista** a partir del brief (sin
LLM): es reproducible, testeable sin claves y honesto (`generator="template"`).
El agente LLM puede refinar el guion despues via chat; el storyboard deriva del
guion con una asignaci\u00f3n determinista de assets de producto a los planos.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AD_DOC_SCHEMA = "comfy_studio.ad.v1"
AD_SCRIPT_SCHEMA = "comfy_studio.ad_script.v1"
AD_STORYBOARD_SCHEMA = "comfy_studio.ad_storyboard.v1"

AD_TONES: tuple[str, ...] = (
    "profesional",
    "energico",
    "cercano",
    "inspirador",
    "humoristico",
)

MAX_AD_SHOTS = 12
MIN_SCENE_DURATION_S = 2.0


class AdBrief(BaseModel):
    """Brief estructurado de un anuncio de producto."""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=1, max_length=120)
    key_message: str = Field(min_length=1, max_length=500)
    audience: str = Field(min_length=1, max_length=200)
    tone: Literal["profesional", "energico", "cercano", "inspirador", "humoristico"] = (
        "profesional"
    )
    target_duration_s: float = Field(default=15.0, ge=5.0, le=60.0)
    cta: str = Field(min_length=1, max_length=120)
    language: str = Field(default="es", pattern=r"^[a-z]{2}(-[A-Za-z]{2})?$")
    product_asset_ids: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("product_name", "key_message", "audience", "cta")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("no puede estar vacío")
        return cleaned

    def clean(self) -> AdBrief:
        return AdBrief(
            product_name=self.product_name.strip(),
            key_message=self.key_message.strip(),
            audience=self.audience.strip(),
            tone=self.tone,
            target_duration_s=self.target_duration_s,
            cta=self.cta.strip(),
            language=self.language.strip().lower(),
            product_asset_ids=[a.strip() for a in self.product_asset_ids if a.strip()],
        )


class AdScene(BaseModel):
    """Una escena del guion del anuncio."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    role: Literal["hook", "problema", "solucion", "cta"]
    duration_s: float = Field(ge=MIN_SCENE_DURATION_S, le=60.0)
    headline: str = Field(max_length=140)
    voiceover: str = Field(max_length=600)
    visual_prompt: str = Field(max_length=800)
    on_screen_text: str = Field(max_length=140)


class AdScript(BaseModel):
    """Guion de anuncio generado a partir del brief."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=AD_SCRIPT_SCHEMA, alias="schema")
    brief: AdBrief
    scenes: list[AdScene] = Field(min_length=3, max_length=MAX_AD_SHOTS)
    total_duration_s: float
    cta: str
    language: str
    generator: Literal["template"] = "template"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class AdShot(BaseModel):
    """Un plano del storyboard: duraci\u00f3n, prompt visual, texto y voz."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    duration_s: float = Field(gt=0.0, le=60.0)
    visual_prompt: str = Field(max_length=800)
    on_screen_text: str = Field(max_length=140)
    voice_line: str = Field(max_length=600)
    asset_id: str | None = None


class AdStoryboard(BaseModel):
    """Storyboard derivado del guion, listo para montar variantes."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=AD_STORYBOARD_SCHEMA, alias="schema")
    ad_id: str
    shots: list[AdShot] = Field(min_length=1, max_length=MAX_AD_SHOTS)
    total_duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# Composer determinista
# ---------------------------------------------------------------------------

_ROLE_SHARE = {"hook": 0.25, "problema": 0.25, "solucion": 0.35, "cta": 0.15}


def _quantize(value: float) -> float:
    return max(MIN_SCENE_DURATION_S, round(value * 2) / 2)


def compose_script(brief: AdBrief) -> AdScript:
    """Compone un guion de 4 escenas a partir del brief (determinista, sin LLM)."""
    b = brief.clean()
    total = float(b.target_duration_s)
    roles = ("hook", "problema", "solucion", "cta")
    durations = [_quantize(total * _ROLE_SHARE[r]) for r in roles]
    # Ajustar el ultimo plano para que la suma sea exactamente el objetivo.
    diff = round(total - sum(durations), 3)
    durations[-1] = _quantize(durations[-1] + diff)
    if durations[-1] < MIN_SCENE_DURATION_S:
        durations[-1] = MIN_SCENE_DURATION_S

    product = b.product_name
    audience = b.audience
    tone = b.tone

    scenes: list[AdScene] = [
        AdScene(
            index=1,
            role="hook",
            duration_s=durations[0],
            headline=f"{product}",
            voiceover=(
                f"¿Y si pudiera conseguirlo con {product}? "
                f"Esto es para {audience}."
            ),
            visual_prompt=(
                f"Plano cinematográfico de apertura mostrando {product} en primer plano, "
                f"estilo {tone}, iluminación suave, movimiento de cámara lento hacia el producto"
            ),
            on_screen_text=product,
        ),
        AdScene(
            index=2,
            role="problema",
            duration_s=durations[1],
            headline="El problema del día a día",
            voiceover=(
                f"Cada día, {audience} se enfrenta al mismo reto. "
                f"Y las soluciones de siempre se quedan cortas."
            ),
            visual_prompt=(
                f"Escena cotidiana de {audience} con el problema en evidencia, "
                f"estilo {tone}, cámara en mano, luz natural"
            ),
            on_screen_text="¿Te suena?",
        ),
        AdScene(
            index=3,
            role="solucion",
            duration_s=durations[2],
            headline=b.key_message[:140],
            voiceover=b.key_message[:600],
            visual_prompt=(
                f"Plano del producto {product} en uso por {audience}, mostrando el beneficio: "
                f"{b.key_message} Estilo {tone}, cámara lenta sobre el detalle clave"
            ),
            on_screen_text=b.key_message[:140],
        ),
        AdScene(
            index=4,
            role="cta",
            duration_s=durations[3],
            headline=b.cta[:140],
            voiceover=f"{b.cta}",
            visual_prompt=(
                f"Packshot final de {product} con fondo limpio, estilo {tone}, "
                f"zoom-out suave y logo espacio reservado para el texto en pantalla"
            ),
            on_screen_text=b.cta[:140],
        ),
    ]
    return AdScript(
        brief=b,
        scenes=scenes,
        total_duration_s=round(sum(s.duration_s for s in scenes), 3),
        cta=b.cta,
        language=b.language,
    )


def build_storyboard(script: AdScript, *, ad_id: str) -> AdStoryboard:
    """Deriva el storyboard del guion y asigna assets de producto a los planos.

    Los `product_asset_ids` del brief se reparten en round-robin entre los
    planos (empezando por el plano de solución) para mantener la referencia del
    producto visible en las variantes montadas.
    """
    assets = list(script.brief.product_asset_ids)
    shots: list[AdShot] = []
    asset_cursor = 0
    for scene in script.scenes:
        asset_id: str | None = None
        if assets:
            asset_id = assets[asset_cursor % len(assets)]
            asset_cursor += 1
        shots.append(
            AdShot(
                index=scene.index,
                duration_s=scene.duration_s,
                visual_prompt=scene.visual_prompt,
                on_screen_text=scene.on_screen_text,
                voice_line=scene.voiceover,
                asset_id=asset_id,
            )
        )
    return AdStoryboard(
        ad_id=ad_id,
        shots=shots,
        total_duration_s=round(sum(s.duration_s for s in shots), 3),
    )


def ad_doc_dict(
    *,
    ad_id: str,
    project_id: str,
    brief: AdBrief,
    script: AdScript | None = None,
    storyboard: AdStoryboard | None = None,
    variants: list[dict[str, Any]] | None = None,
    created_at: str,
    updated_at: str,
    gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Documento de anuncio persistido en `project_meta` (clave `ad:<id>`)."""
    return {
        "schema": AD_DOC_SCHEMA,
        "id": ad_id,
        "project_id": project_id,
        "brief": brief.model_dump(mode="json"),
        "script": script.to_dict() if script else None,
        "storyboard": storyboard.to_dict() if storyboard else None,
        "variants": list(variants or []),
        "gate": gate or {"state": "drafting"},
        "created_at": created_at,
        "updated_at": updated_at,
    }
