import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import auth_routes


def setup_function():
    auth_routes._login_attempts.clear()
    auth_routes._login_locked_until.clear()


def test_valid_username_allows_safe_chars():
    assert auth_routes._is_valid_username("user.name-01")
    assert auth_routes._is_valid_username("admin@example.com")


def test_invalid_username_blocks_sql_like_payloads():
    assert not auth_routes._is_valid_username("admin' OR '1'='1")
    assert not auth_routes._is_valid_username("<script>alert(1)</script>")
    assert not auth_routes._is_valid_username("ab")


def test_action_rate_limit_locks_after_threshold(monkeypatch):
    monkeypatch.setattr(auth_routes.Config, "LOGIN_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(auth_routes.Config, "LOGIN_WINDOW_SECONDS", 60)
    monkeypatch.setattr(auth_routes.Config, "LOGIN_LOCKOUT_SECONDS", 120)

    key = "login:127.0.0.1:test"
    auth_routes._register_failed_attempt(key)
    locked, _ = auth_routes._is_action_locked(key)
    assert not locked

    auth_routes._register_failed_attempt(key)
    locked, remaining = auth_routes._is_action_locked(key)
    assert locked
    assert remaining > 0


def test_clear_failed_attempts_unlocks_key(monkeypatch):
    monkeypatch.setattr(auth_routes.Config, "LOGIN_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(auth_routes.Config, "LOGIN_WINDOW_SECONDS", 60)
    monkeypatch.setattr(auth_routes.Config, "LOGIN_LOCKOUT_SECONDS", 120)

    key = "password-change:127.0.0.1:test"
    auth_routes._register_failed_attempt(key)
    locked, _ = auth_routes._is_action_locked(key)
    assert locked

    auth_routes._clear_failed_attempts(key)
    locked, _ = auth_routes._is_action_locked(key)
    assert not locked


def test_login_and_password_change_use_independent_counters(monkeypatch):
    monkeypatch.setattr(auth_routes.Config, "LOGIN_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(auth_routes.Config, "LOGIN_WINDOW_SECONDS", 60)
    monkeypatch.setattr(auth_routes.Config, "LOGIN_LOCKOUT_SECONDS", 120)

    login_key = "login:127.0.0.1:test"
    pwd_key = "password-change:127.0.0.1:test"

    auth_routes._register_failed_attempt(login_key)

    login_locked, _ = auth_routes._is_action_locked(login_key)
    pwd_locked, _ = auth_routes._is_action_locked(pwd_key)

    assert login_locked
    assert not pwd_locked
