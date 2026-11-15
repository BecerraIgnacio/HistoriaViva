"""La administración de los tokens de Agentes desde la pantalla (spec, tabla de endpoints
`/api/admin/agentes`, P7c; RF-26, CU-16, DEC-32, DEC-136).

Postgres real a través de la app: la API confirma desde su pool, y lo que `/v1` hará en cada
pedido se prueba con `Credenciales.autenticar` sobre la misma base.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import entrar
from historia_viva.agentes import Credenciales, TokenInvalido
from historia_viva.auditoria import RegistroDeAuditoria
from historia_viva.interruptor import Freno, habilitado
from validacion import ADMINISTRADOR_TECNICO

pytestmark = [
    pytest.mark.rf("RF-26"),
    pytest.mark.confirma("la app confirma el alta, la emisión y la revocación desde su pool"),
]

AGENTES = "/api/admin/agentes"


def _emitir(cliente: TestClient, nombre: str = "Agente 1") -> dict:
    respuesta = cliente.post(AGENTES, json={"nombre": nombre, "alcances": ["fragmentos:leer"]})
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _credenciales(cliente: TestClient, ahora: datetime | None = None) -> Credenciales:
    """Las de `/v1`, sobre la base de la app; con `ahora`, en ese momento."""
    bd = cliente.app.state.bd  # type: ignore[attr-defined]
    return Credenciales(
        bd=bd, auditoria=RegistroDeAuditoria(bd=bd), reloj=(lambda: ahora) if ahora else None
    )


@pytest.mark.historia(151, 155)
def test_emitir_devuelve_el_token_una_sola_vez_y_la_lista_nunca_lo_trae(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    assert cliente.get(AGENTES).json() == {"agentes": []}

    respuesta = cliente.post(
        AGENTES, json={"nombre": "Agente 1", "alcances": ["fragmentos:leer", "piezas:leer"]}
    )
    emitido = respuesta.json()
    principal = _credenciales(cliente).autenticar(emitido["token"])
    lista = cliente.get(AGENTES)

    assert respuesta.status_code == 201
    assert respuesta.headers["cache-control"] == "no-store"
    assert set(emitido) == {"agente_id", "nombre", "alcances", "token_id", "token", "vence_el"}
    assert emitido["nombre"] == "Agente 1"
    assert emitido["alcances"] == ["fragmentos:leer", "piezas:leer"]
    assert principal.nombre == "Agente 1"
    assert lista.headers["cache-control"] == "no-store"
    assert emitido["token"] not in lista.text
    [agente] = lista.json()["agentes"]
    [token] = agente.pop("tokens")
    assert agente == {
        "agente_id": emitido["agente_id"],
        "nombre": "Agente 1",
        "alcances": ["fragmentos:leer", "piezas:leer"],
    }
    assert token["token_id"] == emitido["token_id"]
    assert emitido["token"].startswith(token["prefijo"] + "_")
    assert (token["estado"], token["vence_el"], token["revocado_el"]) == (
        "activo", emitido["vence_el"], None
    )
    assert token["ultimo_uso_el"] is not None  # lo dejó el `autenticar` de arriba
    assert set(token) == {"token_id", "prefijo", "estado", "creado_el", "vence_el",
                          "ultimo_uso_el", "revocado_el"}


@pytest.mark.historia(153)
def test_rotar_con_fecha_deja_valer_al_viejo_y_al_nuevo_hasta_esa_fecha(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    viejo = _emitir(cliente)
    fecha = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)

    respuesta = cliente.post(
        f"{AGENTES}/{viejo['agente_id']}/tokens", json={"vencen_anteriores_el": fecha.isoformat()}
    )

    assert respuesta.status_code == 201
    assert respuesta.headers["cache-control"] == "no-store"
    nuevo = respuesta.json()
    assert (nuevo["agente_id"], nuevo["nombre"], nuevo["alcances"]) == (
        viejo["agente_id"], "Agente 1", ["fragmentos:leer"]
    )
    assert nuevo["token"] != viejo["token"]
    antes = _credenciales(cliente, fecha - timedelta(seconds=1))
    assert antes.autenticar(viejo["token"]).token_id == UUID(viejo["token_id"])
    assert antes.autenticar(nuevo["token"]).token_id == UUID(nuevo["token_id"])
    despues = _credenciales(cliente, fecha)
    with pytest.raises(TokenInvalido):
        despues.autenticar(viejo["token"])
    assert despues.autenticar(nuevo["token"]).nombre == "Agente 1"
    # Durante el solape el viejo sigue activo y la lista dice hasta cuándo.
    tokens = {t["token_id"]: t for t in cliente.get(AGENTES).json()["agentes"][0]["tokens"]}
    assert tokens[viejo["token_id"]]["estado"] == "activo"
    assert tokens[viejo["token_id"]]["revocado_el"] == fecha.isoformat().replace("+00:00", "Z")
    assert tokens[nuevo["token_id"]]["revocado_el"] is None


@pytest.mark.historia(153)
def test_rotar_sin_fecha_emite_otro_y_el_anterior_sigue_valiendo(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    viejo = _emitir(cliente)

    nuevo = cliente.post(f"{AGENTES}/{viejo['agente_id']}/tokens", json={}).json()

    credenciales = _credenciales(cliente)
    assert credenciales.autenticar(viejo["token"]).nombre == "Agente 1"
    assert credenciales.autenticar(nuevo["token"]).nombre == "Agente 1"


@pytest.mark.historia(153)
@pytest.mark.parametrize("horas", [-1, 7 * 24 + 1], ids=["pasada", "mas_de_siete_dias"])
def test_rotar_con_una_fecha_fuera_de_la_ventana_responde_422_y_no_emite(cliente, horas):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    viejo = _emitir(cliente)
    fecha = datetime.now(UTC) + timedelta(hours=horas)

    respuesta = cliente.post(
        f"{AGENTES}/{viejo['agente_id']}/tokens", json={"vencen_anteriores_el": fecha.isoformat()}
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["campo"] == "vencen_anteriores_el"
    assert len(cliente.get(AGENTES).json()["agentes"][0]["tokens"]) == 1


def test_rotar_un_agente_que_no_existe_responde_404(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)

    respuesta = cliente.post(f"{AGENTES}/{uuid4()}/tokens", json={})

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/recurso-no-encontrado"


@pytest.mark.historia(152)
def test_revocar_corta_el_pedido_siguiente_y_revocar_dos_veces_responde_409(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    emitido = _emitir(cliente)
    otro = _emitir(cliente, "Agente 2")
    ruta = f"{AGENTES}/{emitido['agente_id']}/tokens/{emitido['token_id']}/revocacion"

    primera = cliente.post(ruta)
    segunda = cliente.post(ruta)

    assert primera.status_code == 200
    assert set(primera.json()) == {"token_id", "revocado_el"}
    assert primera.json()["token_id"] == emitido["token_id"]
    with pytest.raises(TokenInvalido):
        _credenciales(cliente).autenticar(emitido["token"])
    assert _credenciales(cliente).autenticar(otro["token"]).nombre == "Agente 2"
    assert segunda.status_code == 409
    assert segunda.json()["type"] == "/errores/transicion-invalida"
    assert cliente.get(AGENTES).json()["agentes"][0]["tokens"][0]["estado"] == "revocado"


@pytest.mark.historia(152)
def test_revocar_un_token_que_no_existe_o_es_de_otro_agente_responde_404(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    uno, otro = _emitir(cliente), _emitir(cliente, "Agente 2")

    ajeno = cliente.post(f"{AGENTES}/{otro['agente_id']}/tokens/{uno['token_id']}/revocacion")
    inexistente = cliente.post(f"{AGENTES}/{uno['agente_id']}/tokens/{uuid4()}/revocacion")

    assert (ajeno.status_code, inexistente.status_code) == (404, 404)
    assert ajeno.json()["type"] == "/errores/recurso-no-encontrado"
    assert _credenciales(cliente).autenticar(uno["token"]).nombre == "Agente 1"


@pytest.mark.historia(151)
@pytest.mark.parametrize(
    ("pedido", "campo"),
    [
        ({"nombre": "Agente 1", "alcances": ["piezas:leer"]}, "nombre"),
        ({"nombre": "Agente 2", "alcances": []}, "alcances"),
        ({"nombre": "Agente 2", "alcances": ["todo"]}, "alcances.0"),
        ({"nombre": "", "alcances": ["piezas:leer"]}, "nombre"),
        ({"nombre": "   ", "alcances": ["piezas:leer"]}, "nombre"),
        ({"nombre": " Agente 1 ", "alcances": ["piezas:leer"]}, "nombre"),
    ],
    ids=["nombre_existente", "sin_alcances", "alcance_desconocido", "sin_nombre",
         "nombre_en_blanco", "nombre_existente_con_espacios"],
)
def test_emitir_un_pedido_invalido_responde_422_sin_emitir(cliente, pedido, campo):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    _emitir(cliente)

    respuesta = cliente.post(AGENTES, json=pedido)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (
        "/errores/pedido-invalido", campo
    )
    [agente] = cliente.get(AGENTES).json()["agentes"]
    assert len(agente["tokens"]) == 1


def test_rotar_con_una_fecha_sin_zona_horaria_responde_422(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    agente = _emitir(cliente)

    respuesta = cliente.post(
        f"{AGENTES}/{agente['agente_id']}/tokens", json={"vencen_anteriores_el": "2026-10-17T10:00"}
    )

    assert respuesta.status_code == 422


@pytest.mark.historia(151)
def test_emitir_desde_la_pantalla_no_enciende_la_api_de_agentes(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)

    agente = _emitir(cliente)
    cliente.post(f"{AGENTES}/{agente['agente_id']}/tokens", json={})

    with cliente.app.state.bd.transaccion() as tx:  # type: ignore[attr-defined]
        assert habilitado(tx, Freno.API_AGENTES) is False


@pytest.mark.historia(151, 152, 153)
def test_emitir_rotar_y_revocar_quedan_auditados_con_la_persona_y_sin_el_token(cliente, url_bd):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    viejo = _emitir(cliente)
    fecha = datetime.now(UTC) + timedelta(hours=1)
    nuevo = cliente.post(
        f"{AGENTES}/{viejo['agente_id']}/tokens", json={"vencen_anteriores_el": fecha.isoformat()}
    ).json()
    cliente.post(f"{AGENTES}/{nuevo['agente_id']}/tokens/{nuevo['token_id']}/revocacion")

    with psycopg.connect(url_bd) as conexion:
        eventos = conexion.execute(
            "SELECT accion, actor_tipo, actor_id::text, objeto_id, a::text FROM acervo.auditoria a"
            " WHERE accion LIKE 'agente.%%' ORDER BY id"
        ).fetchall()

    assert [accion for accion, *_ in eventos] == [
        "agente.alta", "agente.emision", "agente.revocacion", "agente.emision",
        "agente.revocacion",
    ]
    assert {(tipo, actor, objeto) for _, tipo, actor, objeto, _ in eventos} == {
        ("persona", ADMINISTRADOR_TECNICO, viejo["agente_id"])
    }
    for *_, fila in eventos:
        assert viejo["token"][12:] not in fila
        assert nuevo["token"][12:] not in fila


@pytest.mark.historia(154, 155)
def test_la_lista_dice_vencido_cuando_pasa_el_vencimiento(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    emitido = _emitir(cliente)
    vence_el = datetime.fromisoformat(emitido["vence_el"])
    # La API trunca al segundo.
    credenciales = _credenciales(cliente, vence_el + timedelta(seconds=1))

    with cliente.app.state.bd.transaccion() as tx:  # type: ignore[attr-defined]
        [agente] = credenciales.listar(tx)

    assert agente.tokens[0].estado.value == "vencido"
