"""El manejador de los trabajos `procesamiento`: lleva una Pieza de `Recibida` a `Pendiente`.
Y el de los trabajos `descripcion`: describe una imagen embebida sin texto (148).

`ejecutar` corre el plan —dos o tres invocaciones a la capa de modelos— sin transacción abierta
(DEC-99); `guardar` escribe la Ficha propuesta en la transacción en la que el trabajador cierra
el trabajo.

**El contenido del archivo nunca toca el canal de instrucciones** (ADR-0003, capa 1). Acá eso
no requiere disciplina: las firmas de `CapaModelos` no tienen dónde poner una instrucción
compuesta, la plantilla se elige por identificador del enum `Plantilla` y el archivo entra
como `BloqueBinario`. Y **ninguna invocación sale sin tope**: la capa de cada trabajo se arma
con la instantánea de `configuracion_capacidad` que el trabajo congeló al encolar, que trae
instrucción y topes (ADR-0004, RF-32, DEC-87).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from historia_viva.bd import Tx
from historia_viva.capa_modelos import imagenes
from historia_viva.capa_modelos.configuracion import (
    ConfiguracionDeCapacidad,
    ConfiguracionEfectiva,
)
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONTEXTO_LOTE,
    ETIQUETA_CUERPO_FICHA,
    ETIQUETA_DOCUMENTO,
    ETIQUETA_IMAGEN,
    BloqueBinario,
    BloqueDatos,
    CapaModelos,
    Capacidad,
    Plantilla,
    ResultadoDescripcion,
    ResultadoTexto,
)
from historia_viva.dominio.afirmaciones import (
    DescripcionPropuesta,
    ParaRehacerResumen,
    UbicacionDeImagen,
    descripcion_con_afirmaciones,
)
from historia_viva.dominio.cruce import cruzar
from historia_viva.dominio.cuerpo import ENCABEZADO_TEXTO_VISIBLE, escapar_marcas
from historia_viva.dominio.errores import (
    DescripcionFueraDeEsquema,
    LimiteExcedido,
    MotorNoAlcanzable,
)
from historia_viva.dominio.originales import es_imagen, es_texto
from historia_viva.dominio.tipos import FichaVigente, OriginalGuardado
from historia_viva.originales import AlmacenDeOriginales
from historia_viva.procesamiento.cola import Cola, TrabajoTomado
from historia_viva.procesamiento.trabajador import ResultadoDeTrabajo

__all__ = [
    "ENCABEZADO_TEXTO_VISIBLE",
    "A_DESCRIBIR_POR_OMISION",
    "DescripcionDeImagen",
    "FichasDeLaDescripcion",
    "FichasDelProcesamiento",
    "FichasDelResumen",
    "ManejadorDeDescripcion",
    "ManejadorDeProcesamiento",
    "ManejadorDeResumen",
    "PropuestaDeFicha",
    "cuerpo_de_la_foto",
]

A_DESCRIBIR_POR_OMISION = 3
"""`imagenes_a_describir` si la fila de `describir_imagen` no lo trae: las imágenes embebidas sin
texto que se describen solas por Pieza (148). En esta PC sin GPU cada una tarda minutos
(Comments de 148); con GPU (154) se sube en la fila."""

_log = logging.getLogger(__name__)

_TIPO_DE_EMBEBIDA = "image/*"
"""El MIME con el que va a la capa una imagen embebida: la capa la normaliza a JPEG sin mirarlo;
basta con que sea una imagen (`es_imagen`)."""


def cuerpo_de_la_foto(descripcion: str, texto_visible: str) -> str:
    """El cuerpo propuesto de una fotografía: la descripción y, debajo, lo que tenga escrito.

    Una foto sin nada escrito devuelve solo la descripción: el OCR no aportó nada y no hay
    por qué dejarle a quien valida un encabezado vacío para borrar (`casos-de-uso.md` CU-03,
    flujo alternativo 2a).
    """
    # Lo que digan el modelo o la foto no abre otra sección ni pone una página (142).
    descripcion, texto_visible = escapar_marcas(descripcion), escapar_marcas(texto_visible)
    if not texto_visible.strip():
        return descripcion.strip()
    visible = f"{ENCABEZADO_TEXTO_VISIBLE}\n\n{texto_visible.strip()}"
    # Con Afirmaciones (144) no hay descripción en prosa: el cuerpo de la v1 es solo lo leído.
    return f"{descripcion.strip()}\n\n{visible}" if descripcion.strip() else visible


def _cuerpo_del_texto(contenido: bytes, tipo_mime: str) -> str:
    """Un texto plano leído con el `charset` que detectó la admisión, en UTF-8 NFC y sin
    caracteres de control salvo salto de línea y tabulación (R-36)."""
    charset = tipo_mime.partition("charset=")[2].strip() or "utf-8"
    # `escapar_marcas` limpia (un `\r` suelto es un fin de línea, Mac clásico) y deja un
    # `## Contexto` o un `--- Página 9 ---` del archivo como texto, no estructura (142).
    return escapar_marcas(contenido.decode(charset).removeprefix("﻿")).strip()


def _resumir(capa: CapaModelos, trabajo: TrabajoTomado, cuerpo: str) -> ResultadoTexto:
    """El resumen de la v1 y el rehecho (146), pedidos igual. El Contexto del Lote lo recibe solo
    `resumir` (DEC-19): describir una foto es decir lo que se ve. Un Lote encolado antes de
    completar su Contexto lo congeló vacío (historia 78, 151): se resume sin él."""
    contexto = "" if trabajo.contexto_lote is None else trabajo.contexto_lote.como_texto()
    return capa.resumir(
        cuerpo=BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=cuerpo),
        contexto=(
            BloqueDatos(etiqueta=ETIQUETA_CONTEXTO_LOTE, contenido=contexto) if contexto else None
        ),
    )


def _describir(
    capa: CapaModelos, imagen: BloqueBinario
) -> tuple[ResultadoDescripcion | None, DescripcionPropuesta | None]:
    """La descripción de una foto y lo que de ella va a la v1. Una respuesta fuera del esquema, o
    cortada por el tope de tokens de salida, no se reintenta: la Pieza sigue sin Afirmaciones,
    con el aviso, y la persona escribe la descripción (144)."""
    try:
        descripcion = capa.describir_imagen(imagen=imagen)
    except DescripcionFueraDeEsquema:
        return None, DescripcionPropuesta(tipo_imagen=None)
    except LimiteExcedido as error:
        if error.limite != "tokens de salida":
            raise
        return None, DescripcionPropuesta(tipo_imagen=None)
    if descripcion.tipo_imagen is None:  # una instrucción de prosa: sin Afirmaciones
        return descripcion, None
    return descripcion, DescripcionPropuesta(descripcion.tipo_imagen, descripcion.afirmaciones)


class FichasDelProcesamiento(Protocol):
    """Las dos operaciones de `ModuloFichas` que el procesamiento necesita.

    Se declara el subconjunto y no la interfaz entera para que quede escrito qué puede
    hacer esta tarea sobre el Acervo: ubicar el original de una Pieza y publicar su versión 1.
    No lista, no edita y no firma nada.
    """

    def obtener_original(self, pieza_id: UUID) -> OriginalGuardado: ...

    def guardar_ficha_propuesta(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        resumen: str,
        cuerpo: str,
        configuracion_efectiva: ConfiguracionEfectiva,
        descripcion: DescripcionPropuesta | None = None,
        imagenes: Sequence[UbicacionDeImagen] = (),
        a_describir: int = 0,
    ) -> FichaVigente: ...


@dataclass(frozen=True, kw_only=True)
class PropuestaDeFicha(ResultadoDeTrabajo):
    """Lo que propuso el plan, todavía sin guardar."""

    resumen: str
    cuerpo: str
    descripcion: DescripcionPropuesta | None = None
    """Las Afirmaciones de una foto, o su falla (144); `None` sin instrucción de esquema."""
    imagenes: tuple[UbicacionDeImagen, ...] = ()
    """Las embebidas sin texto, para describirlas aparte (148)."""


class ManejadorDeProcesamiento:
    """Lee el original, corre el plan de su clase y guarda la versión 1 de la Ficha."""

    def __init__(
        self,
        *,
        capa_para: Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], CapaModelos],
        fichas: FichasDelProcesamiento,
        originales: AlmacenDeOriginales,
        cola: Cola,
    ) -> None:
        self._capa_para = capa_para
        self._fichas = fichas
        self._originales = originales
        self._cola = cola

    def ejecutar(self, trabajo: TrabajoTomado) -> PropuestaDeFicha:
        """De un documento sale su transcripción íntegra; de una fotografía, una descripción
        propuesta más el OCR de su texto visible (user stories 6 y 7, RF-08); de un texto
        plano, el texto mismo. De cualquiera, el resumen."""
        # Con la instantánea del trabajo: una fila editada después de encolar no lo alcanza.
        capa = self._capa_para(trabajo.configuracion_efectiva.por_capacidad)
        original = self._fichas.obtener_original(trabajo.pieza_id)
        # La capa de modelos recibe el archivo entero; acotado por el tope de `transcribir`. Un
        # `.doc`, un `.xls` o un HEIC llegan convertidos y ya medidos por la guarda (153).
        contenido, tipo_mime = self._originales.leer_para_procesar(original)
        digest: str | None = None
        descripcion: ResultadoDescripcion | None = None
        propuesta: DescripcionPropuesta | None = None
        sin_texto: tuple[UbicacionDeImagen, ...] = ()
        if es_texto(tipo_mime):
            # Un texto plano no pasa por ningún modelo para leerse: es su cuerpo.
            cuerpo = para_resumir = _cuerpo_del_texto(contenido, tipo_mime)
        else:
            foto = es_imagen(tipo_mime)
            archivo = BloqueBinario(
                etiqueta=ETIQUETA_IMAGEN if foto else ETIQUETA_DOCUMENTO,
                contenido=contenido,
                tipo_mime=tipo_mime,
                nombre_archivo=original.nombre_archivo,
            )
            # Una foto se describe antes de leerse: describir contesta "qué es esto" y el OCR
            # solo agrega lo que esté escrito, que puede no ser nada.
            if foto:
                descripcion, propuesta = _describir(capa, archivo)
            transcripcion = capa.transcribir(
                archivo=archivo,
                plantilla=(
                    Plantilla.TRANSCRIBIR_IMAGEN if foto else Plantilla.TRANSCRIBIR_DOCUMENTO
                ),
            )
            cuerpo = para_resumir = transcripcion.texto
            if propuesta is not None and propuesta.afirmaciones:
                # El OCR no inventa: lo que el modelo afirma se cruza con lo leído, sin otro
                # modelo (145).
                propuesta = replace(
                    propuesta, respaldos=cruzar(propuesta.afirmaciones, transcripcion.palabras)
                )
            if foto:
                # El resumen sale de lo que propuso el modelo y de lo leído: nadie rechazó nada
                # todavía (ADR-0007). Las Afirmaciones no van al cuerpo de la v1: entran al firmar;
                # el texto visible sí, como «Lectura automática».
                propuesto = "" if descripcion is None else descripcion.texto
                para_resumir = cuerpo_de_la_foto(propuesto, transcripcion.texto)
                cuerpo = cuerpo_de_la_foto(
                    propuesto if propuesta is None else "", transcripcion.texto
                )
            digest = transcripcion.digest
            sin_texto = transcripcion.imagenes_sin_texto
        # El resumen se pide sobre el cuerpo ya armado: de una foto resume la descripción y
        # su texto visible juntos, no una de las dos; de una planilla, su digest y no las
        # tablas (DEC-44).
        resumen = _resumir(capa, trabajo, para_resumir if digest is None else digest)
        # `transcribir` no es un modelo de lenguaje: no gasta tokens.
        resultados = (resumen,) if descripcion is None else (descripcion, resumen)
        return PropuestaDeFicha(
            resumen=resumen.texto,
            cuerpo=cuerpo,
            descripcion=propuesta,
            imagenes=sin_texto,
            tokens_entrada=sum(r.tokens_entrada for r in resultados),
            tokens_salida=sum(r.tokens_salida for r in resultados),
        )

    def guardar(self, tx: Tx, trabajo: TrabajoTomado, resultado: PropuestaDeFicha) -> None:
        # La propuesta **no pre-llena campos duros**: `tipo`, `origen` y la Fecha los conoce
        # una persona, no el archivo, y una propuesta inventada se firma sin mirarla
        # (user story 9 reducida, decisión del 15-sep).
        # Las primeras imágenes sin texto se describen solas, cada una en su trabajo: la Pieza
        # pasa a Pendiente sin esperar minutos por imagen, y sus Afirmaciones llegan después.
        a_describir = min(len(resultado.imagenes), self._a_describir(trabajo))
        self._fichas.guardar_ficha_propuesta(
            tx,
            pieza_id=trabajo.pieza_id,
            resumen=resultado.resumen,
            cuerpo=resultado.cuerpo,
            configuracion_efectiva=trabajo.configuracion_efectiva,
            descripcion=resultado.descripcion,
            imagenes=resultado.imagenes,
            a_describir=a_describir,
        )
        for imagen in range(1, a_describir + 1):
            self._cola.encolar_descripcion(
                tx,
                trabajo.pieza_id,
                imagen=imagen,
                configuracion_efectiva=trabajo.configuracion_efectiva,
            )

    @staticmethod
    def _a_describir(trabajo: TrabajoTomado) -> int:
        fila = trabajo.configuracion_efectiva.por_capacidad.get(Capacidad.DESCRIBIR_IMAGEN)
        if fila is None:
            return 0
        a_describir: int = fila.parametros.get("imagenes_a_describir", A_DESCRIBIR_POR_OMISION)
        return a_describir

    def despues_de_completar(
        self, tx: Tx, trabajo: TrabajoTomado, resultado: PropuestaDeFicha
    ) -> None:
        pass


class FichasDelResumen(Protocol):
    """Lo que el rehecho del resumen necesita de `ModuloFichas` (146): leer con qué se rehace y
    guardar el resultado como propuesta. No crea versión ni firma nada."""

    def para_rehacer_resumen(self, pieza_id: UUID) -> ParaRehacerResumen: ...

    def guardar_resumen_rehecho(
        self, tx: Tx, *, pieza_id: UUID, texto: str, ordenes: Collection[int]
    ) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class ResumenPropuesto(ResultadoDeTrabajo):
    texto: str
    ordenes: tuple[int, ...]
    """Las Afirmaciones con que se rehízo: si al guardar ya no son las no rechazadas, se descarta."""


class ManejadorDeResumen:
    """Rehace el resumen de una foto con las Afirmaciones que nadie rechazó (146, ADR-0007): un
    dato rechazado no vuelve a colarse en la prosa. Lo arma igual que el de la v1 —la clase y las
    Afirmaciones, debajo el texto visible, y el Contexto del Lote aparte—, todo como bloques de
    datos y nada en la instrucción (ADR-0003)."""

    def __init__(
        self,
        *,
        capa_para: Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], CapaModelos],
        fichas: FichasDelResumen,
        cola: Cola,
    ) -> None:
        self._capa_para = capa_para
        self._fichas = fichas
        self._cola = cola

    def ejecutar(self, trabajo: TrabajoTomado) -> ResumenPropuesto:
        capa = self._capa_para(trabajo.configuracion_efectiva.por_capacidad)
        datos = self._fichas.para_rehacer_resumen(trabajo.pieza_id)
        descripcion = descripcion_con_afirmaciones(
            datos.tipo_imagen, (a.texto for a in datos.no_rechazadas)
        )
        resumen = _resumir(capa, trabajo, cuerpo_de_la_foto(descripcion, datos.texto_visible))
        return ResumenPropuesto(
            texto=resumen.texto,
            ordenes=tuple(a.orden for a in datos.no_rechazadas),
            tokens_entrada=resumen.tokens_entrada,
            tokens_salida=resumen.tokens_salida,
        )

    def guardar(self, tx: Tx, trabajo: TrabajoTomado, resultado: ResumenPropuesto) -> None:
        guardado = self._fichas.guardar_resumen_rehecho(
            tx, pieza_id=trabajo.pieza_id, texto=resultado.texto, ordenes=resultado.ordenes
        )
        if not guardado:
            # Las decisiones cambiaron mientras corría: se rehace con las nuevas. Si el cambio ya
            # encoló uno, este no agrega otro (uno encolado por Pieza).
            self._cola.encolar_resumen(
                tx, trabajo.pieza_id, configuracion_efectiva=trabajo.configuracion_efectiva
            )

    def despues_de_completar(
        self, tx: Tx, trabajo: TrabajoTomado, resultado: ResumenPropuesto
    ) -> None:
        pass


class FichasDeLaDescripcion(Protocol):
    """Lo que la descripción de una imagen embebida necesita de `ModuloFichas` (148): ubicar el
    original y la imagen, y guardar sus Afirmaciones. No crea versiones ni firma."""

    def obtener_original(self, pieza_id: UUID) -> OriginalGuardado: ...

    def ubicar_imagen(self, pieza_id: UUID, imagen: int) -> UbicacionDeImagen: ...

    def guardar_descripcion(
        self, tx: Tx, *, pieza_id: UUID, imagen: int, descripcion: DescripcionPropuesta
    ) -> None: ...


@dataclass(frozen=True, kw_only=True)
class DescripcionDeImagen(ResultadoDeTrabajo):
    descripcion: DescripcionPropuesta
    """Sus Afirmaciones, o `tipo_imagen = None` si falló."""


class ManejadorDeDescripcion:
    """Describe una imagen embebida sin texto (148): la saca del original por su ubicación (no
    hay copia guardada), la pasa por `describir_imagen` con el tope de su fila (ADR-0004) y deja
    sus Afirmaciones sin decidir. Una respuesta fuera del esquema, o cortada por un tope, deja la
    imagen descrita sin Afirmaciones y con el aviso: así no traba la firma."""

    def __init__(
        self,
        *,
        capa_para: Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], CapaModelos],
        fichas: FichasDeLaDescripcion,
        originales: AlmacenDeOriginales,
    ) -> None:
        self._capa_para = capa_para
        self._fichas = fichas
        self._originales = originales

    def ejecutar(self, trabajo: TrabajoTomado) -> DescripcionDeImagen:
        assert trabajo.imagen is not None
        capa = self._capa_para(trabajo.configuracion_efectiva.por_capacidad)
        ubicacion = self._fichas.ubicar_imagen(trabajo.pieza_id, trabajo.imagen)
        try:
            original = self._fichas.obtener_original(trabajo.pieza_id)
            contenido, tipo_mime = self._originales.leer_para_procesar(original)
            archivo = BloqueBinario(
                etiqueta=ETIQUETA_IMAGEN,
                contenido=imagenes.sacar(contenido, tipo_mime, ubicacion),
                tipo_mime=_TIPO_DE_EMBEBIDA,
            )
            descripcion, propuesta = _describir(capa, archivo)
        except MotorNoAlcanzable:
            raise  # no llegó al motor: vuelve a la cola sin gastar la generación (DEC-86)
        except Exception:  # noqa: BLE001
            # En una foto un fallo deja el trabajo fallido y la Pieza Recibida; acá la Pieza ya es
            # Pendiente y la firma espera a la imagen: un tope, un motor que corta, un original que
            # no se convierte o una imagen que no se saca ni se normaliza la dejan con el aviso,
            # para describirla a mano.
            _log.exception(
                "No se pudo describir la imagen %s de %s.", trabajo.imagen, trabajo.pieza_id
            )
            descripcion, propuesta = None, None
        return DescripcionDeImagen(
            descripcion=propuesta or DescripcionPropuesta(tipo_imagen=None),
            tokens_entrada=0 if descripcion is None else descripcion.tokens_entrada,
            tokens_salida=0 if descripcion is None else descripcion.tokens_salida,
        )

    def guardar(self, tx: Tx, trabajo: TrabajoTomado, resultado: DescripcionDeImagen) -> None:
        assert trabajo.imagen is not None
        self._fichas.guardar_descripcion(
            tx, pieza_id=trabajo.pieza_id, imagen=trabajo.imagen, descripcion=resultado.descripcion
        )

    def despues_de_completar(
        self, tx: Tx, trabajo: TrabajoTomado, resultado: DescripcionDeImagen
    ) -> None:
        pass
