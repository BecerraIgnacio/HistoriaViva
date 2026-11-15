"""Bases de prueba: el bootstrap de `db/bootstrap.sql`, su vaciado y su borrado.

Vive acá y no en `conftest.py` porque lo importan los tests del esquema, no solo las fixtures.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

BOOTSTRAP = Path(__file__).resolve().parent.parent / "db" / "bootstrap.sql"
# Los roles son del servidor, no de una base: toda la suite usa las mismas claves.
CLAVE_MIGRADOR = "migrador-de-pruebas"
CLAVE_APP = "app-de-pruebas"
CANDADO_DEL_BOOTSTRAP = 20_251_130
"""Candado consultivo del bootstrap. Los roles son del clúster: dos bootstraps a la vez (procesos
de xdist, `base_vacia`) chocan al crearlos o alterarlos."""


def preparar(
    url_superusuario: str,
    base: str,
    *,
    clave_migrador: str = CLAVE_MIGRADOR,
    clave_app: str = CLAVE_APP,
) -> None:
    """Lo que hace la imagen de Postgres sobre un volumen nuevo: la base y el bootstrap.

    El SQL es el mismo; las claves llegan como ajustes de la sesión, como en `db/bootstrap.sh`.
    """
    with psycopg.connect(url_superusuario, autocommit=True) as servidor:
        if not servidor.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (base,)
        ).fetchone():
            servidor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(base)))
        with psycopg.connect(
            make_conninfo(url_superusuario, dbname=base), autocommit=True
        ) as conexion:
            conexion.execute(
                "SELECT set_config('hv.clave_migrador', %s, false),"
                " set_config('hv.clave_app', %s, false)",
                (clave_migrador, clave_app),
            )
            # Un candado consultivo vale dentro de una base: se toma en la del superusuario, la
            # misma para todos.
            servidor.execute("SELECT pg_advisory_lock(%s)", (CANDADO_DEL_BOOTSTRAP,))
            try:
                conexion.execute(BOOTSTRAP.read_bytes())
            finally:
                servidor.execute("SELECT pg_advisory_unlock(%s)", (CANDADO_DEL_BOOTSTRAP,))


def crear_base(url_superusuario: str, base: str) -> str:
    """Crea `base` desde cero con el bootstrap y devuelve el DSN de `hv_migrador`."""
    borrar_base(url_superusuario, base)
    preparar(url_superusuario, base)
    return _dsn_del_migrador(url_superusuario, base)


def vaciar_base(url_superusuario: str, base: str) -> str:
    """Deja `base` como recién salida del bootstrap, sin migraciones, y devuelve el DSN de
    `hv_migrador`. La crea solo si no existe: en Windows crear o borrar una base tarda segundos
    y fuerza un checkpoint, y todo lo que migran las migraciones y los tests vive en `acervo`."""
    preparar(url_superusuario, base)
    dsn = _dsn_del_migrador(url_superusuario, base)
    with psycopg.connect(dsn, autocommit=True) as conexion:
        conexion.execute("DROP SCHEMA IF EXISTS acervo CASCADE")
    return dsn


def _dsn_del_migrador(url_superusuario: str, base: str) -> str:
    return make_conninfo(
        url_superusuario, dbname=base, user="hv_migrador", password=CLAVE_MIGRADOR
    )


def borrar_base(url_superusuario: str, base: str, *, esperar: bool = True) -> None:
    """Sin `esperar`, manda el `DROP DATABASE` y corta: Postgres lo termina aunque el cliente ya
    no esté. Borrar una base fuerza un checkpoint, que al final de la suite tarda decenas de
    segundos."""
    borrar = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(base))
    conexion = psycopg.connect(url_superusuario, autocommit=True)
    if esperar:
        with conexion:
            conexion.execute(borrar)
        return
    conexion.pgconn.send_query(borrar.as_bytes(conexion))
    while conexion.pgconn.flush():
        pass
    conexion.close()
