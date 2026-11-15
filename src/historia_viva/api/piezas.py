"""Cargar una Pieza, seguir su estado y ver su original."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from historia_viva.agentes import Credenciales
from historia_viva.api.dependencias import (
    almacen_de_originales,
    cola_de_trabajos,
    ingesta_de_piezas,
    modulo_de_fichas,
    registro_de_mediciones,
    visible_para,
)
from historia_viva.api.formulario import archivo_del_formulario, cuerpo_crudo
from historia_viva.api.problemas import Problema
from historia_viva.api.mediciones import momento
from historia_viva.api.sesion import exigir, nombre_de_persona
from historia_viva.dominio.cuerpo import CuerpoDeFicha
from historia_viva.dominio.errores import PiezaNoEncontrada, TopeDeReprocesos, TransicionInvalida
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.permisos import (
    Permiso,
    puede_cargar_en_lote_ajeno,
    puede_reprocesar_ajena,
    ve_piezas_no_validadas,
)
from historia_viva.dominio.tipos import DetallePieza, ResumenPieza, ViaDeIngreso
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal
from historia_viva.ingesta import AnalisisDeVideoNoDisponible, Ingesta
from historia_viva.instrumentacion import RegistroMediciones
from historia_viva.interruptor import Freno, habilitado_en
from historia_viva.originales import AlmacenDeOriginales
from historia_viva.indexacion import Indexacion
from historia_viva.procesamiento.cola import Cola, EstadoDeTrabajo, TipoDeTrabajo

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Piezas"])

_SOBRE_MULTIPART = 64 * 1024
"""Lo que el `Content-Length` puede traer además del archivo: frontera y encabezados de la
parte, con un nombre largo. Lo que el pedido declara menos esto es lo mínimo que pesa el
archivo."""


def _fila(pieza: ResumenPieza) -> dict[str, Any]:
    return {
        "pieza_id": str(pieza.pieza_id),
        "estado": pieza.estado.value,
        "tipo": pieza.tipo,
        "resumen": pieza.resumen,
        "nombre_archivo": pieza.nombre_archivo,
        "creada_el": pieza.creada_el.isoformat(),
        "actualizada_el": pieza.actualizada_el.isoformat(),
        "campos_faltantes": pieza.campos_faltantes,
        "alerta_inyeccion": pieza.alerta_inyeccion,
    }


def _detalle(pieza: DetallePieza, error_procesamiento: str | None) -> dict[str, Any]:
    return _fila(pieza.fila) | {
        "tipo_mime": pieza.tipo_mime,
        "tamano_bytes": pieza.tamano_bytes,
        "tipo_actividad": pieza.tipo_actividad,
        "propietario": pieza.propietario,
        "autor": pieza.autor,
        "carreras": list(pieza.carreras),
        "via_ingreso": pieza.via_ingreso.value,
        "error_procesamiento": error_procesamiento,
        "metadata_original": pieza.metadata_original,
    }


@router.post("/piezas", status_code=201)
def subir_pieza(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    ingesta: Annotated[Ingesta, Depends(ingesta_de_piezas)],
    originales: Annotated[AlmacenDeOriginales, Depends(almacen_de_originales)],
) -> dict[str, Any]:
    """Recibe el campo `archivo` en streaming: el disco se mira antes de leer un byte (DEC-112)
    y el tope de la clase se aplica con `Content-Length` y mientras llega (DEC-47). Con
    `lote_id`, que va antes que `archivo`, la Pieza es de ese Lote y hereda su Contexto; si el
    mismo original ya está en ese Lote, responde 200 con esa Pieza y `duplicada: true`."""
    content_length = int(request.headers.get("content-length") or 0)
    originales.exigir_espacio(content_length)
    nombre, flujo, campos = archivo_del_formulario(request)
    lote_id = _lote_id(campos.get("lote_id"))
    # Solo se encola: la respuesta vuelve con la Pieza `Recibida` y el procesamiento ocurre
    # después, en segundo plano. Que este 201 no espere al modelo es el punto (US 1, RF-05).
    detalle, duplicada = ingesta.recibir_archivo(
        flujo=flujo,
        nombre=nombre,
        por=principal.actor,
        tamano_minimo=content_length - _SOBRE_MULTIPART,
        lote_id=lote_id,
        puede_en_lote_ajeno=puede_cargar_en_lote_ajeno(principal.rol),
    )
    pieza = detalle.fila
    _ubicar(response, pieza.pieza_id, duplicada)
    return {
        "pieza_id": str(pieza.pieza_id),
        "estado": pieza.estado.value,
        "nombre_archivo": pieza.nombre_archivo,
        "lote_id": None if detalle.lote_id is None else str(detalle.lote_id),
        "campos_heredados": list(detalle.campos_heredados),
        "creada_el": pieza.creada_el.isoformat(),
        # Se encola igual; avisa que no se procesa hasta reanudar (DEC-112).
        "procesamiento_pausado": not habilitado_en(request.app.state.bd, Freno.PROCESAMIENTO),
        "duplicada": duplicada,
    }


@router.post("/piezas/video", status_code=201)
def subir_video(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    ingesta: Annotated[Ingesta, Depends(ingesta_de_piezas)],
    originales: Annotated[AlmacenDeOriginales, Depends(almacen_de_originales)],
    nombre: Annotated[str, Query(min_length=1, max_length=1024)],
    lote_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Recibe un MP4, MOV o WebM como cuerpo crudo; el `Content-Type` se ignora. La Pieza nace
    `Pendiente` con «Requiere análisis humano», sin trabajo en la cola (ADR-0005)."""
    if not ingesta.admite_video:  # sin `ffprobe`, 503 antes que un 507 del disco
        raise AnalisisDeVideoNoDisponible()
    content_length = int(request.headers.get("content-length") or 0)
    originales.exigir_espacio(content_length)
    detalle, duplicada = ingesta.recibir_video(
        flujo=cuerpo_crudo(request),
        nombre=nombre,
        por=principal.actor,
        tamano_minimo=content_length,
        lote_id=_lote_id(lote_id),
        puede_en_lote_ajeno=puede_cargar_en_lote_ajeno(principal.rol),
    )
    pieza = detalle.fila
    _ubicar(response, pieza.pieza_id, duplicada)
    return {
        "pieza_id": str(pieza.pieza_id),
        "estado": pieza.estado.value,
        "clase_original": detalle.clase_original,
        # Todo video nace con la v1 `sistema` vacía: nadie ni nada lo miró todavía.
        "requiere_analisis_humano": True,
        "nombre_archivo": pieza.nombre_archivo,
        "lote_id": None if detalle.lote_id is None else str(detalle.lote_id),
        "huella": detalle.huella,
        "creada_el": pieza.creada_el.isoformat(),
        "duplicada": duplicada,
    }


