"""`/api/conversaciones`: la Conversación por HTTP (spec, módulo 31; Contrato HTTP §1.8).

El turno responde `text/event-stream`. Lo que falla antes de abrir el flujo (filtro fuera de
catálogo, Conversación ajena, tope de preguntas, índice o base caídos) es un RFC 9457 normal;
después, los eventos. El navegador lo pide con `fetch` y no con `EventSource`, que no manda `POST` ni `X-HV`:
el CSRF es el de toda mutación.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import unicodedata
from collections.abc import AsyncGenerator
from datetime import date, datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints
from starlette.types import Receive, Scope, Send

from historia_viva.api.efemerides import relato_http
from historia_viva.api.filtros import armar_filtro, validar_filtro
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.conversacion import (
    TOPES,
    Conversaciones,
    ConversacionRef,
    EventoDeConversacion,
)
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal

__all__ = ["PING_S", "como_sse", "flujo_sse", "respuesta_sse", "router"]

router = APIRouter(prefix="/api", tags=["Conversación"])

Consultar = Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))]

PING_S = 15.0
"""Un comentario `: ping` cada 15 s mantiene viva la conexión mientras no hay eventos (§1.8)."""


def _normalizar(texto: str) -> str:
    """UTF-8 NFC y sin caracteres de control salvo el salto de línea (R-36): un NUL no llega a
    Postgres."""
    limpio = "".join(c for c in texto if c == "\n" or unicodedata.category(c) != "Cc")
    return unicodedata.normalize("NFC", limpio).strip()


class FiltrosDelTurno(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desde: date | None = None
    hasta: date | None = None
    tipo: list[str] = []
    tipo_actividad: list[str] = []
    carrera: list[str] = []


class PedidoMensaje(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: Annotated[
        str, AfterValidator(_normalizar), StringConstraints(min_length=3, max_length=TOPES.largo_pregunta)
    ]
    filtros: FiltrosDelTurno = FiltrosDelTurno()


def _conversaciones(request: Request) -> Conversaciones:
    conversaciones: Conversaciones = request.app.state.conversaciones
    return conversaciones


@router.post("/conversaciones", status_code=201)
def iniciar(
    request: Request,
    response: Response,
    principal: Consultar,
    conversaciones: Annotated[Conversaciones, Depends(_conversaciones)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        ref = conversaciones.iniciar(tx, principal=principal)
    response.headers["Location"] = f"/api/conversaciones/{ref.conversacion_id}"
    return _resumen(ref)


@router.get("/conversaciones")
def listar(
    request: Request,
    principal: Consultar,
    conversaciones: Annotated[Conversaciones, Depends(_conversaciones)],
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Las mías, la más reciente primero; `siguiente` es `null` en la última página."""
    with request.app.state.bd.transaccion() as tx:
        # Una de más para saber si hay otra página sin contar.
        refs = conversaciones.listar(
            tx,
            persona_id=principal.usuario_id,
            limite=limite + 1,
            antes_de=None if cursor is None else _desde_cursor(cursor),
        )
    return {
        "conversaciones": [_resumen(ref) for ref in refs[:limite]],
        "siguiente": _cursor(refs[limite - 1]) if len(refs) > limite else None,
    }


@router.get("/conversaciones/{conversacion_id}")
def obtener(
    conversacion_id: UUID,
    principal: Consultar,
    conversaciones: Annotated[Conversaciones, Depends(_conversaciones)],
) -> dict[str, Any]:
    vista = conversaciones.obtener(conversacion_id, principal=principal)
    resumen = _resumen(vista.conversacion)
    del resumen["mensajes"]
    return {**resumen, "mensajes": jsonable_encoder(vista.mensajes)}


def _resumen(ref: ConversacionRef) -> dict[str, Any]:
    return {
        "conversacion_id": str(ref.conversacion_id),
        "titulo": ref.titulo,
        "mensajes": ref.mensajes,
        "creada_el": ref.creada_el.isoformat(),
        "actualizada_el": ref.actualizada_el.isoformat(),
    }


def _cursor(ref: ConversacionRef) -> str:
    """Opaco: la última de la página (`actualizada_el`, `conversacion_id`)."""
    clave = f"{ref.actualizada_el.isoformat()}|{ref.conversacion_id}"
    return base64.urlsafe_b64encode(clave.encode()).decode()


def _desde_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        cuando, conversacion_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(cuando), UUID(conversacion_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="El cursor no es válido: usá el `siguiente` de la página anterior.",
            campo="cursor",
        ) from error


