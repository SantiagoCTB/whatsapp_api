import json
import re
from typing import Any


_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")


class TemplateValidationError(ValueError):
    """Error de validación de payloads de plantillas."""


def validate_template_name(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized:
        raise TemplateValidationError("El nombre técnico de la plantilla es obligatorio.")
    if not _TEMPLATE_NAME_RE.match(normalized):
        raise TemplateValidationError(
            "El nombre técnico debe usar solo minúsculas, números y guion bajo (_)."
        )
    return normalized


def extract_placeholders(text: str) -> list[str]:
    if not text:
        return []
    return [match.strip() for match in _PLACEHOLDER_RE.findall(text)]


def _normalize_parameter_format(value: str) -> str:
    normalized = (value or "POSITIONAL").strip().upper()
    if normalized not in {"POSITIONAL", "NAMED"}:
        raise TemplateValidationError("parameter_format debe ser POSITIONAL o NAMED.")
    return normalized


def _validate_body_placeholders(parameter_format: str, body_text: str) -> list[str]:
    placeholders = extract_placeholders(body_text)
    if parameter_format == "POSITIONAL":
        invalid = [p for p in placeholders if not p.isdigit()]
        if invalid:
            raise TemplateValidationError(
                "Con formato POSITIONAL, las variables deben ser numéricas: {{1}}, {{2}}, ..."
            )
    elif parameter_format == "NAMED":
        invalid = [p for p in placeholders if p.isdigit()]
        if invalid:
            raise TemplateValidationError(
                "Con formato NAMED, usa variables con nombre: {{customer_name}}."
            )
    return placeholders


def _build_body_example(parameter_format: str, placeholders: list[str], examples: list[str]) -> dict[str, Any] | None:
    if not placeholders:
        return None

    clean_examples = [str(item).strip() for item in (examples or []) if str(item).strip()]
    if len(clean_examples) < len(placeholders):
        raise TemplateValidationError(
            "Debes enviar valores de ejemplo para todas las variables del cuerpo."
        )

    if parameter_format == "POSITIONAL":
        return {"body_text": [clean_examples[: len(placeholders)]]}

    named_examples = []
    for index, placeholder in enumerate(placeholders):
        named_examples.append(
            {
                "param_name": placeholder,
                "example": clean_examples[index],
            }
        )
    return {"body_text_named_params": named_examples}


def build_template_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    name = validate_template_name(payload.get("template_key") or payload.get("name"))
    language = (payload.get("language") or "").strip()
    if not language:
        raise TemplateValidationError("language es obligatorio.")

    category = (payload.get("category") or "UTILITY").strip().upper()
    if category not in {"UTILITY", "MARKETING", "AUTHENTICATION"}:
        raise TemplateValidationError("category debe ser UTILITY, MARKETING o AUTHENTICATION.")

    parameter_format = _normalize_parameter_format(payload.get("parameter_format"))
    body_text = str(payload.get("body_text") or "").strip()
    if not body_text:
        raise TemplateValidationError("El componente BODY es obligatorio.")

    body_placeholders = _validate_body_placeholders(parameter_format, body_text)
    body_examples = payload.get("body_examples") or []

    components: list[dict[str, Any]] = []

    header = payload.get("header") or {}
    if header.get("enabled"):
        header_format = str(header.get("format") or "TEXT").strip().upper()
        if header_format not in {"TEXT", "IMAGE", "VIDEO", "DOCUMENT", "LOCATION"}:
            raise TemplateValidationError("Formato de HEADER inválido.")

        header_component: dict[str, Any] = {"type": "HEADER", "format": header_format}
        if header_format == "TEXT":
            text = str(header.get("text") or "").strip()
            if not text:
                raise TemplateValidationError("HEADER en formato TEXT requiere texto.")
            header_component["text"] = text
        components.append(header_component)

    body_component: dict[str, Any] = {"type": "BODY", "text": body_text}
    body_example = _build_body_example(parameter_format, body_placeholders, body_examples)
    if body_example:
        body_component["example"] = body_example
    components.append(body_component)

    footer_text = str(payload.get("footer_text") or "").strip()
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    buttons = payload.get("buttons") or []
    normalized_buttons = []
    for button in buttons:
        b_type = str(button.get("type") or "").strip().upper()
        text = str(button.get("text") or "").strip()
        if not b_type or not text:
            raise TemplateValidationError("Cada botón debe incluir type y text.")

        item: dict[str, Any] = {"type": b_type, "text": text}
        if b_type == "URL":
            url = str(button.get("url") or "").strip()
            if not url:
                raise TemplateValidationError("Los botones URL requieren el campo url.")
            item["url"] = url
        if b_type == "PHONE_NUMBER":
            phone = str(button.get("phone_number") or "").strip()
            if not phone:
                raise TemplateValidationError("Los botones PHONE_NUMBER requieren phone_number.")
            item["phone_number"] = phone
        normalized_buttons.append(item)

    if normalized_buttons:
        components.append({"type": "BUTTONS", "buttons": normalized_buttons})

    return {
        "name": name,
        "language": language,
        "category": category,
        "parameter_format": parameter_format,
        "components": components,
    }


def build_template_send_payload(payload: dict[str, Any]) -> dict[str, Any]:
    to = str(payload.get("to") or "").strip()
    if not to:
        raise TemplateValidationError("El destinatario (to) es obligatorio.")

    template_name = validate_template_name(payload.get("template_name") or payload.get("name"))
    language_code = str(payload.get("language_code") or payload.get("language") or "").strip()
    if not language_code:
        raise TemplateValidationError("language_code es obligatorio.")

    body_parameters = payload.get("body_parameters") or []
    parameters = []
    for item in body_parameters:
        value = str(item).strip()
        if value:
            parameters.append({"type": "text", "text": value})

    components = []
    if parameters:
        components.append({"type": "body", "parameters": parameters})

    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }


