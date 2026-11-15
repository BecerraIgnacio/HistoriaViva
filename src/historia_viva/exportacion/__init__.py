"""El único escritor de archivos de salida (DEC-46, DEC-131): todo CSV del sistema y el `.docx` y el
`.xlsx` del Informe de acreditación salen de acá.

Una celda de texto que empieza con `=`, `+`, `-`, `@`, tab o retorno de carro se ejecuta como
fórmula al abrir el archivo en una planilla, y hay textos que elige quien sube (`nombre_archivo`).
Con la regla en el único escritor, el exportador que venga la hereda sin acordarse. Puro: no
conoce la base ni el HTTP.

El `.docx` es texto estructurado (títulos, párrafos, listas y tablas) sin campos, macros ni
vínculos activos: un enlace va como texto. python-docx solo se importa acá para escribir
(R-23; `capa_modelos` lo usa para leer).

El `.xlsx` lo escribe XlsxWriter (R-23), que solo se importa acá: cada texto va con
`write_string` (nunca fórmula ni vínculo) después de `celda_segura`; sin macros, hojas ocultas
ni vínculos externos.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

import xlsxwriter
from docx import Document

__all__ = [
    "BloqueDeDocumento",
    "Celda",
    "HojaDeSalida",
    "Lista",
    "Parrafo",
    "Tabla",
    "Titulo",
    "celda_segura",
    "escribir_csv",
    "escribir_docx",
    "escribir_xlsx",
]

_PREFIJOS_PELIGROSOS = ("=", "+", "-", "@", "\t", "\r")
"""El conjunto que fija el spec (módulo 30, `celda_segura`): el de OWASP sin el salto de línea.
OWASP también lista `\\n`; sumarlo cambia el contrato de `celda_segura`, y eso lo decide el spec.

