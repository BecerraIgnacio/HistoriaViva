"""Completar a mano una Recibida fallida (DEC-122, DEC-146 punto 1).

Un fallo determinista no traba la Pieza: quien la subió o el RGC la pasa a `Pendiente` con la
Ficha v1 `sistema` vacía, la marca «Requiere análisis humano», y la valida una persona.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from espera import esperar_detras_del_candado, esperar_error_procesamiento

from aplicacion import entrar
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from historia_viva.dominio.errores import LimiteExcedido
from test_fichas import divergencias
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA, PDF

pytestmark = [
    pytest.mark.rf("RF-54"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


def _recibida_fallida(cliente, capa_modelos, persona=JORGE, nombre="acta.pdf") -> str:
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    entrar(cliente, persona)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": (nombre, PDF, "application/pdf")}
    ).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)
    return pieza_id


def _trabajos(url_bd: str, pieza_id: str) -> list[tuple[str, str | None]]:
    with psycopg.connect(url_bd) as conexion:
        return conexion.execute(
            "SELECT estado, error FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchall()


# 105 y 292: completada, deja de mostrarse entre las fallidas de Mis Piezas.
@pytest.mark.historia(291, 105, 292)
def test_quien_la_subio_completa_a_mano_una_recibida_fallida(cliente, capa_modelos, url_bd):
    pieza_id = _recibida_fallida(cliente, capa_modelos)
    trabajos_antes = _trabajos(url_bd, pieza_id)

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano")

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "pieza_id": pieza_id,
        "estado": "Pendiente",
        "requiere_analisis_humano": True,
        "version": 1,
        "origen_version": "sistema",
    }
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["origen_version"], ficha["cuerpo"], ficha["contexto"]) == (
        1, "sistema", "", ""
    )
    assert ficha["requiere_analisis_humano"] is True
    # El trabajo fallido queda como historial, y ya no cuenta como fallido.
    assert _trabajos(url_bd, pieza_id) == trabajos_antes
    detalle = cliente.get(f"/api/piezas/{pieza_id}").json()
    assert (detalle["error_procesamiento"], detalle["estado_procesamiento"]) == (None, None)
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.PIEZA_COMPLETAR_A_MANO
    )
    assert (str(evento.actor.usuario_id), evento.objeto.id) == (JORGE, pieza_id)
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


def _sin_cambios(cliente, url_bd, pieza_id, trabajos):
    assert cliente.get(f"/api/piezas/{pieza_id}").json()["estado"] == "Recibida"
    assert cliente.get(f"/api/piezas/{pieza_id}/ficha").status_code == 404
    assert _trabajos(url_bd, pieza_id) == trabajos
    assert RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.PIEZA_COMPLETAR_A_MANO
    ) == []


@pytest.mark.historia(291)
def test_con_un_procesamiento_vivo_despues_del_fallo_se_rechaza(cliente, capa_modelos, url_bd):
    pieza_id = _recibida_fallida(cliente, capa_modelos)
    capa_modelos.arreglar()
    capa_modelos.demorar()
    assert cliente.post(f"/api/piezas/{pieza_id}/reproceso").status_code == 202
    limite = time.monotonic() + 10
    while ("en_curso", None) not in (trabajos := _trabajos(url_bd, pieza_id)):
        assert time.monotonic() < limite, trabajos  # colgado en la capa: no se mueve más
        time.sleep(0.02)

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano")

    assert respuesta.status_code == 409
    assert respuesta.json()["type"] == "/errores/transicion-invalida"
    assert respuesta.json()["estado_actual"] == "Recibida"
    _sin_cambios(cliente, url_bd, pieza_id, trabajos)
    capa_modelos.liberar()


@pytest.mark.historia(291)
def test_una_pieza_que_no_es_recibida_no_se_completa_a_mano(cliente, pieza_pendiente):
    respuesta = cliente.post(f"/api/piezas/{pieza_pendiente}/completar-a-mano")

    assert respuesta.status_code == 409
    assert respuesta.json()["estado_actual"] == "Pendiente"
    assert cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()["origen_version"] == "modelo"


@pytest.mark.historia(291)
def test_el_rgc_completa_a_mano_la_pieza_que_subio_otro(cliente, capa_modelos):
    pieza_id = _recibida_fallida(cliente, capa_modelos, persona=JORGE)

    respuesta = entrar(cliente, MARIA).post(f"/api/piezas/{pieza_id}/completar-a-mano")

    assert respuesta.status_code == 200


@pytest.mark.historia(291)
@pytest.mark.parametrize(
    "persona",
    [JORGE, DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO],
    ids=["coordinador_que_no_la_subio", "director_carrera", "admin_tecnico"],
)
def test_quien_no_la_subio_y_no_es_rgc_recibe_403(cliente, capa_modelos, url_bd, persona):
    pieza_id = _recibida_fallida(cliente, capa_modelos, persona=MARIA)
    trabajos = _trabajos(url_bd, pieza_id)

    respuesta = entrar(cliente, persona).post(f"/api/piezas/{pieza_id}/completar-a-mano")

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"
    _sin_cambios(entrar(cliente, MARIA), url_bd, pieza_id, trabajos)


@pytest.mark.historia(291)
def test_la_v1_de_una_pieza_de_lote_lleva_el_contexto_y_lo_heredado(cliente, capa_modelos, url_bd):
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    lote = entrar(cliente, JORGE).post(
        "/api/lotes",
        json={
            "nombre": "Colaciones 1970-1979",
            "origen": "Archivo fotográfico SEU",
            "fecha_inicio": "1970-01-01",
            "precision_fecha": "decada",
            "fecha_fin": "1979-12-31",
            "evento": "Colación de grados",
            "lugar": "Patio central",
        },
    ).json()["lote"]
    pieza_id = cliente.post(
        "/api/piezas",
        data={"lote_id": lote["lote_id"]},
        files={"archivo": ("acta.pdf", PDF, "application/pdf")},
    ).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)

    assert cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano").status_code == 200

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["cuerpo"], ficha["contexto"]) == (
        "", "Evento: Colación de grados\nLugar: Patio central"
    )
    assert (ficha["frontmatter"]["origen"], ficha["frontmatter"]["lote"]) == (
        "Archivo fotográfico SEU", lote["lote_id"]
    )
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


@pytest.mark.historia(291)
def test_si_algo_falla_a_mitad_no_queda_nada_a_medias(
    cliente, capa_modelos, url_bd, monkeypatch
):
    pieza_id = _recibida_fallida(cliente, capa_modelos)
    trabajos = _trabajos(url_bd, pieza_id)

    def _auditoria_caida(*_args, **_kwargs):
        raise RuntimeError("la auditoría no responde")

    monkeypatch.setattr(cliente.app.state.auditoria, "registrar", _auditoria_caida)
    with pytest.raises(RuntimeError):
        cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano")
    monkeypatch.undo()

    _sin_cambios(cliente, url_bd, pieza_id, trabajos)



@pytest.mark.historia(291)
def test_un_reproceso_que_encola_mientras_tanto_gana_y_no_se_completa(
    cliente, capa_modelos, url_bd
):
    # Un reproceso de 033 bloquea la Pieza y encola; completar a mano espera ese bloqueo y
    # recién después mira la cola: ve el trabajo vivo y responde 409.
    pieza_id = _recibida_fallida(cliente, capa_modelos)
    capa_modelos.arreglar()
    capa_modelos.demorar()
    bloqueada = threading.Event()

    def reprocesar_a_la_vez():
        with psycopg.connect(url_bd) as conexion:
            conexion.execute(
                "SELECT 1 FROM acervo.pieza WHERE pieza_id = %s FOR UPDATE", (pieza_id,)
            )
            conexion.execute(
                "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, tiempo_maximo_ms,"
                " configuracion_efectiva, contexto_lote) SELECT pieza_id, tipo, tiempo_maximo_ms,"
                " configuracion_efectiva, contexto_lote FROM acervo.trabajo_procesamiento"
                " WHERE pieza_id = %s",
                (pieza_id,),
            )
            bloqueada.set()
            # Suelta el bloqueo recién cuando completar a mano lo espera: con un `sleep`, un POST
            # lento llegaba después del COMMIT y el test pasaba sin probar la espera.
            esperar_detras_del_candado(conexion, 1)

    hilo = threading.Thread(target=reprocesar_a_la_vez)
    hilo.start()
    assert bloqueada.wait(10)

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano")
    hilo.join()

    assert respuesta.status_code == 409
    assert respuesta.json()["estado_actual"] == "Recibida"
    assert cliente.get(f"/api/piezas/{pieza_id}/ficha").status_code == 404
    capa_modelos.liberar()
