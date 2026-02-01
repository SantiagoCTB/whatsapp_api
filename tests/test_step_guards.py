import sys
from datetime import datetime
from zoneinfo import ZoneInfo
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

    def execute(self, query, params=None):
        params = params or ()
        step = params[0] if params else query
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
    chat_states = {}

    def fake_enviar(numero, mensaje, **kwargs):
        sent_messages.append((numero, mensaje, kwargs.get('step')))
        return True

    def fake_set_user_step(numero, step, estado='espera_usuario'):
        steps_set.append((numero, step, estado))
        chat_states[numero] = (step, datetime.now())

    def fake_get_chat_state(numero):
        return chat_states.get(numero)

    def fake_update_chat_state(numero, step, estado='espera_usuario'):
        chat_states[numero] = (step, datetime.now())

    def fake_delete_chat_state(numero):
        chat_states.pop(numero, None)

    monkeypatch.setattr(webhook_module, 'enviar_mensaje', fake_enviar)
    monkeypatch.setattr(webhook_module, 'set_user_step', fake_set_user_step)
    monkeypatch.setattr(webhook_module, 'get_chat_state', fake_get_chat_state)
    monkeypatch.setattr(webhook_module, 'update_chat_state', fake_update_chat_state)
    monkeypatch.setattr(webhook_module, 'delete_chat_state', fake_delete_chat_state)

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


def test_dispatch_rule_invokes_ai_only_for_ia_rule(monkeypatch, patch_dependencies):
    responses = {'menu': []}
    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )
    monkeypatch.setattr(webhook_module, 'advance_steps', lambda *_, **__: None)
    monkeypatch.setattr(
        webhook_module,
        'obtener_ultimo_mensaje_cliente',
        lambda *_: 'mensaje cliente',
    )

    ai_calls = []
    monkeypatch.setattr(
        webhook_module,
        '_reply_with_ai',
        lambda *args, **kwargs: ai_calls.append((args, kwargs)),
    )

    regla = (
        77,
        'prompt del sistema',
        'ia',
        'texto',
        None,
        None,
        None,
        'ia',
    )

    webhook_module.dispatch_rule('5215551234', regla, step='menu')

    assert len(ai_calls) == 1
    assert ai_calls[0][0][0] == '5215551234'


def test_dispatch_rule_invokes_ai_for_wildcard_in_ia_step(monkeypatch, patch_dependencies):
    responses = {'ia_chat': []}
    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )
    monkeypatch.setattr(webhook_module, 'advance_steps', lambda *_, **__: None)
    monkeypatch.setattr(
        webhook_module,
        'obtener_ultimo_mensaje_cliente',
        lambda *_: 'mensaje cliente',
    )

    ai_calls = []
    monkeypatch.setattr(
        webhook_module,
        '_reply_with_ai',
        lambda *args, **kwargs: ai_calls.append((args, kwargs)),
    )

    regla = (
        88,
        'prompt del sistema',
        'ia',
        'texto',
        None,
        None,
        None,
        '*',
    )

    webhook_module.dispatch_rule('5215551234', regla, step='ia_chat')

    assert len(ai_calls) == 1
    assert ai_calls[0][0][0] == '5215551234'


def test_dispatch_rule_skips_ai_for_non_ia_rules(monkeypatch, patch_dependencies):
    responses = {'ia': []}
    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )
    monkeypatch.setattr(webhook_module, 'advance_steps', lambda *_, **__: None)

    ai_calls = []
    monkeypatch.setattr(
        webhook_module,
        '_reply_with_ai',
        lambda *args, **kwargs: ai_calls.append((args, kwargs)),
    )

    regla = (
        5,
        'respuesta normal',
        'siguiente',
        'texto',
        None,
        None,
        None,
        'hola',
    )

    webhook_module.dispatch_rule('5215554321', regla, step='ia')

    assert ai_calls == []
    assert patch_dependencies.sent_messages[-1][0] == '5215554321'


