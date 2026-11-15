"""`planillas`: la admisión distingue el texto delimitado del texto plano (DEC-43) y un `.xlsx`
se vuelve tablas markdown por hoja con su digest (DEC-42, DEC-44, DEC-74).

En proceso: `clasificar`, `extraer` y `digest` son funciones puras sobre un flujo o un texto.
"""

from __future__ import annotations

import codecs
import io
import re
import time
import tracemalloc
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

import openpyxl
import pytest
from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904
from openpyxl.worksheet.worksheet import Worksheet

from historia_viva import planillas
from historia_viva.dominio.cuerpo import CuerpoDeFicha, MarcaDeHoja
from historia_viva.dominio.errores import ErrorHistoriaViva
from historia_viva.planillas import (
    TOPES,
    Clasificacion,
    PlanillaExcesiva,
    PlanillaIlegible,
    Topes,
    clasificar,
    digest,
    extraer,
    extraer_csv,
    leer_registros,
)

pytestmark = pytest.mark.rf("RF-38")

CSV = "text/csv"
FILAS = [
    ["Año", "Carrera", "Nota"],
    ["2024", "Ingeniería", "Peña, José"],
    ["2025", "Diseño", "ñandú"],
]



def _activa(libro: Workbook) -> Worksheet:
    hoja = libro.active
    assert isinstance(hoja, Worksheet)
    return hoja


def _texto(separador, filas=FILAS):
    """Las filas como las escribe Excel: cada campo con el separador adentro va entre comillas."""
    def campo(valor):
        return f'"{valor}"' if separador in valor or "\n" in valor else valor

    return "".join(separador.join(campo(v) for v in fila) + "\r\n" for fila in filas)


_JORNADA = FILAS + [["2026", "“Jornada” – €", "x"]]
_SALTOS = FILAS + [["2026", "una línea\notra línea", "x"]] * 5
_UNA_COLUMNA = "Nombre\r\nAna\r\nPeña, José\r\nLuis\r\n".encode()

CASOS_DE_028 = [
    pytest.param(_texto(";").encode("utf-8"), "utf-8", ";", FILAS, id="utf-8-punto-y-coma"),
    pytest.param(
        codecs.BOM_UTF8 + _texto(";").encode("utf-8"), "utf-8-sig", ";", FILAS, id="utf-8-con-bom"
    ),
    pytest.param(
        codecs.BOM_UTF16_LE + _texto(",").encode("utf-16-le"), "utf-16", ",", FILAS, id="utf-16-le"
    ),
    pytest.param(
        codecs.BOM_UTF16_BE + _texto("\t").encode("utf-16-be"),
        "utf-16",
        "\t",
        FILAS,
        id="utf-16-be",
    ),
    # Las comillas tipográficas, la raya y el euro están en cp1252 y no en Latin-1.
    pytest.param(_texto(";", _JORNADA).encode("cp1252"), "cp1252", ";", _JORNADA, id="cp1252"),
    # 0x8D no existe en cp1252: solo Latin-1 lo lee. En la tabla es un control: queda vacío.
    pytest.param(
        _texto("|", FILAS + [["2026", "\x8d", "x"]]).encode("latin-1"),
        "latin-1",
        "|",
        FILAS + [["2026", "", "x"]],
        id="latin-1",
    ),
    pytest.param(_texto("\t").encode("utf-8"), "utf-8", "\t", FILAS, id="tab"),
    pytest.param(_texto(",").encode("utf-8"), "utf-8", ",", FILAS, id="coma"),
    # El salto de línea dentro de las comillas no parte el registro; en la celda es un espacio.
    pytest.param(
        _texto(",", _SALTOS).encode("utf-8"),
        "utf-8",
        ",",
        FILAS + [["2026", "una línea otra línea", "x"]] * 5,
        id="comillas-con-saltos",
    ),
    # Sin separador consistente es una sola columna, aunque algún valor traiga una coma.
    pytest.param(
        _UNA_COLUMNA,
        "utf-8",
        None,
        [["Nombre"], ["Ana"], ["Peña, José"], ["Luis"]],
        id="una-columna",
    ),
]
"""Los casos de codificación y separador de 028 «planillas.clasificar»: contenido, codificación,
separador y las filas que tienen que llegar a la tabla."""


@pytest.mark.historia(97)
@pytest.mark.historia(289)
@pytest.mark.parametrize(("contenido", "codificacion", "separador", "filas"), CASOS_DE_028)
def test_un_csv_se_clasifica_con_su_codificacion_y_su_separador(
    contenido, codificacion, separador, filas
):
    assert clasificar(io.BytesIO(contenido), "datos.csv") == Clasificacion(
        tipo_mime=CSV, codificacion=codificacion, separador=separador
    )


@pytest.mark.historia(97)
@pytest.mark.historia(289)
@pytest.mark.parametrize(("contenido", "codificacion", "separador", "filas"), CASOS_DE_028)
def test_un_csv_llega_a_una_tabla_de_una_hoja_con_las_mismas_filas_y_columnas(
    contenido, codificacion, separador, filas
):
    extraccion = extraer_csv(io.BytesIO(contenido), "inscriptos 2024.csv")

    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert hoja.marca.nombre == "inscriptos 2024"
    assert [hoja.columnas, *hoja.filas] == filas
    assert (hoja.marca.desde, hoja.marca.hasta, hoja.marca.total) == (1, len(filas), len(filas))
    assert (extraccion.avisos, extraccion.truncada) == ((), False)


@pytest.mark.historia(97)
@pytest.mark.parametrize(
    ("contenido", "desde", "hasta", "anotacion"),
    [
        pytest.param(
            _texto(";").encode(),
            1,
            3,
            "Codificación detectada: UTF-8 · Separador: punto y coma",
            id="utf-8",
        ),
        pytest.param(
            codecs.BOM_UTF8 + _texto(",").encode(),
            1,
            3,
            "Codificación detectada: UTF-8 con BOM · Separador: coma",
            id="utf-8-con-bom",
        ),
        pytest.param(
            codecs.BOM_UTF16_LE + _texto(",").encode("utf-16-le"),
            1,
            3,
            "Codificación detectada: UTF-16 · Separador: coma",
            id="utf-16",
        ),
        pytest.param(
            _texto("\t", _JORNADA).encode("cp1252"),
            1,
            4,
            "Codificación detectada: Windows-1252 · Separador: tabulación",
            id="cp1252",
        ),
        pytest.param(
            _texto("|", FILAS + [["2026", "\x8d", "x"]]).encode("latin-1"),
            1,
            4,
            "Codificación detectada: Latin-1 · Separador: barra vertical",
            id="latin-1",
        ),
        pytest.param(
            _UNA_COLUMNA,
            1,
            4,
            "Codificación detectada: UTF-8 · Separador: ninguno, una sola columna",
            id="una-columna",
        ),
        # La fila de encabezado es la primera con datos: las vacías de arriba no se emiten.
        pytest.param(
            b"\r\n;\r\nA;B\r\n1;2\r\n",
            3,
            4,
            "Codificación detectada: UTF-8 · Separador: punto y coma",
            id="encabezado-en-la-fila-3",
        ),
    ],
)
def test_la_marca_de_hoja_anota_la_codificacion_y_el_separador_detectados(
    contenido, desde, hasta, anotacion
):
    cuerpo = extraer_csv(io.BytesIO(contenido), "datos.csv").cuerpo

    marca = MarcaDeHoja("datos", desde, hasta, hasta, anotacion).a_texto()
    assert cuerpo.split("\n")[0] == marca


