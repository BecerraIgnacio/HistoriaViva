"""Esperar a que el procesamiento avance, igual que lo hace la pantalla: sondeando.

La Pieza pasa sola de `Recibida` a `Pendiente` cuando la tarea en segundo plano termina.
Ni la pantalla ni la suite reciben un aviso: las dos preguntan por HTTP hasta que el estado
cambia.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from fastapi.testclient import TestClient

TOPE_S = 10.0
"""Cuánto se espera antes de dar el procesamiento por colgado."""


def esperar_estado(cliente: TestClient, pieza_id: str, estado: str) -> dict[str, Any]:
    """Sondea la Pieza hasta que llega al estado pedido y devuelve su detalle."""
    limite = time.monotonic() + TOPE_S
    while True:
        detalle: dict[str, Any] = cliente.get(f"/api/piezas/{pieza_id}").json()
        if detalle.get("estado") == estado:
            return detalle
        if time.monotonic() > limite:
            raise AssertionError(
                f"La Pieza {pieza_id} sigue en {detalle.get('estado')!r} después de "
                f"{TOPE_S} s; se esperaba {estado!r}."
            )
        time.sleep(0.02)


def esperar_error_procesamiento(cliente: TestClient, pieza_id: str) -> dict[str, Any]:
    """Sondea la Pieza hasta que el trabajo falló y `error_procesamiento` quedó escrito."""
    limite = time.monotonic() + TOPE_S
    while True:
        detalle: dict[str, Any] = cliente.get(f"/api/piezas/{pieza_id}").json()
        if detalle.get("error_procesamiento") is not None:
            return detalle
        if time.monotonic() > limite:
            raise AssertionError(
                f"La Pieza {pieza_id} sigue sin `error_procesamiento` después de {TOPE_S} s."
            )
        time.sleep(0.02)


def esperar_detras_del_candado(candado: psycopg.Connection[Any], cuantas: int) -> None:
    """Hasta que `cuantas` transacciones esperan detrás del candado de `candado`, directa o
    indirectamente (la segunda espera a la primera, no al candado). Solo las de esta prueba:
    con xdist, las bases de los otros workers viven en el mismo servidor."""
    consulta = (
        "WITH RECURSIVE esperando(pid) AS ("
        " SELECT pid FROM pg_stat_activity WHERE %(candado)s = ANY(pg_blocking_pids(pid))"
        " UNION SELECT a.pid FROM pg_stat_activity a JOIN esperando e"
        " ON e.pid = ANY(pg_blocking_pids(a.pid)))"
        " SELECT count(*) FROM esperando"
    )
    parametros = {"candado": candado.info.backend_pid}
    limite = time.monotonic() + TOPE_S
    while True:
        # Dentro de la transacción del candado, pg_stat_activity es una foto fija: sin
        # limpiarla no aparecen las conexiones que se abren después.
        candado.execute("SELECT pg_stat_clear_snapshot()")
        if candado.execute(consulta, parametros).fetchone()[0] >= cuantas:  # type: ignore[index]
            return
        assert time.monotonic() < limite, "Nadie llegó a esperar el candado."
        time.sleep(0.05)
