"""Leer planillas y CSV hostiles (DEC-42, DEC-43).

`clasificar` decide si un original sin firma propia es texto delimitado (CSV o TSV), con qué
codificación y separador, o texto plano. Lo llama `originales` en la admisión (DEC-94);
`leer_registros` lee un CSV chico entero, como el de `catalogo carreras --cargar` (022).
`extraer` vuelve un `.xlsx` tablas markdown por hoja, `extraer_csv` vuelve un CSV o TSV una
tabla de una sola hoja, `imagenes` saca las imágenes de las hojas (143) y `digest` resume ese
cuerpo para `resumir` (DEC-44); los llama
`transcribir` de la capa de modelos, que no es un modelo. No conoce la base, la capa de modelos
ni el HTTP.

Las hojas de un `.xlsx` no las lee openpyxl tal como vienen: su modo lectura arma cada `<row>`
entero antes de entregarlo (una fila de 45 MB de XML llegó a 1,26 GB y 84 s, 088) y, sin
`<dimension>`, recorre la hoja entera al abrir el libro. `_Hoja`, un destino de lxml sin árbol
alimentado de a 64 KiB con el tiempo medido entre bloque y bloque, reescribe cada hoja con solo
las filas y columnas que entran en los topes (y una más de cada, que dice si hay más), cuenta
las filas y columnas reales y lee `hidden="1"` de `<row>` y `<col>`. openpyxl lee esa copia
chica, en modo lectura y con los valores guardados (`data_only`): una fórmula nunca se evalúa.
Las hojas, filas y columnas ocultas se omiten con aviso (historia 96).

Los textos compartidos también los lee el filtro, antes que las hojas y de a bloques con
plazo: cada `<si>` queda en su texto recortado (sin la guía fonética), las celdas `t="s"` salen
en línea y la copia lleva un `<sst/>` vacío. Las demás partes las lee openpyxl enteras: pasan
por el mismo parser para rechazar una DTD (openpyxl lee el libro, los estilos y los `.rels` con
lxml, sin defusedxml) y tienen tope de bytes; openpyxl las lee de una copia cuyo `read` mide el
plazo.
"""

from __future__ import annotations

import codecs
import csv
import io
import posixpath
import re
import time as reloj
import unicodedata
import zipfile
import zlib
from array import array
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from itertools import islice
from pathlib import PurePath
from typing import IO, Any, BinaryIO, Literal
from xml.sax.saxutils import escape

import defusedxml.ElementTree
from lxml import etree
from openpyxl import DEFUSEDXML, load_workbook
from openpyxl.utils.cell import column_index_from_string, get_column_letter

from historia_viva.dominio.cuerpo import CuerpoDeFicha, MarcaDeHoja
from historia_viva.dominio.errores import ErrorHistoriaViva
from historia_viva.dominio.originales import CSV, TEXTO_PLANO

__all__ = [
    "TOPES",
    "Clasificacion",
    "Extraccion",
    "MotivoDeRechazo",
    "PlanillaExcesiva",
    "PlanillaIlegible",
    "Topes",
    "clasificar",
    "digest",
    "extraer",
    "extraer_csv",
    "imagenes",
    "leer_registros",
]

MotivoDeRechazo = Literal["sin-hojas", "csv-binario", "codificacion-desconocida", "fuera-de-topes"]
"""Los motivos de `archivo-ilegible` que da `clasificar`."""

_EXTENSIONES = (".csv", ".tsv")
"""Un texto es delimitado por su extensión **y** su contenido; un `.txt` con comas es texto."""
_EXTENSION_TEXTO = ".txt"
"""Solo el `.txt` es texto plano (decisión del usuario, 028): un HTML o un SVG renombrados no."""

_MUESTRA_BYTES = 512 * 1024
"""Lo que se mira para decidir. Menos que el primer bloque de `originales` (1 MiB): así se sabe
si la muestra corta el archivo y un carácter partido al final no cambia la codificación. Lo que
sigue de un CSV se lee con la codificación ya decidida: un byte que no la cumple no la cambia,
rechaza el archivo."""

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_SIN_BOM = ("utf-8", "cp1252", "latin-1")
"""En este orden (DEC-43): Excel en español escribe UTF-8 o cp1252; Latin-1 lee cualquier byte."""


@dataclass(frozen=True)
class Topes:
    """Los de DEC-42. El archivo, el campo de CSV y el tiempo **rechazan** (`PlanillaExcesiva`);
    hojas visibles, filas por hoja contando el encabezado, columnas, celdas del libro,
    caracteres por celda y caracteres del cuerpo **truncan**, con aviso en la Ficha y en la
    marca de la hoja. La guarda de ZIP es de `originales` (DEC-95)."""

    archivo_bytes: int = 10 * 1000 * 1000
    hojas: int = 20
    filas: int = 1000
    columnas: int = 40
    celdas: int = 100_000
    caracteres_celda: int = 1000
    caracteres_cuerpo: int = 300_000
    campo_csv: int = 32 * 1000
    """En caracteres, como cuenta `csv.field_size_limit`."""
    tiempo_s: float = 60
    parte_bytes: int = 2 * 1000 * 1000
    """No es de DEC-42: lo que puede medir descomprimida una parte que openpyxl lee entera
    (estilos, libro, `.rels`; unas de casi 8 MB tardaban 11 s, sin plazo). Las hojas y los
    textos compartidos no lo tienen: los lee el filtro, con plazo."""
    imagenes_distintas: int = 300
    """No es de DEC-42: las imágenes distintas que se leen de un documento, 10 veces
    `imagenes_maximo`. Lo que pesa es descomprimir y medir cada imagen, no el XML que la refiere:
    mil referencias a una imagen de 1 MB ocupaban 1 GB (SEG-16). Un logo repetido en cada hoja
    cuenta una vez. Lo usa también el Word."""


TOPES = Topes()

_NOMBRES_DE_CODIFICACION = {
    "utf-8-sig": "UTF-8 con BOM",
    "utf-8": "UTF-8",
    "utf-16": "UTF-16",
    "cp1252": "Windows-1252",
    "latin-1": "Latin-1",
}
_NOMBRES_DE_SEPARADOR = {
    ";": "punto y coma",
    ",": "coma",
    "\t": "tabulación",
    "|": "barra vertical",
    None: "ninguno, una sola columna",
}
"""Lo que anota la marca de hoja de un CSV (DEC-43): «Codificación detectada: UTF-8 ·
Separador: coma»."""

_SEPARADORES = (";", ",", "\t", "|")
"""En empate gana el primero: `;` es el de Excel en español."""
_REGISTROS = 20


class PlanillaIlegible(ErrorHistoriaViva):
    """Una planilla admitida que no se puede leer: el trabajador muestra el texto a la persona
    y no un «Error interno». `motivo` es del catálogo de `archivo-ilegible`."""

    def __init__(self, motivo: str, detalle: str) -> None:
        super().__init__(f"No se pudo procesar este archivo: {detalle}")
        self.motivo = motivo


MotivoDeExceso = Literal["archivo", "campo", "tiempo", "parte"]