@pytest.mark.historia(289)
def test_el_texto_de_un_csv_queda_en_nfc_y_sin_caracteres_de_control():
    """R-36: la «ñ» y la «é» descompuestas salen compuestas; los controles, como espacio."""
    contenido = "Apellido;Nombre\r\nPen\u0303a;Jose\u0301\x07\x1b\r\n".encode()

    cuerpo = extraer_csv(io.BytesIO(contenido), "datos.csv").cuerpo

    (hoja,) = CuerpoDeFicha.parsear_cuerpo(cuerpo)
    assert hoja.filas == [["Peña", "José"]]
    assert unicodedata.is_normalized("NFC", cuerpo)
    assert not any(unicodedata.category(c) == "Cc" for c in cuerpo.replace("\n", ""))


@pytest.mark.historia(97)
def test_una_muestra_que_no_es_el_archivo_entero_no_rechaza_la_letra_partida_del_final():
    # 700 KiB de UTF-8 que terminan en la mitad de una «ñ»: el archivo entero se rechaza, el
    # primer bloque de un archivo más largo no.
    contenido = "Año;Carrera\r\n".encode() * (700 * 1024 // 14) + b"\xc3"

    entero = clasificar(io.BytesIO(contenido), "datos.csv")
    comienzo = clasificar(io.BytesIO(contenido), "datos.csv", completo=False)

    assert entero.motivo_rechazo == "codificacion-desconocida"
    assert (comienzo.codificacion, comienzo.separador) == ("utf-8", ";")


@pytest.mark.historia(97)
@pytest.mark.parametrize(
    ("contenido", "motivo"),
    [
        # Pasada la muestra que decidió UTF-8, el archivo termina en la mitad de una letra.
        pytest.param(
            "a;ñ\r\n".encode() * 120_000 + b"\xc3",
            "codificacion-desconocida",
            id="letra-partida-al-final",
        ),
        pytest.param(
            b"a;b\r\n" * 120_000 + b"\xff;x\r\n",
            "codificacion-desconocida",
            id="byte-invalido-pasada-la-muestra",
        ),
    ],
)
def test_un_csv_que_no_se_puede_leer_da_planilla_ilegible_con_el_mensaje_para_la_persona(
    contenido, motivo
):
    with pytest.raises(PlanillaIlegible) as error:
        extraer_csv(io.BytesIO(contenido), "datos.csv")

    assert error.value.motivo == motivo
    # Un `ErrorHistoriaViva`: el trabajador muestra su texto y no «Error interno».
    assert isinstance(error.value, ErrorHistoriaViva)
    assert str(error.value).startswith("No se pudo procesar este archivo: ")


@pytest.mark.rnf("RNF-20")
def test_un_csv_que_pasa_los_topes_de_filas_y_columnas_se_recorta_con_aviso_y_el_total_leido():
    encabezado = [f"c{n}" for n in range(1, 46)]
    filas = [encabezado] + [[str(fila)] * 45 for fila in range(2, 1502)]

    extraccion = extraer_csv(io.BytesIO(_texto(";", filas).encode()), "datos.csv")

    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert hoja.marca == MarcaDeHoja(
        "datos",
        1,
        1000,
        1501,
        "Codificación detectada: UTF-8 · Separador: punto y coma · Se muestran 40 de 45"
        " columnas. Se muestran 1.000 de 1.501 filas. El resto no se leyó.",
    )
    assert len(hoja.columnas) == 40
    assert hoja.filas[-1][0] == "1000"
    assert extraccion.avisos == (
        "Hoja «datos»: se muestran 40 de 45 columnas.",
        "Hoja «datos»: se muestran 1.000 de 1.501 filas. El resto no se leyó.",
    )
    assert extraccion.truncada


@pytest.mark.historia(97)
def test_el_separador_gana_por_consistencia_en_los_primeros_20_registros():
    # Cada fila tiene una coma suelta en el texto; el `;` es el que da siempre dos columnas.
    filas = [["Fecha", "Comentario"]] + [["2024", f"Hola, fila {i}, chau"] for i in range(19)]
    # Pasado el registro 20, la coma daría tres columnas parejas: no se mira.
    resto = "".join(f"a,b,c{i}\r\n" for i in range(200))

    clasificacion = clasificar(io.BytesIO((_texto(";", filas) + resto).encode()), "datos.csv")

    assert clasificacion.separador == ";"


@pytest.mark.historia(97)
def test_una_muestra_que_parte_una_letra_no_cambia_la_codificacion():
    # La muestra de 512 KiB termina en el medio de la «ñ»: el archivo sigue siendo UTF-8.
    relleno = "a;b\r\n" * (512 * 1024 // 5)
    contenido = (relleno + "x" * (512 * 1024 - len(relleno) - 1) + "ñ;b\r\n" * 10).encode()

    clasificacion = clasificar(io.BytesIO(contenido), "datos.csv")

    assert (clasificacion.codificacion, clasificacion.separador) == ("utf-8", ";")


@pytest.mark.historia(97)
def test_un_csv_de_una_sola_linea_mas_larga_que_la_muestra_conserva_su_separador():
    # La muestra de 512 KiB no llega al primer salto de línea: el registro partido es lo único
    # que hay para mirar.
    contenido = (";".join(["Año"] * 200_000) + "\r\n").encode()

    clasificacion = clasificar(io.BytesIO(contenido), "datos.csv")

    assert (clasificacion.codificacion, clasificacion.separador) == ("utf-8", ";")


@pytest.mark.historia(289)
@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param(b"", id="vacio"),
        pytest.param(b" \r\n\t", id="blancos"),
        pytest.param(b"\x01\x02", id="solo-caracteres-de-control"),
        # Invisibles de formato (Cf): el cuerpo se vería vacío.
        pytest.param("\u200b⁠­\r\n".encode(), id="solo-caracteres-de-formato"),
    ],
)
def test_un_txt_sin_texto_esta_vacio(contenido):
    """Lo que no deja texto después de quitar los caracteres de control (R-36) no es una Pieza
    `texto`: la admisión lo rechaza con 415 en vez de crearla con el cuerpo vacío."""
    assert clasificar(io.BytesIO(contenido), "nota.txt") == Clasificacion(vacio=True)


@pytest.mark.historia(97)
def test_un_txt_con_comas_sueltas_es_texto_y_no_csv():
    # Cada línea trae una coma: por el contenido serían dos columnas parejas.
    carta = b"Querida Ana, te escribo desde Quilmes.\nSaludos, Juan.\n"

    assert clasificar(io.BytesIO(carta), "carta.txt") == Clasificacion(
        tipo_mime="text/plain", codificacion="utf-8"
    )


@pytest.mark.historia(97)
@pytest.mark.parametrize(
    ("contenido", "motivo"),
    [
        pytest.param(b"", "sin-hojas", id="vacio"),
        pytest.param(codecs.BOM_UTF8 + b"\r\n \r\n", "sin-hojas", id="solo-bom-y-blancos"),
        # La transcripción quita los caracteres de control (R-36): quedaría sin cuerpo.
        pytest.param(b"\x01\x02\r\n\x7f", "sin-hojas", id="solo-caracteres-de-control"),
        pytest.param("﻿\u200b\r\n".encode(), "sin-hojas", id="solo-caracteres-de-formato"),
        pytest.param(b"a;b\r\n\x00\x01\x02", "csv-binario", id="con-nul"),
        pytest.param(codecs.BOM_UTF16_LE + b"a\x00;", "codificacion-desconocida", id="bom-roto"),
    ],
)
def test_un_csv_que_no_se_puede_leer_se_rechaza_con_motivo(contenido, motivo):
    assert clasificar(io.BytesIO(contenido), "datos.csv") == Clasificacion(motivo_rechazo=motivo)


