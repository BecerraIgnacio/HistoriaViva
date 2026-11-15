"""Los filtros de la recuperación por HTTP, compartidos por `GET /api/busqueda` y el turno de la
Conversación: el mismo armado y la misma validación contra los catálogos (DEC-127)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from historia_viva import catalogos
from historia_viva.bd import Bd
from historia_viva.dominio.tipos import FiltroDeBusqueda

__all__ = ["armar_filtro", "validar_filtro"]


def armar_filtro(
    *,
    desde: date | None,
    hasta: date | None,
    tipo: Sequence[str],
    tipo_actividad: Sequence[str],
    carrera: Sequence[str],
) -> FiltroDeBusqueda:
    """Con los nombres del pedido; un valor repetido cuenta una vez."""
    return FiltroDeBusqueda(
        desde=desde,
        hasta=hasta,
        tipos=tuple(dict.fromkeys(tipo)),
        tipos_actividad=tuple(dict.fromkeys(tipo_actividad)),
        carreras=tuple(dict.fromkeys(carrera)),
    )


def validar_filtro(bd: Bd, filtro: FiltroDeBusqueda) -> None:
    """Contra los catálogos, después de `FiltroDeBusqueda.validar` (un NUL no llega a la base).
    Un valor dado de baja se acepta: las Fichas que ya lo tenían lo conservan.

    Raises:
        ValorFueraDeCatalogo: 422 `tipo-desconocido`, `tipo-actividad-desconocido` o
            `carrera-desconocida`.
    """
    with bd.transaccion() as tx:
        for valor in filtro.tipos:
            catalogos.validar_tipo_de_pieza(tx, valor, conserva=valor)
        for valor in filtro.tipos_actividad:
            catalogos.validar_tipo_actividad(tx, valor, conserva=valor)
        catalogos.validar_carreras(tx, filtro.carreras, conserva=filtro.carreras)
