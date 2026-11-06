"""ASR sin GPU: análisis acústico de WAV sintético y construcción de palabras por span.

Fija el comportamiento actual de `analyze_wav_acoustic_windows` (contrato 16-bit,
ventana de 0.10 s con suelo de 160 muestras, umbral RMS 0.025, corte por cambio
brusco de frecuencia) y de `_build_words_for_span` (reparto lineal de tiempos,
muletillas españolas, garante de duración mínima), usando WAVs deterministas
generados con el módulo estándar `wave`: sin ffmpeg, sin red, sin pesos, sin GPU.
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

import pytest

from comfy_studio.asr.whisperx import _build_words_for_span, analyze_wav_acoustic_windows

SAMPLE_RATE = 16_000


def _silence(duration_sec: float) -> array.array:
    """Silencio 16-bit de la duración indicada."""
    n = int(round(duration_sec * SAMPLE_RATE))
    return array.array("h", bytes(2 * n))


def _tone(freq_hz: float, duration_sec: float, *, amp: float = 0.5) -> array.array:
    """Seno 16-bit determinista (amplitud relativa a full scale)."""
    n = int(round(duration_sec * SAMPLE_RATE))
    out = array.array("h")
    for i in range(n):
        t = i / float(SAMPLE_RATE)
        val = int(amp * 32767.0 * math.sin(2.0 * math.pi * freq_hz * t))
        out.append(max(-32768, min(32767, val)))
    return out


def _write_wav(path: Path, samples: array.array, *, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())


def _interleave(left: array.array, right: array.array) -> array.array:
    out = array.array("h")
    for a, b in zip(left, right, strict=True):
        out.append(a)
        out.append(b)
    return out


# --------------------------------------------------------------------------
# analyze_wav_acoustic_windows: entradas inválidas -> tramo vacío sin ventanas
# --------------------------------------------------------------------------


def test_archivo_inexistente_devuelve_tramo_vacio(tmp_path: Path) -> None:
    result = analyze_wav_acoustic_windows(tmp_path / "no_existe.wav")
    assert result == {"duration_sec": 0.0, "speech_spans": []}
    assert "windows" not in result


def test_wav_corrupto_devuelve_tramo_vacio(tmp_path: Path) -> None:
    path = tmp_path / "corrupto.wav"
    path.write_bytes(b"esto no es un archivo wav valido")
    result = analyze_wav_acoustic_windows(path)
    assert result == {"duration_sec": 0.0, "speech_spans": []}
    assert "windows" not in result


def test_wav_sin_frames_devuelve_tramo_vacio(tmp_path: Path) -> None:
    path = tmp_path / "vacio.wav"
    _write_wav(path, array.array("h"))
    result = analyze_wav_acoustic_windows(path)
    assert result == {"duration_sec": 0.0, "speech_spans": []}
    assert "windows" not in result


def test_wav_8_bit_fuera_de_contrato_devuelve_tramo_vacio(tmp_path: Path) -> None:
    # El contrato es 16-bit (sampwidth == 2); otro ancho no se analiza.
    path = tmp_path / "8bit.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(SAMPLE_RATE))  # 1 s de silencio 8-bit
    result = analyze_wav_acoustic_windows(path)
    assert result == {"duration_sec": 0.0, "speech_spans": []}
    assert "windows" not in result


# --------------------------------------------------------------------------
# Ventanas: rejilla de 0.10 s (1600 muestras), suelo de 160 muestras
# --------------------------------------------------------------------------


def test_silencio_ventanas_cero_sin_tramos_de_voz(tmp_path: Path) -> None:
    path = tmp_path / "silencio.wav"
    _write_wav(path, _silence(1.0))
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.0
    assert result["speech_spans"] == []
    windows = result["windows"]
    assert len(windows) == 10
    assert windows[0] == {"start": 0.0, "end": 0.1, "rms": 0.0, "freq_hz": 0.0}
    assert windows[-1] == {"start": 0.9, "end": 1.0, "rms": 0.0, "freq_hz": 0.0}
    # Ventanas contiguas sin huecos ni solapes
    assert all(a["end"] == b["start"] for a, b in zip(windows, windows[1:], strict=False))


def test_window_sec_005_duplica_el_recuento(tmp_path: Path) -> None:
    path = tmp_path / "tono.wav"
    _write_wav(path, _tone(440.0, 1.0))
    result = analyze_wav_acoustic_windows(path, window_sec=0.05)
    windows = result["windows"]
    assert len(windows) == 20
    assert windows[1]["start"] == 0.05
    assert windows[1]["end"] == 0.1
    # 800 muestras = 22 ciclos exactos -> ZCR cuantizada a 430 Hz
    assert windows[1]["freq_hz"] == 430.0


def test_window_sec_minusculo_usa_suelo_de_160_muestras(tmp_path: Path) -> None:
    path = tmp_path / "tono.wav"
    _write_wav(path, _tone(440.0, 1.0))
    result = analyze_wav_acoustic_windows(path, window_sec=0.001)
    # int(16000 * 0.001) = 16 < 160 -> win_samples queda en el suelo de 160 muestras (0.01 s)
    windows = result["windows"]
    assert len(windows) == 100
    assert windows[0]["start"] == 0.0
    assert windows[0]["end"] == 0.01
    assert windows[1]["start"] == 0.01
    assert windows[1]["end"] == 0.02
    # Con 160 muestras la ZCR por ventana se cuantiza (0.1 s no entero de ciclo)
    assert windows[0]["freq_hz"] == 400.0
    assert windows[1]["freq_hz"] == 450.0


def test_window_sec_mayor_que_la_duracion_una_sola_ventana(tmp_path: Path) -> None:
    path = tmp_path / "tono.wav"
    _write_wav(path, _tone(440.0, 1.0))
    result = analyze_wav_acoustic_windows(path, window_sec=2.0)
    windows = result["windows"]
    assert len(windows) == 1
    assert windows[0]["start"] == 0.0
    assert windows[0]["end"] == 1.0
    # 16000 muestras = 440 ciclos -> 879 cruces (el cruces en el borde se
    # cuenta en la ventana contigua) -> 439.5 Hz
    assert windows[0]["freq_hz"] == 439.5


def test_ventana_final_parcial_recorta_en_la_duracion(tmp_path: Path) -> None:
    path = tmp_path / "tono.wav"
    _write_wav(path, _tone(440.0, 1.05))
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.05
    windows = result["windows"]
    assert len(windows) == 11
    assert windows[-1]["start"] == 1.0
    assert windows[-1]["end"] == 1.05
    # La ventana parcial (800 muestras) mantiene el contrato de ZCR
    assert windows[-1]["freq_hz"] == 430.0


# --------------------------------------------------------------------------
# Umbral de voz: RMS > 0.025 por ventana
# --------------------------------------------------------------------------


def test_tono_continuo_un_tramo_con_rms_y_zcr(tmp_path: Path) -> None:
    path = tmp_path / "tono440.wav"
    _write_wav(path, _tone(440.0, 1.0))
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.0
    windows = result["windows"]
    assert len(windows) == 10
    # Seno de amplitud 0.5: RMS ~= 0.5/sqrt(2) = 0.3536 sobre full scale
    assert all(round(w["rms"], 4) == 0.3535 for w in windows)
    # Cada ventana alinea 44 ciclos completos: 87 cruces -> 435.0 Hz exactos
    assert all(w["freq_hz"] == 435.0 for w in windows)
    assert result["speech_spans"] == [
        {"start": 0.0, "end": 1.0, "rms": 0.3535, "freq_hz": 435.0}
    ]


def test_umbral_rms_0025_por_debajo_sin_tramo(tmp_path: Path) -> None:
    # amp 0.035 -> RMS ~= 0.0247 < 0.025: se analiza (ventanas con energia)
    # pero ninguna ventana se considera voz.
    path = tmp_path / "bajo_umbral.wav"
    _write_wav(path, _tone(440.0, 1.0, amp=0.035))
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.0
    windows = result["windows"]
    assert len(windows) == 10
    assert windows[0]["rms"] == pytest.approx(0.0247, abs=1e-4)
    assert result["speech_spans"] == []


def test_umbral_rms_0025_por_encima_con_tramo(tmp_path: Path) -> None:
    # amp 0.036 -> RMS ~= 0.0254 > 0.025: mismo archivo, un tramo completo.
    # Entre 0.0247 y 0.0254 se fija el umbral 0.025 con margen +/-0.0004.
    path = tmp_path / "sobre_umbral.wav"
    _write_wav(path, _tone(440.0, 1.0, amp=0.036))
    result = analyze_wav_acoustic_windows(path)
    windows = result["windows"]
    assert windows[0]["rms"] == pytest.approx(0.0254, abs=1e-4)
    assert result["speech_spans"] == [
        {"start": 0.0, "end": 1.0, "rms": 0.0254, "freq_hz": 435.0}
    ]


def test_tono_entre_silencios_delimita_un_solo_tramo(tmp_path: Path) -> None:
    samples = array.array("h")
    samples.extend(_silence(0.3))
    samples.extend(_tone(440.0, 0.6))
    samples.extend(_silence(0.3))
    path = tmp_path / "tono_central.wav"
    _write_wav(path, samples)
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.2
    assert len(result["windows"]) == 12
    # El tramo cubre exactamente las 6 ventanas activas (0.3 s a 0.9 s)
    assert result["speech_spans"] == [
        {"start": 0.3, "end": 0.9, "rms": 0.3535, "freq_hz": 435.0}
    ]


def test_cambio_brusco_de_frecuencia_separa_dos_tramos(tmp_path: Path) -> None:
    # 440 Hz durante 0.8 s y luego 3000 Hz con la misma amplitude plena:
    # rms >= 0.15 en ambos lados, |3000 - 435| > 150 Hz y el tramo ya lleva
    # >= 0.5 s -> la regla de corte divide los dos tonos.
    samples = array.array("h")
    samples.extend(_tone(440.0, 0.8))
    samples.extend(_tone(3000.0, 0.8))
    path = tmp_path / "doble_tono.wav"
    _write_wav(path, samples)
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.6
    assert result["speech_spans"] == [
        {"start": 0.0, "end": 0.8, "rms": 0.3535, "freq_hz": 435.0},
        {"start": 0.8, "end": 1.6, "rms": 0.3535, "freq_hz": 2995.0},
    ]


def test_estereo_toma_solo_el_primer_canal(tmp_path: Path) -> None:
    # Canal izquierdo 440 Hz, canal derecho 3000 Hz: el resultado (435 Hz)
    # demuestra que se analiza solo el canal 0 intercalado, no la mezcla.
    left = _tone(440.0, 1.0)
    right = _tone(3000.0, 1.0)
    path = tmp_path / "estereo.wav"
    _write_wav(path, _interleave(left, right), channels=2)
    result = analyze_wav_acoustic_windows(path)
    assert result["duration_sec"] == 1.0
    assert result["speech_spans"] == [
        {"start": 0.0, "end": 1.0, "rms": 0.3535, "freq_hz": 435.0}
    ]


# --------------------------------------------------------------------------
# _build_words_for_span: reparto lineal puro de tiempos y muletillas
# --------------------------------------------------------------------------


def test_build_words_texto_vacio_no_inventa_palabras() -> None:
    assert _build_words_for_span("", 0.0, 1.0) == []
    assert _build_words_for_span("   \t  ", 0.0, 1.0) == []


def test_build_words_distribucion_exacta_en_el_span() -> None:
    words = _build_words_for_span("Hola mundo cruel", 1.0, 4.0)
    # step = 3.0/3 = 1.0 exacto: cortes intermedios en start + step - 0.01,
    # la ultima palabra siempre cierra en end_sec del span.
    assert words == [
        {"word": "Hola", "start": 1.0, "end": 1.99, "score": 0.94, "is_filler": False},
        {"word": "mundo", "start": 2.0, "end": 2.99, "score": 0.94, "is_filler": False},
        {"word": "cruel", "start": 3.0, "end": 4.0, "score": 0.94, "is_filler": False},
    ]


def test_build_words_redondeo_a_tres_decimales() -> None:
    # step = 1/3 no representable: los cortes se redondean a 3 decimales
    words = _build_words_for_span("uno dos tres", 0.0, 1.0)
    assert [(w["start"], w["end"]) for w in words] == [
        (0.0, 0.323),
        (0.333, 0.657),
        (0.667, 1.0),
    ]


def test_build_words_una_sola_palabra_llega_al_fin_del_span() -> None:
    words = _build_words_for_span("Único", 0.0, 2.0)
    assert words == [
        {"word": "Único", "start": 0.0, "end": 2.0, "score": 0.94, "is_filler": False}
    ]


def test_build_words_muletillas_con_puntuacion() -> None:
    words = _build_words_for_span("Bueno, eh... este", 0.0, 1.0)
    # El campo word conserva el token original; la deteccion limpia puntuacion
    # y pasa a minusculas antes de consultar SPANISH_FILLER_WORDS.
    assert [w["word"] for w in words] == ["Bueno,", "eh...", "este"]
    assert all(w["is_filler"] for w in words)
    assert all(w["score"] == 0.78 for w in words)


def test_build_words_palabra_normal_no_es_muletilla() -> None:
    words = _build_words_for_span("Estudio. final", 0.0, 1.0)
    assert words[0]["word"] == "Estudio."
    assert words[0]["is_filler"] is False
    assert words[0]["score"] == 0.94
    assert words[1]["is_filler"] is False


def test_build_words_hablatente_opcional() -> None:
    con_hablatente = _build_words_for_span("hola", 0.0, 1.0, speaker="SPEAKER_01")
    assert [w.get("speaker") for w in con_hablatente] == ["SPEAKER_01"]
    sin_hablatente = _build_words_for_span("hola", 0.0, 1.0)
    assert "speaker" not in sin_hablatente[0]
    hablatente_vacio = _build_words_for_span("hola", 0.0, 1.0, speaker="")
    assert "speaker" not in hablatente_vacio[0]


def test_build_words_span_degenerado_usa_duracion_minima() -> None:
    # end == start -> span_dur = max(0.1, 0) = 0.1; la ultima palabra no
    # queda invertida: se garantiza end = start + 0.02.
    words = _build_words_for_span("uno dos tres", 5.0, 5.0)
    assert len(words) == 3
    assert words[0] == {
        "word": "uno",
        "start": 5.0,
        "end": 5.023,
        "score": 0.94,
        "is_filler": False,
    }
    assert words[-1]["start"] == 5.067
    assert words[-1]["end"] == 5.087
    assert all(w["end"] > w["start"] for w in words)


def test_build_words_span_invertido_no_produce_end_menor_que_start() -> None:
    # end < start tambien cae al suelo de 0.1 s (mismos offsets que duracion cero)
    words = _build_words_for_span("uno dos tres", 2.0, 1.0)
    assert all(w["end"] > w["start"] for w in words)
    assert words[-1]["start"] == 2.067
    assert words[-1]["end"] == 2.087


def test_build_words_paso_minimo_garantiza_guion_de_0_02() -> None:
    # 20 tokens en el suelo de 0.1 s -> step = 0.005 <= 0.01: sin el ajuste
    # end <= start; la funcion garantiza +0.02 s por palabra (solapando).
    text = " ".join(f"tok{i}" for i in range(20))
    words = _build_words_for_span(text, 0.0, 0.0)
    assert len(words) == 20
    for w in words:
        assert w["end"] == round(w["start"] + 0.02, 3)
        assert w["end"] > w["start"]
    assert words[0]["start"] == 0.0
    assert words[-1]["start"] == 0.095
    assert words[-1]["end"] == 0.115
