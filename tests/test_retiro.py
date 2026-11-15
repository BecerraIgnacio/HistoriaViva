"""Retirar una Pieza con motivo (105; spec, módulo 18 `retiro`; RF-29, CU-17, DEC-29).

El RGC, o el Administrador técnico durante el interinato, la retira: en una transacción se
bloquea, se desindexa, se cancelan sus trabajos `encolado` y `en_curso` (DEC-146 punto 1), se
marca con quién, cuándo y por qué, y se audita `pieza.retiro`. Desaparece de todo lo que se lee
y sigue en la base. HTTP contra Postgres real; el único doble es la capa.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from aplicacion import entrar
from espera import esperar_estado
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from historia_viva.dominio.errores import PiezaNoEncontrada
from historia_viva.interruptor import Freno, cambiar
from historia_viva.procesamiento.cola import Cola
from test_api_agentes import agente, emitir, encender  # noqa: F401 — fixtures de /v1
from test_busqueda import (  # noqa: F401 — fixtures de Piezas Validadas e indexadas
    EGRESADOS,
    LABORATORIO,
    bd,
    indexacion,
    trabajador,
    validada,
)
from test_conversacion import nueva, turno
from test_interruptor import TOMAS
from test_lotes import _crear as crear_lote
from test_lotes import _llenar as llenar_lote
from test_lotes import _subir as subir_al_lote
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA, PDF

pytestmark = [
    pytest.mark.rf("RF-29"),
    pytest.mark.historia(157),
    pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools"),
]

MOTIVO = "Se subió por error: es de otra institución."


def retirar(cliente, pieza_id: str, persona: str = MARIA, **cuerpo: Any):
    entrar(cliente, persona)
    return cliente.post(
        f"/api/admin/piezas/{pieza_id}/retiro", json={"motivo": MOTIVO} | cuerpo
    )


def fragmentos_de(url_bd: str, pieza_id: str) -> int:
    with psycopg.connect(url_bd) as conexion:
        (total,) = conexion.execute(
            "SELECT count(*) FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = %s",
            (pieza_id,),
        ).fetchone() or (0,)
    return int(total)


def marca_de(url_bd: str, pieza_id: str) -> tuple[Any, ...] | None:
    with psycopg.connect(url_bd) as conexion:
        return conexion.execute(
            "SELECT estado, borrada_por::text, borrada_motivo, borrada_el IS NOT NULL"
            " FROM acervo.pieza WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()


@pytest.mark.historia(133, 144)
@pytest.mark.xdist_group("indice")
def test_una_validada_indexada_retirada_desaparece_de_todo_y_sigue_en_la_base(
    cliente, validada, agente, url_bd
):
    retirada = validada(LABORATORIO)
    otra = validada(EGRESADOS)
    assert fragmentos_de(url_bd, retirada) > 0

    respuesta = retirar(cliente, retirada)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert (cuerpo["pieza_id"], cuerpo["borrada_por"]) == (retirada, MARIA)
    assert cuerpo["borrada_el"]
    assert [p["pieza_id"] for p in cliente.get("/api/piezas").json()["piezas"]] == [otra]
    assert cliente.get(f"/api/piezas/{retirada}").status_code == 404
    assert cliente.get(f"/api/piezas/{retirada}/ficha").status_code == 404
    assert cliente.get(f"/api/piezas/{retirada}/original").status_code == 404
    _, eventos = turno(cliente, nueva(cliente), "laboratorio de ensayos de materiales")
    assert [f["pieza_id"] for f in dict(eventos)["fuentes"]["fichas"]] == [otra]
    v1 = cliente.get("/v1/fragmentos", params={"desde": "1900-01-01"}, headers=agente)
    assert {f["pieza_id"] for f in v1.json()["fragmentos"]} == {f"pza_{otra}"}
    assert fragmentos_de(url_bd, retirada) == 0
    assert marca_de(url_bd, retirada) == ("Validada", MARIA, MOTIVO, True)


def trabajos_de(url_bd: str, pieza_id: str) -> list[str]:
    with psycopg.connect(url_bd) as conexion:
        filas = conexion.execute(
            "SELECT estado FROM acervo.trabajo_procesamiento WHERE pieza_id = %s"
            " ORDER BY encolado_el, trabajo_id",
            (pieza_id,),
        ).fetchall()
    return [estado for (estado,) in filas]


def subir(cliente, nombre: str = "acta.pdf", contenido: bytes = PDF) -> str:
    entrar(cliente, MARIA)
    respuesta = cliente.post(
        "/api/piezas", files={"archivo": (nombre, contenido, "application/pdf")}
    )
    assert respuesta.status_code == 201, respuesta.text
    pieza_id: str = respuesta.json()["pieza_id"]
    return pieza_id


# DEC-146 punto 1 y ADR-0004: con el procesamiento frenado, el trabajo queda `encolado` y el
# retiro sigue funcionando (lo que se frena es producir).
def test_con_el_procesamiento_frenado_un_trabajo_encolado_queda_cancelado(cliente, url_app, url_bd):
    with psycopg.connect(url_app) as conexion:
        cambiar(conexion, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Prueba del retiro")
    pieza_id = subir(cliente)
    assert trabajos_de(url_bd, pieza_id) == ["encolado"]

    respuesta = retirar(cliente, pieza_id)

    assert respuesta.status_code == 200, respuesta.text
    assert trabajos_de(url_bd, pieza_id) == ["cancelado"]
    assert marca_de(url_bd, pieza_id) == ("Recibida", MARIA, MOTIVO, True)
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RETIRO)
    assert evento.antes == {"estado": "Recibida", "retirada": False}
    assert evento.despues == {"retirada": True, "trabajos_cancelados": 1}
    assert (evento.objeto.id, evento.motivo) == (pieza_id, MOTIVO)


def test_con_un_trabajo_en_curso_su_cierre_se_rechaza_y_la_ficha_no_cambia(
    cliente, capa_modelos, url_bd
):
    capa_modelos.demorar()
    pieza_id = subir(cliente)
    limite = time.monotonic() + 10
    while trabajos_de(url_bd, pieza_id) != ["en_curso"]:
        assert time.monotonic() < limite, trabajos_de(url_bd, pieza_id)
        time.sleep(0.02)

    respuesta = retirar(cliente, pieza_id)
    capa_modelos.liberar()
    # El trabajador es uno solo y va en orden: cuando la siguiente llega a Pendiente, ya
    # intentó cerrar el trabajo de la retirada.
    esperar_estado(cliente, subir(cliente, "otra.pdf", PDF + b"%otra\n"), "Pendiente")

    assert respuesta.status_code == 200, respuesta.text
    assert trabajos_de(url_bd, pieza_id) == ["cancelado"]
    assert marca_de(url_bd, pieza_id) == ("Recibida", MARIA, MOTIVO, True)
    with psycopg.connect(url_bd) as conexion:
        assert conexion.execute(
            "SELECT count(*) FROM acervo.ficha WHERE pieza_id = %s", (pieza_id,)
        ).fetchone() == (0,)


@pytest.mark.xdist_group("indice")
def test_con_una_indexacion_en_curso_la_retirada_queda_sin_fragmentos(
    cliente, validada, trabajador, capa_modelos, url_bd
):
    # Validar encoló la indexación (047); el trabajador la toma y queda colgado en `embeber`.
    pieza_id = validada(LABORATORIO, indexar=False)
    capa_modelos.demorar()
    indexando = threading.Thread(target=trabajador.procesar_siguiente)
    indexando.start()
    limite = time.monotonic() + 10
    while trabajos_de(url_bd, pieza_id) != ["completado", "en_curso"]:
        assert time.monotonic() < limite, trabajos_de(url_bd, pieza_id)
        time.sleep(0.02)

    respuesta = retirar(cliente, pieza_id)
    capa_modelos.liberar()
    indexando.join(10)

    assert respuesta.status_code == 200, respuesta.text
    assert not indexando.is_alive()
    assert fragmentos_de(url_bd, pieza_id) == 0
    assert sorted(trabajos_de(url_bd, pieza_id)) == ["cancelado", "completado"]


@pytest.mark.xdist_group("indice")
def test_una_falla_al_cancelar_revierte_todo(cliente, validada, url_bd, monkeypatch):
    pieza_id = validada(LABORATORIO)
    fragmentos = fragmentos_de(url_bd, pieza_id)

    def fallar(self, tx, pieza_id):
        raise RuntimeError("falla inyectada al cancelar")

    monkeypatch.setattr(Cola, "cancelar_de_pieza", fallar)
    with pytest.raises(RuntimeError, match="falla inyectada"):
        retirar(cliente, pieza_id)

    assert fragmentos_de(url_bd, pieza_id) == fragmentos > 0
    assert marca_de(url_bd, pieza_id) == ("Validada", None, None, False)
    assert cliente.get(f"/api/piezas/{pieza_id}").status_code == 200
    assert RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RETIRO) == []


@pytest.mark.historia(146)
@pytest.mark.parametrize("persona", [JORGE, DIRECTORA_DE_CARRERA], ids=["coordinador", "director"])
def test_sin_retirar_piezas_responde_403_y_no_cambia_nada(cliente, pieza_pendiente, url_bd, persona):
    respuesta = retirar(cliente, pieza_pendiente, persona)

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"
    assert marca_de(url_bd, pieza_pendiente) == ("Pendiente", None, None, False)


def test_el_tecnico_interino_retira_y_la_accion_queda_marcada_interino(
    cliente, pieza_pendiente, url_bd, sin_rgc
):
    respuesta = retirar(cliente, pieza_pendiente, ADMINISTRADOR_TECNICO)

    assert respuesta.status_code == 200, respuesta.text
    assert marca_de(url_bd, pieza_pendiente) == ("Pendiente", ADMINISTRADOR_TECNICO, MOTIVO, True)
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RETIRO)
    assert str(evento.actor.usuario_id) == ADMINISTRADOR_TECNICO
    assert evento.antes == {"estado": "Pendiente", "retirada": False}
    assert evento.despues == {"retirada": True, "trabajos_cancelados": 0, "interino": True}


def test_el_rgc_retira_sin_la_marca_interino(cliente, pieza_pendiente):
    assert retirar(cliente, pieza_pendiente).status_code == 200

    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RETIRO)
    assert "interino" not in evento.despues
    # Tampoco se lee su Ficha por el módulo: ni la vigente ni la propuesta.
    fichas = cliente.app.state.fichas
    for leer in (fichas.obtener_ficha, fichas.obtener_propuesta):
        with pytest.raises(PiezaNoEncontrada):
            leer(UUID(pieza_pendiente))


@pytest.mark.parametrize(
    "cuerpo",
    [{}, {"motivo": "  ab  "}, {"motivo": "x" * 501}],
    ids=["sin_motivo", "corto", "largo"],
)
def test_sin_un_motivo_de_5_a_500_caracteres_responde_422(cliente, pieza_pendiente, url_bd, cuerpo):
    entrar(cliente, MARIA)
    respuesta = cliente.post(f"/api/admin/piezas/{pieza_pendiente}/retiro", json=cuerpo)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (
        "/errores/pedido-invalido", "motivo"
    )
    assert marca_de(url_bd, pieza_pendiente) == ("Pendiente", None, None, False)


def test_retirar_dos_veces_responde_409_sin_segundo_cambio(cliente, pieza_pendiente, url_bd):
    assert retirar(cliente, pieza_pendiente).status_code == 200
    antes = marca_de(url_bd, pieza_pendiente)

    respuesta = retirar(cliente, pieza_pendiente, motivo="Otro motivo distinto")

    assert respuesta.status_code == 409
    assert respuesta.json()["type"] == "/errores/transicion-invalida"
    assert marca_de(url_bd, pieza_pendiente) == antes
    assert len(RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RETIRO)) == 1


def test_una_pieza_inexistente_responde_404(cliente):
    respuesta = retirar(cliente, str(uuid4()))

    assert respuesta.status_code == 404


# Decisión 12 de la auditoría: la retirada no cuenta en el tope de su Lote. El progreso del
# Lote llega con 084 «detalle del Lote» y cuenta lo mismo que el tope (`contar_del_lote`).
@pytest.mark.historia(75)
def test_una_pieza_retirada_libera_su_lugar_en_el_lote(cliente, url_bd):
    lote_id = crear_lote(cliente).json()["lote"]["lote_id"]
    llenar_lote(url_bd, lote_id, 100)
    assert subir_al_lote(cliente, lote_id).status_code == 422
    with psycopg.connect(url_bd) as conexion:
        (una,) = conexion.execute(
            "SELECT pieza_id::text FROM acervo.pieza WHERE lote_id = %s LIMIT 1", (lote_id,)
        ).fetchone()

    assert retirar(cliente, una).status_code == 200
    assert subir_al_lote(cliente, lote_id).status_code == 201


def test_hv_app_no_borra_piezas_y_solo_borra_del_indice(url_app):
    with psycopg.connect(url_app) as conexion:
        conexion.execute("DELETE FROM acervo.data_fragmento WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conexion.execute("DELETE FROM acervo.pieza WHERE false")


def test_ninguna_ruta_publicada_usa_el_verbo_delete(cliente):
    metodos = {m for ruta in cliente.app.openapi()["paths"].values() for m in ruta}

    assert "delete" not in metodos
