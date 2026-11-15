"""`/api/admin/personas`: el padrón desde la pantalla (spec, tabla de endpoints; P7b, RF-27,
CU-16, DEC-113, DEC-124).

Las reglas son de `identidad.Padron` (R-01, R-02, R-03); acá solo se traducen a HTTP. Después
del primer RGC nadie tiene a la vez GESTIONAR_PERSONAS y el rol `admin_tecnico`, así que
`admin_tecnico` se otorga y se quita solo por el comando `alta-persona` (decisión del 111).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from historia_viva.api.mediciones import momento
from historia_viva.api.sesion import exigir
from historia_viva.bd import Bd
from historia_viva.dominio.permisos import Permiso, Rol
from historia_viva.identidad import Padron, PersonaVista, Principal

__all__ = ["router"]

router = APIRouter(prefix="/api/admin/personas", tags=["Administración"])

_GESTIONAR_PERSONAS = exigir(Permiso.GESTIONAR_PERSONAS)
ConGestion = Annotated[Principal, Depends(_GESTIONAR_PERSONAS)]


class PedidoAlta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    nombre: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    rol: Rol


class PedidoCambio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rol: Rol | None = None
    activo: bool | None = None

    @model_validator(mode="after")
    def _exige_un_cambio(self) -> PedidoCambio:
        if self.rol is None and self.activo is None:
            raise ValueError("Falta qué cambiar: rol o activo.")
        return self


def _persona(persona: PersonaVista) -> dict[str, Any]:
    return {
        "usuario_id": str(persona.usuario_id),
        "email": persona.email,
        "nombre": persona.nombre,
        "rol": persona.rol.value,
        "activo": persona.activo,
        "ultimo_acceso_el": (
            None if persona.ultimo_acceso_el is None else momento(persona.ultimo_acceso_el)
        ),
        "vinculada": persona.vinculada,
    }


@router.get("", dependencies=[Depends(_GESTIONAR_PERSONAS)])
def listar_personas(
    request: Request, activo: bool | None = None, rol: Rol | None = None
) -> dict[str, Any]:
    bd: Bd = request.app.state.bd
    padron: Padron = request.app.state.padron
    with bd.transaccion() as tx:
        return {"personas": [_persona(p) for p in padron.listar(tx, activo=activo, rol=rol)]}


@router.post("", status_code=201)
def dar_de_alta_persona(
    pedido: PedidoAlta, request: Request, principal: ConGestion
) -> dict[str, Any]:
    """`persona-existente` 409, `dominio-no-permitido` 422; un RGC que otorga `admin_tecnico`,
    403 (R-02). El alta del primer RGC termina el interinato (R-01)."""
    bd: Bd = request.app.state.bd
    padron: Padron = request.app.state.padron
    with bd.transaccion() as tx:
        persona = padron.alta(tx, pedido.email, pedido.nombre, pedido.rol, por=principal)
    return _persona(persona)


@router.patch("/{usuario_id}")
def cambiar_persona(
    usuario_id: UUID, pedido: PedidoCambio, request: Request, principal: ConGestion
) -> dict[str, Any]:
    """Cambia el rol, da de baja (revoca sus sesiones: el pedido siguiente da 401) o reactiva.
    `ultimo-titular-de-permiso` 409 (R-03), `recurso-no-encontrado` 404."""
    bd: Bd = request.app.state.bd
    padron: Padron = request.app.state.padron
    with bd.transaccion() as tx:
        if pedido.rol is not None:
            padron.cambiar_rol(tx, usuario_id, pedido.rol, por=principal)
        if pedido.activo is False:
            padron.desactivar(tx, usuario_id, por=principal)
        elif pedido.activo:
            padron.reactivar(tx, usuario_id, por=principal)
        persona = padron.obtener(tx, usuario_id)
    return _persona(persona)
