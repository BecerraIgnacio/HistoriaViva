"""Capa de modelos local: OCR determinista más un motor local de modelos (issues 12 y 13).

`transcribir` no es un LLM (`arquitectura.md` §7.4): PyMuPDF lee el texto de un PDF con capa
de texto, `python-docx` el de un DOCX con sus títulos, listas y tablas (`word`), y Tesseract
con el idioma español hace OCR de las páginas escaneadas y de las fotografías, con la imagen
preparada y el orden de lectura de `ocr` (140). Las imágenes embebidas de un PDF, un Word o una
planilla pasan por el mismo OCR (`imagenes`, 143) y entran al cuerpo como bloques «Lectura
automática», cortados con aviso si llegan a sus topes.

`describir_imagen` y `resumir` van al motor local (Ollama) por HTTP. La instrucción sale de
una plantilla constante y el contenido del Acervo va en bloques delimitados (`entradas`);
cada llamada lleva su tope de tiempo y de tokens (`ollama`). `embeber` va al motor de su fila,
el segundo Ollama (DEC-06), en lotes de 16. `responder_en_vivo` entrega la redacción de la
Conversación a medida que el motor la produce (052) y `reformular` reescribe la pregunta de
seguimiento con su historial (054), las dos con la fila de `responder`; `responder` sin
streaming junta la salida en vivo, para el banco de `responder` (078).

Con qué motor, modelo, instrucción y topes corre cada capacidad lo dice su fila de
`configuracion_capacidad` (RF-32, DEC-87): `capa_para` es la única forma de construir la capa.
"""

from __future__ import annotations

import hashlib
import io
import time
import unicodedata
from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import closing
from dataclasses import replace
from functools import cached_property

import pymupdf
from PIL import Image

from historia_viva import planillas
from historia_viva.capa_modelos import entradas, imagenes, ocr
from historia_viva.capa_modelos.configuracion import (
    MOTOR_LOCAL,
    ConfiguracionDeCapacidad,
    ConfiguracionEfectiva,
    Disponibilidad,
    validar,
)
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_IMAGEN,
    BloqueBinario,
    BloqueDatos,
    CapaModelos,
    Capacidad,
    Invocacion,
    Limites,
    PalabraLeida,
    Plantilla,
    ResultadoDescripcion,
    ResultadoTexto,
    ResultadoTranscripcion,
    ResultadoVectores,
    TramoDeRespuesta,
    UsoEmbedding,
    juntar,
)
from historia_viva.capa_modelos.ollama import MotorOllama
from historia_viva.capa_modelos.word import texto_del_word
from historia_viva.dominio.afirmaciones import UbicacionDeImagen, descripcion_con_afirmaciones
from historia_viva.dominio.cuerpo import escapar_marcas
from historia_viva.dominio.errores import (
    ConfiguracionInvalida,
    ErrorDeConfiguracion,
    LimiteExcedido,
)
from historia_viva.dominio.originales import CSV, DOCX, TOPE_BYTES, XLSX, es_imagen

__all__ = ["capa_para", "verificar"]

_TOPE_PAGINAS = 80
"""Tope de páginas de un documento (`arquitectura.md` §5.1), junto con `TOPE_BYTES`."""

_DPI_ESCANEO = 200
"""Resolución a la que se renderiza una página sin capa de texto antes del OCR."""

_MARGEN_S = 5
"""Lo que se reserva del tope de `transcribir` al leer las imágenes embebidas: el armado del cuerpo
y el chequeo final no tienen que pasarse por la última imagen."""

_LOTE_DE_EMBEBER = 16
"""Textos por llamada a `/api/embed`: un lote más grande no rinde más en CPU y estira la
espera de una consulta que llega detrás de una indexación."""


