import os
from redis import Redis
from rq import Queue

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_conn = Redis.from_url(redis_url)
queue = Queue('default', connection=redis_conn)

def enqueue_transcription(audio_path: str, from_number: str, media_id: str, mime_type: str, public_url: str, mensaje_id: int) -> None:
    """Enqueue an audio transcription job."""
    queue.enqueue(
        'services.tasks.process_audio',
        audio_path,
        from_number,
        media_id,
        mime_type,
        public_url,
        mensaje_id,
    )
