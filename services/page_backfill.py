import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from config import Config
from services import db


logger = logging.getLogger(__name__)

GRAPH_FACEBOOK_BASE_URL = f"https://graph.facebook.com/{Config.FACEBOOK_GRAPH_API_VERSION}"
GRAPH_INSTAGRAM_BASE_URL = f"https://graph.instagram.com/{Config.FACEBOOK_GRAPH_API_VERSION}"


def _resolve_graph_base_url(platform: str, page_id: str) -> str:
    normalized = (platform or "").strip().lower()
    if normalized == "instagram":
        return GRAPH_INSTAGRAM_BASE_URL
    return GRAPH_FACEBOOK_BASE_URL


def fetch_conversations(
    page_id: str,
    access_token: str,
    platform: str,
    *,
    include_owner: bool = True,
    base_url: str | None = None,
) -> List[Dict[str, Any]]:
    params = {
        "platform": platform,
        "access_token": access_token,
    }
    if include_owner:
        params["fields"] = "messages,is_owner"

    graph_base_url = base_url or _resolve_graph_base_url(platform, page_id)
    url = f"{graph_base_url}/{page_id}/conversations"
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        logger.warning("Error consultando conversaciones del Page: %s", exc)
        return []

    if not response.ok and include_owner:
        logger.info(
            "Reintentando conversaciones sin fields=is_owner",
            extra={"status": response.status_code},
        )
        params.pop("fields", None)
        try:
            response = requests.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Error consultando conversaciones del Page: %s", exc)
            return []

    if not response.ok:
        _log_graph_error("conversations", response)
        return []

    payload = _safe_json(response)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []

    conversations: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if include_owner and entry.get("is_owner") is False:
            continue
        conversation_id = entry.get("id")
        if not conversation_id:
            continue
        conversations.append(
            {
                "id": conversation_id,
                "updated_time": entry.get("updated_time"),
                "is_owner": entry.get("is_owner"),
            }
        )
    return conversations


def fetch_instagram_user(access_token: str) -> Dict[str, Any] | None:
    url = f"{GRAPH_INSTAGRAM_BASE_URL}/me"
    params = {"fields": "id,username,account_type", "access_token": access_token}
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
    except requests.RequestException as exc:
        logger.warning("Error consultando cuenta de Instagram: %s", exc)
        return None

    if not response.ok:
        _log_graph_error("instagram_user", response)
        return None

    payload = _safe_json(response)
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    return payload