class PlanillaExcesiva(ErrorHistoriaViva):
    """Una planilla que pasa un tope que rechaza (DEC-42). `tope` y `observado` van en bytes
    (`archivo`, `parte`), caracteres (`campo`) o segundos (`tiempo`)."""

    _DETALLE: Mapping[MotivoDeExceso, str] = {
        "archivo": "la planilla pasa el tope de {tope} bytes (tiene {observado}).",
        "campo": "un campo del CSV pasa el tope de {tope} caracteres (tiene {observado}).",
        "tiempo": "leerla tardó más de {tope} s. Dividila en planillas más chicas.",
        "parte": "una parte interna del libro pasa el tope de {tope} bytes.",
    }

    def __init__(self, motivo: MotivoDeExceso, tope: float, observado: float) -> None:
        detalle = self._DETALLE[motivo].format(tope=_miles(tope), observado=_miles(observado))
        super().__init__(f"No se pudo procesar este archivo: {detalle}")
        self.motivo = motivo
        self.tope = tope
        self.observado = observado


class _Plazo:
    """Hasta cuándo se puede leer: el menor entre el tope de DEC-42 y el de quien lee."""

    def __init__(self, topes: Topes, tope_s: float | None) -> None:
        self.tope_s = topes.tiempo_s if tope_s is None else min(tope_s, topes.tiempo_s)
        self._comienzo = reloj.monotonic()

    def exigir(self) -> None:
        transcurrido = reloj.monotonic() - self._comienzo
        if transcurrido >= self.tope_s:
            raise PlanillaExcesiva("tiempo", self.tope_s, round(transcurrido, 1))


@dataclass(frozen=True)
class Clasificacion:
    """Qué es un original sin firma propia.

    `tipo_mime` es `text/csv` (CSV o TSV), `text/plain` (un `.txt`) o `None` si no es ninguno.
    Un CSV que no se puede leer trae `motivo_rechazo`, del catálogo de `archivo-ilegible`.
    """

    tipo_mime: str | None = None
    """Un CSV con un campo de más de 32 KB (`fuera-de-topes`) lo conserva, con su codificación
    y su separador: `extraer_csv` lo lee igual, para decir cuánto mide."""
    codificacion: str | None = None
    """El nombre del códec de Python: `utf-8-sig`, `utf-8`, `utf-16`, `cp1252` o `latin-1`."""
    separador: str | None = None
    """`None` en un CSV de una sola columna."""
    motivo_rechazo: MotivoDeRechazo | None = None
    vacio: bool = False
    """Un `.txt` que no deja texto: la admisión lo rechaza con 415 diciendo que está vacío."""


def clasificar(origen: BinaryIO, nombre: str, *, completo: bool = True) -> Clasificacion:
    """Decide sobre los primeros 512 KiB de `origen`, sin `charset-normalizer` ni `csv.Sniffer`
    (DEC-43); de un CSV más largo lee también el resto, de a 512 KiB, y de todo CSV mira que
    ningún campo pase los 32 KB. `completo=False` dice que `origen` es solo el comienzo del
    archivo: una letra partida al final no lo rechaza."""
    delimitado = nombre.lower().endswith(_EXTENSIONES)
    if not delimitado and not nombre.lower().endswith(_EXTENSION_TEXTO):
        return Clasificacion()
    muestra = origen.read(_MUESTRA_BYTES)
    siguiente = origen.read(1)
    cortada = bool(siguiente)
    try:
        texto, codificacion, decodificador = _decodificar(muestra, final=not cortada)
    except UnicodeDecodeError:
        return Clasificacion(motivo_rechazo="codificacion-desconocida" if delimitado else None)
    if "\x00" in texto:
        return Clasificacion(motivo_rechazo="csv-binario" if delimitado else None)
    # Sin los caracteres de control que quita la transcripción (R-36) ni los de formato
    # invisibles (Cf, como U+200B), ¿queda algo que se vea?
    if all(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in texto):
        if not delimitado:
            return Clasificacion(vacio=True)
        # El catálogo no tiene «vacío»: un CSV sin registros es un libro sin hojas con datos.
        return Clasificacion(motivo_rechazo="sin-hojas")
    if not delimitado:
        return Clasificacion(tipo_mime=TEXTO_PLANO, codificacion=codificacion)
    if cortada and (motivo := _resto_ilegible(siguiente, origen, decodificador, completo)):
        return Clasificacion(motivo_rechazo=motivo)
    # El último registro de la muestra puede estar partido; si es el único, es lo que hay.
    if cortada and "\n" in texto:
        texto = texto[: texto.rfind("\n") + 1]
    separador = _separador(texto)
    excesivo = _campo_excesivo(origen, codificacion, separador)
    return Clasificacion(
        tipo_mime=CSV,
        codificacion=codificacion,
        separador=separador,
        motivo_rechazo="fuera-de-topes" if excesivo else None,
    )


def leer_registros(origen: BinaryIO, nombre: str) -> list[list[str]]:
    """Todos los registros de un CSV chico, con la codificación y el separador de `clasificar`,
    sin los que no tienen nada (Excel deja filas de solo separadores).

    Raises:
        ValueError: No es un `.csv` o `.tsv`, pasa los topes de DEC-42 (10 MB el archivo, 32 KB
            un campo) o no se puede leer entero: otra codificación más allá de la muestra de
            `clasificar`, o una comilla sin cerrar.
    """
    clasificacion = clasificar(origen, nombre)
    if clasificacion.tipo_mime != CSV or clasificacion.codificacion is None:
        raise ValueError(
            f"«{nombre}» no es un CSV legible"
            f" ({clasificacion.motivo_rechazo or 'la extensión tiene que ser .csv o .tsv'})."
        )
    origen.seek(0)
    contenido = origen.read(TOPES.archivo_bytes + 1)
    if len(contenido) > TOPES.archivo_bytes:
        raise ValueError(f"«{nombre}» pasa el tope de 10 MB de un CSV.")
    texto = contenido.decode(clasificacion.codificacion)  # UnicodeDecodeError es ValueError
    registros = csv.reader(
        io.StringIO(texto, newline=""), delimiter=clasificacion.separador or ",", strict=True
    )
    try:
        leidos = [registro for registro in registros if any(c.strip() for c in registro)]
    except csv.Error as error:
        raise ValueError(f"«{nombre}» está malformado: {error}.") from error
    if any(len(campo) > TOPES.campo_csv for registro in leidos for campo in registro):
        raise ValueError(f"«{nombre}» tiene un campo de más de 32 KB.")
    return leidos


@dataclass(frozen=True)
class Extraccion:
    """El cuerpo de una planilla: los avisos de lo que no se leyó y una tabla por hoja."""

    cuerpo: str
    avisos: tuple[str, ...]
    truncada: bool
    """Si algún tope dejó afuera filas, columnas, hojas, celdas o texto."""


_ILEGIBLE = (
    "el libro está dañado o trae contenido que no se permite en un .xlsx. "
    "Abrilo en Excel, guardalo de nuevo como .xlsx y subilo otra vez."
)

# Lo que levanta leer un libro hostil: XML mal formado o con DTD (`SyntaxError`, `ValueError`
# de defusedxml o del filtro), una parte que falta o un índice de texto compartido que no existe
# (`LookupError`), un valor que no es del tipo que dice (`TypeError`), un número de 400 cifras
# (`OverflowError`), un libro sin su parte principal (`OSError`) o un ZIP dañado.
_LIBRO_DANADO = (
    SyntaxError,
    ValueError,
    LookupError,
    TypeError,
    ArithmeticError,
    OSError,
    zipfile.BadZipFile,
    zlib.error,
)


