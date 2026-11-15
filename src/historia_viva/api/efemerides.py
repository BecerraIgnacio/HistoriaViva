"""`/api/efemerides` y `/api/relatos`: la lectura de las efemérides y de los Relatos (spec, módulo
32; OpenAPI `listarEfemerides`, `obtenerEfemeride`, `listarRelatos` y `obtenerRelato`).

Con CONSULTAR se leen las Aprobadas; las Propuestas y Descartadas exigen REVISAR_EFEMERIDES, y
eso lo decide `efemerides` (R-05). 061 suma el filtro por difusión y la aprobación firmada;
062, la edición con `version_base` y el descarte con motivo; 065, lo mismo para los Relatos, con
los mismos cuerpos.

`/api/admin/efemerides/*` es P7f, con OPERAR_SISTEMA: «Generar ahora» y la lectura de la
programación con el pedido pendiente y el resultado de la última corrida (059), y su edición
con `version_base` (060).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.bd import Bd
from historia_viva.dominio.permisos import Permiso
from historia_viva.efemerides import (
    Alcance,
    Efemerides,
    Estado,
    FuenteDeEfemeride,
    Objetivo,
    Periodicidad,
    Programacion,
    ProgramacionDesactualizada,
    VistaDeEfemeride,
    VistaDeRelato,
)
from historia_viva.identidad import Principal
from historia_viva.interruptor import Freno, habilitado_en
from historia_viva.procesamiento.latido import LATIDO_VIGENTE, latidos_por_tipo
from historia_viva.procesamiento.planificador import TIPO as PLANIFICADOR

__all__ = ["relato_http", "router"]

router = APIRouter(prefix="/api", tags=["Efemérides"])

Consultar = Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))]
OperarSistema = Annotated[Principal, Depends(exigir(Permiso.OPERAR_SISTEMA))]

EstadoHttp = Literal["Propuesta", "Aprobada", "Descartada"]
_ESTADOS = {estado.value.capitalize(): estado for estado in Estado}
"""Los estados como los muestra la API: `Propuesta`, `Aprobada`, `Descartada`."""


def _efemerides(request: Request) -> Efemerides:
    efemerides: Efemerides = request.app.state.efemerides
    return efemerides


@router.get("/efemerides")
def listar(
    request: Request,
    principal: Consultar,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
    estado: Annotated[list[EstadoHttp] | None, Query()] = None,
    desde: date | None = None,
    hasta: date | None = None,
) -> dict[str, Any]:
    _rango_valido(desde, hasta)
    with request.app.state.bd.transaccion() as tx:
        vistas = efemerides.listar(
            tx,
            principal=principal,
            estados=[_ESTADOS[e] for e in estado or ()],
            desde=desde,
            hasta=hasta,
        )
    return {"efemerides": [_efemeride(v) for v in vistas], "siguiente": None}


def _rango_valido(desde: date | None, hasta: date | None) -> None:
    if desde and hasta and desde > hasta:
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="«Desde» no puede ser posterior a «Hasta».",
            campo="desde",
        )


@router.get("/efemerides/{efemeride_id}")
def obtener(
    efemeride_id: UUID,
    request: Request,
    principal: Consultar,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return _detalle(efemerides.obtener(tx, efemeride_id, principal=principal))


class Aprobacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_base: int = Field(ge=1)
    """La versión que vio quien revisa: no se aprueba un texto que no se leyó."""


@router.post("/efemerides/{efemeride_id}/aprobacion")
def aprobar(
    efemeride_id: UUID,
    aprobacion: Aprobacion,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.REVISAR_EFEMERIDES))],
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    # 409 `efemeride-no-propuesta` o `version-desactualizada`: `problemas`.
    with request.app.state.bd.transaccion() as tx:
        return _detalle(
            efemerides.aprobar(
                tx, efemeride_id, version_base=aprobacion.version_base, principal=principal
            )
        )


NoVacio = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Edicion(BaseModel):
    """`EfemerideEdicion` del openapi: la versión que se vio y al menos el título o el texto."""

    model_config = ConfigDict(extra="forbid")

    version_base: int = Field(ge=1)
    titulo: NoVacio | None = None
    texto: NoVacio | None = None

    @model_validator(mode="after")
    def _algo_que_guardar(self) -> Edicion:
        if self.titulo is None and self.texto is None:
            raise ValueError("Falta el título o el texto.")
        return self


@router.put("/efemerides/{efemeride_id}")
def editar(
    efemeride_id: UUID,
    edicion: Edicion,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.REVISAR_EFEMERIDES))],
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    # 409 `efemeride-no-propuesta` o `version-desactualizada`: `problemas`.
    with request.app.state.bd.transaccion() as tx:
        return _detalle(
            efemerides.editar(
                tx,
                efemeride_id,
                version_base=edicion.version_base,
                titulo=edicion.titulo,
                texto=edicion.texto,
                principal=principal,
            )
        )


class Descarte(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivo: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)
    ]


@router.post("/efemerides/{efemeride_id}/descarte")
def descartar(
    efemeride_id: UUID,
    descarte: Descarte,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.REVISAR_EFEMERIDES))],
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    # 409 `efemeride-no-propuesta`: `problemas`.
    with request.app.state.bd.transaccion() as tx:
        return _detalle(
            efemerides.descartar(tx, efemeride_id, motivo=descarte.motivo, principal=principal)
        )


def _detalle(vista: VistaDeEfemeride) -> dict[str, Any]:
    return {**_efemeride(vista), "fuentes": [_fuente(f) for f in vista.fuentes]}


@router.get("/relatos")
def listar_relatos(
    request: Request,
    principal: Consultar,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
    estado: Annotated[list[EstadoHttp] | None, Query()] = None,
    desde: date | None = None,
    hasta: date | None = None,
) -> dict[str, Any]:
    """`desde` y `hasta` filtran por la fecha de aprobación."""
    _rango_valido(desde, hasta)
    with request.app.state.bd.transaccion() as tx:
        vistas = efemerides.listar_relatos(
            tx,
            principal=principal,
            estados=[_ESTADOS[e] for e in estado or ()],
            desde=desde,
            hasta=hasta,
        )
    return {"relatos": [_relato(v) for v in vistas], "siguiente": None}


@router.get("/relatos/{relato_id}")
def obtener_relato(
    relato_id: UUID,
    request: Request,
    principal: Consultar,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return relato_http(efemerides.obtener_relato(tx, relato_id, principal=principal))


# Revisar un Relato (065): las mismas reglas y los mismos cuerpos que la efeméride.
# 409 `relato-no-propuesto` o `version-desactualizada`: `problemas`.
Revisa = Annotated[Principal, Depends(exigir(Permiso.REVISAR_EFEMERIDES))]


@router.put("/relatos/{relato_id}")
def editar_relato(
    relato_id: UUID,
    edicion: Edicion,
    request: Request,
    principal: Revisa,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return relato_http(
            efemerides.editar_relato(
                tx,
                relato_id,
                version_base=edicion.version_base,
                titulo=edicion.titulo,
                texto=edicion.texto,
                principal=principal,
            )
        )


@router.post("/relatos/{relato_id}/aprobacion")
def aprobar_relato(
    relato_id: UUID,
    aprobacion: Aprobacion,
    request: Request,
    principal: Revisa,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return relato_http(
            efemerides.aprobar_relato(
                tx, relato_id, version_base=aprobacion.version_base, principal=principal
            )
        )


@router.post("/relatos/{relato_id}/descarte")
def descartar_relato(
    relato_id: UUID,
    descarte: Descarte,
    request: Request,
    principal: Revisa,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return relato_http(
            efemerides.descartar_relato(
                tx, relato_id, motivo=descarte.motivo, principal=principal
            )
        )


def relato_http(vista: VistaDeRelato) -> dict[str, Any]:
    """El `Relato` del OpenAPI, con sus fuentes. Cada una lleva su `posicion`, el `[n]` del texto:
    las de un Relato pueden saltear números (las Citas no verificadas se quitaron, 064)."""
    return {
        **_relato(vista),
        "fuentes": [{"posicion": f.posicion, **_fuente(f)} for f in vista.fuentes],
    }


def _relato(vista: VistaDeRelato) -> dict[str, Any]:
    """El `Relato` del OpenAPI, sin `fuentes` (solo en el detalle)."""
    return {
        "relato_id": str(vista.relato_id),
        "estado": vista.estado.value.capitalize(),
        "titulo": vista.titulo,
        "texto": vista.cuerpo,
        "version": vista.version,
        "aviso_inyeccion": vista.alerta_inyeccion,
        "fuente_retirada": vista.fuente_retirada,
        "mensaje_id": str(vista.mensaje_id),
        "propuesto_por": vista.propuesto_por,
        "propuesto_el": vista.propuesto_el,
        "editado_por": vista.editado_por,
        "editado_el": vista.editado_el,
        "aprobado_por": vista.aprobado_por,
        "aprobado_por_nombre": vista.aprobado_por_nombre,
        "aprobado_el": vista.aprobado_el,
        "metodo_autenticacion": vista.metodo_autenticacion,
        "descartado_por": vista.descartado_por,
        "descartado_por_nombre": vista.descartado_por_nombre,
        "descartado_el": vista.descartado_el,
        "motivo_descarte": vista.descarte_motivo,
        "cantidad_fuentes": vista.cantidad_fuentes,
    }


def _fuente(f: FuenteDeEfemeride) -> dict[str, Any]:
    return {
        "pieza_id": str(f.pieza_id),
        "resumen": f.resumen,
        "fecha_inicio": f.fecha_inicio,
        "fecha_fin": f.fecha_fin,
        "precision_fecha": f.precision_fecha,
        "url_original": f"/api/piezas/{f.pieza_id}/original",
        "alerta_inyeccion": f.alerta_inyeccion,
        "retirada": f.retirada,
    }


class ObjetivoHttp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["dia", "mes"]
    fecha: date
    """De la de mes se usa el mes: el objetivo es su día 1 (059)."""

    def objetivo(self) -> Objetivo:
        if self.tipo == "mes":
            return Objetivo(Alcance.MES, self.fecha.replace(day=1))
        return Objetivo(Alcance.DIA, self.fecha)


class PedidoDeGeneracion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objetivo: ObjetivoHttp | None = None


@router.post("/admin/efemerides/generacion", status_code=202, tags=["Administración"])
def pedir_generacion(
    request: Request,
    principal: OperarSistema,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
    pedido: PedidoDeGeneracion | None = None,
) -> dict[str, Any]:
    """«Generar ahora» (R-08, R-43 c): marca el pedido y lo genera el planificador del
    trabajador en su próxima vuelta; con el procesamiento frenado, cuando se reanude. Sin cuerpo,
    corre la ventana de la programación. Con el planificador caído, reemplaza al pendiente."""
    objetivo = pedido.objetivo if pedido else None
    bd = request.app.state.bd
    vivo = _planificador_vivo(bd)
    with bd.transaccion() as tx:  # 409 `efemeride-existente` o `pedido-pendiente`: `problemas`
        marcado = efemerides.pedir_generacion(
            tx,
            por=principal.actor,
            objetivo=None if objetivo is None else objetivo.objetivo(),
            # Caído, un pedido sin atender por más de un latido vigente se da por trabado.
            trabado_tras=None if vivo else LATIDO_VIGENTE,
        )
    return {
        "estado": "pedida",
        "pedido_manual_el": marcado,
        "procesamiento_pausado": not habilitado_en(bd, Freno.PROCESAMIENTO),
    }


@router.get("/admin/efemerides/programacion", tags=["Administración"])
def leer_programacion(
    request: Request,
    _: OperarSistema,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    """P7f: la programación, el pedido pendiente y el resultado de la última corrida, y lo que
    la pantalla avisa: sin modelo de redacción, procesamiento frenado o planificador caído."""
    bd = request.app.state.bd
    with bd.transaccion() as tx:
        programacion = efemerides.programacion(tx)
    return _programacion(bd, programacion)


class EdicionDeProgramacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_base: int = Field(ge=1)
    habilitada: bool
    periodicidad: Periodicidad
    anticipacion_dias: int = Field(ge=0, le=365)
    maximo_por_corrida: int = Field(ge=1, le=500)


@router.put("/admin/efemerides/programacion", tags=["Administración"])
def guardar_programacion(
    edicion: EdicionDeProgramacion,
    request: Request,
    principal: OperarSistema,
    efemerides: Annotated[Efemerides, Depends(_efemerides)],
) -> dict[str, Any]:
    """P7f (060): la programación con `version_base`; un valor fuera de rango es 422 y la
    edición de otra persona en el medio, 409 con `fila_actual`."""
    bd = request.app.state.bd
    try:
        with bd.transaccion() as tx:
            programacion = efemerides.guardar_programacion(
                tx, **edicion.model_dump(), por=principal.actor
            )
    except ProgramacionDesactualizada as conflicto:
        raise Problema(
            tipo="/errores/version-desactualizada",
            titulo="La programación cambió mientras la editabas",
            estado=409,
            detalle=str(conflicto),
            extensiones={
                "version_base": conflicto.version_base,
                "version_actual": conflicto.actual.version,
                "fila_actual": jsonable_encoder(_programacion(bd, conflicto.actual)),
            },
        ) from conflicto
    return _programacion(bd, programacion)


def _programacion(bd: Bd, programacion: Programacion) -> dict[str, Any]:
    """La `ProgramacionEfemerides` del OpenAPI, con lo que la pantalla avisa."""
    return {
        **asdict(programacion),
        "procesamiento_habilitado": habilitado_en(bd, Freno.PROCESAMIENTO),
        "planificador_vivo": _planificador_vivo(bd),
    }


def _planificador_vivo(bd: Bd) -> bool:
    """Si el planificador de efemérides latió hace menos de un minuto (DEC-106)."""
    return any(
        t.vivo for t in latidos_por_tipo(bd, esperados=(PLANIFICADOR,)) if t.tipo == PLANIFICADOR
    )


def _efemeride(vista: VistaDeEfemeride) -> dict[str, Any]:
    """La `Efemeride` del OpenAPI, sin `fuentes` (solo en el detalle)."""
    return {
        "efemeride_id": str(vista.efemeride_id),
        "estado": vista.estado.value.capitalize(),
        "tipo": vista.alcance.value,
        "dia": vista.fecha_objetivo.day,
        "mes": vista.fecha_objetivo.month,
        "difusion_desde": vista.difusion_desde,
        "difusion_hasta": vista.difusion_hasta,
        "titulo": vista.titulo,
        "texto": vista.cuerpo,
        "version": vista.version,
        "aviso_inyeccion": vista.alerta_inyeccion,
        "plantilla_provisoria": vista.plantilla == "provisoria",
        "fuente_retirada": vista.fuente_retirada,
        "generada_por": vista.generada_por or "sistema",
        "generada_el": vista.generada_el,
        "aprobada_por": vista.aprobada_por,
        "aprobada_por_nombre": vista.aprobada_por_nombre,
        "aprobada_el": vista.aprobada_el,
        "metodo_autenticacion": vista.metodo_autenticacion,
        "editada_por": vista.editada_por,
        "editada_el": vista.editada_el,
        "descartada_por": vista.descartada_por,
        "descartada_por_nombre": vista.descartada_por_nombre,
        "descartada_el": vista.descartada_el,
        "motivo_descarte": vista.descarte_motivo,
    }
