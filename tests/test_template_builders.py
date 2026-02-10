import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from services.template_builders import (
    TemplateValidationError,
    build_template_create_payload,
    build_template_send_payload,
)


def test_build_template_create_payload_with_positional_examples():
    payload = build_template_create_payload(
        {
            "template_key": "order_status",
            "category": "UTILITY",
            "language": "es_CO",
            "parameter_format": "POSITIONAL",
            "body_text": "Tu pedido #{{1}} llega el {{2}}",
            "body_examples": ["ABC123", "25/08/2026"],
            "buttons": [{"type": "URL", "text": "Ver", "url": "https://x.com/{{1}}"}],
        }
    )

    assert payload["name"] == "order_status"
    body = next(comp for comp in payload["components"] if comp["type"] == "BODY")
    assert body["example"]["body_text"] == [["ABC123", "25/08/2026"]]


def test_build_template_create_payload_named_requires_named_variables():
    with pytest.raises(TemplateValidationError):
        build_template_create_payload(
            {
                "template_key": "order_status",
                "category": "UTILITY",
                "language": "es_CO",
                "parameter_format": "NAMED",
                "body_text": "Tu pedido #{{1}}",
                "body_examples": ["ABC123"],
            }
        )


def test_build_template_send_payload_builds_template_message():
    payload = build_template_send_payload(
        {
            "to": "573001112233",
            "template_name": "order_status",
            "language_code": "es_CO",
            "body_parameters": ["ABC123", "25/08/2026"],
        }
    )

    assert payload["type"] == "template"
    assert payload["template"]["name"] == "order_status"
    assert payload["template"]["components"][0]["type"] == "body"
    assert payload["template"]["components"][0]["parameters"][0]["text"] == "ABC123"
