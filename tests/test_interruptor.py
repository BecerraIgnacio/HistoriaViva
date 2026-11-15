"""El interruptor global (spec, módulo `interruptor`; ADR-0004 control 4, DEC-88, DEC-136).

Postgres real. Lo que se prueba sobre `tx` se revierte; el comando y el trabajador confirman
desde su propio pool, y al terminar la suite deja solo los eventos de la migración.
"""

from __future__ import annotations

import getpass
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, cast
from uuid import UUID

import psycopg
import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from espera import esperar_estado
from historia_viva.api_agentes import (
    RETRY_AFTER_INTERRUPTOR_S,
    Cupos,
    ErrorV1,
    con_api_agentes_habilitada,
)
from historia_viva.api_agentes import registrar_manejadores as registrar_manejadores_v1
from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.bd import Bd
from historia_viva import composicion
from historia_viva.composicion import armar_trabajador, main
from historia_viva.interruptor import (
    EventoInterruptor,
    Freno,
    MotivoRequerido,
    cambiar,
    estado,
    frenar_todo,
    habilitado,
    habilitado_en,
    historial,
)
from validacion import ADMINISTRADOR_TECNICO, EDICION_COMPLETA, JORGE, MARIA, PDF

pytestmark = pytest.mark.rf("RF-30")

TOMAS = Actor(
    TipoDeActor.PERSONA,
    "tibarra@fie.undef.edu.ar",
    usuario_id=UUID("44444444-4444-4444-8444-444444444444"),
)
"""El Administrador técnico de las personas de prueba."""


@pytest.mark.historia(159)
def test_la_migracion_deja_el_procesamiento_andando_y_la_api_de_agentes_apagada(tx):
    vigente = estado(tx)

    assert {freno: evento.habilitado for freno, evento in vigente.items()} == {
        Freno.PROCESAMIENTO: True,
        Freno.API_AGENTES: False,
    }
    assert {evento.motivo for evento in vigente.values()} == {"alta inicial"}


@pytest.mark.historia(161)
def test_sin_eventos_el_freno_esta_detenido(tx):
    assert habilitado(tx, Freno.PROCESAMIENTO) is True

    tx.execute("DELETE FROM acervo.evento_interruptor")

    assert habilitado(tx, Freno.PROCESAMIENTO) is False


@pytest.mark.historia(161)
def test_con_un_error_de_lectura_el_freno_esta_detenido(url_app):
    conexion = psycopg.connect(url_app)
    conexion.close()
    bd = Bd(url_app, tamano_pool=1)
    bd.cerrar()

    assert habilitado(conexion, Freno.PROCESAMIENTO) is False
    assert habilitado_en(bd, Freno.PROCESAMIENTO) is False


class _BdQueFallaAlConfirmar:
    """Lee bien y se cae al confirmar: la conexión se cerró a mitad de camino."""

    def __init__(self, tx: psycopg.Connection[Any]) -> None:
        self._tx = tx

    @contextmanager
    def transaccion(self) -> Iterator[psycopg.Connection[Any]]:
        yield self._tx
        raise psycopg.InterfaceError("the connection is closed")


@pytest.mark.historia(161)
def test_si_la_confirmacion_de_la_lectura_falla_el_freno_esta_detenido(tx, caplog):
    bd = cast(Bd, _BdQueFallaAlConfirmar(tx))

    assert habilitado_en(bd, Freno.PROCESAMIENTO) is False
    assert "queda detenido" in caplog.text


@pytest.mark.historia(159)
def test_cambiar_deja_un_evento_por_clave_que_cambia_y_el_mismo_valor_no_repite(tx):
    # La API de Agentes ya está apagada desde la migración: solo cambia el procesamiento.
    primero = cambiar(
        tx,
        por=TOMAS,
        cambios={Freno.PROCESAMIENTO: False, Freno.API_AGENTES: False},
        motivo="Pruebas del modelo nuevo",
    )
    segundo = cambiar(
        tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Pruebas del modelo nuevo"
    )

    assert [(e.clave, e.habilitado, e.motivo, e.actor) for e in primero] == [
        (Freno.PROCESAMIENTO, False, "Pruebas del modelo nuevo", TOMAS)
    ]
    assert segundo == []
    assert habilitado(tx, Freno.PROCESAMIENTO) is False
    assert _eventos(tx) == 3


