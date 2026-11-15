"""Los nueve obligatorios para Validar (DEC-126): sin alguno, la Ficha queda Incompleta.

Tipo de Pieza, tipo de actividad, `fecha_inicio`, `fecha_fin`, `precision_fecha`, origen,
propietario, resumen y `archivo_original`; los mismos para toda clase, y ni el cuerpo ni el
Contexto cuentan (DEC-13). Por HTTP contra Postgres real.
"""

from __future__ import annotations

import psycopg
import pytest
from capa_modelos_falsa import IMAGEN
from aplicacion import entrar
from espera import esperar_estado
from test_fichas import divergencias
from test_procesar_pieza import OBLIGATORIOS
from validacion import EDICION_COMPLETA, MARIA, PDF

pytestmark = [
    pytest.mark.rf("RF-51", "RF-52"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


@pytest.mark.historia(117, 287, 288)
def test_con_los_nueve_queda_validada_y_la_pieza_guarda_tipo_de_actividad_y_propietario(
    cliente, pieza_pendiente, url_bd
):
    resultado = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    assert (resultado.json()["estado"], resultado.json()["campos_faltantes"]) == ("Validada", [])
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT tipo_actividad, propietario FROM acervo.pieza WHERE pieza_id = %s",
            (pieza_pendiente,),
        ).fetchone()
    assert fila == ("Evento Académico", "Secretaría de Extensión Universitaria")


@pytest.mark.historia(287, 288)
def test_el_detalle_de_la_pieza_trae_tipo_de_actividad_y_propietario(cliente, pieza_pendiente):
    cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    detalle = cliente.get(f"/api/piezas/{pieza_pendiente}").json()

    assert (detalle["tipo_actividad"], detalle["propietario"]) == (
        "Evento Académico",
        "Secretaría de Extensión Universitaria",
    )


@pytest.mark.historia(117)
def test_sin_archivo_original_la_ficha_queda_incompleta_con_ese_campo_en_faltantes(
    cliente, pieza_pendiente, url_bd
):
    # Un Aporte llega sin archivo (DEC-12): mientras no haya Aportes, se simula a mano.
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET archivo_original_id = NULL WHERE pieza_id = %s",
            (pieza_pendiente,),
        )
        url = f"/api/piezas/{pieza_pendiente}/ficha"

        resultado = cliente.put(url, json=EDICION_COMPLETA).json()

        assert (resultado["estado"], resultado["campos_faltantes"]) == (
            "Incompleta",
            ["archivo_original"],
        )
        assert cliente.get(url).json()["faltantes"] == ["archivo_original"]
        assert divergencias(conexion) == []


@pytest.mark.historia(285)
def test_no_es_una_actividad_cuenta_como_tipo_de_actividad(cliente, pieza_pendiente):
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"tipo_actividad": "No es una actividad"},
    )

    assert respuesta.json()["estado"] == "Validada"


@pytest.mark.historia(285)
def test_un_tipo_de_actividad_fuera_del_catalogo_responde_422_con_el_campo(
    cliente, pieza_pendiente
):
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"tipo_actividad": "Pasantía"},
    )

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert (problema["type"], problema["campo"]) == (
        "/errores/tipo-actividad-desconocido",
        "tipo_actividad",
    )
    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Pendiente"


@pytest.mark.historia(117)
@pytest.mark.parametrize(
    "archivo",
    [
        ("acta.pdf", PDF, "application/pdf"),
        ("acto-de-egreso.jpg", IMAGEN, "image/jpeg"),
        ("nota.txt", b"Nota del Decano sobre el acto de 1975.", "text/plain"),
    ],
    ids=["documento", "imagen", "texto"],
)
def test_los_nueve_son_los_mismos_para_toda_clase_y_el_cuerpo_y_el_contexto_no_cuentan(
    cliente, archivo
):
    entrar(cliente, MARIA)
    pieza_id = cliente.post("/api/piezas", files={"archivo": archivo}).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    url = f"/api/piezas/{pieza_id}/ficha"

    assert cliente.get(url).json()["obligatorios"] == OBLIGATORIOS
    resultado = cliente.put(url, json=EDICION_COMPLETA | {"cuerpo": "", "contexto": ""}).json()

    assert (resultado["estado"], resultado["campos_faltantes"]) == ("Validada", [])