@pytest.mark.historia(97)
@pytest.mark.parametrize("nombre", ["foto.jpg", "pagina.html", "dibujo.svg", "sin-extension"])
def test_solo_el_txt_es_texto_plano(nombre):
    """Un HTML o un SVG renombrados no entran como texto (decisión del usuario, 028: el `.txt`)."""
    contenido = io.BytesIO(b"<html><body>hola</body></html>")

    assert clasificar(contenido, nombre) == Clasificacion()


@pytest.mark.historia(283)
@pytest.mark.parametrize(
    ("contenido", "mensaje"),
    [
        # Un byte más que el tope de la planilla (DEC-42): no se lee entero.
        pytest.param(
            b"a;b\n" + b"x;y\n" * 2_499_999 + b"z", "10 MB", id="archivo-de-mas-de-10-mb"
        ),
        pytest.param(b"a;b\nx;" + b"y" * 32_001 + b"\n", "32 KB", id="campo-de-mas-de-32-kb"),
    ],
)
def test_leer_registros_rechaza_lo_que_pasa_los_topes_de_dec_42(contenido, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        leer_registros(io.BytesIO(contenido), "carreras.csv")


@pytest.mark.historia(283)
def test_leer_registros_acepta_exactamente_los_topes():
    campo = "y" * 32_000
    relleno = b"x;y\n" * ((10_000_000 - 4 - 2 - 32_000 - 1) // 4)
    contenido = b"a;b\nx;" + campo.encode() + b"\n" + relleno
    contenido += b"\n" * (10_000_000 - len(contenido))

    registros = leer_registros(io.BytesIO(contenido), "carreras.csv")

    assert registros[1] == ["x", campo]


def libro(
    hojas: dict[str, list[list[object]]],
    *,
    preparar: Callable[[Workbook], None] = lambda libro: None,
    parches: dict[str, str] | None = None,
    agregar: dict[str, str] | None = None,
) -> bytes:
    """Un `.xlsx` escrito por openpyxl con una hoja por entrada; `parches` reemplaza texto en el
    XML de sus partes y `agregar` suma partes: lo que openpyxl no escribe (el valor guardado de
    una fórmula, los textos compartidos)."""
    escrito = Workbook()
    escrito.remove(_activa(escrito))
    for nombre, filas in hojas.items():
        hoja = escrito.create_sheet(nombre)
        for fila in filas:
            hoja.append(fila)
    preparar(escrito)
    salida = io.BytesIO()
    escrito.save(salida)
    if not parches and not agregar:
        return salida.getvalue()
    parchado = io.BytesIO()
    with (
        zipfile.ZipFile(salida) as original,
        zipfile.ZipFile(parchado, "w", zipfile.ZIP_DEFLATED) as nuevo,
    ):
        for miembro in original.namelist():
            contenido = original.read(miembro)
            texto = contenido.decode()
            for viejo, otro in (parches or {}).items():
                texto = texto.replace(viejo, otro)
            nuevo.writestr(miembro, texto.encode())
        for miembro, texto in (agregar or {}).items():
            nuevo.writestr(miembro, texto)
    return parchado.getvalue()


TRES_HOJAS: dict[str, list[list[object]]] = {
    "Ingresantes": [
        ["Año", "Carrera", "Cantidad"],
        [2024, "Ingeniería", 120],
        [2025, "Diseño", 80],
    ],
    "Egresados": [["Año", "Cantidad"], [2024, 40]],
    # El nombre lo eligió quien armó la planilla: puede traer comillas y paréntesis.
    "Notas «finales» (2)": [["Alumno", "Nota"], ["Peña, José", 9.5], ["Ana", 7]],
}


@pytest.mark.historia(91)
@pytest.mark.historia(92)
def test_un_libro_de_tres_hojas_da_tres_marcas_y_sus_tablas_van_y_vuelven():
    cuerpo = extraer(io.BytesIO(libro(TRES_HOJAS))).cuerpo

    hojas = CuerpoDeFicha.parsear_cuerpo(cuerpo)

    assert [hoja.marca for hoja in hojas] == [
        MarcaDeHoja("Ingresantes", 1, 3, 3),
        MarcaDeHoja("Egresados", 1, 2, 2),
        MarcaDeHoja("Notas «finales» (2)", 1, 3, 3),
    ]
    assert [[hoja.columnas, *hoja.filas] for hoja in hojas] == [
        [
            ["Año", "Carrera", "Cantidad"],
            ["2024", "Ingeniería", "120"],
            ["2025", "Diseño", "80"],
        ],
        [["Año", "Cantidad"], ["2024", "40"]],
        [["Alumno", "Nota"], ["Peña, José", "9.5"], ["Ana", "7"]],
    ]


def _primera_celda(
    valor: object,
    formato: str | None = None,
    preparar: Callable[[Workbook], None] = lambda libro: None,
) -> str:
    """La celda de datos de una hoja con un encabezado y un valor, ida y vuelta por el cuerpo."""

    def con_formato(escrito: Workbook) -> None:
        preparar(escrito)
        if formato is not None:
            _activa(escrito)["A2"].number_format = formato

    contenido = libro({"Datos": [["Valor"], [valor]]}, preparar=con_formato)
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraer(io.BytesIO(contenido)).cuerpo)
    return hoja.filas[0][0]


@pytest.mark.historia(92)
@pytest.mark.parametrize(
    ("valor", "formato", "esperado"),
    [
        pytest.param(datetime(1975, 3, 12), "dd/mm/yyyy", "1975-03-12", id="fecha"),
        pytest.param(
            datetime(1975, 3, 12, 10, 30),
            "dd/mm/yyyy hh:mm",
            "1975-03-12T10:30:00",
            id="fecha-y-hora",
        ),
        pytest.param(0.25, "0%", "25%", id="porcentaje"),
        pytest.param(0.07, "0.00%", "7%", id="porcentaje-con-decimales"),
        pytest.param(1 / 3, None, "0.333333333333333", id="15-cifras-significativas"),
        pytest.param(True, None, "VERDADERO", id="verdadero"),
        pytest.param(False, None, "FALSO", id="falso"),
        pytest.param(timedelta(hours=26, minutes=5), "[h]:mm", "26:05", id="duracion"),
        pytest.param(
            timedelta(minutes=5, seconds=7), "[h]:mm:ss", "0:05:07", id="duracion-con-segundos"
        ),
        pytest.param("#DIV/0!", None, "#DIV/0!", id="error-de-celda"),
        pytest.param("a|b", None, "a|b", id="barra-vertical"),
        pytest.param("C:\\actas\\", None, "C:\\actas\\", id="barra-invertida"),
        pytest.param("una línea\r\notra\tmás", None, "una línea  otra más", id="saltos-de-linea"),
        pytest.param("\u202eabc\u2066def\u2069", None, "abcdef", id="controles-bidireccionales"),
    ],
)
def test_cada_celda_sigue_la_tabla_de_conversion(valor, formato, esperado):
    assert _primera_celda(valor, formato) == esperado


@pytest.mark.historia(92)
def test_una_fecha_de_un_libro_con_el_calendario_1904_es_la_misma_fecha():
    def calendario_1904(escrito: Workbook) -> None:
        escrito.epoch = CALENDAR_MAC_1904

    # Leída con el calendario 1900, la serie de esta fecha daría 1995-12-31.
    assert _primera_celda(datetime(2000, 1, 1), "dd/mm/yyyy", preparar=calendario_1904) == (
        "2000-01-01"
    )


@pytest.mark.historia(95)
def test_una_formula_muestra_su_valor_guardado_y_sin_valor_queda_vacia():
    # Evaluada, `=1+1` daría 2; Excel guardó 5. `=2+2` no tiene valor guardado.
    contenido = libro(
        {"Datos": [["Guardado", "Sin guardar"], ["=1+1", "=2+2"]]},
        parches={"<f>1+1</f><v></v>": "<f>1+1</f><v>5</v>"},
    )

    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraer(io.BytesIO(contenido)).cuerpo)

    assert hoja.filas == [["5", ""]]


