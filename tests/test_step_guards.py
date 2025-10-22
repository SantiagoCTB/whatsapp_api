import sys
from types import SimpleNamespace

import pytest

ROOT_DIR = __import__('pathlib').Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import webhook as webhook_module  # noqa: E402


class DummyCursor:
    def __init__(self, responses):
        self._responses = responses
        self._current_step = None

    def execute(self, query, params):
        step = params[0]
        self._current_step = step
        self._result = self._responses.get(step, [])

    def fetchone(self):
        if not self._result:
            return None
        return self._result[0]

    def fetchall(self):
        return list(self._result)


class DummyConnection:
    def __init__(self, responses):
        self._responses = responses

    def cursor(self):
        return DummyCursor(self._responses)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def patch_dependencies(monkeypatch):
    sent_messages = []
    steps_set = []

    def fake_enviar(numero, mensaje, **kwargs):
        sent_messages.append((numero, mensaje, kwargs.get('step')))
        return True

    def fake_set_user_step(numero, step, estado='espera_usuario'):
        steps_set.append((numero, step, estado))

    monkeypatch.setattr(webhook_module, 'enviar_mensaje', fake_enviar)
    monkeypatch.setattr(webhook_module, 'set_user_step', fake_set_user_step)

    return SimpleNamespace(sent_messages=sent_messages, steps_set=steps_set)


def test_dispatch_rule_breaks_cycle(monkeypatch, patch_dependencies):
    responses = {
        'auto': [
            (
                1,
                'mensaje automático',
                'auto,final',
                'texto',
                None,
                None,
                None,
                '*',
            )
        ],
        'final': [],
    }

    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )

    regla = responses['auto'][0]
    webhook_module.dispatch_rule('5215550000', regla, step='auto')

    assert patch_dependencies.steps_set[-1][1] == 'final'
    assert len(patch_dependencies.sent_messages) == 1
    numero, _, step = patch_dependencies.sent_messages[0]
    assert numero == '5215550000'
    assert step == 'auto'


def test_advance_steps_unique_chain(monkeypatch, patch_dependencies):
    responses = {
        'intro': [
            (
                10,
                'hola',
                'menu',
                'texto',
                None,
                None,
                None,
                '*',
            )
        ],
        'menu': [],
    }

    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )

    webhook_module.advance_steps('5215559999', 'intro,menu')

    assert patch_dependencies.steps_set[-1][1] == 'menu'
    assert len(patch_dependencies.sent_messages) == 1
    numero, mensaje, step = patch_dependencies.sent_messages[0]
    assert numero == '5215559999'
    assert mensaje == 'hola'
    assert step == 'intro'
