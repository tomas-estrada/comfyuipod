"""Tests puros (CPU, sin GPU/red/bases de datos) de `perception.store`.

Cubre los huecos que `test_perception_index_store.py` y `test_m3_ml_backends.py`
NO cubren de `pack_float32_vector` (L136), `unpack_float32_vector` (L151) y
`cosine_similarity` (L161): truncado antes de normalizar, umbral de norma,
entradas no finitas, límites de precisión float32, largos distintos, entradas
tupla y las ramas de guarda de `unpack`. Ningún test abre SQLite ni importa
`sqlite_vec`; las ramas de comportamiento actual se anclan con aserciones
explícitas (los "bugs" se documentan sin tocar código de producción).
"""

from __future__ import annotations

import math

import pytest

from comfy_studio.perception.store import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    cosine_similarity,
    pack_float32_vector,
    unpack_float32_vector,
)

# ---------------------------------------------------------------------------
# pack_float32_vector (store.py L136-148)
# ---------------------------------------------------------------------------


def test_pack_trunca_antes_de_normalizar() -> None:
    # L142 recorta `vec[:dim]` ANTES de calcular la norma (L145): el 100 final
    # no contamina la normalización (si se normalizara primero el primer
    # componente sería ~0.03 en vez de 0.6).
    blob = pack_float32_vector([3.0, 4.0, 0.0, 0.0, 100.0], 4)
    assert len(blob) == 4 * 4
    rt = unpack_float32_vector(blob, 4)
    assert rt == pytest.approx([0.6, 0.8, 0.0, 0.0], abs=1e-6)


def test_pack_vector_cero_no_divide_por_cero() -> None:
    # L145-146: la norma 0 NO supera 1e-9, así que no se divide; sin esa
    # guarda sería 0/0 = nan en cada componente.
    rt = unpack_float32_vector(pack_float32_vector([0.0] * 4, 4), 4)
    assert rt == [0.0, 0.0, 0.0, 0.0]
    assert not any(math.isnan(v) for v in rt)


def test_pack_umbral_de_norma_1e_9() -> None:
    # Por debajo del umbral de L146 (1e-9) se guarda SIN normalizar...
    below = unpack_float32_vector(pack_float32_vector([1e-10, 0.0], 2), 2)
    assert below == pytest.approx([1e-10, 0.0], rel=1e-6)
    # ...por encima se normaliza a unidad.
    above = unpack_float32_vector(pack_float32_vector([1e-8, 0.0], 2), 2)
    assert above == pytest.approx([1.0, 0.0], abs=1e-7)


def test_pack_acepta_tuplas_identico_a_lista() -> None:
    # L136 declara `list[float] | tuple[float, ...]`; el resultado debe ser
    # byte a byte idéntico para tupla y lista.
    assert pack_float32_vector((3.0, 4.0), 8) == pack_float32_vector([3.0, 4.0], 8)
    rt = unpack_float32_vector(pack_float32_vector((3.0, 4.0), 8), 8)
    assert rt == pytest.approx([0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], abs=1e-6)


def test_pack_preserva_signo_de_componentes_negativos() -> None:
    rt = unpack_float32_vector(pack_float32_vector([-3.0, 4.0], 2), 2)
    assert rt == pytest.approx([-0.6, 0.8], abs=1e-6)
    assert rt[0] < 0.0 < rt[1]


def test_pack_limited_precision_float32_en_roundtrip() -> None:
    # L148 empaqueta en float32: el roundtrip NO reproduce el float64.
    rt = unpack_float32_vector(pack_float32_vector([1.0, 1.0], 2), 2)
    float64 = 1.0 / math.sqrt(2.0)
    # redondeo IEEE-754 float32 determinista (round-half-even) de 1/sqrt(2)
    assert rt[0] == 0.7071067690849304
    assert rt[0] != float64
    assert rt[0] == pytest.approx(float64, abs=1e-7)
    # valores no representables en binario: error < ~1e-7 (eps float32)
    rt3 = unpack_float32_vector(pack_float32_vector([0.1, 0.2, 0.3], 3), 3)
    expected = [x / math.sqrt(0.01 + 0.04 + 0.09) for x in (0.1, 0.2, 0.3)]
    assert rt3 == pytest.approx(expected, rel=1e-6)
    assert rt3 != expected  # jamás igualdad exacta con el float64