def extraer(
    origen: BinaryIO, *, topes: Topes = TOPES, tope_s: float | None = None
) -> Extraccion:
    """Un `.xlsx` ya admitido (la guarda de ZIP es de `originales`, DEC-95) como tablas
    markdown, cada una bajo su marca de hoja (DEC-74). `tope_s` es el tiempo de quien lee: se
    usa el menor entre él y el de DEC-42.

    Raises:
        PlanillaExcesiva: Pasa el tope del archivo, el de una parte o el tiempo, que se mide
            mientras se lee, también mientras lee openpyxl.
        PlanillaIlegible: Una parte XML trae una DTD, está mal formada o tiene una etiqueta de
            más de 256 KiB, hay nombres de miembro repetidos o el libro no se puede leer.
        RuntimeError: Si openpyxl no tiene defusedxml (`OPENPYXL_DEFUSEDXML=False`).
    """
    if not DEFUSEDXML:  # sin defusedxml, openpyxl lee el XML del libro sin endurecer
        raise RuntimeError("openpyxl tiene que leer las planillas con defusedxml.")
    plazo = _Plazo(topes, tope_s)
    contenido = origen.read()
    if len(contenido) > topes.archivo_bytes:
        raise PlanillaExcesiva("archivo", topes.archivo_bytes, len(contenido))
    try:
        copia, recuentos = _filtrar_libro(contenido, topes, plazo)
        extraccion = _extraer(_CopiaConPlazo(copia, plazo), recuentos, topes)
    except _LIBRO_DANADO as error:
        raise PlanillaIlegible("zip-corrupto", _ILEGIBLE) from error
    plazo.exigir()
    return extraccion


@dataclass
class _Resto:
    """Lo que queda del cuerpo y de las celdas del libro para las hojas que siguen."""

    caracteres: int
    celdas: int


class _CopiaConPlazo(io.BytesIO):
    """La copia filtrada, que mide el plazo cada vez que zipfile le pide bytes: openpyxl no
    tiene dónde cortar mientras lee los textos compartidos o los estilos."""

    def __init__(self, copia: bytes, plazo: _Plazo) -> None:
        super().__init__(copia)
        self._plazo = plazo

    def read(self, cuanto: int | None = -1, /) -> bytes:
        self._plazo.exigir()
        return super().read(cuanto)


def _extraer(
    filtrado: BinaryIO, recuentos: Mapping[str, _Recuento], topes: Topes
) -> Extraccion:
    leido = load_workbook(filtrado, read_only=True, data_only=True, keep_links=False)
    try:
        # Lo oculto no se cataloga sin que su autor lo sepa (historia 96).
        visibles = [hoja for hoja in leido.worksheets if hoja.sheet_state == "visible"]
        ocultas = len(leido.worksheets) - len(visibles)
        omisiones = [_aviso_de_ocultas(ocultas)] if ocultas else []
        recortes: list[str] = []
        if len(visibles) > topes.hojas:
            recortes.append(f"Se muestran {topes.hojas} de {len(visibles)} hojas.")
        tablas: list[str] = []
        resto = _Resto(topes.caracteres_cuerpo, topes.celdas)
        for hoja in visibles[: topes.hojas]:
            # La ruta de la parte es de openpyxl: es la que se filtró con ese nombre.
            recuento = recuentos[hoja._worksheet_path]  # type: ignore[attr-defined]
            tabla, cortada = _tabla(hoja, recuento, topes, omisiones, recortes, resto)
            tablas.append(tabla)
            if cortada:
                break
    finally:
        leido.close()
    avisos = (*omisiones, *recortes)
    return Extraccion("\n\n".join([*avisos, *tablas]), avisos, bool(recortes))


def extraer_csv(
    origen: BinaryIO, nombre: str, *, topes: Topes = TOPES, tope_s: float | None = None
) -> Extraccion:
    """Un CSV o TSV ya admitido como una sola hoja, que se llama como el archivo sin la
    extensión (como la abre Excel), con los topes del `.xlsx` (DEC-42). La marca de la hoja
    anota la codificación y el separador que detectó `clasificar` (DEC-43); el texto queda en
    NFC (R-36) y cada campo pasa por la misma limpieza que una celda.

    Raises:
        PlanillaIlegible: No se puede leer entero (`clasificar` con el archivo completo).
        PlanillaExcesiva: Pasa el tope del archivo, el de un campo (también el de `csv`, 128
            KiB) o el tiempo.
        ValueError: `nombre` no es de un `.csv` ni de un `.tsv`.
    """
    plazo = _Plazo(topes, tope_s)
    clasificacion = clasificar(origen, nombre)
    if clasificacion.motivo_rechazo not in (None, "fuera-de-topes"):
        raise PlanillaIlegible(
            clasificacion.motivo_rechazo, f"«{nombre}» no se puede leer como CSV."
        )
    if clasificacion.tipo_mime != CSV or clasificacion.codificacion is None:
        raise ValueError(f"«{nombre}» no es un CSV: la extensión tiene que ser .csv o .tsv.")
    origen.seek(0)
    contenido = origen.read()
    if len(contenido) > topes.archivo_bytes:
        raise PlanillaExcesiva("archivo", topes.archivo_bytes, len(contenido))
    texto = unicodedata.normalize("NFC", contenido.decode(clasificacion.codificacion))
    # Una sola columna se lee con el NUL de separador: la admisión ya rechazó los CSV con NUL.
    registros = csv.reader(
        io.StringIO(texto, newline=""), delimiter=clasificacion.separador or "\0"
    )
    filas: list[list[str]] = []
    largas: set[tuple[int, int]] = set()
    max_filas = max_columnas = 0
    try:
        for numero, registro in enumerate(registros, start=1):
            if numero % 1000 == 0:
                plazo.exigir()
            largo = max(map(len, registro), default=0)
            if largo > topes.campo_csv:
                raise PlanillaExcesiva("campo", topes.campo_csv, largo)
            campos = []
            for columna, campo in enumerate(registro, start=1):
                celda, recortada = _recortar(campo, topes.caracteres_celda)
                if recortada:
                    largas.add((numero, columna))
                campos.append(celda)
            if ancho := _ancho(campos):
                max_filas, max_columnas = numero, max(max_columnas, ancho)
            if numero <= topes.filas + 1:
                filas.append(campos[: topes.columnas + 1])
    except csv.Error as error:
        # Sin `strict` ni NUL (la admisión los rechaza), lo único que no lee es un campo largo.
        raise PlanillaExcesiva(
            "campo", topes.campo_csv, csv.field_size_limit() + 1
        ) from error
    plazo.exigir()
    anotacion = (
        f"Codificación detectada: {_NOMBRES_DE_CODIFICACION[clasificacion.codificacion]}"
        f" · Separador: {_NOMBRES_DE_SEPARADOR[clasificacion.separador]}"
    )
    recortes: list[str] = []
    tabla, _ = _tabla_de_filas(
        _limpiar(PurePath(nombre).stem),
        filas,
        max_filas,
        max_columnas,
        topes,
        recortes,
        _Resto(topes.caracteres_cuerpo, topes.celdas),
        anotacion=anotacion,
        largas=largas,
    )
    return Extraccion("\n\n".join([*recortes, tabla]), tuple(recortes), bool(recortes))


