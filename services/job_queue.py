import logging
from concurrent.futures import ThreadPoolExecutor
from services.tasks import process_audio

executor = ThreadPoolExecutor(max_workers=2)


def enqueue_transcription(
    audio_path: str,
    from_number: str,
    media_id: str,
    mime_type: str,
    public_url: str,
    mensaje_id: int,
) -> bool:
    """Enqueue an audio transcription job."""
    try:
        executor.submit(
            process_audio,
            audio_path,
            from_number,
            media_id,
            mime_type,
            public_url,
            mensaje_id,
        )
        return True
    except Exception as exc:
        logging.error("Error encolando la transcripción: %s", exc)
        return False
