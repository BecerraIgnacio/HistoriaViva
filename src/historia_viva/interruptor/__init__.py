"""El interruptor global de ADR-0004 (control 4): dos frenos y «Frenar todo» (DEC-88, DEC-136).

El estado de cada freno es el último evento de `acervo.evento_interruptor`, que solo se agrega y
es a la vez su auditoría. Se lee sin caché, por pedido, y **falla cerrado**: sin eventos o con un
error de lectura, el freno está «detenido». Es la única puerta a la tabla.

`procesamiento_habilitado` frena toda la cola (el trabajador lo consulta antes de cada toma, por
el `Callable` que arma `composicion`); subir y validar siguen. `api_agentes_habilitada` corta `/v1`
y solo la enciende un cambio explícito de esa clave (DEC-136).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.bd import BaseNoDisponible, Bd
from historia_viva.dominio.errores import ErrorHistoriaViva

__all__ = [
    "MOTIVO_MINIMO",
    "EventoInterruptor",
    "Freno",
    "MotivoRequerido",
    "ProcesamientoDetenido",
    "cambiar",
    "estado",
    "frenar_todo",
    "habilitado",
    "habilitado_en",
    "historial",
]

MOTIVO_MINIMO = 5
"""Caracteres que pide detener un freno (sin contar los espacios de los bordes)."""

_log = logging.getLogger(__name__)


class Freno(StrEnum):
    PROCESAMIENTO = "procesamiento_habilitado"
    API_AGENTES = "api_agentes_habilitada"


class MotivoRequerido(ErrorHistoriaViva):
    """Detener un freno exige decir por qué."""

    def __init__(self) -> None:
        super().__init__(f"Para detener hay que dar un motivo de {MOTIVO_MINIMO} caracteres o más.")


class ProcesamientoDetenido(ErrorHistoriaViva):
    """Lo que el freno `procesamiento_habilitado` niega en vez de encolar: `reindexar` (RF-30)."""

    def __init__(self) -> None:
        super().__init__(
            "El procesamiento está detenido: reanudalo con `interruptor reanudar"
            " procesamiento_habilitado` y volvé a correr el comando."
        )


@dataclass(frozen=True)
class EventoInterruptor:
    evento_id: int
    clave: Freno
    habilitado: bool
    motivo: str
    actor: Actor
    en: datetime


def habilitado(conn: Connection[Any], freno: Freno) -> bool:
    """Si el freno deja andar. Falla cerrado: sin eventos o ante un error de lectura, no.

    Lee en un punto de guardado: si falla, la transacción de quien llama sigue sana.
    """
    try:
        with conn.transaction():
            fila = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    "SELECT habilitado FROM acervo.evento_interruptor WHERE clave = %s"
                    " ORDER BY evento_id DESC LIMIT 1",
                    (freno,),
                )
                .fetchone()
            )
    except psycopg.Error:
        _log.warning("No se pudo leer el freno %s: queda detenido.", freno, exc_info=True)
        return False
    return fila is not None and fila["habilitado"] is True


def habilitado_en(bd: Bd, freno: Freno) -> bool:
    """`habilitado` en una transacción propia; con la base caída o la confirmación rota, también
    detenido."""
    try:
        with bd.transaccion() as tx:
            return habilitado(tx, freno)
    except (BaseNoDisponible, psycopg.Error):
        _log.warning("No se pudo leer el freno %s: queda detenido.", freno, exc_info=True)
        return False


def estado(conn: Connection[Any]) -> dict[Freno, EventoInterruptor]:
    """El último evento de cada freno: su estado vigente, desde cuándo, quién y por qué."""
    filas = (
        conn.cursor(row_factory=dict_row)
        .execute(
            "SELECT DISTINCT ON (clave) * FROM acervo.evento_interruptor"
            " ORDER BY clave, evento_id DESC"
        )
        .fetchall()
    )
    return {Freno(fila["clave"]): _evento(fila) for fila in filas}


def historial(conn: Connection[Any], limite: int = 20) -> list[EventoInterruptor]:
    """Los últimos `limite` eventos de los dos frenos, del más nuevo al más viejo."""
    filas = (
        conn.cursor(row_factory=dict_row)
        .execute(
            "SELECT * FROM acervo.evento_interruptor ORDER BY evento_id DESC LIMIT %s", (limite,)
        )
        .fetchall()
    )
    return [_evento(fila) for fila in filas]


def cambiar(
    tx: Connection[Any], *, por: Actor, cambios: Mapping[Freno, bool], motivo: str
) -> list[EventoInterruptor]:
    """Deja un evento por cada freno que cambia de valor; lo que ya está así no deja ninguno.

    Encender `api_agentes_habilitada` es nombrarla acá: nada la enciende de rebote (DEC-136).

    Raises:
        MotivoRequerido: Si algún cambio detiene y el motivo tiene menos de `MOTIVO_MINIMO`
            caracteres.
    """
    vigente = estado(tx)
    # Un freno que ya está así no cuenta: reanudar uno con el otro detenido no pide motivo.
    cambios = {
        freno: valor
        for freno, valor in cambios.items()
        if freno not in vigente or vigente[freno].habilitado != valor
    }
    if not all(cambios.values()) and len(motivo.strip()) < MOTIVO_MINIMO:
        raise MotivoRequerido
    eventos = []
    for freno, valor in cambios.items():
        fila = (
            tx.cursor(row_factory=dict_row)
            .execute(
                "INSERT INTO acervo.evento_interruptor"
                " (clave, habilitado, motivo, actor_tipo, actor_etiqueta, usuario_id)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (freno, valor, motivo.strip(), por.tipo, por.etiqueta, por.usuario_id),
            )
            .fetchone()
        )
        assert fila is not None
        eventos.append(_evento(fila))
    return eventos


def frenar_todo(tx: Connection[Any], *, por: Actor, motivo: str) -> list[EventoInterruptor]:
    """«Frenar todo»: detiene el procesamiento y la API de Agentes (DEC-136)."""
    return cambiar(tx, por=por, cambios=dict.fromkeys(Freno, False), motivo=motivo)


def _evento(fila: dict[str, Any]) -> EventoInterruptor:
    return EventoInterruptor(
        evento_id=fila["evento_id"],
        clave=Freno(fila["clave"]),
        habilitado=fila["habilitado"],
        motivo=fila["motivo"],
        actor=Actor(
            TipoDeActor(fila["actor_tipo"]), fila["actor_etiqueta"], usuario_id=fila["usuario_id"]
        ),
        en=fila["en"],
    )
