"""`GET` y `PUT /api/admin/interruptor`: el panel de los dos frenos (spec, tabla de endpoints;
DEC-88, DEC-136).

Cambiar es nombrar los dos frenos con su valor: `interruptor.cambiar` deja un evento solo por el
que cambia, así que la API de Agentes se enciende únicamente si el técnico la pone en `true`.
«Frenar todo» es mandar los dos en `false`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.bd import Bd
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal
from historia_viva.interruptor import (
    EventoInterruptor,
    Freno,
    MotivoRequerido,
    cambiar,
    habilitado,
    historial,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/admin", tags=["Administración"])

_OPERAR_SISTEMA = exigir(Permiso.OPERAR_SISTEMA)


class PedidoInterruptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    procesamiento_habilitado: bool
    api_agentes_habilitada: bool
    motivo: str = Field(default="", max_length=500)


def _evento(evento: EventoInterruptor) -> dict[str, Any]:
    return {
        "evento_id": evento.evento_id,
        "clave": evento.clave.value,
        "habilitado": evento.habilitado,
        "persona": evento.actor.etiqueta,
        "motivo": evento.motivo,
        "en": momento(evento.en),
    }


def _panel(bd: Bd, limite: int) -> dict[str, Any]:
    with bd.transaccion() as tx:
        eventos = historial(tx, limite)
        procesamiento = habilitado(tx, Freno.PROCESAMIENTO)
        api_agentes = habilitado(tx, Freno.API_AGENTES)
    ultimo = eventos[0] if eventos else None
    return {
        "procesamiento_habilitado": procesamiento,
        "api_agentes_habilitada": api_agentes,
        "cambiado_por": ultimo and ultimo.actor.etiqueta,
        "cambiado_el": ultimo and momento(ultimo.en),
        "motivo": ultimo and ultimo.motivo,
        "historial": [_evento(e) for e in eventos],
    }


@router.get("/interruptor", dependencies=[Depends(_OPERAR_SISTEMA)])
def consultar_interruptor(
    request: Request, limite: Annotated[int, Query(ge=1, le=200)] = 20
) -> dict[str, Any]:
    """El estado vigente de los dos frenos y sus últimos `limite` eventos, del más nuevo al más
    viejo. `cambiado_*` y `motivo` son los del último evento."""
    return _panel(request.app.state.bd, limite)


@router.put("/interruptor")
def cambiar_interruptor(
    pedido: PedidoInterruptor,
    request: Request,
    principal: Annotated[Principal, Depends(_OPERAR_SISTEMA)],
) -> dict[str, Any]:
    """Detener exige un motivo (422 `pedido-invalido`); devuelve el estado nuevo."""
    bd: Bd = request.app.state.bd
    try:
        with bd.transaccion() as tx:
            cambiar(
                tx,
                por=principal.actor,
                cambios={
                    Freno.PROCESAMIENTO: pedido.procesamiento_habilitado,
                    Freno.API_AGENTES: pedido.api_agentes_habilitada,
                },
                motivo=pedido.motivo,
            )
    except MotivoRequerido as error:
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle=str(error),
            campo="motivo",
        ) from error
    return _panel(bd, 20)