@router.post("/conversaciones/{conversacion_id}/mensajes")
def enviar_mensaje(
    conversacion_id: UUID,
    pedido: PedidoMensaje,
    request: Request,
    principal: Consultar,
    conversaciones: Annotated[Conversaciones, Depends(_conversaciones)],
) -> StreamingResponse:
    filtros = pedido.filtros
    filtro = armar_filtro(
        desde=filtros.desde,
        hasta=filtros.hasta,
        tipo=filtros.tipo,
        tipo_actividad=filtros.tipo_actividad,
        carrera=filtros.carrera,
    )
    filtro.validar()  # un NUL no llega a los catálogos
    validar_filtro(request.app.state.bd, filtro)
    eventos = conversaciones.enviar_mensaje(
        conversacion_id, principal=principal, texto=pedido.texto, filtro=filtro
    )
    return respuesta_sse(eventos)


@router.post("/conversaciones/{conversacion_id}/mensajes/{mensaje_id}/reintentar")
def reintentar(
    conversacion_id: UUID,
    mensaje_id: UUID,
    principal: Consultar,
    conversaciones: Annotated[Conversaciones, Depends(_conversaciones)],
) -> StreamingResponse:
    """«Reintentar este turno»: la misma pregunta, una respuesta nueva (E3)."""
    return respuesta_sse(
        conversaciones.reintentar(conversacion_id, mensaje_id, principal=principal)
    )


@router.post("/conversaciones/{conversacion_id}/mensajes/{mensaje_id}/relato", status_code=201)
def proponer_relato(
    conversacion_id: UUID,
    mensaje_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(exigir(Permiso.REVISAR_EFEMERIDES))],
    conversaciones: Annotated[Conversaciones, Depends(_conversaciones)],
) -> dict[str, Any]:
    """«Proponer como relato» (DEC-147, R-42): sin cuerpo; el título es la pregunta del turno."""
    with request.app.state.bd.transaccion() as tx:
        vista = conversaciones.proponer_relato(
            tx, conversacion_id, mensaje_id, principal=principal
        )
    response.headers["Location"] = f"/api/relatos/{vista.relato_id}"
    return relato_http(vista)


class _Eventos(Protocol):
    def __next__(self) -> EventoDeConversacion: ...
    def close(self) -> None: ...


def respuesta_sse(eventos: _Eventos) -> StreamingResponse:
    return _RespuestaSSE(eventos)


class _RespuestaSSE(StreamingResponse):
    """Cierra el turno al terminar la respuesta, también si el cliente se fue antes de que el
    flujo arrancara: Starlette no cierra el `body_iterator`."""

    def __init__(self, eventos: _Eventos) -> None:
        self._flujo = flujo_sse(eventos)
        super().__init__(
            self._flujo,
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
        self._eventos = eventos

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._flujo.aclose()  # corre su `finally` si llegó a arrancar
            await _cerrar(self._eventos)


async def _cerrar(eventos: _Eventos) -> None:
    # Protegido: con el pedido cancelado, cualquier `await` sin escudo vuelve a cancelarse.
    with anyio.CancelScope(shield=True):
        await anyio.to_thread.run_sync(eventos.close)


async def flujo_sse(eventos: _Eventos, *, ping_s: float = PING_S) -> AsyncGenerator[str]:
    """Los eventos del turno como `text/event-stream`, con `: ping` si pasan `ping_s` sin uno.
    El turno corre en un hilo: guarda en la base y, desde 052, espera al modelo. Al salir, por
    lo que sea, cierra el turno."""
    terminado = object()
    siguiente = asyncio.ensure_future(asyncio.to_thread(next, eventos, terminado))
    try:
        while True:
            try:
                evento = await asyncio.wait_for(asyncio.shield(siguiente), ping_s)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if evento is terminado:
                return
            assert isinstance(evento, EventoDeConversacion)
            yield como_sse(evento)
            siguiente = asyncio.ensure_future(asyncio.to_thread(next, eventos, terminado))
    finally:
        # Un generador no se cierra mientras corre su `next` en otro hilo.
        with anyio.CancelScope(shield=True):
            await asyncio.wait([siguiente])
        await _cerrar(eventos)


def como_sse(evento: EventoDeConversacion) -> str:
    """Un evento de `text/event-stream`: el JSON en una sola línea (Contrato HTTP §1.8)."""
    datos = json.dumps(jsonable_encoder(evento.datos), ensure_ascii=False)
    return f"event: {evento.tipo}\ndata: {datos}\n\n"
