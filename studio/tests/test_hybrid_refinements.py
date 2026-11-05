"""Tests unitarios puros-CPU para refinamientos híbridos en highlights.py.

Cubre el comportamiento de apply_hybrid_refinements() y _snap_to_scene_boundary()
sin dependencias externas (GPU, red, descargas).

M2 (VAL-PERC-006): modo híbrido que ajusta cortes ya medidos usando:
- Señal acústica real (envolvente RMS por ventanas de 100 ms)
- Límites de escena detectados en keyframes

Los tests verifican el comportamiento real de la implementación:
- El refinamiento acústico nunca alarga un corte, solo lo acota (intersección con tramos de habla)
- El ajuste a escena, en cambio, mueve el borde al límite más cercano dentro de tolerancia,
  tanto hacia adentro como hacia AFUERA (puede alargar un poco el corte ya refinado)
- _snap_to_scene_boundary ajusta si el límite está dentro de 0.35s y NO si está fuera
- Cortes que quedarían < 0.2s tras refinamiento se descartan (no aparecen en la salida)
- Si TODOS se descartan, se retornan los cortes originales + reporte sin clave 'refined'
"""

from __future__ import annotations

from comfy_studio.edl.highlights import (
    _snap_to_scene_boundary,
    apply_hybrid_refinements,
)

# ---------------------------------------------------------------------------
# Tests para _snap_to_scene_boundary()
# ---------------------------------------------------------------------------


def test_snap_to_scene_boundary_ajusta_dentro_de_tolerancia():
    """Ajusta cuando el límite está dentro de 0.35s (tolerancia por defecto)."""
    value = 1.0
    boundaries = [1.2]
    snapped, hit = _snap_to_scene_boundary(value, boundaries)
    assert snapped == 1.2
    assert hit == 1.2


def test_snap_to_scene_boundary_no_ajusta_fuera_de_tolerancia():
    """No ajusta cuando el límite está fuera de 0.35s."""
    value = 1.0
    boundaries = [1.36]  # delta = 0.36 > 0.35
    snapped, hit = _snap_to_scene_boundary(value, boundaries)
    assert snapped == 1.0
    assert hit is None


def test_snap_to_scene_boundary_varios_limites_selecciona_el_mas_cercano():
    """Selecciona el límite más cercano dentro de la tolerancia."""
    value = 1.0
    boundaries = [0.7, 1.2, 1.5]  # deltas: 0.3, 0.2, 0.5
    snapped, hit = _snap_to_scene_boundary(value, boundaries)
    assert snapped == 1.2
    assert hit == 1.2


def test_snap_to_scene_boundary_sin_limites_devuelve_original():
    """Si no hay límites, devuelve el valor original y None."""
    value = 1.0
    boundaries = []
    snapped, hit = _snap_to_scene_boundary(value, boundaries)
    assert snapped == 1.0
    assert hit is None


def test_snap_to_scene_boundary_en_el_borde_de_la_tolerancia():
    """Ajusta con un delta justo por debajo de la tolerancia (0.349 < 0.35).

    No se fija el caso delta == 0.35 exacto: la comparación es `<=` sobre flotantes
    y `abs(1.35 - 1.0)` da `0.3500000000000001`, así que el borde exacto depende de
    la representación binaria. Este test fija el comportamiento un poco por dentro.
    """
    snapped, hit = _snap_to_scene_boundary(1.0, [1.349])
    assert snapped == 1.349
    assert hit == 1.349


def test_snap_to_scene_boundary_tolerancia_custom():
    """Respeta una tolerancia personalizada: ajusta dentro y NO ajusta fuera.

    Se eligen deltas claramente dentro (0.03) y claramente fuera (0.10) del umbral
    de 0.05 para no depender del borde de igualdad flotante, que es frágil.
    """
    # Dentro de la tolerancia custom: delta 0.03 <= 0.05 -> ajusta.
    snapped, hit = _snap_to_scene_boundary(1.0, [1.03], tolerance=0.05)
    assert snapped == 1.03
    assert hit == 1.03

    # Fuera de la tolerancia custom: delta 0.10 > 0.05 -> no ajusta.
    snapped, hit = _snap_to_scene_boundary(1.0, [1.10], tolerance=0.05)
    assert snapped == 1.0
    assert hit is None

    # El mismo límite de 1.10 sí entra con la tolerancia por defecto (0.35),
    # lo que confirma que el parámetro realmente gobierna el umbral.
    snapped, hit = _snap_to_scene_boundary(1.0, [1.10], tolerance=0.35)
    assert snapped == 1.10
    assert hit == 1.10


