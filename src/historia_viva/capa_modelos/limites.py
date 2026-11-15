"""Topes de tiempo y de tokens por capacidad (ADR-0004, limite duro 3).

**Toda invocacion de la capa de modelos nace con tope.** Una invocacion que excede su
tiempo se cancela y el trabajo vuelve a la cola con las generaciones ya incrementadas
(`arquitectura.md` §5.2). Un prototipo colgado durante la sesion con la SEU quema la sesion
entera (`.scratch/prototipo/spec.md`, "Que no se resuelve barato").

Los topes con los que corre cada capacidad son los de su fila de `configuracion_capacidad`
(RF-32): cambiar un tope es editar la fila, y rige para los trabajos que se encolan despues
(DEC-87). `contrato.py` reexporta `Capacidad` y `Limites`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Capacidad", "Limites"]


class Capacidad(StrEnum):
    """Las cinco capacidades de la capa de modelos (`spec.md` §6).

    Cada una tiene su modelo configurable de forma independiente. Ninguna llamada al motor
    de modelos existe fuera de esta capa: si una se filtra, RNF-01 muere en silencio.
    """

    DESCRIBIR_IMAGEN = "describir_imagen"
    """Borrador de descripcion de una fotografia."""

    TRANSCRIBIR = "transcribir"
    """OCR y extraccion de texto de documentos. **No es un LLM** (`arquitectura.md` §7.4):
    PyMuPDF para PDF con capa de texto, `python-docx` para DOCX y Tesseract con `spa` para
    lo escaneado, detras de esta misma interfaz."""

    RESUMIR = "resumir"
    """Resumen propuesto para el campo `resumen` del frontmatter."""

    EMBEBER = "embeber"
    """Vectores para la busqueda semantica."""

    RESPONDER = "responder"
    """Redaccion sobre fragmentos recuperados: la respuesta en vivo de la Conversacion
    (`responder_en_vivo`, 052)."""

    REDACTAR_EFEMERIDE = "redactar_efemeride"
    """Texto breve de difusión sobre las Fichas Validadas de un día o un mes, con Citas (057)."""


@dataclass(frozen=True)
class Limites:
    """Los topes con los que nace una invocacion.

    `None` significa "esta capacidad no acota por ese eje", no "sin limite":
    `describir_imagen` no acota la entrada porque su entrada es una imagen normalizada a
    1024 px de lado mayor, `transcribir` acota por tamano del documento (80 paginas /
    40 MB) y no por tokens, y `embeber` no produce tokens de salida sino un vector.
    """

    tiempo_maximo_s: int
    """Tope de tiempo de la operacion completa, en segundos. Se aplica como `timeout` del
    cliente del motor. Siempre presente: es el eje que ADR-0004 no deja negociar."""

    tope_tokens_entrada: int | None
    """Tope de tokens de entrada. Se aplica como truncado previo de la entrada."""

    tope_tokens_salida: int | None
    """Tope de tokens de salida. Se aplica como parametro de la peticion al motor."""
