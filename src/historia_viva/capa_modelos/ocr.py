"""OCR de una imagen: preparación, motor, filtro por confianza y orden de lectura (140).

`leer` prepara la imagen, se la da al motor y arma el texto con las palabras que pasan el
filtro. El motor queda detrás de `MotorOcr`; el MVP tiene uno solo, Tesseract con el idioma
español (`TESSERACT`). Es el único módulo que importa `pytesseract`.

La preparación y el umbral salen de la fila de `transcribir` (`AjustesOcr.de`), así que se
cambian sin tocar código. Los valores por omisión son los que midió el banco de ingesta (137)
sobre su corpus, en esta PC, con Tesseract 5.4 `spa`:

- **Gris y escala ×2** (`ocr_escala`): bajó la tasa de error del cartel de Cyber.AR de 0,47 a
  0,21 (con el orden por bandas y columnas). ×3 no mejoró y tarda más. La escala no lleva la
  imagen más allá de `LADO_MAXIMO_PX` ni la achica. Rige para las fotografías: la página
  escaneada de un PDF ya se renderiza a 200 dpi y se lee sin escalar (`local`).
- **Antes de todo:** la rotación del EXIF (las coordenadas quedan como las muestra el
  navegador) y la transparencia aplanada sobre blanco.
- **Inversión si el fondo es oscuro** (`ocr_invertir`): una imagen de brillo medio bajo 128 se
  invierte para que quede texto oscuro sobre fondo claro.
- **Binarización adaptativa** (`ocr_binarizar`): apagada. Contra la media local pierde el
  texto claro sobre las franjas oscuras del cartel: subió el error a 0,45 (la de Sauvola de
  Tesseract, a 0,52). Queda para piezas de papel con sombras.
- **Umbral de confianza** (`ocr_confianza_minima`): 0. Tesseract le da 0 a «Av. Cabildo» y 23
  al correo del cartel, y más de 40 a basura de íconos como «AAA»: cada umbral probado (10, 20,
  30, 45, 60) subió el error en las dos Piezas del corpus. Las líneas sin letras ni cifras se
  descartan siempre.
"""

from __future__ import annotations

import os
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import pytesseract
from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from historia_viva.capa_modelos.contrato import PalabraLeida
from historia_viva.capa_modelos.limites import Capacidad
from historia_viva.dominio.errores import LimiteExcedido

__all__ = [
    "TESSERACT",
    "AjustesOcr",
    "Lectura",
    "MotorOcr",
    "PalabraDelMotor",
    "leer",
    "ordenar",
    "preparar",
    "sobre_blanco",
]

if _comando_tesseract := os.environ.get("HV_TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = _comando_tesseract

_ENTORNO_DE_TESSERACT = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
                         "TESSDATA_PREFIX", "OMP_THREAD_LIMIT")  # fmt: skip
# Tesseract lee archivos ajenos: hereda solo esto, no `HV_BD_URL` ni los secretos (SEG-18).
pytesseract.pytesseract.environ = {
    variable: os.environ[variable] for variable in _ENTORNO_DE_TESSERACT if variable in os.environ
}

LADO_MAXIMO_PX = 4000
"""La escala no estira la imagen más allá de este lado: el cartel ×2 llega justo, y una foto
de cámara ya grande se lee como viene."""

_VENTANA_BINARIZACION_PX = 31
"""Radio de la media local de la binarización: más que el trazo de una letra a escala ×2."""

_CONTRASTE_BINARIZACION = 10
"""Cuánto más oscuro que su entorno tiene que ser un píxel para ser tinta."""

_SEPARACION_DE_BANDAS = 1.5
"""Un hueco vertical de más de 1,5 renglones (la mediana de alto) corta una banda."""

_HUECO_EN_EL_RENGLON = 3
"""Un espacio de más de tres altos de palabra (la mediana) parte el renglón."""


@dataclass(frozen=True)
class AjustesOcr:
    """La preparación y el umbral, leídos de los `parametros` de la fila de `transcribir`."""

    escala: float = 2.0
    invertir: bool = True
    binarizar: bool = False
    confianza_minima: float = 0.0

    @classmethod
    def de(cls, parametros: Mapping[str, Any]) -> AjustesOcr:
        """Los `ocr_*` de la fila; los que faltan quedan con el valor medido."""
        base = cls()
        return cls(
            escala=parametros.get("ocr_escala", base.escala),
            invertir=parametros.get("ocr_invertir", base.invertir),
            binarizar=parametros.get("ocr_binarizar", base.binarizar),
            confianza_minima=parametros.get("ocr_confianza_minima", base.confianza_minima),
        )