def fetch_instagram_conversations(access_token: str) -> List[Dict[str, Any]]:
    url = f"{GRAPH_INSTAGRAM_BASE_URL}/me/conversations"
    params = {
        "platform": "instagram",
        "fields": "id,updated_time,participants",
        "access_token": access_token,
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    conversations: List[Dict[str, Any]] = []
    next_url = url
    next_params = params
    retried_without_fields = False

    while next_url:
        try:
            response = requests.get(
                next_url, params=next_params, headers=headers, timeout=15
            )
        except requests.RequestException as exc:
            logger.warning("Error consultando conversaciones de Instagram: %s", exc)
            break

        if not response.ok:
            if not retried_without_fields and next_params and "fields" in next_params:
                logger.info(
                    "Reintentando conversaciones de Instagram con fields=participants",
                    extra={"status": response.status_code},
                )
                next_params = {k: v for k, v in next_params.items() if k != "fields"}
                next_params["fields"] = "participants"
                retried_without_fields = True
                continue
            _log_graph_error("instagram_conversations", response)
            break

        payload = _safe_json(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            data = []

        paging = payload.get("paging") if isinstance(payload, dict) else None
        next_url = paging.get("next") if isinstance(paging, dict) else None
        next_params = None

        if not data and not retried_without_fields and next_url is None and next_params is None:
            logger.info(
                "Reintentando conversaciones de Instagram con fields=participants por lista vacía",
                extra={"status": response.status_code},
            )
            next_url = url
            next_params = {k: v for k, v in params.items() if k != "fields"}
            next_params["fields"] = "participants"
            retried_without_fields = True
            continue

        for entry in data:
            if not isinstance(entry, dict):
                continue
            conversation_id = entry.get("id")
            if not conversation_id:
                continue
            conversations.append(
                {
                    "id": conversation_id,
                    "updated_time": entry.get("updated_time"),
                    "participant_ids": _extract_participant_ids(entry),
                    "participants": _extract_participants(entry),
                }
            )

    return conversations


def fetch_instagram_messages(
    conversation_id: str,
    access_token: str,
) -> List[Dict[str, Any]]:
    url = f"{GRAPH_INSTAGRAM_BASE_URL}/{conversation_id}/messages"
    params = {
        "fields": "id,from,to,message,created_time",
        "limit": 50,
        "access_token": access_token,
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    messages: List[Dict[str, Any]] = []
    next_url = url
    next_params = params

    while next_url:
        try:
            response = requests.get(
                next_url, params=next_params, headers=headers, timeout=15
            )
        except requests.RequestException as exc:
            logger.warning("Error consultando mensajes de Instagram: %s", exc)
            break

        next_params = None
        if not response.ok:
            _log_graph_error(
                "instagram_messages",
                response,
                conversation_id=conversation_id,
            )
            break

        payload = _safe_json(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            messages.extend([item for item in data if isinstance(item, dict)])

        paging = payload.get("paging") if isinstance(payload, dict) else None
        next_url = paging.get("next") if isinstance(paging, dict) else None

    return messages


def fetch_conversation_messages(
    conversation_id: str,
    access_token: str,
    *,
    base_url: str,
) -> List[Dict[str, Any]]:
    url = f"{base_url}/{conversation_id}"
    params = {
        "fields": "messages",
        "access_token": access_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        logger.warning("Error consultando mensajes de conversación: %s", exc)
        return []

    if not response.ok:
        _log_graph_error("conversation_messages", response)
        return []

    payload = _safe_json(response)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    data = messages.get("data") if isinstance(messages, dict) else None
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def fetch_message_detail(
    message_id: str,
    access_token: str,
    *,
    base_url: str,
) -> Dict[str, Any] | None:
    url = f"{base_url}/{message_id}"
    params = {
        "fields": "id,created_time,from,to,message,reply_to",
        "access_token": access_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        logger.warning("Error consultando detalle de mensaje: %s", exc)
        return None

    if not response.ok:
        _log_graph_error("message_detail", response, message_id=message_id)
        return None

    payload = _safe_json(response)
    return payload if isinstance(payload, dict) else None


def run_page_backfill(
    *,
    tenant_key: str,
    db_settings: db.DatabaseSettings,
    page_id: str,
    access_token: str,
    platform: str,
):
    logger.info(
        "Iniciando backfill de conversaciones",
        extra={"tenant_key": tenant_key, "platform": platform},
    )

    if (platform or "").strip().lower() == "instagram":
        instagram_user = fetch_instagram_user(access_token)
        if not instagram_user:
            logger.info(
                "No se pudo resolver la cuenta de Instagram para backfill",
                extra={"tenant_key": tenant_key},
            )
            return

        page_id = str(instagram_user.get("id"))
        instagram_username = instagram_user.get("username")
        actor_id = None
        logger.info(
            "Backfill de Instagram: cuenta resuelta",
            extra={
                "tenant_key": tenant_key,
                "instagram_me_id": page_id,
                "instagram_username": instagram_username,
            },
        )
        conversations = fetch_instagram_conversations(access_token)
        if not conversations:
            logger.info(
                "No se encontraron conversaciones para backfill",
                extra={"tenant_key": tenant_key, "platform": platform},
            )
            return
        logger.info(
            "Backfill de Instagram: conversaciones obtenidas",
            extra={
                "tenant_key": tenant_key,
                "instagram_me_id": page_id,
                "conversation_count": len(conversations),
            },
        )

        seen_message_ids = set()
        for conversation in conversations:
            conversation_id = conversation.get("id")
            if not conversation_id:
                continue
            participant_ids = conversation.get("participant_ids") or []
            participants = conversation.get("participants") or []
            if not actor_id and instagram_username:
                actor_id = _resolve_actor_id_from_participants(
                    participants,
                    instagram_username,
                )
                if actor_id:
                    logger.info(
                        "Backfill de Instagram: actor_id resuelto desde participants",
                        extra={
                            "tenant_key": tenant_key,
                            "conversation_id": conversation_id,
                            "actor_id": actor_id,
                            "instagram_username": instagram_username,
                        },
                    )
            messages = fetch_instagram_messages(conversation_id, access_token)
            logger.info(
                "Backfill de Instagram: mensajes obtenidos",
                extra={
                    "tenant_key": tenant_key,
                    "conversation_id": conversation_id,
                    "message_count": len(messages),
                    "participant_ids": participant_ids,
                },
            )
            for message in messages:
                message_id = message.get("id")
                if not message_id or message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                if not actor_id and instagram_username:
                    from_obj = message.get("from") or {}
                    if (
                        isinstance(from_obj, dict)
                        and from_obj.get("username") == instagram_username
                    ):
                        actor_id = str(from_obj.get("id") or "") or None
                        if actor_id:
                            logger.info(
                                "Backfill de Instagram: actor_id resuelto desde mensaje",
                                extra={
                                    "tenant_key": tenant_key,
                                    "conversation_id": conversation_id,
                                    "message_id": message_id,
                                    "actor_id": actor_id,
                                    "instagram_username": instagram_username,
                                },
                            )
                enriched_message = _ensure_instagram_to_field(
                    message,
                    participant_ids=participant_ids,
                    actor_id=actor_id,
                )
                _store_message_detail(
                    enriched_message,
                    tenant_key=tenant_key,
                    db_settings=db_settings,
                    platform=platform,
                    page_id=page_id,
                    conversation_id=conversation_id,
                    participant_ids=participant_ids,
                    actor_id=actor_id,
                    instagram_me_id=page_id,
                    instagram_username=instagram_username,
                )
        return

    base_url = _resolve_graph_base_url(platform, page_id)
    conversations = fetch_conversations(
        page_id,
        access_token,
        platform,
        base_url=base_url,
    )
    if not conversations:
        logger.info(
            "No se encontraron conversaciones para backfill",
            extra={"tenant_key": tenant_key, "platform": platform},
        )
        return

    seen_message_ids = set()
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not conversation_id:
            continue
        messages = fetch_conversation_messages(
            conversation_id,
            access_token,
            base_url=base_url,
        )
        for message in messages:
            message_id = message.get("id")
            if not message_id or message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
            detail = fetch_message_detail(
                message_id,
                access_token,
                base_url=base_url,
            )
            if not detail:
                continue
            _store_message_detail(
                detail,
                tenant_key=tenant_key,
                db_settings=db_settings,
                platform=platform,
                page_id=page_id,
                conversation_id=conversation_id,
            )


def enqueue_page_backfill(
    *,
    tenant_key: str,
    db_settings: db.DatabaseSettings,
    page_id: str,
    access_token: str,
    platform: str,
):
    def _runner():
        try:
            run_page_backfill(
                tenant_key=tenant_key,
                db_settings=db_settings,
                page_id=page_id,
                access_token=access_token,
                platform=platform,
            )
        except Exception:
            logger.exception(
                "Error inesperado en backfill de conversaciones",
                extra={"tenant_key": tenant_key, "platform": platform},
            )

    thread = threading.Thread(target=_runner, name=f"backfill-{tenant_key}", daemon=True)
    thread.start()


def _store_message_detail(
    detail: Dict[str, Any],
    *,
    tenant_key: str,
    db_settings: db.DatabaseSettings,
    platform: str,
    page_id: str,
    conversation_id: str,
    participant_ids: List[str] | None = None,
    actor_id: str | None = None,
    instagram_me_id: str | None = None,
    instagram_username: str | None = None,
):
    message_id = detail.get("id")
    if not message_id:
        return

    from_obj = detail.get("from") or {}
    to_obj = detail.get("to") or {}
    reply_to = detail.get("reply_to") or {}
    message_text = _extract_message_text(detail)

    to_ids = _extract_to_ids(to_obj)
    to_ids_json = json.dumps(to_ids, ensure_ascii=False)

    db.guardar_page_message(
        tenant_key=tenant_key,
        platform=platform,
        page_id=page_id,
        conversation_id=conversation_id,
        message_id=message_id,
        created_time=detail.get("created_time"),
        from_id=from_obj.get("id"),
        to_ids_json=to_ids_json,
        message=message_text,
        reply_to_mid=reply_to.get("mid"),
        is_self_reply=reply_to.get("is_self_reply"),
        db_settings=db_settings,
    )

    numero = _resolve_numero_from_message(
        from_id=from_obj.get("id"),
        to_ids=to_ids,
        page_id=page_id,
        participant_ids=participant_ids or [],
        actor_id=actor_id,
    )
    if (platform or "").strip().lower() == "instagram":
        logger.info(
            "Backfill de Instagram: detalle de mensaje",
            extra={
                "instagram_me_id": instagram_me_id,
                "instagram_username": instagram_username,
                "conversation_id": conversation_id,
                "participant_ids": participant_ids or [],
                "message_id": message_id,
                "from_id": from_obj.get("id"),
                "from_username": from_obj.get("username"),
                "to_ids": to_ids,
                "actor_id": actor_id,
                "numero": numero,
            },
        )

    if not numero:
        if (platform or "").strip().lower() == "instagram":
            logger.info(
                "Backfill de Instagram: numero no resuelto, se omite guardar_mensaje",
                extra={
                    "instagram_me_id": instagram_me_id,
                    "instagram_username": instagram_username,
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "from_id": from_obj.get("id"),
                    "to_ids": to_ids,
                    "participant_ids": participant_ids or [],
                    "actor_id": actor_id,
                },
            )
        return

    compare_id = actor_id or page_id
    tipo_base = (
        "asesor" if str(from_obj.get("id") or "") == str(compare_id) else "cliente"
    )
    channel = "messenger" if platform == "messenger" else "instagram"
    tipo = f"{tipo_base}_{channel}"

    db.guardar_mensaje(
        numero,
        message_text or "",
        tipo,
        wa_id=message_id,
        reply_to_wa_id=reply_to.get("mid"),
        timestamp=_parse_created_time(detail.get("created_time")),
        dedupe_wa_id=True,
        db_settings=db_settings,
    )


def _extract_to_ids(to_obj: Dict[str, Any]) -> List[str]:
    data = to_obj.get("data")
    if not isinstance(data, list):
        return []
    ids = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        to_id = entry.get("id")
        if to_id:
            ids.append(str(to_id))
    return ids


def _extract_participant_ids(entry: Dict[str, Any]) -> List[str]:
    participants = entry.get("participants")
    if not isinstance(participants, dict):
        return []
    data = participants.get("data")
    if not isinstance(data, list):
        return []
    ids = []
    for participant in data:
        if not isinstance(participant, dict):
            continue
        participant_id = participant.get("id")
        if participant_id:
            ids.append(str(participant_id))
    return ids


def _extract_participants(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    participants = entry.get("participants")
    if not isinstance(participants, dict):
        return []
    data = participants.get("data")
    if not isinstance(data, list):
        return []
    extracted: List[Dict[str, Any]] = []
    for participant in data:
        if not isinstance(participant, dict):
            continue
        participant_id = participant.get("id")
        if not participant_id:
            continue
        extracted.append(
            {
                "id": str(participant_id),
                "username": participant.get("username"),
            }
        )
    return extracted


def _resolve_actor_id_from_participants(
    participants: List[Dict[str, Any]],
    instagram_username: str,
) -> str | None:
    if not instagram_username:
        return None
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        if participant.get("username") == instagram_username:
            participant_id = participant.get("id")
            if participant_id:
                return str(participant_id)
    return None


def _ensure_instagram_to_field(
    message: Dict[str, Any],
    *,
    participant_ids: List[str],
    actor_id: str | None,
) -> Dict[str, Any]:
    if not isinstance(message, dict):
        return message
    to_obj = message.get("to") if isinstance(message.get("to"), dict) else {}
    existing_to_ids = _extract_to_ids(to_obj)
    if existing_to_ids:
        return message

    from_obj = message.get("from") if isinstance(message.get("from"), dict) else {}
    from_id = str(from_obj.get("id") or "")

    fallback_ids = []
    for participant_id in participant_ids:
        if participant_id and participant_id != from_id:
            fallback_ids.append(participant_id)
    if not fallback_ids and actor_id and actor_id != from_id:
        fallback_ids.append(actor_id)

    if not fallback_ids:
        return message

    enriched = dict(message)
    enriched["to"] = {"data": [{"id": participant_id} for participant_id in fallback_ids]}
    return enriched


def _resolve_numero_from_message(
    *,
    from_id: str | None,
    to_ids: List[str],
    page_id: str | None,
    participant_ids: List[str],
    actor_id: str | None,
) -> str | None:
    if from_id:
        from_id = str(from_id)
    page_id = str(page_id) if page_id else None
    normalized_to_ids = [str(item) for item in to_ids if item]
    normalized_participant_ids = [str(item) for item in participant_ids if item]
    effective_actor_id = str(actor_id) if actor_id else page_id

    if not normalized_to_ids and normalized_participant_ids:
        for candidate in normalized_participant_ids:
            if not effective_actor_id or candidate != effective_actor_id:
                return candidate
        return None

    if effective_actor_id and from_id == effective_actor_id:
        for candidate in normalized_to_ids:
            if candidate != effective_actor_id:
                return candidate
        return None

    if from_id:
        return from_id

    for candidate in normalized_to_ids:
        if not effective_actor_id or candidate != effective_actor_id:
            return candidate
    return None


def _parse_created_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _extract_message_text(detail: Dict[str, Any]) -> str | None:
    if not isinstance(detail, dict):
        return None
    message_value = detail.get("message")
    if isinstance(message_value, str):
        return message_value
    if isinstance(message_value, dict):
        text_value = message_value.get("text")
        if isinstance(text_value, str):
            return text_value
    text_value = detail.get("text")
    if isinstance(text_value, str):
        return text_value
    if message_value is not None:
        try:
            return json.dumps(message_value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(message_value)
    return None


def _safe_json(response: requests.Response) -> Dict[str, Any] | List[Any]:
    try:
        return response.json()
    except ValueError:
        return {}


def _log_graph_error(context: str, response: requests.Response, **extra):
    payload = _safe_json(response)
    details = payload.get("error") if isinstance(payload, dict) else None
    logger.warning(
        "Respuesta inválida de Graph API",
        extra={
            "context": context,
            "status": response.status_code,
            "details": details or payload,
            **extra,
        },
    )
