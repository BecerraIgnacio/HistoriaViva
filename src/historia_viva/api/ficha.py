"""La Ficha de una Pieza: todo lo que la pantalla partida necesita en una sola respuesta.

Además del frontmatter y el cuerpo viajan `obligatorios` y `faltantes` —qué hace falta para
validar, **desde el principio** y no recién al guardar (user story 16)— y `propuesta`, la
versión 1 tal como la escribió el modelo, que es contra lo que la pantalla marca qué cambió
y contra lo que la instrumentación mide si el modelo ayuda o estorba (user story 23).

Con los controles de datos personales encendidos (125, C31) viajan también `datos_personales`,
el aviso de DNI, CUIL o CBU del texto (no bloquea ni se guarda), `publicable` y
`puede_marcar_publicable`. Apagados, esas claves no están y `publicable` en el pedido es un 409.
"""

from __future__ import annotations

from dataclasses import fields, replace
from functools import lru_cache
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, field_validator

from historia_viva.api.dependencias import (
    almacen_de_originales,
    cola_de_trabajos,
    ingesta_de_piezas,
    modulo_de_fichas,
    registro_de_mediciones,
    validacion_de_fichas,
    visible_para,
)
from historia_viva.api.mediciones import momento
from historia_viva.api.problemas import Problema
from historia_viva.api.sesion import exigir, nombre_de_persona
from historia_viva.capa_modelos import imagenes
from historia_viva.dominio.afirmaciones import (
    UbicacionDeImagen,
    AVISO_DESCRIPCION_FALLIDA,
    LECTURA_AUTOMATICA,
    Afirmacion,
    DescripcionPropuesta,
    EstadoAfirmacion,
    EstadoDeImagen,
    ImagenDescribible,
)
from historia_viva.dominio.cruce import confirmacion_para_aceptar
from historia_viva.dominio.cuerpo import CuerpoDeFicha, tiene_afirmaciones
from historia_viva.dominio.errores import (
    AfirmacionNoEncontrada,
    ImagenNoEncontrada,
    TransicionInvalida,
    VersionDesactualizada,
)
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.permisos import Permiso
from historia_viva.dominio.tipos import (
    CAMPOS_OBLIGATORIOS,
    CamposDuros,
    CamposFicha,
    EdicionFicha,
    FichaVigente,
    OriginalGuardado,
    procedencia_de_campos,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal
from historia_viva.ingesta import Ingesta
from historia_viva.instrumentacion import RegistroMediciones, medir_cierre
from historia_viva.interruptor import Freno, habilitado_en
from historia_viva.originales import (
    AlmacenDeOriginales,
    ConversionFallida,
    OriginalNoDisponible,
    RechazoDeAdmision,
)
from historia_viva.procesamiento.cola import Cola, EstadoDeTrabajo
from historia_viva.validacion import ControlesDeDatosPersonalesApagados, Validacion

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Ficha"])


class PedidoEdicion(BaseModel):
    """Lo que la pantalla partida manda al guardar.

    `extra="forbid"` no es una precaución de estilo: es lo que hace que `estado`,
    `validada_por`, `validada_el`, `modo_validacion`, `id`, `lote` y `archivo_original`
    reboten con un `422` en vez de colarse. Que la pantalla no pueda firmar una Ficha por su
    cuenta es estructural, no una regla escrita en un comentario (ADR-0002).
    """

    model_config = ConfigDict(extra="forbid")

    version_base: int
    """La `version` de la Ficha que la pantalla abrió: si otra persona guardó después, 409."""

    medicion_id: UUID | None = None
    """El que devolvió `POST /api/piezas/{id}/apertura`. Cierra el reloj de la validación.
    Nulo solo si la Ficha se guarda sin haber pasado por la pantalla partida."""

    tipo: str | None = None
    fecha_inicio: date | None = None
    precision_fecha: PrecisionFecha | None = None
    fecha_fin: date | None = None
    """Solo con `precision_fecha: rango`. Con las otras cuatro el fin se deriva."""

    origen: str | None = None
    tipo_actividad: str | None = None
    propietario: str | None = None
    autor: str | None = None
    """Opcional; «Desconocido» vale (DEC-126)."""
    carreras: tuple[str, ...] = ()
    """`carrera_id` de `GET /api/carreras`, la lista entera: la que falta se quita."""
    resumen: str | None = None
    publicable: bool | None = None
    """Solo el RGC y con los controles de C31 encendidos (125); ausente, queda el que estaba."""
    cuerpo: str
    contexto: str

    @field_validator("tipo", "tipo_actividad", "origen", "propietario", "autor", "resumen")
    @classmethod
    def _blanco_es_vacio(cls, valor: str | None) -> str | None:
        # Un obligatorio con solo espacios no completa nada: deja la Ficha Incompleta (RF-14).
        return None if valor is None else valor.strip() or None

    @field_validator("cuerpo")
    @classmethod
    def _sin_encabezado_de_contexto(cls, valor: str) -> str:
        CuerpoDeFicha(valor, "")  # Rechaza la línea `## Contexto`: el Contexto viaja aparte.
        return valor


class PedidoEnLectura(BaseModel):
    """Lo que la pantalla partida manda con la Pieza Recibida: los campos duros y el Contexto
    (152). Sin `version_base`, cuerpo ni resumen, que todavía no existen; `extra="forbid"`
    rebota cualquier otro campo, también los de la firma (ADR-0002)."""

    model_config = ConfigDict(extra="forbid")

    tipo: str | None = None
    tipo_actividad: str | None = None
    fecha_inicio: date | None = None
    precision_fecha: PrecisionFecha | None = None
    fecha_fin: date | None = None
    origen: str | None = None
    propietario: str | None = None
    autor: str | None = None
    carreras: tuple[str, ...] = ()
    contexto: str = ""

    @field_validator("tipo", "tipo_actividad", "origen", "propietario", "autor")
    @classmethod
    def _blanco_es_vacio(cls, valor: str | None) -> str | None:
        return None if valor is None else valor.strip() or None


def _valor(valor: object) -> Any:
    if isinstance(valor, Enum):
        return valor.value
    if isinstance(valor, UUID):
        return str(valor)
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    return valor


def frontmatter(campos: CamposFicha) -> dict[str, Any]:
    """El frontmatter como JSON.

    Los campos y su orden salen de `CamposFicha`, que ya está en el orden de `spec.md` §4:
    agregar un campo al frontmatter no puede exigir acordarse de esta lista.
    """
    return {campo.name: _valor(getattr(campos, campo.name)) for campo in fields(campos)}


def faltantes(campos: CamposFicha) -> list[str]:
    """Cuáles de los obligatorios están vacíos ahora mismo."""
    return [campo for campo in CAMPOS_OBLIGATORIOS if getattr(campos, campo) is None]


def _contenido(ficha: FichaVigente) -> dict[str, Any]:
    cuerpo = CuerpoDeFicha.partir(ficha.cuerpo)
    return {
        "frontmatter": frontmatter(ficha.campos),
        "cuerpo": cuerpo.principal,
        "contexto": cuerpo.contexto,
    }


def respuesta_ficha(
    vigente: FichaVigente,
    propuesta: FichaVigente,
    heredados: tuple[str, ...],
    de_la_persona: tuple[str, ...] = (),
    requiere_analisis_humano: bool = False,
    resumenes_rehechos: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "pieza_id": str(vigente.pieza_id),
        "ficha_id": str(vigente.ficha_id),
        "version": vigente.version,
        "origen_version": vigente.origen_version,
        # `dev`: la pantalla marca «Firma de ensayo, identidad no verificada» (DEC-139).
        "metodo_autenticacion": _valor(vigente.metodo_autenticacion),
        # Derivada (DEC-122): ninguna versión la escribió un modelo ni una persona; la de la
        # herencia tardía del Lote es `sistema` y no cuenta (151). La calcula `fichas`.
        "requiere_analisis_humano": requiere_analisis_humano,
        **_contenido(vigente),
        "obligatorios": list(CAMPOS_OBLIGATORIOS),
        "faltantes": faltantes(vigente.campos),
        # Lo heredado del Lote no lo propuso el modelo: la pantalla lo marca aparte.
        "campos_heredados": list(heredados),
        # Lo que una persona escribió mientras se leía tampoco lo propuso el modelo (152).
        "campos_de_la_persona": list(de_la_persona),
        "procedencia": procedencia_de_campos(
            vigente.campos, propuesta.campos, heredados, de_la_persona, resumenes_rehechos
        ),
        "propuesta": _contenido(propuesta),
        # Contenido no confiable: la pantalla pinta los extractos con `textContent` (109).
        "informe_inyeccion": vigente.informe_inyeccion,
    }


def controles_de_datos_personales(
    validacion: Validacion, principal: Principal, ficha: FichaVigente
) -> dict[str, Any]:
    """El aviso de DNI, CUIL o CBU de lo que está guardado y si `principal` puede cambiar
    `publicable`; nada con los controles apagados (125)."""
    cuerpo = CuerpoDeFicha.partir(ficha.cuerpo)
    avisos = validacion.avisos_de_datos_personales(
        cuerpo=cuerpo.principal, contexto=cuerpo.contexto, resumen=ficha.campos.resumen
    )
    if avisos is None:
        return {}
    return {
        "datos_personales": [
            {"campo": campo, "tipo": h.tipo.value, "inicio": h.inicio, "fin": h.fin}
            for campo, h in avisos
        ],
        "publicable": ficha.campos.publicable,
        "puede_marcar_publicable": validacion.puede_marcar_publicable(principal),
    }


def _version_desactualizada(conflicto: VersionDesactualizada, request: Request) -> Problema:
    vigente = conflicto.vigente
    bd = request.app.state.bd
    nombre = nombre_de_persona(bd, vigente.editada_por)
    if vigente.origen_version == "persona":
        quien = f"{nombre or 'Otra persona'} guardó la versión {vigente.version}"
    elif vigente.origen_version == "sistema":
        # 151: lo que el Lote completó tarde llega a la Pendiente en una versión `sistema`.
        quien = f"La versión {vigente.version} trae lo que se completó en el Lote"
    else:
        autor = "del Agente" if vigente.origen_version == "agente_externo" else "del modelo"
        quien = f"La versión {vigente.version} es la propuesta {autor}"
    return Problema(
        tipo="/errores/version-desactualizada",
        titulo="La Ficha cambió mientras la editabas",
        estado=409,
        detalle=f"{quien}. Recargá la Ficha para ver sus cambios.",
        extensiones={
            "version_base": conflicto.version_base,
            "version_actual": vigente.version,
            "editada_por": nombre,
            "editada_el": momento(vigente.editada_el),
            # Extensión del catálogo: con una Validada, la pantalla avisa quién la firmó
            # (R11 E1).
            "estado": vigente.campos.estado.value,
            "validada_por": nombre_de_persona(bd, vigente.campos.validada_por),
            # Aplicar lo tuyo sobre la nueva firma con su alerta: la pantalla la repite (109).
            "ficha": {
                "version": vigente.version,
                **_contenido(vigente),
                "informe_inyeccion": vigente.informe_inyeccion,
            },
        },
    )


@router.get("/piezas/{pieza_id}/ficha")
def obtener_ficha(
    pieza_id: UUID,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    validacion: Annotated[Validacion, Depends(validacion_de_fichas)],
) -> dict[str, Any]:
    pieza = visible_para(principal, fichas.obtener_pieza(pieza_id))
    # Una Pieza todavía `Recibida` no tiene Ficha que abrir: el Módulo de Fichas levanta
    # `PiezaNoEncontrada` y el borde HTTP lo traduce a 404.
    vigente = fichas.obtener_ficha(pieza_id)
    return {
        **respuesta_ficha(
            vigente,
            fichas.obtener_propuesta(pieza_id),
            pieza.campos_heredados,
            pieza.campos_de_la_persona,
            pieza.fila.requiere_analisis_humano,
            tuple(r.texto for r in fichas.resumenes_rehechos(pieza_id)),
        ),
        **controles_de_datos_personales(validacion, principal, vigente),
    }


@router.put("/piezas/{pieza_id}/ficha")
def guardar_ficha(
    pieza_id: UUID,
    pedido: PedidoEdicion,
    request: Request,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    validacion: Annotated[Validacion, Depends(validacion_de_fichas)],
    registro: Annotated[RegistroMediciones, Depends(registro_de_mediciones)],
) -> dict[str, Any]:
    """Guarda lo validado, cierra el reloj de la medición y devuelve el estado en que quedó.

    Guardar siempre se permite: con los obligatorios completos la Ficha queda `Validada` y
    firmada (user stories 17 y 19); si falta un dato duro queda `Incompleta` y
    `campos_faltantes` dice cuál (user story 18).
    """
    try:
        # Lo firmado con una sesión `dev` es una firma de ensayo (DEC-139): el método es el
        # de la sesión de `principal`.
        resultado = validacion.guardar_ficha(
            pieza_id=pieza_id,
            version_base=pedido.version_base,
            edicion=EdicionFicha(**pedido.model_dump(exclude={"medicion_id", "version_base"})),
            principal=principal,
        )
    except VersionDesactualizada as conflicto:
        raise _version_desactualizada(conflicto, request) from conflicto
    except ControlesDeDatosPersonalesApagados as error:
        raise Problema(
            tipo="/errores/datos-personales-deshabilitados",
            titulo="«Publicable» todavía no se ofrece",
            estado=409,
            detalle=str(error),
            campo="publicable",
        ) from error
    except ValueError as error:
        # `derivar_rango` es la autoridad sobre la Fecha y explica en castellano qué está
        # mal; acá solo se le pone el código y el campo al que apunta la pantalla.
        raise Problema(
            tipo="/errores/fecha-invalida",
            titulo="La Fecha no es válida",
            estado=422,
            detalle=str(error),
            campo="fecha_fin",
        ) from error
    # La firma y el número de versión los escribió el Módulo de Fichas: se leen de la Ficha
    # que quedó vigente y no se recomponen acá, que es como empezarían a discrepar.
    vigente = resultado.ficha
    # La medición se cierra después de guardar y con la Ficha ya escrita en mano: la
    # propuesta se lee recién acá para que una Pieza todavía `Recibida` siga respondiendo el
    # 409 de "la Ficha todavía no existe" y no el 404 de "no hay versión 1 que leer".
    pieza = fichas.obtener_pieza(pieza_id)
    registro.cerrar(
        medicion_id=pedido.medicion_id,
        pieza_id=pieza_id,
        cierre=medir_cierre(
            propuesta=fichas.obtener_propuesta(pieza_id),
            guardada=vigente,
            resultado=resultado,
            # Ni lo del Lote ni lo escrito en Recibida lo propuso el modelo (M1, 152).
            heredados=(*pieza.campos_heredados, *pieza.campos_de_la_persona),
            metodo=principal.metodo,
        ),
    )
    return {
        "estado": resultado.estado_nuevo.value,
        "campos_faltantes": resultado.campos_faltantes,
        "pieza_id": str(pieza_id),
        "version": vigente.version,
        "validada_por": _valor(vigente.campos.validada_por),
        "validada_el": _valor(vigente.campos.validada_el),
        "modo_validacion": vigente.campos.modo_validacion,
        "metodo_autenticacion": _valor(vigente.metodo_autenticacion),
        # La alerta de lo que quedó guardado: con esto decide la pantalla al firmar después (109).
        "informe_inyeccion": vigente.informe_inyeccion,
        # Y el aviso de DNI, CUIL o CBU de lo guardado, con los controles encendidos (125).
        **controles_de_datos_personales(validacion, principal, vigente),
    }


class PedidoDecision(BaseModel):
    """Aceptar o rechazar una Afirmación con un clic (ADR-0007). Aceptar una que pide
    confirmación (`confirmar`, 145) exige `confirmada`, como la pantalla (SEG-21)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["aceptada", "rechazada"]
    confirmada: bool = False


def _afirmacion(afirmacion: Afirmacion, tipo_imagen: str | None) -> dict[str, Any]:
    """Con su cruce con el OCR (145): si está respaldada, dónde están sus datos en la imagen y,
    si aceptarla pide confirmación, por qué. Sin puntaje de confianza del modelo."""
    respaldada = afirmacion.respaldo is not None
    confirmar = confirmacion_para_aceptar(
        afirmacion.texto, respaldada=respaldada, tipo_imagen=tipo_imagen
    )
    return {
        "orden": afirmacion.orden,
        "texto": afirmacion.texto,
        "estado": afirmacion.estado.value,
        "decidida_por": _valor(afirmacion.decidida_por),
        "decidida_el": _valor(afirmacion.decidida_el),
        "respaldada": respaldada,
        "region": [r.a_json() for r in afirmacion.respaldo or ()] or None,
        "confirmar": None if confirmar is None else confirmar.value,
        "imagen": afirmacion.imagen,
    }


def _imagen(imagen: ImagenDescribible, trabajo: EstadoDeTrabajo | None) -> dict[str, Any]:
    estado = imagen.estado
    # Encolada, pero su trabajo falló sin dejar resultado (o el proceso cayó dos veces): se
    # puede volver a pedir. Lo sabe la cola, no la Ficha; la firma usa la misma regla (148).
    if estado is EstadoDeImagen.ENCOLADA and trabajo not in (
        EstadoDeTrabajo.ENCOLADO,
        EstadoDeTrabajo.EN_CURSO,
    ):
        estado = EstadoDeImagen.FALLIDA
    return {
        "imagen": imagen.imagen,
        "ubicacion": imagen.ubicacion.texto,
        "estado": estado.value,
        "tipo_imagen": imagen.tipo_imagen,
        "aviso": (
            AVISO_DESCRIPCION_FALLIDA
            if estado is EstadoDeImagen.FALLIDA
            or (estado is EstadoDeImagen.DESCRITA and imagen.tipo_imagen is None)
            else None
        ),
        "alerta_inyeccion": imagen.alerta_inyeccion,
    }


def _clases(
    descripcion: DescripcionPropuesta | None, embebidas: list[ImagenDescribible]
) -> dict[int, str | None]:
    """La clase de la imagen de cada Afirmación: la de la foto (0) o la de su imagen embebida."""
    clases = {i.imagen: i.tipo_imagen for i in embebidas}
    clases[0] = None if descripcion is None else descripcion.tipo_imagen
    return clases


@router.get("/piezas/{pieza_id}/afirmaciones")
def listar_afirmaciones(
    pieza_id: UUID,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    cola: Annotated[Cola, Depends(cola_de_trabajos)],
    request: Request,
) -> dict[str, Any]:
    """Las Afirmaciones de la descripción de una foto, con su decisión y quién la tomó; la clase
    de la imagen; el aviso si la respuesta del modelo no cumplió el esquema; y el texto visible
    con su Procedencia, «Lectura automática»: lo lee el OCR, no el modelo (ADR-0007). Sin nada
    de eso para una Pieza que no lo tiene (otra clase, Recibida o anterior a 144). Quien no
    valida ve solo las aceptadas y no quién las decidió: lo rechazado no se publica.

    A quien valida, además, el resumen rehecho sin las rechazadas (146): `estado` es
    `rehaciendo` mientras está en la cola, y si no, `listo` o `fallido`; `texto` es el último
    rehecho, la propuesta que la pantalla pone en el campo. `atendido`: la Ficha se guardó después
    de que llegó, así que la pantalla ya lo puso u ofreció. `frenado`: el procesamiento está
    detenido y el rehecho no va a correr hasta que se reanude. `None` si nunca se rehízo.

    `imagenes` son las embebidas sin texto de un documento o una planilla (148), con su
    ubicación y dónde está su descripción (`sin_describir`, `encolada`, `descrita`, `fallida`);
    cada Afirmación dice de qué `imagen` salió (0, la foto entera). `firmadas`: una firma ya las
    escribió en el cuerpo; desde ahí se corrigen ahí y no se deciden (144)."""
    visible_para(principal, fichas.obtener_pieza(pieza_id))
    # Tres lecturas en transacciones distintas, en el orden en que el trabajador las confirma al
    # revés: un trabajo terminado ya dejó su imagen descrita, y una imagen descrita, sus
    # Afirmaciones. Así una descripción que termina entre medio no sale fallida ni sin ellas.
    trabajos = cola.estados_de_descripciones(pieza_id)
    embebidas = fichas.leer_imagenes(pieza_id)
    descripcion, afirmaciones = fichas.leer_afirmaciones(pieza_id)
    clases = _clases(descripcion, embebidas)
    resumen = None
    if principal.rol.puede(Permiso.VALIDAR) and afirmaciones:
        resumen = _resumen_rehecho(fichas, cola, pieza_id)
        if resumen is not None:
            resumen["frenado"] = not habilitado_en(request.app.state.bd, Freno.PROCESAMIENTO)
    if not principal.rol.puede(Permiso.VALIDAR):
        afirmaciones = [
            replace(a, decidida_por=None, decidida_el=None)
            for a in afirmaciones
            if a.estado is EstadoAfirmacion.ACEPTADA
        ]
    visible = ""
    firmadas = False
    if descripcion is not None or embebidas:
        cuerpo = CuerpoDeFicha.partir(fichas.obtener_ficha(pieza_id).cuerpo)
        firmadas = tiene_afirmaciones(cuerpo.principal)
        if descripcion is not None:
            _, visible = cuerpo.partir_texto_visible()
    return {
        "pieza_id": str(pieza_id),
        "tipo_imagen": None if descripcion is None else descripcion.tipo_imagen,
        "aviso": (
            AVISO_DESCRIPCION_FALLIDA
            if descripcion is not None and descripcion.tipo_imagen is None
            else None
        ),
        "afirmaciones": [_afirmacion(a, clases.get(a.imagen)) for a in afirmaciones],
        "firmadas": firmadas,
        "imagenes": [_imagen(i, trabajos.get(i.imagen)) for i in embebidas],
        "texto_visible": (
            {"procedencia": LECTURA_AUTOMATICA, "texto": visible} if visible else None
        ),
        "resumen": resumen,
    }


def _resumen_rehecho(
    fichas: ModuloFichasPostgres, cola: Cola, pieza_id: UUID
) -> dict[str, Any] | None:
    trabajo = cola.estado_del_resumen(pieza_id)
    if trabajo is None:
        return None
    rehechos = fichas.resumenes_rehechos(pieza_id)
    ultimo = rehechos[-1] if rehechos else None
    guardada_el = fichas.obtener_ficha(pieza_id).editada_el
    estado = {
        EstadoDeTrabajo.ENCOLADO: "rehaciendo",
        EstadoDeTrabajo.EN_CURSO: "rehaciendo",
        EstadoDeTrabajo.FALLIDO: "fallido",
    }.get(trabajo, "listo")
    return {
        "estado": estado,
        "texto": None if ultimo is None else ultimo.texto,
        "rehecho_el": None if ultimo is None else momento(ultimo.rehecho_el),
        "atendido": ultimo is not None and guardada_el > ultimo.rehecho_el,
        # Contenido no confiable, como el de la versión: la pantalla lo pinta con `textContent`.
        "informe_inyeccion": None if ultimo is None else ultimo.informe_inyeccion,
    }


@router.put("/piezas/{pieza_id}/afirmaciones/{orden}")
def decidir_afirmacion(
    pieza_id: UUID,
    orden: int,
    pedido: PedidoDecision,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    validacion: Annotated[Validacion, Depends(validacion_de_fichas)],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
) -> dict[str, Any]:
    """Acepta o rechaza una Afirmación y guarda quién la decidió. Se puede cambiar hasta firmar;
    con la Pieza Validada (o Recibida) es un 409. No crea versión: entra al cuerpo al firmar.
    Aceptar sin `confirmada` una que pide confirmación también es un 409 (SEG-21), después del
    estado: con la Pieza Validada manda la transición inválida."""
    descripcion, afirmaciones = fichas.leer_afirmaciones(pieza_id)
    clases = _clases(descripcion, fichas.leer_imagenes(pieza_id))
    if (
        pedido.decision == "aceptada"
        and not pedido.confirmada
        and fichas.obtener_pieza(pieza_id).fila.estado in (Estado.PENDIENTE, Estado.INCOMPLETA)
    ):
        previa = next((a for a in afirmaciones if a.orden == orden), None)
        motivo = previa and confirmacion_para_aceptar(
            previa.texto,
            respaldada=previa.respaldo is not None,
            tipo_imagen=clases.get(previa.imagen),
        )
        if motivo:
            raise Problema(
                tipo="/errores/confirmacion-requerida",
                titulo="Aceptarla pide confirmación",
                estado=409,
                detalle="La lectura automática no respalda esta Afirmación: revisala contra el"
                " original y confirmá que la aceptás.",
                extensiones={"confirmar": motivo.value},
            )
    try:
        afirmacion = validacion.decidir_afirmacion(
            pieza_id=pieza_id,
            orden=orden,
            estado=EstadoAfirmacion(pedido.decision),
            principal=principal,
        )
    except AfirmacionNoEncontrada as error:
        raise Problema(
            tipo="/errores/recurso-no-encontrado",
            titulo="No existe esa Afirmación",
            estado=404,
            detalle=str(error),
        ) from error
    return _afirmacion(afirmacion, clases.get(afirmacion.imagen))


def _sin_imagen(error: ImagenNoEncontrada | ValueError) -> Problema:
    return Problema(
        tipo="/errores/recurso-no-encontrado",
        titulo="No existe esa imagen",
        estado=404,
        detalle=str(error),
    )


@router.post("/piezas/{pieza_id}/imagenes/{imagen}/descripcion", status_code=202)
def describir_imagen(
    pieza_id: UUID,
    imagen: int,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    ingesta: Annotated[Ingesta, Depends(ingesta_de_piezas)],
) -> dict[str, Any]:
    """«Describir esta imagen» (148): encola la descripción de una imagen embebida sin texto.
    Sus Afirmaciones llegan a `GET …/afirmaciones` cuando termina; mientras tanto la firma
    espera. 409 si ya está descrita o en la cola, o si la Pieza ya no se valida."""
    try:
        ingesta.describir_imagen(pieza_id, imagen, por=principal.actor)
    except ImagenNoEncontrada as error:
        raise _sin_imagen(error) from error
    except TransicionInvalida as error:
        raise Problema(
            tipo="/errores/transicion-invalida",
            titulo="La imagen no admite otra descripción",
            estado=409,
            detalle=str(error),
            extensiones={"estado_actual": error.estado_actual},
        ) from error
    return {"pieza_id": str(pieza_id), "imagen": imagen, "estado": EstadoDeImagen.ENCOLADA.value}


@router.get("/piezas/{pieza_id}/imagenes/{imagen}/miniatura")
def miniatura_de_imagen(
    pieza_id: UUID,
    imagen: int,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
    originales: Annotated[AlmacenDeOriginales, Depends(almacen_de_originales)],
) -> Response:
    """La miniatura JPEG de una imagen embebida sin texto (148), sacada del original por su
    ubicación cada vez: no se guarda ninguna copia que la supresión tenga que borrar."""
    visible_para(principal, fichas.obtener_pieza(pieza_id))
    try:
        ubicacion = fichas.ubicar_imagen(pieza_id, imagen)
        jpeg = _miniatura(originales, fichas.obtener_original(pieza_id), ubicacion)
    except (ImagenNoEncontrada, ValueError) as error:
        raise _sin_imagen(error) from error
    except (ConversionFallida, OriginalNoDisponible, RechazoDeAdmision) as error:
        raise Problema(
            tipo="/errores/miniatura-no-disponible",
            titulo="No se pudo mostrar la miniatura",
            estado=503,
            detalle=f"{error} La imagen se ve en el original.",
        ) from error
    # Privada: es parte del original, que no se sirve a quien no lo puede ver.
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@lru_cache(maxsize=256)
def _miniatura(
    originales: AlmacenDeOriginales, original: OriginalGuardado, ubicacion: UbicacionDeImagen
) -> bytes:
    """La miniatura, guardada en la memoria de `web` (unos 20 KB cada una). De un `.doc` o un
    `.xls` sale del convertido que dejó el procesamiento: `web` no corre LibreOffice (SEG-14). No
    va a disco: la supresión no tiene nada que borrar, y una Pieza suprimida ya no pasa
    `ubicar_imagen`."""
    contenido, tipo_mime = originales.leer_convertido(original)
    return imagenes.miniatura(contenido, tipo_mime, ubicacion)


def _lectura_terminada(error: TransicionInvalida) -> Problema:
    return Problema(
        tipo="/errores/transicion-invalida",
        titulo="La lectura ya terminó",
        estado=409,
        detalle=f"{error} Recargá la Ficha: lo que escribiste sigue en pantalla.",
        extensiones={"estado_actual": error.estado_actual},
    )


@router.get("/piezas/{pieza_id}/en-lectura")
def obtener_en_lectura(
    pieza_id: UUID,
    principal: Annotated[Principal, Depends(exigir(Permiso.CONSULTAR))],
    fichas: Annotated[ModuloFichasPostgres, Depends(modulo_de_fichas)],
) -> dict[str, Any]:
    """Lo que abre la pantalla partida con la Pieza Recibida (152), con la forma de la Ficha:
    los campos duros que heredó o ya escribió una persona y el Contexto, sin cuerpo ni resumen.
    Ningún modelo propuso nada: la «propuesta» es la misma fila, así nada se marca 🤖. 409 si
    la lectura ya terminó."""
    pieza = visible_para(principal, fichas.obtener_pieza(pieza_id))
    try:
        campos, contexto = fichas.leer_en_recibida(pieza_id)
    except TransicionInvalida as error:
        raise _lectura_terminada(error) from error
    contenido = {"frontmatter": frontmatter(campos), "cuerpo": "", "contexto": contexto}
    return {
        "pieza_id": str(pieza_id),
        "ficha_id": None,
        "version": 0,
        "en_lectura": True,
        "origen_version": None,
        "metodo_autenticacion": None,
        "requiere_analisis_humano": False,
        **contenido,
        "obligatorios": list(CAMPOS_OBLIGATORIOS),
        "faltantes": faltantes(campos),
        "campos_heredados": list(pieza.campos_heredados),
        "campos_de_la_persona": list(pieza.campos_de_la_persona),
        "procedencia": procedencia_de_campos(
            campos, campos, pieza.campos_heredados, pieza.campos_de_la_persona
        ),
        "propuesta": contenido,
        "informe_inyeccion": None,
    }


@router.put("/piezas/{pieza_id}/en-lectura")
def guardar_en_lectura(
    pieza_id: UUID,
    pedido: PedidoEnLectura,
    principal: Annotated[Principal, Depends(exigir(Permiso.VALIDAR))],
    validacion: Annotated[Validacion, Depends(validacion_de_fichas)],
) -> dict[str, Any]:
    """Guarda los campos duros y el Contexto con la Pieza Recibida (152). Nunca firma ni crea
    una versión: la Pieza sigue Recibida y la v1 los toma al terminar la lectura. 409 si la
    lectura terminó antes; la pantalla recarga la Ficha y conserva lo escrito."""
    try:
        detalle = validacion.completar_en_recibida(
            pieza_id=pieza_id, duros=CamposDuros(**pedido.model_dump()), principal=principal
        )
    except TransicionInvalida as error:
        raise _lectura_terminada(error) from error
    except ValueError as error:
        raise Problema(
            tipo="/errores/fecha-invalida",
            titulo="La Fecha no es válida",
            estado=422,
            detalle=str(error),
            campo="fecha_fin",
        ) from error
    return {
        "pieza_id": str(pieza_id),
        "estado": detalle.fila.estado.value,
        "campos_de_la_persona": list(detalle.campos_de_la_persona),
        "faltantes": detalle.fila.campos_faltantes,
    }