def test_pack_dim_cero_da_blob_vacio() -> None:
    # Caso degenerate sin guarda explícita: L142 deja [], la norma es 0 y
    # L148 hace `struct.pack("0f")` -> b"" (no lanza).
    assert pack_float32_vector([], 0) == b""
    assert pack_float32_vector([1.0, 2.0, 3.0], 0) == b""
    assert unpack_float32_vector(b"", 0) == []


def test_pack_acepta_no_finitos_y_produce_nan() -> None:
    # COMPORTAMIENTO ACTUAL (posible bug, store.py L145-147): no hay
    # comprobación de finiteness. NaN no supera la guarda de L146
    # (`nan > 1e-9` es False) y se persiste tal cual; Inf sí entra en la
    # división y Inf/Inf = nan. `validate_embedding_dim` (L120) solo valida
    # vacío/longitud, así que un embedding no finito llegaría a la BD
    # (L303/L516/L657) y de ahí a las búsquedas.
    rt_nan = unpack_float32_vector(pack_float32_vector([math.nan, 0.0], 2), 2)
    assert math.isnan(rt_nan[0])
    assert rt_nan[1] == 0.0
    rt_inf = unpack_float32_vector(pack_float32_vector([math.inf, 0.0], 2), 2)
    assert math.isnan(rt_inf[0])  # Inf normalizado -> Inf/Inf = nan
    assert rt_inf[1] == 0.0
    rt_ninf = unpack_float32_vector(pack_float32_vector([-math.inf, 0.0], 2), 2)
    assert math.isnan(rt_ninf[0])
    assert rt_ninf[1] == 0.0


# ---------------------------------------------------------------------------
# unpack_float32_vector (store.py L151-158)
# ---------------------------------------------------------------------------


def test_unpack_blob_vacio_devuelve_ceros() -> None:
    # L156: `not blob` -> ceros, sin excepción.
    assert unpack_float32_vector(b"", 4) == [0.0, 0.0, 0.0, 0.0]


def test_unpack_tamano_incorrecto_devuelve_ceros() -> None:
    # L156: cualquier longitud != dim*4 -> ceros silenciosos (ni 1 byte de
    # más ni 1 de menos pasa). Incluye la deriva real de dimensión: un blob
    # empaquetado para FACE (512) leído como CLIP (768) devuelve ceros.
    assert unpack_float32_vector(b"\x00" * 15, 4) == [0.0] * 4
    assert unpack_float32_vector(b"\x00" * 17, 4) == [0.0] * 4
    blob512 = pack_float32_vector([1.0, 2.0, 3.0], FACE_EMBED_DIM)
    assert len(blob512) == FACE_EMBED_DIM * 4
    assert unpack_float32_vector(blob512, CLIP_EMBED_DIM) == [0.0] * CLIP_EMBED_DIM


def test_unpack_longitud_valida_no_valida_el_contenido() -> None:
    # COMPORTAMIENTO ACTUAL (store.py L158): solo se valida la longitud;
    # bytes con representación float32 inválida (NaN en binario) pasan sin
    # error ni aviso. Un blob corrupto de tamaño correcto se devolvería como
    # si fuera un embedding legítimo.
    rt = unpack_float32_vector(b"\xff\xff\xff\x7f" * 2, 2)
    assert len(rt) == 2
    assert all(math.isnan(v) for v in rt)


# ---------------------------------------------------------------------------
# cosine_similarity (store.py L161-171)
# ---------------------------------------------------------------------------


def test_cosine_vec_b_vacio_devuelve_cero() -> None:
    # L163: cubre el segundo operando `not vec_b` (el test existente solo
    # cubre el primero, con vec_a vacío).
    assert cosine_similarity([1.0, 0.0], []) == 0.0
    assert cosine_similarity([], []) == 0.0


def test_cosine_norma_cero_en_vec_b_devuelve_cero() -> None:
    # L169: cubre `norm_b <= 1e-9` con vec_a no nulo (el test existente solo
    # alcanza el lado `norm_a`).
    assert cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0


