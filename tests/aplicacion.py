"""La aplicación de la suite: el grafo de `web` sobre una capa cualquiera (historia 64).

En el compose `web` y el trabajador son procesos aparte (historia 50). La suite los corre en
uno solo, sobre la misma base: el trabajador vive en el bucle del `TestClient` e `ingesta` lo
despierta al encolar, así un test no espera la vuelta del sondeo.

Las apps que la suite arranca sobre la base de su proceso comparten un pool (`pool_compartido`):
en Windows cada conexión es un proceso nuevo de Postgres, que los crea de a uno, unos 6 por
segundo, y abrir uno por test era el cuello de botella de la suite (ticket 130).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from historia_viva.api.app import crear_app
from historia_viva.api.sesion import COOKIE_DE_SESION
from historia_viva.bd import Bd
from historia_viva.composicion import CapaPara, Validar, armar_grafo, armar_trabajador
from historia_viva.dominio.originales import Contenedor
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.identidad.oidc import ConfiguracionOIDC
from historia_viva.ingesta import AnalizarVideo, DatosTecnicos
from historia_viva.originales import Conversor

TAMANO_POOL = 10

ORIGEN = "http://testserver"
"""El `base_url` del `TestClient`: el `HV_ORIGEN` de las apps de la suite."""
CABECERAS = {"Origin": ORIGEN, "X-HV": "1"}
"""Lo que manda la pantalla en toda mutación (CSRF, DEC-37)."""

_compartidas: dict[str, Bd] = {}


class _BdCompartida(Bd):
    def cerrar(self) -> None:
        """La app la cierra al apagarse; el pool sigue para la próxima, hasta que termina
        `pool_compartido`."""


@contextmanager
def pool_compartido(url_bd: str) -> Iterator[None]:
    """Mientras dura, toda app de la suite sobre `url_bd` usa el mismo pool."""
    bd = _compartidas[url_bd] = _BdCompartida(url_bd, tamano_pool=TAMANO_POOL)
    try:
        yield
    finally:
        del _compartidas[url_bd]
        Bd.cerrar(bd)


def _bd(url_bd: str) -> Bd:
    return _compartidas.get(url_bd) or Bd(url_bd, tamano_pool=TAMANO_POOL)


def sin_videos(ruta: Path, contenedor: Contenedor) -> DatosTecnicos:
    """El `analizar_video` de las apps de la suite: hay `ffprobe` para la salud, y un test que
    sube un video inyecta el suyo."""
    raise AssertionError("este test no esperaba subir un video")


def entrar(
    cliente: TestClient, persona_id: str, metodo: MetodoAutenticacion | None = None
) -> TestClient:
    """Abre una sesión de `persona_id` por el seam de `identidad`, sin `/auth/dev` ni su
    `sesion.inicio`, y deja la cookie en `cliente`. El método es el del modo de la app."""
    estado = cliente.app.state  # type: ignore[attr-defined]
    with estado.bd.transaccion() as tx:
        sesion = estado.sesiones.abrir(tx, UUID(persona_id), metodo or estado.modo_de_ingreso)
    cliente.cookies.set(COOKIE_DE_SESION[estado.modo_de_ingreso], sesion)
    return cliente


def app_sin_trabajador(
    url_bd: str,
    *,
    capa_para: CapaPara,
    validar: Validar,
    directorio_originales: Path,
    modo_de_ingreso: MetodoAutenticacion = MetodoAutenticacion.DEV,
    reloj: Callable[[], datetime] | None = None,
    proxy: str | None = None,
    analizar_video: AnalizarVideo | None = sin_videos,
    tipos_esperados: tuple[str, ...] = (),
    oidc: ConfiguracionOIDC | None = None,
    bloque_habilitado: bool = False,
    datos_personales_habilitados: bool = False,
) -> FastAPI:
    return crear_app(
        armar_grafo(
            bd=_bd(url_bd),
            capa_para=capa_para,
            validar=validar,
            directorio_originales=directorio_originales,
            avisar=lambda: None,
            modo_de_ingreso=modo_de_ingreso,
            origen=ORIGEN,
            reloj=reloj,
            proxy=proxy,
            analizar_video=analizar_video,
            tipos_esperados=tipos_esperados,
            oidc=oidc,
            bloque_habilitado=bloque_habilitado,
            datos_personales_habilitados=datos_personales_habilitados,
        )
    )


@contextmanager
def en_marcha(
    url_bd: str,
    *,
    capa_para: CapaPara,
    validar: Validar,
    directorio_originales: Path,
    reloj: Callable[[], datetime] | None = None,
    bloque_habilitado: bool = False,
    datos_personales_habilitados: bool = False,
    conversor: Conversor | None = None,
) -> Iterator[TestClient]:
    """`web` arrancada con el trabajador del procesamiento al lado; al salir, los apaga."""
    bd = _bd(url_bd)
    trabajador = armar_trabajador(
        bd=bd,
        capa_para=capa_para,
        directorio_originales=directorio_originales,
        conversor=conversor,
    )
    grafo = armar_grafo(
        bd=bd,
        capa_para=capa_para,
        validar=validar,
        directorio_originales=directorio_originales,
        avisar=trabajador.despertar,
        modo_de_ingreso=MetodoAutenticacion.DEV,
        origen=ORIGEN,
        analizar_video=sin_videos,
        conversor=conversor,
        reloj=reloj,
        bloque_habilitado=bloque_habilitado,
        datos_personales_habilitados=datos_personales_habilitados,
    )
    with TestClient(crear_app(grafo), headers=CABECERAS) as cliente:
        assert cliente.portal is not None
        cliente.portal.call(trabajador.iniciar)
        try:
            yield cliente
        finally:
            # Antes de que el ciclo de vida de `web` cierre la base que comparten.
            cliente.portal.call(trabajador.detener)
