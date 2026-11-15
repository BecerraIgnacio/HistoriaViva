"""Fallos del procesamiento, reintento y modo sin modelos (issue 11).

Un fallo de procesamiento no es un estado: la Pieza sigue `Recibida`, con el porqué en
`error_procesamiento` (`spec.md` §3, contrato §8). Desde ahí se reintenta con
`POST /api/piezas/{id}/reproceso`. Si el motor no responde, `GET /api/salud` avisa sin que
el resto de la aplicación deje de funcionar (ADR-0004, interruptor global).
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from espera import esperar_error_procesamiento, esperar_estado

from historia_viva.dominio.errores import LimiteExcedido
from aplicacion import entrar
from validacion import ADMINISTRADOR_TECNICO

pytestmark = [
    pytest.mark.rf("RF-43"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

MARIA = "11111111-1111-4111-8111-111111111111"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def _subir(cliente, nombre="acta-fundacion.pdf", contenido=PDF):
    entrar(cliente, MARIA)
    return cliente.post("/api/piezas", files={"archivo": (nombre, contenido, "application/pdf")})


def _limite_excedido() -> LimiteExcedido:
    return LimiteExcedido("transcribir", "tiempo", 300, 301)


def _fila_del_trabajo(url_bd: str, pieza_id: str) -> tuple[str, str | None, int]:
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT estado, error, generaciones FROM acervo.trabajo_procesamiento"
            " WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
    assert fila is not None
    return fila


def test_un_limite_excedido_deja_la_pieza_recibida_con_el_error_escrito(
    cliente, capa_modelos, url_bd
):
    capa_modelos.romper(_limite_excedido())

    pieza_id = _subir(cliente).json()["pieza_id"]

    detalle = esperar_error_procesamiento(cliente, pieza_id)
    assert detalle["estado"] == "Recibida"
    assert "transcribir" in detalle["error_procesamiento"]
    # Ninguna Ficha se llegó a escribir: un trabajo fallido no genera contenido a medias.
    assert cliente.get(f"/api/piezas/{pieza_id}/ficha").status_code == 404

    estado, error, _ = _fila_del_trabajo(url_bd, pieza_id)
    assert estado == "fallido"
    assert error is not None


def test_reprocesar_desde_recibida_reencola_y_responde_202(cliente, capa_modelos):
    capa_modelos.romper(_limite_excedido())
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)
    capa_modelos.arreglar()

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 202
    cuerpo = respuesta.json()
    assert cuerpo["pieza_id"] == pieza_id
    assert cuerpo["estado"] == "Recibida"
    detalle = esperar_estado(cliente, pieza_id, "Pendiente")
    # El reintento borra el error del intento anterior: ya no queda nada que reintentar.
    assert detalle["error_procesamiento"] is None


def test_reprocesar_una_pieza_pendiente_responde_409_y_no_genera_contenido_nuevo(
    cliente, url_bd
):
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 409
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    assert respuesta.json()["type"] == "/errores/transicion-invalida"
    with psycopg.connect(url_bd) as conexion:
        (cantidad,) = conexion.execute(
            "SELECT count(*) FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
    assert cantidad == 1


def test_reprocesar_una_pieza_incompleta_o_validada_tambien_responde_409(cliente):
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    cliente.put(
        f"/api/piezas/{pieza_id}/ficha",
        json={"cuerpo": "cuerpo corregido"},
    )

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 409


def test_reprocesar_mientras_el_trabajo_original_sigue_activo_responde_409(
    cliente, capa_modelos
):
    # La Pieza está `Recibida`, pero su trabajo ya está `en_curso`: el chequeo de estado
    # solo no alcanza para evitar un segundo trabajo activo para la misma Pieza.
    capa_modelos.demorar()
    pieza_id = _subir(cliente).json()["pieza_id"]

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 409
    assert respuesta.json()["type"] == "/errores/transicion-invalida"


def test_reprocesar_una_pieza_que_no_existe_responde_404(cliente):
    entrar(cliente, MARIA)

    respuesta = cliente.post("/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/reproceso")

    assert respuesta.status_code == 404


def test_un_trabajo_colgado_por_un_reinicio_se_retoma_solo_al_vencer_su_arriendo(
    cliente, capa_modelos, url_bd
):
    # El primer intento falla y deja la Pieza Recibida: ningún hilo sigue tocando su
    # trabajo, así que a partir de acá la fila se puede mover con SQL sin pisarle nada
    # a un procesamiento real en curso.
    capa_modelos.romper(_limite_excedido())
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)
    capa_modelos.arreglar()

    # Simula un trabajo que un proceso tomó y que murió antes de terminarlo: queda
    # `en_curso` con su arriendo ya vencido, y nadie más que la base lo sabe.
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.trabajo_procesamiento SET estado = 'en_curso',"
            " tomado_por = 'web-fantasma', iniciado_el = now(),"
            " arriendo_vence_el = now() - interval '1 second' WHERE pieza_id = %s",
            (pieza_id,),
        )

    # Se retoma solo: nadie llama a un endpoint de recuperación, el sondeo normal del
    # trabajador lo reclama en su próxima vuelta.
    esperar_estado(cliente, pieza_id, "Pendiente")

    _, _, generaciones = _fila_del_trabajo(url_bd, pieza_id)
    # Una al primer intento y otra al retomarlo: recuperar el arriendo no suma (DEC-146 punto 2).
    assert generaciones == 2


def test_un_trabajo_que_agoto_su_tope_de_generaciones_queda_fallido_sin_reintentar_solo(
    cliente, capa_modelos, url_bd
):
    capa_modelos.romper(_limite_excedido())
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)
    capa_modelos.arreglar()

    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.trabajo_procesamiento SET estado = 'en_curso',"
            " tomado_por = 'web-fantasma', iniciado_el = now(), generaciones = tope_generaciones,"
            " arriendo_vence_el = now() - interval '1 second' WHERE pieza_id = %s",
            (pieza_id,),
        )

    detalle = esperar_error_procesamiento(cliente, pieza_id)
    assert detalle["estado"] == "Recibida"

    estado, error, generaciones = _fila_del_trabajo(url_bd, pieza_id)
    assert estado == "fallido"
    assert error is not None
    assert generaciones == 2


def test_tomar_un_trabajo_escribe_un_arriendo_con_margen_sobre_su_tope(cliente, url_bd):
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")

    with psycopg.connect(url_bd) as conexion:
        tiempo_maximo_ms, iniciado_el, arriendo_vence_el = conexion.execute(
            "SELECT tiempo_maximo_ms, iniciado_el, arriendo_vence_el"
            " FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
    assert arriendo_vence_el is not None
    assert arriendo_vence_el - iniciado_el >= timedelta(milliseconds=tiempo_maximo_ms)


def test_salud_ok_cuando_el_motor_de_modelos_responde(cliente):
    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud")

    assert respuesta.status_code == 200
    salud = respuesta.json()
    assert salud["estado"] == "ok"
    assert salud["capa_modelos"] == "ok"


def test_salud_degradada_cuando_el_motor_de_modelos_no_responde(cliente, capa_modelos):
    capa_modelos.salud_ok = False

    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud")

    assert respuesta.status_code == 200
    salud = respuesta.json()
    assert salud["estado"] == "degradado"
    assert salud["base_de_datos"] == "ok"
    assert salud["capa_modelos"] == "sin_respuesta"
