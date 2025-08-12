from services.transcripcion import transcribir
from services.db import guardar_mensaje
from services.whatsapp_api import enviar_mensaje


def process_audio(audio_path: str, from_number: str, media_id: str, mime_type: str, public_url: str) -> None:
    """Background job to transcribe audio and respond to the user."""
    with open(audio_path, 'rb') as f:
        audio_bytes = f.read()
    texto = transcribir(audio_bytes)

    guardar_mensaje(
        from_number,
        texto,
        'audio',
        media_id=media_id,
        media_url=public_url,
        mime_type=mime_type,
    )

    if texto:
        enviar_mensaje(from_number, "Audio recibido correctamente.", tipo='bot')
    else:
        enviar_mensaje(
            from_number,
            "Audio recibido. No se realizó transcripción por exceder la duración permitida.",
            tipo='bot',
        )