_RELACIONES = "http://schemas.openxmlformats.org/package/2006/relationships"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DIBUJO = "http://schemas.openxmlformats.org/drawingml/2006/main"


def imagenes(contenido: bytes, *, topes: Topes = TOPES) -> list[tuple[str, int, bytes]]:
    """Las imágenes de las hojas visibles de un `.xlsx` ya leído por `extraer` (143): el nombre
    de la hoja, el número de la imagen en la hoja y sus bytes, en el orden del dibujo.

    `extraer` lee en modo lectura, que no carga imágenes: salen de `xl/media` por las relaciones
    hoja → dibujo → imagen (`a:blip`). Un gráfico es otra parte (`c:chart`) y no se procesa: sus
    datos ya están en las celdas. Las hojas ocultas no dan imágenes, como no dan tablas. Un libro
    cuyas relaciones no se pueden leer no da imágenes: sus tablas ya se leyeron.

    shortcut: solo los espacios de nombres transicionales de OOXML (los de Excel); un libro
    «Strict» no da imágenes. Si aparece uno, sumar los de `purl.oclc.org`.
    """
    encontradas: list[tuple[str, int, bytes]] = []
    leidas: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as libro:
            nombres = set(libro.namelist())

            def xml(ruta: str) -> Any:
                if libro.getinfo(ruta).file_size > topes.parte_bytes:
                    raise ValueError(f"{ruta} pasa el tope de bytes")
                return defusedxml.ElementTree.fromstring(libro.read(ruta))

            def relaciones(ruta: str) -> list[tuple[str, str, str]]:
                """`(Id, Type, destino)` de las relaciones internas de la parte `ruta`."""
                carpeta, nombre = posixpath.split(ruta)
                rels = posixpath.join(carpeta, "_rels", f"{nombre}.rels")
                if rels not in nombres:
                    return []
                return [
                    (r.get("Id", ""), r.get("Type", ""), _destino(carpeta, r.get("Target", "")))
                    for r in xml(rels).iter(f"{{{_RELACIONES}}}Relationship")
                    if r.get("TargetMode") != "External"
                ]

            # El libro, por la relación del paquete (`_rels/.rels`) y no por su nombre.
            [ruta_libro] = [
                destino for _, tipo, destino in relaciones("") if tipo.endswith("/officeDocument")
            ]
            del_libro = {id_: destino for id_, _, destino in relaciones(ruta_libro)}
            visibles = [
                hoja
                for hoja in xml(ruta_libro).iter(f"{{{_PRINCIPAL}}}sheet")
                if hoja.get("state", "visible") == "visible"
            ]
            for hoja in visibles[: topes.hojas]:
                ruta_hoja = del_libro.get(hoja.get(f"{{{_R}}}id", ""))
                if ruta_hoja is None:
                    continue
                numero = 0
                for _, tipo, dibujo in relaciones(ruta_hoja):
                    if not tipo.endswith("/drawing") or dibujo not in nombres:
                        continue
                    medios = {id_: destino for id_, _, destino in relaciones(dibujo)}
                    for blip in xml(dibujo).iter(f"{{{_DIBUJO}}}blip"):
                        medio = medios.get(blip.get(f"{{{_R}}}embed", ""))
                        if medio in nombres:
                            if medio not in leidas:  # la misma imagen se descomprime una vez
                                # Una de más: `imagenes.leer` avisa que se cortó (SEG-16).
                                if len(leidas) > topes.imagenes_distintas:
                                    return encontradas
                                leidas[medio] = libro.read(medio)
                            numero += 1
                            encontradas.append(
                                (_limpiar(hoja.get("name", "")), numero, leidas[medio])
                            )
    except _LIBRO_DANADO:
        return []
    return encontradas


def _destino(carpeta: str, destino: str) -> str:
    """La ruta en el ZIP de una relación: absoluta desde la raíz o relativa a su parte."""
    if destino.startswith("/"):
        return destino.lstrip("/")
    return posixpath.normpath(posixpath.join(carpeta, destino))


def digest(cuerpo: str) -> str:
    """Lo que recibe `resumir` de una planilla (DEC-44): por hoja, su nombre, sus dimensiones,
    el encabezado y 5 filas, con las celdas a 80 caracteres y el total a 6000."""
    partes = []
    for hoja in CuerpoDeFicha.parsear_cuerpo(cuerpo):
        partes.append(
            f"Hoja «{hoja.marca.nombre}»: {hoja.marca.total} filas"
            f" y {len(hoja.columnas)} columnas."
        )
        for fila in [hoja.columnas, *hoja.filas[:5]]:
            partes.append(_fila(_escapar(celda[:80]) for celda in fila))
    return "\n".join(partes)[:6000]


def _aviso_de_ocultas(ocultas: int) -> str:
    return "Se omitió 1 hoja oculta." if ocultas == 1 else f"Se omitieron {ocultas} hojas ocultas."


def _cantidad(numero: int, singular: str, plural: str) -> str:
    return f"{_miles(numero)} {singular if numero == 1 else plural}"


def _nota_de_ocultas(filas: int, columnas: int) -> str:
    partes = [
        *([_cantidad(filas, "fila oculta", "filas ocultas")] if filas else []),
        *([_cantidad(columnas, "columna oculta", "columnas ocultas")] if columnas else []),
    ]
    verbo = "Se omitió" if (filas or columnas) == 1 and len(partes) == 1 else "Se omitieron"
    return f"{verbo} {' y '.join(partes)}."


def _tabla(
    hoja: Any,
    recuento: _Recuento,
    topes: Topes,
    omisiones: list[str],
    recortes: list[str],
    resto: _Resto,
) -> tuple[str, bool]:
    """La marca y la tabla de una hoja de la copia filtrada (ver `_tabla_de_filas`)."""
    nombre = _limpiar(hoja.title)
    # Con `max_row` y `max_col` explícitos, una celda en XFD1048576 no arma un millón de filas.
    leidas = hoja.iter_rows(max_row=topes.filas + 1, max_col=topes.columnas + 1)
    filas: list[list[str]] = []
    largas: set[tuple[int, int]] = set()
    for numero, fila in enumerate(leidas, start=1):
        convertidas = []
        for columna, celda in enumerate(fila, start=1):
            texto, recortada = _celda(celda, topes.caracteres_celda)
            if recortada:
                largas.add((numero, columna))
            convertidas.append(texto)
        filas.append(convertidas)
    notas = []
    if recuento.filas_ocultas or recuento.columnas_ocultas:
        nota = _nota_de_ocultas(recuento.filas_ocultas, len(recuento.columnas_ocultas))
        notas.append(nota)
        omisiones.append(_aviso_de_hoja(nombre, nota))
    return _tabla_de_filas(
        nombre,
        filas,
        recuento.filas,
        recuento.columnas,
        topes,
        recortes,
        resto,
        notas=notas,
        largas=largas,
        sin_leer=recuento.sin_leer,
    )


def _aviso_de_hoja(nombre: str, nota: str) -> str:
    """La nota de la marca, como aviso de la Ficha: «Hoja «X»: se muestran…»."""
    return f"Hoja «{nombre}»: {nota[0].lower()}{nota[1:]}"


