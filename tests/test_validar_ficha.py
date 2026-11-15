"""Validar un documento en pantalla partida: de Pendiente a Validada, firmada.

Es el criterio de salida del plan para documentos. La pantalla en sí no se prueba acá —que
sea cómoda es lo que mide la sesión con la SEU—; lo que se prueba es que el acto de validar
deje la Ficha firmada, versionada y con el Contexto que aportó la persona.
"""

from __future__ import annotations

import psycopg
import pytest
from aplicacion import entrar
from capa_modelos_falsa import TRANSCRIPCION_FIJA
from validacion import (
    CON_CONTEXTO,
    CONTEXTO,
    EDICION_COMPLETA,
    MARIA,
    PDF,
    TRANSCRIPCION_CORREGIDA,
)

pytestmark = [
    pytest.mark.rf("RF-11", "RF-12"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


def test_guardar_con_los_obligatorios_completos_deja_la_ficha_validada_a_su_nombre(
    cliente, pieza_pendiente
):
    guardado = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    assert guardado.status_code == 200
    resultado = guardado.json()
    assert resultado["estado"] == "Validada"
    assert resultado["campos_faltantes"] == []
    assert resultado["version"] == 2
    assert resultado["validada_por"] == MARIA
    assert resultado["validada_el"]

    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (2, "persona")
    assert ficha["faltantes"] == []
    frontmatter = ficha["frontmatter"]
    assert frontmatter["estado"] == "Validada"
    assert frontmatter["validada_por"] == MARIA
    assert frontmatter["validada_el"] == resultado["validada_el"]
    assert frontmatter["modo_validacion"] == "individual"
    # Una Fecha exacta es un rango de un solo día (CONTEXT.md, "Fecha").
    assert (frontmatter["fecha_inicio"], frontmatter["fecha_fin"]) == (
        "1975-05-03",
        "1975-05-03",
    )


def test_la_pieza_validada_aparece_como_tal_en_la_lista(cliente, pieza_pendiente):
    cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    (fila,) = cliente.get("/api/piezas?estado=Validada").json()["piezas"]

    assert fila["pieza_id"] == pieza_pendiente
    assert fila["campos_faltantes"] == []
    assert fila["tipo"] == "acta"


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("estado", "Validada"),
        ("validada_por", MARIA),
        ("validada_el", "2026-10-05T14:02:18Z"),
        ("modo_validacion", "individual"),
        ("id", "9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9"),
        ("lote", "9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9"),
        ("archivo_original", "local://originales/otra-cosa"),
        ("alerta_inyeccion", True),
    ],
)
def test_el_guardado_rechaza_los_campos_que_no_edita_una_persona(
    cliente, pieza_pendiente, campo, valor
):
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA | {campo: valor}
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["campo"] == campo
    # Un pedido rechazado no deja nada a medio guardar: la Pieza sigue donde estaba.
    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Pendiente"


def test_el_guardado_rechaza_un_tipo_que_no_esta_en_la_lista(cliente, pieza_pendiente):
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA | {"tipo": "expediente"}
    )

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/tipo-desconocido"
    assert problema["campo"] == "tipo"
    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Pendiente"


def test_los_tipos_salen_de_una_lista_que_se_cambia_sin_desplegar(
    cliente, url_bd, pieza_pendiente
):
    assert "expediente" not in cliente.get("/api/tipos").json()["tipos"]

    # La taxonomía la define la SEU y todavía no existe (consulta C4): corregirla entre dos
    # Piezas de la sesión es editar la lista, no desplegar de nuevo.
    _agregar_tipo(url_bd, "expediente")
    try:
        assert "expediente" in cliente.get("/api/tipos").json()["tipos"]
        respuesta = cliente.put(
            f"/api/piezas/{pieza_pendiente}/ficha",
            json=EDICION_COMPLETA | {"tipo": "expediente"},
        )
        assert respuesta.status_code == 200
        assert respuesta.json()["estado"] == "Validada"
    finally:
        _quitar_tipo(url_bd, "expediente")


def test_las_correcciones_al_cuerpo_y_el_contexto_quedan_en_la_version_guardada(
    cliente, pieza_pendiente
):
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"cuerpo": TRANSCRIPCION_CORREGIDA, "contexto": CONTEXTO},
    )

    # Al reabrir vuelve lo guardado, con el Contexto en su propia caja (user stories 14, 15
    # y 20).
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert (ficha["cuerpo"], ficha["contexto"]) == (TRANSCRIPCION_CORREGIDA, CONTEXTO)


def test_la_propuesta_sigue_intacta_como_version_1(cliente, pieza_pendiente):
    propuesta = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()["propuesta"]

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | CON_CONTEXTO,
    )

    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert ficha["version"] == 2
    # La versión 1 es contra lo que la instrumentación compara: no se pisa nunca (US 23).
    assert ficha["propuesta"] == propuesta
    assert ficha["propuesta"]["cuerpo"] == TRANSCRIPCION_FIJA
    assert ficha["propuesta"]["frontmatter"]["estado"] == "Pendiente"
    assert ficha["propuesta"]["frontmatter"]["tipo"] is None


def test_guardar_una_pieza_todavia_recibida_responde_409(cliente, capa_modelos):
    capa_modelos.demorar()
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ("acta-fundacion.pdf", PDF, "application/pdf")}
    ).json()["pieza_id"]

    respuesta = cliente.put(f"/api/piezas/{pieza_id}/ficha", json=EDICION_COMPLETA)

    assert respuesta.status_code == 409
    assert respuesta.json()["type"] == "/errores/transicion-invalida"


def test_guardar_una_pieza_que_no_existe_responde_404(cliente):
    entrar(cliente, MARIA)

    respuesta = cliente.put(
        "/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/ficha", json=EDICION_COMPLETA
    )

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/pieza-no-encontrada"


def test_guardar_sin_sesion_responde_401(cliente):
    respuesta = cliente.put(
        "/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/ficha", json=EDICION_COMPLETA
    )

    assert respuesta.status_code == 401


def _agregar_tipo(url_bd: str, nombre: str) -> None:
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "INSERT INTO acervo.tipo_pieza (nombre, orden) VALUES (%s, 99)", (nombre,)
        )


def _quitar_tipo(url_bd: str, nombre: str) -> None:
    with psycopg.connect(url_bd) as conexion:
        conexion.execute("DELETE FROM acervo.tipo_pieza WHERE nombre = %s", (nombre,))
