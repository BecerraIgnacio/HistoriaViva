"""La sesión en el borde HTTP: quién pide y qué puede hacer (spec, `identidad` y `api`; DEC-37).

La cookie lleva el id de una sesión de servidor (`identidad.Sesiones`); el rol se lee de la base
en cada pedido (DEC-124). Bajo `HV_AUTH=dev` la cookie es `hv_sesion`, sin prefijo ni `Secure`
porque el acceso de desarrollo es HTTP en la PC; en OIDC, `__Host-hv_sesion` (DEC-146 punto 17).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from historia_viva.api.problemas import Problema, respuesta
from historia_viva.bd import Bd
from historia_viva.dominio.permisos import Permiso
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.identidad import INACTIVIDAD, PersonaNoHabilitada, Principal, Sesiones
from historia_viva.identidad.oidc import VIDA_DEL_INTENTO, IngresoOIDC, RechazoDeIngreso
from historia_viva.interruptor import Freno, habilitado_en

__all__ = [
    "COOKIE_DE_SESION",
    "MiddlewareCSRF",
    "exigir",
    "nombre_de_persona",
    "principal_actual",
    "router",
    "router_auth",
    "router_dev",
    "router_oidc",
]

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Sesión"])
router_auth = APIRouter(prefix="/auth", tags=["Sesión"])

COOKIE_DE_SESION = {
    MetodoAutenticacion.DEV: "hv_sesion",
    MetodoAutenticacion.OIDC: "__Host-hv_sesion",
}


def _origen_no_permitido(detalle: str) -> Problema:
    return Problema(
        tipo="/errores/origen-no-permitido",
        titulo="Origen no permitido",
        estado=403,
        detalle=detalle,
    )


class MiddlewareCSRF:
    """Toda mutación trae el `Origin` de la aplicación (`HV_ORIGEN`) y `X-HV: 1`, que un
    formulario de otro sitio no puede poner; si no, 403 `origen-no-permitido` (DEC-37). Mira
    solo `/api` y `/auth`: `/v1` va con Bearer y no lleva cookie. `GET`, `HEAD` y `OPTIONS` no
    mutan y no se miran. Sin CORS: nadie de afuera puede leer la respuesta.

    Es ASGI y no una dependencia porque FastAPI lee el cuerpo antes que las dependencias: así
    se mira antes que la sesión y que el cuerpo, que no toca (la subida sigue en streaming)."""

    def __init__(self, app: ASGIApp, origen: str) -> None:
        self._app = app
        self._origen = origen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in ("GET", "HEAD", "OPTIONS")
            and scope["path"].startswith(("/api/", "/auth/"))
        ):
            cabeceras = Headers(scope=scope)
            if cabeceras.get("origin") != self._origen or cabeceras.get("x-hv") != "1":
                # Rastro del intento (SEG-07, 165), sin el `Origin`: lo escribe quien ataca.
                _log.warning("CSRF rechazado: %s %s", scope["method"], scope["path"])
                rechazo = _origen_no_permitido(
                    "El pedido no viene de la aplicación. Recargá la página y probá de nuevo."
                )
                await respuesta(rechazo)(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _cookie(request: Request) -> dict[str, Any]:
    modo: MetodoAutenticacion = request.app.state.modo_de_ingreso
    return {
        "key": COOKIE_DE_SESION[modo],
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": modo is MetodoAutenticacion.OIDC,
    }


def principal_actual(request: Request) -> Principal:
    """La persona de la sesión de la cookie; sin sesión activa, 401 `sin-sesion`."""
    sesiones: Sesiones = request.app.state.sesiones
    sesion = request.cookies.get(COOKIE_DE_SESION[request.app.state.modo_de_ingreso])
    principal = sesiones.validar(sesion) if sesion else None
    if principal is None:
        raise Problema(
            tipo="/errores/sin-sesion",
            titulo="No hay persona en la sesión",
            estado=401,
            detalle="Entrá con tu email antes de trabajar sobre una Ficha.",
        )
    return principal


ConSesion = Annotated[Principal, Depends(principal_actual)]


def exigir(permiso: Permiso) -> Callable[[Request, Principal], Principal]:
    """Dependencia que exige `permiso` a la persona de la sesión (DEC-124), con el interinato
    del Administrador técnico: sin sesión, 401 `sin-sesion`; sin el permiso, 403
    `permiso-insuficiente`, que queda en `auditoria` como `sesion.rechazo`. `permiso` queda en
    la dependencia para que `test_seg_permisos_rutas` lea qué exige cada ruta."""

    def con_permiso(request: Request, principal: ConSesion) -> Principal:
        if not principal.puede(permiso):
            sesiones: Sesiones = request.app.state.sesiones
            sesiones.registrar_rechazo(
                principal, permiso=permiso, ruta=f"{request.method} {request.url.path}"
            )
            # El catálogo de errores no dice qué permiso falta.
            raise Problema(
                tipo="/errores/permiso-insuficiente",
                titulo="No tenés permiso para esto",
                estado=403,
                detalle="Tu rol no permite esta acción.",
            )
        return principal

    con_permiso.permiso = permiso  # type: ignore[attr-defined]
    return con_permiso


def nombre_de_persona(bd: Bd, persona_id: UUID | None) -> str | None:
    """El nombre con que se muestra a una persona; `None` si no hay persona."""
    if persona_id is None:
        return None
    with bd.transaccion() as conexion:
        fila = conexion.execute(
            "SELECT nombre FROM acervo.usuario WHERE usuario_id = %s", (persona_id,)
        ).fetchone()
    return None if fila is None else str(fila["nombre"])


@router.get("/yo")
def yo(principal: ConSesion, request: Request) -> dict[str, Any]:
    """Quién entró, con su rol y sus permisos: la pantalla oculta lo que el rol no puede, pero
    lo decide `dominio`, no la pantalla. Con el interinato vigente, el Administrador técnico
    trae además GESTIONAR_PERSONAS y RETIRAR_PIEZAS (R-01). `sistema` alimenta la franja
    «Procesamiento detenido» (DEC-88) y la sección de la aprobación en bloque (R-28).
    """
    return {
        "usuario_id": str(principal.usuario_id),
        "nombre": principal.nombre,
        "email": principal.email,
        "rol": principal.rol.value,
        "permisos": [permiso for permiso in Permiso if principal.puede(permiso)],
        "modo_auth": request.app.state.modo_de_ingreso.value,
        "sesion_vence_el": principal.vence_el,
        "inactividad_min": INACTIVIDAD // timedelta(minutes=1),
        "sistema": {
            "procesamiento_habilitado": habilitado_en(request.app.state.bd, Freno.PROCESAMIENTO),
            # Con falso la pantalla no muestra la aprobación en bloque (R-28).
            "bloque_habilitado": request.app.state.validacion.bloque_habilitado,
        },
    }


@router_auth.post("/salir", status_code=204)
def salir(principal: ConSesion, request: Request, response: Response) -> None:
    """Revoca la sesión (la marca, no la borra) y borra la cookie."""
    cookie = _cookie(request)
    sesiones: Sesiones = request.app.state.sesiones
    sesiones.cerrar(request.cookies[cookie["key"]], por=principal.actor)
    response.delete_cookie(**cookie)


def _es_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # un nombre o nada, no una dirección
        return False


def _direcciones(host: str | None) -> set[str]:
    """Las de `host`, resueltas en cada pedido: Caddy puede cambiar de IP al reiniciarse."""
    if host is None:
        return set()
    try:
        return {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except OSError:  # Caddy todavía no está
        return set()


def _solo_en_la_pc(request: Request) -> None:
    """`/auth/dev` atiende solo pedidos hechos en la PC (DEC-85). Se mira el socket, nunca el
    `Host` ni `X-Forwarded-For`, que los pone el cliente (`web` corre sin `proxy_headers`): vale
    uno loopback (uvicorn en la PC) o el de Caddy (`HV_PROXY`), que el compose publica solo en
    `127.0.0.1:80` (`test_compose_sin_puertos_publicados`). Con `compose.sesion-presencial.yml`
    arriba, ese socket también trae tráfico de la LAN (el 8000): la invariante depende de que el
    8000 no enrute `/auth` (`test_desde_la_lan_caddy_enruta_solo_v1`).

    Riesgo residual aceptado en la PC del alumno: cualquier contenedor de la red del compose que
    llegue a `caddy:80` cuenta como la PC."""
    host = request.client.host if request.client else ""
    if not (_es_loopback(host) or host in _direcciones(request.app.state.proxy)):
        raise _origen_no_permitido(
            "El acceso de desarrollo atiende solo pedidos hechos en esta PC."
        )


router_dev = APIRouter(
    prefix="/auth", tags=["Sesión"], dependencies=[Depends(_solo_en_la_pc)]
)
"""Existe solo con `HV_AUTH=dev`, que exige `HV_ENTORNO=desarrollo` (`composicion`)."""


class PedidoIngreso(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


@router_dev.post("/dev", status_code=204)
def ingresar_dev(pedido: PedidoIngreso, request: Request, response: Response) -> None:
    """El ingreso de desarrollo: solo el email, sin selector de personas (DEC-112). Lo firmado
    con esta sesión es una firma de ensayo (DEC-139)."""
    cookie = _cookie(request)
    sesiones: Sesiones = request.app.state.sesiones
    try:
        sesion = sesiones.ingresar_dev(pedido.email, previa=request.cookies.get(cookie["key"]))
    except PersonaNoHabilitada as error:
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="No hay una persona habilitada con ese email.",
            campo="email",
        ) from error
    response.set_cookie(value=sesion, **cookie)


router_oidc = APIRouter(prefix="/auth", tags=["Sesión"])
"""Existe solo con el cliente OIDC configurado (`HV_OIDC_CLIENTE_ID`), que `dev` no admite."""

_INTENTO: dict[str, Any] = {
    "key": "__Host-hv_intento", "path": "/", "httponly": True, "samesite": "lax", "secure": True
}
"""La cookie previa al login: `state`, `nonce` y verificador firmados. `Lax` alcanza: la vuelta
del proveedor es una navegación de primer nivel."""


@router_oidc.get("/google")
def ingresar_con_google(request: Request, siguiente: str | None = None) -> RedirectResponse:
    """La ida al proveedor (CU-15). `siguiente` vale solo si es de la lista cerrada."""
    oidc: IngresoOIDC = request.app.state.oidc
    try:
        url, intento = oidc.iniciar(siguiente)
    except RechazoDeIngreso as rechazo:
        return RedirectResponse(f"/?error={rechazo.codigo}", status_code=302)
    respuesta = RedirectResponse(url, status_code=302)
    respuesta.set_cookie(value=intento, max_age=VIDA_DEL_INTENTO, **_INTENTO)
    return respuesta


@router_oidc.get("/callback")
def volver_de_google(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """La vuelta: sesión nueva o `/?error=<código cerrado>`, sin cuerpo ni RFC 9457 (§3.3). La
    cookie del intento se borra siempre."""
    oidc: IngresoOIDC = request.app.state.oidc
    cookie = _cookie(request)
    try:
        sesion, siguiente = oidc.completar(
            codigo=code,
            estado=state,
            error=error,
            intento=request.cookies.get(_INTENTO["key"]),
            previa=request.cookies.get(cookie["key"]),
        )
    except RechazoDeIngreso as rechazo:
        respuesta = RedirectResponse(f"/?error={rechazo.codigo}", status_code=302)
    else:
        respuesta = RedirectResponse(siguiente, status_code=302)
        respuesta.set_cookie(value=sesion, **cookie)
    respuesta.delete_cookie(**_INTENTO)
    return respuesta
