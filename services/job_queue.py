import os
import logging
import redis
from rq import Queue


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
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        redis_conn = redis.Redis.from_url(redis_url)
        queue = Queue("default", connection=redis_conn)
        queue.enqueue(
            "services.tasks.process_audio",
            audio_path,
            from_number,
            media_id,
            mime_type,
            public_url,
            mensaje_id,
        )
        return True
    except redis.exceptions.ConnectionError as exc:
        logging.error("Redis connection error: %s", exc)
        return False