@pytest.mark.historia(160)
def test_frenar_todo_apaga_las_dos_claves_con_un_evento_cada_una(tx):
    cambiar(tx, por=TOMAS, cambios={Freno.API_AGENTES: True}, motivo="")

    eventos = frenar_todo(tx, por=TOMAS, motivo="Fin de la jornada")

    assert {(e.clave, e.habilitado) for e in eventos} == {
        (Freno.PROCESAMIENTO, False),
        (Freno.API_AGENTES, False),
    }
    assert not any(evento.habilitado for evento in estado(tx).values())


@pytest.mark.historia(159)
@pytest.mark.parametrize("motivo", ["", "  ab  ", "cuat"])
def test_apagar_sin_un_motivo_de_cinco_caracteres_se_rechaza(tx, motivo):
    with pytest.raises(MotivoRequerido):
        cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo=motivo)
    with pytest.raises(MotivoRequerido):
        frenar_todo(tx, por=TOMAS, motivo=motivo)

    assert _eventos(tx) == 2


@pytest.mark.historia(159)
def test_historial_trae_los_ultimos_eventos_del_mas_nuevo_al_mas_viejo(tx):
    cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Corrida del banco")
    cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: True}, motivo="")

    eventos = historial(tx)

    assert [(e.clave, e.habilitado, e.motivo) for e in eventos[:2]] == [
        (Freno.PROCESAMIENTO, True, ""),
        (Freno.PROCESAMIENTO, False, "Corrida del banco"),
    ]
    assert len(eventos) == 4
    assert historial(tx, limite=1) == eventos[:1]


def _eventos(tx: psycopg.Connection[Any]) -> int:
    fila = tx.execute("SELECT count(*) FROM acervo.evento_interruptor").fetchone()
    assert fila is not None
    return int(fila[0])


def _vigente(url_app: str) -> dict[Freno, EventoInterruptor]:
    with psycopg.connect(url_app) as conexion:
        return estado(conexion)


@pytest.mark.historia(159)
@pytest.mark.confirma("el comando confirma desde su propia conexión, como `hv_app`")
def test_el_comando_detiene_una_clave_con_motivo_y_deja_el_evento_como_cli(
    url_app, monkeypatch, capsys
):
    monkeypatch.setenv("HV_BD_URL", url_app)

    codigo = main(
        ["interruptor", "detener", "procesamiento_habilitado", "--motivo", "Corrida del banco"]
    )

    procesamiento = _vigente(url_app)[Freno.PROCESAMIENTO]
    assert codigo == 0
    assert (procesamiento.habilitado, procesamiento.motivo) == (False, "Corrida del banco")
    assert (procesamiento.actor.tipo, procesamiento.actor.usuario_id) == (TipoDeActor.CLI, None)
    assert procesamiento.actor.etiqueta == getpass.getuser()
    assert "procesamiento_habilitado: detenido" in capsys.readouterr().out


@pytest.mark.historia(159)
def test_el_comando_sin_motivo_se_niega(url_app, monkeypatch, capsys):
    monkeypatch.setenv("HV_BD_URL", url_app)

    codigo = main(["interruptor", "detener", "procesamiento_habilitado"])

    assert codigo == 2
    assert "motivo" in capsys.readouterr().err
    assert _vigente(url_app)[Freno.PROCESAMIENTO].habilitado is True


@pytest.mark.historia(161)
def test_el_comando_muestra_detenida_una_clave_sin_eventos(url_app, monkeypatch, capsys):
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setattr(
        composicion, "estado", lambda tx: {Freno.PROCESAMIENTO: estado(tx)[Freno.PROCESAMIENTO]}
    )

    assert main(["interruptor", "estado"]) == 0

    salida = capsys.readouterr().out
    assert "procesamiento_habilitado: habilitado" in salida
    assert "api_agentes_habilitada: detenido" in salida


