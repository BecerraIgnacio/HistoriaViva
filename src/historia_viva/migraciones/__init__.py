"""El esquema versionado (DEC-31, DEC-105).

`aplicar` es el runner: corre como `hv_migrador`, nunca al arrancar la aplicación, aplica en
orden los `.sql` de `sql/` y anota cada uno en `acervo.schema_migracion`. Ninguna migración
necesita superusuario: los roles `hv_migrador` y `hv_app` y la extensión `vector` los deja el
bootstrap (`db/bootstrap.sh`), que corre una sola vez.

El número de una migración es su orden de aplicación: cada una toma el siguiente libre, y el
runner rechaza un número menor que el último aplicado. Los números del spec son nombres de
referencia (auditoría de tickets, decisión 4).

Sin ORM y sin Alembic: la única dependencia es psycopg.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from psycopg import connect

__all__ = ["DIRECTORIO", "Migracion", "MigracionRechazada", "aplicar", "sembrar_desarrollo"]

DIRECTORIO = Path(__file__).resolve().parent / "sql"
SEMILLA_DESARROLLO = Path(__file__).resolve().parent / "semilla_desarrollo.sql"
MARCA_SIN_TRANSACCION = "-- sin-transaccion"
CANDADO = 20_250_049
"""Clave del candado consultivo de `migrar`; cualquier número fijo sirve si nadie más lo usa."""


class MigracionRechazada(Exception):
    """El runner no aplica nada: lo que hay en disco no cuadra con lo ya aplicado."""


@dataclass(frozen=True)
class Migracion:
    """Un archivo `NNNN_nombre.sql`: el número es el orden de aplicación."""

    version: int
    nombre: str
    sql: str
    checksum: str
    sin_transaccion: bool
    """Marcada con `-- sin-transaccion` en la primera línea, para lo que no corre dentro de
    una (`CREATE INDEX CONCURRENTLY`). Lleva una sola sentencia: varias, Postgres las agrupa
    en una transacción implícita."""

    @classmethod
    def leer(cls, ruta: Path) -> Migracion:
        numero, _, nombre = ruta.stem.partition("_")
        # Sin CRLF: con `core.autocrlf` el mismo archivo no puede dar otro checksum en Windows.
        texto = ruta.read_bytes().decode("utf-8").replace("\r\n", "\n")
        return cls(
            version=int(numero),
            nombre=nombre,
            sql=texto,
            checksum=hashlib.sha256(texto.encode("utf-8")).hexdigest(),
            sin_transaccion=texto.startswith(MARCA_SIN_TRANSACCION),
        )


def aplicar(dsn_migrador: str, *, directorio: Path = DIRECTORIO) -> list[Migracion]:
    """Aplica en orden las migraciones que falten y devuelve las que aplicó."""
    migraciones = sorted(
        (Migracion.leer(ruta) for ruta in directorio.glob("*.sql")), key=lambda m: m.version
    )
    for anterior, siguiente in pairwise(migraciones):
        if anterior.version == siguiente.version:
            raise MigracionRechazada(
                f"{anterior.version:04d}_{anterior.nombre} y {siguiente.version:04d}_"
                f"{siguiente.nombre} tienen el mismo número. Renumerá una con el siguiente libre."
            )
    aplicadas: list[Migracion] = []
    with connect(dsn_migrador, autocommit=True) as conexion:
        # Candado de sesión: un segundo `migrar` espera acá y después no encuentra nada que
        # hacer. Se suelta al cerrar la conexión.
        conexion.execute("SELECT pg_advisory_lock(%s)", (CANDADO,))
        conexion.execute("CREATE SCHEMA IF NOT EXISTS acervo")
        conexion.execute(
            "CREATE TABLE IF NOT EXISTS acervo.schema_migracion ("
            " version integer PRIMARY KEY, nombre text NOT NULL, checksum text NOT NULL,"
            " aplicada_el timestamptz NOT NULL DEFAULT now())"
        )
        ya: dict[int, str] = dict(
            conexion.execute("SELECT version, checksum FROM acervo.schema_migracion").fetchall()
        )
        ultima = max(ya, default=0)
        # Una base restaurada de un respaldo más nuevo, o un tag anterior: este código no
        # conoce su esquema y no debe escribir sobre ella.
        desconocidas = sorted(set(ya) - {migracion.version for migracion in migraciones})
        if desconocidas:
            raise MigracionRechazada(
                f"La base tiene migraciones que este código no trae"
                f" ({', '.join(f'{v:04d}' for v in desconocidas)}): es más nueva que el"
                " código. Actualizá el código antes de migrar."
            )
        for migracion in migraciones:
            if ya.get(migracion.version, migracion.checksum) != migracion.checksum:
                raise MigracionRechazada(
                    f"{migracion.version:04d}_{migracion.nombre}: cambió el checksum de una"
                    " migración ya aplicada. Las aplicadas no se editan: escribí una nueva."
                )
            if migracion.version not in ya and migracion.version < ultima:
                raise MigracionRechazada(
                    f"{migracion.version:04d}_{migracion.nombre}: su número es menor que la"
                    f" última aplicada ({ultima:04d}). Renumerala con el siguiente libre."
                )
        for migracion in migraciones:
            if migracion.version in ya:
                continue
            registrar = (
                ("INSERT INTO acervo.schema_migracion (version, nombre, checksum)"
                " VALUES (%s, %s, %s)"),
                (migracion.version, migracion.nombre, migracion.checksum),
            )
            if migracion.sin_transaccion:
                conexion.execute(migracion.sql.encode("utf-8"))
                conexion.execute(*registrar)
            else:
                with conexion.transaction():
                    conexion.execute(migracion.sql.encode("utf-8"))
                    conexion.execute(*registrar)
            aplicadas.append(migracion)
    return aplicadas


def sembrar_desarrollo(dsn_migrador: str) -> None:
    """Las personas de prueba, una por rol (DEC-124), y las Carreras de prueba (C45). Solo en
    desarrollo; se puede repetir."""
    with connect(dsn_migrador) as conexion:
        conexion.execute(SEMILLA_DESARROLLO.read_bytes())
