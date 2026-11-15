"""Las Afirmaciones de la descripción de una imagen (ADR-0007, 144).

`describir_imagen` propone la descripción de una foto como una lista de datos cortos; una persona
acepta o rechaza cada uno. Se guardan con la v1 que las propuso y, al firmar, las aceptadas se
escriben en el cuerpo de la Ficha (`dominio.cuerpo.escribir_afirmaciones`): el Índice se
reconstruye solo desde las Fichas (ADR-0001).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from historia_viva.dominio.cruce import Respaldo
from historia_viva.dominio.cuerpo import ubicacion_de_imagen

__all__ = [
    "AVISO_DESCRIPCION_FALLIDA",
    "FALTAN_AFIRMACIONES",
    "FALTAN_DESCRIPCIONES",
    "LECTURA_AUTOMATICA",
    "Afirmacion",
    "DescripcionPropuesta",
    "EstadoAfirmacion",
    "EstadoDeImagen",
    "ImagenDescribible",
    "ParaRehacerResumen",
    "ResumenRehecho",
    "UbicacionDeImagen",
    "descripcion_con_afirmaciones",
]

AVISO_DESCRIPCION_FALLIDA = "La descripción automática falló"
"""Lo que ve quien valida cuando la respuesta no cumplió el esquema: escribe la descripción."""

FALTAN_AFIRMACIONES = "afirmaciones"
"""En `campos_faltantes`, junto a los nueve obligatorios: hay Afirmaciones sin decidir."""

FALTAN_DESCRIPCIONES = "descripciones"
"""En `campos_faltantes`: hay imágenes embebidas con su descripción en curso (148). La firma espera
a que lleguen sus Afirmaciones."""

LECTURA_AUTOMATICA = "Lectura automática"
"""La Procedencia del texto visible de una foto: lo lee el OCR, no un modelo (ADR-0007)."""


def descripcion_con_afirmaciones(tipo_imagen: str, afirmaciones: Iterable[str]) -> str:
    """La descripción de una foto con esquema (144): su clase y debajo sus Afirmaciones, una por
    renglón. Es lo que se resume en la v1 y en el rehecho (146): los dos con la misma forma."""
    return "\n".join((f"{tipo_imagen}.", *afirmaciones))


class EstadoDeImagen(StrEnum):
    """Dónde está la descripción de una imagen embebida sin texto (148)."""

    SIN_DESCRIBIR = "sin_describir"
    """Pasó el tope de las que se describen solas: espera «Describir esta imagen»."""
    ENCOLADA = "encolada"
    """Su trabajo no terminó: la firma espera."""
    DESCRITA = "descrita"
    """Con sus Afirmaciones, o sin ninguna si la descripción falló (el aviso)."""
    FALLIDA = "fallida"
    """Su trabajo falló sin dejar resultado: se puede volver a pedir. No se guarda: lo dice la
    cola."""


@dataclass(frozen=True)
class UbicacionDeImagen:
    """Dónde está una imagen embebida en el original, como la marca de 143."""

    numero: int
    pagina: int | None = None
    hoja: str | None = None

    @property
    def texto(self) -> str:
        """«Página N · Imagen M», «Hoja «H» · Imagen M» o «Imagen M»."""
        return ubicacion_de_imagen(self.numero, pagina=self.pagina, hoja=self.hoja)


@dataclass(frozen=True)
class ImagenDescribible:
    """Una imagen embebida sin texto de la v1 (148). `imagen` la identifica en la Pieza: desde 1,
    en el orden del documento."""

    imagen: int
    ubicacion: UbicacionDeImagen
    estado: EstadoDeImagen
    tipo_imagen: str | None
    """La clase de 139; `None` sin describir o si la descripción falló."""

    alerta_inyeccion: bool = False
    """Una de sus Afirmaciones trae una etiqueta de control (ADR-0003): el modelo copió una
    orden escrita en la imagen."""


class EstadoAfirmacion(StrEnum):
    SIN_DECIDIR = "sin_decidir"
    ACEPTADA = "aceptada"
    RECHAZADA = "rechazada"


@dataclass(frozen=True)
class DescripcionPropuesta:
    """Lo que dejó `describir_imagen` para la v1 de una foto."""

    tipo_imagen: str | None
    """La clase de 139; `None` si la respuesta no cumplió el esquema (lleva el aviso)."""

    afirmaciones: tuple[str, ...] = ()
    respaldos: tuple[Respaldo | None, ...] = ()
    """El cruce de cada Afirmación con el OCR, en su orden (145); vacío si nadie las cruzó."""


@dataclass(frozen=True)
class Afirmacion:
    orden: int
    """Desde 1, en el orden en que las propuso el modelo."""

    texto: str
    estado: EstadoAfirmacion
    decidida_por: UUID | None
    decidida_el: datetime | None
    respaldo: Respaldo | None = None
    """Dónde están sus datos en la imagen según el OCR; `None`: sin respaldo (145)."""

    imagen: int = 0
    """De qué imagen salió: 0 la foto entera (144); desde 1, una embebida (148)."""

    ubicacion: str | None = None
    """La de la imagen embebida («Página 3 · Imagen 2»); `None` en una foto."""


@dataclass(frozen=True)
class ParaRehacerResumen:
    """Con qué se rehace el resumen de una foto (146, ADR-0007): la clase, las Afirmaciones que
    nadie rechazó y el texto visible que leyó el OCR en la v1."""

    tipo_imagen: str
    no_rechazadas: tuple[Afirmacion, ...]
    texto_visible: str


@dataclass(frozen=True)
class ResumenRehecho:
    """Un resumen rehecho sin las Afirmaciones rechazadas: una propuesta, no una versión."""

    texto: str
    rehecho_el: datetime
    informe_inyeccion: dict[str, Any]