@pytest.mark.historia(160)
@pytest.mark.confirma("el comando confirma desde su propia conexión, como `hv_app`")
def test_el_comando_frena_todo_y_reanudar_el_procesamiento_no_enciende_la_api(
    url_app, monkeypatch
):
    monkeypatch.setenv("HV_BD_URL", url_app)
    main(["interruptor", "reanudar", "api_agentes_habilitada"])

    assert main(["interruptor", "frenar-todo", "--motivo", "Fin de la jornada"]) == 0
    assert main(["interruptor", "reanudar", "procesamiento_habilitado"]) == 0

    vigente = _vigente(url_app)
    assert (vigente[Freno.PROCESAMIENTO].habilitado, vigente[Freno.API_AGENTES].habilitado) == (
        True,
        False,
    )
    assert vigente[Freno.API_AGENTES].motivo == "Fin de la jornada"


@pytest.mark.historia(162, 84)
@pytest.mark.confirma("`web` y el trabajador confirman desde sus pools")
def test_con_el_procesamiento_detenido_se_sube_y_se_encola_pero_no_se_procesa_hasta_reanudar(
    url_app, capa_modelos, directorio_originales
):
    web = TestClient(
        app_sin_trabajador(
            url_app,
            capa_para=capa_modelos.para,
            validar=capa_modelos.validar,
            directorio_originales=directorio_originales,
        ),
        headers=CABECERAS,
    )
    entrar(web, MARIA)
    bd = Bd(url_app, tamano_pool=2)
    trabajador = armar_trabajador(
        bd=bd, capa_para=capa_modelos.para, directorio_originales=directorio_originales
    )
    try:
        with bd.transaccion() as tx:
            cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Corrida del banco")

        respuesta = web.post("/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")})

        assert respuesta.status_code == 201
        assert respuesta.json()["procesamiento_pausado"] is True
        assert trabajador.procesar_siguiente() is False
        assert capa_modelos.invocaciones == []

        with bd.transaccion() as tx:
            cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: True}, motivo="")

        assert trabajador.procesar_siguiente() is True
    finally:
        bd.cerrar()
    pieza = web.get(respuesta.headers["Location"]).json()
    assert pieza["estado"] == "Pendiente"


@pytest.mark.historia(162)
@pytest.mark.confirma("`web` y el trabajador confirman desde sus pools")
def test_seg_interruptor_detenido_la_cola_no_avanza_y_lo_encolado_se_completa_al_reanudar(
    cliente, capa_modelos, url_app
):
    bd = Bd(url_app, tamano_pool=1)
    try:
        with bd.transaccion() as tx:
            cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Corrida del banco")
        entrar(cliente, MARIA)
        respuestas = [
            cliente.post(
                "/api/piezas",
                files={"archivo": (f"acta-{n}.pdf", PDF + bytes([n]), "application/pdf")},
            )
            for n in range(3)
        ]
        assert [r.status_code for r in respuestas] == [201] * 3
        time.sleep(0.5)  # cada subida ya despertó al trabajador en marcha
        assert capa_modelos.invocaciones == []

        with bd.transaccion() as tx:
            cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: True}, motivo="")
    finally:
        bd.cerrar()

    for respuesta in respuestas:
        esperar_estado(cliente, respuesta.json()["pieza_id"], "Pendiente")


# ── El panel de Administración (037) ─────────────────────────────────────────────────────


@pytest.mark.historia(172)
@pytest.mark.confirma("el 403 queda en `auditoria` desde el pool de la app")
@pytest.mark.parametrize("persona", [MARIA, JORGE], ids=["rgc", "coordinador"])
def test_seg_interruptor_el_panel_exige_operar_sistema(cliente, persona):
    entrar(cliente, persona)

    lectura = cliente.get("/api/admin/interruptor")
    cambio = cliente.put(
        "/api/admin/interruptor",
        json={"procesamiento_habilitado": False, "api_agentes_habilitada": False,
              "motivo": "Fin de la jornada"},
    )

    assert (lectura.status_code, cambio.status_code) == (403, 403)
    assert cambio.json()["type"] == "/errores/permiso-insuficiente"
    assert _vigente_en(cliente)[Freno.PROCESAMIENTO].habilitado is True


