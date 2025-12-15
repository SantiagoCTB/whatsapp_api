# app.py
from flask import Flask, abort, g, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import os
import logging
import sys

load_dotenv()

from config import Config

from services import tenants
from routes.auth_routes import auth_bp
from routes.chat_routes import chat_bp
from routes.configuracion import config_bp
from routes.roles_routes import roles_bp
from routes.tenant_admin_routes import tenant_admin_bp
from routes.users_routes import users_bp
from routes.webhook import webhook_bp
from routes.tablero_routes import tablero_bp
from routes.export_routes import export_bp

def _ensure_media_root():
    """Create the directory where user uploads are stored."""
    env = tenants.get_tenant_env(None)
    media_root = env.get("MEDIA_ROOT") or Config.MEDIA_ROOT
    try:
        os.makedirs(media_root, exist_ok=True)
        logging.getLogger(__name__).info("MEDIA_ROOT inicializado en %s", media_root)
    except OSError as exc:
        raise RuntimeError(
            f"No se pudo preparar el directorio de medios en '{media_root}'."
        ) from exc

    static_uploads = os.path.join(Config.BASEDIR, "static", "uploads")
    if os.path.abspath(static_uploads) != media_root:
        logging.getLogger(__name__).warning(
            "MEDIA_ROOT (%s) es distinto del directorio estándar de static/uploads. "
            "Asegúrate de exponerlo correctamente en tu servidor.",
            media_root,
        )


def create_app():
    _ensure_media_root()
    app = Flask(
        __name__,
        static_folder=os.path.join(Config.BASEDIR, "static"),
        template_folder=os.path.join(Config.BASEDIR, "templates"),
    )
    # Si usas clase de config:
    app.config.from_object(Config)
    app.config.setdefault("PREFERRED_URL_SCHEME", "https")

    # Honra los encabezados de Nginx cuando estamos detrás de un proxy TLS.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)

    if not app.debug:
        log_format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                logging.FileHandler('app.log'),
                logging.StreamHandler(sys.stdout)
            ]
        )

    # Registra blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(roles_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(tenant_admin_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(tablero_bp)
    app.register_blueprint(export_bp)

    @app.before_request
    def bind_tenant():
        header_key = request.headers.get(Config.TENANT_HEADER)
        query_key = request.args.get("tenant")
        form_key = request.form.get("tenant") if request.method == "POST" else None
        session_key = session.get("tenant")

        tenant_key = (
            header_key
            or query_key
            or form_key
            or session_key
            or Config.DEFAULT_TENANT
        )

        if not tenant_key:
            # Modo legacy (single-tenant): no se exige encabezado ni tenant
            # por defecto. Se usa la base configurada en DB_*.
            g.tenant = None
            tenants.clear_current_tenant()
            tenants.set_current_tenant_env(tenants.get_tenant_env(None))
            session.pop("tenant", None)
            return

        try:
            if header_key or query_key:
                tenant = tenants.resolve_tenant_from_request(request)
            else:
                tenant = tenants.get_tenant(tenant_key)
                if tenant is None:
                    raise tenants.TenantNotFoundError(
                        f"No se encontró la empresa '{tenant_key}'."
                    )
        except tenants.TenantResolutionError as exc:
            abort(400, description=str(exc))
        except tenants.TenantNotFoundError as exc:
            abort(404, description=str(exc))

        g.tenant = tenant
        session["tenant"] = tenant.tenant_key
        tenants.set_current_tenant(tenant)
        tenants.set_current_tenant_env(tenants.get_tenant_env(tenant))

    @app.teardown_request
    def clear_tenant_context(exc):
        tenants.clear_current_tenant()

    with app.app_context():
        tenants.bootstrap_tenant_registry()
        default_tenant = tenants.ensure_default_tenant_registered()

        # Inicializa BD por defecto para evitar errores en entornos nuevos.
        # Puede deshabilitarse con INIT_DB_ON_START=0 si se prefiere controlar
        # la migración manualmente.
        if os.getenv("INIT_DB_ON_START", "1") != "0":
            if default_tenant:
                tenants.ensure_tenant_schema(default_tenant)
            tenants.ensure_registered_tenants_schema(
                skip={default_tenant.tenant_key} if default_tenant else None
            )

    return app

# Objeto WSGI para Gunicorn
app = create_app()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
