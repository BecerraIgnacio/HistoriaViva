"""`/api/informes`: el Informe de acreditación por HTTP (spec, módulo 33; DEC-131).

Los permisos finos los decide `informes` (DEC-124): la ruta exige lo mínimo para entrar, y el
módulo responde 404 a quien solo consulta y pide un Borrador: no se entera de que existe (R-05).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict

from historia_viva.api.sesion import exigir
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal
from historia_viva.informes import Formato, Informes, VistaDeInforme

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Informes"])

Consultar = Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))]
Generar = Annotated[Principal, Depends(exigir(Permiso.GENERAR_INFORME))]
Aprobar = Annotated[Principal, Depends(exigir(Permiso.APROBAR_INFORME))]


class PedidoInforme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desde: date
    hasta: date
    carreras: list[str] = []
    """`carrera_id` (auditoría, decisión 3); vacío = todas."""
    reemplaza: UUID | None = None
    """«Generar de nuevo»: el Borrador vigente que este reemplaza (R-22)."""


def _informes(request: Request) -> Informes:
    informes: Informes = request.app.state.informes
    return informes


def _json(vista: VistaDeInforme, *, detalle: bool = True) -> dict[str, Any]:
    cuerpo = asdict(vista)
    cuerpo["estado"] = vista.estado.capitalize()
    valor, total = vista.integridad, vista.total_piezas
    # shortcut: se guarda solo el valor, con 4 decimales; las completas se derivan y son exactas
    # mientras el informe tenga menos de 5000 Piezas. Guardarlas si alguno llega a tantas.
    cuerpo["integridad"] = {
        "valor": valor,
        "piezas_completas": 0 if valor is None else round(valor * total),
        "piezas_total": total,
    }
    grupos = cuerpo.pop("grupos")
    faltantes = cuerpo.pop("faltantes")
    cambios = cuerpo.pop("cambios")
    if detalle:
        for grupo in grupos:
            for pieza in grupo["piezas"]:
                pieza["citas"] = [pieza.pop("cita")]
        # R-22: derivado al leer; los nombres son para el aviso de P9.
        cuerpo |= {
            "grupos": grupos, "faltantes": faltantes,
            "cambio_despues_de_aprobar": vista.cambio_despues_de_aprobar, "cambios": cambios,
        }
    return dict(jsonable_encoder(cuerpo))


@router.post("/informes", status_code=201)
def generar(
    pedido: PedidoInforme,
    request: Request,
    response: Response,
    principal: Generar,
    informes: Annotated[Informes, Depends(_informes)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        vista = informes.generar(
            tx, desde=pedido.desde, hasta=pedido.hasta, carreras=pedido.carreras,
            principal=principal, reemplaza=pedido.reemplaza,
        )
    response.headers["Location"] = f"/api/informes/{vista.informe_id}"
    return _json(vista)


@router.get("/informes")
def listar(
    request: Request, principal: Consultar, informes: Annotated[Informes, Depends(_informes)]
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        vistas = informes.listar(tx, principal=principal)
    return {"informes": [_json(vista, detalle=False) for vista in vistas]}


@router.get("/informes/{informe_id}")
def obtener(
    informe_id: UUID,
    request: Request,
    principal: Consultar,
    informes: Annotated[Informes, Depends(_informes)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return _json(informes.obtener(tx, informe_id, principal=principal))


@router.post("/informes/{informe_id}/aprobaciones", status_code=201)
def aprobar(
    informe_id: UUID,
    request: Request,
    principal: Aprobar,
    informes: Annotated[Informes, Depends(_informes)],
) -> dict[str, Any]:
    # 409 `aprobador-repetido`, `informe-ya-aprobado-por-rol` o `transicion-invalida`: `problemas`.
    with request.app.state.bd.transaccion() as tx:
        return _json(informes.aprobar(tx, informe_id, principal=principal))


@router.get("/informes/{informe_id}/exportacion")
def exportar(
    informe_id: UUID,
    formato: Formato,
    request: Request,
    principal: Consultar,
    informes: Annotated[Informes, Depends(_informes)],
) -> Response:
    # 403 sin GENERAR_INFORME ni APROBAR_INFORME y 409 `informe-no-aprobado`: `problemas`. Un
    # `formato` fuera de `Formato` es 422. `no-store` lo pone `cabeceras`.
    with request.app.state.bd.transaccion() as tx:
        archivo = informes.exportar(tx, informe_id, formato, principal=principal)
    return Response(
        archivo.contenido,
        media_type=archivo.tipo_mime,
        headers={"Content-Disposition": f'attachment; filename="{archivo.nombre}"'},
    )


@router.post("/informes/{informe_id}/descarte")
def descartar(
    informe_id: UUID,
    request: Request,
    principal: Generar,
    informes: Annotated[Informes, Depends(_informes)],
) -> dict[str, Any]:
    # 409 `transicion-invalida` si ya no es un Borrador vigente: `problemas`.
    with request.app.state.bd.transaccion() as tx:
        return _json(informes.descartar(tx, informe_id, principal=principal))
