import sys
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import webhook as webhook_module


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