def capa_para(
    configuracion: Mapping[Capacidad, ConfiguracionDeCapacidad],
    motores: Mapping[str, MotorOllama],
) -> CapaModelos:
    """La capa que corre cada capacidad con su fila. La usa `composicion` con sus motores.

    Raises:
        ConfiguracionInvalida: Con todos los errores del conjunto (`validar`).
    """
    validar(configuracion, motores.keys(), completa=False)
    return _CapaModelosLocal(configuracion, motores)


def verificar(
    config: ConfiguracionDeCapacidad, motores: Mapping[str, MotorOllama]
) -> Disponibilidad:
    """Si el modelo de `config` está en su motor: una consulta a `/api/tags`, nunca lanza."""
    if config.motor == MOTOR_LOCAL or config.modelo is None:
        return Disponibilidad.NO_APLICA
    if config.motor not in motores:
        return Disponibilidad.MOTOR_NO_RESPONDE
    faltantes = motores[config.motor].faltantes([config.modelo])
    if faltantes is None:
        return Disponibilidad.MOTOR_NO_RESPONDE
    return Disponibilidad.NO_DESCARGADO if faltantes else Disponibilidad.DESCARGADO


class _CapaModelosLocal:
    """`CapaModelos` real: OCR determinista para transcribir y el motor local para el resto."""

    def __init__(
        self,
        configuracion: Mapping[Capacidad, ConfiguracionDeCapacidad],
        motores: Mapping[str, MotorOllama],
    ) -> None:
        self._configuracion = configuracion
        self._motores = motores

    def describir_imagen(
        self,
        *,
        imagen: BloqueBinario,
        plantilla: Plantilla | None = None,
        limites: Limites | None = None,
    ) -> ResultadoDescripcion:
        plantilla = plantilla or self._configuracion[Capacidad.DESCRIBIR_IMAGEN].plantilla
        esquema = entradas.esquema_de(plantilla)
        resultado = self._invocar_motor(
            Capacidad.DESCRIBIR_IMAGEN,
            plantilla,
            limites,
            datos=entradas.DATOS_DE_LA_FOTOGRAFIA,
            bloques=(_huella(imagen),),
            imagenes=(entradas.normalizar_imagen(imagen.contenido),),
            formato=esquema,
        )
        if esquema is None:
            return ResultadoDescripcion(**vars(resultado))
        # Sin reintento (144): una respuesta fuera del esquema levanta y la Pieza sigue sin ella.
        tipo, afirmaciones, no_determinable = entradas.leer_descripcion(resultado.texto)
        return ResultadoDescripcion(
            **(vars(resultado) | {"texto": descripcion_con_afirmaciones(tipo, afirmaciones)}),
            tipo_imagen=tipo,
            afirmaciones=afirmaciones,
            no_determinable=no_determinable,
        )

    def transcribir(
        self,
        *,
        archivo: BloqueBinario,
        plantilla: Plantilla = Plantilla.TRANSCRIBIR_DOCUMENTO,
        limites: Limites | None = None,
    ) -> ResultadoTranscripcion:
        config = self._configuracion[Capacidad.TRANSCRIBIR]
        limites = limites or config.limites
        if len(archivo.contenido) > TOPE_BYTES:
            raise LimiteExcedido(
                Capacidad.TRANSCRIBIR, "tamaño", TOPE_BYTES, len(archivo.contenido)
            )
        comenzado_en = time.monotonic()
        avisos: tuple[str, ...] = ()
        truncada = False
        palabras: tuple[PalabraLeida, ...] = ()
        ajustes = ocr.AjustesOcr.de(config.parametros)
        topes = imagenes.TopesDeImagenes.de(config.parametros)

        def leer_imagenes(candidatas: list[imagenes.Embebida]) -> imagenes.ImagenesLeidas:
            # Van después del texto y de las páginas escaneadas: con lo que quede del tope de
            # `transcribir`, cortan con aviso en vez de hacer fallar la Pieza (ADR-0004).
            fin = min(
                time.monotonic() + topes.tiempo_maximo_s,
                comenzado_en + limites.tiempo_maximo_s - _MARGEN_S,
            )
            return imagenes.leer(candidatas, ajustes, topes, fin=fin)

        leidas = imagenes.ImagenesLeidas((), (), truncada=False)
        if es_imagen(archivo.tipo_mime):
            lectura = ocr.leer(
                imagenes.abrir_fotografia(archivo.contenido, Capacidad.TRANSCRIBIR),
                ajustes,
                tiempo_maximo_s=limites.tiempo_maximo_s,
            )
            texto, palabras = lectura.texto, lectura.palabras
        elif archivo.tipo_mime == "application/pdf":
            texto, leidas = _transcribir_pdf(
                archivo.contenido,
                ajustes,
                leer_imagenes,
                comenzado_en=comenzado_en,
                tiempo_maximo_s=limites.tiempo_maximo_s,
            )
        elif archivo.tipo_mime == DOCX:
            leidas = leer_imagenes(imagenes.del_word(archivo.contenido))
            texto = _transcribir_docx(archivo.contenido, leidas)
        elif archivo.tipo_mime in (XLSX, CSV):
            extraccion = _extraer_planilla(archivo, tope_s=limites.tiempo_maximo_s)
            if archivo.tipo_mime == XLSX:
                leidas = leer_imagenes(
                    imagenes.de_la_planilla(planillas.imagenes(archivo.contenido))
                )
            # Las imágenes de cada hoja van al final, después de todas las tablas.
            texto = "\n\n".join([extraccion.cuerpo, *(b for _, b in leidas.bloques)])
            avisos, truncada = extraccion.avisos, extraccion.truncada
        else:
            raise ValueError(f"Tipo de archivo no admitido para transcribir: {archivo.tipo_mime}")
        # La planilla corta su lectura mientras corre (`planillas`, DEC-42); la rama DOCX no tiene
        # un punto intermedio donde frenar como el bucle de páginas del PDF: este chequeo final
        # es la red que le da tope (ADR-0004). shortcut: no corta un Word en curso.
        transcurrido_s = time.monotonic() - comenzado_en
        if transcurrido_s > limites.tiempo_maximo_s:
            raise LimiteExcedido(
                Capacidad.TRANSCRIBIR, "tiempo", limites.tiempo_maximo_s, int(transcurrido_s)
            )
        # Los avisos de las imágenes van al principio del cuerpo, como los de una planilla: así
        # quien valida los ve en la Ficha.
        limpio = _texto_limpio("\n\n".join([*leidas.avisos, texto]))
        return ResultadoTranscripcion(
            texto=limpio,
            avisos=(*avisos, *leidas.avisos),
            truncada=truncada or leidas.truncada,
            # Una planilla se lee sin modelos; `resumir` recibe su digest, no las tablas (DEC-44).
            digest=planillas.digest(limpio) if archivo.tipo_mime in (XLSX, CSV) else None,
            configuracion_efectiva=ConfiguracionEfectiva({Capacidad.TRANSCRIBIR: config}),
            palabras=palabras,
            imagenes_sin_texto=tuple(
                UbicacionDeImagen(imagen.numero, pagina=imagen.pagina, hoja=imagen.hoja)
                for imagen in leidas.sin_texto
            ),
        )

    def resumir(
        self,
        *,
        cuerpo: BloqueDatos,
        contexto: BloqueDatos | None = None,
        plantilla: Plantilla | None = None,
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        datos, enviados = entradas.delimitar(
            (cuerpo,) if contexto is None else (cuerpo, contexto),
            tope_tokens_entrada=(
                limites or self._configuracion[Capacidad.RESUMIR].limites
            ).tope_tokens_entrada,
        )
        return self._invocar_motor(
            Capacidad.RESUMIR, plantilla, limites, datos=datos, bloques=enviados
        )

    def _invocar_motor(
        self,
        capacidad: Capacidad,
        plantilla: Plantilla | None,
        limites: Limites | None,
        *,
        datos: str,
        bloques: tuple[BloqueDatos, ...],
        imagenes: tuple[bytes, ...] = (),
        formato: Mapping[str, object] | None = None,
    ) -> ResultadoTexto:
        """Una capacidad que va al motor: `datos` e `imagenes` al motor, `bloques` al registro."""
        config = self._configuracion[capacidad]
        plantilla = plantilla or config.plantilla
        limites = limites or config.limites
        respuesta = self._motores[config.motor].conversar(
            capacidad=capacidad,
            plantilla=plantilla,
            config=config,
            datos=datos,
            imagenes=imagenes,
            limites=limites,
            formato=formato,
        )
        return ResultadoTexto(
            texto=respuesta.texto,
            invocacion=Invocacion(
                capacidad=capacidad, plantilla=plantilla, bloques=bloques, limites=limites
            ),
            duracion_ms=respuesta.duracion_ms,
            tokens_entrada=respuesta.tokens_entrada,
            tokens_salida=respuesta.tokens_salida,
        )

    def embeber(self, *, textos: tuple[BloqueDatos, ...], uso: UsoEmbedding) -> ResultadoVectores:
        config = self._configuracion[Capacidad.EMBEBER]
        motor = self._motores[config.motor]
        comenzado_en = time.monotonic()
        # Antes de embeber: sin el digest no hay `modelo@digest12`, y un motor caído falla acá.
        digest = motor.digest(config.modelo or "")
        # El prefijo es un dato de la fila; el texto del Acervo va tal cual, sin escapar: el
        # pedido a `/api/embed` no tiene canal de instrucciones (ADR-0003).
        prefijados = [config.parametros[f"prefijo_{uso}"] + texto.contenido for texto in textos]
        vectores = [
            vector
            for inicio in range(0, len(prefijados), _LOTE_DE_EMBEBER)
            for vector in motor.vectorizar(
                config=config, textos=prefijados[inicio : inicio + _LOTE_DE_EMBEBER]
            )
        ]
        dimension = config.parametros["dimension"]
        if otras := {len(v) for v in vectores} - {dimension}:
            raise ConfiguracionInvalida(
                [
                    ErrorDeConfiguracion(
                        Capacidad.EMBEBER.value,
                        "dimension",
                        f"{config.modelo} devuelve vectores de {min(otras)} y la fila dice"
                        f" {dimension}",
                    )
                ]
            )
        return ResultadoVectores(
            vectores=tuple(vectores),
            modelo=f"{config.modelo}@{digest[:12]}",
            duracion_ms=int((time.monotonic() - comenzado_en) * 1000),
        )

    def responder(
        self,
        *,
        consulta: BloqueDatos,
        fragmentos: tuple[BloqueDatos, ...],
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        return juntar(
            self.responder_en_vivo(consulta=consulta, fragmentos=fragmentos, limites=limites)
        )

    def responder_en_vivo(
        self,
        *,
        consulta: BloqueDatos,
        fragmentos: tuple[BloqueDatos, ...],
        limites: Limites | None = None,
    ) -> Generator[TramoDeRespuesta]:
        config = self._configuracion[Capacidad.RESPONDER]
        limites = limites or config.limites
        datos, enviados = entradas.delimitar(
            (consulta, *fragmentos), tope_tokens_entrada=limites.tope_tokens_entrada
        )
        invocacion = Invocacion(
            capacidad=Capacidad.RESPONDER,
            plantilla=config.plantilla,
            bloques=enviados,
            limites=limites,
        )
        # Cerrar esta redacción cierra la del motor, y con ella su conexión (R-17).
        with closing(
            self._motores[config.motor].conversar_en_vivo(
                capacidad=Capacidad.RESPONDER,
                plantilla=config.plantilla,
                config=config,
                datos=datos,
                limites=limites,
            )
        ) as tramos:
            for tramo in tramos:
                if isinstance(tramo, str):
                    yield TramoDeRespuesta(token=tramo)
                else:
                    yield TramoDeRespuesta(
                        resultado=ResultadoTexto(
                            texto=tramo.texto,
                            invocacion=invocacion,
                            duracion_ms=tramo.duracion_ms,
                            tokens_entrada=tramo.tokens_entrada,
                            tokens_salida=tramo.tokens_salida,
                        )
                    )

    def redactar_efemeride(
        self,
        *,
        fichas: tuple[BloqueDatos, ...],
        plantilla: Plantilla | None = None,
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        datos, enviados = entradas.delimitar(
            fichas,
            tope_tokens_entrada=(
                limites or self._configuracion[Capacidad.REDACTAR_EFEMERIDE].limites
            ).tope_tokens_entrada,
        )
        return self._invocar_motor(
            Capacidad.REDACTAR_EFEMERIDE, plantilla, limites, datos=datos, bloques=enviados
        )

    def reformular(
        self,
        *,
        pregunta: BloqueDatos,
        historial: BloqueDatos,
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        tope = (limites or self._configuracion[Capacidad.RESPONDER].limites).tope_tokens_entrada
        historial = entradas.recortar_el_comienzo(
            historial, despues_de=pregunta, tope_tokens_entrada=tope
        )
        datos, enviados = entradas.delimitar((pregunta, historial), tope_tokens_entrada=tope)
        return self._invocar_motor(
            Capacidad.RESPONDER,
            Plantilla.REFORMULAR_CON_HISTORIAL,
            limites,
            datos=datos,
            bloques=enviados,
        )

    def verificar_salud(self, capacidades: Collection[Capacidad] | None = None) -> bool:
        # El OCR corre en proceso y no tiene nada que pueda estar "caído": lo que se comprueba
        # son los motores de las capacidades que tienen modelo.
        motores = {
            config.motor
            for capacidad, config in self._configuracion.items()
            if capacidades is None or capacidad in capacidades
        }
        return all(
            faltantes is not None
            for motor, faltantes in self._faltantes_por_motor.items()
            if motor in motores
        )

    def modelos_faltantes(self) -> tuple[Capacidad, ...]:
        faltantes = self._faltantes_por_motor
        return tuple(
            capacidad
            for capacidad, config in self._configuracion.items()
            if config.modelo in (faltantes.get(config.motor) or ())
        )

    # Una capa por pedido o por trabajo (`capa_vigente`): la salud consulta una sola vez.
    @cached_property
    def _faltantes_por_motor(self) -> dict[str, frozenset[str] | None]:
        modelos: dict[str, set[str]] = {}
        for config in self._configuracion.values():
            if config.motor in self._motores and config.modelo is not None:
                modelos.setdefault(config.motor, set()).add(config.modelo)
        return {motor: self._motores[motor].faltantes(m) for motor, m in modelos.items()}


def _marca_pagina(numero: int) -> str:
    return f"--- Página {numero} ---"


def _transcribir_pdf(
    contenido: bytes,
    ajustes: ocr.AjustesOcr,
    leer_imagenes: Callable[[list[imagenes.Embebida]], imagenes.ImagenesLeidas],
    *,
    comenzado_en: float,
    tiempo_maximo_s: int,
) -> tuple[str, imagenes.ImagenesLeidas]:
    """Cada página bajo su marca: su capa de texto o, si no tiene (escaneada), el OCR de su
    render, igual que una fotografía. Las imágenes de las páginas con capa de texto se leen
    al final, con el tiempo que quede, y su bloque va al final de su página (143)."""
    with pymupdf.open(stream=contenido, filetype="pdf") as documento:
        if documento.page_count > _TOPE_PAGINAS:
            raise LimiteExcedido(
                Capacidad.TRANSCRIBIR, "páginas", _TOPE_PAGINAS, documento.page_count
            )
        textos: list[str] = []
        candidatas: list[imagenes.Embebida] = []
        for indice, pagina in enumerate(documento, start=1):
            tiempo_restante = tiempo_maximo_s - (time.monotonic() - comenzado_en)
            if tiempo_restante <= 0:
                raise LimiteExcedido(
                    Capacidad.TRANSCRIBIR, "tiempo", tiempo_maximo_s, tiempo_maximo_s
                )
            texto = pagina.get_text().strip()
            if texto:
                candidatas += imagenes.de_la_pagina(documento, indice)
            else:
                texto = _ocr_de_la_pagina(pagina, ajustes, tiempo_restante=tiempo_restante).texto
            textos.append(escapar_marcas(texto))
        leidas = leer_imagenes(candidatas)
    paginas = [
        "\n\n".join([_marca_pagina(indice), texto, *leidas.de_la_pagina(indice)])
        for indice, texto in enumerate(textos, start=1)
    ]
    return "\n\n".join(paginas), leidas


def _ocr_de_la_pagina(
    pagina: pymupdf.Page, ajustes: ocr.AjustesOcr, *, tiempo_restante: float
) -> ocr.Lectura:
    dpi = imagenes.dpi_dentro_del_tope(pagina.rect.width, pagina.rect.height, _DPI_ESCANEO)
    pixmap = pagina.get_pixmap(dpi=dpi)
    imagen = Image.open(io.BytesIO(pixmap.tobytes("png")))
    # A 200 dpi la letra ya tiene el tamaño que Tesseract lee bien: la escala ×2 costaba ~40 %
    # más por página (A4: 10,6 s contra 7,6 s) con el mismo texto, y adelantaba el tope de 300 s.
    return ocr.leer(imagen, replace(ajustes, escala=1), tiempo_maximo_s=tiempo_restante)


def _transcribir_docx(contenido: bytes, leidas: imagenes.ImagenesLeidas) -> str:
    # Un DOCX no tiene páginas sin volver a maquetarlo: se lo trata como una sola, para que
    # el cuerpo de la Ficha lleve la misma marca que un PDF (issue 12, criterio 1); sus
    # títulos parten el Fragmento (142). Sus imágenes van al final, «Imagen M» (143).
    texto = escapar_marcas(texto_del_word(contenido))
    return "\n\n".join([_marca_pagina(1), texto, *(b for _, b in leidas.bloques)])


def _extraer_planilla(archivo: BloqueBinario, *, tope_s: float) -> planillas.Extraccion:
    """Un `.xlsx` en tablas por hoja; un CSV o TSV, en una sola hoja que se llama como el
    archivo (DEC-42, DEC-43). La lectura usa el menor entre `tope_s` y los 60 s de DEC-42."""
    if archivo.tipo_mime == XLSX:
        return planillas.extraer(io.BytesIO(archivo.contenido), tope_s=tope_s)
    # El nombre lo trae siempre el procesamiento; sin él, la hoja se llama «planilla».
    nombre = archivo.nombre_archivo or "planilla.csv"
    return planillas.extraer_csv(io.BytesIO(archivo.contenido), nombre, tope_s=tope_s)


def _texto_limpio(texto: str) -> str:
    """UTF-8 NFC y sin caracteres de control salvo salto de línea y tabulación (R-36, DIR-19):
    un PDF puede traer la «é» descompuesta o un control que no se ve y rompe la búsqueda."""
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFC", texto)
        if caracter in "\n\t" or unicodedata.category(caracter) != "Cc"
    )


def _huella(archivo: BloqueBinario) -> BloqueDatos:
    """Registra un binario por su huella, sin copiarlo (ver `Invocacion.bloques`)."""
    etiqueta = ETIQUETA_IMAGEN if es_imagen(archivo.tipo_mime) else archivo.etiqueta
    huella = hashlib.sha256(archivo.contenido).hexdigest()
    return BloqueDatos(etiqueta=etiqueta, contenido=f"sha256:{huella}")