def _ubicar(response: Response, pieza_id: UUID, duplicada: bool) -> None:
    """201 con `Location`; el mismo original en el mismo Lote es la Pieza que ya estaba: 200."""
    response.headers["Location"] = f"/api/piezas/{pieza_id}"
    if duplicada:
        response.status_code = 200


def _lote_id(valor: str | None) -> UUID | None:
    if not valor:
        return None
    try:
        return UUID(valor)
    except ValueError:
        raise Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle="El campo lote_id no es un identificador válido.",
            campo="lote_id",
        ) from None


@router.get("/piezas")
def listar_piezas(
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    cola: Annotated[Cola, Depends(cola_de_trabajos)],
    request: Request,
    estado: Annotated[list[Estado] | None, Query()] = None,
    lote_id: Annotated[UUID | None, Query()] = None,
    mias: Annotated[bool, Query()] = False,
) -> dict[str, list[dict[str, Any]]]:
    """Con `mias`, Mis Piezas: lo que la persona subió y no está Validado, las Incompletas que
    trabajó y sus Validadas todavía sin indexar; un Aporte no es de nadie (DEC-123, R-43 h)."""
    if not ve_piezas_no_validadas(principal.rol):
        estado = [e for e in estado or [Estado.VALIDADA] if e is Estado.VALIDADA]
    # shortcut: sin `mias` ni `lote_id` trae todo el Acervo (Mis Piezas con «Todas» lo sondea
    # cada 3 s); alcanza con cientos de Piezas. Con miles, sumar `limite` y `cursor` (spec) o
    # dejar afuera las Validadas indexadas en el servidor.
    piezas = fichas.listar_por_estado(
        estados=estado, lote_id=lote_id, mias=principal.usuario_id if mias else None
    )
    recibidas = [p.pieza_id for p in piezas if p.estado is Estado.RECIBIDA]
    validadas = [p.pieza_id for p in piezas if p.estado is Estado.VALIDADA]
    indexacion: Indexacion = request.app.state.indexacion
    with request.app.state.bd.transaccion() as tx:
        procesamientos = cola.estado_de_trabajos(tx, recibidas, TipoDeTrabajo.PROCESAMIENTO)
        no_indexadas = indexacion.no_indexadas(tx, validadas)
    if mias:
        # Una Validada ya buscable no es trabajo de nadie: sale de Mis Piezas (DEC-146 punto 6).
        piezas = tuple(
            p for p in piezas if p.estado is not Estado.VALIDADA or p.pieza_id in no_indexadas
        )
    return {
        "piezas": [
            _fila(pieza)
            | {
                "lote_id": None if pieza.lote_id is None else str(pieza.lote_id),
                "creada_por": None if pieza.creada_por is None else str(pieza.creada_por),
                "requiere_analisis_humano": pieza.requiere_analisis_humano,
                # Los estados visibles de una Pieza con problemas (R-30, DEC-146 puntos 6 y 16).
                "fallida": procesamientos.get(pieza.pieza_id) is EstadoDeTrabajo.FALLIDO,
                "no_indexada": pieza.pieza_id in no_indexadas,
            }
            for pieza in piezas
        ]
    }


