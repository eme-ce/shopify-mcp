#!/usr/bin/env python3
import json
import os
import sys
import logging
import time
import asyncio
import secrets
from collections import deque
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import httpx
import nh3
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

SHOPIFY_STORE           = os.environ.get("SHOPIFY_STORE", "")
API_VERSION             = os.environ.get("SHOPIFY_API_VERSION", "2025-01")
PORT                    = int(os.environ.get("PORT", "8000"))
BEARER_TOKEN            = os.environ.get("BEARER_TOKEN", "")
ALLOW_TOKEN_QUERY_PARAM = os.environ.get("ALLOW_TOKEN_QUERY_PARAM", "").lower() in ("1", "true", "yes")
MAX_REQUEST_BODY        = int(os.environ.get("MAX_REQUEST_BODY", str(1 * 1024 * 1024)))
TOKEN_REFRESH_BUFFER    = int(os.environ.get("TOKEN_REFRESH_BUFFER", "1800"))

_RATE_LIMIT_RPM  = int(os.environ.get("RATE_LIMIT_RPM", "60"))
_MAX_TRACKED_IPS = int(os.environ.get("RATE_LIMIT_MAX_IPS", "10000"))
_TRUSTED_PROXIES = max(0, int(os.environ.get("TRUSTED_PROXY_COUNT", "1")))

