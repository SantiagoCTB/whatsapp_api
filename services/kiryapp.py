"""Cliente para la API de terceros de KiryApp (venta de tiquetes de bus).

Host por defecto: kiryapp.agestion.net:63431
Autenticación: Bearer Token en el header Authorization.

Todos los métodos retornan el dict JSON decodificado de la respuesta.
Lanzan ``KiryappError`` si el servidor devuelve un código de error HTTP.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20  # segundos


class KiryappError(Exception):
    """Error devuelto por la API de KiryApp."""
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Modelos de request
# ---------------------------------------------------------------------------

@dataclass
class BearingByIdRequest:
    bearingId: int
    originId: int
    destinyId: int


@dataclass
class TicketPassenger:
    """Datos de un pasajero / asiento dentro de una reserva."""
    bearingId: int
    seatId: str
    date: str = ""          # se saca del bearing
    destiny: int = 0
    origin: int = 0
    details: str = "WHATSAPP"
    document: str = ""
    name: str = ""
    telephone: str = ""


@dataclass
class TicketSaleRequest:
    """Body para POST /api/v1/ticket-sale/reserve."""
    customerName: str
    customerLastName: str
    customerDocument: str
    customerDocumentType: int
    customerEmail: str
    customerPhone: str
    customerAddress: str
    tickets: list[dict] = field(default_factory=list)
    SellerName: str = "WHATSAPP"


@dataclass
class PaymentEntry:
    paymentMethod: int
    value: str
    reference: str = ""


@dataclass
class TicketPayRequest:
    """Body para POST /api/v1/ticket-sale/pay."""
    thirdClientId: int
    ticketsToPay: list[dict]  # [{bearingId, id, seatId}]
    payments: list[dict]      # [{paymentMethod, value, reference}]


# ---------------------------------------------------------------------------
# Cliente principal
# ---------------------------------------------------------------------------

class KiryappClient:
    """Wrapper tipado para la API de KiryApp."""

    def __init__(self, base_url: str, token: str = "", timeout: int = _DEFAULT_TIMEOUT, auth_header: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        auth_hdr = auth_header or (f"Bearer {token}" if token else "")
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if auth_hdr:
            self._session.headers["Authorization"] = auth_hdr

    # ── helpers ─────────────────────────────────────────────────────────────

    def _get(self, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, timeout=self.timeout, **kwargs)
        return self._parse(resp)

    def _post(self, path: str, body: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._session.post(url, json=body, timeout=self.timeout)
        return self._parse(resp)

    @staticmethod
    def _parse(resp: requests.Response) -> Any:
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        if resp.status_code >= 400:
            msg = f"KiryApp API error {resp.status_code}"
            if isinstance(data, dict):
                msg = data.get("message") or data.get("error") or msg
            raise KiryappError(str(msg), status_code=resp.status_code, body=data)
        return data

    # ── endpoints ────────────────────────────────────────────────────────────

    def get_origins_and_destinies(self) -> Any:
        """GET /api/v1/origins-and-destinies"""
        return self._get("/api/v1/origins-and-destinies")

    def get_bearing_by_route(
        self,
        origin_id: int | str,
        destiny_id: int | str,
        departure_date: str,
    ) -> Any:
        """GET /api/v1/bearing/by-route/{originId}/{destinyId}/{departureDate}"""
        return self._get(
            f"/api/v1/bearing/by-route/{origin_id}/{destiny_id}/{departure_date}"
        )

    def get_bearing_by_id(
        self,
        bearing_id: int,
        origin_id: int,
        destiny_id: int,
    ) -> Any:
        """POST /api/v1/bearing/by-id"""
        return self._post(
            "/api/v1/bearing/by-id",
            {"bearingId": bearing_id, "originId": origin_id, "destinyId": destiny_id},
        )

    def reserve_ticket(self, request_data: dict) -> Any:
        """POST /api/v1/ticket-sale/reserve"""
        return self._post("/api/v1/ticket-sale/reserve", request_data)

    def cancel_ticket(self, tickets_to_cancel: list[dict]) -> Any:
        """POST /api/v1/ticket-sale/cancel"""
        return self._post(
            "/api/v1/ticket-sale/cancel",
            {"ticketsToCancel": tickets_to_cancel},
        )

    def pay_ticket(self, request_data: dict) -> Any:
        """POST /api/v1/ticket-sale/pay"""
        return self._post("/api/v1/ticket-sale/pay", request_data)

    def annulate_ticket(self, third_client_id: int, tickets: list[dict]) -> Any:
        """POST /api/v1/ticket-sale/annulate"""
        return self._post(
            "/api/v1/ticket-sale/annulate",
            {"thirdClientId": third_client_id, "ticketsToAnnulate": tickets},
        )

    def get_ticket_state(self, ticket_ids: list[int]) -> Any:
        """POST /api/v1/ticket-sale/ticket-state"""
        return self._post(
            "/api/v1/ticket-sale/ticket-state",
            {"ticketIds": ticket_ids},
        )


# ---------------------------------------------------------------------------
# Factory: crea un cliente desde una api_conexion guardada en la BD
# ---------------------------------------------------------------------------

def get_client_from_conexion(conexion_id: int) -> KiryappClient:
    """Construye un KiryappClient usando la conexión almacenada en api_conexiones."""
    from services import db as db_service  # import tardío para evitar circulares

    row = db_service.get_conexion(conexion_id)
    if not row:
        raise KiryappError(f"No se encontró la conexión con id={conexion_id}.")

    base_url = (row.get("url") or "").strip()
    auth_tipo = (row.get("auth_tipo") or "none").lower()
    auth_valor = (row.get("auth_valor") or "").strip()

    if auth_tipo == "bearer" and auth_valor:
        return KiryappClient(base_url=base_url, token=auth_valor)

    # Fallback: leer Authorization desde el campo headers JSON de la conexión
    import json as _json
    headers_raw = (row.get("headers") or "").strip()
    if headers_raw:
        try:
            h = _json.loads(headers_raw)
            if isinstance(h, dict) and h.get("Authorization"):
                return KiryappClient(base_url=base_url, auth_header=h["Authorization"])
        except Exception:
            pass

    raise KiryappError(
        "La conexión de KiryApp debe usar autenticación Bearer con un token configurado."
    )