def _tabla_de_filas(
    nombre: str,
    filas: list[list[str]],
    max_filas: int,
    max_columnas: int,
    topes: Topes,
    recortes: list[str],
    resto: _Resto,
    *,
    anotacion: str | None = None,
    notas: list[str] | None = None,
    largas: set[tuple[int, int]] | None = None,
    sin_leer: bool = False,
) -> tuple[str, bool]:
    """La marca y la tabla de una hoja, desde su primera fila con datos hasta la última: las
    vacías del medio quedan, así la fila de la tabla es la de la planilla (DEC-112). `filas`
    son las celdas ya convertidas de hasta una fila y una columna más que los topes;
    `max_filas` y `max_columnas`, hasta dónde llegan los datos, contados leyendo; `largas`, las
    celdas (fila, columna) que se recortaron. Lo que no entró va como nota en la marca (después
    de `anotacion`) y como aviso en `recortes`; descuenta de `resto` y dice si el cuerpo o las
    celdas del libro cortaron la tabla (`sin_leer`: el filtro ya dejó de copiar filas)."""
    notas = list(notas or [])
    previas = len(notas)
    mas_filas, mas_columnas = max_filas > topes.filas, max_columnas > topes.columnas
    filas = [fila[: topes.columnas] for fila in filas[: topes.filas]]
    con_datos = [numero for numero, fila in enumerate(filas, start=1) if any(fila)]
    total = max_filas if mas_filas else max(con_datos, default=0)
    if not con_datos:
        if mas_filas or mas_columnas:
            notas.append(
                f"Las primeras {_miles(topes.filas)} filas y {topes.columnas} columnas están"
                " vacías. El resto no se leyó."
            )
            recortes.append(_aviso_de_hoja(nombre, notas[-1]))
        marca = MarcaDeHoja(nombre, 0, 0, total, _anotar(anotacion, notas))
        resto.caracteres -= len(marca.a_texto()) + 2
        return marca.a_texto(), False
    desde, hasta = con_datos[0], con_datos[-1]
    ancho = max(map(_ancho, filas))
    lineas = [_fila((fila + [""] * ancho)[:ancho]) for fila in filas[desde - 1 : hasta]]
    lineas.insert(1, _fila(["---"] * ancho))
    if mas_columnas:
        columnas = _miles(max(max_columnas, topes.columnas + 1))
        notas.append(f"Se muestran {ancho} de {columnas} columnas.")
    previa = MarcaDeHoja(nombre, desde, hasta, total, _anotar(anotacion, notas)).a_texto()
    corte, largo, celdas = None, len(previa), 0
    for cuantas, linea in enumerate(lineas):
        largo += len(linea) + 1
        if cuantas > 1 and largo > resto.caracteres:  # el encabezado y los guiones van siempre
            corte = f"El cuerpo llegó al tope de {_miles(topes.caracteres_cuerpo)} caracteres."
        elif cuantas > 1 and celdas + ancho > resto.celdas:
            corte = f"El libro llegó al tope de {_miles(topes.celdas)} celdas."
        if corte:
            lineas = lineas[:cuantas]
            break
        celdas += 0 if cuantas == 1 else ancho
    if corte is None and sin_leer:
        corte = f"El cuerpo llegó al tope de {_miles(topes.caracteres_cuerpo)} caracteres."
    hasta = desde + len(lineas) - 2
    if mas_filas:
        mostradas = _miles(hasta - desde + 1)
        notas.append(f"Se muestran {mostradas} de {_miles(total)} filas. El resto no se leyó.")
    recortadas = sum(
        1 for fila, columna in largas or () if desde <= fila <= hasta and columna <= ancho
    )
    if recortadas:
        notas.append(
            f"{_cantidad(recortadas, 'celda pasa', 'celdas pasan')} de"
            f" {_miles(topes.caracteres_celda)} caracteres y"
            f" {'se recortó' if recortadas == 1 else 'se recortaron'}."
        )
    recortes.extend(_aviso_de_hoja(nombre, nota) for nota in notas[previas:])
    if corte:
        corte += " El resto no se leyó."
        notas.append(corte)
        recortes.append(corte)
    marca = MarcaDeHoja(nombre, desde, hasta, total, _anotar(anotacion, notas))
    tabla = "\n".join([marca.a_texto(), *lineas])
    resto.caracteres -= len(tabla) + 2
    resto.celdas -= celdas
    return tabla, corte is not None


def _anotar(anotacion: str | None, notas: list[str]) -> str | None:
    return " · ".join(filter(None, [anotacion, " ".join(notas)])) or None


def _ancho(fila: list[str]) -> int:
    """Hasta la última celda con datos."""
    return max((numero for numero, celda in enumerate(fila, start=1) if celda), default=0)


def _miles(numero: float) -> str:
    return f"{numero:,}".replace(",", ".") if isinstance(numero, int) else f"{numero:g}"


_BIDI = dict.fromkeys([0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])
"""Los controles bidireccionales: dan vuelta lo que se ve sin cambiar lo que se lee."""


def _celda(celda: Any, tope: int) -> tuple[str, bool]:
    """La tabla de conversión (DEC-42): fechas en ISO, números con hasta 15 cifras
    significativas, booleanos en castellano y errores como texto; después, `_recortar`. Dice
    también si la celda pasaba los `tope` caracteres."""
    valor = celda.value
    if valor is None:
        texto = ""
    elif isinstance(valor, bool):
        texto = "VERDADERO" if valor else "FALSO"
    elif isinstance(valor, int | float):
        porcentaje = "%" in (celda.number_format or "")
        texto = f"{valor * 100:.15g}%" if porcentaje else f"{valor:.15g}"
    elif isinstance(valor, datetime) and valor.time() == time():
        texto = valor.date().isoformat()
    elif isinstance(valor, datetime):
        texto = valor.isoformat(timespec="seconds")
    elif isinstance(valor, timedelta):  # una duración (`[h]:mm`): `str` diría «1 day, 2:05:00»
        signo, segundos = "-" if valor < timedelta() else "", round(abs(valor.total_seconds()))
        minutos, segundos = divmod(segundos, 60)
        texto = f"{signo}{minutos // 60}:{minutos % 60:02}"
        texto += f":{segundos:02}" if segundos else ""
    else:
        texto = str(valor)
    return _recortar(texto, tope)


def _recortar(texto: str, tope: int) -> tuple[str, bool]:
    """`_limpiar`, recortado a `tope` caracteres y con `|` y `\\` escapados: una celda nunca
    parte la línea ni la tabla. Dice si se recortó."""
    limpio = _limpiar(texto[: _CARACTERES_A_LIMPIAR * tope + 1])
    return _escapar(limpio[:tope]), len(limpio) > tope


_CARACTERES_A_LIMPIAR = 4
"""Por cada carácter que se muestra, cuántos se leen: los controles y los espacios de los
bordes se van al limpiar, y lo que queda tiene que alcanzar para decir si pasa el tope."""


def _escapar(texto: str) -> str:
    return texto.replace("\\", "\\\\").replace("|", "\\|")


def _limpiar(texto: str) -> str:
    """Saltos de línea y controles a espacio, sin controles bidireccionales."""
    return "".join(
        " " if unicodedata.category(caracter) in ("Cc", "Zl", "Zp") else caracter
        for caracter in texto.translate(_BIDI)
    ).strip()


def _fila(celdas: Iterable[str]) -> str:
    return f"| {' | '.join(celdas)} |"


