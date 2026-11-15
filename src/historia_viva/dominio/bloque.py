"""La regla única de la aprobación en bloque (CU-07; DEC-22, DEC-135, DEC-146 punto 1).

La usan la previa y la confirmación: una Pieza entra en el bloque si es Pendiente, no está
retirada, ningún modelo dejó de leerla («Requiere análisis humano», también todo video), no
tiene alerta de inyección ni Afirmaciones sin decidir (ADR-0007) y, con lo heredado del Lote
más lo que pone el bloque, no le falta ninguno de los nueve obligatorios. En la confirmación,
además, la versión vigente tiene que ser la que la persona vio. Las que no entran siguen
Pendientes con su motivo: nadie las abrió, no pasan a Incompleta.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from historia_viva.dominio.estados import Estado
from historia_viva.dominio.tipos import CAMPOS_OBLIGATORIOS

__all__ = [
    "CAMPOS_DEL_BLOQUE",
    "Candidata",
    "Evaluacion",
    "Motivo",
    "Rechazo",
    "evaluar_aprobacion",
]

CAMPOS_DEL_BLOQUE = ("tipo", "tipo_actividad")
"""Los obligatorios que pone el bloque (DEC-135); el resto viene heredado del Lote o del modelo."""


class Motivo(StrEnum):
    NO_PENDIENTE = "no_pendiente"
    PROCESANDO = "procesando"
    ALERTA_INYECCION = "alerta_inyeccion"
    AFIRMACIONES_SIN_DECIDIR = "afirmaciones_sin_decidir"
    REQUIERE_ANALISIS_HUMANO = "requiere_analisis_humano"
    VERSION_DESACTUALIZADA = "version_desactualizada"
    BORRADA = "borrada"
    FALTA_CAMPO = "falta_campo"


@dataclass(frozen=True)
class Candidata:
    """Lo que la regla mira de una Pieza del Lote, leído con la Pieza bloqueada al confirmar."""

    pieza_id: UUID
    estado: Estado
    version: int | None
    """La de la Ficha vigente; `None` mientras está Recibida."""
    borrada: bool
    alerta_inyeccion: bool
    requiere_analisis_humano: bool
    faltantes: tuple[str, ...]
    """De los nueve obligatorios, los vacíos en la Pieza (un resumen en blanco cuenta)."""
    afirmaciones_sin_decidir: bool = False
    """Si alguna Afirmación de su foto espera que una persona la acepte o la rechace (144)."""


@dataclass(frozen=True)
class Rechazo:
    pieza_id: UUID
    motivo: Motivo
    campo: str | None = None


@dataclass(frozen=True)
class Evaluacion:
    elegibles: tuple[Candidata, ...]
    rechazadas: tuple[Rechazo, ...]


def evaluar_aprobacion(
    candidatas: Iterable[Candidata],
    *,
    versiones_vistas: Mapping[UUID, int | None] | None = None,
) -> Evaluacion:
    """Separa las que se firman de las que no, en el orden recibido. Sin `versiones_vistas` es
    la previa; con ellas, la confirmación, donde `None` es una Pieza que la previa no mostró
    como elegible. Una versión que cambió sale primero, sea cual sea el estado en que quedó
    («Cambió mientras revisabas»); una que la previa no mostró sale con su motivo y, si no
    tiene otro, también como desactualizada: nadie la vio para firmarla."""
    elegibles: list[Candidata] = []
    rechazadas: list[Rechazo] = []
    for candidata in candidatas:
        rechazo = _rechazo(candidata, versiones_vistas)
        if rechazo is None:
            elegibles.append(candidata)
        else:
            rechazadas.append(rechazo)
    return Evaluacion(tuple(elegibles), tuple(rechazadas))


def _rechazo(candidata: Candidata, vistas: Mapping[UUID, int | None] | None) -> Rechazo | None:
    pieza_id = candidata.pieza_id
    vista = None if vistas is None else vistas.get(pieza_id)
    if candidata.borrada:
        return Rechazo(pieza_id, Motivo.BORRADA)
    if vista is not None and vista != candidata.version:
        return Rechazo(pieza_id, Motivo.VERSION_DESACTUALIZADA)
    if candidata.estado is Estado.RECIBIDA:
        return Rechazo(pieza_id, Motivo.PROCESANDO)
    if candidata.estado is not Estado.PENDIENTE:
        return Rechazo(pieza_id, Motivo.NO_PENDIENTE)
    if candidata.requiere_analisis_humano:
        return Rechazo(pieza_id, Motivo.REQUIERE_ANALISIS_HUMANO)
    if candidata.alerta_inyeccion:
        return Rechazo(pieza_id, Motivo.ALERTA_INYECCION)
    if candidata.afirmaciones_sin_decidir:
        return Rechazo(pieza_id, Motivo.AFIRMACIONES_SIN_DECIDIR)
    faltan = [
        c for c in CAMPOS_OBLIGATORIOS if c in candidata.faltantes and c not in CAMPOS_DEL_BLOQUE
    ]
    if faltan:
        return Rechazo(pieza_id, Motivo.FALTA_CAMPO, faltan[0])
    if vistas is not None and vista is None:
        return Rechazo(pieza_id, Motivo.VERSION_DESACTUALIZADA)
    return None
