"""Encabezados de seguridad de toda respuesta, en un solo middleware (spec, tabla de
encabezados; DEC-109, RNF-21).

Los pone la aplicación y no Caddy: así valen también sin el proxy, y Caddy solo quita su
`Server`. Una ruta que fija el suyo lo conserva (la cabecera propia de `/original`, si el visor
de PDF de algún navegador la necesita, DEC-146 punto 21). Sin HSTS: en la PC se entra por HTTP;
en el servidor de la universidad, con HTTPS, lo agrega Caddy (ver `Caddyfile`).
"""

from __future__ import annotations

import logging

from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from historia_viva.api.problemas import Problema, respuesta

__all__ = ["CSP", "MiddlewareCabeceras"]

CSP = (
    "default-src 'self'; img-src 'self'; media-src 'self'; frame-src 'self'; "
    "frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
)
"""Sin `'unsafe-inline'`: las pantallas traen sus scripts y estilos de `/estaticos`."""

_log = logging.getLogger(__name__)

_CABECERAS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # El original fija el suyo (`private, max-age=86400`); lo demás no se guarda.
    "Cache-Control": "no-store",
}


class MiddlewareCabeceras:
    """Agrega los encabezados de seguridad y `Cache-Control: no-store` a toda respuesta que no
    trae los suyos (spec, `Cache-Control`).

    Es ASGI puro: no acumula el cuerpo, así el original con `Range` y el flujo de la
    Conversación siguen en streaming. Un error no manejado sale como `error-interno` (RFC
    9457, sin traza) y con estos encabezados."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        empezada = False

        async def enviar(mensaje: Message) -> None:
            nonlocal empezada
            if mensaje["type"] == "http.response.start":
                empezada = True
                cabeceras = MutableHeaders(scope=mensaje)
                for nombre, valor in _CABECERAS.items():
                    cabeceras.setdefault(nombre, valor)
            await send(mensaje)

        try:
            await self._app(scope, receive, enviar)
        except ClientDisconnect:
            # Quien sube cerró la conexión (canceló o se cortó): no hay a quién responder y no
            # es una falla del servidor; lo que se estaba guardando ya se descartó.
            _log.info("El cliente cortó el pedido %s %s.", scope["method"], scope["path"])
        except Exception:
            # El `ServerErrorMiddleware` de Starlette va por fuera de todo middleware: si el
            # 500 lo escribiera él, saldría sin estos encabezados. Se relanza para el log.
            if not empezada:
                error = Problema(
                    tipo="/errores/error-interno",
                    titulo="Error inesperado",
                    estado=500,
                    detalle="Algo falló de nuestro lado. Probá de nuevo en un rato.",
                )
                await respuesta(error)(scope, receive, enviar)
            raise
