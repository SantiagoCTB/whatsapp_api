from services.transcripcion import transcribir
from services.db import update_mensaje_texto
from services.whatsapp_api import enviar_mensaje


def process_audio(
    audio_path: str,
    from_number: str,
    media_id: str,
    mime_type: str,
    public_url: str,
    mensaje_id: int,
) -> None:
    """Background job to transcribe audio and respond to the user."""
    with open(audio_path, 'rb') as f:
        audio_bytes = f.read()
    texto = transcribir(audio_bytes)

    update_mensaje_texto(mensaje_id, texto)

    if texto:
        enviar_mensaje(from_number, f"Transcripción lista: {texto}", tipo='bot')
    else:
        enviar_mensaje(
            from_number,
            "Audio recibido. No se realizó transcripción por exceder la duración permitida.",
            tipo='bot',
        )
