"""Campos duros durante la espera (152; decisión del 15-sep, DEC-19, DEC-122, ADR-0001).

Mientras la Pieza está Recibida, la persona completa los campos duros y el Contexto en la
pantalla partida. Se guardan en la fila de la Pieza, sin versión ni firma, y la v1 los toma al
terminar la lectura (o al completarla a mano) como escritos por la persona.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from capa_modelos_falsa import RESUMEN_FIJO
from espera import esperar_detras_del_candado, esperar_error_procesamiento, esperar_estado

from aplicacion import entrar
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from historia_viva.dominio.errores import LimiteExcedido
from test_fichas import divergencias
from test_lotes import _crear, _subir
from validacion import ADMINISTRADOR_TECNICO, MARIA, PDF

pytestmark = [
    pytest.mark.rf("RF-05"),
    pytest.mark.rf("RF-14"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

DUROS = {
    "tipo": "acta",
    "tipo_actividad": "Evento Académico",
    "fecha_inicio": "1975-05-03",
    "precision_fecha": "dia",
    "origen": "Archivo de la Secretaría de Extensión Universitaria",
    "propietario": "Secretaría de Extensión Universitaria",
    "autor": "Desconocido",
    "carreras": ["ing-civil"],
    "contexto": "Es la sesión en la que se fundó la Facultad.",
}
ESCRITOS = [
    "tipo", "tipo_actividad", "fecha_inicio", "fecha_fin", "precision_fecha", "origen",
    "propietario", "autor", "carreras",
]


def _recibida(cliente, capa_modelos) -> str:
    """Una Pieza que se está leyendo: la capa queda colgada hasta `liberar`."""
    capa_modelos.demorar()
    entrar(cliente, MARIA)
    return cliente.post(
        "/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}
    ).json()["pieza_id"]


def _guardar(cliente, pieza_id, pedido=DUROS):
    return cliente.put(f"/api/piezas/{pieza_id}/en-lectura", json=pedido)


def _versiones(url_bd, pieza_id):
    with psycopg.connect(url_bd) as conexion:
        return conexion.execute(
            "SELECT count(*) FROM acervo.ficha WHERE pieza_id = %s", (pieza_id,)
        ).fetchone()[0]


def _de_la_persona(ficha) -> dict[str, str]:
    return {campo: ficha["procedencia"][campo] for campo in ESCRITOS if campo in ficha["procedencia"]}


def test_en_recibida_se_guardan_sin_version_ni_firma_y_queda_en_la_auditoria(
    cliente, capa_modelos, url_bd
):
    pieza_id = _recibida(cliente, capa_modelos)

    respuesta = _guardar(cliente, pieza_id)

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "Recibida"
    assert respuesta.json()["campos_de_la_persona"] == ESCRITOS
    assert cliente.get(f"/api/piezas/{pieza_id}").json()["estado"] == "Recibida"
    assert cliente.get(f"/api/piezas/{pieza_id}/ficha").status_code == 404
    assert _versiones(url_bd, pieza_id) == 0
    with psycopg.connect(url_bd) as conexion:
        assert conexion.execute(
            "SELECT validada_por, validada_el, modo_validacion FROM acervo.pieza"
            " WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone() == (None, None, None)
        assert divergencias(conexion) == []
    abierta = cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()
    assert abierta["en_lectura"] is True
    assert {c: abierta["frontmatter"][c] for c in ("tipo", "origen", "autor", "carreras")} == {
        c: DUROS[c] for c in ("tipo", "origen", "autor", "carreras")
    }
    assert (abierta["cuerpo"], abierta["contexto"]) == ("", DUROS["contexto"])
    assert set(_de_la_persona(abierta).values()) == {"persona"}
    assert abierta["faltantes"] == ["resumen"]
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.PIEZA_COMPLETAR_EN_RECIBIDA
    )
    assert (str(evento.actor.usuario_id), evento.objeto.id) == (MARIA, pieza_id)
    assert evento.despues == {"campos": ESCRITOS, "contexto": True}


@pytest.mark.parametrize(
    ("cambio", "codigo", "tipo"),
    [
        ({"tipo": "inexistente"}, 422, "/errores/tipo-desconocido"),
        ({"tipo_actividad": "inexistente"}, 422, "/errores/tipo-actividad-desconocido"),
        ({"carreras": ["inexistente"]}, 422, "/errores/carrera-desconocida"),
        ({"fecha_fin": "1975-06-01"}, 422, "/errores/fecha-invalida"),
        # Ni la firma, ni el cuerpo, ni el resumen: no existen todavía (ADR-0002).
        ({"validada_por": MARIA}, 422, None),
        ({"resumen": "Un resumen."}, 422, None),
        ({"version_base": 1}, 422, None),
    ],
)
def test_en_recibida_se_valida_igual_que_al_guardar_la_ficha(
    cliente, capa_modelos, url_bd, cambio, codigo, tipo
):
    pieza_id = _recibida(cliente, capa_modelos)

    respuesta = _guardar(cliente, pieza_id, DUROS | cambio)

    assert respuesta.status_code == codigo
    if tipo:
        assert respuesta.json()["type"] == tipo
    assert cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()["campos_de_la_persona"] == []


def test_sin_validar_no_se_guarda_en_recibida(cliente, capa_modelos):
    pieza_id = _recibida(cliente, capa_modelos)
    entrar(cliente, ADMINISTRADOR_TECNICO)

    assert _guardar(cliente, pieza_id).status_code == 403


def test_al_terminar_la_lectura_la_v1_conserva_lo_escrito_como_de_la_persona(
    cliente, capa_modelos, url_bd
):
    pieza_id = _recibida(cliente, capa_modelos)
    assert _guardar(cliente, pieza_id).status_code == 200

    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (1, "modelo")
    assert {c: ficha["frontmatter"][c] for c in ("tipo", "origen", "autor", "carreras")} == {
        c: DUROS[c] for c in ("tipo", "origen", "autor", "carreras")
    }
    assert ficha["contexto"] == DUROS["contexto"]
    assert ficha["frontmatter"]["resumen"] == RESUMEN_FIJO
    # ADR-0001 deriva la procedencia de la v1: lo escrito en Recibida no es del modelo.
    assert set(_de_la_persona(ficha).values()) == {"persona"}
    assert ficha["procedencia"]["resumen"] == "modelo"
    assert ficha["campos_de_la_persona"] == ESCRITOS
    assert cliente.get(f"/api/piezas/{pieza_id}/en-lectura").status_code == 409
    assert _guardar(cliente, pieza_id).status_code == 409
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


def test_la_v1_toma_el_contexto_de_la_persona_en_lugar_del_heredado_y_lo_demas_del_lote(
    cliente, capa_modelos
):
    lote = _crear(cliente, evento="Colación de grados", lugar="Patio central").json()["lote"]
    capa_modelos.demorar()
    pieza_id = _subir(cliente, lote["lote_id"]).json()["pieza_id"]
    abierta = cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()
    assert abierta["contexto"] == "Evento: Colación de grados\nLugar: Patio central"
    pedido = {
        "tipo": "acta",
        "fecha_inicio": lote["fecha_inicio"],
        "precision_fecha": lote["precision_fecha"],
        "origen": lote["origen"],
        "contexto": f"{abierta['contexto']}\nAparece el primer decano.",
    }
    assert _guardar(cliente, pieza_id, pedido).json()["campos_de_la_persona"] == ["tipo"]

    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert ficha["contexto"] == pedido["contexto"]
    assert (ficha["procedencia"]["tipo"], ficha["procedencia"]["origen"]) == ("persona", "lote")


def test_si_el_guardado_llega_primero_la_v1_espera_y_lo_toma(cliente, capa_modelos, url_bd):
    pieza_id = _recibida(cliente, capa_modelos)
    with psycopg.connect(url_bd) as candado:
        candado.execute("SELECT 1 FROM acervo.pieza WHERE pieza_id = %s FOR UPDATE", (pieza_id,))
        with ThreadPoolExecutor(1) as hilos:
            guardado = hilos.submit(_guardar, cliente, pieza_id)
            esperar_detras_del_candado(candado, 1)
            capa_modelos.liberar()  # La propuesta llega y espera detrás del guardado.
            esperar_detras_del_candado(candado, 2)
            candado.commit()
            assert guardado.result().status_code == 200

    esperar_estado(cliente, pieza_id, "Pendiente")
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert ficha["frontmatter"]["tipo"] == DUROS["tipo"]
    assert ficha["contexto"] == DUROS["contexto"]
    assert set(_de_la_persona(ficha).values()) == {"persona"}


def test_si_la_propuesta_llega_primero_el_guardado_rebota_y_lo_anterior_queda(
    cliente, capa_modelos, url_bd
):
    pieza_id = _recibida(cliente, capa_modelos)
    assert _guardar(cliente, pieza_id).status_code == 200
    with psycopg.connect(url_bd) as candado:
        candado.execute("SELECT 1 FROM acervo.pieza WHERE pieza_id = %s FOR UPDATE", (pieza_id,))
        capa_modelos.liberar()
        esperar_detras_del_candado(candado, 1)
        with ThreadPoolExecutor(1) as hilos:
            segundo = hilos.submit(_guardar, cliente, pieza_id, DUROS | {"origen": "Otro"})
            esperar_detras_del_candado(candado, 2)
            candado.commit()
            respuesta = segundo.result()

    # La pantalla recarga la Ficha y conserva lo escrito: nada se guarda a medias.
    assert respuesta.status_code == 409
    assert respuesta.json()["estado_actual"] == "Pendiente"
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert ficha["frontmatter"]["origen"] == DUROS["origen"]
    assert _versiones(url_bd, pieza_id) == 1


@pytest.mark.rf("RF-54")
def test_si_la_lectura_falla_lo_escrito_queda_para_completar_a_mano(cliente, capa_modelos):
    pieza_id = _recibida(cliente, capa_modelos)
    assert _guardar(cliente, pieza_id).status_code == 200
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    capa_modelos.liberar()
    esperar_error_procesamiento(cliente, pieza_id)

    assert cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano").status_code == 200

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (1, "sistema")
    assert ficha["frontmatter"]["tipo"] == DUROS["tipo"]
    assert ficha["contexto"] == DUROS["contexto"]
    assert set(_de_la_persona(ficha).values()) == {"persona"}


def test_ningun_campo_duro_lo_pre_llena_un_modelo(cliente, capa_modelos):
    pieza_id = _recibida(cliente, capa_modelos)
    abierta = cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()
    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    for vista in (abierta, ficha):
        assert all(vista["frontmatter"][c] in (None, []) for c in ESCRITOS), vista["frontmatter"]
        assert "modelo" not in _de_la_persona(vista).values()