def _vigente_en(cliente: TestClient) -> dict[Freno, EventoInterruptor]:
    with cliente.app.state.bd.transaccion() as tx:  # type: ignore[attr-defined]
        return estado(tx)


@pytest.mark.historia(159, 172)
@pytest.mark.confirma("la app confirma el evento desde su pool")
def test_el_tecnico_detiene_el_procesamiento_desde_el_panel_y_el_get_trae_el_historial(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)

    cambio = cliente.put(
        "/api/admin/interruptor",
        json={"procesamiento_habilitado": False, "api_agentes_habilitada": False,
              "motivo": "Corrida del banco"},
    )
    panel = cliente.get("/api/admin/interruptor").json()

    assert cambio.status_code == 200
    assert cambio.json() == panel
    assert (panel["procesamiento_habilitado"], panel["api_agentes_habilitada"]) == (False, False)
    assert (panel["cambiado_por"], panel["motivo"]) == ("tibarra@fie.undef.edu.ar",
                                                        "Corrida del banco")
    # Solo cambió el procesamiento: la API ya estaba apagada desde la migración.
    assert [(e["clave"], e["habilitado"], e["persona"], e["motivo"]) for e in panel["historial"]] == [
        ("procesamiento_habilitado", False, "tibarra@fie.undef.edu.ar", "Corrida del banco"),
        ("api_agentes_habilitada", False, "migracion", "alta inicial"),
        ("procesamiento_habilitado", True, "migracion", "alta inicial"),
    ]
    detenido = _vigente_en(cliente)[Freno.PROCESAMIENTO]
    assert detenido.actor == TOMAS
    assert cliente.get("/api/admin/interruptor", params={"limite": 1}).json()["historial"] == (
        panel["historial"][:1]
    )


@pytest.mark.historia(159)
@pytest.mark.confirma("la sesión se abre desde el pool de la app")
@pytest.mark.parametrize("motivo", [None, "", "cuat", "x" * 501],
                         ids=["sin_motivo", "vacio", "corto", "largo"])
def test_apagar_desde_el_panel_sin_motivo_responde_422(cliente, motivo):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    pedido: dict[str, Any] = {"procesamiento_habilitado": False, "api_agentes_habilitada": False}
    if motivo is not None:
        pedido["motivo"] = motivo

    respuesta = cliente.put("/api/admin/interruptor", json=pedido)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (
        "/errores/pedido-invalido", "motivo"
    )
    assert _vigente_en(cliente)[Freno.PROCESAMIENTO].habilitado is True


@pytest.mark.historia(159)
@pytest.mark.confirma("los comandos y la app confirman desde sus propias conexiones")
def test_seg_api_agentes_apagada_por_defecto(cliente, url_app, monkeypatch):
    monkeypatch.setenv("HV_BD_URL", url_app)
    assert _vigente(url_app)[Freno.API_AGENTES].habilitado is False

    # Ningún comando que no la nombre la enciende, tampoco emitir un token (DEC-136).
    assert main(["interruptor", "frenar-todo", "--motivo", "Fin de la jornada"]) == 0
    assert main(["interruptor", "reanudar", "procesamiento_habilitado"]) == 0
    assert main(["emitir-token", "Agente 1", "--alcances", "fragmentos:leer"]) == 0
    entrar(cliente, ADMINISTRADOR_TECNICO)
    assert cliente.put("/api/admin/interruptor",
                json={"procesamiento_habilitado": False, "api_agentes_habilitada": False,
                      "motivo": "Corrida del banco"}).status_code == 200
    assert _vigente(url_app)[Freno.API_AGENTES].habilitado is False

    # Solo el cambio explícito del técnico.
    encender = {"procesamiento_habilitado": True, "api_agentes_habilitada": True}
    assert cliente.put("/api/admin/interruptor", json=encender).status_code == 200

    encendida = _vigente(url_app)[Freno.API_AGENTES]
    assert (encendida.habilitado, encendida.actor) == (True, TOMAS)


