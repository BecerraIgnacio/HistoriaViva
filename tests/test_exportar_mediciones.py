"""Exportar las mediciones de la sesión: la tabla que se lleva a la documentación del gate.

Es la user story 25. El responsable del PPS descarga un CSV y lo abre en una planilla; lo
que se prueba es que ese archivo diga lo mismo que `GET /api/mediciones`, en el formato que
fija el contrato.
"""

from __future__ import annotations

import csv
import io
import re

import pytest
from aplicacion import entrar
from espera import esperar_estado
from validacion import (
    ADMINISTRADOR_TECNICO,
    CONTEXTO,
    DIRECTORA_DE_CARRERA,
    EDICION_COMPLETA,
    JORGE,
    MARIA,
    PDF,
    abrir,
    edicion_sin,
    mediciones,
)

pytestmark = [
    pytest.mark.rf("RF-41"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

COLUMNAS = [
    "medicion_id",
    "pieza_id",
    "persona_id",
    "nombre_archivo",
    "abierta_el",
    "guardada_el",
    "duracion_s",
    "estado_final",
    "campos_modificados",
    "campos_faltantes",
    "cuerpo_editado",
    "caracteres_cuerpo_propuesta",
    "caracteres_cuerpo_guardado",
    "clase_original",
    "metodo_autenticacion",
    "modo_validacion",
]
"""Las 16 columnas: las 13 del Prototipo primero y en su orden, después las de DEC-92."""


def exportar(cliente):
    """Lo baja el Administrador técnico: son tiempos de trabajo por persona (DEC-93)."""
    entrar(cliente, ADMINISTRADOR_TECNICO)
    respuesta = cliente.get("/api/mediciones.csv")
    assert respuesta.status_code == 200
    return respuesta


def filas_del_csv(respuesta):
    texto = respuesta.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(texto, newline="")))


LISTAS = {"campos_modificados", "campos_faltantes"}
ENTEROS = {"duracion_s", "caracteres_cuerpo_propuesta", "caracteres_cuerpo_guardado"}


def leer(fila):
    """Vuelve una fila del CSV a la forma de `GET /api/mediciones`, como la leería una planilla."""
    leida = {}
    for columna, celda in fila.items():
        if columna in LISTAS:
            leida[columna] = celda.split(";") if celda else []
        elif celda == "":
            leida[columna] = None
        elif columna == "cuerpo_editado":
            leida[columna] = {"true": True, "false": False}[celda]
        elif columna in ENTEROS:
            leida[columna] = int(celda)
        else:
            leida[columna] = celda
    return leida


def test_descarga_un_adjunto_utf8_con_bom_y_las_columnas_del_contrato(cliente, pieza_pendiente):
    abrir(cliente, pieza_pendiente)

    respuesta = exportar(cliente)

    assert respuesta.headers["content-type"].startswith("text/csv")
    assert respuesta.headers["content-disposition"] == (
        'attachment; filename="mediciones-historia-viva.csv"'
    )
    # El BOM es lo que hace que Excel abra las tildes bien sin preguntar la codificación.
    assert respuesta.content.startswith(b"\xef\xbb\xbf")
    encabezado = respuesta.content.decode("utf-8-sig").splitlines()[0]
    assert encabezado == ",".join(COLUMNAS)


