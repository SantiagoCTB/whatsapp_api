import sys
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import webhook as webhook_module
from services import tenants


class DummyTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_process_buffered_messages_handles_multiple_entries(monkeypatch):
    calls = []

    def fake_handle(numero, texto, save=True):
        calls.append((numero, texto, save))

    monkeypatch.setattr(webhook_module, 'handle_text_message', fake_handle)
    monkeypatch.setattr(webhook_module, 'message_buffer', {})
    monkeypatch.setattr(webhook_module, 'pending_timers', {})
    monkeypatch.setattr(webhook_module, 'cache_lock', threading.Lock())

    timer = DummyTimer()
    with webhook_module.cache_lock:
        webhook_module.message_buffer['5215550000'] = [
            {'raw': 'Hola', 'normalized': 'hola'},
            {'raw': '2', 'normalized': '2'},
        ]
        webhook_module.pending_timers['5215550000'] = timer

    webhook_module.process_buffered_messages('5215550000')

    assert timer.cancelled is True
    assert '5215550000' not in webhook_module.message_buffer
    assert '5215550000' not in webhook_module.pending_timers
    assert calls == [
        ('5215550000', 'Hola', False),
        ('5215550000', '2', False),
    ]


def test_process_buffered_messages_skips_empty_entries(monkeypatch):
    calls = []

    def fake_handle(numero, texto, save=True):
        calls.append((numero, texto, save))

    monkeypatch.setattr(webhook_module, 'handle_text_message', fake_handle)
    monkeypatch.setattr(webhook_module, 'message_buffer', {})
    monkeypatch.setattr(webhook_module, 'pending_timers', {})
    monkeypatch.setattr(webhook_module, 'cache_lock', threading.Lock())

    timer = DummyTimer()
    with webhook_module.cache_lock:
        webhook_module.message_buffer['5215550001'] = [
            {'raw': '', 'normalized': ''},
            {'raw': '   ', 'normalized': ''},
            '   ',
            {'raw': 'Listo', 'normalized': 'listo'},
        ]
        webhook_module.pending_timers['5215550001'] = timer

    webhook_module.process_buffered_messages('5215550001')

    assert timer.cancelled is True
    assert '5215550001' not in webhook_module.message_buffer
    assert '5215550001' not in webhook_module.pending_timers
    assert calls == [('5215550001', 'Listo', False)]


def test_process_buffered_messages_sets_tenant_context(monkeypatch):
    env_for_tenant = {'META_TOKEN': 'abc', 'PHONE_NUMBER_ID': '123'}
    default_env = {'fallback': True}
    env_used = []

    def fake_handle(numero, texto, save=True):
        env_used.append(tenants.get_current_tenant_env())

    def fake_set_current_tenant_env(env):
        env_used.append(env)

    def fake_set_current_tenant(tenant_obj):
        fake_set_current_tenant_env(env_for_tenant if tenant_obj else default_env)

    monkeypatch.setattr(webhook_module, 'handle_text_message', fake_handle)
    monkeypatch.setattr(webhook_module, 'message_buffer', {})
    monkeypatch.setattr(webhook_module, 'pending_timers', {})
    monkeypatch.setattr(webhook_module, 'cache_lock', threading.Lock())
    monkeypatch.setattr(tenants, 'set_current_tenant_env', fake_set_current_tenant_env)
    monkeypatch.setattr(tenants, 'set_current_tenant', fake_set_current_tenant)
    monkeypatch.setattr(tenants, 'clear_current_tenant', lambda: None)
    monkeypatch.setattr(tenants, 'get_tenant', lambda key: object() if key == 'acme' else None)
    monkeypatch.setattr(tenants, 'get_tenant_env', lambda tenant=None: default_env)
    monkeypatch.setattr(tenants, '_CURRENT_TENANT_ENV', threading.local())
    monkeypatch.setattr(tenants, 'get_current_tenant_env', lambda: env_used[-1] if env_used else default_env)

    timer = DummyTimer()
    with webhook_module.cache_lock:
        webhook_module.message_buffer['5215550002'] = [
            {
                'raw': 'Hola',
                'normalized': 'hola',
                'tenant_key': 'acme',
                'tenant_env': env_for_tenant,
            }
        ]
        webhook_module.pending_timers['5215550002'] = timer

    webhook_module.process_buffered_messages('5215550002')

    assert env_for_tenant in env_used
    assert env_used[-1] == env_for_tenant