ENTIDADES_ANIDADAS = "".join(
    f'<!ENTITY e{n} "{f"&e{n - 1};" * 10 if n else "a"}">' for n in range(10)
)
"""Mil millones de «a» si alguien expandiera `&e9;`."""


SST = (
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<si><t>{}</t></si></sst>"
)


def _con_textos_compartidos(sst: str) -> bytes:
    """Un libro de una hoja cuya celda A1 es el texto compartido 0 de `sst` (openpyxl escribe
    los textos en línea; Excel, compartidos)."""
    tipo = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
    return libro(
        {"Datos": [["x"]]},
        parches={
            '<c r="A1" t="inlineStr"><is><t>x</t></is></c>': '<c r="A1" t="s"><v>0</v></c>',
            "</Types>": (
                f'<Override PartName="/xl/sharedStrings.xml" ContentType="{tipo}"/></Types>'
            ),
        },
        agregar={"xl/sharedStrings.xml": sst},
    )


@pytest.mark.rnf("RNF-20")
def test_un_texto_compartido_llega_a_la_tabla():
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(
        extraer(io.BytesIO(_con_textos_compartidos(SST.format("Año")))).cuerpo
    )

    assert hoja.columnas == ["Año"]


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(219)
@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param(
            lambda: libro(
                {"Datos": [["Valor"], ["x"]]},
                parches={"<worksheet ": f"<!DOCTYPE worksheet [{ENTIDADES_ANIDADAS}]><worksheet "},
            ),
            id="en-la-hoja",
        ),
        # Lo que no pasa por el filtro de las hojas lo lee openpyxl, con defusedxml.
        pytest.param(
            lambda: _con_textos_compartidos(
                f"<!DOCTYPE sst [{ENTIDADES_ANIDADAS}]>" + SST.format("&e9;")
            ),
            id="en-los-textos-compartidos",
        ),
        # lxml no resuelve la entidad externa y corta con `XMLSyntaxError`.
        pytest.param(
            lambda: libro(
                {"Datos": [["Valor"]]},
                parches={
                    "<workbook ": '<!DOCTYPE workbook [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                    "<workbook ",
                    'name="Datos"': 'name="&x;"',
                },
            ),
            id="externa-en-el-libro",
        ),
    ],
)
def test_un_xml_con_entidades_da_planilla_ilegible(contenido):
    assert openpyxl.DEFUSEDXML is True

    with pytest.raises(PlanillaIlegible) as rechazo:
        extraer(io.BytesIO(contenido()))

    assert rechazo.value.motivo == "zip-corrupto"
    assert str(rechazo.value).startswith("No se pudo procesar este archivo: ")
    assert "aaaa" not in str(rechazo.value)
    assert rechazo.value.__cause__ is not None


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(219)
@pytest.mark.parametrize(
    "contenido",
    [
        # openpyxl lee el libro, los estilos y los `.rels` con lxml, sin defusedxml.
        pytest.param(
            lambda: libro(
                {"Datos": [["x"]]},
                parches={
                    "<workbook ": '<!DOCTYPE workbook [<!ENTITY x "Datos">]><workbook ',
                    'name="Datos"': 'name="&x;"',
                },
            ),
            id="entidad-interna-en-el-libro",
        ),
        pytest.param(
            lambda: libro(
                {"Datos": [["x"]]},
                parches={"<styleSheet ": f"<!DOCTYPE s [{ENTIDADES_ANIDADAS}]><styleSheet "},
            ),
            id="dtd-sin-usar-en-los-estilos",
        ),
        pytest.param(
            lambda: _con_textos_compartidos(f"<!DOCTYPE sst [{ENTIDADES_ANIDADAS}]>" + SST),
            id="dtd-en-los-textos-compartidos",
        ),
    ],
)
def test_una_dtd_en_cualquier_parte_se_rechaza_antes_de_openpyxl(contenido, monkeypatch):
    """OPC prohíbe las DTD: el filtro las rechaza en toda parte XML, sin dársela a openpyxl."""
    monkeypatch.setattr(planillas, "load_workbook", _no_debe_llamarse)

    with pytest.raises(PlanillaIlegible) as rechazo:
        extraer(io.BytesIO(contenido()))

    assert isinstance(rechazo.value.__cause__, ValueError)
    assert "DTD" in str(rechazo.value.__cause__)


def _no_debe_llamarse(*_: object, **__: object) -> None:
    raise AssertionError("openpyxl no tiene que leer un libro con DTD")


@pytest.mark.historia(96)
def test_las_hojas_ocultas_se_omiten_con_un_aviso_visible():
    def ocultar(escrito: Workbook) -> None:
        escrito["Oculta"].sheet_state = "hidden"
        escrito["Muy oculta"].sheet_state = "veryHidden"

    contenido = libro(
        {"Oculta": [["secreto"]], "Visible": [["Año"], [2024]], "Muy oculta": [["secretísimo"]]},
        preparar=ocultar,
    )

    extraccion = extraer(io.BytesIO(contenido))

    assert extraccion.avisos == ("Se omitieron 2 hojas ocultas.",)
    assert extraccion.cuerpo.startswith("Se omitieron 2 hojas ocultas.\n")
    assert [hoja.marca.nombre for hoja in CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)] == [
        "Visible"
    ]
    assert "secret" not in extraccion.cuerpo


@pytest.mark.historia(96)
def test_las_filas_y_columnas_ocultas_se_omiten_con_aviso_y_conservan_la_numeracion():
    """El spike de 088: `ReadOnlyWorksheet` no las expone; se leen del `hidden="1"` del XML."""

    def ocultar(escrito: Workbook) -> None:
        _activa(escrito).row_dimensions[3].hidden = True
        _activa(escrito).row_dimensions[5].hidden = True
        _activa(escrito).column_dimensions["B"].hidden = True
        # Ocultar lo que no tiene datos (las columnas a la derecha) no se avisa.
        _activa(escrito).column_dimensions.group("E", "Z", hidden=True)

    filas = [["A", "B", "C"], ["a2", "b2", "c2"], ["a3", "b3", "c3"], ["a4", "b4", "c4"]]
    contenido = libro({"Datos": [*filas, ["a5", "b5", "c5"]]}, preparar=ocultar)

    extraccion = extraer(io.BytesIO(contenido))

    aviso = "Se omitieron 2 filas ocultas y 1 columna oculta."
    assert extraccion.avisos == (f"Hoja «Datos»: s{aviso[1:]}",)
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    # La fila de la tabla sigue siendo la de la planilla (DEC-112): la oculta queda vacía.
    assert hoja.marca == MarcaDeHoja("Datos", 1, 4, 4, aviso)
    assert [hoja.columnas, *hoja.filas] == [
        ["A", "", "C"],
        ["a2", "", "c2"],
        ["", "", ""],
        ["a4", "", "c4"],
    ]
    assert "b2" not in extraccion.cuerpo and "a3" not in extraccion.cuerpo


