"""Conexión en ejecución: pool, transacción y salud (spec, módulo `bd`).

`web` y el trabajador conectan como `hv_app`, sin `DELETE` ni DDL (DEC-31): arrancar la
aplicación nunca cambia el esquema. `transaccion()` es el único contexto transaccional: confirma
al salir y revierte todo ante cualquier excepción. Una base que no responde llega como
`BaseNoDisponible`, que la API traduce a 503 (falla cerrado, historia 55).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

__all__ = ["AJUSTES_DE_SESION", "ESPERA_S", "BaseNoDisponible", "Bd", "Tx"]

Tx = psycopg.Connection[DictRow]

ESPERA_S = 3
"""Cuánto se espera una conexión: la salud responde 503 si la base no responde en 3 s."""

AJUSTES_DE_SESION = "-c lock_timeout=5s -c idle_in_transaction_session_timeout=60s"
"""DEC-145: ningún candado se espera más de 5 s y ninguna transacción queda ociosa más de 60 s
(DEC-99: un modelo nunca corre con una conexión tomada)."""


class BaseNoDisponible(Exception):
    """La base no responde: no hay conexión, se cayó a mitad de camino o el pool se cerró."""

    def __init__(self, causa: Exception) -> None:
        super().__init__(f"La base de datos no responde: {causa}")
        self.causa = causa


class Bd:
    def __init__(self, dsn: str, *, tamano_pool: int) -> None:
        self.dsn = dsn
        """`indice` arma con él la conexión de `PGVectorStore`."""
        self._pool = ConnectionPool(
            dsn,
            connection_class=Tx,
            kwargs={
                "row_factory": dict_row,
                "options": AJUSTES_DE_SESION,
                "connect_timeout": ESPERA_S,
            },
            min_size=1,
            max_size=tamano_pool,
            timeout=ESPERA_S,
            # Tras reiniciar Postgres, la conexión rota se descarta antes de entregarla.
            check=ConnectionPool.check_connection,
            open=True,
        )

    @contextmanager
    def transaccion(self) -> Iterator[Tx]:
        try:
            # El BEGIN explícito hace que un `tx.transaction()` adentro sea siempre un punto de
            # guardado: sin él, como primera sentencia, confirmaría por su cuenta.
            with self._pool.connection() as tx, tx.transaction():
                yield tx
        except psycopg.OperationalError as error:  # incluye el pool agotado o cerrado
            raise BaseNoDisponible(error) from error

    @contextmanager
    def sesion(self) -> Iterator[Tx]:
        """Una conexión sin transacción abierta, para lo que dura más que una: un candado
        consultivo de sesión que se tiene mientras un modelo redacta (DEC-99)."""
        try:
            with self._pool.connection() as conexion:
                conexion.autocommit = True
                try:
                    yield conexion
                finally:
                    # Sobre una conexión rota no se toca nada: taparía el error original, y el
                    # pool la descarta igual.
                    if conexion.info.transaction_status == TransactionStatus.IDLE:
                        conexion.autocommit = False
        except psycopg.OperationalError as error:
            raise BaseNoDisponible(error) from error

    def ping(self) -> None:
        with self.transaccion() as tx:
            tx.execute("SELECT 1")

    def cerrar(self) -> None:
        self._pool.close()