def test_handle_text_message_delays_ia_chat(monkeypatch):
    now = datetime.now()
    chat_state = {"row": ("ia_chat", now, "ia_chat_pending")}
    update_calls = []
    ai_calls = []
    chain_calls = []

    monkeypatch.setattr(webhook_module, '_get_session_timeout', lambda: 0)
    monkeypatch.setattr(webhook_module, 'guardar_mensaje', lambda *_, **__: None)
    monkeypatch.setattr(webhook_module, 'handle_global_command', lambda *_: False)
    monkeypatch.setattr(webhook_module, '_reply_with_ai', lambda *args, **__: ai_calls.append(args))
    monkeypatch.setattr(
        webhook_module,
        'process_step_chain',
        lambda *args, **__: chain_calls.append(args) or False,
    )

    def fake_get_chat_state(_):
        return chat_state["row"]

    def fake_update_chat_state(_, step, estado=None):
        update_calls.append((step, estado))
        chat_state["row"] = (step, datetime.now(), estado)

    monkeypatch.setattr(webhook_module, 'get_chat_state', fake_get_chat_state)
    monkeypatch.setattr(webhook_module, 'update_chat_state', fake_update_chat_state)

    webhook_module.handle_text_message("5215559999", "hola")

    assert len(ai_calls) == 1
    assert chain_calls == [("5215559999", "hola")]
    assert update_calls[-1] == ("ia_chat", "espera_usuario")


def test_is_schedule_active_honors_overnight_ranges():
    tzinfo = ZoneInfo("America/Bogota")
    hours = "17:30-07:30"
    days = "lun, mar, mie, jue, vie"

    assert webhook_module._is_schedule_active(
        hours,
        days,
        datetime(2024, 4, 2, 6, 15, tzinfo=tzinfo),
    )
    assert webhook_module._is_schedule_active(
        hours,
        days,
        datetime(2024, 4, 6, 6, 15, tzinfo=tzinfo),
    )
    assert not webhook_module._is_schedule_active(
        hours,
        days,
        datetime(2024, 4, 2, 12, 0, tzinfo=tzinfo),
    )

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


def test_process_step_chain_skips_wildcard_when_disabled(monkeypatch, patch_dependencies):
    responses = {
        'menu': [
            (
                21,
                'elige una opción',
                'espera',
                'texto',
                None,
                None,
                None,
                '*',
            ),
            (
                22,
                'opción 1',
                'final',
                'texto',
                None,
                None,
                None,
                '1',
            ),
        ],
        'final': [],
    }

    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )

    webhook_module.set_user_step('5215557777', 'menu')

    webhook_module.process_step_chain(
        '5215557777',
        'hola',
        allow_wildcard_with_text=False,
    )

    assert patch_dependencies.sent_messages == []


def test_process_step_chain_allows_wildcard_without_specific_rules(monkeypatch, patch_dependencies):
    responses = {
        'captura': [
            (
                30,
                'Ingresa tu nombre',
                'final',
                'texto',
                None,
                None,
                None,
                '*',
            ),
        ],
        'final': [],
    }

    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )

    webhook_module.set_user_step('5215558888', 'captura')

    webhook_module.process_step_chain(
        '5215558888',
        'Juan',
        allow_wildcard_with_text=False,
    )

    assert len(patch_dependencies.sent_messages) == 1
    numero, mensaje, step = patch_dependencies.sent_messages[0]
    assert numero == '5215558888'
    assert mensaje == 'Ingresa tu nombre'
    assert step == 'captura'


