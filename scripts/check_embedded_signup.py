"""Utilidad de diagnóstico para Embedded Signup.

Ejecuta comprobaciones básicas desde la línea de comandos para ayudar a
identificar por qué el cuadro de login de Facebook no aparece en
``/configuracion/signup``.

Uso:
    python scripts/check_embedded_signup.py
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from typing import Iterable

from config import Config


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        emoji = "✅" if self.ok else "⚠️"
        return f"{emoji} {self.name}: {self.detail}"


def _mask_value(value: str | None, visible: int = 4) -> str:
    if not value:
        return "<vacío>"
    if len(value) <= visible:
        return value
    return f"{value[:visible]}…(len={len(value)})"


def check_env_vars() -> Iterable[CheckResult]:
    yield CheckResult(
        "FACEBOOK_APP_ID",
        bool(Config.FACEBOOK_APP_ID),
        _mask_value(Config.FACEBOOK_APP_ID),
    )
    yield CheckResult(
        "SIGNUP_FACEBOOK",
        bool(Config.SIGNUP_FACEBOOK),
        _mask_value(Config.SIGNUP_FACEBOOK),
    )
    yield CheckResult(
        "DEFAULT_TENANT",
        bool(Config.DEFAULT_TENANT),
        Config.DEFAULT_TENANT or "<no definido>",
    )


def check_https_forwarding() -> CheckResult:
    value = os.getenv("PREFERRED_URL_SCHEME") or os.getenv("FLASK_PREFERRED_URL_SCHEME")
    ok = value == "https"
    detail = value or "<no establecido>"
    return CheckResult("Preferencia de esquema HTTPS", ok, detail)


def check_facebook_dns() -> CheckResult:
    host = "connect.facebook.net"
    try:
        socket.gethostbyname(host)
        return CheckResult("Resolución DNS de Facebook", True, host)
    except socket.gaierror as exc:  # pragma: no cover - depende del entorno
        return CheckResult("Resolución DNS de Facebook", False, f"Error: {exc}")


def run() -> int:
    checks: list[CheckResult] = []
    checks.extend(check_env_vars())
    checks.append(check_https_forwarding())
    checks.append(check_facebook_dns())

    print("Diagnóstico Embedded Signup")
    print("===========================")
    for check in checks:
        print(check.render())

    failing = [c for c in checks if not c.ok]
    if failing:
        print("\nSugerencias:")
        for item in failing:
            if item.name == "FACEBOOK_APP_ID":
                print(" - Define FACEBOOK_APP_ID en el contenedor web y reinicia.")
            elif item.name == "SIGNUP_FACEBOOK":
                print(" - Define SIGNUP_FACEBOOK con el config_id provisto por Meta.")
            elif item.name == "DEFAULT_TENANT":
                print(" - Revisa que DEFAULT_TENANT esté configurado para resolver el tenant actual.")
            elif item.name == "Preferencia de esquema HTTPS":
                print(" - Sirve la página bajo HTTPS; el flujo embebido requiere HTTPS en producción.")
            elif item.name == "Resolución DNS de Facebook":
                print(" - Verifica conectividad saliente hacia Facebook (firewall/DNS).")

    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(run())

