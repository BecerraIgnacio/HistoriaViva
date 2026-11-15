"""Qué es un original para el dominio: su clase, los MIME que nombra el resto y el tope de
`transcribir`.

Cómo se reconoce un original por su contenido y cuánto puede pesar cada clase lo decide el
módulo `originales` (DEC-48), no este.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

__all__ = [
    "CONVERTIDO_DE",
    "CSV",
    "DOC",
    "DOCX",
    "HEIC",
    "JPEG",
    "TEXTO_PLANO",
    "TOPE_BYTES",
    "XLS",
    "XLSX",
    "ClaseDeOriginal",
    "Contenedor",
    "es_imagen",
    "es_texto",
]

Contenedor = Literal["mov", "matroska,webm"]
"""El demuxer que se le fuerza a `ffprobe` con `-f`: lo elige `ingesta` por la firma del video."""

TOPE_BYTES = 40 * 1024 * 1024
"""Tope de tamaño del original. Es el mismo que acota la capacidad `transcribir`."""

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEXTO_PLANO = "text/plain"
CSV = "text/csv"
JPEG = "image/jpeg"

DOC = "application/msword"
XLS = "application/vnd.ms-excel"
HEIC = "image/heic"
CONVERTIDO_DE = {DOC: DOCX, XLS: XLSX, HEIC: JPEG}
"""Los formatos que el navegador o los lectores no abren y el procesamiento convierte (153): el
original se conserva tal cual y se lee su convertido."""


class ClaseDeOriginal(StrEnum):
    """La clase decide el plan de modelos, el visor y el tope (columna `clase_original`)."""

    IMAGEN = "imagen"
    DOCUMENTO = "documento"
    PLANILLA = "planilla"
    VIDEO = "video"
    TEXTO = "texto"


def es_imagen(tipo_mime: str) -> bool:
    """Si este original es una fotografía y no un documento de texto.

    Es la única pregunta que el resto del sistema le hace al MIME, y de ella dependen tres
    cosas distintas —qué capacidades se invocan, con qué etiqueta entra el archivo al canal
    de datos y qué rótulo ve quien valida—. Vive acá, junto a la detección, para que las tres
    la contesten igual.
    """
    return tipo_mime.startswith("image/")


def es_texto(tipo_mime: str) -> bool:
    """Si este original es texto plano: ya es su propio cuerpo y no se transcribe."""
    return tipo_mime.split(";")[0].strip() == TEXTO_PLANO
