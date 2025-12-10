import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import chat_routes


@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_admin_can_delete_chat(client, monkeypatch):
    calls = {}

    def fake_hide(numero):
        calls['hidden'] = numero

    def fake_clear(numero):
        calls['cleared'] = numero

    monkeypatch.setattr(chat_routes, 'hide_chat', fake_hide)
    monkeypatch.setattr(chat_routes, 'clear_chat_runtime_state', fake_clear)

    with client.session_transaction() as sess:
        sess['user'] = 'admin'
        sess['roles'] = ['admin']

    response = client.post('/delete_chat', json={'numero': '5215550000'})

    assert response.status_code == 200
    assert response.json == {'status': 'ok'}
    assert calls == {'hidden': '5215550000', 'cleared': '5215550000'}


def test_delete_chat_requires_admin(client, monkeypatch):
    monkeypatch.setattr(chat_routes, 'hide_chat', lambda numero: (_ for _ in ()).throw(RuntimeError('should not run')))

    with client.session_transaction() as sess:
        sess['user'] = 'user'
        sess['roles'] = ['agente']

    response = client.post('/delete_chat', json={'numero': '5215550000'})

    assert response.status_code == 403
    assert response.json == {'error': 'No autorizado'}
