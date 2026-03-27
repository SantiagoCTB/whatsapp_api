from pathlib import Path
import sys

from flask import Blueprint, Flask


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import users_routes


class _FakeCursor:
    def __init__(self):
        self.queries = []
        self._fetchall_calls = 0

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))

    def fetchall(self):
        self._fetchall_calls += 1
        if self._fetchall_calls == 1:
            return [(1, "Administrador")]
        return [
            (2, "agente", "Agente", "Administrador"),
        ]


class _FakeConnection:
    def __init__(self):
        self.cursor_instance = _FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


def _app_with_admin_session():
    app = Flask(__name__, template_folder=str(ROOT_DIR / "templates"))
    app.secret_key = "test"
    chat_bp = Blueprint("chat", __name__)

    @chat_bp.route("/")
    def index():
        return "ok"

    app.register_blueprint(chat_bp)
    app.register_blueprint(users_routes.users_bp)

    @app.before_request
    def _fake_login():
        from flask import session

        session["user"] = "admin"
        session["roles"] = ["admin"]

    return app


def test_manage_users_excludes_superadmin_from_listing(monkeypatch):
    app = _app_with_admin_session()
    fake_conn = _FakeConnection()
    monkeypatch.setattr(users_routes, "get_connection", lambda: fake_conn)

    with app.test_client() as client:
        response = client.get("/usuarios")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "agente" in body
    assert "superadmin" not in body

    users_query = fake_conn.cursor_instance.queries[-1][0]
    assert "WHERE LOWER(u.username) <> 'superadmin'" in users_query
