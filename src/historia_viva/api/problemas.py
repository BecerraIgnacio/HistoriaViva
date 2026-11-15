"""Errores HTTP en formato RFC 9457 (`application/problem+json`).

El `type` es estable: es contra ese identificador que programan los tests y la pantalla, no
contra el texto. `title` y `detail` están en español porque se muestran a la persona.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from historia_viva.bd import BaseNoDisponible
from historia_viva.busqueda import BuscadorNoDisponible, IndiceNoDisponible
from historia_viva.catalogos import ValorFueraDeCatalogo
from historia_viva.conversacion import (
    ConversacionNoEncontrada,
    LimiteDeConsultas,
    MensajeNoEncontrado,
    TurnoEnCurso,
    TurnoNoReintentable,
)
from historia_viva.efemerides import (
    EfemerideDesactualizada,
    EfemerideExistente,
    EfemerideNoEncontrada,
    EfemerideNoPropuesta,
    PedidoPendiente,
    RelatoNoEncontrado,
    RelatoNoPropuesto,
    RelatoYaPropuesto,
    RespuestaNoProponible,
)
from historia_viva.dominio.errores import (
    ConfiguracionInvalida,
    ConsultaInvalida,
    ContextoCongelado,
    LimiteExcedido,
    LoteLleno,
    LoteNoEncontrado,
    PermisoInsuficiente,
    PiezaDuplicada,
    PiezaNoEncontrada,
    TransicionInvalida,
)
from historia_viva.identidad import PersonaNoEncontrada, RechazoDelPadron, UltimoTitularDePermiso
from historia_viva.informes import AprobacionRechazada, InformeNoAprobado, InformeNoEncontrado
from historia_viva.ingesta import (
    AnalisisDeVideoNoDisponible,
    DemasiadasSubidas,
    VideoDemasiadoLargo,
)
from historia_viva.originales import (
    AlmacenamientoInsuficiente,
    ArchivoDemasiadoGrande,
    ArchivoIlegible,
    OriginalNoDisponible,
    TipoNoAdmitido,
)

__all__ = ["Problema", "registrar_manejadores", "respuesta"]

TIPO_MIME = "application/problem+json"

_MENSAJES_VALIDACION = {
    "missing": "Falta el campo {campo}.",
    "extra_forbidden": "El campo {campo} no está admitido.",
    "uuid_parsing": "El campo {campo} no es un identificador válido.",
    "uuid_type": "El campo {campo} no es un identificador válido.",
}


class Problema(Exception):
    """Un error que llega al borde HTTP con forma de RFC 9457."""

    def __init__(
        self,
        *,
        tipo: str,
        titulo: str,
        estado: int,
        detalle: str,
        campo: str | None = None,
        extensiones: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detalle)
        self.tipo = tipo
        self.titulo = titulo
        self.estado = estado
        self.detalle = detalle
        self.campo = campo
        self.extensiones = extensiones or {}


def respuesta(problema: Problema) -> JSONResponse:
    cuerpo: dict[str, Any] = {
        "type": problema.tipo,
        "title": problema.titulo,
        "status": problema.estado,
        "detail": problema.detalle,
    } | problema.extensiones
    if problema.campo is not None:
        cuerpo["campo"] = problema.campo
    return JSONResponse(cuerpo, status_code=problema.estado, media_type=TIPO_MIME)


async def _manejar_problema(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, Problema)
    return respuesta(error)


async def _manejar_pedido_invalido(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    primero = error.errors()[0] if error.errors() else {}
    ubicacion = [str(parte) for parte in primero.get("loc", ()) if parte != "body"]
    campo = ".".join(ubicacion) or None
    plantilla = _MENSAJES_VALIDACION.get(
        str(primero.get("type")), "El campo {campo} no tiene un valor válido."
    )
    detalle = (
        plantilla.format(campo=campo) if campo else "El cuerpo del pedido no es válido."
    )
    return respuesta(
        Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle=detalle,
            campo=campo,
        )
    )


async def _manejar_pieza_no_encontrada(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/pieza-no-encontrada",
            titulo="No existe la Pieza",
            estado=404,
            detalle=str(error),
        )
    )


async def _manejar_lote_no_encontrado(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/lote-no-encontrado",
            titulo="No existe el Lote",
            estado=404,
            detalle=str(error),
        )
    )


async def _manejar_conversacion_no_encontrada(_: Request, error: Exception) -> JSONResponse:
    # Inexistente o de otra persona, con el mismo cuerpo: no se revela que existe (R-31).
    return respuesta(
        Problema(
            tipo="/errores/conversacion-no-encontrada",
            titulo="La Conversación no existe",
            estado=404,
            detalle="No existe la Conversación.",
        )
    )


async def _manejar_efemeride_no_encontrada(_: Request, error: Exception) -> JSONResponse:
    # Inexistente o no visible con solo CONSULTAR, con el mismo cuerpo (R-05).
    return respuesta(
        Problema(
            tipo="/errores/recurso-no-encontrado",
            titulo="No existe",
            estado=404,
            detalle="No existe la efeméride.",
            extensiones={"recurso": "efemeride"},
        )
    )


async def _manejar_relato_no_encontrado(_: Request, error: Exception) -> JSONResponse:
    # Inexistente o no visible con solo CONSULTAR, con el mismo cuerpo (R-05).
    return respuesta(
        Problema(
            tipo="/errores/recurso-no-encontrado",
            titulo="No existe",
            estado=404,
            detalle="No existe el relato.",
            extensiones={"recurso": "relato"},
        )
    )


async def _manejar_mensaje_no_encontrado(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/recurso-no-encontrado",
            titulo="No existe",
            estado=404,
            detalle="No existe ese mensaje en la Conversación.",
            extensiones={"recurso": "mensaje"},
        )
    )


async def _manejar_respuesta_no_proponible(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RespuestaNoProponible)
    return respuesta(
        Problema(
            tipo="/errores/respuesta-no-proponible",
            titulo="Esa respuesta no puede proponerse como relato",
            estado=409,
            detalle=str(error),
            extensiones={"estado_actual": error.estado_actual},
        )
    )


async def _manejar_relato_ya_propuesto(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RelatoYaPropuesto)
    return respuesta(
        Problema(
            tipo="/errores/relato-ya-propuesto",
            titulo="Esa respuesta ya se propuso como relato",
            estado=409,
            detalle="Ya hay un Relato de esa respuesta en revisión.",
            extensiones={"relato_id": str(error.relato_id)},
        )
    )


async def _manejar_efemeride_existente(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, EfemerideExistente)
    return respuesta(
        Problema(
            tipo="/errores/efemeride-existente",
            titulo="Ya existe una efeméride de este día",
            estado=409,
            detalle=str(error),
            extensiones={"efemeride_id": str(error.efemeride_id)},
        )
    )


async def _manejar_efemeride_no_propuesta(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, EfemerideNoPropuesta)
    return respuesta(
        Problema(
            tipo="/errores/efemeride-no-propuesta",
            titulo="La Efeméride ya no está Propuesta",
            estado=409,
            detalle=str(error),
            extensiones={"estado_actual": error.estado.value.capitalize()},
        )
    )


async def _manejar_relato_no_propuesto(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RelatoNoPropuesto)
    return respuesta(
        Problema(
            tipo="/errores/relato-no-propuesto",
            titulo="El Relato ya no está Propuesto",
            estado=409,
            detalle=str(error),
            extensiones={"estado_actual": error.estado.value.capitalize()},
        )
    )


async def _manejar_efemeride_desactualizada(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, EfemerideDesactualizada)
    return respuesta(
        Problema(
            tipo="/errores/version-desactualizada",
            titulo=f"{error.cual} cambió durante la revisión",
            estado=409,
            detalle=str(error),
            extensiones={
                "version_base": error.version_base,
                "version_actual": error.version_actual,
            },
        )
    )


async def _manejar_pedido_pendiente(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, PedidoPendiente)
    return respuesta(
        Problema(
            tipo="/errores/pedido-pendiente",
            titulo="Ya hay un pedido de generación sin atender",
            estado=409,
            detalle=str(error),
            extensiones={"pedido_pendiente": error.pendiente},
        )
    )


async def _manejar_informe_no_encontrado(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/recurso-no-encontrado",
            titulo="No existe",
            estado=404,
            detalle="No existe ese informe.",
            extensiones={"recurso": "informe"},
        )
    )


async def _manejar_informe_no_aprobado(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, InformeNoAprobado)
    return respuesta(
        Problema(
            tipo="/errores/informe-no-aprobado",
            titulo="El Informe no está aprobado",
            estado=409,
            detalle=str(error),
            extensiones={"estado_actual": error.estado_actual},
        )
    )


_TITULOS_DE_APROBACION = {
    "aprobador-repetido": "Ya aprobaste este informe",
    "informe-ya-aprobado-por-rol": "Ese rol ya aprobó el informe",
    "transicion-invalida": "El informe ya no es un Borrador vigente",
    "informe-desactualizado": "El Informe quedó desactualizado",
}


async def _manejar_aprobacion_rechazada(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, AprobacionRechazada)
    return respuesta(
        Problema(
            tipo=f"/errores/{error.codigo}",
            titulo=_TITULOS_DE_APROBACION[error.codigo],
            estado=409,
            detalle=str(error),
        )
    )


async def _manejar_turno_en_curso(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/turno-en-curso",
            titulo="Ya hay un turno respondiendo",
            estado=409,
            detalle="Esperá a que termine la respuesta anterior de esta conversación.",
        )
    )


async def _manejar_limite_de_consultas(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, LimiteDeConsultas)
    problema = respuesta(
        Problema(
            tipo="/errores/limite-de-consultas",
            titulo="Demasiadas consultas",
            estado=429,
            detalle="Llegaste al tope de preguntas. Probá de nuevo en unos minutos.",
        )
    )
    problema.headers["Retry-After"] = str(error.retry_after_s)
    return problema


async def _manejar_turno_no_reintentable(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/turno-no-reintentable",
            titulo="Ese turno no se puede reintentar",
            estado=409,
            detalle="Solo se reintenta la última respuesta de la conversación, si falló o se cortó.",
        )
    )


async def _manejar_lote_lleno(_: Request, error: Exception) -> JSONResponse:
    # Al cargar es 422; al restaurar una retirada, 409 (DEC-138): eso lo traduce 106.
    assert isinstance(error, LoteLleno)
    return respuesta(
        Problema(
            tipo="/errores/lote-lleno",
            titulo="El Lote llegó a su tope",
            estado=422,
            detalle=str(error),
            extensiones={"tope": error.tope},
        )
    )


async def _manejar_contexto_congelado(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ContextoCongelado)
    return respuesta(
        Problema(
            tipo="/errores/contexto-congelado",
            titulo="El Contexto del Lote ya no se puede editar",
            estado=409,
            detalle=str(error),
            extensiones={"piezas": error.piezas},
        )
    )


async def _manejar_permiso_insuficiente(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/permiso-insuficiente",
            titulo="No tenés permiso para esto",
            estado=403,
            detalle=str(error),
        )
    )


async def _manejar_pieza_duplicada(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, PiezaDuplicada)
    return respuesta(
        Problema(
            tipo="/errores/pieza-duplicada",
            titulo="La Pieza ya está cargada",
            estado=409,
            detalle=str(error),
            extensiones={
                "pieza_id": str(error.pieza_id),
                "lote_id": None if error.lote_id is None else str(error.lote_id),
                "estado": error.estado,
            },
        )
    )


_RECHAZOS_DEL_PADRON: dict[str, tuple[int, str]] = {
    "persona-existente": (409, "La persona ya existe"),
    "dominio-no-permitido": (422, "El correo no es del dominio permitido"),
    "ultimo-titular-de-permiso": (409, "No se puede dejar el permiso sin titular"),
    "recurso-no-encontrado": (404, "No existe"),
}
"""Estado y título de cada `RechazoDelPadron` (catálogo de errores del spec)."""


async def _manejar_rechazo_del_padron(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RechazoDelPadron)
    estado, titulo = _RECHAZOS_DEL_PADRON[error.codigo]
    extensiones: dict[str, Any] = {}
    if isinstance(error, UltimoTitularDePermiso):
        extensiones["permiso"] = error.permiso.value
    if isinstance(error, PersonaNoEncontrada):
        extensiones["recurso"] = "persona"
    return respuesta(
        Problema(
            tipo=f"/errores/{error.codigo}",
            titulo=titulo,
            estado=estado,
            detalle=str(error),
            campo="email" if error.codigo == "dominio-no-permitido" else None,
            extensiones=extensiones,
        )
    )


async def _manejar_transicion_invalida(_: Request, error: Exception) -> JSONResponse:
    # Lo único que hoy llega al borde HTTP es guardar o abrir una Ficha que todavía no
    # existe: la Pieza sigue `Recibida` y su procesamiento no terminó. Las transiciones del
    # procesamiento las maneja el trabajador y nunca salen por acá.
    return respuesta(
        Problema(
            tipo="/errores/transicion-invalida",
            titulo="La Ficha todavía no existe",
            estado=409,
            detalle=str(error),
        )
    )


async def _manejar_no_disponible(_: Request, error: Exception) -> JSONResponse:
    base = isinstance(error, BaseNoDisponible)
    problema = respuesta(
        Problema(
            tipo="/errores/servicio-no-disponible",
            titulo="El servicio no está disponible",
            estado=503,
            detalle="La base de datos no responde." if base else str(error),
            # Sin `ffprobe` reintentar no sirve: la pantalla lo distingue de la base caída (083).
            extensiones=(
                {"motivo": "sin-ffprobe"} if isinstance(error, AnalisisDeVideoNoDisponible) else None
            ),
        )
    )
    if base:
        problema.headers["Retry-After"] = "30"  # DEC-78: base o motor caído
    return problema


async def _manejar_tipo_no_admitido(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, TipoNoAdmitido)
    extensiones: dict[str, Any] = {"admitidos": list(error.admitidos)}
    if error.motivo is not None:
        extensiones["motivo"] = error.motivo
    return respuesta(
        Problema(
            tipo="/errores/tipo-no-admitido",
            titulo="Tipo de archivo no admitido",
            estado=415,
            detalle=str(error),
            extensiones=extensiones,
        )
    )


async def _manejar_archivo_demasiado_grande(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ArchivoDemasiadoGrande)
    return respuesta(
        Problema(
            tipo="/errores/archivo-demasiado-grande",
            titulo="El archivo es demasiado grande",
            estado=413,
            detalle=str(error),
            extensiones={"tope_bytes": error.tope_bytes, "clase_original": error.clase},
        )
    )


async def _manejar_archivo_ilegible(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ArchivoIlegible)
    return respuesta(
        Problema(
            tipo="/errores/archivo-ilegible",
            titulo="No se pudo leer el archivo",
            estado=422,
            detalle=str(error),
            extensiones={"motivo": error.motivo},
        )
    )


async def _manejar_video_demasiado_largo(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, VideoDemasiadoLargo)
    return respuesta(
        Problema(
            tipo="/errores/video-demasiado-largo",
            titulo="El video dura más de lo permitido",
            estado=422,
            detalle=str(error),
            extensiones={"tope_segundos": error.tope_segundos},
        )
    )


async def _manejar_demasiadas_subidas(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, DemasiadasSubidas)
    problema = respuesta(
        Problema(
            tipo="/errores/demasiadas-subidas",
            titulo="Demasiadas subidas a la vez",
            estado=429,
            detalle=str(error),
            extensiones={"maximo": error.maximo},
        )
    )
    # shortcut: un valor fijo; un video tarda minutos, así que la pantalla vuelve a la fila.
    problema.headers["Retry-After"] = "30"
    return problema


async def _manejar_almacenamiento_insuficiente(_: Request, error: Exception) -> JSONResponse:
    return respuesta(
        Problema(
            tipo="/errores/almacenamiento-insuficiente",
            titulo="No hay espacio para guardar el archivo",
            estado=507,
            detalle=str(error),
        )
    )


async def _manejar_original_no_disponible(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, OriginalNoDisponible)
    return respuesta(
        Problema(
            tipo="/errores/original-no-disponible",
            titulo="El original no está disponible",
            estado=404,
            detalle=str(error),
            extensiones={"archivo_original": error.archivo_original},
        )
    )


async def _manejar_configuracion_invalida(_: Request, error: Exception) -> JSONResponse:
    # Una fila editada por SQL con la aplicación en marcha: el trabajo no nace (DEC-87).
    assert isinstance(error, ConfiguracionInvalida)
    return respuesta(
        Problema(
            tipo="/errores/configuracion-invalida",
            titulo="La configuración no es válida",
            estado=422,
            detalle=str(error),
            extensiones={"errores": [e._asdict() for e in error.errores]},
        )
    )


async def _manejar_fuera_de_catalogo(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ValorFueraDeCatalogo)
    return respuesta(
        Problema(
            tipo=f"/errores/{error.codigo}",
            titulo=error.titulo,
            estado=422,
            detalle=str(error),
            campo=error.campo,
        )
    )


async def _manejar_consulta_invalida(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ConsultaInvalida)
    return respuesta(
        Problema(
            tipo="/errores/pedido-invalido",
            titulo="El pedido es inválido",
            estado=422,
            detalle=str(error),
            campo=error.campo,
        )
    )


async def _manejar_buscador_no_disponible(_: Request, error: Exception) -> JSONResponse:
    problema = respuesta(
        Problema(
            tipo="/errores/capa-modelos-no-disponible",
            titulo="La lectura automática no está disponible",
            estado=503,
            detalle=f"No se pudo leer la consulta: {error}",
        )
    )
    problema.headers["Retry-After"] = "30"  # DEC-78: base o motor caído
    return problema


async def _manejar_indice_no_disponible(_: Request, error: Exception) -> JSONResponse:
    problema = respuesta(
        Problema(
            tipo="/errores/indice-no-disponible",
            titulo="El índice no está disponible",
            estado=503,
            detalle=str(error),
        )
    )
    problema.headers["Retry-After"] = "30"
    return problema


async def _manejar_limite_excedido(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, LimiteExcedido)
    if error.limite != "tiempo":
        raise error  # otro tope (tokens) no es un 504: sigue como antes, sin manejador propio
    return respuesta(
        Problema(
            tipo="/errores/tiempo-agotado",
            titulo="La operación tardó más de lo permitido",
            estado=504,
            detalle=str(error),
            # El tope de tiempo de la fila está en segundos (`timeout_s`).
            extensiones={"capacidad": error.capacidad, "tope_ms": error.tope * 1000},
        )
    )


async def _manejar_http(request: Request, error: Exception) -> Response:
    # El 404 de Starlette es una ruta que no existe o se retiró; el 405, un verbo sin handler
    # (también el `OPTIONS` entre orígenes, sin CORS); el resto, como lo da FastAPI.
    assert isinstance(error, HTTPException)
    if error.status_code == 404:
        return respuesta(
            Problema(
                tipo="/errores/ruta-inexistente",
                titulo="No existe",
                estado=404,
                detalle="La ruta no existe.",
            )
        )
    if error.status_code == 405:
        rechazo = respuesta(
            Problema(
                tipo="/errores/metodo-no-permitido",
                titulo="Método no permitido",
                estado=405,
                detalle="La ruta no admite ese método.",
            )
        )
        if error.headers and "Allow" in error.headers:
            rechazo.headers["Allow"] = error.headers["Allow"]
        return rechazo
    return await http_exception_handler(request, error)


def registrar_manejadores(app: FastAPI) -> None:
    app.add_exception_handler(Problema, _manejar_problema)
    app.add_exception_handler(HTTPException, _manejar_http)
    app.add_exception_handler(RequestValidationError, _manejar_pedido_invalido)
    app.add_exception_handler(PiezaNoEncontrada, _manejar_pieza_no_encontrada)
    app.add_exception_handler(PiezaDuplicada, _manejar_pieza_duplicada)
    app.add_exception_handler(LoteNoEncontrado, _manejar_lote_no_encontrado)
    app.add_exception_handler(LoteLleno, _manejar_lote_lleno)
    app.add_exception_handler(ContextoCongelado, _manejar_contexto_congelado)
    app.add_exception_handler(ConversacionNoEncontrada, _manejar_conversacion_no_encontrada)
    app.add_exception_handler(TurnoEnCurso, _manejar_turno_en_curso)
    app.add_exception_handler(TurnoNoReintentable, _manejar_turno_no_reintentable)
    app.add_exception_handler(LimiteDeConsultas, _manejar_limite_de_consultas)
    app.add_exception_handler(EfemerideNoEncontrada, _manejar_efemeride_no_encontrada)
    app.add_exception_handler(EfemerideExistente, _manejar_efemeride_existente)
    app.add_exception_handler(EfemerideNoPropuesta, _manejar_efemeride_no_propuesta)
    app.add_exception_handler(EfemerideDesactualizada, _manejar_efemeride_desactualizada)
    app.add_exception_handler(PedidoPendiente, _manejar_pedido_pendiente)
    app.add_exception_handler(RelatoNoEncontrado, _manejar_relato_no_encontrado)
    app.add_exception_handler(MensajeNoEncontrado, _manejar_mensaje_no_encontrado)
    app.add_exception_handler(RespuestaNoProponible, _manejar_respuesta_no_proponible)
    app.add_exception_handler(RelatoYaPropuesto, _manejar_relato_ya_propuesto)
    app.add_exception_handler(RelatoNoPropuesto, _manejar_relato_no_propuesto)
    app.add_exception_handler(InformeNoEncontrado, _manejar_informe_no_encontrado)
    app.add_exception_handler(AprobacionRechazada, _manejar_aprobacion_rechazada)
    app.add_exception_handler(InformeNoAprobado, _manejar_informe_no_aprobado)
    app.add_exception_handler(PermisoInsuficiente, _manejar_permiso_insuficiente)
    app.add_exception_handler(RechazoDelPadron, _manejar_rechazo_del_padron)
    app.add_exception_handler(TransicionInvalida, _manejar_transicion_invalida)
    app.add_exception_handler(BaseNoDisponible, _manejar_no_disponible)
    app.add_exception_handler(TipoNoAdmitido, _manejar_tipo_no_admitido)
    app.add_exception_handler(ArchivoDemasiadoGrande, _manejar_archivo_demasiado_grande)
    app.add_exception_handler(ArchivoIlegible, _manejar_archivo_ilegible)
    app.add_exception_handler(VideoDemasiadoLargo, _manejar_video_demasiado_largo)
    app.add_exception_handler(AnalisisDeVideoNoDisponible, _manejar_no_disponible)
    app.add_exception_handler(DemasiadasSubidas, _manejar_demasiadas_subidas)
    app.add_exception_handler(AlmacenamientoInsuficiente, _manejar_almacenamiento_insuficiente)
    app.add_exception_handler(OriginalNoDisponible, _manejar_original_no_disponible)
    app.add_exception_handler(ConfiguracionInvalida, _manejar_configuracion_invalida)
    app.add_exception_handler(ValorFueraDeCatalogo, _manejar_fuera_de_catalogo)
    app.add_exception_handler(ConsultaInvalida, _manejar_consulta_invalida)
    app.add_exception_handler(BuscadorNoDisponible, _manejar_buscador_no_disponible)
    app.add_exception_handler(IndiceNoDisponible, _manejar_indice_no_disponible)
    app.add_exception_handler(LimiteExcedido, _manejar_limite_excedido)