# Se aplica a cada llamada saliente a la API de Shopify — evita que los workers queden colgados cuando Shopify está lento o no responde.
_SHOPIFY_TIMEOUT = httpx.Timeout(30.0)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("shopify_mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Validación de inicio
# ---------------------------------------------------------------------------

if not BEARER_TOKEN and not os.environ.get("ALLOW_OPEN_SERVER"):
    logger.critical(
        "BEARER_TOKEN is not set. Refusing to start without authentication. "
        "Set BEARER_TOKEN in your environment variables, or set ALLOW_OPEN_SERVER=1 to bypass (not recommended)."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Limitador de tasa (ventana deslizante, en proceso)
# ---------------------------------------------------------------------------

_rate_limit_store: dict[str, deque] = {}
_rate_limit_lock = asyncio.Lock()


async def _check_rate_limit(ip: str) -> bool:
    now    = time.monotonic()
    window = 60.0
    async with _rate_limit_lock:
        if ip not in _rate_limit_store:
            if len(_rate_limit_store) >= _MAX_TRACKED_IPS:
                # Elimina entradas vencidas antes de fallar en modo abierto — recupera espacio
                # ocupado por la rotación de IPs de bots.
                stale = [k for k, v in _rate_limit_store.items() if not v or now - max(v) >= window]
                for k in stale:
                    del _rate_limit_store[k]
                if len(_rate_limit_store) >= _MAX_TRACKED_IPS:
                    logger.warning("rate-limit store full, failing open for %s", ip)
                    return True
            _rate_limit_store[ip] = deque()
        dq = _rate_limit_store[ip]
        while dq and now - dq[0] >= window:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT_RPM:
            return False
        dq.append(now)
        return True


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff and _TRUSTED_PROXIES > 0:
        parts = [p.strip() for p in xff.split(",")]
        idx   = max(0, len(parts) - _TRUSTED_PROXIES)
        return parts[idx]
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Gestor de tokens
# ---------------------------------------------------------------------------

class TokenManager:
    """
    Dos modos:
      1. Estático — SHOPIFY_ACCESS_TOKEN
      2. OAuth    — SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (auto-renovación al expirar)

    Las credenciales se leen del entorno en el momento de uso, no se guardan como atributos de instancia.
    """

    def __init__(self, store: str, refresh_buffer: int = 1800):
        self._store          = store
        self._refresh_buffer = refresh_buffer

        self._access_token: str   = ""
        self._expires_at:   float = 0.0
        self._lock = asyncio.Lock()

        client_id     = os.environ.get("SHOPIFY_CLIENT_ID", "")
        client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
        static_token  = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")

        self._use_client_credentials = bool(client_id and client_secret)

        if self._use_client_credentials:
            logger.info("Token mode: client_credentials (auto-refresh enabled)")
        elif static_token:
            logger.info("Token mode: static SHOPIFY_ACCESS_TOKEN (no auto-refresh)")
            self._access_token = static_token
            self._expires_at   = float("inf")
        else:
            logger.warning(
                "No credentials configured. Set SHOPIFY_ACCESS_TOKEN or "
                "SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET."
            )

    @property
    def is_expired(self) -> bool:
        if not self._access_token:
            return True
        return time.time() >= (self._expires_at - self._refresh_buffer)

    @property
    def is_oauth_capable(self) -> bool:
        return self._use_client_credentials

    async def get_token(self) -> str:
        if not self.is_expired:
            return self._access_token
        async with self._lock:
            if not self.is_expired:
                return self._access_token
            if self._use_client_credentials:
                await self._do_refresh()
            elif not self._access_token:
                raise RuntimeError(
                    "No valid token available. "
                    "Set SHOPIFY_ACCESS_TOKEN in your environment variables."
                )
        return self._access_token

    async def force_refresh(self) -> str:
        if not self._use_client_credentials:
            raise RuntimeError(
                "Cannot refresh — using a static token. "
                "Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET to enable auto-refresh."
            )
        async with self._lock:
            await self._do_refresh()
        return self._access_token

    async def _do_refresh(self) -> None:
        client_id     = os.environ.get("SHOPIFY_CLIENT_ID", "")
        client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "SHOPIFY_CLIENT_ID or SHOPIFY_CLIENT_SECRET missing from environment."
            )

        url = f"https://{self._store}.myshopify.com/admin/oauth/access_token"
        logger.info("Refreshing Shopify access token via client_credentials grant...")

        async with httpx.AsyncClient(timeout=_SHOPIFY_TIMEOUT) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                logger.error("Token refresh failed (%s): %s", resp.status_code, resp.text[:500])
                raise RuntimeError(
                    f"Token refresh failed ({resp.status_code}). "
                    "Check SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET."
                )

            data               = resp.json()
            self._access_token = data["access_token"]
            expires_in         = data.get("expires_in", 86399)
            self._expires_at   = time.time() + expires_in

            scope         = data.get("scope", "")
            scope_preview = scope[:80] + "..." if len(scope) > 80 else scope
            logger.info(
                "Token refreshed. Expires in %ds (%dh %dm). Scopes: %s",
                expires_in, expires_in // 3600, (expires_in % 3600) // 60, scope_preview,
            )


_token_manager = TokenManager(store=SHOPIFY_STORE, refresh_buffer=TOKEN_REFRESH_BUFFER)

# ---------------------------------------------------------------------------
# Funciones auxiliares HTTP
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}"


async def _headers() -> dict:
    token = await _token_manager.get_token()
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


async def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body:   Optional[dict] = None,
    _retried: bool = False,
) -> dict:
    """Función auxiliar HTTP central — todas las llamadas a la API pasan por acá.
    Reintenta una vez automáticamente ante un 401 cuando se usan credenciales OAuth.
    """
    if not SHOPIFY_STORE:
        raise RuntimeError(
            "Missing SHOPIFY_STORE environment variable. "
            "Set it before starting the server."
        )

    if method in ("POST", "PUT", "PATCH", "DELETE") and not _retried:
        logger.info("AUDIT %s %s", method, path)

    url     = f"{_base_url()}/{path}"
    headers = await _headers()

    async with httpx.AsyncClient(timeout=_SHOPIFY_TIMEOUT) as client:
        resp = await client.request(
            method, url,
            headers=headers,
            params=params,
            json=body,
        )

        if resp.status_code == 401 and not _retried and _token_manager.is_oauth_capable:
            logger.warning("Got 401 from Shopify API — refreshing token and retrying...")
            await _token_manager.force_refresh()
            return await _request(method, path, params=params, body=body, _retried=True)

        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()


# ---------------------------------------------------------------------------
# Manejador de errores + funciones auxiliares
# ---------------------------------------------------------------------------

