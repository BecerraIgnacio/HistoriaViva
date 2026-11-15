"""Construcción de la aplicación web.

`crear_app` recibe el `Grafo` que arma `composicion` (`componer_web`): la API no construye
nada, solo lo sirve, y nada se arma al importar (historia 64). La capa de modelos se pide con
la configuración vigente (`capa_vigente`): es el único seam de la spec, y la suite arma su
grafo con una capa falsa.

`web` no procesa: la cola la consume el trabajador, un proceso aparte (historia 50), que se
entera por sondeo (DEC-106).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from historia_viva import api_agentes
from historia_viva.agentes import Credenciales
from historia_viva.api import (
    agentes,
    ayuda,
    auditoria,
    busqueda,
    cabeceras,
    catalogos,
    conversaciones,
    efemerides,
    ficha,
    informes,
    interruptor,
    lotes,
    mediciones,
    operacion,
    modelos,
    personas,
    piezas,
    retiro,
    sesion,
)
from historia_viva.api.problemas import registrar_manejadores
from historia_viva.auditoria import RegistroDeAuditoria
from historia_viva.bd import BaseNoDisponible, Bd
from historia_viva.busqueda import Busqueda
from historia_viva.capa_modelos.configuracion import CapaPara
from historia_viva.capa_modelos.contrato import CapaModelos
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.conversacion import Conversaciones
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.efemerides import Efemerides
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Padron, Sesiones
from historia_viva.identidad.oidc import IngresoOIDC
from historia_viva.indexacion import Indexacion
from historia_viva.informes import Informes
from historia_viva.ingesta import Ingesta
from historia_viva.instrumentacion import RegistroMediciones
from historia_viva.lotes import Lotes
from historia_viva.originales import AlmacenDeOriginales
from historia_viva.procesamiento.cola import Cola
from historia_viva.retiro import Retiro
from historia_viva.validacion import Validacion

__all__ = ["Grafo", "crear_app"]

DIRECTORIO_WEB = Path(__file__).resolve().parent.parent / "web"

_HUERFANO_DESDE = timedelta(hours=1)
"""Antigüedad del `mtime` desde la que se borra un temporal o un original cortado antes del
`COMMIT` (spec, Respaldos). Un original sin Pieza que no viene de un corte no se toca."""


@dataclass(frozen=True)
class Grafo:
    """Lo que `composicion` construye y la API sirve."""

    bd: Bd
    originales: AlmacenDeOriginales
    fichas: ModuloFichasPostgres
    mediciones: RegistroMediciones
    validacion: Validacion
    ingesta: Ingesta
    cola: Cola
    lotes: Lotes
    capa_vigente: Callable[[], CapaModelos]
    """La capa con la configuración vigente; valida las filas (`ConfiguracionInvalida`)."""
    configuracion: ConfiguracionDeModelos
    """Las filas de los modelos: la pestaña Modelos las lee y las edita (110)."""
    capa_para: CapaPara
    """La capa con unas filas cualesquiera: la pestaña Modelos le pregunta si el modelo está
    descargado, también el de una fila sin guardar."""
    busqueda: Busqueda
    conversaciones: Conversaciones
    efemerides: Efemerides
    informes: Informes
    indexacion: Indexacion
    """El progreso del índice para la salud (DEC-146 punto 6)."""
    retiro: Retiro
    sesiones: Sesiones
    auditoria: RegistroDeAuditoria
    credenciales: Credenciales
    """Los tokens de los Agentes externos: la pantalla de tokens los emite y los revoca (112) y
    `/v1` los autentica en cada pedido (097). Una sola instancia para los dos."""
    padron: Padron
    """Altas, cambios de rol y bajas: la pestaña Personas (111) y el comando `alta-persona`."""
    modo_de_ingreso: MetodoAutenticacion
    """`HV_AUTH`: decide la cookie y si existe `/auth/dev`."""
    origen: str
    """`HV_ORIGEN`: el `Origin` que tiene que traer toda mutación (CSRF, DEC-37)."""
    proxy: str | None
    """`HV_PROXY`: el host de Caddy, cuyo socket cuenta como la PC para `/auth/dev`."""
    url_publica: str
    """`HV_URL_PUBLICA`: base de `url_original` en `/v1`."""
    retry_after_s: int
    """`HV_RETRY_AFTER_S`: el `Retry-After` del 503 de `/v1` por el freno."""
    cupos: api_agentes.Cupos
    """El limitador de `/v1`, en la memoria de este proceso (`HV_CUPO_*`, 098)."""
    cupos_de_busqueda: api_agentes.Cupos
    """`/api/busqueda` con texto embebe la consulta: un cupo por minuto por persona
    (`HV_CUPO_BUSQUEDAS_MIN`, SEG-09, 165), en la memoria de este proceso como el de `/v1`."""
    tipos_esperados: tuple[str, ...] = ()
    """Los tipos de trabajo que tienen que tener un trabajador vivo; sin latido, la salud los
    da por caídos (035)."""
    oidc: IngresoOIDC | None = None
    """El ingreso con Google; sin `HV_OIDC_CLIENTE_ID`, no hay `/auth/google`."""
    eleccion_del_banco: Callable[[str], tuple[str | None, str | None] | None] = lambda _: None
    """El ganador y el mejor no chino de la última corrida del banco para una capacidad, o
    `None` sin corrida (DEC-132): la nota de la pestaña Modelos."""


def crear_app(grafo: Grafo) -> FastAPI:
    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
        # Una fila inválida de `configuracion_capacidad` corta el arranque con
        # `ConfiguracionInvalida` (DEC-87). Tras un corte puede quedar un original sin Pieza
        # (historia 231, DEC-106). Con la base caída no se valida ni se barre: la aplicación
        # arranca igual y responde 503.
        with suppress(BaseNoDisponible):
            grafo.capa_vigente()
            grafo.originales.barrer_huerfanos(
                grafo.fichas.originales_registrados, mas_viejos_que=_HUERFANO_DESDE
            )
        try:
            yield
        finally:
            grafo.bd.cerrar()

    app = FastAPI(
        title="Historia Viva — API del prototipo",
        version="1.0.0",
        lifespan=ciclo_de_vida,
        # Sin `/openapi.json`, `/docs` ni `/redoc` públicos: el contrato de `/v1` es el archivo
        # congelado y nadie lee el de `/api` (ASVS 13.4.5). `app.openapi()` sigue andando.
        openapi_url=None,
    )
    # Antes que la sesión y que el cuerpo: una mutación de otro sitio no llega a nada.
    app.add_middleware(sesion.MiddlewareCSRF, origen=grafo.origen)
    app.state.bd = grafo.bd
    app.state.capa_vigente = grafo.capa_vigente
    app.state.configuracion = grafo.configuracion
    app.state.capa_para = grafo.capa_para
    app.state.eleccion_del_banco = grafo.eleccion_del_banco
    app.state.fichas = grafo.fichas
    app.state.originales = grafo.originales
    app.state.mediciones = grafo.mediciones
    app.state.validacion = grafo.validacion
    app.state.ingesta = grafo.ingesta
    app.state.cola = grafo.cola
    app.state.lotes = grafo.lotes
    app.state.busqueda = grafo.busqueda
    app.state.cupos_de_busqueda = grafo.cupos_de_busqueda
    app.state.salud_sin_detalle = (float("-inf"), "")
    app.state.conversaciones = grafo.conversaciones
    app.state.efemerides = grafo.efemerides
    app.state.informes = grafo.informes
    app.state.indexacion = grafo.indexacion
    app.state.retiro = grafo.retiro
    app.state.sesiones = grafo.sesiones
    app.state.auditoria = grafo.auditoria
    app.state.credenciales = grafo.credenciales
    app.state.padron = grafo.padron
    app.state.modo_de_ingreso = grafo.modo_de_ingreso
    app.state.proxy = grafo.proxy
    app.state.tipos_esperados = grafo.tipos_esperados
    app.state.oidc = grafo.oidc
    app.state.url_publica = grafo.url_publica
    registrar_manejadores(app)
    # Después del CSRF: queda por fuera y ve toda respuesta de `/v1`.
    api_agentes.registrar_manejadores(
        app, retry_after_s=grafo.retry_after_s, cupos=grafo.cupos
    )
    # El último queda por fuera de todo: el 403 del CSRF y el 503 de `/v1` llevan la CSP y
    # `no-store`, y su 500 no le gana al 503 de una falla no prevista en `/v1` (039).
    app.add_middleware(cabeceras.MiddlewareCabeceras)
    app.include_router(sesion.router)
    app.include_router(sesion.router_auth)
    app.include_router(catalogos.router)
    app.include_router(piezas.router)
    app.include_router(lotes.router)
    app.include_router(ficha.router)
    app.include_router(mediciones.router)
    app.include_router(operacion.router)
    app.include_router(auditoria.router)
    app.include_router(interruptor.router)
    app.include_router(busqueda.router)
    app.include_router(conversaciones.router)
    app.include_router(efemerides.router)
    app.include_router(informes.router)
    app.include_router(agentes.router)
    app.include_router(modelos.router)
    app.include_router(api_agentes.router)
    app.include_router(personas.router)
    app.include_router(retiro.router)
    app.include_router(ayuda.router)
    if grafo.modo_de_ingreso is MetodoAutenticacion.DEV:
        app.include_router(sesion.router_dev)
    # La Entrada muestra el botón de Google o el campo de email según esto (036).
    ingreso = "dev" if grafo.modo_de_ingreso is MetodoAutenticacion.DEV else ""
    if grafo.oidc is not None:
        app.include_router(sesion.router_oidc)
        ingreso = "google"
    entrada = (DIRECTORIO_WEB / "index.html").read_text(encoding="utf-8").replace(
        '<meta name="hv-ingreso" content="">', f'<meta name="hv-ingreso" content="{ingreso}">'
    )

    @app.get("/", include_in_schema=False)
    def pantalla_de_entrada() -> HTMLResponse:
        return HTMLResponse(entrada)

    @app.get("/validar.html", include_in_schema=False)
    def pantalla_de_validacion() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "validar.html")

    @app.get("/admin.html", include_in_schema=False)
    def pantalla_de_administracion() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "admin.html")

    @app.get("/conversacion.html", include_in_schema=False)
    def pantalla_de_conversacion() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "conversacion.html")

    @app.get("/lote.html", include_in_schema=False)
    def pantalla_del_lote() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "lote.html")
    @app.get("/informes.html", include_in_schema=False)
    def pantalla_de_informes() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "informes.html")

    @app.get("/lotes.html", include_in_schema=False)
    def pantalla_de_lotes() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "lotes.html")

    @app.get("/efemerides.html", include_in_schema=False)
    def pantalla_de_efemerides() -> FileResponse:
        return FileResponse(DIRECTORIO_WEB / "efemerides.html")

    # Los scripts y estilos de las pantallas: la CSP no admite nada en línea (DEC-109).
    app.mount("/estaticos", StaticFiles(directory=DIRECTORIO_WEB / "estaticos"), name="estaticos")
    return app
