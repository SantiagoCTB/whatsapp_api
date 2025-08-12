import json
import os
import subprocess
import tempfile
import time
import wave
import logging
from typing import Optional

from vosk import Model, KaldiRecognizer
from config import Config

_MODEL: Optional[Model] = None

logger = logging.getLogger(__name__)

_TOTAL_TIME = 0.0
_CALL_COUNT = 0
_TRANSCRIPTION_ENABLED = True


def _get_model() -> Model:
    global _MODEL
    if _MODEL is None:
        # Cargar modelo por defecto en español
        _MODEL = Model(lang="es")
    return _MODEL


def _normalize_audio(input_bytes: bytes) -> str:
    """Convierte los bytes de audio a un wav mono 16k usando ffmpeg."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".input") as in_f:
        in_f.write(input_bytes)
        input_path = in_f.name
    out_f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    out_f.close()
    output_path = out_f.name

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        output_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(input_path)
    return output_path


def transcribir(audio_bytes: bytes) -> str:
    """Normaliza el audio y devuelve el texto transcrito.

    Si la duración del audio excede el máximo permitido, retorna una cadena
    vacía sin pasar por el modelo de Vosk.
    """
    if not _TRANSCRIPTION_ENABLED:
        logger.warning("Transcription disabled due to high average runtime")
        return ""

    start = time.perf_counter()
    wav_path = _normalize_audio(audio_bytes)
    wf = wave.open(wav_path, "rb")

    try:
        duracion_ms = (wf.getnframes() / wf.getframerate()) * 1000
        if duracion_ms > Config.MAX_TRANSCRIPTION_DURATION_MS:
            return ""

        model = _get_model()
        rec = KaldiRecognizer(model, wf.getframerate())
        texto = []
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                texto.append(res.get("text", ""))
        res = json.loads(rec.FinalResult())
        texto.append(res.get("text", ""))
        return " ".join(t for t in texto if t).strip()
    finally:
        wf.close()
        os.remove(wav_path)
        elapsed = time.perf_counter() - start
        _record_transcription_time(elapsed)


def _record_transcription_time(elapsed: float) -> None:
    global _TOTAL_TIME, _CALL_COUNT, _TRANSCRIPTION_ENABLED
    _TOTAL_TIME += elapsed
    _CALL_COUNT += 1
    average = _TOTAL_TIME / _CALL_COUNT
    logger.info(
        "Transcription took %.3f s (avg %.3f s over %d calls)",
        elapsed,
        average,
        _CALL_COUNT,
    )
    if average > Config.TRANSCRIPTION_MAX_AVG_TIME_SEC:
        logger.warning(
            "Average transcription time %.3f s exceeds threshold %.3f s; disabling transcription",
            average,
            Config.TRANSCRIPTION_MAX_AVG_TIME_SEC,
        )
        _TRANSCRIPTION_ENABLED = False