@pytest.mark.historia(91)
def test_las_filas_vacias_de_los_bordes_no_se_emiten_y_las_del_medio_conservan_la_numeracion():
    contenido = libro({"Datos": [[], [None, "Año", None], [None, 2024], [], [None, 2025], []]})

    cuerpo = extraer(io.BytesIO(contenido)).cuerpo
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(cuerpo)

    # La fila de encabezado es `desde` (DEC-112): la 2 de la hoja.
    assert hoja.marca == MarcaDeHoja("Datos", 2, 5, 5)
    assert [hoja.columnas, *hoja.filas] == [["", "Año"], ["", "2024"], ["", ""], ["", "2025"]]


@pytest.mark.historia(91)
def test_una_hoja_vacia_queda_con_su_marca_y_sin_tabla():
    cuerpo = extraer(io.BytesIO(libro({"Vacía": [], "Datos": [["Año"]]}))).cuerpo

    assert [(hoja.marca, hoja.columnas) for hoja in CuerpoDeFicha.parsear_cuerpo(cuerpo)] == [
        (MarcaDeHoja("Vacía", 0, 0, 0), []),
        (MarcaDeHoja("Datos", 1, 1, 1), ["Año"]),
    ]


@pytest.mark.historia(92)
def test_el_digest_trae_por_hoja_nombre_dimensiones_encabezado_y_cinco_filas():
    largo = "x" * 100
    cuerpo = extraer(
        io.BytesIO(
            libro(
                {
                    "Alumnos": [["Nombre", "Nota"], *([f"Alumno {n}", n] for n in range(1, 8))],
                    "Notas": [["Detalle"], [largo]],
                }
            )
        )
    ).cuerpo

    assert digest(cuerpo) == (
        "Hoja «Alumnos»: 8 filas y 2 columnas.\n"
        "| Nombre | Nota |\n"
        "| Alumno 1 | 1 |\n"
        "| Alumno 2 | 2 |\n"
        "| Alumno 3 | 3 |\n"
        "| Alumno 4 | 4 |\n"
        "| Alumno 5 | 5 |\n"
        "Hoja «Notas»: 2 filas y 1 columnas.\n"
        "| Detalle |\n"
        f"| {'x' * 80} |"
    )


@pytest.mark.historia(92)
def test_el_digest_no_pasa_de_6000_caracteres():
    filas = [[f"{n}-{'y' * 70}" for n in range(30)] for _ in range(6)]
    cuerpo = extraer(io.BytesIO(libro({f"Hoja {n}": filas for n in range(3)}))).cuerpo

    assert len(cuerpo) > 6000
    assert len(digest(cuerpo)) == 6000


@pytest.mark.rnf("RNF-20")
def test_lo_que_pasa_los_topes_de_hojas_filas_y_columnas_se_recorta_con_aviso():
    hojas: dict[str, list[list[object]]] = {
        "Ancha": [[f"c{n}" for n in range(1, 43)]],
        "Larga": [[n] for n in range(1, 1003)],
    }
    hojas |= {f"Hoja {n}": [["x"]] for n in range(19)}

    extraccion = extraer(io.BytesIO(libro(hojas)))

    assert extraccion.truncada
    assert extraccion.avisos == (
        "Se muestran 20 de 21 hojas.",
        "Hoja «Ancha»: se muestran 40 de 42 columnas.",
        "Hoja «Larga»: se muestran 1.000 de 1.002 filas. El resto no se leyó.",
    )
    tablas = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert len(tablas) == 20
    assert tablas[0].columnas == [f"c{n}" for n in range(1, 41)]
    assert tablas[1].marca == MarcaDeHoja(
        "Larga", 1, 1000, 1002, "Se muestran 1.000 de 1.002 filas. El resto no se leyó."
    )
    assert tablas[1].filas[-1] == ["1000"]


@pytest.mark.rnf("RNF-20")
def test_lo_que_sigue_a_un_hueco_pasado_el_tope_se_avisa_por_la_dimension():
    def total_y_dato_lejano(escrito: Workbook) -> None:
        _activa(escrito)["A1002"] = "TOTAL"  # la fila 1001 queda en blanco
        _activa(escrito)["AQ5"] = "dato"  # la columna 41 queda en blanco

    contenido = libro({"Datos": [[n] for n in range(1, 1001)]}, preparar=total_y_dato_lejano)
    with zipfile.ZipFile(io.BytesIO(contenido)) as comprimido:
        assert b'<dimension ref="A1:AQ1002"/>' in comprimido.read("xl/worksheets/sheet1.xml")

    extraccion = extraer(io.BytesIO(contenido))

    assert extraccion.truncada
    assert extraccion.avisos == (
        "Hoja «Datos»: se muestran 1 de 43 columnas.",
        "Hoja «Datos»: se muestran 1.000 de 1.002 filas. El resto no se leyó.",
    )


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    ("celda", "total"),
    [pytest.param("A1500", 1500, id="fila"), pytest.param("AZ1", 0, id="columna")],
)
def test_una_hoja_sin_datos_dentro_de_los_topes_avisa_lo_que_no_leyo(celda, total):
    def lejos(escrito: Workbook) -> None:
        _activa(escrito)[celda] = "dato"

    extraccion = extraer(io.BytesIO(libro({"Datos": []}, preparar=lejos)))

    assert extraccion.truncada
    assert extraccion.avisos == (
        ("Hoja «Datos»: las primeras 1.000 filas y 40 columnas están vacías."
        " El resto no se leyó."),
    )
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert hoja.marca == MarcaDeHoja(
        "Datos",
        0,
        0,
        total,
        "Las primeras 1.000 filas y 40 columnas están vacías. El resto no se leyó.",
    )


@pytest.mark.rnf("RNF-20")
def test_el_cuerpo_se_corta_en_la_ultima_fila_entera_antes_de_300_000_caracteres():
    # 400 filas de 1000 caracteres: el cuerpo pasa su tope antes que las filas el suyo.
    contenido = libro({"Actas": [["Texto"], *([f"{n:04} {'z' * 995}"] for n in range(1, 400))]})

    extraccion = extraer(io.BytesIO(contenido))

    assert len(extraccion.cuerpo) < 300_200
    assert extraccion.avisos[-1] == (
        "El cuerpo llegó al tope de 300.000 caracteres. El resto no se leyó."
    )
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert hoja.marca.hasta == len(hoja.filas) + 1
    assert hoja.filas[-1][0] == f"{hoja.marca.hasta - 1:04} {'z' * 995}"


@pytest.mark.historia(220)
def test_un_nombre_de_hoja_con_saltos_de_linea_no_parte_la_marca():
    # Excel no deja escribirlo; el XML del libro, sí.
    contenido = libro(
        {"Datos": [["Año"]]}, parches={'name="Datos"': 'name="Datos&#10;## Contexto"'}
    )

    cuerpo = extraer(io.BytesIO(contenido)).cuerpo

    assert CuerpoDeFicha.partir(cuerpo).contexto == ""
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(cuerpo)
    assert hoja.marca.nombre == "Datos ## Contexto"


@pytest.mark.historia(220)
def test_la_barra_vertical_y_la_invertida_van_escapadas_en_la_tabla():
    cuerpo = extraer(io.BytesIO(libro({"Datos": [["Valor"], ["a|b\\"]]}))).cuerpo

    assert r"| a\|b\\ |" in cuerpo.split("\n")


