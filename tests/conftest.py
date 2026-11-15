"""La suite maneja la aplicación por HTTP contra Postgres real.

Lo único sustituido es la capa de modelos (`CapaModelosFalsa`), inyectada al construir la
aplicación. Nada de dobles por módulo (`.scratch/prototipo/spec.md`, "El seam").

El servidor se indica con `HV_BD_URL_PRUEBAS`, como superusuario (el de `compose.pruebas.yml`,
nunca el del Prototipo). Al empezar la sesión cada proceso (uno solo, o uno por proceso de
pytest-xdist con `-n`) rehace su base con el bootstrap y las migraciones y siembra sus personas.

Entre test y test las tablas que los tests escriben quedan vacías. Un test que escribe en la base
lo hace en `tx`, que se revierte al terminar; el que necesita confirmar de verdad (la app con su
propio pool y su trabajador, dos conexiones, candados) lleva `@pytest.mark.confirma("motivo")` y
al terminar se le borran las filas. Un test sin la marca que deja filas confirmadas falla.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from aplicacion import en_marcha, entrar, pool_compartido
from base_de_pruebas import CLAVE_APP, vaciar_base
from capa_modelos_falsa import CapaModelosFalsa
from espera import esperar_estado
from historia_viva.migraciones import aplicar, sembrar_desarrollo
from validacion import MARIA, PDF

pytest_plugins = ["pytester", "marcador_de_requisito"]

# Los comandos que la suite corre con `Entorno.desde_variables(os.environ)` buscan sin corte: el
# umbral calibrado es del modelo real, no de los trigramas de la capa falsa (133).
os.environ["HV_UMBRAL_SIMILITUD"] = "0"

BASE_DE_LA_SUITE = "hv_suite"
# Las tablas que escriben los tests, en un orden que respeta sus claves foráneas.
TABLAS_DE_LOS_TESTS = (
    "aporte_fragmento_fuente",
    "aporte_pieza_fuente",
    "aporte",
    "efemeride_fuente",
    "efemeride",
    "relato_fuente",
    "relato",
    "informe_aprobacion",
    "informe_pieza",
    "informe",
    "mensaje_cita",
    "mensaje_conversacion",
    "conversacion",
    "token_agente",
    "agente_externo",
    "medicion_validacion",
    "trabajo_procesamiento",
    "ficha",
    "aprobacion_en_bloque",
    "data_fragmento",
    "pieza_carrera",
    "replicacion_drive",
    "pieza",
    "lote_carrera",
    "lote",
    "auditoria",
    "sesion",
)
# El de `prototipo/docker-compose.yml` (historia 56; lo controla `test_composicion`).
PUERTO_BD_DEL_PROTOTIPO = "55432"


_MODULOS_AL_RECOLECTAR = pytest.StashKey[frozenset[str]]()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Toda corrida deja su `junit` en `.resultados` (antes que `junitxml` lea la opción): la
    # `matriz` toma de ahí el último resultado de cada test (historia 221).
    # shortcut: los archivos se acumulan; si molestan, borrar los viejos de `.resultados`.
    if not config.option.collectonly and getattr(config.option, "xmlpath", "") is None:
        momento = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        config.option.xmlpath = str(config.rootpath / ".resultados" / f"junit-{momento}.xml")
    config.addinivalue_line(
        "markers",
        "confirma(motivo): el test confirma filas fuera de `tx`; se borran al terminar."
        " El motivo dice por qué no le alcanza el rollback.",
    )
    config.addinivalue_line(
        "markers",
        "drive_real: sube a la unidad compartida real; se saltea sin HV_DRIVE_CLAVE_PRUEBA (126).",
    )
    config.addinivalue_line(
        "markers",
        "real: corre contra un Ollama real (`-m real`); sin el motor o sin sus modelos, se saltea.",
    )


# Confirman cambios en `configuracion_capacidad`, que la app de los otros workers lee mientras el
# test corre: van en serie con la búsqueda, la indexación y las semillas (grupo `indice`).
_CAMBIAN_LA_CONFIGURACION = {"editar_fila", "borrar_fila", "filas_restauradas"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        usa = _CAMBIAN_LA_CONFIGURACION & set(getattr(item, "fixturenames", ()))
        if usa and item.get_closest_marker("xdist_group") is None:
            item.add_marker(pytest.mark.xdist_group("indice"))


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    # `-n auto`: en esta PC 8 y 10 procesos no bajan el tiempo y con 14 las conexiones esperaban
    # más que los 3 s de `ESPERA_S` (Postgres en Windows abre unas 6 por segundo; ticket 130).
    return min(6, os.cpu_count() or 1)


def pytest_collection_finish(session: pytest.Session) -> None:
    session.config.stash[_MODULOS_AL_RECOLECTAR] = frozenset(sys.modules)


@pytest.fixture
def modulos_al_recolectar(pytestconfig: pytest.Config) -> frozenset[str]:
    """Los módulos importados al terminar de recolectar: lo que paga cada proceso de xdist."""
    return pytestconfig.stash[_MODULOS_AL_RECOLECTAR]


@pytest.fixture(scope="session")
def url_superusuario() -> str:
    url = os.environ.get("HV_BD_URL_PRUEBAS")
    if not url:
        pytest.exit(
            "Falta HV_BD_URL_PRUEBAS: la suite corre contra Postgres real.", returncode=2
        )
    puerto = conninfo_to_dict(url).get("port") or os.environ.get("PGPORT")
    if str(puerto) == PUERTO_BD_DEL_PROTOTIPO:
        pytest.exit(
            "HV_BD_URL_PRUEBAS apunta a la base del Prototipo y la suite vacía tablas: usá la de "
            "`compose.pruebas.yml` (historia 56).",
            returncode=2,
        )
    return url


@pytest.fixture
def base_vacia(url_superusuario: str, worker_id: str) -> str:
    """Una base recién salida del bootstrap, sin migraciones: el DSN de `hv_migrador`. Cada
    proceso tiene la suya y se vacía antes de cada test."""
    return vaciar_base(url_superusuario, f"hv_vacia_{worker_id}")


@pytest.fixture(scope="session")
def url_bd(url_superusuario: str, worker_id: str) -> str:
    """La base de la suite, vaciada y migrada desde cero en cada sesión con `migrar`: el DSN
    de `hv_migrador`, el dueño, con el que los tests preparan y vacían las tablas. Cada proceso
    de xdist tiene la suya (`hv_suite_gw0`, ...); sin `-n`, `worker_id` es `master`."""
    base = BASE_DE_LA_SUITE if worker_id == "master" else f"{BASE_DE_LA_SUITE}_{worker_id}"
    dsn = vaciar_base(url_superusuario, base)
    aplicar(dsn)
    # La base nace sin personas (DEC-52): la suite usa las de prueba de desarrollo, una por rol.
    sembrar_desarrollo(dsn)
    return dsn


@pytest.fixture(scope="session")
def url_app(url_bd: str) -> Iterator[str]:
    """El DSN con el que conecta la aplicación: `hv_app`, sin DELETE ni DDL (DEC-31). Las apps
    que la suite arranca con él comparten un pool por proceso."""
    url = make_conninfo(url_bd, user="hv_app", password=CLAVE_APP)
    with pool_compartido(url):
        yield url


@pytest.fixture(scope="session")
def _conexion_del_dueno(url_bd: str) -> Iterator[psycopg.Connection[Any]]:
    """Una conexión de `hv_migrador` por proceso: la de `tx` y la que limpia. Conectar en
    Windows cuesta cientos de milisegundos."""
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        yield conexion


@pytest.fixture
def tx(_conexion_del_dueno: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    """Conexión de `hv_migrador` en una transacción que se revierte al terminar el test. Un
    `tx.transaction()` adentro es un punto de guardado."""
    with _conexion_del_dueno.transaction(force_rollback=True):
        yield _conexion_del_dueno


def _vaciar_tablas_de_los_tests(conexion: psycopg.Connection[Any]) -> int:
    """Borra lo que escriben los tests y devuelve cuántas filas había."""
    dejadas = sum(
        conexion.execute(sql.SQL("DELETE FROM acervo.{}").format(sql.Identifier(t))).rowcount
        for t in TABLAS_DE_LOS_TESTS
    )
    # El interruptor vuelve a como lo dejó la migración: un freno puesto pararía al resto.
    return dejadas + conexion.execute(
        "DELETE FROM acervo.evento_interruptor WHERE actor_tipo <> 'sistema'"
    ).rowcount


@pytest.fixture(autouse=True)
def base_limpia(request: pytest.FixtureRequest) -> Iterator[None]:
    # Los tests que solo leen archivos no piden la base: corren sin Postgres.
    if "url_bd" not in request.fixturenames:
        yield
        return
    conexion: psycopg.Connection[Any] = request.getfixturevalue("_conexion_del_dueno")
    yield
    with conexion.transaction():
        dejadas = _vaciar_tablas_de_los_tests(conexion)
        # La fila única de la programación vuelve sin pedido ni corrida (058) y con los valores
        # iniciales (060: toda edición sube la versión).
        dejadas += conexion.execute(
            "UPDATE acervo.programacion_efemerides SET pedido_manual_el = NULL,"
            " pedido_manual_objetivo = NULL, pedido_manual_por = NULL, ultima_corrida_el = NULL,"
            " ultima_corrida_resultado = NULL, ultima_programada_el = NULL, habilitada = false,"
            " periodicidad = 'semanal', anticipacion_dias = 14, maximo_por_corrida = 50,"
            " alcances = '{dia,mes}', version = 1, actualizada_por = NULL"
            " WHERE pedido_manual_el IS NOT NULL OR ultima_corrida_el IS NOT NULL"
            " OR ultima_programada_el IS NOT NULL OR version <> 1"
        ).rowcount
        # No cuenta como dejada: el trabajador de la app late solo, sin que el test lo pida.
        conexion.execute("DELETE FROM acervo.latido_trabajador")
    if dejadas and request.node.get_closest_marker("confirma") is None:
        pytest.fail(
            f"El test dejó {dejadas} filas confirmadas: escribí en `tx` o marcalo con"
            " `@pytest.mark.confirma(motivo)`."
        )


@pytest.fixture
def editar_fila(
    _conexion_del_dueno: psycopg.Connection[Any],
) -> Iterator[Callable[[LiteralString], None]]:
    """Edita `configuracion_capacidad` por SQL, como el Administrador técnico (DEC-87), y al
    terminar deja las filas como estaban: la base de la suite se comparte. Cada edición se
    confirma enseguida (la conexión es `autocommit`): la app la lee desde su pool."""
    conexion = _conexion_del_dueno
    filas = conexion.execute(
        "SELECT motor, modelo, instruccion_id, parametros::text, capacidad"
        " FROM acervo.configuracion_capacidad"
    ).fetchall()

    def editar(sentencia: LiteralString) -> None:
        conexion.execute(sentencia)

    yield editar
    with conexion.transaction():
        conexion.cursor().executemany(
            "UPDATE acervo.configuracion_capacidad SET motor = %s, modelo = %s,"
            " instruccion_id = %s, parametros = %s::jsonb WHERE capacidad = %s",
            filas,
        )


@pytest.fixture
def borrar_fila(
    _conexion_del_dueno: psycopg.Connection[Any],
) -> Iterator[Callable[[str], None]]:
    """Borra la fila de una capacidad de `configuracion_capacidad`, confirmado, y al terminar la
    reinserta tal cual (`editar_fila` restaura con UPDATE: no devuelve una fila borrada)."""
    conexion = _conexion_del_dueno
    conexion.execute(
        "CREATE TEMP TABLE filas_borradas AS"
        " SELECT * FROM acervo.configuracion_capacidad WITH NO DATA"
    )

    def borrar(capacidad: str) -> None:
        conexion.execute(
            "WITH borrada AS (DELETE FROM acervo.configuracion_capacidad WHERE capacidad = %s"
            " RETURNING *) INSERT INTO filas_borradas SELECT * FROM borrada",
            (capacidad,),
        )

    try:
        yield borrar
    finally:
        with conexion.transaction():
            conexion.execute("INSERT INTO acervo.configuracion_capacidad SELECT * FROM filas_borradas")
            conexion.execute("DROP TABLE filas_borradas")


@pytest.fixture
def capa_modelos() -> CapaModelosFalsa:
    return CapaModelosFalsa()


@pytest.fixture
def directorio_originales(tmp_path: Path) -> Path:
    return tmp_path / "originales"


@pytest.fixture
def cliente(
    url_app: str, capa_modelos: CapaModelosFalsa, directorio_originales: Path
) -> Iterator[TestClient]:
    # Se libera la capa antes de salir: apagar el trabajador espera al trabajo en curso, y un
    # test que dejó la capa demorada no tiene por qué costar esa espera.
    with en_marcha(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    ) as cliente:
        try:
            yield cliente
        finally:
            capa_modelos.liberar()


@pytest.fixture
def pieza_pendiente(cliente: TestClient) -> str:
    """Sube un documento y espera a que su Ficha propuesta esté lista para validar.

    Es el punto de partida de todo lo que hace una persona en la pantalla partida: la Pieza
    ya tiene su Ficha propuesta y nadie la abrió todavía.
    """
    entrar(cliente, MARIA)
    pieza_id: str = cliente.post(
        "/api/piezas", files={"archivo": ("acta-fundacion.pdf", PDF, "application/pdf")}
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    return pieza_id


@pytest.fixture
def padron_restaurado(_conexion_del_dueno: psycopg.Connection[Any]) -> Iterator[None]:
    """Para los tests que confirman altas, bajas o cambios de rol: al terminar, el padrón vuelve
    a ser el de la semilla, con sus roles, todos activos como estaban y sin el `sub` que haya
    vinculado un ingreso con Google."""
    conexion = _conexion_del_dueno
    semilla = conexion.execute(
        "SELECT usuario_id, rol, activo, sub_google, ultimo_acceso_el FROM acervo.usuario"
    ).fetchall()
    ids = [fila[0] for fila in semilla]
    yield
    with conexion.transaction():
        # Antes que `base_limpia`: los Lotes y las Piezas de una persona nueva la referencian.
        _vaciar_tablas_de_los_tests(conexion)
        conexion.execute("DELETE FROM acervo.sesion WHERE usuario_id <> ALL(%s)", (ids,))
        conexion.execute("DELETE FROM acervo.usuario WHERE usuario_id <> ALL(%s)", (ids,))
        conexion.cursor().executemany(
            "UPDATE acervo.usuario SET rol = %s, activo = %s, sub_google = %s,"
            " ultimo_acceso_el = %s"
            " WHERE usuario_id = %s",
            [(rol, activo, sub, acceso, usuario_id)
             for usuario_id, rol, activo, sub, acceso in semilla],
        )


@pytest.fixture
def sin_rgc(padron_restaurado: None, _conexion_del_dueno: psycopg.Connection[Any]) -> None:
    """El padrón antes de que la SEU nombre al RGC (C47): rige el interinato del Administrador
    técnico (R-01). Se confirma para que lo vea la app; `padron_restaurado` lo deshace."""
    _conexion_del_dueno.execute("UPDATE acervo.usuario SET activo = false WHERE rol = 'rgc'")
