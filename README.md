# Servidor MCP de Shopify

Un servidor [Model Context Protocol (MCP)](https://modelcontextprotocol.io) que conecta a Claude directamente con tu tienda Shopify. Gestioná productos, pedidos, clientes, colecciones, inventario y cumplimientos — todo mediante lenguaje natural.

---

## Qué podés hacer con esto

Una vez conectado, podés hablarle a tu tienda Shopify así:

- *"Mostrame todos los pedidos sin cumplir de hoy"*
- *"Creá un producto nuevo llamado Remera de Verano, precio $29.99, como borrador"*
- *"¿Cuántos productos activos tenemos?"*
- *"Buscá clientes con el email juan@ejemplo.com"*
- *"Actualizá el inventario del producto 123 a 50 unidades"*

---

## Requisitos

- Python 3.11 o superior
- Una tienda Shopify (cualquier plan)
- Una cuenta de Claude.ai Pro, Team o Enterprise (para conexiones MCP remotas)

---

## Paso 1 — Conseguí tus credenciales de Shopify

Necesitás dos cosas: el **nombre de tu tienda** y un **access token de la Admin API**.

### Encontrá el nombre de tu tienda

El nombre de tu tienda es la parte antes de `.myshopify.com`.
Ejemplo: si tu URL de admin es `https://mi-tienda.myshopify.com/admin`, el nombre de tu tienda es `mi-tienda`.

### Creá una Custom App y conseguí tu access token

> ⚠️ **Importante:** Una API key normal de Shopify NO va a funcionar. Necesitás un **access token de la Admin API** generado desde una Custom App. Seguí estos pasos exactamente.

1. Andá a **Shopify Admin** → **Configuración** → **Apps y canales de venta**
2. Hacé clic en **Desarrollar apps**, arriba a la derecha
3. Si te lo pide, hacé clic en **Permitir el desarrollo de apps personalizadas**
4. Hacé clic en **Crear una app**, ponele un nombre (por ej. `MCP Server`), hacé clic en **Crear app**
5. Andá a la pestaña **Configuración** → hacé clic en **Configurar alcances de la Admin API**
6. Habilitá los alcances que necesites. Para acceso completo, seleccioná:
   - `read_products`, `write_products`
   - `read_orders`, `write_orders`
   - `read_customers`, `write_customers`
   - `read_inventory`, `write_inventory`
   - `read_fulfillments`, `write_fulfillments`
   - `read_webhooks`, `write_webhooks`
7. Hacé clic en **Guardar**
8. Andá a la pestaña **Credenciales de API** → hacé clic en **Instalar app** → confirmá
9. Hacé clic en **Revelar token una vez** y copiá el token de inmediato — empieza con `shpat_`

> 💡 Shopify solo muestra este token una vez. Si lo perdés, volvé a Credenciales de API, desinstalá la app y volvé a instalarla para generar uno nuevo.

---

## Paso 2 — Configurar el servidor localmente

### Cloná este repo

```bash
git clone https://github.com/daanjonk/shopify-mcp.git
cd shopify-mcp
```

### Instalá las dependencias

```bash
pip install -r requirements.txt
```

### Configurá tus variables de entorno

```bash
cp env.example .env
```

Abrí `.env` y completá tus valores:

```env
SHOPIFY_STORE=nombre-de-tu-tienda
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxx
BEARER_TOKEN=elegi-una-cadena-aleatoria-larga
```

> `SHOPIFY_STORE`, `SHOPIFY_ACCESS_TOKEN` y `BEARER_TOKEN` son obligatorios. Dejá todo lo demás como está.

### Iniciá el servidor

```bash
python server.py
```

Deberías ver algo como:

```
INFO  Token mode: static SHOPIFY_ACCESS_TOKEN (no auto-refresh)
INFO  Bearer auth: ENABLED
INFO  Uvicorn running on http://0.0.0.0:8000
```

Tu servidor MCP está corriendo en `http://localhost:8000/mcp`.

---

## Paso 3 — Desplegar en la nube

Para usar este servidor con Claude.ai, necesita una URL pública. La opción más fácil es **Railway** — el plan gratuito alcanza para empezar.

### Desplegar en Railway

1. Forkeá este repo de GitHub a tu propia cuenta
2. Andá a [railway.app](https://railway.app) e iniciá sesión con GitHub
3. Hacé clic en **New Project** → **Deploy from GitHub repo**
4. Seleccioná tu repo `shopify-mcp` forkeado
5. Railway detecta el `Dockerfile` y empieza a compilar automáticamente
6. Cuando termine el build, andá a tu servicio → **Settings** → **Networking** → **Generate Domain**
7. Copiá tu URL pública — se ve algo así como `https://shopify-mcp-production.up.railway.app`

### Agregá tus variables de entorno en Railway

En tu proyecto de Railway, andá a **Variables** y agregá lo siguiente:

| Variable | Requerida | Valor |
|---|---|---|
| `SHOPIFY_STORE` | ✅ | `nombre-de-tu-tienda` |
| `SHOPIFY_ACCESS_TOKEN` | ✅ | `shpat_xxxxxxxxxxxxxxxxxxxx` |
| `BEARER_TOKEN` | ✅ | Una cadena aleatoria larga — la vas a ingresar en Claude también |
| `PORT` | No | `8000` |
| `MCP_TRANSPORT` | No | `streamable-http` |
| `ALLOW_TOKEN_QUERY_PARAM` | No | `1` — solo si Claude.ai no puede enviar headers de Authorization |

Railway reinicia tu servidor automáticamente después de guardar.

---

## Paso 4 — Conectar con Claude

### Tu URL de endpoint MCP

Combiná tu URL de Railway con `/mcp`:

```
https://tu-app.up.railway.app/mcp
```

### Agregá el servidor en Claude.ai

> ⚠️ **Token de autenticación:** al agregar un servidor MCP remoto en Claude.ai, te va a pedir un token de autenticación. Este es un token de seguridad que protege el endpoint de tu servidor — es **distinto** de tu access token de Shopify.

**Para conectar:**

1. Andá a [claude.ai](https://claude.ai) → hacé clic en tu ícono de perfil (abajo a la izquierda) → **Settings**
2. Navegá a **Integrations**
3. Hacé clic en **Add integration**
4. Completá:
   - **Name:** `Shopify`
   - **URL:** `https://tu-app.up.railway.app/mcp`
5. En el campo de **authentication token**: pegá el valor de tu variable `BEARER_TOKEN` de Railway

---

## Herramientas disponibles

| Herramienta | Descripción |
|---|---|
| `shopify_list_products` | Lista productos con filtros opcionales |
| `shopify_get_product` | Obtiene un producto por su ID |
| `shopify_create_product` | Crea un nuevo producto |
| `shopify_update_product` | Actualiza un producto existente |
| `shopify_delete_product` | Elimina un producto de forma permanente |
| `shopify_count_products` | Cuenta productos (con filtros) |
| `shopify_list_orders` | Lista pedidos con filtros |
| `shopify_get_order` | Obtiene un pedido por su ID |
| `shopify_count_orders` | Cuenta pedidos |
| `shopify_close_order` | Cierra un pedido |
| `shopify_cancel_order` | Cancela un pedido |
| `shopify_list_customers` | Lista clientes |
| `shopify_search_customers` | Busca clientes por nombre/email |
| `shopify_get_customer` | Obtiene un cliente por su ID |
| `shopify_create_customer` | Crea un nuevo cliente |
| `shopify_update_customer` | Actualiza un cliente existente |
| `shopify_get_customer_orders` | Obtiene todos los pedidos de un cliente |
| `shopify_list_collections` | Lista colecciones personalizadas o inteligentes |
| `shopify_get_collection_products` | Obtiene los productos de una colección |
| `shopify_list_locations` | Lista las ubicaciones de inventario |
| `shopify_get_inventory_levels` | Obtiene los niveles actuales de inventario |
| `shopify_set_inventory_level` | Establece la cantidad de inventario en una ubicación |
| `shopify_list_fulfillments` | Lista los cumplimientos de un pedido |
| `shopify_create_fulfillment` | Cumple (despacha) un pedido |
| `shopify_get_shop` | Obtiene info de la tienda (nombre, moneda, plan, etc.) |
| `shopify_list_webhooks` | Lista los webhooks configurados |
| `shopify_create_webhook` | Crea un nuevo webhook |

---

## Referencia de variables de entorno

| Variable | Requerida | Valor por defecto | Descripción |
|---|---|---|---|
| `SHOPIFY_STORE` | ✅ | — | Nombre de la tienda, ej. `mi-tienda` (no la URL completa) |
| `SHOPIFY_ACCESS_TOKEN` | ✅* | — | Token de la Admin API de una Custom App (`shpat_...`) |
| `BEARER_TOKEN` | ✅ | — | Protege tu endpoint MCP — configurá el mismo valor en Claude |
| `SHOPIFY_CLIENT_ID` | No | — | Client ID de OAuth (avanzado, reemplaza al token estático) |
| `SHOPIFY_CLIENT_SECRET` | No | — | Client secret de OAuth (avanzado) |
| `SHOPIFY_API_VERSION` | No | `2025-01` | Versión de la Admin API de Shopify |
| `PORT` | No | `8000` | Puerto en el que escucha el servidor |
| `MCP_TRANSPORT` | No | `streamable-http` | Protocolo de transporte |
| `ALLOW_TOKEN_QUERY_PARAM` | No | — | Poner en `1` solo si tu cliente MCP no puede enviar headers de Authorization |
| `MAX_REQUEST_BODY` | No | `1048576` | Tamaño máximo del cuerpo de la request entrante, en bytes (1 MB) |
| `RATE_LIMIT_RPM` | No | `60` | Máximo de requests por minuto por IP |
| `RATE_LIMIT_MAX_IPS` | No | `10000` | Máximo de IPs rastreadas por el limitador de tasa |
| `TRUSTED_PROXY_COUNT` | No | `1` | Cantidad de proxies inversos delante del servidor |
| `TOKEN_REFRESH_BUFFER` | No | `1800` | Segundos antes del vencimiento del token para disparar una renovación (modo OAuth) |

*Se requiere `SHOPIFY_ACCESS_TOKEN` **o** `SHOPIFY_CLIENT_ID` + `SHOPIFY_CLIENT_SECRET`.

---

## Resolución de problemas

**"Authentication failed" (401)**
Tu `SHOPIFY_ACCESS_TOKEN` está mal o expiró. Asegurate de que empiece con `shpat_` y de que la Custom App esté instalada en tu tienda.

**"Permission denied" (403)**
A tu token le faltan los alcances de API necesarios. Volvé a tu Custom App → Configuración → agregá los alcances faltantes → Guardar → reinstalá la app (esto genera un token nuevo).

**"Missing SHOPIFY_STORE environment variable"**
Verificá que `SHOPIFY_STORE` esté configurado solo con el nombre de la tienda — no la URL completa.
✅ `mi-tienda` &nbsp; ❌ `mi-tienda.myshopify.com` &nbsp; ❌ `https://mi-tienda.myshopify.com`

**Claude no puede conectarse al servidor**
Asegurate de que tu despliegue en Railway esté activo y de que se haya generado un dominio. Probá abriendo `https://tu-app.up.railway.app/mcp` en el navegador — deberías obtener una respuesta, no un 404.

**Perdí mi access token de Shopify**
Shopify solo lo muestra una vez. Andá a Shopify Admin → Configuración → Apps → tu app → Credenciales de API → Desinstalar app → Instalar app de nuevo → Revelar token una vez.

---

## Licencia

MIT
