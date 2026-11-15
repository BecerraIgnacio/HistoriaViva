"""Mis Piezas (087, P2): lo que la persona subió y no está Validado, más las Incompletas que
trabajó y sus Validadas todavía no indexadas; los Aportes solo con «Todas» (DEC-123, R-43 h).

Por HTTP contra Postgres real, con el trabajador de procesamiento de la app y la capa falsa; la
indexación la corre el trabajador aparte de `test_indexacion`, para ver la «No indexada».
"""

from __future__ import annotations

import re

import pytest

from aplicacion import entrar
from espera import esperar_estado
from test_api_agentes import aportador, aportar, emitir, encender  # noqa: F401 (fixtures)
from test_indexacion import bd, trabajador, validar  # noqa: F401 (fixtures)
from validacion import JORGE, MARIA, PDF, edicion_sin

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")


def _subir(cliente, persona: str, nombre: str) -> str:
    entrar(cliente, persona)
    respuesta = cliente.post(
        "/api/piezas", files={"archivo": (nombre, PDF + nombre.encode(), "application/pdf")}
    )
    assert respuesta.status_code == 201, respuesta.text
    pieza_id: str = respuesta.json()["pieza_id"]
    return pieza_id


def _pendiente(cliente, persona: str, nombre: str) -> str:
    pieza_id = _subir(cliente, persona, nombre)
    esperar_estado(cliente, pieza_id, "Pendiente")
    return pieza_id


def _incompleta(cliente, pieza_id: str) -> None:
    version = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["version"]
    respuesta = cliente.put(
        f"/api/piezas/{pieza_id}/ficha", json=edicion_sin("origen") | {"version_base": version}
    )
    assert respuesta.json()["estado"] == "Incompleta", respuesta.text


def _filas(cliente, **params) -> dict[str, dict]:
    respuesta = cliente.get("/api/piezas", params=params)
    assert respuesta.status_code == 200, respuesta.text
    return {fila["pieza_id"]: fila for fila in respuesta.json()["piezas"]}


@pytest.mark.historia(104, 293, 296)
@pytest.mark.xdist_group("indice")  # embeber carga llama-index: un solo proceso lo paga (130)
def test_mias_trae_lo_que_subi_sin_validar_lo_que_trabaje_y_mis_validadas_no_indexadas(
    cliente, capa_modelos, trabajador
):
    indexada = _pendiente(cliente, MARIA, "indexada.pdf")
    validar(cliente, indexada)
    while trabajador.procesar_siguiente():  # la cola puede traer indexaciones de otros tests
        pass
    no_indexada = _pendiente(cliente, MARIA, "no-indexada.pdf")
    validar(cliente, no_indexada)
    pendiente = _pendiente(cliente, MARIA, "pendiente.pdf")
    incompleta = _pendiente(cliente, MARIA, "incompleta.pdf")
    _incompleta(cliente, incompleta)
    trabajada = _pendiente(cliente, JORGE, "trabajada.pdf")
    entrar(cliente, MARIA)
    _incompleta(cliente, trabajada)
    ajena = _pendiente(cliente, JORGE, "ajena.pdf")
    capa_modelos.demorar()
    recibida = _subir(cliente, MARIA, "recibida.pdf")

    filas = _filas(cliente, mias="true")

    mias = {
        pieza_id: (filas[pieza_id]["estado"], filas[pieza_id]["no_indexada"])
        for pieza_id in (indexada, no_indexada, pendiente, incompleta, trabajada, ajena, recibida)
        if pieza_id in filas
    }
    assert mias == {
        no_indexada: ("Validada", True),
        pendiente: ("Pendiente", False),
        incompleta: ("Incompleta", False),
        trabajada: ("Incompleta", False),
        recibida: ("Recibida", False),
    }
    capa_modelos.liberar()
    esperar_estado(cliente, recibida, "Pendiente")


@pytest.mark.historia(106)
def test_los_aportes_solo_aparecen_con_todas_y_cada_fila_dice_quien_la_subio(
    cliente, aportador
):
    acuse = aportar(cliente, aportador, clave="clave-mis-piezas")
    assert acuse.status_code == 201, acuse.text
    aporte = acuse.json()["pieza_id"].removeprefix("pza_")
    subida = _pendiente(cliente, MARIA, "propia.pdf")
    ajena = _pendiente(cliente, JORGE, "de-jorge.pdf")
    entrar(cliente, MARIA)

    todas, mias = _filas(cliente), _filas(cliente, mias="true")

    assert {pieza_id: todas[pieza_id]["creada_por"] for pieza_id in (aporte, subida, ajena)} == {
        aporte: None,
        subida: MARIA,
        ajena: JORGE,
    }
    assert (aporte in mias, subida in mias, ajena in mias) == (False, True, False)


@pytest.mark.historia(104, 292, 296)
def test_la_pantalla_tiene_los_cinco_grupos_el_filtro_y_un_solo_anuncio(cliente):
    pantalla = cliente.get("/").text

    titulos = re.findall(r'<h3 id="titulo-[^"]+" hidden>([^<]+)</h3>', pantalla)
    assert titulos == [
        "Mis Lotes",  # los contadores de los Lotes propios (086)
        "Para validar",
        "Procesando",
        "Fallaron",
        "Requieren análisis humano",
        "Validadas, no indexadas",
    ]
    assert pantalla.count("aria-live") == 1
    assert 'value="mias" checked' in pantalla and 'value="todas"' in pantalla
    for estado in ("Cargando tus Piezas…", "No pudimos cargar la lista.", "No tenés Piezas"):
        assert estado in pantalla
