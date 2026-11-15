"""Los tokens de los Agentes externos (spec, módulo `agentes`; RF-26, DEC-32, DEC-136).

Postgres real y como `hv_app`: `autenticar` lee desde su propio pool lo que confirmó la emisión,
como lo hará `/v1` en cada pedido. El reloj es inyectado.
"""

from __future__ import annotations

import getpass
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY
from uuid import UUID

import psycopg
import pytest

from historia_viva.agentes import (
    Credenciales,
    PrincipalAgente,
    Scope,
    TokenEmitido,
    TokenInexistente,
    TokenInvalido,
)
from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria, TipoDeActor
from historia_viva.bd import Bd
from historia_viva.composicion import main
from historia_viva.interruptor import Freno, cambiar, estado, historial

pytestmark = [
    pytest.mark.rf("RF-26"),
    pytest.mark.confirma("`autenticar` lee desde su propio pool lo que confirmó la emisión"),
]

TOMAS = Actor(
    TipoDeActor.PERSONA,
    "tibarra@fie.undef.edu.ar",
    usuario_id=UUID("44444444-4444-4444-8444-444444444444"),
)
"""El Administrador técnico de las personas de prueba."""

INICIO = datetime(2026, 10, 1, 12, tzinfo=UTC)


class Reloj:
    def __init__(self) -> None:
        self.ahora = INICIO

    def __call__(self) -> datetime:
        return self.ahora


@pytest.fixture(scope="module")
def bd(url_app: str) -> Iterator[Bd]:
    bd = Bd(url_app, tamano_pool=1)
    yield bd
    bd.cerrar()


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def credenciales(bd: Bd, reloj: Reloj) -> Credenciales:
    return Credenciales(bd=bd, auditoria=RegistroDeAuditoria(bd=bd), reloj=reloj)


def _agente(bd: Bd, credenciales: Credenciales, nombre: str = "Agente 1") -> UUID:
    with bd.transaccion() as tx:
        return credenciales.crear_agente(
            tx, nombre=nombre, scopes=frozenset({Scope.FRAGMENTOS_LEER}), por=TOMAS
        )


@pytest.mark.historia(151)
def test_el_token_tiene_55_caracteres_prefijos_distintos_y_la_base_guarda_otro_hash(
    bd, credenciales, url_bd
):
    agente_id = _agente(bd, credenciales)
    with bd.transaccion() as tx:
        emitidos = [credenciales.emitir(tx, agente_id=agente_id, por=TOMAS) for _ in range(3)]

    for emitido in emitidos:
        assert re.fullmatch(r"hv_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", emitido.token)
        assert len(emitido.token) == 55
        assert emitido.token.startswith(emitido.prefijo + "_")
    assert len({e.prefijo for e in emitidos}) == 3
    with psycopg.connect(url_bd) as conexion:
        guardados = conexion.execute(
            "SELECT token_hash, token_prefijo FROM acervo.token_agente"
        ).fetchall()
    assert {prefijo for _, prefijo in guardados} == {e.prefijo for e in emitidos}
    assert not {hash_ for hash_, _ in guardados} & {e.token for e in emitidos}
    assert all(len(hash_) == 64 for hash_, _ in guardados)


def _emitir(bd: Bd, credenciales: Credenciales, agente_id: UUID) -> TokenEmitido:
    with bd.transaccion() as tx:
        return credenciales.emitir(tx, agente_id=agente_id, por=TOMAS)



@pytest.mark.historia(151)
def test_el_repr_del_token_emitido_no_muestra_el_token(bd, credenciales):
    emitido = _emitir(bd, credenciales, _agente(bd, credenciales))

    assert emitido.token[12:] not in repr(emitido)
    assert emitido.prefijo in repr(emitido)