def test_branching_chain_processes_common_steps(monkeypatch, patch_dependencies):
    responses = {
        'menu': [
            (
                40,
                'elige opción 1',
                'rama_a_intro,rama_comun',
                'texto',
                None,
                None,
                None,
                '1',
            ),
            (
                41,
                'elige opción 2',
                'rama_b_intro,rama_comun',
                'texto',
                None,
                None,
                None,
                '2',
            ),
        ],
        'rama_a_intro': [
            (
                42,
                'mensaje rama A',
                'rama_comun',
                'texto',
                None,
                None,
                None,
                '*',
            )
        ],
        'rama_b_intro': [
            (
                43,
                'mensaje rama B',
                'rama_comun',
                'texto',
                None,
                None,
                None,
                '*',
            )
        ],
        'rama_comun': [
            (
                44,
                'mensaje común',
                'final',
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

    regla = responses['menu'][0]
    webhook_module.dispatch_rule('5215554321', regla, step='menu')

    mensajes = [msg for _, msg, _ in patch_dependencies.sent_messages]
    assert mensajes == ['elige opción 1', 'mensaje rama A', 'mensaje común']
    assert patch_dependencies.steps_set[-1][1] == 'final'


def test_ai_step_skipped_on_first_message(monkeypatch):
    states = {}
    ai_calls = []

    monkeypatch.setattr(webhook_module.Config, 'INITIAL_STEP', 'ia')
    monkeypatch.setattr(webhook_module, 'process_step_chain', lambda *_, **__: None)
    monkeypatch.setattr(webhook_module, 'handle_global_command', lambda *_, **__: False)
    monkeypatch.setattr(webhook_module, 'guardar_mensaje', lambda *_, **__: None)

    def fake_get_chat_state(numero):
        return states.get(numero)

    def fake_update_chat_state(numero, step, estado='espera_usuario'):
        states[numero] = (step, datetime.utcnow(), estado)

    monkeypatch.setattr(webhook_module, 'get_chat_state', fake_get_chat_state)
    monkeypatch.setattr(webhook_module, 'update_chat_state', fake_update_chat_state)
    monkeypatch.setattr(webhook_module, 'delete_chat_state', lambda numero: states.pop(numero, None))
    monkeypatch.setattr(webhook_module, '_reply_with_ai', lambda *args, **kwargs: ai_calls.append((args, kwargs)))

    webhook_module.handle_text_message('5215552468', 'hola')

    assert states['5215552468'][0] == 'ia'
    assert ai_calls == []


def test_rule_sequence_respects_order_with_ai_and_flow(monkeypatch, patch_dependencies):
    responses = {
        'iniciar': [
            (
                101,
                'saludo inicial',
                'menu_principal',
                'texto',
                None,
                None,
                None,
                '*',
            )
        ],
        'menu_principal': [
            (
                102,
                '',
                'flow_step',
                'texto',
                None,
                None,
                None,
                'ia',
            )
        ],
        'flow_step': [
            (
                103,
                '',
                None,
                'flow',
                None,
                '{"flow_cta":"Continuar","flow_name":"flow_prueba"}',
                None,
                '*',
            )
        ],
    }

    monkeypatch.setattr(webhook_module.Config, 'INITIAL_STEP', 'iniciar')
    monkeypatch.setattr(
        webhook_module,
        'get_connection',
        lambda: DummyConnection(responses),
    )
    monkeypatch.setattr(webhook_module, 'guardar_mensaje', lambda *_, **__: None)
    monkeypatch.setattr(webhook_module, 'handle_global_command', lambda *_, **__: False)
    monkeypatch.setattr(
        webhook_module,
        'obtener_ultimo_mensaje_cliente',
        lambda *_: 'mensaje cliente',
    )

    ai_calls = []
    monkeypatch.setattr(
        webhook_module,
        '_reply_with_ai',
        lambda *args, **kwargs: ai_calls.append((args, kwargs)),
    )

    webhook_module.handle_text_message('5215551111', 'hola')
    webhook_module.handle_text_message('5215551111', 'ia')

    mensajes = [msg for _, msg, _ in patch_dependencies.sent_messages]
    steps = [step for _, _, step in patch_dependencies.sent_messages]

    assert mensajes == ['saludo inicial', '']
    assert steps == ['iniciar', 'flow_step']
    assert len(ai_calls) == 1
    assert patch_dependencies.steps_set[-1][1] == 'flow_step'
