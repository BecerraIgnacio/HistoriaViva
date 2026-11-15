"""`exportacion`: el único escritor de CSV, que neutraliza las fórmulas (DEC-46).

Una celda de texto que empieza con `=`, `+`, `-`, `@`, tab o retorno de carro se ejecuta como
fórmula al abrir el archivo en una planilla, y `nombre_archivo` lo elige quien sube. Se prueba en
proceso: son funciones puras.
"""

from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from docx import Document
from openpyxl import load_workbook

from historia_viva.exportacion import (
    HojaDeSalida,
    Lista,
    Parrafo,
    Tabla,
    Titulo,
    celda_segura,
    escribir_csv,
    escribir_docx,
    escribir_xlsx,
)

pytestmark = pytest.mark.historia(196)


@pytest.mark.parametrize(
    "texto",
    ["=1+1", "+x", "-x", "@x", "\t=x", "\r=x", "  =x", '=HYPERLINK("http://x")'],
)
def test_un_texto_que_empieza_como_formula_sale_con_apostrofo(texto):
    assert celda_segura(texto) == "'" + texto


@pytest.mark.parametrize(
    "valor", [-5, -2.5, None, "acta del consejo 1975.pdf", "", "x=1", True]
)
def test_un_numero_un_nulo_y_un_texto_normal_no_cambian(valor):
    assert celda_segura(valor) == valor


def releer(contenido: bytes) -> list[list[str]]:
    """Lo que ve una planilla que abre el archivo: el BOM dice la codificación."""
    return list(csv.reader(io.StringIO(contenido.decode("utf-8-sig"), newline="")))


@pytest.mark.historia(197)
def test_comas_comillas_y_saltos_de_linea_se_releen_sin_perdida():
    filas = [
        ["acta, consejo 1975.pdf", 'la "primera" sesión', "dos\r\nlíneas"],
        ["sin nada raro", "línea\nsola", 'termina en "'],
    ]

    contenido = escribir_csv(["nombre", "nota", "cuerpo"], filas)

    assert releer(contenido) == [["nombre", "nota", "cuerpo"], *filas]


@pytest.mark.historia(197)
def test_cada_celda_del_csv_pasa_por_celda_segura():
    contenido = escribir_csv(
        ["nombre", "campos", "-duracion_s"],
        [['=HYPERLINK("http://x")', ["@SUM(A1)", "tipo"], -5], ["\t=x", ["tipo", "=x"], 3]],
    )

    # La lista se neutraliza ya unida: lo que se ejecutaría es el principio de la celda.
    assert releer(contenido) == [
        ["nombre", "campos", "'-duracion_s"],
        ['\'=HYPERLINK("http://x")', "'@SUM(A1);tipo", "-5"],
        ["'\t=x", "tipo;=x", "3"],
    ]


@pytest.mark.historia(197)
def test_utf8_con_bom_coma_crlf_booleanos_y_listas_con_punto_y_coma():
    contenido = escribir_csv(
        ["nombre", "editado", "campos", "faltantes", "duracion_s", "guardada_el"],
        [
            ["Inscripción", True, ["tipo", "origen"], [], 42, None],
            ["acta", False, [], ["origen"], -5, ""],
        ],
    )

    # El BOM es lo que hace que Excel abra las tildes bien sin preguntar la codificación.
    assert contenido == (
        "﻿nombre,editado,campos,faltantes,duracion_s,guardada_el\r\n"
        "Inscripción,true,tipo;origen,,42,\r\n"
        "acta,false,,origen,-5,\r\n"
    ).encode()


@pytest.mark.historia(197)
def test_las_fechas_salen_en_iso_8601():
    contenido = escribir_csv(
        ["abierta_el", "fecha"],
        [[datetime(1975, 5, 3, 14, 30, tzinfo=UTC), date(1975, 5, 3)]],
    )

    assert releer(contenido)[1] == ["1975-05-03T14:30:00+00:00", "1975-05-03"]


# 070: el `.docx` del Informe de acreditación (DEC-131, R-23).

GENERADO = datetime(2026, 10, 1, 10, 5, tzinfo=timezone(timedelta(hours=-3)))
BLOQUES = (
    Titulo(0, "Informe de acreditación"),
    Parrafo('=HYPERLINK("http://x")'),
    Lista(("primero", "segundo")),
    Tabla(("Rol", "Persona"), (("RGC", "María"), ("Coordinador de Extensión", "Jorge"))),
    Titulo(1, "Transferencia"),
    Parrafo("Cita: pza_1 · https://historiaviva.fie.undef.edu.ar/api/piezas/1/original"),
)


def _docx(bloques=BLOQUES) -> bytes:
    return escribir_docx(bloques, titulo="Informe", autor="Historia Viva", fecha=GENERADO)


