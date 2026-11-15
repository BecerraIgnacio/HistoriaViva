"""El Word como texto con estructura (142): párrafos, títulos, listas y tablas en su orden.

El texto es literal del archivo y va rotulado «Del archivo» (135): ningún modelo lo toca. Un
título va como encabezado markdown, un `#` por nivel, y parte el Fragmento
(`dominio.cuerpo.segmentos`); un ítem de lista, con `- ` o su número y sangrado por nivel; una
tabla, como tabla markdown, una fila por línea. Lo llama `transcribir` (`local`), que escapa
las marcas que traiga el texto (`escapar_marcas`) y le pone la suya de página.
"""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.document import Document as DocumentoWord
from docx.oxml.exceptions import InvalidXmlError
from docx.table import Table
from docx.text.paragraph import Paragraph

__all__ = ["texto_del_word"]

# Una cadena `basedOn` más larga es un archivo armado: Word no anida tanto (SEG-15).
_PROFUNDIDAD_DE_ESTILOS = 20


def texto_del_word(contenido: bytes) -> str:
    documento = Document(io.BytesIO(contenido))
    numeracion = _Numeracion(documento)
    lineas: list[str] = []
    for bloque in documento.iter_inner_content():
        if isinstance(bloque, Table):
            lineas += _tabla(bloque)
        elif bloque.text.strip():
            lineas.append(_parrafo(bloque, numeracion))
    return "\n".join(lineas)


def _parrafo(parrafo: Paragraph, numeracion: _Numeracion) -> str:
    nivel = _nivel_de_titulo(parrafo)
    if nivel:
        return f"{'#' * nivel} {' '.join(parrafo.text.split())}"
    return numeracion.prefijo(parrafo) + parrafo.text


def _nivel_de_titulo(parrafo: Paragraph) -> int:
    nombre = parrafo.style.name if parrafo.style is not None else ""
    if nombre == "Title":
        return 1
    if nombre.startswith("Heading ") and nombre[8:].isdigit():
        return min(max(int(nombre[8:]), 1), 6)
    return 0


def _tabla(tabla: Table) -> list[str]:
    """Una fila por línea; la primera es el encabezado, como en una planilla. Una celda
    combinada repite su texto en cada columna que ocupa."""
    filas = [[_celda(celda.text) for celda in fila.cells] for fila in tabla.rows]
    filas = [fila for fila in filas if any(fila)]
    lineas = [f"| {' | '.join(fila)} |" for fila in filas]
    if lineas:
        lineas.insert(1, f"| {' | '.join(['---'] * len(filas[0]))} |")
    return lineas


def _celda(texto: str) -> str:
    """En una línea y con `|` y `\\` escapados: una celda nunca parte la fila ni la tabla."""
    return " ".join(texto.split()).replace("\\", "\\\\").replace("|", "\\|")


class _Numeracion:
    """El prefijo de cada ítem de lista: la numeración del párrafo o, si no tiene, la de su
    estilo (`List Number`, `List Paragraph` con `w:numPr`). Cuenta por lista y nivel, y un ítem
    de un nivel reinicia los de abajo.

    shortcut: los formatos con letras o romanos se numeran con cifras y se ignoran los
    `lvlOverride`; si un archivo real lo necesita, leerlos de `w:lvl`.
    """

    def __init__(self, documento: DocumentoWord) -> None:
        self._documento = documento
        self._cuentas: dict[int, list[int]] = {}

    def prefijo(self, parrafo: Paragraph) -> str:
        lista = _lista(parrafo)
        if lista is None:
            return ""
        num_id, nivel = lista
        formato, inicio = self._formato(num_id, nivel)
        sangria = "  " * nivel
        if formato == "bullet":
            return f"{sangria}- "
        if formato == "none":
            return sangria
        cuentas = self._cuentas.setdefault(num_id, [0] * 9)
        cuentas[nivel] += 1
        cuentas[nivel + 1 :] = [0] * (8 - nivel)
        return f"{sangria}{inicio + cuentas[nivel] - 1}. "

    def _formato(self, num_id: int, nivel: int) -> tuple[str, int]:
        """`numFmt` y `start` del nivel. Sin definición o con una rota (un `w:num` sin
        `abstractNumId`, un `start` que no es número), una viñeta: un Word mal armado no tumba
        `transcribir`."""
        try:
            numeracion = self._documento.part.numbering_part.element
            abstracto = numeracion.num_having_numId(num_id).abstractNumId.val
            lvl = f'w:abstractNum[@w:abstractNumId="{abstracto}"]/w:lvl[@w:ilvl="{nivel}"]'
            formato = numeracion.xpath(f"{lvl}/w:numFmt/@w:val")
            inicio = numeracion.xpath(f"{lvl}/w:start/@w:val")
            return (formato[0] if formato else "bullet"), int(inicio[0]) if inicio else 1
        except (AttributeError, InvalidXmlError, KeyError, NotImplementedError, ValueError):
            return "bullet", 1


def _lista(parrafo: Paragraph) -> tuple[int, int] | None:
    """`(numId, ilvl)` del párrafo o de la cadena de su estilo; `numId` 0 es «sin lista»."""
    fuentes = [parrafo._p.pPr]
    estilo = parrafo.style
    vistos: set[str | None] = set()
    # Un `basedOn` en ciclo (A → B → A) colgaría al trabajador para siempre (SEG-15).
    while (
        estilo is not None
        and estilo.style_id not in vistos
        and len(vistos) < _PROFUNDIDAD_DE_ESTILOS
    ):
        vistos.add(estilo.style_id)
        fuentes.append(estilo.element.pPr)
        estilo = estilo.base_style
    for ppr in fuentes:
        # python-docx tipa `numPr` como su descriptor: el elemento real se lee como `Any`.
        num_pr: Any = ppr.numPr if ppr is not None else None
        if num_pr is not None and num_pr.numId is not None:
            num_id = num_pr.numId.val
            nivel = num_pr.ilvl.val if num_pr.ilvl is not None else 0
            return (num_id, min(max(nivel, 0), 8)) if num_id else None
    return None