def test_snap_to_scene_boundary_mueve_hacia_atras():
    """El límite elegido puede ser ANTERIOR al instante (mueve hacia atrás).

    _snap_to_scene_boundary solo busca el más cercano en valor absoluto, así que
    puede devolver un instante menor. Ese resultado es el que permite que el ajuste
    de escena alargue un corte hacia afuera, a diferencia del refinamiento acústico.
    """
    snapped, hit = _snap_to_scene_boundary(1.0, [0.8])
    assert snapped == 0.8
    assert hit == 0.8
    assert snapped < 1.0


# ---------------------------------------------------------------------------
# Tests para apply_hybrid_refinements()
# ---------------------------------------------------------------------------


def test_acoustic_refinement_nunca_alarga_corte():
    """Refinamiento acústico nunca alarga un corte, solo lo acota."""
    cuts = [
        {"in_sec": 1.0, "out_sec": 3.0, "text": "corte1", "reason": "highlight"}
    ]
    acoustic = {"available": True, "spans": [{"start": 1.5, "end": 2.5}]}
    scene_boundaries = None

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    assert refined[0]["in_sec"] == 1.5
    assert refined[0]["out_sec"] == 2.5
    assert refined[0]["text"] == "corte1"
    assert refined[0]["reason"] == "highlight"

    # Duración refinada <= original
    dur_refined = refined[0]["out_sec"] - refined[0]["in_sec"]
    dur_original = 3.0 - 1.0
    assert dur_refined <= dur_original

    assert report["acoustic_available"] is True
    assert report["acoustic_span_count"] == 1
    assert report["scene_boundary_count"] == 0
    assert report["trimmed_by_acoustic"] == 1
    assert report["snapped_to_scene"] == 0
    assert report["dropped_too_short"] == 0
    assert report.get("refined") is True


def test_acoustic_refinement_sin_solapamiento_devuelve_original():
    """Si no hay solapamiento con tramos de habla, devuelve cortes originales."""
    cuts = [
        {"in_sec": 1.0, "out_sec": 3.0, "text": "corte2", "reason": "highlight"}
    ]
    acoustic = {"available": True, "spans": [{"start": 5.0, "end": 6.0}]}
    scene_boundaries = None

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    assert refined[0]["in_sec"] == 1.0
    assert refined[0]["out_sec"] == 3.0
    assert report["trimmed_by_acoustic"] == 0


def test_scene_snapping_aplica_dentro_de_tolerancia():
    """Ajusta cortes a límites de escena cuando están dentro de 0.35s.

    Los dos bordes se mueven, pero `snapped_to_scene` solo cuenta el ajuste de
    `in_sec`; el de `out_sec` se aplica sin incrementar el contador.
    """
    cuts = [
        {"in_sec": 1.0, "out_sec": 3.0, "text": "corte3", "reason": "highlight"}
    ]
    acoustic = {"available": False, "spans": []}
    scene_boundaries = [1.2, 2.8]

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    assert refined[0]["in_sec"] == 1.2
    assert refined[0]["out_sec"] == 2.8
    assert report["snapped_to_scene"] == 1  # solo in_sec incrementa el contador