@dataclass(frozen=True)
class PalabraDelMotor:
    """Lo que devuelve un motor: la palabra, su caja en la imagen que recibió y su renglón."""

    texto: str
    izquierda: int
    arriba: int
    ancho: int
    alto: int
    confianza: float
    renglon: tuple[int, ...]
    """Identifica el renglón dentro de la imagen; las palabras de un renglón van juntas."""
    parrafo: tuple[int, ...] = ()
    """Identifica el párrafo: entre dos párrafos el texto lleva una línea en blanco, que es
    donde prefiere cortar la fragmentación del índice."""


class MotorOcr(Protocol):
    def palabras(self, imagen: Image.Image, *, tiempo_maximo_s: float) -> list[PalabraDelMotor]:
        """Las palabras de `imagen`, en el orden del motor: las de un renglón, seguidas y de
        izquierda a derecha.

        Raises:
            LimiteExcedido: Si no terminó en `tiempo_maximo_s` (ADR-0004).
        """
        ...


@dataclass(frozen=True)
class Lectura:
    texto: str
    palabras: tuple[PalabraLeida, ...]
    """Las que llegaron al texto, en coordenadas de la imagen original."""


class _Tesseract:
    """Tesseract en español con la segmentación automática (`--psm 3`, la de omisión):
    `--psm 11` y `--psm 6` mezclaron más el cartel."""

    def palabras(self, imagen: Image.Image, *, tiempo_maximo_s: float) -> list[PalabraDelMotor]:
        try:
            datos = pytesseract.image_to_data(
                imagen, lang="spa", timeout=tiempo_maximo_s, output_type=pytesseract.Output.DICT
            )
        except RuntimeError as error:
            # `pytesseract` solo levanta `RuntimeError` por esto: "Tesseract process timeout".
            # Cualquier otro `RuntimeError` es una falla real del motor (falta el idioma 'spa',
            # se rompió el pipe) y no debe quedar disfrazada de límite de tiempo.
            if "timeout" not in str(error).lower():
                raise
            raise LimiteExcedido(
                Capacidad.TRANSCRIBIR, "tiempo", int(tiempo_maximo_s), int(tiempo_maximo_s) + 1
            ) from error
        # Una fila por elemento (página, bloque, párrafo, renglón, palabra); solo las palabras
        # traen texto. El tope de tiempo vale para el proceso mismo, no después (issue 12).
        return [
            PalabraDelMotor(
                texto=texto.strip(),
                izquierda=datos["left"][i],
                arriba=datos["top"][i],
                ancho=datos["width"][i],
                alto=datos["height"][i],
                confianza=float(datos["conf"][i]),
                renglon=(datos["block_num"][i], datos["par_num"][i], datos["line_num"][i]),
                parrafo=(datos["block_num"][i], datos["par_num"][i]),
            )
            for i, texto in enumerate(datos["text"])
            if texto.strip()
        ]


TESSERACT: MotorOcr = _Tesseract()


def sobre_blanco(imagen: Image.Image) -> Image.Image:
    """La imagen sin transparencia, sobre blanco: el gris y el JPEG descartan el alfa, y el texto
    negro sobre transparente quedaría negro sobre negro."""
    if imagen.mode not in ("RGBA", "LA", "PA") and "transparency" not in imagen.info:
        return imagen
    rgba = imagen.convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)


def preparar(imagen: Image.Image, ajustes: AjustesOcr) -> tuple[Image.Image, float]:
    """La imagen derecha según su EXIF, sin transparencia, en gris, escalada, invertida si el
    fondo es oscuro y, si se pide, binarizada; con el factor de escala aplicado, para devolver
    las coordenadas a la imagen como la muestra el navegador."""
    # Una foto de celular trae la rotación en el EXIF: sin aplicarla se lee de costado.
    gris = ImageOps.grayscale(sobre_blanco(ImageOps.exif_transpose(imagen)))
    factor = min(ajustes.escala, max(1.0, LADO_MAXIMO_PX / max(gris.size)))
    if factor != 1:
        tamano = (round(gris.width * factor), round(gris.height * factor))
        gris = gris.resize(tamano, Image.Resampling.LANCZOS)
    if ajustes.invertir and ImageStat.Stat(gris).mean[0] < 128:
        gris = ImageOps.invert(gris)
    if ajustes.binarizar:
        media = gris.filter(ImageFilter.BoxBlur(_VENTANA_BINARIZACION_PX))
        # Tinta: lo que es más oscuro que su entorno por más del contraste; el resto, blanco.
        oscuridad = ImageChops.subtract(media, gris)
        gris = oscuridad.point(lambda v: 0 if v > _CONTRASTE_BINARIZACION else 255)
    return gris, factor


