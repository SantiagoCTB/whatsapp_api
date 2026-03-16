import logging
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import webhook as webhook_module


@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_webhook_logs_get_validation(client, caplog):
    caplog.set_level(logging.INFO, logger='routes.webhook')

    response = client.get(
        '/webhook',
        query_string={'hub.verify_token': 'my_secret_token', 'hub.challenge': '123'},
        headers={'User-Agent': 'pytest-client', 'X-Hub-Signature-256': 'sig'},
    )

    assert response.status_code == 200
    request_log = next((r for r in caplog.records if 'Webhook request:' in r.message), None)
    assert request_log is not None
    assert 'method=GET' in request_log.message
    assert 'User-Agent' in request_log.message

    status_log = next((r for r in caplog.records if 'Returning verification challenge' in r.message), None)
    assert status_log is not None


def test_webhook_logs_missing_object(client, caplog):
    caplog.set_level(logging.INFO, logger='routes.webhook')

    response = client.post('/webhook', json={'entry': []})

    assert response.status_code == 400
    assert 'Returning status=no_object' in caplog.text
    assert 'reason=missing object field' in caplog.text


def test_webhook_logs_duplicate_message(client, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='routes.webhook')

    class DuplicateCursor:
        def __init__(self):
            self.last_query = ''

        def execute(self, query, params):
            self.last_query = query

        def fetchone(self):
            if 'SELECT 1 FROM mensajes_procesados' in self.last_query:
                return (1,)
            return None

        def close(self):
            pass

    class DuplicateConnection:
        def __init__(self):
            self._cursor = DuplicateCursor()

        def cursor(self):
            return self._cursor

        def close(self):
            pass

        def commit(self):
            pass

    monkeypatch.setattr(webhook_module, 'get_connection', lambda: DuplicateConnection())

    response = client.post(
        '/webhook',
        json={
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'changes': [
                        {
                            'value': {
                                'messages': [
                                    {
                                        'id': 'ABCD1234567890',
                                        'from': '5215555555555',
                                        'type': 'text',
                                        'text': {'body': 'Hola'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json == {'status': 'received'}

    assert 'Message skipped as duplicate' in caplog.text
    assert "'duplicates': 1" in caplog.text
    request_log = next((r for r in caplog.records if 'Webhook request:' in r.message), None)
    assert request_log is not None
    assert 'message_ids' in request_log.message
    assert 'ABCD...90' in request_log.message


def test_webhook_referral_bootstraps_rules(client, monkeypatch):
    class Cursor:
        def __init__(self):
            self.last_query = ''

        def execute(self, query, params):
            self.last_query = query

        def fetchone(self):
            if 'SELECT 1 FROM mensajes_procesados' in self.last_query:
                return None
            return None

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self._cursor = Cursor()

        def cursor(self):
            return self._cursor

        def close(self):
            pass

        def commit(self):
            pass

    calls = []

    monkeypatch.setattr(webhook_module, 'get_connection', lambda: Connection())
    monkeypatch.setattr(webhook_module, 'get_chat_state', lambda *_: None)
    monkeypatch.setattr(webhook_module, 'get_current_step', lambda *_: None)
    monkeypatch.setattr(webhook_module, 'guardar_mensaje', lambda *_, **__: 1)
    monkeypatch.setattr(webhook_module, 'update_chat_state', lambda *_, **__: None)
    monkeypatch.setattr(webhook_module, 'start_typing_feedback', lambda *_, **__: None)
    monkeypatch.setattr(
        webhook_module,
        'handle_text_message',
        lambda numero, texto, save=True, platform=None: calls.append((numero, texto, save, platform)),
    )

    response = client.post(
        '/webhook',
        json={
            'object': 'whatsapp_business_account',
            'entry': [
                {
                    'changes': [
                        {
                            'value': {
                                'messages': [
                                    {
                                        'id': 'wamid.test_referral_001',
                                        'from': '573103884607',
                                        'type': 'referral',
                                        'referral': {
                                            'source_url': 'https://fb.me/4jDjvZdTD',
                                            'headline': 'Whapco',
                                            'body': 'Promo',
                                            'thumbnail_url': 'https://example.com/thumb.jpg',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json == {'status': 'received'}
    assert calls == [('573103884607', '', False, None)]
