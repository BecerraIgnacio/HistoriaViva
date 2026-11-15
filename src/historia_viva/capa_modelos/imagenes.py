"""Las imágenes embebidas de un PDF, un Word o una planilla, leídas con el OCR (143).

Cada extractor da sus imágenes con su ubicación: `de_la_pagina` (PDF), `del_word` y, para la
planilla, `planillas.imagenes`. `leer` descarta las decorativas, pasa las demás por el OCR
preparado (`ocr`, 140) y arma un bloque por imagen con texto, bajo su marca
(`dominio.cuerpo.marca_de_imagen`). El texto es de Tesseract, no de un modelo: entra como
«Lectura automática» (ADR-0007) y escapado (`escapar_marcas`). Una imagen sin texto no deja
bloque.

- **Sin texto (148):** la que el OCR leyó con menos de `PALABRAS_DE_TEXTO` palabras confiables
  (una foto, un gráfico pegado) queda en `ImagenesLeidas.sin_texto`: la describe el modelo de
  visión. La miniatura y esa descripción la vuelven a sacar del original por su ubicación
  (`sacar`, `miniatura`): no se guarda ninguna copia.

- **Decorativas, sin aviso:** menos de `LADO_MINIMO_PX` de lado o de `AREA_MINIMA_PX` de área
  (íconos, viñetas, filetes), y la misma imagen (por huella) en `_LUGARES_DE_MEMBRETE` páginas
  u hojas o más (un logo o un membrete). Una repetida en menos lugares se lee una sola vez.
  Se descarta antes de decodificarla.
- **El escaneo de una página con capa de texto:** una imagen que cubre casi toda la página de
  un PDF es la página escaneada, y su capa de texto ya es su OCR: leerla duplicaría el texto.
- **Omitidas con aviso:** más píxeles que `TOPE_PIXELES` (bombas de descompresión), WMF y EMF,
  y lo que Pillow no abre. La Pieza no falla.
- **Topes, en la fila de `transcribir`:** `imagenes_maximo` imágenes leídas por Pieza e
  `imagenes_tiempo_maximo_s` para leerlas, dentro del tope total de `transcribir` (ADR-0004,
  lo calcula `local`). Al llegar a cualquiera se corta con aviso en la Ficha. Se lee una imagen
  por vez: la memoria no crece con la cantidad (el trabajador tiene 2 GB).
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import time
import warnings
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

import pymupdf
from docx import Document
from docx.oxml.ns import qn
from PIL import ExifTags, Image

from historia_viva import planillas
from historia_viva.capa_modelos import ocr
from historia_viva.dominio.afirmaciones import UbicacionDeImagen
from historia_viva.dominio.cuerpo import escapar_marcas, marca_de_imagen, ubicacion_de_imagen
from historia_viva.dominio.errores import LimiteExcedido
from historia_viva.dominio.originales import DOCX

__all__ = [
    "AREA_MINIMA_PX",
    "CONFIANZA_DE_TEXTO",
    "LADO_MINIMO_PX",
    "LADO_DE_MINIATURA_PX",
    "PALABRAS_DE_TEXTO",
    "TOPE_DE_DECODIFICACION_BYTES",
    "TOPE_PIXELES",
    "Embebida",
    "ImagenesLeidas",
    "TopesDeImagenes",
    "abrir_fotografia",
    "de_la_pagina",
    "de_la_planilla",
    "del_word",
    "dpi_dentro_del_tope",
    "leer",
    "miniatura",
    "sacar",
]

LADO_MINIMO_PX = 24
"""Un filete, una viñeta o un separador: ni una letra legible entra en menos."""

AREA_MINIMA_PX = 10_000
"""Unos 100 × 100 px: un ícono o un logo chico. Un renglón de texto pegado como captura mide más
(400 × 40 son 16 000)."""

TOPE_PIXELES = min(Image.MAX_IMAGE_PIXELS or 25_000_000, 25_000_000)
"""El tope de Pillow contra bombas de descompresión (89 M de píxeles), bajado a 25 M (una foto de
24 Mpx entra): una de 89 M en RGBA, más su copia en gris y Tesseract, se acercaba a los 2 GB del
trabajador."""

TOPE_DE_DECODIFICACION_BYTES = 500 * 1000 * 1000
"""Lo que puede ocupar una fotografía decodificada entera antes de achicarla a `TOPE_PIXELES`
(SEG-17): un escaneo A3 a 600 dpi (unos 70 Mpx) entra en RGB y en gris de 16 bits. Con la copia
achicada (100 MB) y Tesseract después, queda holgado en los 2 GB del trabajador."""

PALABRAS_DE_TEXTO = 5
"""Con menos palabras confiables, la imagen casi no tiene texto y la describe el modelo (148). Una
captura o un cartel traen renglones enteros; una foto o un gráfico, a lo sumo un título, una
fecha o los rótulos de los ejes. Tres de «Fuerzas Armadas · 1975» no la hacen texto."""

CONFIANZA_DE_TEXTO = 60
"""Una palabra cuenta como confiable con esta confianza de Tesseract o más y al menos tres letras
o cifras. No es `ocr_confianza_minima` (0, para no perder texto real del cartel): acá se decide
si hay texto, y la basura que Tesseract saca de una foto o de un ícono («AAA», «il») queda entre
40 y 60 o es de una o dos letras (140)."""

LADO_DE_MINIATURA_PX = 320
"""El lado mayor de la miniatura de la pantalla partida: se reconoce la imagen sin abrir el
original."""

_PROPORCION_DE_ESCANEO = 0.8
"""Una imagen que cubre más de esta parte de una página con capa de texto es su escaneo."""

_DPI_RECORTE = 200
"""A la que se recorta una imagen en línea de un PDF (sin `xref`), como la página escaneada."""

_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

_FORMATOS = ("PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP", "JPEG2000", "WMF")
"""Los que se intentan abrir: un EPS embebido no llama a Ghostscript. WMF (que también reconoce
EMF) se abre solo para leer su cabecera y omitirlo con aviso."""

_LUGARES_DE_MEMBRETE = 3
"""La misma imagen en tres páginas u hojas distintas o más es un logo o un membrete: no se lee.
Repetida en menos (la misma captura pegada dos veces en un Word), se lee la primera vez."""

_IMAGENES_DISTINTAS = planillas.TOPES.imagenes_distintas
"""Las imágenes distintas (por huella) de un documento que se miran: el extractor da una de más
y `leer` corta con aviso (SEG-16). Las repeticiones de un logo no cuentan."""

_FORMATOS_DE_FOTOGRAFIA = ("JPEG", "PNG", "TIFF")
"""Los de una fotografía subida (`originales`); el HEIC llega convertido a JPEG."""

_SIN_PILLOW = ("WMF", "EMF")
"""Pillow los identifica pero solo los dibuja en Windows: se omiten en todos lados por igual."""


@dataclass(frozen=True)
class TopesDeImagenes:
    """`imagenes_maximo` e `imagenes_tiempo_maximo_s` de la fila de `transcribir`."""

    maximo: int = 30
    """Imágenes que pasan por el OCR en una Pieza; 0 no lee ninguna."""
    tiempo_maximo_s: int = 120
    """Para leerlas todas: con 2 a 8 s por imagen en esta PC, deja 180 s de los 300 de
    `transcribir` a las páginas escaneadas, que van primero."""

    @classmethod
    def de(cls, parametros: Mapping[str, Any]) -> TopesDeImagenes:
        base = cls()
        return cls(
            maximo=parametros.get("imagenes_maximo", base.maximo),
            tiempo_maximo_s=parametros.get("imagenes_tiempo_maximo_s", base.tiempo_maximo_s),
        )


@dataclass(frozen=True)
class Embebida:
    """Una imagen como la da su extractor: dónde está, su huella y cómo sacarle los bytes."""

    numero: int
    """Su orden entre las imágenes de su página, su hoja o su Word."""
    huella: str
    cargar: Callable[[], bytes]
    pagina: int | None = None
    hoja: str | None = None
    tamano: tuple[int, int] | None = None
    """Ancho y alto en píxeles, si el extractor lo sabe sin decodificarla (PDF)."""

    @property
    def ubicacion(self) -> str:
        return ubicacion_de_imagen(self.numero, pagina=self.pagina, hoja=self.hoja)


@dataclass(frozen=True)
class ImagenesLeidas:
    bloques: tuple[tuple[Embebida, str], ...]
    """Cada imagen con texto y su bloque: la marca, una línea en blanco y el texto escapado."""
    avisos: tuple[str, ...]
    truncada: bool
    """Si un tope dejó imágenes sin leer."""
    sin_texto: tuple[Embebida, ...] = ()
    """Las leídas con menos de `PALABRAS_DE_TEXTO` palabras confiables, en orden (148)."""

    def de_la_pagina(self, pagina: int) -> list[str]:
        return [bloque for imagen, bloque in self.bloques if imagen.pagina == pagina]


class _Omitida(Exception):
    """La imagen no se lee; el mensaje es el motivo, para el aviso."""


def leer(
    candidatas: Sequence[Embebida],
    ajustes: ocr.AjustesOcr,
    topes: TopesDeImagenes,
    *,
    fin: float,
    motor: ocr.MotorOcr = ocr.TESSERACT,
) -> ImagenesLeidas:
    """El OCR de `candidatas` sin las decorativas, hasta `fin` (`time.monotonic`) o
    `topes.maximo` imágenes leídas: lo que queda se corta con aviso."""
    avisos: list[str] = []
    distintas = list(dict.fromkeys(candidata.huella for candidata in candidatas))
    recortada = len(distintas) > _IMAGENES_DISTINTAS
    if recortada:
        miradas = set(distintas[:_IMAGENES_DISTINTAS])
        candidatas = [candidata for candidata in candidatas if candidata.huella in miradas]
        avisos.append(
            f"El documento tiene más de {_IMAGENES_DISTINTAS} imágenes distintas: se miraron las"
            f" primeras {_IMAGENES_DISTINTAS}."
        )
    lugares: dict[str, set[tuple[int | None, str | None]]] = defaultdict(set)
    for candidata in candidatas:
        lugares[candidata.huella].add((candidata.pagina, candidata.hoja))
    vistas: set[str] = set()
    unicas: list[Embebida] = []
    for candidata in candidatas:
        if (
            candidata.huella not in vistas
            and len(lugares[candidata.huella]) < _LUGARES_DE_MEMBRETE
            and not _chica(candidata.tamano)
        ):
            unicas.append(candidata)
        vistas.add(candidata.huella)
    bloques: list[tuple[Embebida, str]] = []
    sin_texto: list[Embebida] = []
    leidas = 0

    def cortar(indice: int, motivo: str = "se agotó el tiempo para leerlas") -> ImagenesLeidas:
        avisos.append(_aviso_de_corte(leidas, len(unicas) - indice, motivo))
        return ImagenesLeidas(tuple(bloques), tuple(avisos), truncada=True, sin_texto=tuple(sin_texto))

    for indice, candidata in enumerate(unicas):
        if leidas >= topes.maximo:
            return cortar(indice, f"el tope es de {topes.maximo} por Pieza")
        if fin - time.monotonic() <= 0:
            return cortar(indice)
        try:
            imagen = _abrir(candidata)
        except _Omitida as omitida:
            avisos.append(f"Se omitió la imagen «{candidata.ubicacion}»: {omitida}.")
            continue
        if imagen is None:
            continue
        # Abrir y decodificar hasta 25 Mpx también gasta del tope: se mide de nuevo.
        restante = fin - time.monotonic()
        if restante <= 0:
            return cortar(indice)
        try:
            lectura = ocr.leer(imagen, ajustes, tiempo_maximo_s=restante, motor=motor)
        except LimiteExcedido:
            return cortar(indice)
        leidas += 1
        if lectura.texto.strip():
            bloques.append((candidata, f"{_marca(candidata)}\n\n{escapar_marcas(lectura.texto)}"))
        if _palabras_confiables(lectura) < PALABRAS_DE_TEXTO:
            sin_texto.append(candidata)
    return ImagenesLeidas(
        tuple(bloques), tuple(avisos), truncada=recortada, sin_texto=tuple(sin_texto)
    )


def _palabras_confiables(lectura: ocr.Lectura) -> int:
    return sum(
        1
        for palabra in lectura.palabras
        if palabra.confianza >= CONFIANZA_DE_TEXTO
        and sum(caracter.isalnum() for caracter in palabra.texto) >= 3
    )


def _aviso_de_corte(leidas: int, sin_leer: int, motivo: str) -> str:
    quedaron = "Quedó 1 imagen" if sin_leer == 1 else f"Quedaron {sin_leer} imágenes"
    return f"{quedaron} sin leer ({leidas} leídas): {motivo}."


def _marca(imagen: Embebida) -> str:
    return marca_de_imagen(imagen.numero, pagina=imagen.pagina, hoja=imagen.hoja)


def _chica(tamano: tuple[int, int] | None) -> bool:
    if tamano is None:
        return False
    ancho, alto = tamano
    return min(ancho, alto) < LADO_MINIMO_PX or ancho * alto < AREA_MINIMA_PX


def _abrir(candidata: Embebida) -> Image.Image | None:
    """La imagen decodificada, `None` si es chica (decorativa) o `_Omitida` con el motivo. Mide
    antes de decodificar: la cabecera ya dice el tamaño."""
    if candidata.tamano and candidata.tamano[0] * candidata.tamano[1] > TOPE_PIXELES:
        raise _Omitida(f"pasa el tope de {TOPE_PIXELES:,} píxeles".replace(",", "."))
    try:
        with warnings.catch_warnings():
            # Pillow solo avisa entre su tope y el doble: acá también es una bomba.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            imagen = Image.open(io.BytesIO(candidata.cargar()), formats=_FORMATOS)
            if _chica(imagen.size):
                return None
            if imagen.format in _SIN_PILLOW:
                raise _Omitida(f"su formato ({imagen.format}) no se puede leer")
            if imagen.width * imagen.height > TOPE_PIXELES:
                raise _Omitida(f"pasa el tope de {TOPE_PIXELES:,} píxeles".replace(",", "."))
            imagen.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise _Omitida("pasa el tope de píxeles de Pillow") from error
    except (OSError, ValueError, SyntaxError, RuntimeError) as error:
        # Lo que levanta Pillow con un archivo que no reconoce o que viene cortado, y PyMuPDF
        # con una imagen que no puede sacar.
        raise _Omitida("no se pudo abrir") from error
    return imagen


def abrir_fotografia(
    contenido: bytes, capacidad: str, *, formatos: tuple[str, ...] | None = _FORMATOS_DE_FOTOGRAFIA
) -> Image.Image:
    """La fotografía (o la imagen que se describe) con a lo sumo `TOPE_PIXELES`, lo más cerca
    posible del tope, por los 2 GB del trabajador (SEG-17). Una más grande no se rechaza: un JPEG
    (o el MPO de un celular) se decodifica ya achicado a 1/2, 1/4 o 1/8 sin bajar del tope; un PNG
    o un TIFF (un escaneo A3 a 600 dpi), entero si entra en `TOPE_DE_DECODIFICACION_BYTES`. Las
    dos se llevan después al tope con `resize`, con su orientación.

    Raises:
        LimiteExcedido: Si decodificarla pasa `TOPE_DE_DECODIFICACION_BYTES`.
    """
    try:
        with warnings.catch_warnings():
            # El tamaño lo mide este tope, no el aviso de bomba de Pillow (89 Mpx).
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            imagen = Image.open(io.BytesIO(contenido), formats=formatos)
    except Image.DecompressionBombError as error:
        # Pillow corta antes de decodificar, arriba del doble de su tope (178 Mpx).
        medida = re.search(r"\((\d+) pixels\)", str(error))
        tope_de_pillow = 2 * (Image.MAX_IMAGE_PIXELS or 0)
        alcanzado = int(medida[1]) if medida else tope_de_pillow + 1
        raise LimiteExcedido(capacidad, "píxeles", tope_de_pillow, alcanzado) from error
    if imagen.width * imagen.height <= TOPE_PIXELES:
        return imagen
    if imagen.format in _CON_DRAFT:
        _achicar_al_decodificar(imagen)
    pixeles = imagen.width * imagen.height
    tope = TOPE_DE_DECODIFICACION_BYTES // _bytes_por_pixel(imagen.mode)
    if pixeles > tope:
        raise LimiteExcedido(capacidad, "píxeles", tope, pixeles)
    # Un TIFF aplica su orientación al decodificarse y la borra; la que queda, `resize` no la
    # copia en todos los formatos: se le vuelve a poner a la achicada.
    imagen.load()
    orientacion = imagen.getexif().get(ExifTags.Base.Orientation)
    previo = _PARA_ACHICAR.get(imagen.mode)
    convertida = imagen if previo is None else imagen.convert(
        "RGBA" if "transparency" in imagen.info else previo
    )
    escala = math.sqrt(TOPE_PIXELES / pixeles)
    tamano = (max(1, int(imagen.width * escala)), max(1, int(imagen.height * escala)))
    achicada = convertida.resize(tamano, Image.Resampling.LANCZOS, reducing_gap=2.0)
    if orientacion:
        achicada.getexif()[ExifTags.Base.Orientation] = orientacion
    return achicada


_CON_DRAFT = ("JPEG", "MPO")
"""Los que se pueden decodificar ya achicados: una foto de celular suele abrir como MPO."""


def _achicar_al_decodificar(imagen: Image.Image) -> None:
    """`draft` a la escala más chica (1/8, 1/4, 1/2) que no deja la imagen bajo `TOPE_PIXELES`:
    `draft` elige la que da al menos lo pedido. Un JPEG de 26 Mpx se decodifica entero."""
    ancho, alto = imagen.size
    for divisor in (8, 4, 2):
        if (ancho // divisor) * (alto // divisor) >= TOPE_PIXELES:
            imagen.draft(imagen.mode, (ancho // divisor, alto // divisor))
            return


_PARA_ACHICAR = {"1": "L", "P": "RGB", "I;16": "I", "I;16B": "I", "I;16L": "I", "I;16N": "I"}
"""Se convierten antes de achicar: `resize` no los promedia (1 bit y paleta) o no los acepta."""


def _bytes_por_pixel(modo: str) -> int:
    """Lo que ocupa en Pillow un píxel decodificado (1 byte en gris y en paleta, 2 en gris de 16
    bits, 4 en color: RGB también usa 4), más su copia si `_PARA_ACHICAR` la convierte."""
    if modo.startswith("I;16"):
        return 2 + 4
    return {"1": 1 + 1, "L": 1, "P": 1 + 4}.get(modo, 4)


def dpi_dentro_del_tope(ancho_pt: float, alto_pt: float, dpi: int) -> int:
    """El `dpi` para renderizar una página de `ancho_pt` × `alto_pt` sin pasar `TOPE_PIXELES`:
    una página de 14 400 pt de lado pesa 522 bytes y a 200 dpi pide 4,8 GB (SEG-17)."""
    pixeles = ancho_pt * alto_pt * (dpi / 72) ** 2
    if pixeles <= TOPE_PIXELES:
        return dpi
    return max(1, int(dpi * (TOPE_PIXELES / pixeles) ** 0.5))


def de_la_pagina(documento: pymupdf.Document, numero: int) -> list[Embebida]:
    """Las imágenes de la página `numero` (desde 1) de un PDF con capa de texto, sin su escaneo.
    `get_image_info` da su caja, su tamaño y su huella sin sacarla; las que tienen `xref` se
    sacan como vienen y las que van en línea (`xref` 0) se recortan del render de la página."""
    pagina = documento[numero - 1]
    area = pagina.rect.get_area()
    candidatas = []
    for orden, info in enumerate(pagina.get_image_info(hashes=True, xrefs=True), start=1):
        caja = pymupdf.Rect(info["bbox"]) & pagina.rect
        if caja.is_empty or caja.get_area() > _PROPORCION_DE_ESCANEO * area:
            continue
        if info["xref"]:
            tamano = (info["width"], info["height"])
            cargar = partial(_sacar, documento, info["xref"])
        else:
            escala = _DPI_RECORTE / 72
            tamano = (round(caja.width * escala), round(caja.height * escala))
            cargar = partial(_recortar, documento, numero, caja)
        candidatas.append(
            Embebida(orden, info["digest"].hex(), cargar, pagina=numero, tamano=tamano)
        )
    return candidatas


def _sacar(documento: pymupdf.Document, xref: int) -> bytes:
    extraida = documento.extract_image(xref)
    if not extraida:
        raise ValueError(f"PyMuPDF no saca la imagen {xref}")
    return bytes(extraida["image"])


def _recortar(documento: pymupdf.Document, numero: int, caja: pymupdf.Rect) -> bytes:
    return bytes(documento[numero - 1].get_pixmap(clip=caja, dpi=_DPI_RECORTE).tobytes("png"))


def del_word(contenido: bytes) -> list[Embebida]:
    """Las imágenes del cuerpo de un Word, en el orden del documento, también las de las celdas
    de una tabla y las de un dibujo: cada `a:blip` con su parte (`r:embed`). Una imagen
    vinculada a un archivo externo (`r:link`) no tiene bytes y se saltea, y también la copia
    de `mc:Fallback`, que repite la de `mc:Choice` y la haría pasar por repetida. Los
    encabezados y los pies no se leen, como su texto (142)."""
    documento = Document(io.BytesIO(contenido))
    partes = documento.part.related_parts
    blips = [
        blip
        for blip in documento.element.body.iter(qn("a:blip"))
        if not any(True for _ in blip.iterancestors(_FALLBACK))
    ]
    candidatas: list[Embebida] = []
    huellas: dict[str, str] = {}
    for numero, blip in enumerate(blips, start=1):
        parte = partes.get(blip.get(qn("r:embed")) or "")
        if parte is None:
            continue
        blob: bytes = parte.blob
        nombre = str(parte.partname)
        if nombre not in huellas:  # la misma parte referida mil veces se mide una vez (SEG-16)
            if len(huellas) > _IMAGENES_DISTINTAS:  # una de más: `leer` avisa el corte
                break
            huellas[nombre] = hashlib.sha256(blob).hexdigest()
        candidatas.append(Embebida(numero, huellas[nombre], partial(bytes, blob)))
    return candidatas


def de_la_planilla(encontradas: Sequence[tuple[str, int, bytes]]) -> list[Embebida]:
    """Las imágenes que da `planillas.imagenes`, cada una con su hoja. La misma imagen referida
    muchas veces llega como el mismo objeto: se mide una vez (SEG-16)."""
    huellas: dict[int, str] = {}
    candidatas = []
    for hoja, numero, blob in encontradas:
        if id(blob) not in huellas:
            huellas[id(blob)] = hashlib.sha256(blob).hexdigest()
        candidatas.append(Embebida(numero, huellas[id(blob)], partial(bytes, blob), hoja=hoja))
    return candidatas


def sacar(contenido: bytes, tipo_mime: str, ubicacion: UbicacionDeImagen) -> bytes:
    """Los bytes de la imagen embebida en `ubicacion` del original, como los da su extractor
    (148): la miniatura y la descripción la sacan cada vez, no se guarda una copia.

    Raises:
        ValueError: Si el original no tiene esa imagen.
    """
    if ubicacion.pagina is not None:
        with pymupdf.open(stream=contenido, filetype="pdf") as documento:
            if ubicacion.pagina > documento.page_count:
                raise ValueError(f"El PDF no tiene la página {ubicacion.pagina}.")
            return _la_numero(de_la_pagina(documento, ubicacion.pagina), ubicacion).cargar()
    if ubicacion.hoja is not None:
        return _la_numero(de_la_planilla(planillas.imagenes(contenido)), ubicacion).cargar()
    if tipo_mime != DOCX:
        raise ValueError(f"Un {tipo_mime} no tiene imágenes embebidas.")
    return _la_numero(del_word(contenido), ubicacion).cargar()


def miniatura(contenido: bytes, tipo_mime: str, ubicacion: UbicacionDeImagen) -> bytes:
    """La imagen en `ubicacion` como JPEG de `LADO_DE_MINIATURA_PX` de lado mayor.

    Raises:
        ValueError: Si el original no tiene esa imagen o no se puede abrir (`_Omitida`).
    """
    embebida = Embebida(ubicacion.numero, "", partial(sacar, contenido, tipo_mime, ubicacion))
    try:
        imagen = _abrir(embebida)
    except _Omitida as omitida:
        raise ValueError(str(omitida)) from omitida
    if imagen is None:
        raise ValueError("Es una imagen decorativa.")
    imagen = ocr.sobre_blanco(imagen).convert("RGB")
    imagen.thumbnail((LADO_DE_MINIATURA_PX, LADO_DE_MINIATURA_PX))
    salida = io.BytesIO()
    imagen.save(salida, format="JPEG", quality=85)
    return salida.getvalue()


def _la_numero(candidatas: Sequence[Embebida], ubicacion: UbicacionDeImagen) -> Embebida:
    for candidata in candidatas:
        if candidata.numero == ubicacion.numero and candidata.hoja == ubicacion.hoja:
            return candidata
    raise ValueError(f"El original no tiene la imagen «{ubicacion.texto}».")