@pytest.mark.historia(220)
@pytest.mark.parametrize(
    ("celda", "esperada"),
    [
        ("\n## Contexto\nhola", "## Contexto hola"),
        ("--- Hoja «Y» (filas 1–2 de 3) ---", "--- Hoja «Y» (filas 1–2 de 3) ---"),
        ("x\n--- Hoja «Y» (filas 1–2 de 3) ---", "x --- Hoja «Y» (filas 1–2 de 3) ---"),
    ],
)
def test_una_celda_que_imita_la_estructura_no_crea_seccion_ni_hoja(celda, esperada):
    cuerpo = extraer(io.BytesIO(libro({"Datos": [["Valor"], [celda]]}))).cuerpo

    assert CuerpoDeFicha.partir(cuerpo).contexto == ""
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(cuerpo)
    assert hoja.filas == [[esperada]]


# --- 090: planilla hostil ---------------------------------------------------------------------


@pytest.mark.rnf("RNF-20")
def test_los_topes_son_los_de_dec_42():
    assert TOPES == Topes(
        archivo_bytes=10_000_000,
        hojas=20,
        filas=1000,
        columnas=40,
        celdas=100_000,
        caracteres_celda=1000,
        caracteres_cuerpo=300_000,
        campo_csv=32_000,
        tiempo_s=60,
        parte_bytes=2_000_000,
    )


def _columna(n: int) -> list[list[object]]:
    return [[f"v{fila}"] for fila in range(1, n + 1)]


CASOS_DE_TOPE = [
    pytest.param(
        replace(TOPES, hojas=2),
        {"A": [["x"]], "B": [["x"]]},
        {"A": [["x"]], "B": [["x"]], "C": [["x"]]},
        ("Se muestran 2 de 3 hojas.",),
        id="hojas",
    ),
    pytest.param(
        replace(TOPES, filas=3),
        {"Datos": _columna(3)},
        {"Datos": _columna(4)},
        ("Hoja «Datos»: se muestran 3 de 4 filas. El resto no se leyó.",),
        id="filas",
    ),
    pytest.param(
        replace(TOPES, columnas=3),
        {"Datos": [["a", "b", "c"]]},
        {"Datos": [["a", "b", "c", "d"]]},
        ("Hoja «Datos»: se muestran 3 de 4 columnas.",),
        id="columnas",
    ),
    pytest.param(
        replace(TOPES, celdas=3),
        {"Datos": _columna(3)},
        {"Datos": _columna(4)},
        ("El libro llegó al tope de 3 celdas. El resto no se leyó.",),
        id="celdas",
    ),
    pytest.param(
        replace(TOPES, caracteres_celda=5),
        {"Datos": [["abcde"]]},
        {"Datos": [["abcdef"]]},
        ("Hoja «Datos»: 1 celda pasa de 5 caracteres y se recortó.",),
        id="caracteres-por-celda",
    ),
]


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(93)
@pytest.mark.parametrize(("topes", "justo", "arriba", "avisos"), CASOS_DE_TOPE)
def test_cada_tope_que_trunca_deja_entrar_lo_justo_y_avisa_uno_arriba(
    topes, justo, arriba, avisos
):
    entra = extraer(io.BytesIO(libro(justo)), topes=topes)
    recortada = extraer(io.BytesIO(libro(arriba)), topes=topes)

    assert (entra.avisos, entra.truncada) == ((), False)
    assert (recortada.avisos, recortada.truncada) == (avisos, True)
    assert recortada.cuerpo.startswith("\n".join(avisos) + "\n\n")
    # Lo que se muestra es lo mismo que entra justo.
    assert [(h.columnas, h.filas) for h in CuerpoDeFicha.parsear_cuerpo(recortada.cuerpo)] == [
        (h.columnas, h.filas) for h in CuerpoDeFicha.parsear_cuerpo(entra.cuerpo)
    ]


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(93)
def test_el_tope_del_cuerpo_deja_entrar_lo_justo_y_corta_uno_arriba():
    contenido = libro({"Datos": [["Texto"], ["a" * 50], ["b" * 50]]})
    largo = len(extraer(io.BytesIO(contenido)).cuerpo)

    entra = extraer(io.BytesIO(contenido), topes=replace(TOPES, caracteres_cuerpo=largo))
    cortada = extraer(io.BytesIO(contenido), topes=replace(TOPES, caracteres_cuerpo=largo - 1))

    assert (entra.avisos, len(entra.cuerpo)) == ((), largo)
    aviso = f"El cuerpo llegó al tope de {largo - 1} caracteres. El resto no se leyó."
    assert cortada.avisos == (aviso,)
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(cortada.cuerpo)
    assert hoja.marca == MarcaDeHoja("Datos", 1, 2, 3, aviso)
    assert hoja.filas == [["a" * 50]]


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(93)
def test_el_aviso_de_truncamiento_va_en_la_ficha_y_en_la_marca_de_la_hoja():
    extraccion = extraer(io.BytesIO(libro({"Actas": _columna(1340)})))

    assert extraccion.cuerpo.startswith(
        "Hoja «Actas»: se muestran 1.000 de 1.340 filas. El resto no se leyó.\n\n"
    )
    assert (
        "--- Hoja «Actas» (filas 1–1000 de 1340)"
        " · Se muestran 1.000 de 1.340 filas. El resto no se leyó. ---"
    ) in extraccion.cuerpo.split("\n")


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_un_libro_de_mas_de_10_mb_se_rechaza_con_planilla_excesiva():
    contenido = libro(TRES_HOJAS)

    extraer(io.BytesIO(contenido), topes=replace(TOPES, archivo_bytes=len(contenido)))
    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer(io.BytesIO(contenido), topes=replace(TOPES, archivo_bytes=len(contenido) - 1))

    assert (rechazo.value.motivo, rechazo.value.tope, rechazo.value.observado) == (
        "archivo",
        len(contenido) - 1,
        len(contenido),
    )
    assert str(rechazo.value).startswith("No se pudo procesar este archivo: ")
    assert not isinstance(rechazo.value, PlanillaIlegible)


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
@pytest.mark.parametrize(
    ("topes", "justo", "arriba", "motivo", "tope", "observado"),
    [
        pytest.param(
            replace(TOPES, archivo_bytes=12),
            b"a;b\r\nc;d\r\n12",
            b"a;b\r\nc;d\r\n123",
            "archivo",
            12,
            13,
            id="archivo",
        ),
        pytest.param(
            replace(TOPES, campo_csv=5),
            b"a;b\r\nc;12345\r\n",
            b"a;b\r\nc;123456\r\n",
            "campo",
            5,
            6,
            id="campo",
        ),
        # El de la admisión: `clasificar` lo marca y `extraer_csv` dice cuánto mide.
        pytest.param(
            TOPES,
            b'a;b\r\nc;"' + b"y" * 32_000 + b'"\r\n',
            b'a;b\r\nc;"' + b"y" * 32_001 + b'"\r\n',
            "campo",
            32_000,
            32_001,
            id="campo-de-32-kb",
        ),
    ],
)
def test_un_csv_fuera_de_topes_se_rechaza_con_planilla_excesiva(
    topes, justo, arriba, motivo, tope, observado
):
    extraer_csv(io.BytesIO(justo), "datos.csv", topes=topes)
    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer_csv(io.BytesIO(arriba), "datos.csv", topes=topes)

    assert (rechazo.value.motivo, rechazo.value.tope, rechazo.value.observado) == (
        motivo,
        tope,
        observado,
    )
    assert isinstance(rechazo.value, ErrorHistoriaViva)


@pytest.mark.rnf("RNF-20")
def test_un_campo_que_csv_no_lee_es_planilla_excesiva():
    contenido = b'a;b\r\n1;"' + b"y" * 200_000 + b'"\r\n'

    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer_csv(io.BytesIO(contenido), "datos.csv")

    assert (rechazo.value.motivo, rechazo.value.tope) == ("campo", 32_000)
    assert rechazo.value.observado > 128 * 1024