def _parrafos(contenido: bytes) -> list[str]:
    return [p.text for p in Document(io.BytesIO(contenido)).paragraphs]


@pytest.mark.rf("RF-49")
@pytest.mark.historia(276)
def test_el_docx_relee_titulos_parrafos_listas_y_tablas_en_orden():
    documento = Document(io.BytesIO(_docx()))

    assert [(p.style.name, p.text) for p in documento.paragraphs] == [
        ("Title", "Informe de acreditación"),
        ("Normal", '=HYPERLINK("http://x")'),
        ("List Bullet", "primero"),
        ("List Bullet", "segundo"),
        ("Heading 1", "Transferencia"),
        ("Normal", "Cita: pza_1 · https://historiaviva.fie.undef.edu.ar/api/piezas/1/original"),
    ]
    [tabla] = documento.tables
    assert [[c.text for c in fila.cells] for fila in tabla.rows] == [
        ["Rol", "Persona"], ["RGC", "María"], ["Coordinador de Extensión", "Jorge"],
    ]
    propiedades = documento.core_properties
    assert (propiedades.title, propiedades.author, propiedades.created, propiedades.modified) == (
        "Informe", "Historia Viva", GENERADO, GENERADO,
    )


@pytest.mark.rf("RF-49")
@pytest.mark.historia(276)
def test_el_docx_no_tiene_campos_macros_ni_vinculos_activos():
    """Una fórmula o un enlace del Acervo quedan como texto en un tramo (`w:t`)."""
    with zipfile.ZipFile(io.BytesIO(_docx())) as paquete:
        partes = {nombre: paquete.read(nombre) for nombre in paquete.namelist()}

    assert not [n for n in partes if re.search(r"vba|\.bin$|activeX|embeddings", n, re.I)]
    xml = b"".join(partes.values())
    for prohibido in (b"<w:hyperlink", b"<w:fldSimple", b"<w:instrText", b"<w:fldChar",
                      b'TargetMode="External"', b"/relationships/hyperlink", b"macroEnabled",
                      b"attachedTemplate"):
        assert prohibido not in xml, prohibido
    assert b'<w:t>=HYPERLINK("http://x")</w:t>' in partes["word/document.xml"]


@pytest.mark.rf("RF-49")
@pytest.mark.historia(276)
def test_el_docx_quita_los_caracteres_de_control_y_bidireccionales():
    sucio = (
        "acta" + chr(0x202E) + "fdp.exe" + chr(7) + chr(0x200F) + " de" + chr(0x2066)
        + chr(0xFFFE) + " 1975" + chr(0xFFFF)
    )
    linea = "dos" + chr(13) + chr(10) + "líneas"

    [limpio, con_salto] = _parrafos(_docx((Parrafo(sucio), Parrafo(linea))))

    assert limpio == "actafdp.exe de 1975"
    assert con_salto == "dos" + chr(10) + "líneas"


@pytest.mark.rf("RF-49")
@pytest.mark.historia(276)
def test_el_mismo_contenido_da_los_mismos_bytes_aunque_se_escriba_en_otro_momento(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000_000_000.0)
    antes = _docx()
    monkeypatch.setattr(time, "time", lambda: 1_900_000_000.0)
    despues = _docx()

    assert antes == despues
    with zipfile.ZipFile(io.BytesIO(antes)) as paquete:
        assert {i.date_time for i in paquete.infolist()} == {(1980, 1, 1, 0, 0, 0)}


# 071: el `.xlsx` del Informe de acreditación (DEC-131, R-23).

HOJAS = (
    HojaDeSalida("Resumen", ("Dato", "Valor"), (
        ("Desde", date(2018, 1, 1)),
        ("Aprobación RGC", "María", datetime(2026, 10, 2, 9, 30, tzinfo=GENERADO.tzinfo)),
        ("Piezas incluidas", 2),
        ("Integridad", 0.5),
        ("Negativo", -5),
        ("Vacía", None),
    )),
    HojaDeSalida("Piezas", ("Resumen", "Cita"), (
        ('=HYPERLINK("http://x")', "https://historiaviva.fie.undef.edu.ar/api/piezas/1/original"),
        ("+x", "@x"),
        ("-3", " =1+1"),
        ("	=x", "normal"),
    )),
)


def _xlsx(hojas=HOJAS) -> bytes:
    return escribir_xlsx(hojas, titulo="Informe", autor="Historia Viva", fecha=GENERADO)


