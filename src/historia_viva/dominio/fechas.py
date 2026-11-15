"""La Fecha de una Pieza: siempre un rango cerrado, con su precision declarada.

`CONTEXT.md` define Fecha como "un rango con inicio y fin. Una fecha exacta es un rango
de un solo dia; 'entre 2015 y 2019' es un rango legitimo y suficiente para indexar".

`precision_fecha` declara **que se sabe de verdad**, no cuanto dura el hecho: sin ella,
`[1975-01-01, 1975-12-31]` es indistinguible de un evento que efectivamente duro todo
1975 (`modelo-datos.md` §4).

**Decision D-P1 — el valor `rango`.** `modelo-datos.md` §4 y la restriccion
`pieza_precision_coherente` de §7 admiten cuatro precisiones: `dia`, `mes`, `anio` y
`decada`. Ninguna de las cuatro acepta "entre 2015 y 2019", que `CONTEXT.md` declara Fecha
legitima y que la user story 13 exige poder firmar. `rango` es la quinta precision y cubre
ese hueco: bordes libres, con la unica condicion de que el fin no preceda al inicio.
El fundamento completo esta en `docs/prototipo/contratos.md`.
"""

from __future__ import annotations

import calendar
from datetime import date
from enum import StrEnum

__all__ = ["PrecisionFecha", "derivar_rango", "fecha_legible"]


class PrecisionFecha(StrEnum):
    """Con que resolucion se conoce la Fecha de una Pieza."""

    DIA = "dia"
    """Se sabe el dia exacto: el rango es de un solo dia."""

    MES = "mes"
    """Se sabe el mes: el rango es el mes calendario completo."""

    ANIO = "anio"
    """Se sabe el anio: el rango va del 1 de enero al 31 de diciembre."""

    DECADA = "decada"
    """Se sabe la decada: el rango son los diez anios que empiezan en anio terminado en cero."""

    RANGO = "rango"
    """Se saben dos bordes y nada mas: "entre 2015 y 2019" (decision D-P1)."""


def derivar_rango(
    fecha_inicio: date,
    precision: PrecisionFecha,
    fecha_fin: date | None = None,
) -> tuple[date, date]:
    """Deriva el rango cerrado `[inicio, fin]` de una Fecha a partir de su precision.

    La pantalla de validacion pide una fecha y una precision; el rango no se pide dos
    veces (`spec.md` §4, nota de `fecha_fin`). Esta funcion es la unica autoridad sobre
    como se deriva, para que la API, el Modulo de Fichas y los tests coincidan.

    El resultado esta **normalizado**: quien guarde la Pieza debe escribir
    `fecha_inicio = rango[0]` y `fecha_fin = rango[1]`, no la fecha que recibio. Con
    precision `decada`, `1975-05-03` produce `[1970-01-01, 1979-12-31]`, y guardar el
    `1975-05-03` original violaria la restriccion `pieza_precision_coherente`.

    Args:
        fecha_inicio: La fecha declarada. Para `decada`, `anio` y `mes` alcanza con que
            caiga dentro del periodo: el borde se calcula.
        precision: Con que resolucion se conoce la Fecha.
        fecha_fin: Obligatoria si `precision` es `RANGO`. Con las otras cuatro
            precisiones es opcional y, si se pasa, debe coincidir con el borde derivado.

    Returns:
        El par `(inicio, fin)` del rango cerrado. `inicio <= fin` siempre.

    Raises:
        ValueError: Si falta `fecha_fin` con precision `RANGO`, si el fin precede al
            inicio, o si la `fecha_fin` recibida contradice el borde derivado de la
            precision declarada.
    """
    if precision is PrecisionFecha.RANGO:
        if fecha_fin is None:
            raise ValueError(
                "La precision 'rango' exige una fecha de fin explicita: es el unico "
                "caso en que el borde no se deriva."
            )
        if fecha_fin < fecha_inicio:
            raise ValueError(
                f"El fin del rango ({fecha_fin.isoformat()}) no puede preceder a su "
                f"inicio ({fecha_inicio.isoformat()})."
            )
        return (fecha_inicio, fecha_fin)

    derivado = _derivar_borde_cerrado(fecha_inicio, precision)

    if fecha_fin is not None and fecha_fin != derivado[1]:
        raise ValueError(
            f"La fecha de fin recibida ({fecha_fin.isoformat()}) contradice la precision "
            f"'{precision.value}', que deriva el fin en {derivado[1].isoformat()}. "
            f"Con precision '{precision.value}' el fin no se declara: se deriva."
        )
    return derivado


def _derivar_borde_cerrado(
    fecha_inicio: date, precision: PrecisionFecha
) -> tuple[date, date]:
    """Calcula los dos bordes de las cuatro precisiones que no son `RANGO`."""
    if precision is PrecisionFecha.DIA:
        return (fecha_inicio, fecha_inicio)

    if precision is PrecisionFecha.MES:
        ultimo_dia = calendar.monthrange(fecha_inicio.year, fecha_inicio.month)[1]
        return (
            date(fecha_inicio.year, fecha_inicio.month, 1),
            date(fecha_inicio.year, fecha_inicio.month, ultimo_dia),
        )

    if precision is PrecisionFecha.ANIO:
        return (date(fecha_inicio.year, 1, 1), date(fecha_inicio.year, 12, 31))

    if precision is PrecisionFecha.DECADA:
        anio_base = (fecha_inicio.year // 10) * 10
        return (date(anio_base, 1, 1), date(anio_base + 9, 12, 31))

    raise ValueError(f"Precision de fecha desconocida: {precision!r}.")


_MESES = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()


def fecha_legible(inicio: date, fin: date, precision: PrecisionFecha) -> str:
    """La Fecha como la diría una persona: «3 de mayo de 1975», «década de 1970»."""
    if precision is PrecisionFecha.DIA:
        return f"{inicio.day} de {_MESES[inicio.month - 1]} de {inicio.year}"
    if precision is PrecisionFecha.MES:
        return f"{_MESES[inicio.month - 1]} de {inicio.year}"
    if precision is PrecisionFecha.ANIO:
        return str(inicio.year)
    if precision is PrecisionFecha.DECADA:
        return f"década de {inicio.year}"
    return f"entre {inicio.isoformat()} y {fin.isoformat()}"