# --- El filtro de las hojas -------------------------------------------------------------------

_BLOQUE = 64 * 1024
_TOPE_ETIQUETA = 256 * 1024
_ETIQUETA_LARGA = re.compile(b"<[^>]{%d}" % _TOPE_ETIQUETA)
"""libxml2 es cuadrático en los atributos de un elemento y no avisa hasta cerrar la etiqueta
(009: 1 MiB de atributos, 3 s de CPU): una más larga que esto se rechaza antes de dársela. Una
etiqueta real de Excel ocupa unos cientos de bytes y el texto de una celda no tiene `<`."""
_PARTES_XML = (".xml", ".rels")
"""Para leer valores hacen falta solo las partes XML; las imágenes y los binarios no se copian."""
_PRINCIPAL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_COLUMNAS_XLSX = 18_278
"""`ZZZ`: la última columna que openpyxl sabe nombrar (Excel llega a `XFD`, la 16 384)."""
_TIPOS_DE_CELDA = frozenset({"b", "d", "e", "inlineStr", "n", "s", "str"})
_REFERENCIA = re.compile(r"([A-Z]{1,3})[0-9]{1,7}")


@dataclass
class _Recuento:
    """Lo que el filtro contó leyendo una hoja entera, sin creerle a su `<dimension>`."""

    filas: int = 0
    """La última fila visible con datos."""
    columnas: int = 0
    """La última columna visible con datos."""
    filas_ocultas: int = 0
    """Las ocultas que tenían datos: ocultar filas vacías no se avisa."""
    columnas_ocultas: set[int] = field(default_factory=set)
    sin_leer: bool = False
    """Si dejó de copiar filas dentro del tope porque las copiadas ya llenaban el cuerpo."""


class _NoEsHoja(Exception):
    """La raíz de la parte no es `worksheet`: se copia tal cual."""


def _filtrar_libro(
    contenido: bytes, topes: Topes, plazo: _Plazo
) -> tuple[bytes, dict[str, _Recuento]]:
    """Una copia del libro con cada hoja reescrita por `_Hoja` y las demás partes XML tal cual,
    y lo que se contó de cada hoja, por el nombre de su parte. La hoja se reconoce por su raíz y
    no por su nombre: una hoja con otro nombre también pasa por el filtro."""
    salida = io.BytesIO()
    recuentos: dict[str, _Recuento] = {}
    with (
        zipfile.ZipFile(io.BytesIO(contenido)) as libro,
        zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as copia,
    ):
        # OPC prohíbe nombres equivalentes sin distinguir mayúsculas: de dos, la copia tendría
        # el último y openpyxl podría buscar el otro (como en la guarda de `originales`).
        nombres = [miembro.filename.lower() for miembro in libro.infolist()]
        if len(nombres) != len(set(nombres)):
            raise ValueError("nombres de miembro repetidos")
        partes = [m for m in libro.infolist() if m.filename.lower().endswith(_PARTES_XML)]
        raices = {}
        for miembro in partes:
            with libro.open(miembro) as parte:
                raices[miembro.filename] = _raiz(parte, plazo)
        de_textos = [nombre for nombre, raiz in raices.items() if raiz == "sst"]
        if len(de_textos) > 1:
            raise ValueError("más de una parte de textos compartidos")
        textos = _TextosCompartidos()
        for nombre in de_textos:
            with libro.open(nombre) as parte:
                textos = _leer_textos(parte, topes, plazo)
        for miembro in partes:
            with copia.open(miembro.filename, "w") as destino:
                if miembro.filename in de_textos:
                    destino.write(f'<sst xmlns="{_PRINCIPAL}"/>'.encode())
                    continue
                with libro.open(miembro) as parte:
                    if recuento := _copiar_parte(parte, destino, topes, plazo, textos):
                        recuentos[miembro.filename] = recuento
    return salida.getvalue(), recuentos


class _Raiz(Exception):
    def __init__(self, nombre: str) -> None:
        super().__init__(nombre)
        self.nombre = nombre


class _BuscaRaiz:
    """Destino del parser que corta en la primera etiqueta; una DTD la rechaza."""

    def doctype(self, *_: object) -> None:
        raise ValueError("una parte XML con DTD")

    def start(self, etiqueta: str, _: Mapping[str, str]) -> None:
        raise _Raiz(etiqueta.rpartition("}")[2])

    def close(self) -> None:
        pass


def _raiz(parte: IO[bytes], plazo: _Plazo) -> str | None:
    """El nombre local de la raíz de una parte, o `None` si no es XML."""
    buscador = _BuscaRaiz()
    parser = etree.XMLParser(target=buscador, resolve_entities=False, no_network=True)
    abierta = b""
    try:
        while bloque := parte.read(_BLOQUE):
            plazo.exigir()
            abierta = _sin_etiquetas_largas(abierta, bloque)
            parser.feed(bloque)
    except _Raiz as raiz:
        return raiz.nombre
    except etree.XMLSyntaxError:
        return None
    return None


class _TextosCompartidos:
    """Los textos de `<si>` en orden, en un solo búfer UTF-8 con sus finales: un millón de
    textos cortos no son un millón de objetos."""

    def __init__(self) -> None:
        self._datos = bytearray()
        self._fines = array("Q")

    def agregar(self, texto: str) -> None:
        self._datos += texto.encode()
        self._fines.append(len(self._datos))

    def __getitem__(self, indice: int) -> str:
        if not 0 <= indice < len(self._fines):
            raise IndexError(f"no hay texto compartido {indice}")
        desde = self._fines[indice - 1] if indice else 0
        return self._datos[desde : self._fines[indice]].decode()


class _Textos:
    """Destino del parser para la parte de textos compartidos: cada `<si>` reducido a su texto
    (los `<t>`, sin los de `<rPh>`), recortado como una celda."""

    def __init__(self, topes: Topes) -> None:
        self.textos = _TextosCompartidos()
        self._tope = _CARACTERES_A_LIMPIAR * topes.caracteres_celda + 1
        self._espacio: str | None = None
        self._en_si = self._fonetica = self._leyendo = False
        self._partes: list[str] = []
        self._largo = 0

    def doctype(self, *_: object) -> None:
        raise ValueError("una parte XML con DTD")

    def start(self, etiqueta: str, _: Mapping[str, str]) -> None:
        if self._espacio is None:
            self._espacio = etiqueta.rpartition("}")[0] + "}"
            return
        nombre = self._nombre(etiqueta)
        if nombre == "si":
            self._en_si, self._partes, self._largo = True, [], 0
        elif nombre == "rPh":
            self._fonetica = True
        elif nombre == "t" and self._en_si and not self._fonetica:
            self._leyendo = True

    def end(self, etiqueta: str) -> None:
        nombre = self._nombre(etiqueta)
        if nombre == "t":
            self._leyendo = False
        elif nombre == "rPh":
            self._fonetica = False
        elif nombre == "si" and self._en_si:
            self._en_si = False
            self.textos.agregar("".join(self._partes))

    def data(self, texto: str) -> None:
        if self._leyendo and self._largo < self._tope:
            self._partes.append(texto[: self._tope - self._largo])
            self._largo += len(self._partes[-1])

    def close(self) -> None:
        pass

    def _nombre(self, etiqueta: str) -> str:
        espacio = self._espacio or ""
        return etiqueta[len(espacio) :] if etiqueta.startswith(espacio) else ""