def test_una_sesion_con_una_validada_y_una_incompleta_coincide_con_la_lista(
    cliente, pieza_pendiente
):
    incompleta = abrir(cliente, pieza_pendiente)
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen")
        | {"medicion_id": incompleta["medicion_id"], "contexto": CONTEXTO},
    )
    validada = abrir(cliente, pieza_pendiente)
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA
        | {"medicion_id": validada["medicion_id"], "contexto": CONTEXTO, "version_base": 2},
    )

    esperadas = mediciones(cliente)
    filas = filas_del_csv(exportar(cliente))

    # Las mismas filas y en el mismo orden: el CSV es la lista, no otra consulta (US 25).
    assert [fila["estado_final"] for fila in filas] == ["Validada", "Incompleta"]
    assert [leer(fila) for fila in filas] == esperadas
    # Fechas en ISO 8601 UTC al segundo, y los booleanos escritos como en el JSON.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", filas[0]["guardada_el"])
    assert filas[0]["cuerpo_editado"] == "true"
    # Las listas van separadas por punto y coma dentro de la celda, para no chocar con la
    # coma que separa las columnas.
    assert filas[0]["campos_modificados"] == (
        "tipo;tipo_actividad;fecha_inicio;fecha_fin;precision_fecha;origen;propietario;resumen"
    )
    assert filas[1]["campos_faltantes"] == "origen"
    assert filas[0]["campos_faltantes"] == ""


def test_una_medicion_abierta_sin_guardar_deja_las_celdas_vacias(cliente, pieza_pendiente):
    abrir(cliente, pieza_pendiente)

    (fila,) = filas_del_csv(exportar(cliente))

    # Abierta y no guardada también es un dato: una Pieza que alguien miró y dejó.
    assert fila["guardada_el"] == ""
    assert fila["duracion_s"] == ""
    assert fila["estado_final"] == ""
    assert fila["caracteres_cuerpo_guardado"] == ""
    assert fila["cuerpo_editado"] == "false"


def test_un_nombre_de_archivo_con_coma_no_corre_las_columnas(cliente):
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas",
        files={"archivo": ("acta, consejo 1975.pdf", PDF, "application/pdf")},
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    abrir(cliente, pieza_id)

    (fila,) = filas_del_csv(exportar(cliente))

    assert fila["nombre_archivo"] == "acta, consejo 1975.pdf"
    assert fila["pieza_id"] == pieza_id


@pytest.mark.historia(196)
@pytest.mark.historia(200)
def test_un_nombre_de_archivo_que_empieza_como_formula_sale_neutralizado(cliente):
    # Con `/` el nombre se acortaría a lo que sigue a la última barra (`_nombre_de_archivo`).
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ('=HYPERLINK("x")', PDF, "application/pdf")}
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    abrir(cliente, pieza_id)
    (medicion,) = mediciones(cliente)

    respuesta = exportar(cliente)

    assert respuesta.content.startswith(b"\xef\xbb\xbf")
    assert respuesta.content.decode("utf-8-sig").splitlines()[0] == ",".join(COLUMNAS)
    (fila,) = filas_del_csv(respuesta)
    # Quien abre el CSV en una planilla ve el nombre como texto: el `'` no deja ejecutarlo.
    assert medicion["nombre_archivo"].startswith("=HYPERLINK(")
    assert fila["nombre_archivo"] == "'" + medicion["nombre_archivo"]


def test_sin_mediciones_descarga_solo_el_encabezado(cliente):
    entrar(cliente, MARIA)

    respuesta = exportar(cliente)

    assert respuesta.content.decode("utf-8-sig").splitlines() == [",".join(COLUMNAS)]


def test_la_pantalla_de_entrada_ofrece_la_descarga(cliente):
    respuesta = cliente.get("/")

    # El responsable del PPS la baja desde la aplicación, sin armar la URL a mano.
    assert 'href="/api/mediciones.csv"' in respuesta.text


@pytest.mark.historia(170)
@pytest.mark.parametrize("ruta", ["/api/mediciones", "/api/mediciones.csv"])
@pytest.mark.parametrize("persona", [MARIA, JORGE, DIRECTORA_DE_CARRERA],
                         ids=["rgc", "coordinador", "director_carrera"])
def test_las_mediciones_sin_operar_sistema_responden_403(cliente, persona, ruta):
    # Son tiempos de trabajo por persona: la lista y el CSV, los dos (DEC-93).
    entrar(cliente, persona)

    respuesta = cliente.get(ruta)

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"


def test_exportar_sin_sesion_responde_401(cliente):
    assert cliente.get("/api/mediciones.csv").status_code == 401