@pytest.mark.historia(151)
def test_autenticar_acepta_un_token_vigente_con_su_agente_y_sus_alcances(bd, credenciales):
    agente_id = _agente(bd, credenciales)
    emitido = _emitir(bd, credenciales, agente_id)

    principal = credenciales.autenticar(emitido.token)

    assert principal == PrincipalAgente(
        agente_id, "Agente 1", emitido.token_id, emitido.prefijo, frozenset({Scope.FRAGMENTOS_LEER})
    )
    assert principal.puede(Scope.FRAGMENTOS_LEER)
    assert not principal.puede(Scope.APORTES_ESCRIBIR)


@pytest.mark.historia(152)
def test_revocado_vencido_malformado_y_desconocido_dan_el_mismo_error(bd, credenciales, reloj):
    agente_id = _agente(bd, credenciales)
    revocado, vencido = _emitir(bd, credenciales, agente_id), _emitir(bd, credenciales, agente_id)
    with bd.transaccion() as tx:
        credenciales.revocar_token(tx, revocado.token_id, por=TOMAS)
    desconocido = vencido.prefijo + "_" + "A" * 43
    mensajes = set()

    def rechazado(token: str) -> None:
        with pytest.raises(TokenInvalido) as error:
            credenciales.autenticar(token)
        mensajes.add(str(error.value))

    for token in (revocado.token, "hv_123_corto", "", desconocido):
        rechazado(token)
    credenciales.autenticar(vencido.token)
    reloj.ahora = INICIO + timedelta(days=180)
    rechazado(vencido.token)
    assert len(mensajes) == 1


@pytest.mark.historia(154)
def test_vence_a_los_180_dias(bd, credenciales, reloj):
    emitido = _emitir(bd, credenciales, _agente(bd, credenciales))
    assert emitido.vence_el == INICIO + timedelta(days=180)

    reloj.ahora = INICIO + timedelta(days=180) - timedelta(seconds=1)
    credenciales.autenticar(emitido.token)
    reloj.ahora = INICIO + timedelta(days=180)
    with pytest.raises(TokenInvalido):
        credenciales.autenticar(emitido.token)


@pytest.mark.historia(152)
def test_revocar_rige_en_el_pedido_siguiente(bd, credenciales):
    emitido = _emitir(bd, credenciales, _agente(bd, credenciales))
    credenciales.autenticar(emitido.token)

    with bd.transaccion() as tx:
        credenciales.revocar_token(tx, emitido.token_id, por=TOMAS)

    with pytest.raises(TokenInvalido):
        credenciales.autenticar(emitido.token)


def test_revocar_dos_veces_o_un_token_que_no_existe_se_rechaza(bd, credenciales):
    emitido = _emitir(bd, credenciales, _agente(bd, credenciales))
    with bd.transaccion() as tx:
        credenciales.revocar_token(tx, emitido.token_id, por=TOMAS)

    for token_id in (emitido.token_id, UUID(int=0)):
        with pytest.raises(TokenInexistente), bd.transaccion() as tx:
            credenciales.revocar_token(tx, token_id, por=TOMAS)


@pytest.mark.historia(152)
def test_revocar_un_agente_revoca_sus_tokens_y_no_los_de_otro(bd, credenciales):
    agente_id, otro_id = _agente(bd, credenciales), _agente(bd, credenciales, "Agente 2")
    suyos = [_emitir(bd, credenciales, agente_id) for _ in range(2)]
    ajeno = _emitir(bd, credenciales, otro_id)

    with bd.transaccion() as tx:
        assert credenciales.revocar_agente(tx, agente_id, por=TOMAS) == 2

    for emitido in suyos:
        with pytest.raises(TokenInvalido):
            credenciales.autenticar(emitido.token)
    assert credenciales.autenticar(ajeno.token).nombre == "Agente 2"