Riesgo residual aceptado (OWASP, «field separator»): Excel en es-AR abre el CSV separando con
`;`, no con la coma. Ahí un texto como `x;=1+1` se parte y la segunda celda empieza como fórmula
sin `'`, porque el prefijo mira solo el comienzo del valor entero. El separador del contrato es
la coma (user story 25); quien abre con `;` sale de ese contrato."""


def celda_segura(valor: object) -> object:
    """`valor` con `'` delante si es texto y, tras los espacios iniciales, empieza como fórmula.

    Los números reales, los booleanos y `None` no se tocan: un `-5` numérico sigue siendo `-5`.
    """
    if isinstance(valor, str) and valor.lstrip(" ").startswith(_PREFIJOS_PELIGROSOS):
        return "'" + valor
    return valor


def escribir_csv(columnas: Sequence[str], filas: Iterable[Sequence[object]]) -> bytes:
    """El encabezado y las filas como CSV, con cada celda por `celda_segura`.

    UTF-8 con BOM, separador coma y fin de línea `\\r\\n`. `None` es una celda vacía, los
    booleanos se escriben como en JSON (`true`, `false`), las fechas en ISO 8601 y una lista va
    en una sola celda, unida con `;` para no chocar con la coma que separa las columnas.
    """
    # El BOM es lo que hace que Excel abra las tildes bien sin preguntar la codificación.
    texto = io.StringIO(newline="")
    escritor = csv.writer(texto, lineterminator="\r\n")
    escritor.writerow(celda_segura(columna) for columna in columnas)
    escritor.writerows([celda_segura(_celda(valor)) for valor in fila] for fila in filas)
    return texto.getvalue().encode("utf-8-sig")


def _celda(valor: object) -> object:
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, list):
        return ";".join(str(elemento) for elemento in valor)
    if isinstance(valor, date):  # también `datetime`, que hereda de `date`
        return valor.isoformat()
    return valor


@dataclass(frozen=True)
class Titulo:
    nivel: int
    """0 es el título del documento; 1 a 9, los de sección."""
    texto: str


@dataclass(frozen=True)
class Parrafo:
    texto: str


@dataclass(frozen=True)
class Lista:
    items: tuple[str, ...]


@dataclass(frozen=True)
class Tabla:
    columnas: tuple[str, ...]
    filas: tuple[tuple[str, ...], ...]


BloqueDeDocumento = Titulo | Parrafo | Lista | Tabla

_BIDIRECCIONALES = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_NO_XML = frozenset("\ufffe\uffff")
"""Los dos no-caracteres que XML 1.0 excluye fuera de Cc y Cs: lxml los rechaza."""
_QUITADOS = _BIDIRECCIONALES | _NO_XML
_FECHA_ZIP = (1980, 1, 1, 0, 0, 0)
"""La más vieja que admite un `.zip`, en cada miembro: no depende de cuándo se escribe."""


def escribir_docx(
    bloques: Iterable[BloqueDeDocumento], *, titulo: str, autor: str, fecha: datetime
) -> bytes:
    """Los bloques en orden, como un `.docx` que abre en Word y en Google Docs.

    Las propiedades del documento (título, autor, creado y modificado) las fija quien llama, y
    los miembros del `.zip` llevan una fecha fija: el mismo contenido da los mismos bytes. Los
    caracteres de control (salvo el salto de línea y el tab) y los bidireccionales se quitan.
    """
    documento = Document()
    propiedades = documento.core_properties
    propiedades.title = _limpio(titulo)
    propiedades.author = propiedades.last_modified_by = _limpio(autor)
    propiedades.comments = ""  # la plantilla trae «generated by python-docx»
    propiedades.revision = 1
    # python-docx escribe la fecha tal cual con `Z`: la quiere en UTC y sin zona.
    propiedades.created = propiedades.modified = fecha.astimezone(UTC).replace(tzinfo=None)
    for bloque in bloques:
        match bloque:
            case Titulo(nivel, texto):
                documento.add_heading(_limpio(texto), level=nivel)
            case Parrafo(texto):
                documento.add_paragraph(_limpio(texto))
            case Lista(items):
                for item in items:
                    documento.add_paragraph(_limpio(item), style="List Bullet")
            case Tabla(columnas, filas):
                tabla = documento.add_table(rows=1 + len(filas), cols=len(columnas))
                tabla.style = "Table Grid"
                for fila, valores in zip(tabla.rows, (columnas, *filas), strict=True):
                    for celda, valor in zip(fila.cells, valores, strict=True):
                        celda.text = _limpio(valor)
    crudo = io.BytesIO()
    documento.save(crudo)
    return _con_fecha_fija(crudo)


def _limpio(texto: str) -> str:
    texto = texto.replace("\r\n", "\n")
    return "".join(
        c for c in texto
        if c in "\n\t"
        or (unicodedata.category(c) not in ("Cc", "Cs") and c not in _QUITADOS)
    )


def _con_fecha_fija(crudo: io.BytesIO) -> bytes:
    """python-docx fecha cada miembro con la hora de escritura: se reempaqueta con una fija."""
    salida = io.BytesIO()
    with zipfile.ZipFile(crudo) as entrada, zipfile.ZipFile(salida, "w") as zip_salida:
        for miembro in entrada.infolist():
            info = zipfile.ZipInfo(miembro.filename, date_time=_FECHA_ZIP)
            info.create_system = 0  # el sistema que escribe tampoco cambia los bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            zip_salida.writestr(info, entrada.read(miembro))
    return salida.getvalue()


Celda = str | int | float | date | None
"""`datetime` hereda de `date`: también entra, sin zona o con ella."""


@dataclass(frozen=True)
class HojaDeSalida:
    nombre: str
    columnas: tuple[str, ...]
    filas: tuple[tuple[Celda, ...], ...]
    """Una fila puede tener más o menos celdas que columnas."""


_PRIMERA_FECHA_DE_EXCEL = date(1900, 3, 1)
"""Antes, Excel corre un día (cuenta el 29/02/1900, que no existió) y antes de 1900 muestra
`####`: un Acervo histórico tiene esas Fechas, que van como texto ISO."""
_MAXIMO_DE_CELDA = 32_767
"""Los caracteres que admite una celda de Excel; XlsxWriter cortaría en silencio."""
_RECORTADO = "… [recortado]"
_PROHIBIDOS_EN_HOJA = re.compile(r"[\[\]:*?/\\]")


def escribir_xlsx(
    hojas: Sequence[HojaDeSalida], *, titulo: str, autor: str, fecha: datetime
) -> bytes:
    """Las hojas en orden, como un `.xlsx` que abre en Excel y en Google Sheets.

    Cada texto se limpia como en el `.docx`, pasa por `celda_segura` y se escribe como texto:
    `=HYPERLINK(...)` nunca es fórmula ni vínculo. Los números y las fechas conservan su tipo
    (una `datetime` con zona va en la hora de esa zona); una fecha anterior al 01/03/1900 va
    como texto ISO y un texto de más de 32 767 caracteres se recorta con una marca visible.
    Los nombres de hoja se sanean. Las
    propiedades llevan `fecha` y los miembros del `.zip` una fecha fija: el mismo contenido da
    los mismos bytes.
    """
    crudo = io.BytesIO()
    libro = xlsxwriter.Workbook(crudo, {"in_memory": True})
    # XlsxWriter escribe `created` tal cual con `Z`: la quiere en UTC.
    libro.set_properties({
        "title": _limpio(titulo), "author": _limpio(autor),
        "created": fecha.astimezone(UTC).replace(tzinfo=None),
    })
    formato_fecha = libro.add_format({"num_format": "dd/mm/yyyy"})
    formato_hora = libro.add_format({"num_format": "dd/mm/yyyy hh:mm"})
    for hoja in hojas:
        hoja_xlsx = libro.add_worksheet(_nombre_de_hoja(hoja.nombre))
        for fila, valores in enumerate((hoja.columnas, *hoja.filas)):
            for columna, valor in enumerate(valores):
                if isinstance(valor, datetime):
                    valor = valor.replace(tzinfo=None)
                match valor:
                    case None:
                        pass
                    case str():
                        hoja_xlsx.write_string(fila, columna, _texto_de_celda(valor))
                    case date() if _dia(valor) < _PRIMERA_FECHA_DE_EXCEL:
                        hoja_xlsx.write_string(fila, columna, valor.isoformat())
                    case datetime():
                        hoja_xlsx.write_datetime(fila, columna, valor, formato_hora)
                    case date():
                        hoja_xlsx.write_datetime(fila, columna, valor, formato_fecha)
                    case _:
                        hoja_xlsx.write_number(fila, columna, valor)
    libro.close()
    return _con_fecha_fija(crudo)


def _dia(valor: date) -> date:
    return valor.date() if isinstance(valor, datetime) else valor


def _texto_de_celda(texto: str) -> str:
    seguro = str(celda_segura(_limpio(texto)))
    if len(seguro) <= _MAXIMO_DE_CELDA:
        return seguro
    return seguro[: _MAXIMO_DE_CELDA - len(_RECORTADO)] + _RECORTADO


def _nombre_de_hoja(nombre: str) -> str:
    r"""Sin `[]:*?/\`, saltos ni apóstrofo en los bordes y con 31 caracteres a lo sumo (Excel)."""
    saneado = _PROHIBIDOS_EN_HOJA.sub("_", " ".join(_limpio(nombre).split()))
    return saneado[:31].strip("'") or "Hoja"
