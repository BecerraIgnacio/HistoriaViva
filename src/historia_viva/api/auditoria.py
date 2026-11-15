"""`GET /api/admin/auditoria`: los eventos de `auditoria` para quien opera el sistema (spec,
tabla de endpoints; DEC-40).

El cursor es opaco (keyset sobre el `id` del evento): la pantalla lo devuelve tal cual.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.auditoria import Accion, Evento, ObjetoRef, RegistroDeAuditoria
from historia_viva.dominio.permisos import Permiso

__all__ = ["router"]

router = APIRouter(
    prefix="/api/admin",
    tags=["Administración"],
    dependencies=[Depends(exigir(Permiso.OPERAR_SISTEMA))],
)


def _cursor(evento: Evento) -> str:
    return base64.urlsafe_b64encode(str(evento.id).encode()).decode()


def _antes_de(cursor: str) -> int:
    try:
        antes_de = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError):
        antes_de = 0
    if not 0 < antes_de < 2**63:  # el `id` es `bigint`
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="El cursor no es válido: usá el `siguiente` de la página anterior.",
            campo="cursor",
        )
    return antes_de


def _evento(evento: Evento) -> dict[str, Any]:
    return {
        "evento_id": evento.id,
        "fecha": momento(evento.cuando),
        "persona": evento.actor.etiqueta,
        "accion": evento.accion.value,
        "objeto_tipo": evento.objeto.tipo,
        "objeto_id": evento.objeto.id,
        "detalle": {"antes": evento.antes, "despues": evento.despues, "motivo": evento.motivo},
    }


@router.get("/auditoria")
def listar_auditoria(
    request: Request,
    objeto_tipo: str | None = None,
    objeto_id: str | None = None,
    accion: Accion | None = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Del más nuevo al más viejo; `siguiente` es `null` en la última página."""
    registro: RegistroDeAuditoria = request.app.state.auditoria
    objeto = None
    if objeto_tipo is not None or objeto_id is not None:
        # `listar` filtra por cada uno aparte: un `objeto_id` sin tipo también vale.
        objeto = ObjetoRef(objeto_tipo, objeto_id)  # type: ignore[arg-type]
    # Uno de más para saber si hay otra página sin contar.
    eventos = registro.listar(
        objeto=objeto,
        accion=accion,
        limite=limite + 1,
        antes_de=None if cursor is None else _antes_de(cursor),
    )
    return {
        "eventos": [_evento(e) for e in eventos[:limite]],
        "siguiente": _cursor(eventos[limite - 1]) if len(eventos) > limite else None,
    }
