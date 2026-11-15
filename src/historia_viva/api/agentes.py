"""`/api/admin/agentes`: los tokens de los Agentes externos desde la pantalla (spec, tabla de
endpoints; P7c, RF-26, CU-16, DEC-32).

El token en claro sale solo en la respuesta que lo emite, con `no-store` (lo pone
`MiddlewareCabeceras` en toda respuesta, 039); la lista y la
auditoría llevan el prefijo. Emitir no toca el freno `api_agentes_habilitada` (DEC-136).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from historia_viva.agentes import (
    AgenteExistente,
    AgenteListado,
    Credenciales,
    Scope,
    TokenEmitido,
    TokenInexistente,
    TokenListado,
    TokenYaRevocado,
)
from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.bd import Bd
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal

__all__ = ["router"]


router = APIRouter(prefix="/api/admin/agentes", tags=["Administración"])

_OPERAR_SISTEMA = exigir(Permiso.OPERAR_SISTEMA)


class PedidoAlta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Sin espacios a los costados: «x » no es otro agente que «x», y «   » no es un nombre.
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    alcances: list[Scope] = Field(min_length=1)


def _momento_o_nulo(marca: datetime | None) -> str | None:
    return None if marca is None else momento(marca)


def _token(token: TokenListado) -> dict[str, Any]:
    return {
        "token_id": token.token_id,
        "prefijo": token.prefijo,
        "estado": token.estado.value,
        "creado_el": momento(token.creado_el),
        "vence_el": _momento_o_nulo(token.vence_el),
        "ultimo_uso_el": _momento_o_nulo(token.ultimo_uso_el),
        "revocado_el": _momento_o_nulo(token.revocado_el),
    }


def _agente(agente: AgenteListado) -> dict[str, Any]:
    return {
        "agente_id": agente.agente_id,
        "nombre": agente.nombre,
        "alcances": [scope.value for scope in agente.scopes],
        "tokens": [_token(t) for t in agente.tokens],
    }


def _emitido(nombre: str, alcances: list[str], emitido: TokenEmitido) -> dict[str, Any]:
    return {
        "agente_id": emitido.agente_id,
        "nombre": nombre,
        "alcances": alcances,
        "token_id": emitido.token_id,
        "token": emitido.token,
        "vence_el": _momento_o_nulo(emitido.vence_el),
    }


@router.get("", dependencies=[Depends(_OPERAR_SISTEMA)])
def listar_agentes(request: Request) -> dict[str, Any]:
    """Los agentes con sus tokens: prefijo, estado y fechas; nunca el token."""
    bd: Bd = request.app.state.bd
    credenciales: Credenciales = request.app.state.credenciales
    with bd.transaccion() as tx:
        return {"agentes": [_agente(a) for a in credenciales.listar(tx)]}


@router.post("", status_code=201)
def dar_de_alta_agente(
    pedido: PedidoAlta,
    request: Request,
    principal: Annotated[Principal, Depends(_OPERAR_SISTEMA)],
) -> dict[str, Any]:
    """Da de alta el agente y emite su primer token: la única respuesta que lo trae."""
    bd: Bd = request.app.state.bd
    credenciales: Credenciales = request.app.state.credenciales
    alcances = sorted({scope.value for scope in pedido.alcances})
    try:
        with bd.transaccion() as tx:
            if credenciales.agente_id(tx, pedido.nombre) is not None:
                raise AgenteExistente(pedido.nombre)
            agente_id = credenciales.crear_agente(
                tx, nombre=pedido.nombre, scopes=frozenset(pedido.alcances), por=principal.actor
            )
            emitido = credenciales.emitir(tx, agente_id=agente_id, por=principal.actor)
    except AgenteExistente as error:
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle=f"{error} Para darle otro token, rotalo.",
            campo="nombre",
        ) from error
    return _emitido(pedido.nombre, alcances, emitido)


class PedidoRotacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vencen_anteriores_el: AwareDatetime | None = None


def _no_encontrado(detalle: str) -> Problema:
    return Problema(
        tipo="/errores/recurso-no-encontrado",
        titulo="No existe el recurso",
        estado=404,
        detalle=detalle,
    )


@router.post("/{agente_id}/tokens", status_code=201)
def emitir_otro_token(
    agente_id: UUID,
    pedido: PedidoRotacion,
    request: Request,
    principal: Annotated[Principal, Depends(_OPERAR_SISTEMA)],
) -> dict[str, Any]:
    """Rotar: un token nuevo, que vence a los 180 días. Con `vencen_anteriores_el`, los vigentes
    del agente valen hasta esa fecha (el solape, a lo sumo 7 días); sin ella, siguen valiendo."""
    bd: Bd = request.app.state.bd
    credenciales: Credenciales = request.app.state.credenciales
    with bd.transaccion() as tx:
        # shortcut: busca en la lista entera; alcanza para los pocos agentes del MVP.
        agente = next((a for a in credenciales.listar(tx) if a.agente_id == agente_id), None)
        if agente is None:
            raise _no_encontrado(f"No existe el Agente {agente_id}.")
        if pedido.vencen_anteriores_el is None:
            emitido = credenciales.emitir(tx, agente_id=agente_id, por=principal.actor)
        else:
            try:
                emitido = credenciales.rotar(
                    tx,
                    agente_id=agente_id,
                    vencen_anteriores_el=pedido.vencen_anteriores_el,
                    por=principal.actor,
                )
            except ValueError as error:
                raise Problema(
                    tipo="/errores/pedido-invalido",
                    titulo="El pedido es inválido",
                    estado=422,
                    detalle=str(error),
                    campo="vencen_anteriores_el",
                ) from error
    return _emitido(agente.nombre, [scope.value for scope in agente.scopes], emitido)


@router.post("/{agente_id}/tokens/{token_id}/revocacion")
def revocar_token(
    agente_id: UUID,
    token_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(_OPERAR_SISTEMA)],
) -> dict[str, Any]:
    """Revoca ya: el pedido siguiente a `/v1` con ese token da 401. Los demás no se afectan."""
    bd: Bd = request.app.state.bd
    credenciales: Credenciales = request.app.state.credenciales
    try:
        with bd.transaccion() as tx:
            revocado_el = credenciales.revocar_token(
                tx, token_id, por=principal.actor, agente_id=agente_id
            )
    except TokenYaRevocado as error:
        raise Problema(
            tipo="/errores/transicion-invalida",
            titulo="El token ya está revocado",
            estado=409,
            detalle=f"El token {token_id} ya está revocado.",
        ) from error
    except TokenInexistente as error:
        raise _no_encontrado(f"El Agente {agente_id} no tiene un token {token_id}.") from error
    return {"token_id": token_id, "revocado_el": momento(revocado_el)}
