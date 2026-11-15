"""Crear un Lote (026), leerlo con su progreso (084), listarlos y editar su Contexto (085),
aprobar sus Pendientes en bloque (108) y crearlo sin pedir campos al arrastrar varios archivos
(151). La carga en un Lote es `POST /api/piezas` con `lote_id`
(DEC-20): no hay endpoint de carga acá."""

from __future__ import annotations

import base64
import math
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from historia_viva.api.dependencias import validacion_de_fichas
from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir
from historia_viva.dominio.bloque import Rechazo
from historia_viva.dominio.fechas import PrecisionFecha, derivar_rango
from historia_viva.dominio.lote import ContextoDeLote, EstadoDeLote
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.permisos import Permiso
from historia_viva.identidad import Principal
from historia_viva.ingesta import normalizar_linea
from historia_viva.lotes import Lote, Lotes, ResumenDeLote, VistaDeLote
from historia_viva.validacion import (
    AprobacionNoAbierta,
    BloqueDeshabilitado,
    PiezaVista,
    ResultadoDeBloque,
    Validacion,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Lotes"])

# Normalizado antes de recortar y medir: un texto de solo controles queda vacío (R-36).
_Texto = Annotated[
    str,
    AfterValidator(lambda texto: normalizar_linea(texto).strip()),
    StringConstraints(min_length=1),
]


class PedidoLote(BaseModel):
    """La Fecha es obligatoria en el Lote (DEC-113): las tres partes, con las reglas de la
    Ficha. Propietario y Carreras son opcionales en el Lote (DEC-126)."""

    model_config = ConfigDict(extra="forbid")

    nombre: _Texto
    origen: _Texto
    fecha_inicio: date
    precision_fecha: PrecisionFecha
    fecha_fin: date
    evento: _Texto | None = None
    lugar: _Texto | None = None
    propietario: _Texto | None = None
    carreras: list[str] = []


class PedidoLoteImplicito(BaseModel):
    """El Lote de una carga de varios archivos (151): solo el nombre provisorio que arma la
    pantalla. Origen, Fecha y Propietario se piden después, en la pantalla del Lote."""

    model_config = ConfigDict(extra="forbid")

    nombre: _Texto


class PedidoEdicionDeLote(BaseModel):
    """Los campos de `PedidoLote` que cambian (al menos uno). `evento`, `lugar` y `propietario`
    en `null` se borran; los obligatorios no. Con Piezas, solo se completa lo vacío (151)."""

    model_config = ConfigDict(extra="forbid")

    nombre: _Texto | None = None
    origen: _Texto | None = None
    fecha_inicio: date | None = None
    precision_fecha: PrecisionFecha | None = None
    fecha_fin: date | None = None
    evento: _Texto | None = None
    lugar: _Texto | None = None
    propietario: _Texto | None = None
    carreras: list[str] | None = None


_NO_SE_BORRAN = ("nombre", "origen", "fecha_inicio", "precision_fecha", "fecha_fin", "carreras")


def _lotes(request: Request) -> Lotes:
    lotes: Lotes = request.app.state.lotes
    return lotes


def _respuesta(lote: Lote) -> dict[str, Any]:
    contexto = lote.contexto
    return {
        "lote": {
            "lote_id": str(lote.lote_id),
            "nombre": lote.nombre,
            **contexto.a_json(),
            "propietario": lote.propietario,
            "carreras": list(lote.carreras),
            "creado_por": str(lote.creado_por),
            "creado_el": lote.creado_el.isoformat(),
        }
    }


def _rango(inicio: date, precision: PrecisionFecha, fin: date | None) -> tuple[date, date]:
    try:
        return derivar_rango(inicio, precision, fin)
    except ValueError as error:
        raise Problema(
            tipo="/errores/fecha-invalida",
            titulo="La Fecha no es válida",
            estado=422,
            detalle=str(error),
            campo="fecha_fin",
        ) from error


@router.post("/lotes", status_code=201)
def crear_lote(
    pedido: PedidoLote,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    lotes: Annotated[Lotes, Depends(_lotes)],
) -> dict[str, Any]:
    inicio, fin = _rango(pedido.fecha_inicio, pedido.precision_fecha, pedido.fecha_fin)
    contexto = ContextoDeLote(
        origen=pedido.origen,
        fecha_inicio=inicio,
        fecha_fin=fin,
        precision_fecha=pedido.precision_fecha,
        evento=pedido.evento,
        lugar=pedido.lugar,
    )
    with request.app.state.bd.transaccion() as tx:
        lote = lotes.crear(
            tx,
            nombre=pedido.nombre,
            contexto=contexto,
            creado_por=principal.usuario_id,
            propietario=pedido.propietario,
            carreras=tuple(pedido.carreras),
        )
    response.headers["Location"] = f"/api/lotes/{lote.lote_id}"
    return _respuesta(lote)


@router.post("/lotes/implicito", status_code=201)
def crear_lote_implicito(
    pedido: PedidoLoteImplicito,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    lotes: Annotated[Lotes, Depends(_lotes)],
) -> dict[str, Any]:
    """Arrastrar varios archivos sin Lote lo crea con un nombre provisorio y sin Contexto; las
    Piezas se suben después con su `lote_id`, como en cualquier Lote (DEC-20, R-27)."""
    with request.app.state.bd.transaccion() as tx:
        lote = lotes.crear_implicito(tx, nombre=pedido.nombre, por=principal.actor)
    response.headers["Location"] = f"/api/lotes/{lote.lote_id}"
    return _respuesta(lote)


def _pedido_invalido(detalle: str, campo: str | None) -> Problema:
    return Problema(
        tipo="/errores/pedido-invalido",
        titulo="El pedido es inválido",
        estado=422,
        detalle=detalle,
        campo=campo,
    )


def _cursor(resumen: ResumenDeLote) -> str:
    clave = f"{resumen.lote.creado_el.isoformat()}|{resumen.lote.lote_id}"
    return base64.urlsafe_b64encode(clave.encode()).decode()


def _despues_de(cursor: str) -> tuple[datetime, UUID]:
    try:
        creado_el, lote_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(creado_el), UUID(lote_id)
    except ValueError as error:  # también `binascii.Error` y `UnicodeDecodeError`
        raise _pedido_invalido(
            "El cursor no es válido: usá el `siguiente` de la página anterior.", "cursor"
        ) from error


@router.get("/lotes")
def listar_lotes(
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    lotes: Annotated[Lotes, Depends(_lotes)],
    creado_por: str | None = None,
    estado: EstadoDeLote | None = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """«Mis Lotes» es `creado_por=yo`; sin `creado_por`, todos. `siguiente` es `null` al final."""
    if creado_por == "yo":
        creador: UUID | None = principal.usuario_id
    else:
        try:
            creador = None if creado_por is None else UUID(creado_por)
        except ValueError as error:
            raise _pedido_invalido(
                "`creado_por` es `yo` o un identificador.", "creado_por"
            ) from error
    with request.app.state.bd.transaccion() as tx:
        # Uno de más para saber si hay otra página sin contar.
        pagina = lotes.listar(
            tx,
            creado_por=creador,
            estado=estado,
            despues_de=None if cursor is None else _despues_de(cursor),
            limite=limite + 1,
        )
    return {
        "lotes": [_fila(resumen) for resumen in pagina[:limite]],
        "siguiente": _cursor(pagina[limite - 1]) if len(pagina) > limite else None,
    }


def _fila(resumen: ResumenDeLote) -> dict[str, Any]:
    return _respuesta(resumen.lote)["lote"] | {
        "creado_por_nombre": resumen.creado_por_nombre,
        "estado": resumen.estado.value,
        "total": resumen.total,
        "por_estado": {estado.value: n for estado, n in resumen.por_estado.items()},
        "puede_cargar": resumen.puede_cargar,
        "fallidas": resumen.fallidas,
        "estimacion": _segundos(resumen.estimacion),
    }


def _segundos(estimacion: timedelta | None) -> int | None:
    """La espera en segundos enteros, redondeada para arriba; `null` sin estimación."""
    return None if estimacion is None else math.ceil(estimacion.total_seconds())


@router.get("/lotes/{lote_id}")
def obtener_lote(
    lote_id: UUID,
    request: Request,
    # Leer un Lote es abierto a todo rol que consulta (R-06, DEC-125).
    _: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    lotes: Annotated[Lotes, Depends(_lotes)],
) -> dict[str, Any]:
    with request.app.state.bd.transaccion() as tx:
        return _vista(lotes.ver(tx, lote_id))


@router.patch("/lotes/{lote_id}")
def editar_lote(
    lote_id: UUID,
    pedido: PedidoEdicionDeLote,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    lotes: Annotated[Lotes, Depends(_lotes)],
) -> dict[str, Any]:
    """Todo, mientras el Lote no tiene Piezas; con Piezas, solo completar Origen, Fecha o
    Propietario vacíos, que se copian a sus Recibidas y Pendientes (151). Lo demás, 409
    `contexto-congelado`."""
    cambios = pedido.model_dump(exclude_unset=True)
    if not cambios:
        raise _pedido_invalido("Mandá al menos un campo para cambiar.", None)
    for campo in _NO_SE_BORRAN:
        if campo in cambios and cambios[campo] is None:
            raise _pedido_invalido(f"`{campo}` no puede quedar vacío.", campo)

    def editar(lote: Lote) -> Lote:
        antes = lote.contexto
        precision = cambios.get("precision_fecha", antes.precision_fecha)
        inicio = cambios.get("fecha_inicio", antes.fecha_inicio)
        fin = cambios.get("fecha_fin", antes.fecha_fin)
        if inicio is not None and precision is not None:
            inicio, fin = _rango(inicio, precision, fin)
        elif inicio is not None or precision is not None or fin is not None:
            # Un Lote sin Fecha la recibe entera (151): inicio, precisión y, en rango, fin.
            falta = "fecha_inicio" if inicio is None else "precision_fecha"
            raise _pedido_invalido("La Fecha lleva inicio y precisión.", falta)
        return replace(
            lote,
            nombre=cambios.get("nombre", lote.nombre),
            contexto=ContextoDeLote(
                origen=cambios.get("origen", antes.origen),
                fecha_inicio=inicio,
                fecha_fin=fin,
                precision_fecha=precision,
                evento=cambios.get("evento", antes.evento),
                lugar=cambios.get("lugar", antes.lugar),
            ),
            propietario=cambios.get("propietario", lote.propietario),
            carreras=tuple(cambios.get("carreras", lote.carreras)),
        )

    with request.app.state.bd.transaccion() as tx:
        return _respuesta(lotes.actualizar(tx, lote_id, editar=editar, por=principal.actor))


def _vista(vista: VistaDeLote) -> dict[str, Any]:
    progreso = vista.progreso
    respuesta = _respuesta(vista.lote)
    respuesta["lote"]["contexto_editable"] = vista.contexto_editable
    respuesta["lote"]["por_completar"] = list(vista.por_completar)
    return respuesta | {
        "progreso": {
            "total": progreso.total,
            "estado": progreso.estado.value,
            "por_estado": {estado.value: n for estado, n in progreso.por_estado.items()},
            "fallidas": progreso.fallidas,
            "para_analisis_humano": progreso.para_analisis_humano,
            "sin_indexar": progreso.sin_indexar,
            "estimacion": _segundos(progreso.estimacion),
        },
        "puede_cargar": vista.puede_cargar,
    }


# --- Aprobación en bloque (108; CU-07, DEC-22, DEC-135) -------------------------------------


def _sin_video(clase: ClaseDeOriginal) -> ClaseDeOriginal:
    if clase is ClaseDeOriginal.VIDEO:
        raise ValueError("Un video no se firma en bloque: validalo de a uno (DEC-135).")
    return clase


class PedidoPrevia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clase: Annotated[ClaseDeOriginal, AfterValidator(_sin_video)]
    """Solo Pendientes de una misma clase: homogéneo es eso (spec, módulo `validacion`)."""


class PiezaVistaPedida(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pieza_id: UUID
    version: int


class PedidoConfirmacion(BaseModel):
    """El bloque aplica solo tipo de Pieza, tipo de actividad y contexto adicional: origen, Fecha,
    Propietario y Carreras vienen heredados del Lote y `extra="forbid"` los rebota con 422
    (DEC-114, DEC-135)."""

    model_config = ConfigDict(extra="forbid")

    tipo: _Texto
    tipo_actividad: _Texto
    contexto_adicional: _Texto | None = None
    # El tope del Lote (R-27): no hay bloque más grande que un Lote.
    piezas: list[PiezaVistaPedida] = Field(min_length=1, max_length=100)


def _bloque_deshabilitado(error: BloqueDeshabilitado) -> Problema:
    return Problema(
        tipo="/errores/bloque-deshabilitado",
        titulo="La aprobación en bloque está deshabilitada",
        estado=409,
        detalle=str(error),
    )


def _rechazo(rechazo: Rechazo) -> dict[str, Any]:
    fila: dict[str, Any] = {"pieza_id": str(rechazo.pieza_id), "motivo": rechazo.motivo.value}
    if rechazo.campo is not None:
        fila["campo"] = rechazo.campo
    return fila


@router.post("/lotes/{lote_id}/aprobaciones", status_code=201)
def abrir_aprobacion(
    lote_id: UUID,
    pedido: PedidoPrevia,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    validacion: Annotated[Validacion, Depends(validacion_de_fichas)],
) -> dict[str, Any]:
    """Paso 1, la previa: no firma nada. 409 `bloque-deshabilitado` con `HV_BLOQUE_HABILITADO`
    en falso (R-28); 404 `lote-no-encontrado`."""
    try:
        previa = validacion.previa_en_bloque(
            lote_id=lote_id, clase=pedido.clase, principal=principal
        )
    except BloqueDeshabilitado as error:
        raise _bloque_deshabilitado(error) from error
    return {
        "aprobacion_id": str(previa.aprobacion_id),
        "abierta_el": momento(previa.abierta_el),
        "elegibles": [
            {"pieza_id": str(c.pieza_id), "version": c.version} for c in previa.elegibles
        ],
        "rechazadas": [_rechazo(r) for r in previa.rechazadas],
    }


@router.post("/lotes/{lote_id}/aprobaciones/{aprobacion_id}/confirmacion")
def confirmar_aprobacion(
    lote_id: UUID,
    aprobacion_id: UUID,
    pedido: PedidoConfirmacion,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    validacion: Annotated[Validacion, Depends(validacion_de_fichas)],
) -> dict[str, Any]:
    """Paso 2: firma en una transacción las que siguen cumpliendo la regla, con
    `modo_validacion = bloque` y una indexación por Pieza. Una versión que cambió desde la
    previa queda en `rechazadas` con `version_desactualizada`. 409 `bloque-deshabilitado`
    (R-28) y `aprobacion-no-abierta` (repetida o de otro Lote); 422 `tipo-desconocido`."""
    try:
        resultado: ResultadoDeBloque = validacion.confirmar_en_bloque(
            aprobacion_id=aprobacion_id,
            lote_id=lote_id,
            principal=principal,
            tipo=pedido.tipo,
            tipo_actividad=pedido.tipo_actividad,
            contexto_adicional=pedido.contexto_adicional,
            piezas=[PiezaVista(p.pieza_id, p.version) for p in pedido.piezas],
        )
    except BloqueDeshabilitado as error:
        raise _bloque_deshabilitado(error) from error
    except AprobacionNoAbierta as error:
        raise Problema(
            tipo="/errores/aprobacion-no-abierta",
            titulo="La aprobación en bloque no está abierta",
            estado=409,
            detalle=str(error),
        ) from error
    return {
        "aprobadas": [str(p) for p in resultado.aprobadas],
        "rechazadas": [_rechazo(r) for r in resultado.rechazadas],
        "duracion_s": resultado.duracion_s,
    }
