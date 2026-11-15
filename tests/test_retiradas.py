"""Vista de Retiradas y Restaurar (106; spec, módulo 18 `retiro`; DEC-138, R-27, R-43 d).

La pestaña Retiradas de Administración (P7e) lista las Piezas retiradas con su motivo, quién y
cuándo, y el conflicto que impediría restaurarlas. Restaurar devuelve la Pieza al Acervo: una
Validada se reindexa, una Recibida encola un procesamiento nuevo y una Pendiente o Incompleta
vuelve como estaba. HTTP contra Postgres real; el único doble es la capa.
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from aplicacion import entrar
from espera import esperar_estado
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from historia_viva.dominio.errores import PiezaDuplicada
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.interruptor import Freno, cambiar
from test_busqueda import (  # noqa: F401 — fixtures de Piezas Validadas e indexadas
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
from test_retiro import MOTIVO, retirar, trabajos_de
from test_validacion import MARIA_FIRMANDO
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA, PDF

pytestmark = [
    pytest.mark.historia(158, 294, 295),
    pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools"),
]


def subir(cliente, contenido: bytes, nombre: str = "acta.pdf") -> str:
    entrar(cliente, MARIA)
    respuesta = cliente.post(
        "/api/piezas", files={"archivo": (nombre, contenido, "application/pdf")}
    )
    assert respuesta.status_code == 201, respuesta.text
    pieza_id: str = respuesta.json()["pieza_id"]
    return pieza_id


def retiradas(cliente, persona: str = MARIA, **parametros: Any):
    entrar(cliente, persona)
    return cliente.get("/api/admin/retiradas", params=parametros)


def test_la_lista_trae_motivo_quien_cuando_y_el_conflicto_de_restauracion(cliente, url_bd):
    # Sin conflicto: una suelta cuyo archivo no volvió a subirse.
    libre = subir(cliente, PDF + b"%libre\n", "libre.pdf")
    # `pieza-duplicada`: después de retirarla, el mismo archivo volvió a entrar.
    duplicada = subir(cliente, PDF + b"%dup\n", "desfile-1975.pdf")
    # `lote-lleno`: su Lote se llenó mientras estaba retirada.
    lote_id = crear_lote(cliente).json()["lote"]["lote_id"]
    del_lote = subir_al_lote(cliente, lote_id, "del-lote.pdf", PDF + b"%lote\n").json()["pieza_id"]
    for pieza_id in (libre, duplicada, del_lote):
        assert retirar(cliente, pieza_id).status_code == 200
    otra = subir(cliente, PDF + b"%dup\n", "desfile-1975.pdf")
    llenar_lote(url_bd, lote_id, 100)

    respuesta = retiradas(cliente)

    assert respuesta.status_code == 200, respuesta.text
    filas = respuesta.json()["retiradas"]
    # De la más reciente retirada a la más antigua.
    assert [f["pieza_id"] for f in filas] == [del_lote, duplicada, libre]
    assert {f["pieza_id"]: f["conflicto_de_restauracion"] for f in filas} == {
        del_lote: "lote-lleno", duplicada: "pieza-duplicada", libre: None
    }
    fila = filas[1]
    assert (fila["nombre_archivo"], fila["motivo"], fila["retirada_por"]) == (
        "desfile-1975.pdf", MOTIVO, "Maria Fernanda Rossi"
    )
    assert fila["estado"] in {"Recibida", "Pendiente"}
    assert fila["retirada_el"] and fila["lote_id"] is None
    assert filas[0]["lote_id"] == lote_id
    assert respuesta.json()["siguiente"] is None
    assert otra != duplicada


def test_la_lista_pagina_con_cursor(cliente):
    ids = [subir(cliente, PDF + f"%{n}\n".encode(), f"p{n}.pdf") for n in range(3)]
    for pieza_id in ids:
        assert retirar(cliente, pieza_id).status_code == 200

    primera = retiradas(cliente, limite=2).json()
    segunda = retiradas(cliente, limite=2, cursor=primera["siguiente"]).json()

    assert [f["pieza_id"] for f in primera["retiradas"]] == ids[:0:-1]
    assert [f["pieza_id"] for f in segunda["retiradas"]] == ids[:1]
    assert segunda["siguiente"] is None
    assert retiradas(cliente, cursor="no-es-un-cursor").json()["campo"] == "cursor"


@pytest.mark.parametrize("persona", [JORGE, DIRECTORA_DE_CARRERA], ids=["coordinador", "director"])
def test_la_lista_sin_retirar_piezas_responde_403(cliente, persona):
    respuesta = retiradas(cliente, persona)

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"


def restaurar(cliente, pieza_id: str, persona: str = MARIA, **cuerpo: Any):
    entrar(cliente, persona)
    return cliente.post(
        f"/api/admin/piezas/{pieza_id}/restauracion",
        json={"motivo": "Se retiró por error."} | cuerpo,
    )


def retirada(url_bd: str, pieza_id: str) -> bool:
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT borrada_el IS NOT NULL FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
        ).fetchone()
    assert fila is not None
    return bool(fila[0])


def restauraciones(cliente) -> list[Any]:
    return RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RESTAURACION)


def sin_hora(cuerpo: dict[str, Any]) -> dict[str, Any]:
    assert cuerpo.pop("restaurada_el")
    return cuerpo


@pytest.mark.historia(296)
@pytest.mark.xdist_group("indice")
def test_una_validada_restaurada_figura_no_indexada_hasta_que_corre_la_reindexacion(
    cliente, validada, trabajador
):
    pieza_id = validada(LABORATORIO)
    assert retirar(cliente, pieza_id).status_code == 200

    respuesta = restaurar(cliente, pieza_id)

    assert respuesta.status_code == 200, respuesta.text
    assert sin_hora(respuesta.json()) == {
        "pieza_id": pieza_id, "indexacion_encolada": True, "procesamiento_encolado": False
    }
    [fila] = cliente.get("/api/piezas", params={"mias": True}).json()["piezas"]
    assert (fila["pieza_id"], fila["estado"], fila["no_indexada"]) == (pieza_id, "Validada", True)
    assert trabajador.procesar_siguiente() is True
    assert cliente.get("/api/piezas", params={"mias": True}).json()["piezas"] == []
    _, eventos = turno(cliente, nueva(cliente), "laboratorio de ensayos de materiales")
    assert [f["pieza_id"] for f in dict(eventos)["fuentes"]["fichas"]] == [pieza_id]


def test_una_recibida_restaurada_encola_un_procesamiento_nuevo_que_espera_al_motor(
    cliente, url_app, url_bd
):
    with psycopg.connect(url_app) as conexion:
        cambiar(conexion, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Prueba")
    pieza_id = subir(cliente, PDF + b"%recibida\n")
    assert retirar(cliente, pieza_id).status_code == 200

    respuesta = restaurar(cliente, pieza_id)

    assert respuesta.status_code == 200, respuesta.text
    assert sin_hora(respuesta.json()) == {
        "pieza_id": pieza_id, "indexacion_encolada": False, "procesamiento_encolado": True
    }
    assert trabajos_de(url_bd, pieza_id) == ["cancelado", "encolado"]
    detalle = cliente.get(f"/api/piezas/{pieza_id}").json()
    # Lo que la pantalla dice «esperando al motor (procesamiento detenido)».
    assert (detalle["estado"], detalle["estado_procesamiento"], detalle["procesamiento_pausado"]) == (
        "Recibida", "encolado", True
    )
    with psycopg.connect(url_app) as conexion:
        cambiar(conexion, por=TOMAS, cambios={Freno.PROCESAMIENTO: True}, motivo="Prueba")
    esperar_estado(cliente, pieza_id, "Pendiente")


def test_una_pendiente_vuelve_como_estaba_y_la_restauracion_queda_auditada_como_interina(
    cliente, pieza_pendiente, url_bd, sin_rgc
):
    assert retirar(cliente, pieza_pendiente, ADMINISTRADOR_TECNICO).status_code == 200

    respuesta = restaurar(cliente, pieza_pendiente, ADMINISTRADOR_TECNICO)

    assert respuesta.status_code == 200, respuesta.text
    assert sin_hora(respuesta.json()) == {
        "pieza_id": pieza_pendiente, "indexacion_encolada": False, "procesamiento_encolado": False
    }
    assert not retirada(url_bd, pieza_pendiente)
    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Pendiente"
    [evento] = restauraciones(cliente)
    assert (str(evento.actor.usuario_id), evento.objeto.id, evento.motivo) == (
        ADMINISTRADOR_TECNICO, pieza_pendiente, "Se retiró por error."
    )
    assert evento.antes == {"estado": "Pendiente", "retirada": True}
    assert evento.despues == {
        "retirada": False,
        "indexacion_encolada": False,
        "procesamiento_encolado": False,
        "interino": True,
    }


def test_restaurar_con_otra_pieza_activa_del_mismo_original_responde_409_con_su_id(
    cliente, url_bd
):
    vieja = subir(cliente, PDF + b"%dup\n")
    assert retirar(cliente, vieja).status_code == 200
    nueva_ = subir(cliente, PDF + b"%dup\n")

    respuesta = restaurar(cliente, vieja)

    assert respuesta.status_code == 409
    assert (respuesta.json()["type"], respuesta.json()["pieza_id"]) == (
        "/errores/pieza-duplicada", nueva_
    )
    assert retirada(url_bd, vieja)
    assert restauraciones(cliente) == []


def test_restaurar_en_un_lote_lleno_responde_409_lote_lleno(cliente, url_bd):
    lote_id = crear_lote(cliente).json()["lote"]["lote_id"]
    pieza_id = subir_al_lote(cliente, lote_id, "del-lote.pdf", PDF + b"%lote\n").json()["pieza_id"]
    assert retirar(cliente, pieza_id).status_code == 200
    llenar_lote(url_bd, lote_id, 100)

    respuesta = restaurar(cliente, pieza_id)

    assert respuesta.status_code == 409
    assert (respuesta.json()["type"], respuesta.json()["tope"]) == ("/errores/lote-lleno", 100)
    assert retirada(url_bd, pieza_id)


def test_restaurar_una_no_retirada_o_dos_veces_responde_409_transicion_invalida(
    cliente, pieza_pendiente
):
    assert restaurar(cliente, pieza_pendiente).json()["type"] == "/errores/transicion-invalida"
    assert retirar(cliente, pieza_pendiente).status_code == 200
    assert restaurar(cliente, pieza_pendiente).status_code == 200

    respuesta = restaurar(cliente, pieza_pendiente)

    assert respuesta.status_code == 409
    assert respuesta.json()["type"] == "/errores/transicion-invalida"
    assert len(restauraciones(cliente)) == 1


def test_restaurar_responde_404_403_y_422(cliente, pieza_pendiente, url_bd):
    assert retirar(cliente, pieza_pendiente).status_code == 200

    assert restaurar(cliente, str(uuid4())).status_code == 404
    prohibido = restaurar(cliente, pieza_pendiente, JORGE)
    assert (prohibido.status_code, prohibido.json()["type"]) == (
        403, "/errores/permiso-insuficiente"
    )
    invalido = restaurar(cliente, pieza_pendiente, motivo="ab")
    assert (invalido.status_code, invalido.json()["campo"]) == (422, "motivo")
    assert retirada(url_bd, pieza_pendiente)


@pytest.mark.historia(295)
def test_de_dos_restauraciones_concurrentes_del_mismo_original_entra_una_sola(
    cliente, url_bd, monkeypatch
):
    primera = subir(cliente, PDF + b"%carrera\n")
    assert retirar(cliente, primera).status_code == 200
    segunda = subir(cliente, PDF + b"%carrera\n")
    assert retirar(cliente, segunda).status_code == 200
    # Las dos pasan la comprobación de la huella antes de que alguna quite su marca: solo el
    # índice único por SHA-256 decide (spec, módulo 18).
    las_dos_comprobaron = threading.Barrier(2, timeout=10)
    desmarcar = ModuloFichasPostgres.desmarcar_borrada

    def desmarcar_juntas(self, tx, pieza_id):
        las_dos_comprobaron.wait()
        return desmarcar(self, tx, pieza_id)

    monkeypatch.setattr(ModuloFichasPostgres, "desmarcar_borrada", desmarcar_juntas)
    retiro = cliente.app.state.retiro
    resultados: dict[str, object] = {}

    def restaurar_en_hilo(pieza_id: str) -> None:
        try:
            retiro.restaurar_pieza(
                pieza_id=UUID(pieza_id), principal=MARIA_FIRMANDO, motivo="Se retiró por error."
            )
            resultados[pieza_id] = "restaurada"
        except PiezaDuplicada as error:
            resultados[pieza_id] = str(error.pieza_id)

    hilos = [threading.Thread(target=restaurar_en_hilo, args=(p,)) for p in (primera, segunda)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(20)

    [ganadora] = [p for p, r in resultados.items() if r == "restaurada"]
    [perdedora] = [p for p in (primera, segunda) if p != ganadora]
    assert resultados[perdedora] == ganadora
    assert (retirada(url_bd, ganadora), retirada(url_bd, perdedora)) == (False, True)
    assert len(restauraciones(cliente)) == 1
