"""Los cuatro estados de una Pieza.

Los valores son exactamente los del vocabulario canonico de `CONTEXT.md`, con mayuscula
inicial, porque son el mismo texto que viaja al frontmatter de la Ficha, a la columna
`pieza.estado` y al JSON de la API (`modelo-datos.md`, encabezado).

`TRANSICIONES` son los cambios de estado que el dominio permite; los hace el Modulo de Fichas
(`arquitectura.md` §3.1) y la base rechaza cualquier otro (`pieza_transicion`, migracion 0050).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["TRANSICIONES", "Estado"]


class Estado(StrEnum):
    """Estado de una Pieza. Solo `Validada` es indexable, buscable y citable."""

    RECIBIDA = "Recibida"
    """El archivo llego; el procesamiento asincrono todavia no termino."""

    PENDIENTE = "Pendiente"
    """La Ficha propuesta esta lista y nadie la abrio."""

    INCOMPLETA = "Incompleta"
    """Alguien la trabajo y falta un campo obligatorio que no conocia."""

    VALIDADA = "Validada"
    """Campos obligatorios completos y firmada por una persona identificada."""


TRANSICIONES: Final[frozenset[tuple[Estado, Estado]]] = frozenset(
    {
        # El procesamiento propone la Ficha, o se completa a mano tras un fallo (DEC-122).
        (Estado.RECIBIDA, Estado.PENDIENTE),
        # Guardar: incompleta si falta un obligatorio, firmada si no (la aprobacion en bloque
        # tambien parte de Pendiente, DEC-146 punto 1).
        (Estado.PENDIENTE, Estado.INCOMPLETA),
        (Estado.PENDIENTE, Estado.VALIDADA),
        (Estado.INCOMPLETA, Estado.VALIDADA),
        # Reabrir una Validada y quitarle un obligatorio (spec, modulo 1).
        (Estado.VALIDADA, Estado.INCOMPLETA),
    }
)
"""Los cambios de estado permitidos, sin quedarse en el mismo. Nada vuelve a Recibida ni a
Pendiente (H-12): el reproceso, el retiro y la restauracion no cambian el estado."""