def test_cosine_escala_unidad_y_signo() -> None:
    # Dirección idéntica sin vectores unitarios -> 1.0; opuestos -> -1.0
    # (la cara negativa del rango no estaba cubierta; el existente solo
    # probaba 1.0 y 0.0).
    assert cosine_similarity([2.0, 0.0], [5.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0
    # empaquetados escalados: pack normaliza ambos a la misma dirección
    a = unpack_float32_vector(pack_float32_vector([1.0, 2.0, 3.0], 8), 8)
    b = unpack_float32_vector(pack_float32_vector([2.0, 4.0, 6.0], 8), 8)
    assert cosine_similarity(a, b) == 1.0


def test_cosine_angulo_no_trivial_dentro_del_rango() -> None:
    # Rama general de L171: resultado estrictamente dentro de (-1, 1),
    # no exactamente 0.0 ni 1.0 como en los tests existentes.
    assert cosine_similarity([1.0, 0.0], [1.0, 1.0]) == pytest.approx(
        math.sqrt(0.5), rel=1e-12
    )
    assert cosine_similarity([0.3, 0.4], [0.6, 0.8]) == pytest.approx(1.0)


def test_cosine_reclama_el_ratio_de_redondeo_mayor_que_uno() -> None:
    # L171: con estos valores el cociente float64 SIN reclamar excede 1.0
    # (error de redondeo de la raíz); la función debe devolver 1.0 exacto.
    a = [0.1, 0.1, 0.3]
    n = len(a)
    dot = sum(a[i] * a[i] for i in range(n))
    norm = math.sqrt(dot)
    raw = dot / (norm * norm)
    assert raw > 1.0  # ancla: sin el clamp de L171 el valor superaría 1.0
    assert cosine_similarity(a, a) == 1.0


def test_cosine_largos_distintos_usa_solo_el_prefijo() -> None:
    # COMPORTAMIENTO ACTUAL (posible bug, store.py L165-168): producto
    # escalar y normas se calculan SOBRE los primeros `min(len)` elementos;
    # los componentes extra del vector más largo se ignoran por completo
    # (tampoco entran en su norma). El coseno completo de [1,1] vs [1] es
    # 1/sqrt(2) ~= 0.7071, pero devuelve 1.0.
    full = 1.0 / math.sqrt(2.0)
    assert cosine_similarity([1.0, 1.0], [1.0]) == 1.0
    assert cosine_similarity([1.0], [1.0, 1.0]) == 1.0
    # con intersección completa, el mismo vector da el coseno esperado
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 1.0]) == pytest.approx(
        full, rel=1e-12
    )


def test_cosine_nan_devuelve_similitud_perfecta() -> None:
    # BUG ACTUAL (store.py L169 y L171): `nan <= 1e-9` es False, y
    # `min(1.0, nan)` devuelve 1.0 (Python conserva el primer argumento
    # cuando la comparación es False), así que un NaN puntúa similitud
    # PERFECTA en vez de rechazarse. Alcance: validate_embedding_dim (L120)
    # no valida finiteness, pack persiste NaN (test anterior), la búsqueda
    # llama a cosine_similarity (L490/L750) y face_index.py L316 usa el
    # umbral 0.60: una detección NaN casaría con la primera identidad.
    assert cosine_similarity([math.nan, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [math.nan, 0.0]) == 1.0
    # única vía donde un no finito sí se detecta: la guarda de L169 con
    # norma exactamente 0.0 (0.0 <= 1e-9 es True incluso con NaN en el otro)
    assert cosine_similarity([0.0, 0.0], [math.nan, 1.0]) == 0.0


def test_cosine_infinito_devuelve_similitud_perfecta() -> None:
    # BUG ACTUAL (mismo mecanismo que el test anterior): Inf/Inf = nan en
    # L171 -> min(1.0, nan) = 1.0. Incluso el caso "ortogonal" [inf,0] vs
    # [0,1] devuelve 1.0 porque inf*0 = nan en el producto escalar (L166).
    assert cosine_similarity([math.inf, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([-math.inf, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([math.inf, 0.0], [0.0, 1.0]) == 1.0


def test_cosine_magnitudes_tiny_underflow_a_cero() -> None:
    # Límite numérico de L166-L168: (1e-200)^2 = 1e-400 subdesborda a 0.0
    # en float64, la norma resulta 0.0 y L169 devuelve 0.0 incluso para
    # vectores idénticos (en vez de 1.0).
    tiny = [1e-200, 1e-200]
    assert cosine_similarity(tiny, tiny) == 0.0