def test_scene_snapping_puede_alargar_hacia_afuera():
    """El ajuste de escena puede alargar el corte moviendo el borde hacia afuera.

    Es la diferencia con el paso acústico (que solo acorta): aquí `in_sec` se
    adelanta de 1.0 a 0.8 porque ese límite está dentro de los 0.35s.
    """
    cuts = [
        {"in_sec": 1.0, "out_sec": 3.0, "text": "corte3b", "reason": "highlight"}
    ]
    acoustic = {"available": False, "spans": []}
    scene_boundaries = [0.8]

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    assert refined[0]["in_sec"] == 0.8
    assert refined[0]["out_sec"] == 3.0
    # Duración creció respecto a la original medida.
    assert refined[0]["out_sec"] - refined[0]["in_sec"] > 3.0 - 1.0
    assert report["snapped_to_scene"] == 1


def test_corte_muy_corto_se_descarta():
    """Corte que quedaría < 0.2s tras refinamiento se descarta.

    Cubre las dos ramas:
    - Sin supervivientes: se devuelven los cortes ORIGINALES y el reporte no
      lleva la clave 'refined' (fallback documentado en apply_hybrid_refinements).
    - Con algún superviviente: el corte corto desaparece de la salida.
    """
    corte_corto = {"in_sec": 1.0, "out_sec": 1.1, "text": "cortoCorto", "reason": "highlight"}

    # --- Rama A: único corte, todos se descartan -> se devuelven los originales.
    refined, report = apply_hybrid_refinements(
        [dict(corte_corto)],
        acoustic={"available": True, "spans": [{"start": 1.0, "end": 1.05}]},
        scene_boundaries=None,
    )
    assert len(refined) == 1  # se devuelve el corte original sin recortar
    assert refined[0]["in_sec"] == 1.0
    assert refined[0]["out_sec"] == 1.1
    assert report["dropped_too_short"] == 1
    assert report["trimmed_by_acoustic"] == 1
    assert "refined" not in report

    # --- Rama B: un corte corto + uno que sobrevive -> solo queda el largo.
    refined, report = apply_hybrid_refinements(
        [
            dict(corte_corto),
            {"in_sec": 2.0, "out_sec": 3.0, "text": "corteLargo", "reason": "highlight"},
        ],
        acoustic={
            "available": True,
            "spans": [{"start": 1.0, "end": 1.05}, {"start": 2.1, "end": 2.9}],
        },
        scene_boundaries=None,
    )
    assert [c["text"] for c in refined] == ["corteLargo"]
    assert refined[0]["in_sec"] == 2.1
    assert refined[0]["out_sec"] == 2.9
    assert report["dropped_too_short"] == 1
    assert report["trimmed_by_acoustic"] == 2
    assert report["refined"] is True


def test_todos_los_cortes_se_descarta_retorna_originales():
    """Si todos los cortes se descartan, retorna los originales + reporte."""
    cuts = [
        {"in_sec": 1.0, "out_sec": 1.15, "text": "corte5", "reason": "highlight"},
        {"in_sec": 2.0, "out_sec": 2.1, "text": "corte6", "reason": "highlight"},
    ]
    acoustic = {
        "available": True,
        "spans": [{"start": 1.0, "end": 1.1}, {"start": 2.0, "end": 2.05}],
    }
    scene_boundaries = None

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    # Fallback documentado: ningún corte sobrevive -> se devuelven los originales.
    assert refined == cuts
    assert len(refined) == 2
    assert refined[0]["in_sec"] == 1.0
    assert refined[0]["out_sec"] == 1.15
    assert refined[1]["in_sec"] == 2.0
    assert refined[1]["out_sec"] == 2.1

    assert report["dropped_too_short"] == 2
    assert report["trimmed_by_acoustic"] == 2
    assert "refined" not in report
    assert report["acoustic_available"] is True
    assert report["acoustic_span_count"] == 2


def test_acoustic_no_disponible_no_aplica_refinamiento():
    """Si acoustic no disponible, no aplica refinamiento y retorna originales."""
    cuts = [
        {"in_sec": 1.0, "out_sec": 3.0, "text": "corte7", "reason": "highlight"}
    ]
    acoustic = {"available": False, "spans": []}
    scene_boundaries = [1.5]

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    assert refined[0]["in_sec"] == 1.0
    assert refined[0]["out_sec"] == 3.0
    assert report["acoustic_available"] is False
    assert report["trimmed_by_acoustic"] == 0
    assert report["snapped_to_scene"] == 0