def leer(
    imagen: Image.Image,
    ajustes: AjustesOcr,
    *,
    tiempo_maximo_s: float,
    motor: MotorOcr = TESSERACT,
) -> Lectura:
    """El texto de `imagen` y sus palabras, sin las dudosas ni los renglones sin letras."""
    preparada, factor = preparar(imagen, ajustes)
    leidas = [
        palabra
        for palabra in motor.palabras(preparada, tiempo_maximo_s=tiempo_maximo_s)
        if palabra.confianza >= ajustes.confianza_minima
    ]
    hueco = _HUECO_EN_EL_RENGLON * statistics.median([p.alto for p in leidas] or [0])
    renglones: list[list[PalabraDelMotor]] = []
    anterior: PalabraDelMotor | None = None
    for palabra in leidas:
        # Un hueco grande parte el renglón: el motor une a veces un ícono lejano con el texto,
        # y ese renglón ancho juntaría dos columnas en `ordenar`.
        if (
            anterior is None
            or palabra.renglon != anterior.renglon
            or palabra.izquierda - (anterior.izquierda + anterior.ancho) > hueco
        ):
            renglones.append([])
        renglones[-1].append(palabra)
        anterior = palabra
    con_letras = [
        renglon
        for renglon in renglones
        if any(c.isalnum() for palabra in renglon for c in palabra.texto)
    ]
    ordenados = ordenar(con_letras)
    texto = ""
    for i, renglon in enumerate(ordenados):
        if i:
            texto += "\n\n" if renglon[0].parrafo != ordenados[i - 1][0].parrafo else "\n"
        texto += " ".join(p.texto for p in renglon)
    return Lectura(
        texto=texto,
        palabras=tuple(
            _en_la_original(palabra, factor) for renglon in ordenados for palabra in renglon
        ),
    )


def ordenar(renglones: Sequence[list[PalabraDelMotor]]) -> list[list[PalabraDelMotor]]:
    """Los renglones en orden de lectura por bloques, para que un cronograma conserve juntos
    el día, la actividad y el horario.

    Primero, bandas: franjas horizontales sin huecos de más de `_SEPARACION_DE_BANDAS`
    renglones. Dentro de cada banda, columnas: los renglones que se solapan en horizontal. Las
    columnas van de izquierda a derecha y sus renglones de arriba abajo. Tesseract tomaba la
    columna de días del cartel como un bloque aparte: todos los días salían antes que todas
    las actividades.
    """
    if not renglones:
        return []

    def arriba(renglon: list[PalabraDelMotor]) -> int:
        return min(p.arriba for p in renglon)

    def abajo(renglon: list[PalabraDelMotor]) -> int:
        return max(p.arriba + p.alto for p in renglon)

    def izquierda(renglon: list[PalabraDelMotor]) -> int:
        return min(p.izquierda for p in renglon)

    def derecha(renglon: list[PalabraDelMotor]) -> int:
        return max(p.izquierda + p.ancho for p in renglon)

    hueco = _SEPARACION_DE_BANDAS * statistics.median(abajo(r) - arriba(r) for r in renglones)
    bandas: list[list[list[PalabraDelMotor]]] = []
    fondo = 0
    for renglon in sorted(renglones, key=arriba):
        if not bandas or arriba(renglon) > fondo + hueco:
            bandas.append([])
            fondo = abajo(renglon)
        bandas[-1].append(renglon)
        fondo = max(fondo, abajo(renglon))
    ordenados = []
    for banda in bandas:
        columnas: list[list[list[PalabraDelMotor]]] = []
        borde = 0
        for renglon in sorted(banda, key=izquierda):
            if not columnas or izquierda(renglon) > borde:
                columnas.append([])
                borde = derecha(renglon)
            columnas[-1].append(renglon)
            borde = max(borde, derecha(renglon))
        for columna in columnas:
            ordenados += sorted(columna, key=arriba)
    return ordenados


def _en_la_original(palabra: PalabraDelMotor, factor: float) -> PalabraLeida:
    return PalabraLeida(
        texto=palabra.texto,
        izquierda=round(palabra.izquierda / factor),
        arriba=round(palabra.arriba / factor),
        ancho=round(palabra.ancho / factor),
        alto=round(palabra.alto / factor),
        confianza=palabra.confianza,
    )