def build_flow_send_payload(payload: dict[str, Any]) -> dict[str, Any]:
    to = str(payload.get("to") or "").strip()
    if not to:
        raise TemplateValidationError("El destinatario (to) es obligatorio.")

    flow_cta = str(payload.get("flow_cta") or "").strip()
    if not flow_cta:
        raise TemplateValidationError("flow_cta es obligatorio para enviar un Flow.")

    flow_id = str(payload.get("flow_id") or "").strip()
    flow_name = str(payload.get("flow_name") or "").strip()
    if bool(flow_id) == bool(flow_name):
        raise TemplateValidationError("Debes indicar solamente flow_id o flow_name, no ambos.")

    parameters: dict[str, Any] = {
        "flow_message_version": str(payload.get("flow_message_version") or "3").strip() or "3",
        "flow_cta": flow_cta,
    }
    if flow_id:
        parameters["flow_id"] = flow_id
    if flow_name:
        parameters["flow_name"] = flow_name

    for key in ("mode", "flow_token", "flow_action"):
        value = str(payload.get(key) or "").strip()
        if value:
            parameters[key] = value

    action_payload = payload.get("flow_action_payload")
    if isinstance(action_payload, str):
        action_payload = action_payload.strip()
        if action_payload:
            try:
                action_payload = json.loads(action_payload)
            except Exception as exc:  # pragma: no cover - controlado por validación externa
                raise TemplateValidationError("flow_action_payload debe ser JSON válido.") from exc
        else:
            action_payload = None
    if action_payload is not None:
        if not isinstance(action_payload, dict):
            raise TemplateValidationError("flow_action_payload debe ser un objeto JSON.")
        parameters["flow_action_payload"] = action_payload

    body_text = str(payload.get("flow_body") or payload.get("body_text") or "Continuemos").strip() or "Continuemos"
    interactive: dict[str, Any] = {
        "type": "flow",
        "body": {"text": body_text},
        "action": {
            "name": "flow",
            "parameters": parameters,
        },
    }

    header_text = str(payload.get("flow_header") or "").strip()
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text}

    footer_text = str(payload.get("flow_footer") or "").strip()
    if footer_text:
        interactive["footer"] = {"text": footer_text}

    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
