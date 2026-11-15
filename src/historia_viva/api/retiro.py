"""Retirar una Pieza con motivo (105), la lista de las Retiradas y restaurarlas (106; spec,
módulo 18).

La regla es de `retiro`; acá solo se traduce a HTTP. El permiso va a nivel de router: RGC y,
durante el interinato, el Administrador técnico (DEC-124). Sin verbo `DELETE` (DEC-29, N16).
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.dominio.errores import LoteLleno, TransicionInvalida
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal
from historia_viva.retiro import Retirada, Retiro

__all__ = ["router"]

router = APIRouter(prefix="/api/admin", tags=["Administración"])


class PedidoRetiro(BaseModel):
    """El motivo del retiro y de la restauración (`MotivoDeRetiro` del openapi)."""

    model_config = ConfigDict(extra="forbid")

    motivo: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)
    ]


@router.post("/piezas/{pieza_id}/retiro")
def retirar_pieza(
    pieza_id: UUID,
    pedido: PedidoRetiro,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.RETIRAR_PIEZAS))],
) -> dict[str, Any]:
    """Bloquea, desindexa, cancela los trabajos `encolado` y `en_curso`, marca y audita
    `pieza.retiro`, en una transacción. 404 `pieza-no-encontrada`; 409 `transicion-invalida`
    si ya está retirada (R-43 d); 422 `pedido-invalido` con `campo: motivo`."""
    retiro: Retiro = request.app.state.retiro
    try:
        retirada = retiro.retirar_pieza(
            pieza_id=pieza_id, principal=principal, motivo=pedido.motivo
        )
    except TransicionInvalida as error:
        raise Problema(
            tipo="/errores/transicion-invalida",
            titulo="La Pieza ya está retirada",
            estado=409,
            detalle=str(error),
        ) from error
    return {
        "pieza_id": str(retirada.pieza_id),
        "borrada_el": momento(retirada.borrada_el),
        "borrada_por": str(retirada.borrada_por),
    }


@router.post("/piezas/{pieza_id}/restauracion")
def restaurar_pieza(
    pieza_id: UUID,
    pedido: PedidoRetiro,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.RETIRAR_PIEZAS))],
) -> dict[str, Any]:
    """Quita la marca, reencola la indexación de una Validada o un procesamiento nuevo de una
    Recibida y audita `pieza.restauracion` (DEC-138). 404 `pieza-no-encontrada`; 409
    `transicion-invalida` si no está retirada (R-43 d), `pieza-duplicada` con el `pieza_id` de
    la existente o `lote-lleno`; 422 `pedido-invalido` con `campo: motivo`."""
    retiro: Retiro = request.app.state.retiro
    try:
        restaurada = retiro.restaurar_pieza(
            pieza_id=pieza_id, principal=principal, motivo=pedido.motivo
        )
    except TransicionInvalida as error:
        raise Problema(
            tipo="/errores/transicion-invalida",
            titulo="La Pieza no está retirada",
            estado=409,
            detalle=str(error),
        ) from error
    except LoteLleno as error:
        # Al cargar es 422; al restaurar, 409 (R-27).
        raise Problema(
            tipo="/errores/lote-lleno",
            titulo="El Lote llegó a su tope",
            estado=409,
            detalle=f"El Lote de la Pieza ya tiene {error.tope} Piezas: no se puede restaurar.",
            extensiones={"tope": error.tope},
        ) from error
    return {
        "pieza_id": str(restaurada.pieza_id),
        "restaurada_el": momento(restaurada.restaurada_el),
        "indexacion_encolada": restaurada.indexacion_encolada,
        "procesamiento_encolado": restaurada.procesamiento_encolado,
    }


@router.get("/retiradas")
def listar_retiradas(
    request: Request,
    _: Annotated[Principal, Depends(exigir(Permiso.RETIRAR_PIEZAS))],
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """P7e: de la más reciente retirada a la más antigua, con `conflicto_de_restauracion` para
    apagar «Restaurar». `siguiente` es `null` al final."""
    retiro: Retiro = request.app.state.retiro
    with request.app.state.bd.transaccion() as tx:
        # Una de más para saber si hay otra página sin contar.
        pagina = retiro.retiradas(
            tx, despues_de=None if cursor is None else _despues_de(cursor), limite=limite + 1
        )
    return {
        "retiradas": [_fila(retirada) for retirada in pagina[:limite]],
        "siguiente": _cursor(pagina[limite - 1]) if len(pagina) > limite else None,
    }


def _fila(retirada: Retirada) -> dict[str, Any]:
    fila = retirada.fila
    return {
        "pieza_id": str(fila.pieza_id),
        "nombre_archivo": fila.nombre_archivo,
        "estado": fila.estado.value,
        "tipo": fila.tipo,
        "lote_id": None if fila.lote_id is None else str(fila.lote_id),
        "lote_nombre": retirada.lote_nombre,
        "retirada_el": momento(fila.borrada_el),
        "retirada_por": fila.retirada_por,
        "motivo": fila.motivo,
        "conflicto_de_restauracion": retirada.conflicto_de_restauracion,
    }


def _cursor(retirada: Retirada) -> str:
    clave = f"{retirada.fila.borrada_el.isoformat()}|{retirada.fila.pieza_id}"
    return base64.urlsafe_b64encode(clave.encode()).decode()


def _despues_de(cursor: str) -> tuple[datetime, UUID]:
    try:
        borrada_el, pieza_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(borrada_el), UUID(pieza_id)
    except ValueError as error:  # también `binascii.Error` y `UnicodeDecodeError`
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="El cursor no es válido: usá el `siguiente` de la página anterior.",
            campo="cursor",
        ) from error