def _leer_textos(parte: IO[bytes], topes: Topes, plazo: _Plazo) -> _TextosCompartidos:
    """Los textos compartidos, de a 64 KiB y con el plazo entre bloque y bloque."""
    destino = _Textos(topes)
    parser = etree.XMLParser(target=destino, resolve_entities=False, no_network=True)
    abierta = b""
    while bloque := parte.read(_BLOQUE):
        plazo.exigir()
        abierta = _sin_etiquetas_largas(abierta, bloque)
        parser.feed(bloque)
    parser.close()
    return destino.textos


def _copiar_parte(
    parte: IO[bytes],
    destino: IO[bytes],
    topes: Topes,
    plazo: _Plazo,
    textos: _TextosCompartidos,
) -> _Recuento | None:
    """Copia una parte XML, de a 64 KiB y midiendo el tiempo entre bloque y bloque: si su raíz
    es `worksheet`, filtrada y con sus textos compartidos en línea; si no, tal cual y con tope
    de bytes. Una DTD, en cualquier parte, la rechaza. Devuelve lo que contó de la hoja, o
    `None` si no era una."""
    hoja = _Hoja(topes, textos)
    parser = etree.XMLParser(target=hoja, resolve_entities=False, no_network=True)
    pendientes: list[bytes] = []  # lo leído mientras no se sabe si es una hoja
    filtrando: bool | None = None
    abierta = b""
    leidos = 0
    while bloque := parte.read(_BLOQUE):
        plazo.exigir()
        # Lo leído hasta el bloque anterior: con el primero, la raíz todavía no se sabe.
        _exigir_tope_de_parte(hoja, leidos, topes)
        abierta = _sin_etiquetas_largas(abierta, bloque)
        leidos += len(bloque)
        if filtrando is False:
            destino.write(bloque)
            continue
        try:
            parser.feed(bloque)
        except (_NoEsHoja, etree.XMLSyntaxError):
            if hoja.es_hoja:
                raise
            # Antes de la raíz: no es una hoja, o no es XML. openpyxl dirá si la necesita.
            filtrando = False
            destino.write(b"".join([*pendientes, bloque]))
            continue
        if hoja.es_hoja:
            filtrando = True
            destino.write("".join(hoja.salida).encode())
            hoja.salida.clear()
        else:
            pendientes.append(bloque)
    _exigir_tope_de_parte(hoja, leidos, topes)
    if filtrando is None:
        destino.write(b"".join(pendientes))
    if not filtrando:
        return None
    parser.close()
    destino.write(("".join(hoja.salida) + "</sheetData></worksheet>").encode())
    return hoja.recuento


def _exigir_tope_de_parte(hoja: _Hoja, leidos: int, topes: Topes) -> None:
    """Una parte que openpyxl lee entera no pasa `parte_bytes`: las hojas se filtran."""
    if not hoja.es_hoja and leidos > topes.parte_bytes:
        raise PlanillaExcesiva("parte", topes.parte_bytes, leidos)


def _sin_etiquetas_largas(abierta: bytes, bloque: bytes) -> bytes:
    """Rechaza una etiqueta de más de `_TOPE_ETIQUETA`; devuelve lo que queda abierto al final
    del bloque, para mirarlo con el que sigue."""
    datos = abierta + bloque
    if _ETIQUETA_LARGA.search(datos):
        raise ValueError(f"una etiqueta XML de más de {_TOPE_ETIQUETA} bytes")
    inicio = datos.rfind(b"<")
    return datos[inicio:] if inicio >= 0 and b">" not in datos[inicio:] else b""