def _hoja_cruda(filas: str, *, dimension: str | None = None) -> bytes:
    """Un libro de una hoja cuyo `<sheetData>` es `filas`, tal cual (lo que openpyxl no
    escribe: filas sin `<dimension>`, celdas sin referencia, valores en línea)."""
    sana = libro({"Datos": [["x"]]})
    salida = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(sana)) as original, zipfile.ZipFile(
        salida, "w", zipfile.ZIP_DEFLATED
    ) as nuevo:
        for miembro in original.namelist():
            contenido = original.read(miembro)
            if miembro == "xl/worksheets/sheet1.xml":
                texto = contenido.decode()
                texto = re.sub(r"<dimension [^>]*/>", dimension or "", texto)
                datos = f"<sheetData>{filas}</sheetData>"
                texto = re.sub(r"<sheetData>.*</sheetData>", datos, texto)
                contenido = texto.encode()
            nuevo.writestr(miembro, contenido)
    return salida.getvalue()


def _filas_en_linea(desde: int, hasta: int) -> str:
    return "".join(
        f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>f{n}</t></is></c></row>'
        for n in range(desde, hasta + 1)
    )


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    "dimension",
    [
        pytest.param(None, id="sin-dimension"),
        pytest.param('<dimension ref="A1:A2"/>', id="dimension-que-miente"),
    ],
)
def test_el_total_de_filas_se_cuenta_leyendo(dimension):
    contenido = _hoja_cruda(_filas_en_linea(1, 1500), dimension=dimension)

    extraccion = extraer(io.BytesIO(contenido))

    assert extraccion.avisos == (
        "Hoja «Datos»: se muestran 1.000 de 1.500 filas. El resto no se leyó.",
    )
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert (hoja.marca.total, hoja.filas[-1]) == (1500, ["f1000"])


@pytest.mark.rnf("RNF-20")
def test_lo_que_sigue_a_un_hueco_sin_dimension_no_se_pierde_en_silencio():
    contenido = _hoja_cruda(_filas_en_linea(1, 3) + _filas_en_linea(5000, 5000))

    extraccion = extraer(io.BytesIO(contenido))

    assert extraccion.avisos == (
        "Hoja «Datos»: se muestran 3 de 5.000 filas. El resto no se leyó.",
    )


@pytest.mark.rnf("RNF-20")
def test_celdas_sin_referencia_y_valores_en_linea_se_leen_como_los_escribe_excel():
    filas = (
        '<row><c t="inlineStr"><is><r><t>Ho</t></r><r><t>la</t></r>'
        "<rPh><t>fonética</t></rPh></is></c><c><v>7</v></c></row>"
        '<row><c r="B2" t="b"><v>1</v></c></row>'
    )

    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraer(io.BytesIO(_hoja_cruda(filas))).cuerpo)

    assert [hoja.columnas, *hoja.filas] == [["Hola", "7"], ["", "VERDADERO"]]


@pytest.mark.rnf("RNF-20")
def test_una_fila_enorme_se_lee_con_memoria_acotada():
    """El modo lectura de openpyxl armaba el `<row>` entero: 45 MB de XML, 1,26 GB (088)."""
    celdas = "".join(f'<c r="{_letras(n)}1"><v>{n}</v></c>' for n in range(1, 16_385))
    contenido = _hoja_cruda(f'<row r="1">{celdas * 1}</row>' + _filas_en_linea(2, 3))

    tracemalloc.start()
    try:
        extraccion = extraer(io.BytesIO(contenido))
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert pico < 16 * 1024 * 1024
    assert extraccion.avisos == ("Hoja «Datos»: se muestran 40 de 16.384 columnas.",)


def _letras(columna: int) -> str:
    letras = ""
    while columna:
        columna, resto = divmod(columna - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


@pytest.mark.rnf("RNF-20")
def test_una_fila_de_decenas_de_megas_no_llena_la_memoria():
    # Unos 40 MB de XML en una sola fila: la parte la acota la guarda de ZIP, no la lectura.
    celda = '<c t="inlineStr"><is><t>' + "z" * 1000 + "</t></is></c>"
    contenido = _hoja_cruda(f'<row r="1">{celda * 40_000}</row>')

    tracemalloc.start()
    try:
        extraer(io.BytesIO(contenido))
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # El libro comprimido está en memoria (unos KB); lo leído, no.
    assert pico < 32 * 1024 * 1024


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_la_lectura_se_corta_mientras_corre_cuando_vence_su_tiempo():
    # Si la lectura siguiera hasta el final, la cola rota daría `PlanillaIlegible`.
    contenido = _hoja_cruda(_filas_en_linea(1, 50_000) + "<row><c><v>1</v></row>")

    comienzo = time.monotonic()
    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer(io.BytesIO(contenido), tope_s=0)

    assert time.monotonic() - comienzo < 5
    assert (rechazo.value.motivo, rechazo.value.tope) == ("tiempo", 0)
    with pytest.raises(PlanillaIlegible):
        extraer(io.BytesIO(contenido))


@pytest.mark.rnf("RNF-20")
def test_el_tiempo_de_la_lectura_es_el_menor_entre_60_s_y_el_de_la_capacidad():
    contenido = _hoja_cruda(_filas_en_linea(1, 50_000))

    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer(io.BytesIO(contenido), topes=replace(TOPES, tiempo_s=0), tope_s=600)

    assert (rechazo.value.motivo, rechazo.value.tope) == ("tiempo", 0)


@pytest.mark.rnf("RNF-20")
def test_una_etiqueta_de_mas_de_256_kib_es_ilegible_sin_esperar_a_libxml2():
    """libxml2 es cuadrático en los atributos de un elemento (009)."""
    atributos = " ".join(f'a{n}="1"' for n in range(60_000))
    contenido = _hoja_cruda(f'<row r="1" {atributos}><c r="A1"><v>1</v></c></row>')

    comienzo = time.monotonic()
    with pytest.raises(PlanillaIlegible) as rechazo:
        extraer(io.BytesIO(contenido))

    assert time.monotonic() - comienzo < 5
    assert rechazo.value.motivo == "zip-corrupto"


def _copia_de_la_hoja(contenido: bytes) -> bytes:
    """La hoja tal como la deja el filtro, la que lee openpyxl."""
    copia, _ = planillas._filtrar_libro(
        contenido, TOPES, planillas._Plazo(TOPES, None)
    )
    with zipfile.ZipFile(io.BytesIO(copia)) as comprimido:
        return comprimido.read("xl/worksheets/sheet1.xml")


@pytest.mark.rnf("RNF-20")
def test_las_celdas_y_las_filas_repetidas_no_se_copian_dos_veces():
    """Un `<c r="A2">` repetido un millón de veces son 58 KB comprimidos: copiado entero, la
    hoja que lee openpyxl pasaba los 60 MB."""
    repetidas = '<c r="A2"><v>2</v></c>' * 100_000
    filas = (
        '<row r="1"><c r="A1"><v>1</v></c></row>'
        f'<row r="2"><c r="A2"><v>1</v></c>{repetidas}<c r="B2"><v>3</v></c></row>'
        '<row r="2"><c r="A2"><v>9</v></c></row>'
        '<row r="1"><c r="A1"><v>9</v></c></row>'
        '<row><c><v>4</v></c><c r="A3"><v>9</v></c></row>'
    )
    contenido = _hoja_cruda(filas)

    copia = _copia_de_la_hoja(contenido)
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraer(io.BytesIO(contenido)).cuerpo)

    assert len(copia) < 1000
    # La fila sin `r` va después de la última leída, aunque las repetidas digan 2 y 1.
    assert [hoja.columnas, *hoja.filas] == [["1", ""], ["1", "3"], ["4", ""]]