def _libro(contenido: bytes):
    # Sin `data_only`: una fórmula se leería como fórmula (`data_type == "f"`), no como su valor.
    return load_workbook(io.BytesIO(contenido))


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_xlsx_escribe_cada_texto_como_texto_con_celda_segura():
    hoja = _libro(_xlsx())["Piezas"]

    assert [[(c.value, c.data_type) for c in fila] for fila in hoja.iter_rows()] == [
        [("Resumen", "s"), ("Cita", "s")],
        [("'=HYPERLINK(\"http://x\")", "s"),
         ("https://historiaviva.fie.undef.edu.ar/api/piezas/1/original", "s")],
        [("'+x", "s"), ("'@x", "s")],
        [("'-3", "s"), ("' =1+1", "s")],
        [("'	=x", "s"), ("normal", "s")],
    ]
    assert not hoja._hyperlinks


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_xlsx_conserva_numeros_y_fechas_y_las_propiedades():
    libro = _libro(_xlsx())
    filas = list(libro["Resumen"].iter_rows(min_row=2, values_only=True))

    # openpyxl completa cada fila hasta la última columna usada.
    assert filas == [
        ("Desde", datetime(2018, 1, 1), None),
        ("Aprobación RGC", "María", datetime(2026, 10, 2, 9, 30)),
        ("Piezas incluidas", 2, None),
        ("Integridad", 0.5, None),
        ("Negativo", -5, None),
        ("Vacía", None, None),
    ]
    assert libro["Resumen"]["B2"].number_format == "dd/mm/yyyy"
    assert libro.sheetnames == ["Resumen", "Piezas"]
    propiedades = libro.properties
    assert (propiedades.title, propiedades.creator, propiedades.created) == (
        "Informe", "Historia Viva", GENERADO.astimezone(UTC).replace(tzinfo=None),
    )


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_xlsx_sanea_los_nombres_de_hoja_y_limpia_los_textos():
    sucio = "acta" + chr(0x202E) + "fdp" + chr(7) + chr(0xFFFE) + " 1975"
    hojas = (
        HojaDeSalida("'Hoja [1]: a*b?c/d\\e con un nombre muy largo'", ("x",), ((sucio,),)),
        HojaDeSalida(chr(0x200F), ("y",), ()),
    )

    libro = _libro(_xlsx(hojas))

    assert libro.sheetnames == ["Hoja _1__ a_b_c_d_e con un nom", "Hoja"]
    assert libro.worksheets[0]["A2"].value == "acta" + "fdp 1975"


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_xlsx_no_tiene_macros_hojas_ocultas_ni_vinculos_externos():
    with zipfile.ZipFile(io.BytesIO(_xlsx())) as paquete:
        partes = {nombre: paquete.read(nombre) for nombre in paquete.namelist()}

    assert not [
        n for n in partes if re.search(r"vba|\.bin$|activeX|externalLink|embeddings", n, re.I)
    ]
    xml = b"".join(partes.values())
    for prohibido in (b"<f>", b"<f ", b"<hyperlink", b'TargetMode="External"', b"macroEnabled",
                      b'state="hidden"', b'state="veryHidden"'):
        assert prohibido not in xml, prohibido


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_mismo_libro_da_los_mismos_bytes_aunque_se_escriba_en_otro_momento(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000_000_000.0)
    antes = _xlsx()
    monkeypatch.setattr(time, "time", lambda: 1_900_000_000.0)
    despues = _xlsx()

    assert antes == despues
    with zipfile.ZipFile(io.BytesIO(antes)) as paquete:
        assert {i.date_time for i in paquete.infolist()} == {(1980, 1, 1, 0, 0, 0)}


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_xlsx_escribe_como_texto_las_fechas_que_excel_no_representa():
    hojas = (HojaDeSalida("Fechas", ("Fecha",), (
        (date(1850, 1, 1),), (datetime(1899, 7, 9, 10, 30),), (date(1900, 2, 28),),
        (date(1900, 3, 1),), (date(1975, 5, 3),),
    )),)

    hoja = _libro(_xlsx(hojas))["Fechas"]

    assert [c.value for (c,) in hoja.iter_rows(min_row=2)] == [
        "1850-01-01", "1899-07-09T10:30:00", "1900-02-28",
        datetime(1900, 3, 1), datetime(1975, 5, 3),
    ]


@pytest.mark.rf("RF-49")
@pytest.mark.historia(277)
def test_el_xlsx_recorta_un_texto_largo_con_una_marca_visible():
    largo = "a" * 40_000
    hojas = (HojaDeSalida("Largo", ("Texto",), ((largo,), ("=" + "b" * 32_766,), ("c" * 32_767,))),)

    hoja = _libro(_xlsx(hojas))["Largo"]
    [recortado, con_formula, justo] = [c.value for (c,) in hoja.iter_rows(min_row=2)]

    assert len(recortado) == len(con_formula) == 32_767
    assert recortado.endswith("a… [recortado]")
    assert con_formula.startswith("'=b") and con_formula.endswith("… [recortado]")
    assert justo == "c" * 32_767
