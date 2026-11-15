"""El latido de los trabajadores: cómo sabe la salud que hay uno vivo de cada tipo (DEC-106).

Cada proceso trabajador reescribe su fila de `acervo.latido_trabajador` cada
`INTERVALO_DE_LATIDO_S` desde un hilo propio, con el mismo id con que toma trabajos. Un tipo
está vivo si alguno de sus trabajadores latió hace menos de `LATIDO_VIGENTE`: así la fila que
deja un proceso reiniciado no lo da por caído.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from historia_viva.bd import Bd

__all__ = ["INTERVALO_DE_LATIDO_S", "LATIDO_VIGENTE", "LatidoDeTipo", "latidos_por_tipo", "latir"]

INTERVALO_DE_LATIDO_S = 15.0
"""Cada cuánto late un trabajador (spec §2.4, «Trabajador vivo»)."""

LATIDO_VIGENTE = timedelta(seconds=60)
"""Sin latido en este lapso, el tipo figura caído: cuatro latidos perdidos."""


@dataclass(frozen=True)
class LatidoDeTipo:
    tipo: str
    latido_el: datetime | None
    """El latido más reciente de los trabajadores del tipo; `None` si nunca latió ninguno."""
    vivo: bool


def latir(bd: Bd, trabajador: str, tipos: Iterable[str]) -> None:
    """Anota que `trabajador` sigue en pie, en una transacción corta y propia."""
    with bd.transaccion() as tx:
        tx.execute(
            "INSERT INTO acervo.latido_trabajador (trabajador, tipos) VALUES (%s, %s)"
            " ON CONFLICT (trabajador) DO UPDATE SET tipos = EXCLUDED.tipos, latido_el = now()",
            (trabajador, list(tipos)),
        )


def latidos_por_tipo(
    bd: Bd, *, esperados: Iterable[str] = (), ahora: datetime | None = None
) -> list[LatidoDeTipo]:
    """El último latido de cada tipo esperado o que alguna vez tuvo trabajador, y si está vivo.

    Un tipo de `esperados` sin ninguna fila figura caído: su trabajador nunca arrancó. `vivo`
    se calcula con el `now()` de la base, el mismo reloj que escribió el latido; `ahora` lo
    reemplaza solo en los tests.
    """
    with bd.transaccion() as tx:
        filas = tx.execute(
            "SELECT tipo, max(l.latido_el) AS latido_el,"
            " coalesce(coalesce(%s, now()) - max(l.latido_el) < %s, false) AS vivo"
            " FROM unnest(%s::text[]) AS esperado(tipo)"
            " FULL JOIN (SELECT unnest(tipos) AS tipo, latido_el"
            "  FROM acervo.latido_trabajador) AS l USING (tipo)"
            " GROUP BY tipo ORDER BY tipo",
            (ahora, LATIDO_VIGENTE, list(esperados)),
        ).fetchall()
    return [LatidoDeTipo(**fila) for fila in filas]