@pytest.mark.historia(153)
def test_rotar_deja_dos_vivos_durante_el_solape_y_uno_despues(bd, credenciales, reloj):
    agente_id = _agente(bd, credenciales)
    viejo = _emitir(bd, credenciales, agente_id)

    with bd.transaccion() as tx:
        nuevo = credenciales.rotar(
            tx, agente_id=agente_id, vencen_anteriores_el=INICIO + timedelta(days=7), por=TOMAS
        )

    reloj.ahora = INICIO + timedelta(days=7) - timedelta(seconds=1)
    assert credenciales.autenticar(viejo.token).token_id == viejo.token_id
    assert credenciales.autenticar(nuevo.token).token_id == nuevo.token_id
    reloj.ahora = INICIO + timedelta(days=7)
    with pytest.raises(TokenInvalido):
        credenciales.autenticar(viejo.token)
    assert credenciales.autenticar(nuevo.token).token_id == nuevo.token_id



@pytest.mark.historia(153)
def test_rotar_no_programa_la_revocacion_de_un_token_ya_vencido(bd, credenciales, reloj, url_bd):
    agente_id = _agente(bd, credenciales)
    with bd.transaccion() as tx:
        vencido = credenciales.emitir(
            tx, agente_id=agente_id, por=TOMAS, vence_el=INICIO + timedelta(hours=1)
        )
    vigente = _emitir(bd, credenciales, agente_id)
    reloj.ahora = INICIO + timedelta(hours=2)

    with bd.transaccion() as tx:
        credenciales.rotar(
            tx, agente_id=agente_id, vencen_anteriores_el=reloj.ahora + timedelta(days=1), por=TOMAS
        )

    with psycopg.connect(url_bd) as conexion:
        revocados = conexion.execute(
            "SELECT token_prefijo FROM acervo.token_agente WHERE revocado_el IS NOT NULL"
        ).fetchall()
        eventos = conexion.execute(
            "SELECT despues->>'token_prefijo' FROM acervo.auditoria"
            " WHERE accion = 'agente.revocacion'"
        ).fetchall()
    assert revocados == eventos == [(vigente.prefijo,)]
    assert vencido.prefijo != vigente.prefijo

def test_rotar_con_un_solape_mayor_a_siete_dias_se_rechaza(bd, credenciales):
    agente_id = _agente(bd, credenciales)
    with pytest.raises(ValueError), bd.transaccion() as tx:
        credenciales.rotar(
            tx, agente_id=agente_id, vencen_anteriores_el=INICIO + timedelta(days=8), por=TOMAS
        )


def _ultimo_uso(url_bd: str, token_id: UUID) -> datetime | None:
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT ultimo_uso_el FROM acervo.token_agente WHERE token_id = %s", (token_id,)
        ).fetchone()
    assert fila is not None
    uso: datetime | None = fila[0]
    return uso


@pytest.mark.historia(154)
def test_el_ultimo_uso_avanza_a_lo_sumo_una_vez_por_minuto(bd, credenciales, reloj, url_bd):
    emitido = _emitir(bd, credenciales, _agente(bd, credenciales))
    assert _ultimo_uso(url_bd, emitido.token_id) is None

    credenciales.autenticar(emitido.token)
    assert _ultimo_uso(url_bd, emitido.token_id) == INICIO
    reloj.ahora = INICIO + timedelta(seconds=59)
    credenciales.autenticar(emitido.token)
    assert _ultimo_uso(url_bd, emitido.token_id) == INICIO
    reloj.ahora = INICIO + timedelta(seconds=61)
    credenciales.autenticar(emitido.token)
    assert _ultimo_uso(url_bd, emitido.token_id) == INICIO + timedelta(seconds=61)


