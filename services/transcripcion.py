import json
import os
import subprocess
import tempfile
import wave
from typing import Optional

from vosk import Model, KaldiRecognizer
from config import Config

_MODEL: Optional[Model] = None


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
    wav_path = _normalize_audio(audio_bytes)
    wf = wave.open(wav_path, "rb")

    duracion_ms = (wf.getnframes() / wf.getframerate()) * 1000
    if duracion_ms > Config.MAX_TRANSCRIPTION_DURATION_MS:
        wf.close()
        os.remove(wav_path)
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
    wf.close()
    os.remove(wav_path)
    return " ".join(t for t in texto if t).strip()