SST_NS = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'


def _con_parte(base: bytes, nombre: str, datos: str) -> bytes:
    """`base` con la parte `nombre` reemplazada por `datos`."""
    salida = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(base)) as original,
        zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as nuevo,
    ):
        for miembro in original.namelist():
            contenido = datos.encode() if miembro == nombre else original.read(miembro)
            nuevo.writestr(miembro, contenido)
    return salida.getvalue()


def _con_runs(cuantos: int) -> bytes:
    """Un solo `<si>` de `cuantos` runs de una letra: defusedxml lo armaba entero."""
    runs = "<r><t>a</t></r>" * cuantos
    return _con_textos_compartidos(SST.replace("<si><t>{}</t></si>", f"<si>{runs}</si>"))


TEXTOS_HOSTILES = [
    pytest.param(
        lambda: _con_textos_compartidos(
            SST.format("Año").replace("</sst>", "<si><t>a</t></si>" * 1_000_000 + "</sst>")
        ),
        id="un-millon-de-textos",
    ),
    pytest.param(lambda: _con_runs(1_000_000), id="un-texto-de-un-millon-de-runs"),
    # Unos estilos con raíz `sst`: no hay parte exenta del tope que no pase por el filtro.
    pytest.param(
        lambda: _con_parte(
            libro({"Datos": [["x"]]}),
            "xl/styles.xml",
            SST_NS + "<si><t>a</t></si>" * 1_000_000 + "</sst>",
        ),
        id="estilos-con-raiz-sst",
    ),
]


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
@pytest.mark.parametrize("contenido", TEXTOS_HOSTILES)
def test_los_textos_compartidos_se_leen_en_el_filtro_y_se_cortan_cuando_vence_el_tiempo(
    contenido,
):
    libro_hostil = contenido()

    comienzo = time.monotonic()
    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer(io.BytesIO(libro_hostil), tope_s=0.3)

    assert time.monotonic() - comienzo < 1.5
    assert (rechazo.value.motivo, rechazo.value.tope) == ("tiempo", 0.3)


@pytest.mark.rnf("RNF-20")
def test_un_texto_compartido_de_millones_de_runs_se_lee_con_memoria_acotada():
    contenido = _con_runs(150_000)

    tracemalloc.start()
    try:
        extraccion = extraer(io.BytesIO(contenido))
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert pico < 32 * 1024 * 1024
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraccion.cuerpo)
    assert hoja.columnas == ["a" * 1000]
    assert extraccion.avisos == ("Hoja «Datos»: 1 celda pasa de 1.000 caracteres y se recortó.",)


@pytest.mark.rnf("RNF-20")
def test_los_textos_compartidos_llegan_a_openpyxl_en_linea_y_sin_la_guia_fonetica():
    sst = SST_NS + "<si><r><t>Ho</t></r><r><t>la</t></r><rPh><t>fonética</t></rPh></si></sst>"
    contenido = _con_textos_compartidos(sst)

    copia, _ = planillas._filtrar_libro(contenido, TOPES, planillas._Plazo(TOPES, None))
    with zipfile.ZipFile(io.BytesIO(copia)) as comprimido:
        assert b't="s"' not in comprimido.read("xl/worksheets/sheet1.xml")
        assert b"<si>" not in comprimido.read("xl/sharedStrings.xml")
    (hoja,) = CuerpoDeFicha.parsear_cuerpo(extraer(io.BytesIO(contenido)).cuerpo)
    assert hoja.columnas == ["Hola"]


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    "indice", [pytest.param("1", id="fuera-de-rango"), pytest.param("-1", id="negativo")]
)
def test_un_indice_de_texto_compartido_que_no_existe_es_ilegible(indice):
    contenido = _con_textos_compartidos(SST.format("x"))
    contenido = _con_parte(
        contenido,
        "xl/worksheets/sheet1.xml",
        zipfile.ZipFile(io.BytesIO(contenido))
        .read("xl/worksheets/sheet1.xml")
        .decode()
        .replace("<v>0</v>", f"<v>{indice}</v>"),
    )

    with pytest.raises(PlanillaIlegible):
        extraer(io.BytesIO(contenido))


@pytest.mark.rnf("RNF-20")
def test_dos_partes_de_textos_compartidos_son_ilegibles():
    contenido = _con_parte(
        _con_textos_compartidos(SST.format("x")), "xl/styles.xml", SST_NS + "</sst>"
    )

    with pytest.raises(PlanillaIlegible) as rechazo:
        extraer(io.BytesIO(contenido))

    assert "textos compartidos" in str(rechazo.value.__cause__)


def _con_estilos_de(tamano: int) -> bytes:
    """Un libro cuyos estilos miden `tamano` bytes, rellenos con un comentario."""
    base = libro({"Datos": [["x"]]})
    with zipfile.ZipFile(io.BytesIO(base)) as comprimido:
        estilos = comprimido.read("xl/styles.xml").decode()
    relleno = "x" * (tamano - len(estilos) - len("<!---->"))
    return libro({"Datos": [["x"]]}, parches={"</styleSheet>": f"<!--{relleno}--></styleSheet>"})


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_una_parte_que_openpyxl_lee_entera_tiene_tope_de_bytes():
    """Unos estilos de 31 MB tardaban 40 s en openpyxl."""
    topes = replace(TOPES, parte_bytes=200_000)

    extraer(io.BytesIO(_con_estilos_de(200_000)), topes=topes)
    with pytest.raises(PlanillaExcesiva) as rechazo:
        extraer(io.BytesIO(_con_estilos_de(200_001)), topes=topes)

    assert (rechazo.value.motivo, rechazo.value.tope) == ("parte", 200_000)
    assert str(rechazo.value).startswith("No se pudo procesar este archivo: ")


@pytest.mark.rnf("RNF-20")
def test_los_textos_compartidos_no_tienen_el_tope_de_las_partes():
    # 340 KB de textos compartidos; el resto de las partes, menos de 50 KB.
    sst = SST.format("Año").replace("</sst>", "<si><t>a</t></si>" * 20_000 + "</sst>")

    (hoja,) = CuerpoDeFicha.parsear_cuerpo(
        extraer(
            io.BytesIO(_con_textos_compartidos(sst)), topes=replace(TOPES, parte_bytes=50_000)
        ).cuerpo
    )

    assert hoja.columnas == ["Año"]


@pytest.mark.rnf("RNF-20")
def test_un_numero_de_400_cifras_es_planilla_ilegible_y_no_error_interno():
    contenido = _hoja_cruda('<row r="1"><c r="A1"><v>' + "9" * 400 + "</v></c></row>")

    with pytest.raises(PlanillaIlegible):
        extraer(io.BytesIO(contenido))


@pytest.mark.rnf("RNF-20")
def test_nombres_de_miembro_repetidos_son_planilla_ilegible():
    """OPC los prohíbe: de dos, zipfile lee uno y openpyxl podría leer el otro."""
    salida = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(libro({"Datos": [["x"]]}))) as original,
        zipfile.ZipFile(salida, "w") as nuevo,
    ):
        for miembro in original.namelist():
            nuevo.writestr(miembro, original.read(miembro))
        nuevo.writestr("XL/worksheets/sheet1.xml", original.read("xl/worksheets/sheet1.xml"))

    with pytest.raises(PlanillaIlegible) as rechazo:
        extraer(io.BytesIO(salida.getvalue()))

    assert "repetid" in str(rechazo.value.__cause__)
