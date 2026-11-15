"""`GET /api/busqueda`: la lista de recuperación, para el panel de fuentes y para M1 (R-14).

La pantalla de consulta es la Conversación (DEC-129); esta ruta no redacta. Los valores de
los filtros se validan contra `catalogos`; uno dado de baja se acepta, porque las Fichas que ya
lo tenían lo conservan (DEC-127).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from historia_viva.api.filtros import armar_filtro, validar_filtro
from historia_viva.api.problemas import Problema, respuesta
from historia_viva.api.sesion import exigir
from historia_viva.busqueda import LIMITE_MAXIMO, Consulta
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Búsqueda"])

Consultar = Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))]
Repetible = Annotated[list[str], Query()]


@router.get("/busqueda")
def buscar(
    request: Request,
    principal: Consultar,
    q: Annotated[str | None, Query(min_length=3, max_length=500)] = None,
    desde: date | None = None,
    hasta: date | None = None,
    tipo: Repetible = [],  # noqa: B006 — FastAPI copia el valor por defecto en cada pedido
    tipo_actividad: Repetible = [],  # noqa: B006
    carrera: Repetible = [],  # noqa: B006
    limite: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = 10,
) -> JSONResponse:
    filtro = armar_filtro(
        desde=desde, hasta=hasta, tipo=tipo, tipo_actividad=tipo_actividad, carrera=carrera
    )
    # Se valida al crearla: un NUL no llega a Postgres, ni a los catálogos.
    consulta = Consulta(texto=q, filtro=filtro, limite=limite)
    validar_filtro(request.app.state.bd, filtro)
    # Solo cuenta la que embebe (sin texto no toca el motor), y después de validar: un 400 no
    # gasta cupo (SEG-09).
    if q is not None:
        (_, _, reinicio_s), admitida = request.app.state.cupos_de_busqueda.tomar(
            principal.usuario_id, "GET"
        )
        if not admitida:
            # Por proceso, como los cupos de `/v1`: `web` es un solo uvicorn.
            rechazo = respuesta(Problema(
                tipo="/errores/limite-de-consultas",
                titulo="Demasiadas consultas",
                estado=429,
                detalle="Llegaste al tope de búsquedas por minuto. Probá de nuevo en un rato.",
            ))
            rechazo.headers["Retry-After"] = str(max(reinicio_s, 1))
            return rechazo
    resultado = request.app.state.busqueda.buscar(consulta)
    cuerpo: dict[str, Any] = {
        "resultados": [asdict(r) for r in resultado.resultados],
        "total": resultado.total,
        "limite": limite,
        "filtros": {
            "desde": desde,
            "hasta": hasta,
            "tipo": list(filtro.tipos),
            "tipo_actividad": list(filtro.tipos_actividad),
            "carrera": list(filtro.carreras),
        },
        "avisos": ["indice_incompleto"] if resultado.indice_incompleto else [],
    }
    return JSONResponse(
        jsonable_encoder(cuerpo),
        headers={"Server-Timing": f"busqueda;dur={resultado.duracion_ms}"},
    )