@pytest.mark.historia(151)
def test_la_auditoria_y_los_logs_guardan_el_prefijo_y_nunca_el_token(
    bd, credenciales, url_bd, url_app, monkeypatch, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    agente_id = _agente(bd, credenciales)
    emitido = _emitir(bd, credenciales, agente_id)
    credenciales.autenticar(emitido.token)
    with bd.transaccion() as tx:
        credenciales.revocar_token(tx, emitido.token_id, por=TOMAS)
    with pytest.raises(TokenInvalido):
        credenciales.autenticar(emitido.token)
    # Y por los comandos: el token sale una vez por stdout, nunca por los logs ni la auditoría.
    monkeypatch.setenv("HV_BD_URL", url_app)
    main(["emitir-token", "Agente 2", "--alcances", "piezas:leer"])
    por_comando = _token_impreso(capsys.readouterr().out)
    main(["revocar-token", por_comando[:11]])

    eventos = RegistroDeAuditoria(bd=bd).listar(objeto=ObjetoRef("agente", str(agente_id)))
    assert [e.accion for e in reversed(eventos)] == [
        Accion.AGENTE_ALTA,
        Accion.AGENTE_EMISION,
        Accion.AGENTE_REVOCACION,
    ]
    assert eventos[0].despues["token_prefijo"] == emitido.prefijo
    assert eventos[1].despues["token_prefijo"] == emitido.prefijo
    assert all(e.actor == TOMAS for e in eventos)
    with psycopg.connect(url_bd) as conexion:
        [volcado] = conexion.execute(
            "SELECT string_agg(a::text, ' ') FROM acervo.auditoria a"
        ).fetchone()
    for token in (emitido.token, por_comando):
        secreto = token[12:]
        assert secreto not in volcado
        assert secreto not in caplog.text


@pytest.mark.historia(151)
@pytest.mark.parametrize(
    "sentencia",
    [
        "DELETE FROM acervo.token_agente",
        "DELETE FROM acervo.agente_externo",
        "UPDATE acervo.token_agente SET token_hash = 'otro'",
        "UPDATE acervo.token_agente SET vence_el = now()",
        "UPDATE acervo.agente_externo SET scopes = '{piezas:leer}'",
    ],
)
def test_hv_app_no_borra_y_solo_actualiza_ultimo_uso_y_revocacion(bd, credenciales, sentencia):
    _emitir(bd, credenciales, _agente(bd, credenciales))

    with pytest.raises(psycopg.errors.InsufficientPrivilege), bd.transaccion() as tx:
        tx.execute(sentencia)
    with bd.transaccion() as tx:
        tx.execute(
            "UPDATE acervo.token_agente SET ultimo_uso_el = now(), revocado_el = now(),"
            " revocado_por = NULL"
        )


def test_el_alcance_efemerides_leer_ya_existe_y_uno_desconocido_se_rechaza(bd, credenciales):
    with bd.transaccion() as tx:
        credenciales.crear_agente(
            tx, nombre="Agente 1", scopes=frozenset({Scope.EFEMERIDES_LEER}), por=TOMAS
        )
    with pytest.raises(psycopg.errors.CheckViolation), bd.transaccion() as tx:
        tx.execute(
            "INSERT INTO acervo.agente_externo (nombre, scopes)"
            " VALUES ('Otro', '{efemerides:escribir}')"
        )


@pytest.fixture
def al_dia(bd: Bd) -> Credenciales:
    """Con el reloj de verdad, el que usan los comandos."""
    return Credenciales(bd=bd, auditoria=RegistroDeAuditoria(bd=bd))


def _token_impreso(salida: str) -> str:
    [token] = re.findall(r"hv_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", salida)
    return token


def _freno(url_app: str) -> tuple[list[int], bool]:
    """Los ids de los eventos del interruptor y si la API de Agentes está encendida."""
    with psycopg.connect(url_app) as conexion:
        eventos = [e.evento_id for e in historial(conexion, limite=1000)]
        return eventos, estado(conexion)[Freno.API_AGENTES].habilitado


@pytest.mark.historia(218)
@pytest.mark.historia(151)
@pytest.mark.parametrize("api_encendida", [False, True])
def test_emitir_token_da_de_alta_al_agente_imprime_el_token_una_vez_y_no_toca_el_freno(
    bd, al_dia, url_app, monkeypatch, capsys, api_encendida
):
    monkeypatch.setenv("HV_BD_URL", url_app)
    if api_encendida:
        with bd.transaccion() as tx:
            cambiar(tx, por=TOMAS, cambios={Freno.API_AGENTES: True}, motivo="sesión con Juan")
    antes = _freno(url_app)

    codigo = main(["emitir-token", "Agente 1", "--alcances", "fragmentos:leer", "piezas:leer"])

    assert codigo == 0
    token = _token_impreso(capsys.readouterr().out)
    principal = al_dia.autenticar(token)
    assert principal.scopes == {Scope.FRAGMENTOS_LEER, Scope.PIEZAS_LEER}
    # DEC-136: emitir no deja eventos del interruptor; con la API apagada, `/v1` sigue en 503
    # (lo cablea 097).
    assert _freno(url_app) == antes
    assert antes[1] is api_encendida
    [emision] = RegistroDeAuditoria(bd=bd).listar(accion=Accion.AGENTE_EMISION)
    assert emision.actor == Actor(TipoDeActor.CLI, getpass.getuser())
    assert emision.despues == {"token_prefijo": principal.token_prefijo, "vence_el": ANY}


def test_emitir_token_a_un_agente_existente_emite_otro_sin_cambiar_alcances(
    al_dia, url_app, monkeypatch, capsys
):
    monkeypatch.setenv("HV_BD_URL", url_app)
    main(["emitir-token", "Agente 1", "--alcances", "fragmentos:leer"])
    primero = _token_impreso(capsys.readouterr().out)

    assert main(["emitir-token", "Agente 1"]) == 0
    segundo = _token_impreso(capsys.readouterr().out)
    assert main(["emitir-token", "Agente 1", "--alcances", "piezas:leer"]) == 2
    assert "alcances" in capsys.readouterr().err
    assert main(["emitir-token", "Agente 2"]) == 2
    assert "alcances" in capsys.readouterr().err

    assert al_dia.autenticar(primero) != al_dia.autenticar(segundo)
    assert al_dia.autenticar(segundo).scopes == {Scope.FRAGMENTOS_LEER}


@pytest.mark.historia(152)
def test_revocar_token_por_su_prefijo_rige_en_el_pedido_siguiente(
    al_dia, url_app, monkeypatch, capsys
):
    monkeypatch.setenv("HV_BD_URL", url_app)
    main(["emitir-token", "Agente 1", "--alcances", "fragmentos:leer"])
    token = _token_impreso(capsys.readouterr().out)
    prefijo = al_dia.autenticar(token).token_prefijo

    assert main(["revocar-token", prefijo]) == 0
    assert prefijo in capsys.readouterr().out
    with pytest.raises(TokenInvalido):
        al_dia.autenticar(token)
    assert main(["revocar-token", prefijo]) == 1
    assert main(["revocar-token", "hv_00000000"]) == 1
    capsys.readouterr()
    # El token entero pegado por error no se repite en la terminal.
    assert main(["revocar-token", token]) == 2
    assert token[12:] not in capsys.readouterr().err


def test_dos_altas_simultaneas_del_mismo_agente_dan_un_error_legible(
    url_app, monkeypatch, capsys
):
    monkeypatch.setenv("HV_BD_URL", url_app)
    main(["emitir-token", "Agente 1", "--alcances", "fragmentos:leer"])
    capsys.readouterr()
    # La otra alta ya confirmó entre la búsqueda por nombre y el INSERT de esta.
    monkeypatch.setattr(Credenciales, "agente_id", lambda self, tx, nombre: None)

    assert main(["emitir-token", "Agente 1", "--alcances", "fragmentos:leer"]) == 1
    salida = capsys.readouterr()
    assert "a la vez" in salida.err
    assert not re.search(r"hv_[0-9a-f]{8}_", salida.out)