@router.get("/piezas/{pieza_id}")
def obtener_pieza(
    pieza_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    cola: Annotated[Cola, Depends(cola_de_trabajos)],
    registro: Annotated[RegistroMediciones, Depends(registro_de_mediciones)],
    ingesta: Annotated[Ingesta, Depends(ingesta_de_piezas)],
) -> dict[str, Any]:
    # Un fallo no es un estado de la Pieza: el porqué lo tiene la cola. Fuera de `Recibida` el
    # procesamiento ya no cuenta (la completaron a mano, DEC-122).
    detalle = visible_para(principal, fichas.obtener_pieza(pieza_id))
    recibida = detalle.fila.estado is Estado.RECIBIDA
    return _detalle(detalle, cola.error_del_procesamiento(pieza_id) if recibida else None) | {
        # «Esperando al motor» (encolado con el freno puesto o el motor caído) no es «falló»
        # (DEC-146 punto 16); la pantalla cruza el motor con `/api/salud`.
        "estado_procesamiento": cola.estado_del_procesamiento(pieza_id) if recibida else None,
        "procesamiento_pausado": not habilitado_en(request.app.state.bd, Freno.PROCESAMIENTO),
        # Aviso informativo «abierta por X»: no reserva la Pieza, la exclusión es el 409 (DEC-24).
        "en_edicion_por": _en_edicion_por(request, registro, pieza_id, principal),
        "aporte": _aporte(request, ingesta, detalle),
        # Enter firma y abre la siguiente Pendiente del mismo Lote (147).
        "lote_id": None if detalle.lote_id is None else str(detalle.lote_id),
    }


def _aporte(request: Request, ingesta: Ingesta, detalle: DetallePieza) -> dict[str, Any] | None:
    """Quién mandó el Aporte, cuándo y por dónde: el encabezado de P5f."""
    if detalle.via_ingreso is not ViaDeIngreso.APORTE:
        return None
    credenciales: Credenciales = request.app.state.credenciales
    with request.app.state.bd.transaccion() as tx:
        datos = ingesta.datos_del_aporte(tx, detalle.fila.pieza_id)
        if datos is None:
            return None
        return {
            "agente": credenciales.nombre(tx, datos.agente_id),
            "recibido_el": momento(datos.recibido_el),
            "tipo_contenido": datos.tipo_contenido,
            "canal": datos.canal,
        }


def _en_edicion_por(
    request: Request, registro: RegistroMediciones, pieza_id: UUID, principal: Principal
) -> dict[str, Any] | None:
    apertura = registro.abierta_por(pieza_id, excepto=principal.usuario_id)
    if apertura is None:
        return None
    return {
        "nombre": nombre_de_persona(request.app.state.bd, apertura.persona_id),
        "desde": momento(apertura.abierta_el),
    }


@router.post("/piezas/{pieza_id}/reproceso", status_code=202)
def reprocesar_pieza(
    pieza_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    ingesta: Annotated[Ingesta, Depends(ingesta_de_piezas)],
) -> dict[str, Any]:
    """Reencola el procesamiento de una Pieza `Recibida` cuyo trabajo falló.

    Solo desde `Recibida`: una Pieza que ya tiene Ficha propuesta no se reprocesa por su
    cuenta, para no pisar lo que una persona pudo haber empezado a corregir (invariante 8,
    ADR-0004). La reintenta quien la subió o el RGC (403 si no). Nunca fuerza el tope de 6:
    eso es solo por comando (R-30, DEC-137).
    """
    try:
        detalle = ingesta.reprocesar(
            pieza_id, por=principal.actor, puede_ajena=puede_reprocesar_ajena(principal.rol)
        )
    except TransicionInvalida as error:
        # No está `Recibida`, o su trabajo sigue activo o completado: alguien lo reprocesó dos
        # veces, o pidió reproceso mientras el trabajo todavía corría. El índice único de
        # trabajo activo es la misma invariante del lado de la base (ADR-0004).
        recibida = error.estado_actual == Estado.RECIBIDA.value
        raise Problema(
            tipo="/errores/transicion-invalida",
            titulo="La Pieza no admite reproceso",
            estado=409,
            detalle=(
                "Su procesamiento sigue en curso o ya terminó: no hay nada que reintentar."
                if recibida
                else f"La Pieza ya fue procesada: está {error.estado_actual}."
            ),
            extensiones={"estado_actual": error.estado_actual},
        ) from error
    except TopeDeReprocesos as error:
        raise Problema(
            tipo="/errores/tope-de-reprocesos",
            titulo="Se alcanzó el tope de reprocesos",
            estado=409,
            detalle=str(error),
            extensiones={"reprocesos": error.reprocesos, "tope": error.tope},
        ) from error
    pieza = detalle.fila
    return {
        "pieza_id": str(pieza.pieza_id),
        "estado": pieza.estado.value,
        "nombre_archivo": pieza.nombre_archivo,
        "creada_el": pieza.creada_el.isoformat(),
        # Se encola igual: con el freno puesto espera al motor, no falla (DEC-146 punto 16).
        "procesamiento_pausado": not habilitado_en(request.app.state.bd, Freno.PROCESAMIENTO),
    }