def test_scene_boundaries_vacio_no_aplica_snap():
    """Si scene_boundaries está vacío, no aplica snapping."""
    cuts = [
        {"in_sec": 1.0, "out_sec": 3.0, "text": "corte8", "reason": "highlight"}
    ]
    acoustic = {"available": False, "spans": []}
    scene_boundaries = []

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    assert refined[0]["in_sec"] == 1.0
    assert refined[0]["out_sec"] == 3.0
    assert report["scene_boundary_count"] == 0
    assert report["snapped_to_scene"] == 0


def test_refinamiento_combinado_acustico_y_escena():
    """Aplica refinamiento acústico y, sobre su resultado, el ajuste de escena.

    Se eligen límites a 0.1s del borde resultante del recorte acústico
    (1.5-4.5), dentro de los 0.35s de HYBRID_SCENE_SNAP_SEC, para que los dos
    pasos realmente encadenen: (1.5, 4.5) -> (1.6, 4.4).
    """
    cuts = [
        {"in_sec": 1.0, "out_sec": 5.0, "text": "corte9", "reason": "highlight"}
    ]
    acoustic = {"available": True, "spans": [{"start": 1.5, "end": 4.5}]}
    scene_boundaries = [1.6, 4.4]

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert len(refined) == 1
    # Acústico acota 1.0-5.0 a 1.5-4.5 y luego la escena alinea a 1.6-4.4.
    assert refined[0]["in_sec"] == 1.6
    assert refined[0]["out_sec"] == 4.4
    assert report["trimmed_by_acoustic"] == 1
    # Solo el ajuste de in_sec incrementa el contador (el de out_sec también ocurrió).
    assert report["snapped_to_scene"] == 1
    assert report["scene_boundary_count"] == 2
    assert report["dropped_too_short"] == 0


def test_multiple_cortes_parciales():
    """Múltiples cortes: cada uno se refina según sus propios tramos y límites.

    Solo corteA tiene un límite de escena dentro de tolerancia de su `in_sec`
    recortado (0.8 -> 1.0). Para corteB el límite de 4.0 queda a 0.8s de sus
    bordes (muy por encima de 0.35s), así que no aplica ningún snap y se
    conserva el recorte puramente acústico 3.2-4.8.
    """
    cuts = [
        {"in_sec": 0.5, "out_sec": 2.5, "text": "corteA", "reason": "highlight"},
        {"in_sec": 3.0, "out_sec": 5.0, "text": "corteB", "reason": "highlight"},
        {"in_sec": 6.0, "out_sec": 6.1, "text": "corteC", "reason": "highlight"},
    ]
    acoustic = {
        "available": True,
        "spans": [
            {"start": 0.8, "end": 2.2},
            {"start": 3.2, "end": 4.8},
            {"start": 6.0, "end": 6.05},
        ],
    }
    scene_boundaries = [1.0, 4.0]

    refined, report = apply_hybrid_refinements(
        cuts, acoustic=acoustic, scene_boundaries=scene_boundaries
    )

    assert [c["text"] for c in refined] == ["corteA", "corteB"]  # corteC se descarta

    # corteA: acústico 0.8-2.2 y escena mueve in de 0.8 a 1.0 (delta 0.2 < 0.35).
    assert refined[0]["in_sec"] == 1.0
    assert refined[0]["out_sec"] == 2.2

    # corteB: acústico 3.2-4.8; 4.0 está a 0.8s de ambos bordes -> sin snap.
    assert refined[1]["in_sec"] == 3.2
    assert refined[1]["out_sec"] == 4.8

    assert report["dropped_too_short"] == 1
    # Los tres cortes fueron recortados por señal acústica (incluido el descartado).
    assert report["trimmed_by_acoustic"] == 3
    # Solo corteA movió su in_sec.
    assert report["snapped_to_scene"] == 1
    assert report["refined"] is True
