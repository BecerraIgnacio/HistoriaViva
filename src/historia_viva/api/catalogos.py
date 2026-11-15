"""Las listas de la Ficha, para los desplegables y las casillas de las pantallas (DEC-127).

Las sirve `catalogos`, que es también contra lo que se valida lo que llega al guardar: una sola
fuente, para que la pantalla no pueda ofrecer un valor que la API después rechaza.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from historia_viva import catalogos
from historia_viva.api.sesion import exigir
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Catálogos"])

Consulta = Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))]


@router.get("/carreras")
def listar_carreras(request: Request, _: Consulta) -> dict[str, list[dict[str, str]]]:
    with request.app.state.bd.transaccion() as tx:
        return {"carreras": [asdict(carrera) for carrera in catalogos.carreras(tx)]}


@router.get("/tipos-actividad")
def listar_tipos_actividad(request: Request, _: Consulta) -> dict[str, list[str]]:
    with request.app.state.bd.transaccion() as tx:
        return {"tipos_actividad": [t.nombre for t in catalogos.tipos_actividad(tx)]}


@router.get("/tipos")
def listar_tipos(request: Request, _: Consulta) -> dict[str, list[str]]:
    with request.app.state.bd.transaccion() as tx:
        tipos = catalogos.tipos_de_pieza(tx)
    return {
        "tipos": [tipo.nombre for tipo in tipos],
        "provisorios": [tipo.nombre for tipo in tipos if tipo.provisorio],
    }