class _Hoja:
    """Destino del parser de lxml para una parte del libro, sin árbol. Si la raíz no es
    `worksheet`, corta con `_NoEsHoja`; si lo es, deja en `salida` una hoja nueva con solo las
    filas y columnas dentro de los topes (más una de cada), con referencia explícita en cada
    fila y celda, sin las ocultas ni las fórmulas (`data_only`), y con el texto de cada celda
    recortado; y cuenta en `recuento` lo que sigue."""

    def __init__(self, topes: Topes, textos: _TextosCompartidos) -> None:
        self.topes = topes
        self._textos = textos
        self.salida: list[str] = []
        self.recuento = _Recuento()
        self.es_hoja = False
        self.raiz: str | None = None
        self._espacio = ""
        self._ocultas = bytearray(_COLUMNAS_XLSX + 1)
        self._fila = 0
        self._mayor_fila = 0
        self._ultima_fila_copiada = 0
        self._mayor_columna = 0
        self._ultima_columna_copiada = 0
        self._fila_oculta = False
        self._oculta_con_datos = False
        self._celdas: list[str] = []
        self._columna = 0
        self._copiar: tuple[str | None, str | None] | None = None
        """`(t, s)` de la celda abierta, si se copia."""
        self._en_celda = self._en_linea = self._fonetica = self._leyendo = False
        self._con_datos = False
        self._texto: list[str] = []
        self._largo = 0
        self._copiados = 0
        self._llena = False

    def doctype(self, *_: object) -> None:
        # OPC (ECMA-376 parte 2) prohíbe las DTD: sin DTD no hay entidades, ni XXE ni billion
        # laughs. lxml propaga esto desde `feed`, antes de leer el subconjunto interno.
        raise ValueError("una parte XML con DTD")

    def start(self, etiqueta: str, atributos: Mapping[str, str]) -> None:
        if self.raiz is None:
            espacio, _, self.raiz = etiqueta.rpartition("}")
            if self.raiz != "worksheet":
                raise _NoEsHoja
            self.es_hoja, self._espacio = True, f"{espacio}}}"
            self.salida.append(f'<worksheet xmlns="{_PRINCIPAL}"><sheetData>')
            return
        if not etiqueta.startswith(self._espacio):
            return
        nombre = etiqueta[len(self._espacio) :]
        if nombre == "c":
            self._abrir_celda(atributos)
        elif nombre == "row":
            self._abrir_fila(atributos)
        elif not self._en_celda:
            if nombre == "col":
                self._ocultar_columnas(atributos)
        elif nombre == "v":
            self._leyendo = True
        elif nombre == "is":
            self._en_linea = True
        elif nombre == "rPh":  # la guía fonética no es parte del valor
            self._fonetica = True
        elif nombre == "t" and self._en_linea and not self._fonetica:
            self._leyendo = True

    def end(self, etiqueta: str) -> None:
        nombre = etiqueta[len(self._espacio) :] if etiqueta.startswith(self._espacio) else ""
        if nombre in ("v", "t"):
            self._leyendo = False
        elif nombre == "rPh":
            self._fonetica = False
        elif nombre == "is":
            self._en_linea = False
        elif nombre == "c":
            self._cerrar_celda()
        elif nombre == "row":
            self._cerrar_fila()

    def data(self, texto: str) -> None:
        if not self._leyendo:
            return
        if not self._con_datos and texto.strip():
            self._con_datos = True
        tope = _CARACTERES_A_LIMPIAR * self.topes.caracteres_celda + 1
        if self._copiar is not None and self._largo < tope:
            self._texto.append(texto[: tope - self._largo])
            self._largo += len(self._texto[-1])

    def close(self) -> None:
        pass

    def _ocultar_columnas(self, atributos: Mapping[str, str]) -> None:
        if atributos.get("hidden") not in ("1", "true"):
            return
        try:
            desde = max(int(atributos["min"]), 1)
            hasta = min(int(atributos["max"]), _COLUMNAS_XLSX)
        except (KeyError, ValueError):
            return
        if desde <= hasta:
            self._ocultas[desde : hasta + 1] = b"\x01" * (hasta - desde + 1)

    def _abrir_fila(self, atributos: Mapping[str, str]) -> None:
        r = atributos.get("r", "")
        # Sin referencia, la fila sigue a la mayor leída: una repetida no la hace retroceder.
        self._fila = int(r) if r.isdigit() and len(r) <= 7 else self._mayor_fila + 1
        self._mayor_fila = max(self._mayor_fila, self._fila)
        self._fila_oculta = atributos.get("hidden") in ("1", "true")
        self._oculta_con_datos = False
        self._columna = self._mayor_columna = self._ultima_columna_copiada = 0
        self._celdas = []

    def _abrir_celda(self, atributos: Mapping[str, str]) -> None:
        referencia = _REFERENCIA.fullmatch(atributos.get("r", ""))
        columna = self._mayor_columna + 1
        if referencia:
            try:
                columna = column_index_from_string(referencia[1])
            except ValueError:
                pass
        self._columna = min(columna, _COLUMNAS_XLSX)
        self._mayor_columna = max(self._mayor_columna, self._columna)
        self._en_celda, self._con_datos, self._texto, self._largo = True, False, [], 0
        self._copiar = None
        if self._dentro():
            tipo, estilo = atributos.get("t"), atributos.get("s", "")
            self._copiar = (
                tipo if tipo in _TIPOS_DE_CELDA else None,
                estilo if estilo.isdigit() and len(estilo) <= 6 else None,
            )

    def _dentro(self) -> bool:
        """Si la celda abierta es visible, entra en los topes (más una fila y una columna) y va
        después de la última copiada: un `<c r="A2">` o un `<row r="2">` repetidos un millón de
        veces se copian una sola vez."""
        return (
            self._fila > self._ultima_fila_copiada
            and self._columna > self._ultima_columna_copiada
            and not self._fila_oculta
            and not self._ocultas[self._columna]
            and self._fila <= self.topes.filas + 1
            and self._columna <= self.topes.columnas + 1
        )

    def _cerrar_celda(self) -> None:
        self._en_celda = self._en_linea = self._fonetica = self._leyendo = False
        if not self._con_datos:
            return
        if self._fila_oculta:
            self._oculta_con_datos = True
            return
        if self._ocultas[self._columna]:
            self.recuento.columnas_ocultas.add(self._columna)
            return
        self.recuento.filas = max(self.recuento.filas, self._fila)
        self.recuento.columnas = max(self.recuento.columnas, self._columna)
        if self._copiar is None:
            return
        if self._llena:
            self.recuento.sin_leer = True
            return
        tipo, estilo = self._copiar
        texto = "".join(self._texto)
        if tipo == "s":  # la copia no lleva textos compartidos: van en línea
            texto, tipo = self._textos[int(texto)], "inlineStr"
        self._copiados += len(texto)
        # Sin la referencia, el `\r` de una celda llegaría a openpyxl como fin de línea.
        texto = escape(texto, {"\r": "&#13;"})
        valor = (
            f'<is><t xml:space="preserve">{texto}</t></is>'
            if tipo == "inlineStr"
            else f"<v>{texto}</v>"
        )
        atributos = f'r="{get_column_letter(self._columna)}{self._fila}"'
        atributos += f' t="{tipo}"' if tipo else ""
        atributos += f' s="{estilo}"' if estilo else ""
        self._celdas.append(f"<c {atributos}>{valor}</c>")
        self._ultima_columna_copiada = self._columna

    def _cerrar_fila(self) -> None:
        if self._oculta_con_datos:
            self.recuento.filas_ocultas += 1
        if self._celdas:
            self.salida.append(f'<row r="{self._fila}">{"".join(self._celdas)}</row>')
            self._ultima_fila_copiada = self._fila
            # Lo copiado ya llena el cuerpo: lo que sigue no se va a mostrar.
            self._llena = self._copiados > self.topes.caracteres_cuerpo
        self._celdas = []


def _decodificar(
    muestra: bytes, *, final: bool
) -> tuple[str, str, codecs.IncrementalDecoder]:
    """BOM → UTF-8 estricto → cp1252 → Latin-1. Con BOM, el BOM manda. Devuelve también el
    decodificador, que sigue con lo que viene después de la muestra.

    Raises: UnicodeDecodeError si el BOM promete una codificación que el texto no cumple.
    """
    candidatas = [c for bom, c in _BOMS if muestra.startswith(bom)][:1] or list(_SIN_BOM)
    for codificacion in candidatas:
        decodificador = codecs.getincrementaldecoder(codificacion)("strict")
        try:
            return decodificador.decode(muestra, final=final), codificacion, decodificador
        except UnicodeDecodeError:
            if codificacion == candidatas[-1]:
                raise
    raise AssertionError("Latin-1 lee cualquier byte")


def _resto_ilegible(
    siguiente: bytes,
    origen: BinaryIO,
    decodificador: codecs.IncrementalDecoder,
    completo: bool,
) -> MotivoDeRechazo | None:
    """Lo que sigue a la muestra, con la codificación que ella decidió: un byte que no la
    cumple, un NUL o, si `origen` es el archivo entero, una letra partida al final rechazan
    el CSV entero."""
    bloque = siguiente
    try:
        while bloque:
            if "\x00" in decodificador.decode(bloque):
                return "csv-binario"
            bloque = origen.read(_MUESTRA_BYTES)
        if completo:
            decodificador.decode(b"", final=True)
    except UnicodeDecodeError:
        return "codificacion-desconocida"
    return None


def _campo_excesivo(origen: BinaryIO, codificacion: str, separador: str | None) -> bool:
    """Si algún campo pasa los 32 KB (DEC-42), leyendo `origen` entero de a registros: la
    memoria la acota el tope de `csv` (128 KiB por campo), que también lo dice."""
    origen.seek(0)
    texto = io.TextIOWrapper(origen, encoding=codificacion, newline="")
    try:
        registros = csv.reader(texto, delimiter=separador or "\0")
        return any(len(campo) > TOPES.campo_csv for registro in registros for campo in registro)
    except csv.Error:
        return True
    except UnicodeDecodeError:
        # Solo la letra partida al final de un comienzo (`completo=False`): el resto ya se leyó.
        return False
    finally:
        texto.detach()


def _separador(texto: str) -> str | None:
    """El que da la misma cantidad de columnas (más de una) en la mayor proporción de los
    primeros 20 registros, si es más de la mitad; si ninguno, una sola columna."""
    elegido, mejor = None, 0.5
    for separador in _SEPARADORES:
        try:
            columnas = [
                len(registro)
                for registro in islice(
                    csv.reader(io.StringIO(texto, newline=""), delimiter=separador), _REGISTROS
                )
                if registro
            ]
        except csv.Error:  # un campo enorme sin cerrar: este separador no ordena el texto
            continue
        repetidas = Counter(n for n in columnas if n > 1).most_common(1)
        if repetidas and repetidas[0][1] / len(columnas) > mejor:
            elegido, mejor = separador, repetidas[0][1] / len(columnas)
    return elegido