@pytest.mark.historia(84)
@pytest.mark.confirma("la app confirma el evento desde su pool")
def test_yo_trae_el_estado_del_procesamiento_para_la_franja(cliente):
    entrar(cliente, MARIA)
    assert cliente.get("/api/yo").json()["sistema"]["procesamiento_habilitado"] is True

    with cliente.app.state.bd.transaccion() as tx:  # type: ignore[attr-defined]
        cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Corrida del banco")

    assert cliente.get("/api/yo").json()["sistema"]["procesamiento_habilitado"] is False


# ── El freno de la API de Agentes (037; lo cablea 097 a `/v1`) ───────────────────────────


def _autenticar_falso(authorization: Annotated[str, Header()] = "") -> str:
    """El autenticador de prueba: un único token vale."""
    if authorization != "Bearer bueno":
        raise ErrorV1(401, "token_invalido", "El token no es válido.")
    return "agente-de-prueba"


@pytest.fixture
def v1_de_prueba(url_app) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.bd = Bd(url_app, tamano_pool=1)
    registrar_manejadores_v1(app, retry_after_s=RETRY_AFTER_INTERRUPTOR_S, cupos=Cupos())

    @app.get("/v1/prueba")
    def prueba(
        agente: Annotated[str, Depends(con_api_agentes_habilitada(_autenticar_falso))],
    ) -> dict[str, str]:
        return {"agente": agente}

    try:
        yield TestClient(app)
    finally:
        app.state.bd.cerrar()


@pytest.mark.historia(161, 186)
@pytest.mark.confirma("el freno se lee desde el pool de la app de prueba")
def test_seg_interruptor_api_agentes_apagada_responde_503_despues_de_autenticar(
    v1_de_prueba, url_app
):
    detenida = v1_de_prueba.get("/v1/prueba", headers={"Authorization": "Bearer bueno"})
    anonimo = v1_de_prueba.get("/v1/prueba", headers={"Authorization": "Bearer malo"})

    assert detenida.status_code == 503
    assert detenida.headers["Retry-After"] == "300"
    assert detenida.json()["codigo"] == "servicio_no_disponible"
    assert set(detenida.json()) == {"codigo", "mensaje", "solicitud_id"}
    # Un anónimo no averigua el estado del sistema (DEC-109).
    assert (anonimo.status_code, anonimo.json()["codigo"]) == (401, "token_invalido")

    with psycopg.connect(url_app) as conexion:
        cambiar(conexion, por=TOMAS, cambios={Freno.API_AGENTES: True}, motivo="")

    encendida = v1_de_prueba.get("/v1/prueba", headers={"Authorization": "Bearer bueno"})
    assert (encendida.status_code, encendida.json()) == (200, {"agente": "agente-de-prueba"})


@pytest.mark.historia(162)
@pytest.mark.confirma("`web` y el trabajador confirman desde sus pools")
def test_seg_interruptor_detenido_la_lista_y_el_guardado_de_la_ficha_siguen(
    cliente, pieza_pendiente
):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    cliente.put("/api/admin/interruptor",
                json={"procesamiento_habilitado": False, "api_agentes_habilitada": False,
                      "motivo": "Corrida del banco"})
    entrar(cliente, MARIA)

    lista = cliente.get("/api/piezas")
    guardado = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)
    subida = cliente.post("/api/piezas", files={"archivo": ("otra.pdf", PDF + b"1", "application/pdf")})

    assert cliente.get("/api/yo").json()["sistema"]["procesamiento_habilitado"] is False
    assert lista.status_code == 200
    assert guardado.status_code == 200
    assert (subida.status_code, subida.json()["procesamiento_pausado"]) == (201, True)


@pytest.mark.historia(159)
def test_reanudar_no_pide_motivo_aunque_el_otro_freno_siga_detenido(tx):
    frenar_todo(tx, por=TOMAS, motivo="Fin de la jornada")

    # Lo que manda el panel: los dos frenos, y solo cambia el procesamiento.
    cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: True, Freno.API_AGENTES: False}, motivo="")

    assert (habilitado(tx, Freno.PROCESAMIENTO), habilitado(tx, Freno.API_AGENTES)) == (True, False)