def _error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text[:500]
        logger.error("Shopify API error %s: %s", status, json.dumps(detail, default=str))
        messages = {
            401: "Authentication failed — check your SHOPIFY_ACCESS_TOKEN (should start with shpat_).",
            403: "Permission denied — your token may be missing required API scopes.",
            404: "Resource not found — double-check the ID.",
            422: "Validation error — Shopify rejected the request. Check your inputs and server logs.",
            429: "Rate-limited — wait a moment and retry.",
        }
        return messages.get(status, f"Shopify API error {status}. Check server logs for details.")
    if isinstance(e, httpx.TimeoutException):
        return "Request timed out — try again."
    if isinstance(e, RuntimeError):
        return str(e)
    logger.error("Unexpected error: %s: %s", type(e).__name__, e)
    return f"Unexpected error: {type(e).__name__}. Check server logs for details."


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


_ALLOWED_HTML_TAGS = {
    "p", "br", "b", "i", "strong", "em", "u", "s",
    "h1", "h2", "h3", "h4", "ul", "ol", "li",
    "a", "span", "div", "blockquote",
}


def _sanitize_html(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return nh3.clean(value, tags=_ALLOWED_HTML_TAGS)


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

mcp = FastMCP("shopify-mcp", host="0.0.0.0", port=PORT, json_response=True)


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTOS
# ═══════════════════════════════════════════════════════════════════════════

class ListProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:          Optional[int]  = Field(default=50, ge=1, le=250, description="Máximo de productos a devolver (1-250)")
    status:         Optional[str]  = Field(default=None, description="Filtra por estado: active, archived, draft")
    product_type:   Optional[str]  = Field(default=None, description="Filtra por tipo de producto")
    vendor:         Optional[str]  = Field(default=None, description="Filtra por nombre de proveedor")
    collection_id:  Optional[int]  = Field(default=None, description="Filtra por ID de colección")
    since_id:       Optional[int]  = Field(default=None, description="Paginación: devuelve productos posteriores a este ID")
    fields:         Optional[str]  = Field(default=None, description="Campos a incluir, separados por coma")


@mcp.tool(
    name="shopify_list_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_products(params: ListProductsInput) -> str:
    """Lista productos de la tienda Shopify con filtros opcionales."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        for field in ["status", "product_type", "vendor", "collection_id", "since_id", "fields"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data     = await _request("GET", "products.json", params=p)
        products = data.get("products", [])
        return _fmt({"count": len(products), "products": products})
    except Exception as e:
        return _error(e)


class GetProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., description="El ID del producto en Shopify")


@mcp.tool(
    name="shopify_get_product",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_product(params: GetProductInput) -> str:
    """Obtiene un producto por su ID, incluyendo todas sus variantes e imágenes."""
    try:
        data = await _request("GET", f"products/{params.product_id}.json")
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class CreateProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title:        str                            = Field(..., min_length=1, description="Título del producto")
    body_html:    Optional[str]                  = Field(default=None, description="Descripción en HTML")
    vendor:       Optional[str]                  = Field(default=None)
    product_type: Optional[str]                  = Field(default=None)
    tags:         Optional[str]                  = Field(default=None, description="Tags separados por coma")
    status:       Optional[str]                  = Field(default="draft", description="active, archived o draft")
    variants:     Optional[List[Dict[str, Any]]] = Field(default=None, description="Objetos de variante con precio, sku, etc.")
    options:      Optional[List[Dict[str, Any]]] = Field(default=None, description="Opciones del producto (talle, color, etc.)")
    images:       Optional[List[Dict[str, Any]]] = Field(default=None, description="Objetos de imagen con URL en src")

    @field_validator("body_html")
    @classmethod
    def sanitize_body_html(cls, v: Optional[str]) -> Optional[str]:
        return _sanitize_html(v)


@mcp.tool(
    name="shopify_create_product",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_product(params: CreateProductInput) -> str:
    """Crea un nuevo producto en la tienda Shopify."""
    try:
        product: Dict[str, Any] = {"title": params.title}
        for field in ["body_html", "vendor", "product_type", "tags", "status", "variants", "options", "images"]:
            val = getattr(params, field)
            if val is not None:
                product[field] = val
        data = await _request("POST", "products.json", body={"product": product})
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class UpdateProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id:   int            = Field(..., description="ID del producto a actualizar")
    title:        Optional[str]  = Field(default=None)
    body_html:    Optional[str]  = Field(default=None)
    vendor:       Optional[str]  = Field(default=None)
    product_type: Optional[str]  = Field(default=None)
    tags:         Optional[str]  = Field(default=None)
    status:       Optional[str]  = Field(default=None, description="active, archived o draft")
    variants:     Optional[List[Dict[str, Any]]] = Field(default=None)

    @field_validator("body_html")
    @classmethod
    def sanitize_body_html(cls, v: Optional[str]) -> Optional[str]:
        return _sanitize_html(v)


@mcp.tool(
    name="shopify_update_product",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_update_product(params: UpdateProductInput) -> str:
    """Actualiza un producto existente. Solo se modifican los campos provistos."""
    try:
        product: Dict[str, Any] = {}
        for field in ["title", "body_html", "vendor", "product_type", "tags", "status", "variants"]:
            val = getattr(params, field)
            if val is not None:
                product[field] = val
        data = await _request("PUT", f"products/{params.product_id}.json", body={"product": product})
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class DeleteProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., description="ID del producto a eliminar")


@mcp.tool(
    name="shopify_delete_product",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_delete_product(params: DeleteProductInput) -> str:
    """Elimina un producto de forma permanente. Esta acción no se puede deshacer."""
    try:
        await _request("DELETE", f"products/{params.product_id}.json")
        return f"Product {params.product_id} deleted."
    except Exception as e:
        return _error(e)


class ProductCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status:       Optional[str] = Field(default=None, description="active, archived o draft")
    vendor:       Optional[str] = Field(default=None)
    product_type: Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_count_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_count_products(params: ProductCountInput) -> str:
    """Obtiene la cantidad total de productos, opcionalmente filtrada."""
    try:
        p: Dict[str, Any] = {}
        for field in ["status", "vendor", "product_type"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data = await _request("GET", "products/count.json", params=p)
        return _fmt(data)
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# PEDIDOS
# ═══════════════════════════════════════════════════════════════════════════

class ListOrdersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:               Optional[int] = Field(default=50, ge=1, le=250)
    status:              Optional[str] = Field(default="any", description="open, closed, cancelled, any")
    financial_status:    Optional[str] = Field(default=None, description="authorized, pending, paid, refunded, voided, any")
    fulfillment_status:  Optional[str] = Field(default=None, description="shipped, partial, unshipped, unfulfilled, any")
    since_id:            Optional[int] = Field(default=None)
    created_at_min:      Optional[str] = Field(default=None, description="Fecha ISO 8601, ej. 2024-01-01T00:00:00Z")
    created_at_max:      Optional[str] = Field(default=None)
    fields:              Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_list_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_orders(params: ListOrdersInput) -> str:
    """Lista pedidos con filtros opcionales de estado, estado financiero/de cumplimiento y rango de fechas."""
    try:
        p: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        for field in ["financial_status", "fulfillment_status", "since_id", "created_at_min", "created_at_max", "fields"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data   = await _request("GET", "orders.json", params=p)
        orders = data.get("orders", [])
        return _fmt({"count": len(orders), "orders": orders})
    except Exception as e:
        return _error(e)


class GetOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(..., description="El ID del pedido en Shopify")


@mcp.tool(
    name="shopify_get_order",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_order(params: GetOrderInput) -> str:
    """Obtiene un pedido por su ID con todos los detalles."""
    try:
        data = await _request("GET", f"orders/{params.order_id}.json")
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


class OrderCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status:             Optional[str] = Field(default="any")
    financial_status:   Optional[str] = Field(default=None)
    fulfillment_status: Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_count_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_count_orders(params: OrderCountInput) -> str:
    """Obtiene la cantidad total de pedidos, opcionalmente filtrada."""
    try:
        p: Dict[str, Any] = {"status": params.status}
        for field in ["financial_status", "fulfillment_status"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data = await _request("GET", "orders/count.json", params=p)
        return _fmt(data)
    except Exception as e:
        return _error(e)


class CloseOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(..., description="ID del pedido a cerrar")


@mcp.tool(
    name="shopify_close_order",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_close_order(params: CloseOrderInput) -> str:
    """Cierra un pedido (lo marca como completado)."""
    try:
        data = await _request("POST", f"orders/{params.order_id}/close.json")
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


class CancelOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int            = Field(..., description="ID del pedido a cancelar")
    reason:   Optional[str]  = Field(default=None, description="customer, fraud, inventory, declined, other")
    email:    Optional[bool] = Field(default=True,  description="Envía email de cancelación al cliente")
    restock:  Optional[bool] = Field(default=False, description="Repone el stock de los ítems")


@mcp.tool(
    name="shopify_cancel_order",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_cancel_order(params: CancelOrderInput) -> str:
    """Cancela un pedido. Opcionalmente repone el stock y notifica al cliente."""
    try:
        body: Dict[str, Any] = {}
        for field in ["reason", "email", "restock"]:
            val = getattr(params, field)
            if val is not None:
                body[field] = val
        data = await _request("POST", f"orders/{params.order_id}/cancel.json", body=body)
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# CLIENTES
# ═══════════════════════════════════════════════════════════════════════════

class ListCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:          Optional[int] = Field(default=50, ge=1, le=250)
    since_id:       Optional[int] = Field(default=None)
    created_at_min: Optional[str] = Field(default=None, description="Fecha ISO 8601")
    created_at_max: Optional[str] = Field(default=None)
    fields:         Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_list_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_customers(params: ListCustomersInput) -> str:
    """Lista los clientes de la tienda."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        for f in ["since_id", "created_at_min", "created_at_max", "fields"]:
            val = getattr(params, f)
            if val is not None:
                p[f] = val
        data      = await _request("GET", "customers.json", params=p)
        customers = data.get("customers", [])
        return _fmt({"count": len(customers), "customers": customers})
    except Exception as e:
        return _error(e)


class SearchCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str           = Field(..., min_length=1, description="Consulta de búsqueda (nombre, email, etc.)")
    limit: Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_search_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_search_customers(params: SearchCustomersInput) -> str:
    """Busca clientes por nombre, email u otros campos."""
    try:
        p         = {"query": params.query, "limit": params.limit}
        data      = await _request("GET", "customers/search.json", params=p)
        customers = data.get("customers", [])
        return _fmt({"count": len(customers), "customers": customers})
    except Exception as e:
        return _error(e)


class GetCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int = Field(..., description="ID del cliente en Shopify")


@mcp.tool(
    name="shopify_get_customer",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_customer(params: GetCustomerInput) -> str:
    """Obtiene un cliente por su ID."""
    try:
        data = await _request("GET", f"customers/{params.customer_id}.json")
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class CreateCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    first_name:         Optional[str]  = Field(default=None)
    last_name:          Optional[str]  = Field(default=None)
    email:              Optional[str]  = Field(default=None)
    phone:              Optional[str]  = Field(default=None)
    tags:               Optional[str]  = Field(default=None)
    note:               Optional[str]  = Field(default=None)
    addresses:          Optional[List[Dict[str, Any]]] = Field(default=None)
    send_email_invite:  Optional[bool] = Field(default=False)


@mcp.tool(
    name="shopify_create_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_customer(params: CreateCustomerInput) -> str:
    """Crea un nuevo cliente."""
    try:
        customer: Dict[str, Any] = {}
        for field in ["first_name", "last_name", "email", "phone", "tags", "note", "addresses", "send_email_invite"]:
            val = getattr(params, field)
            if val is not None:
                customer[field] = val
        data = await _request("POST", "customers.json", body={"customer": customer})
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class UpdateCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    customer_id: int           = Field(..., description="ID del cliente a actualizar")
    first_name:  Optional[str] = Field(default=None)
    last_name:   Optional[str] = Field(default=None)
    email:       Optional[str] = Field(default=None)
    phone:       Optional[str] = Field(default=None)
    tags:        Optional[str] = Field(default=None)
    note:        Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_update_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_update_customer(params: UpdateCustomerInput) -> str:
    """Actualiza un cliente existente. Solo se modifican los campos provistos."""
    try:
        customer: Dict[str, Any] = {}
        for field in ["first_name", "last_name", "email", "phone", "tags", "note"]:
            val = getattr(params, field)
            if val is not None:
                customer[field] = val
        data = await _request("PUT", f"customers/{params.customer_id}.json", body={"customer": customer})
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class CustomerOrdersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int           = Field(..., description="ID del cliente")
    limit:       Optional[int] = Field(default=50, ge=1, le=250)
    status:      Optional[str] = Field(default="any")


@mcp.tool(
    name="shopify_get_customer_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_customer_orders(params: CustomerOrdersInput) -> str:
    """Obtiene todos los pedidos de un cliente específico."""
    try:
        p      = {"limit": params.limit, "status": params.status}
        data   = await _request("GET", f"customers/{params.customer_id}/orders.json", params=p)
        orders = data.get("orders", [])
        return _fmt({"count": len(orders), "orders": orders})
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# COLECCIONES (Personalizadas + Inteligentes)
# ═══════════════════════════════════════════════════════════════════════════

class ListCollectionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit:           Optional[int] = Field(default=50, ge=1, le=250)
    since_id:        Optional[int] = Field(default=None)
    collection_type: Optional[str] = Field(default="custom", description="'custom' o 'smart'")


@mcp.tool(
    name="shopify_list_collections",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_collections(params: ListCollectionsInput) -> str:
    """Lista colecciones personalizadas o inteligentes."""
    try:
        endpoint = "custom_collections.json" if params.collection_type == "custom" else "smart_collections.json"
        p: Dict[str, Any] = {"limit": params.limit}
        if params.since_id:
            p["since_id"] = params.since_id
        data = await _request("GET", endpoint, params=p)
        key  = "custom_collections" if params.collection_type == "custom" else "smart_collections"
        collections = data.get(key, [])
        return _fmt({"count": len(collections), "collections": collections})
    except Exception as e:
        return _error(e)


class GetCollectionProductsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_id: int           = Field(..., description="ID de la colección")
    limit:         Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_get_collection_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_collection_products(params: GetCollectionProductsInput) -> str:
    """Obtiene todos los productos de una colección específica."""
    try:
        p        = {"limit": params.limit, "collection_id": params.collection_id}
        data     = await _request("GET", "products.json", params=p)
        products = data.get("products", [])
        return _fmt({"count": len(products), "products": products})
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# INVENTARIO
# ═══════════════════════════════════════════════════════════════════════════

class ListInventoryLocationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_list_locations",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_locations(params: ListInventoryLocationsInput) -> str:
    """Lista todas las ubicaciones de inventario de la tienda."""
    try:
        data      = await _request("GET", "locations.json")
        locations = data.get("locations", [])
        return _fmt({"count": len(locations), "locations": locations})
    except Exception as e:
        return _error(e)


class GetInventoryLevelsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id:         Optional[int] = Field(default=None, description="Filtra por ID de ubicación")
    inventory_item_ids:  Optional[str] = Field(default=None, description="IDs de ítems de inventario separados por coma")


@mcp.tool(
    name="shopify_get_inventory_levels",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_inventory_levels(params: GetInventoryLevelsInput) -> str:
    """Obtiene los niveles de inventario para ubicaciones o ítems de inventario específicos."""
    try:
        p: Dict[str, Any] = {}
        if params.location_id:
            p["location_ids"] = params.location_id
        if params.inventory_item_ids:
            p["inventory_item_ids"] = params.inventory_item_ids
        data   = await _request("GET", "inventory_levels.json", params=p)
        levels = data.get("inventory_levels", [])
        return _fmt({"count": len(levels), "inventory_levels": levels})
    except Exception as e:
        return _error(e)


class SetInventoryLevelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_item_id: int = Field(..., description="ID del ítem de inventario")
    location_id:       int = Field(..., description="ID de la ubicación")
    available:         int = Field(..., description="Cantidad disponible a establecer")


@mcp.tool(
    name="shopify_set_inventory_level",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_set_inventory_level(params: SetInventoryLevelInput) -> str:
    """Establece el inventario disponible de un ítem en una ubicación."""
    try:
        body = {
            "inventory_item_id": params.inventory_item_id,
            "location_id":       params.location_id,
            "available":         params.available,
        }
        data = await _request("POST", "inventory_levels/set.json", body=body)
        return _fmt(data.get("inventory_level", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# CUMPLIMIENTO DE PEDIDOS
# ═══════════════════════════════════════════════════════════════════════════

class ListFulfillmentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int           = Field(..., description="ID del pedido")
    limit:    Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_list_fulfillments",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_fulfillments(params: ListFulfillmentsInput) -> str:
    """Lista los cumplimientos de un pedido específico."""
    try:
        p            = {"limit": params.limit}
        data         = await _request("GET", f"orders/{params.order_id}/fulfillments.json", params=p)
        fulfillments = data.get("fulfillments", [])
        return _fmt({"count": len(fulfillments), "fulfillments": fulfillments})
    except Exception as e:
        return _error(e)


class CreateFulfillmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id:         int                            = Field(..., description="ID del pedido a despachar")
    location_id:      int                            = Field(..., description="ID de la ubicación desde la que se despacha")
    tracking_number:  Optional[str]                  = Field(default=None)
    tracking_company: Optional[str]                  = Field(default=None, description="ej. UPS, FedEx, USPS")
    tracking_url:     Optional[str]                  = Field(default=None)
    line_items:       Optional[List[Dict[str, Any]]] = Field(default=None, description="Ítems específicos (omitir para todos)")
    notify_customer:  Optional[bool]                 = Field(default=True, description="Envía email de notificación de envío")


@mcp.tool(
    name="shopify_create_fulfillment",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_fulfillment(params: CreateFulfillmentInput) -> str:
    """Crea un cumplimiento para un pedido (despacha los ítems)."""
    try:
        fulfillment: Dict[str, Any] = {"location_id": params.location_id}
        for field in ["tracking_number", "tracking_company", "tracking_url", "line_items", "notify_customer"]:
            val = getattr(params, field)
            if val is not None:
                fulfillment[field] = val
        data = await _request(
            "POST",
            f"orders/{params.order_id}/fulfillments.json",
            body={"fulfillment": fulfillment},
        )
        return _fmt(data.get("fulfillment", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# INFO DE LA TIENDA
# ═══════════════════════════════════════════════════════════════════════════

class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_get_shop",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_shop(params: EmptyInput) -> str:
    """Obtiene información de la tienda: nombre, dominio, plan, moneda, huso horario, etc."""
    try:
        data = await _request("GET", "shop.json")
        return _fmt(data.get("shop", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════

class ListWebhooksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: Optional[int] = Field(default=50, ge=1, le=250)
    topic: Optional[str] = Field(default=None, description="Filtra por topic, ej. orders/create")


@mcp.tool(
    name="shopify_list_webhooks",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_webhooks(params: ListWebhooksInput) -> str:
    """Lista los webhooks configurados."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        if params.topic:
            p["topic"] = params.topic
        data     = await _request("GET", "webhooks.json", params=p)
        webhooks = data.get("webhooks", [])
        return _fmt({"count": len(webhooks), "webhooks": webhooks})
    except Exception as e:
        return _error(e)


class CreateWebhookInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    topic:   str           = Field(..., description="Topic del webhook, ej. orders/create, products/update")
    address: str           = Field(..., description="URL HTTPS que recibirá el POST del webhook")
    format:  Optional[str] = Field(default="json", description="json o xml")

    @field_validator("address")
    @classmethod
    def must_be_https_with_hostname(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme != "https":
            raise ValueError("Webhook address must use HTTPS.")
        if not parsed.hostname:
            raise ValueError("Webhook address must have a valid hostname.")
        return v


@mcp.tool(
    name="shopify_create_webhook",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_webhook(params: CreateWebhookInput) -> str:
    """Crea una nueva suscripción de webhook."""
    try:
        webhook = {"topic": params.topic, "address": params.address, "format": params.format}
        data    = await _request("POST", "webhooks.json", body={"webhook": webhook})
        return _fmt(data.get("webhook", data))
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Middleware de autenticación
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    # Estos endpoints deben ser accesibles antes de establecer la autenticación (handshake OAuth de MCP).
    _PUBLIC_PREFIXES = (
        "/.well-known/",  # RFC 8414 — descubrimiento de OAuth
        "/register",      # RFC 7591 — Registro dinámico de clientes
        "/authorize",     # RFC 6749 — Endpoint de autorización
        "/token",         # RFC 6749 — Endpoint de token
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Chequeo de salud — sin autenticación.
        if path == "/health":
            return JSONResponse({"status": "ok"})

        # Descubrimiento de metadatos de OAuth — se deja pasar hacia el SDK de MCP.
        if any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await call_next(request)

        ip = _client_ip(request)

        # Verifica el tamaño antes de autenticar — evita OOM por cuerpos enormes en requests sin autenticar.
        content_length = int(request.headers.get("content-length", 0))
        if content_length > MAX_REQUEST_BODY:
            return JSONResponse({"error": "Request body too large"}, status_code=413)

        # Limita la tasa antes de autenticar para bloquear intentos de fuerza bruta sobre el token.
        if not await _check_rate_limit(ip):
            return JSONResponse(
                {"error": "Too many requests"},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        if BEARER_TOKEN:
            token: str = ""
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:]
            elif ALLOW_TOKEN_QUERY_PARAM:
                token = request.query_params.get("token", "")

            if not token or not secrets.compare_digest(token.encode(), BEARER_TOKEN.encode()):
                logger.warning("Unauthorized attempt from %s %s %s", ip, request.method, path)
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


class _HostRewriteMiddleware:
    """Reescribe el header Host a 'localhost' antes de la verificación de DNS-rebinding del SDK de MCP.
    Railway termina el TLS y valida el hostname real río arriba, así que esto es seguro."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {
                **scope,
                "headers": [
                    (b"host", b"localhost") if k == b"host" else (k, v)
                    for k, v in scope.get("headers", [])
                ],
            }
        await self.app(scope, receive, send)


# Se agrega el middleware directamente a la app de FastMCP para preservar su lifespan
# (el task group del gestor de sesiones).
app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)
# _HostRewriteMiddleware se agrega al final para que se ejecute primero (más externo) —
# debe reescribir el header antes de la autenticación.
app.add_middleware(_HostRewriteMiddleware)

# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    if not SHOPIFY_STORE:
        logger.warning("SHOPIFY_STORE is not set")
    if ALLOW_TOKEN_QUERY_PARAM:
        logger.warning("ALLOW_TOKEN_QUERY_PARAM=1 — token query param is ENABLED. Credentials may appear in proxy access logs.")
    else:
        logger.info("Token query param: disabled")

    logger.info("Bearer auth: %s", "ENABLED" if BEARER_TOKEN else "DISABLED")
    logger.info("Rate limit: %d req/min per IP | Max tracked IPs: %d | Trusted proxies: %d",
                _RATE_LIMIT_RPM, _MAX_TRACKED_IPS, _TRUSTED_PROXIES)
    logger.info("MCP endpoint: http://0.0.0.0:%d/mcp", PORT)

    # Deshabilita el access log de uvicorn cuando ?token= está activo — de lo contrario
    # registraría la URL completa, incluyendo el token.
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=not ALLOW_TOKEN_QUERY_PARAM)
