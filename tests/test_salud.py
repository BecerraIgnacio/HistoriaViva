"""Salud: comprobar en un segundo que el prototipo está en pie antes de la sesión con la SEU.

Sin sesión responde solo `{estado}`; el detalle (base, motores, trabajadores, cola,
interruptor) es para quien tiene OPERAR_SISTEMA (historia 233, DEC-102, DEC-106).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, MARIA, PDF

pytestmark = pytest.mark.rnf("RNF-25")

CONFIRMA_SESION = pytest.mark.confirma("la sesión la confirma la app desde su propio pool")


@pytest.fixture
def web(url_app, capa_modelos, directorio_originales) -> Iterator[TestClient]:
    """`web` sin trabajador: los latidos y la cola los pone el test."""
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )
    with TestClient(app, headers=CABECERAS) as cliente:
        yield cliente


def _sql(url_bd: str, sentencia: str, *parametros: object) -> list[tuple[object, ...]]:
    with psycopg.connect(url_bd) as conexion:
        cursor = conexion.execute(sentencia, parametros)  # type: ignore[arg-type]
        return cursor.fetchall() if cursor.description else []


@CONFIRMA_SESION
def test_con_la_base_en_pie_el_prototipo_informa_que_esta_ok(cliente):
    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud")

    assert respuesta.status_code == 200
    salud = respuesta.json()
    assert salud["estado"] == "ok"
    assert salud["base_de_datos"] == "ok"
    assert salud["version"] == "1.0.0"


def test_con_la_base_caida_el_prototipo_responde_503(capa_modelos, directorio_originales):
    app = app_sin_trabajador(
        "postgresql://nadie:nada@127.0.0.1:1/inexistente?connect_timeout=2",
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )
    with TestClient(app) as cliente:
        respuesta = cliente.get("/api/salud")

    assert respuesta.status_code == 503
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    assert respuesta.json()["type"] == "/errores/servicio-no-disponible"


@pytest.mark.historia(233)
@CONFIRMA_SESION
def test_seg_salud_sin_sesion_solo_estado(cliente):
    sin_sesion = cliente.get("/api/salud")
    otro_rol = entrar(cliente, DIRECTORA_DE_CARRERA).get("/api/salud")
    detalle = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud")

    assert (sin_sesion.status_code, sin_sesion.json()) == (200, {"estado": "ok"})
    # Spec ~4293: «sin sesión o sin el permiso: 200 {estado} y nada más» (DEC-102).
    assert (otro_rol.status_code, otro_rol.json()) == (200, {"estado": "ok"})
    assert detalle.status_code == 200
    assert set(detalle.json()) == {
        "estado",
        "base_de_datos",
        "capa_modelos",
        "modelos_faltantes",
        "video",
        "procesamiento",
        "api_agentes",
        "trabajadores",
        "cola",
        "indice",
        "drive",
        "version",
    }


@pytest.mark.historia(233)
@CONFIRMA_SESION
def test_un_trabajador_sin_latido_hace_60_s_figura_caido_y_la_salud_queda_degradada(
    web, url_bd
):
    _sql(
        url_bd,
        "INSERT INTO acervo.latido_trabajador (trabajador, tipos, latido_el) VALUES"
        " ('el-que-murio', '{procesamiento}', now() - interval '61 seconds'),"
        " ('el-que-late', '{indexacion}', now())",
    )

    sin_sesion = web.get("/api/salud").json()
    salud = entrar(web, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert sin_sesion == {"estado": "degradado"}
    assert {tipo: t["vivo"] for tipo, t in salud["trabajadores"].items()} == {
        "procesamiento": False,
        "indexacion": True,
    }
    [(ultimo,)] = _sql(
        url_bd,
        "SELECT latido_el FROM acervo.latido_trabajador WHERE trabajador = 'el-que-murio'",
    )
    assert datetime.fromisoformat(salud["trabajadores"]["procesamiento"]["latido_el"]) == ultimo


@pytest.mark.historia(233)
@CONFIRMA_SESION
def test_la_salud_muestra_la_cola_y_el_interruptor(web, url_bd):
    entrar(web, MARIA)
    primera, segunda, tercera = (
        web.post(
            "/api/piezas", files={"archivo": (f"acta{n}.pdf", PDF + bytes([n]), "application/pdf")}
        ).json()["pieza_id"]
        for n in range(3)
    )
    _sql(
        url_bd,
        "UPDATE acervo.trabajo_procesamiento SET estado = 'fallido', error = 'x',"
        " terminado_el = now() WHERE pieza_id = %s",
        segunda,
    )
    # Una descripción fallida no cuenta: `reprocesar --fallidos` no la toma (148).
    _sql(
        url_bd,
        "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, imagen, estado, error,"
        " terminado_el, configuracion_efectiva, tiempo_maximo_ms)"
        " SELECT pieza_id, 'descripcion', 1, 'fallido', 'x', now(), configuracion_efectiva,"
        " tiempo_maximo_ms FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
        segunda,
    )
    _sql(
        url_bd,
        "UPDATE acervo.trabajo_procesamiento SET estado = 'en_curso', tomado_por = 'otro',"
        " iniciado_el = now(), arriendo_vence_el = now() + interval '1 hour'"
        " WHERE pieza_id = %s",
        tercera,
    )
    [(encolado_el,)] = _sql(
        url_bd,
        "SELECT encolado_el FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
        primera,
    )

    salud = entrar(web, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    cola = salud["cola"]
    assert datetime.fromisoformat(cola.pop("mas_viejo_el")) == encolado_el
    assert cola == {"encolados": 1, "en_curso": 1, "fallidos": 1}
    # Como los deja la migración: el procesamiento andando y la API de Agentes apagada (DEC-136).
    assert (salud["procesamiento"], salud["api_agentes"]) == ("habilitado", "pausada")
    # Sin trabajador no hay latidos: no hay de qué tipo decir que está caído.
    assert salud["trabajadores"] == {}


@pytest.mark.historia(233)
@CONFIRMA_SESION
def test_un_tipo_esperado_cuyo_trabajador_nunca_arranco_figura_caido(
    url_app, capa_modelos, directorio_originales
):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        tipos_esperados=("procesamiento",),
    )
    with TestClient(app, headers=CABECERAS) as cliente:
        sin_sesion = cliente.get("/api/salud").json()
        salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert sin_sesion == {"estado": "degradado"}
    assert salud["trabajadores"] == {"procesamiento": {"vivo": False, "latido_el": None}}


@pytest.mark.historia(233)
@CONFIRMA_SESION
def test_seg_salud_sin_sesion_no_le_pega_al_motor_en_cada_pedido(web):
    """SEG-08 (165): una ráfaga anónima reusa el `{estado}` 5 s; el detalle se calcula siempre."""
    estado = web.app.state  # type: ignore[attr-defined]
    vigente, preguntas = estado.capa_vigente, []
    estado.capa_vigente = lambda: preguntas.append(1) or vigente()

    anonimas = [web.get("/api/salud") for _ in range(20)]
    detalle = entrar(web, ADMINISTRADOR_TECNICO).get("/api/salud")

    assert {(r.status_code, r.text) for r in anonimas} == {(200, anonimas[0].text)}
    assert set(anonimas[0].json()) == {"estado"}
    assert "capa_modelos" in detalle.json()
    assert len(preguntas) == 2