@router.post("/piezas/{pieza_id}/completar-a-mano")
def completar_a_mano(
    pieza_id: UUID,
    principal: Annotated[Principal, Depends(exigir(Permiso.CARGAR))],
    ingesta: Annotated[Ingesta, Depends(ingesta_de_piezas)],
) -> dict[str, Any]:
    """Pasa una `Recibida` cuyo último procesamiento falló a `Pendiente`, con la Ficha v1
    `sistema` vacía y «Requiere análisis humano» (DEC-122). La completa quien la subió o el RGC
    (403 si no)."""
    try:
        ficha = ingesta.completar_a_mano(
            pieza_id, por=principal.actor, puede_ajena=puede_reprocesar_ajena(principal.rol)
        )
    except TransicionInvalida as error:
        raise Problema(
            tipo="/errores/transicion-invalida",
            titulo="La Pieza no es una Recibida fallida",
            estado=409,
            detalle="Solo se completa a mano una Pieza Recibida cuyo procesamiento falló.",
            extensiones={"estado_actual": error.estado_actual},
        ) from error
    return {
        "pieza_id": str(pieza_id),
        "estado": ficha.campos.estado.value,
        "requiere_analisis_humano": True,
        "version": ficha.version,
        "origen_version": ficha.origen_version,
    }


@router.get("/piezas/{pieza_id}/original")
def obtener_original(
    pieza_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    originales: Annotated[AlmacenDeOriginales, Depends(almacen_de_originales)],
    descarga: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    """Sirve el original, entero o por el tramo que pida el visor (US 10, US 11, US 114).

    El visor de PDF y el reproductor de video leen de a rangos: sin `Range`, un original de
    hasta 2 GiB viaja completo para mostrar una página o saltar a un minuto. Con
    `?descarga=1` va como adjunto (el enlace «Descargar el original»). Sin él, de un HEIC va
    su JPEG convertido, que es lo que el navegador muestra (153); la descarga es el original.
    """
    visible_para(principal, fichas.obtener_pieza(pieza_id))
    respuesta = originales.abrir(
        fichas.obtener_original(pieza_id),
        request.headers.get("range"),
        si_rango=request.headers.get("if-range"),
        descarga=descarga,
        convertido=not descarga,
    )
    return StreamingResponse(
        respuesta.bloques, status_code=respuesta.estado, headers=respuesta.cabeceras
    )


@router.get("/piezas/{pieza_id}/planilla")
def obtener_planilla(
    pieza_id: UUID,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
) -> dict[str, Any]:
    """Las hojas del visor de planilla, desde la v1 sin Contexto: lo que extrajo el sistema y
    no lo que editó quien valida (historia 113). Las celdas van como texto (DEC-45)."""
    # R-05 antes que el 409: quien solo consulta no se entera de que existe una Recibida.
    detalle = visible_para(principal, fichas.obtener_pieza(pieza_id))
    if detalle.fila.estado is Estado.RECIBIDA:
        raise TransicionInvalida(pieza_id, detalle.fila.estado.value, Estado.PENDIENTE.value)
    if detalle.clase_original != ClaseDeOriginal.PLANILLA:
        raise PiezaNoEncontrada(pieza_id)
    cuerpo = CuerpoDeFicha.partir(fichas.obtener_propuesta(pieza_id).cuerpo).principal
    return {
        "avisos": CuerpoDeFicha.avisos_de_planilla(cuerpo),
        "hojas": [
            {
                "nombre": hoja.marca.nombre,
                "desde": hoja.marca.desde,
                "hasta": hoja.marca.hasta,
                "total": hoja.marca.total,
                "nota": hoja.marca.anotacion,
                "columnas": hoja.columnas,
                "filas": hoja.filas,
            }
            for hoja in CuerpoDeFicha.parsear_cuerpo(cuerpo)
        ],
    }
