import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import webhook as webhook_module


class _FakeTimer:
    def __init__(self, delay_seconds, callback, args=(), kwargs=None):
        self.delay_seconds = delay_seconds
        self.callback = callback
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = False
        self._alive = False

    def start(self):
        self._alive = True

    def cancel(self):
        self._alive = False

    def is_alive(self):
        return self._alive


def _build_followup_config():
    return {
        "interval_minutes": 5,
        "messages": [
            {"text": "Hola", "media_url": None, "media_tipo": None},
            {"text": "Seguimos atentos", "media_url": None, "media_tipo": None},
        ],
    }


def test_followup_schedule_only_one_round_per_conversation(monkeypatch):
    created_timers = []

    def timer_factory(delay_seconds, callback, args=(), kwargs=None):
        timer = _FakeTimer(delay_seconds, callback, args=args, kwargs=kwargs)
        created_timers.append(timer)
        return timer

    monkeypatch.setattr(webhook_module.threading, "Timer", timer_factory)
    monkeypatch.setattr(webhook_module, "_schedule_unattended_alert", lambda *_: None)
    monkeypatch.setattr(webhook_module, "_get_ia_followup_config", _build_followup_config)

    webhook_module._clear_followup_timers("573001112233")
    webhook_module._schedule_followup_messages("573001112233", "ia_chat")
    first_round_count = len(created_timers)
    webhook_module._schedule_followup_messages("573001112233", "ia_chat")

    assert first_round_count == 2
    assert len(created_timers) == first_round_count
    assert len(webhook_module.followup_timers["573001112233"]) == first_round_count
