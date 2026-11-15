"""El Contexto común de un Lote y lo que cada Pieza hereda de él (DEC-18, DEC-19, DEC-126).

La herencia es una copia: el origen, la Fecha, el propietario y las Carreras pasan a la Pieza y a
su Ficha, y el evento y el lugar se precargan en su Contexto. `tipo` y `tipo_actividad` nunca se
heredan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from statistics import median
from typing import Any
from uuid import UUID

from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha

__all__ = [
    "CAMPOS_DE_LA_FECHA",
    "COMPLETABLES",
    "ContextoDeLote",
    "EstadoDeLote",
    "Herencia",
    "estado_de_lote",
    "estimar_espera",
]

CAMPOS_DE_LA_FECHA: tuple[str, ...] = ("fecha_inicio", "fecha_fin", "precision_fecha")
"""La Fecha va entera o no va: sus tres partes se completan juntas (151)."""

COMPLETABLES: tuple[str, ...] = (*CAMPOS_DE_LA_FECHA, "origen", "propietario")
"""Lo que un Lote con Piezas todavía puede completar si lo tiene vacío, y se copia a sus Recibidas
y Pendientes (151, historia 74): nunca se cambia lo que ya tenía."""

_TOPE_DEL_BLOQUE = 5000
"""Caracteres del Contexto que llegan a `resumir` (spec, `ContextoDeLote`)."""


@dataclass(frozen=True, kw_only=True)
class ContextoDeLote:
    """Lo que declara quien crea el Lote. La Fecha llega ya derivada (`derivar_rango`). Un Lote
    creado al arrastrar varios archivos nace sin Origen ni Fecha, que se piden después (151)."""

    origen: str | None = None
    fecha_inicio: date | None = None
    fecha_fin: date | None = None
    precision_fecha: PrecisionFecha | None = None
    evento: str | None = None
    lugar: str | None = None

    def contexto_de_la_ficha(self) -> str | None:
        """Las líneas `Evento:` y `Lugar:` no nulas, para la sección Contexto de la v1."""
        lineas = [
            f"{rotulo}: {valor}"
            for rotulo, valor in (("Evento", self.evento), ("Lugar", self.lugar))
            if valor
        ]
        return "\n".join(lineas) or None

    def como_texto(self) -> str:
        """El contenido del bloque `contexto_lote` que recibe `resumir`, truncado. El
        delimitador lo escapa la capa de modelos, como en todo bloque de datos."""
        fecha = (
            ""
            if self.fecha_inicio is None or self.fecha_fin is None or self.precision_fecha is None
            else f"Fecha: {self.fecha_inicio.isoformat()} a {self.fecha_fin.isoformat()}"
            f" (precisión: {self.precision_fecha.value})"
        )
        origen = "" if self.origen is None else f"Origen: {self.origen}"
        lineas = [origen, fecha, self.contexto_de_la_ficha() or ""]
        return "\n".join(filter(None, lineas))[:_TOPE_DEL_BLOQUE]

    def a_json(self) -> dict[str, Any]:
        return {
            "origen": self.origen,
            "fecha_inicio": None if self.fecha_inicio is None else self.fecha_inicio.isoformat(),
            "fecha_fin": None if self.fecha_fin is None else self.fecha_fin.isoformat(),
            "precision_fecha": None if self.precision_fecha is None else self.precision_fecha.value,
            "evento": self.evento,
            "lugar": self.lugar,
        }

    @classmethod
    def desde_json(cls, datos: dict[str, Any]) -> ContextoDeLote:
        return cls(
            origen=datos["origen"],
            fecha_inicio=_fecha(datos["fecha_inicio"]),
            fecha_fin=_fecha(datos["fecha_fin"]),
            precision_fecha=(
                None if datos["precision_fecha"] is None
                else PrecisionFecha(datos["precision_fecha"])
            ),
            evento=datos["evento"],
            lugar=datos["lugar"],
        )


@dataclass(frozen=True, kw_only=True)
class Herencia:
    """Lo que una Pieza copia de su Lote al nacer."""

    lote_id: UUID
    contexto: ContextoDeLote
    propietario: str | None = None
    carreras: tuple[str, ...] = ()

    @property
    def campos(self) -> tuple[str, ...]:
        """Los `campos_heredados` de la Pieza: lo que el Lote no declaró, la Pieza lo sigue
        pidiendo."""
        return (
            *(("origen",) if self.contexto.origen else ()),
            *(CAMPOS_DE_LA_FECHA if self.contexto.fecha_inicio else ()),
            *(("propietario",) if self.propietario else ()),
            *(("carreras",) if self.carreras else ()),
        )


def _fecha(valor: str | None) -> date | None:
    return None if valor is None else date.fromisoformat(valor)


class EstadoDeLote(StrEnum):
    """El estado del Lote: derivado de sus Piezas, nunca guardado (DEC-18)."""

    VACIO = "Vacío"
    PROCESANDO = "Procesando"
    EN_VALIDACION = "En validación"
    VALIDADO = "Validado"


def estado_de_lote(conteos: Mapping[Estado, int]) -> EstadoDeLote:
    """Sobre las Piezas no retiradas (spec §5.3). Una Recibida fallida sigue contando como
    Recibida: el Lote queda Procesando hasta que se reintenta o se completa a mano (DEC-122)."""
    if not any(conteos.values()):
        return EstadoDeLote.VACIO
    if conteos.get(Estado.RECIBIDA):
        return EstadoDeLote.PROCESANDO
    if conteos.get(Estado.PENDIENTE) or conteos.get(Estado.INCOMPLETA):
        return EstadoDeLote.EN_VALIDACION
    return EstadoDeLote.VALIDADO


def estimar_espera(duraciones_ms: Sequence[int], pendientes: int) -> timedelta | None:
    """La espera del Lote: la mediana de los procesamientos terminados por sus Recibidas que
    faltan procesar (DEC-25, RF-44). `None` sin historial o sin nada que esperar."""
    if not duraciones_ms or pendientes <= 0:
        return None
    return timedelta(milliseconds=median(duraciones_ms) * pendientes)
