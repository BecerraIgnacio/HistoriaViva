"""La sesión de servidor (spec, módulo `identidad`; DEC-37): quién es la persona y hasta cuándo.

`/auth/dev` abre la sesión con un email (DEC-112) y la cookie lleva un id aleatorio cuyo
`sha256` es lo único que guarda la base. La suite abre sus sesiones por el mismo seam
(`aplicacion.entrar`), sin pasar por HTTP.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row, tuple_row

from aplicacion import CABECERAS, ORIGEN, app_sin_trabajador, entrar
from espera import esperar_detras_del_candado
from base_de_pruebas import CLAVE_APP
from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria, TipoDeActor
from historia_viva.api.app import crear_app
from historia_viva.composicion import Entorno, EntornoInvalido, componer_web, main
from historia_viva.dominio.errores import PermisoInsuficiente
from historia_viva.dominio.permisos import Permiso, Rol
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.identidad import (
    DominioNoPermitido,
    Padron,
    PersonaExistente,
    Principal,
    UltimoTitularDePermiso,
)
from historia_viva.identidad.oidc import ConfiguracionOIDC
from historia_viva.migraciones import aplicar
from proveedor_oidc_falso import CLIENTE_ID, EMISOR, SECRETO, ProveedorFalso
from test_lotes import LOTE
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, EDICION_COMPLETA, JORGE, MARIA

pytestmark = pytest.mark.historia(87)


@pytest.fixture
def app(url_app, capa_modelos, directorio_originales):
    return app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )


@pytest.mark.rnf("RNF-13")
@pytest.mark.parametrize("nombre", ["hv_persona", "hv_sesion"])
def test_seg_cookie_con_uuid_de_usuario_no_autentica(app, nombre):
    cliente = TestClient(app, headers=CABECERAS)
    cliente.cookies.set(nombre, MARIA)

    respuesta = cliente.get("/api/piezas")

    assert respuesta.status_code == 401
    assert respuesta.json()["type"] == "/errores/sin-sesion"


LOOPBACK = ("127.0.0.1", 50000)
"""El socket de un pedido hecho en la PC: `/auth/dev` atiende solo esos (DEC-85)."""
MARIA_EMAIL = "mrossi@fie.undef.edu.ar"


def _en_la_pc(app) -> TestClient:
    return TestClient(app, headers=CABECERAS, client=LOOPBACK)


def _ingresar(cliente: TestClient, email: str = MARIA_EMAIL):
    return cliente.post("/auth/dev", json={"email": email})


@pytest.mark.historia(228)
@pytest.mark.confirma("`/auth/dev` confirma la sesión y su `sesion.inicio` desde el pool de la app")
def test_seg_atributos_de_la_cookie_bajo_dev(app):
    cliente = _en_la_pc(app)

    respuesta = _ingresar(cliente)

    assert respuesta.status_code == 204
    cookie = respuesta.headers["set-cookie"]
    # Sin prefijo ni `Secure`: el acceso de desarrollo es HTTP en la PC (DEC-146 punto 17, R-35).
    assert cookie.startswith("hv_sesion=")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/" in cookie
    assert cliente.get("/api/yo").json()["email"] == MARIA_EMAIL


@pytest.mark.rnf("RNF-13")
@pytest.mark.confirma("`/auth/dev` confirma la sesión y su `sesion.inicio` desde el pool de la app")
def test_la_base_guarda_solo_el_hash_del_id_de_la_sesion(app, tx):
    cliente = _en_la_pc(app)
    _ingresar(cliente)
    sesion = cliente.cookies["hv_sesion"]

    filas = tx.execute(
        "SELECT sesion_id, s::text AS fila FROM acervo.sesion s WHERE usuario_id = %s",
        (MARIA,),
    ).fetchall()

    assert hashlib.sha256(sesion.encode()).hexdigest() in [fila[0] for fila in filas]
    assert not any(sesion in fila[1] for fila in filas)


@pytest.mark.rnf("RNF-13")
@pytest.mark.confirma("`/auth/dev` confirma la sesión y su `sesion.inicio` desde el pool de la app")
def test_seg_login_cambia_la_cookie(app):
    cliente = _en_la_pc(app)
    _ingresar(cliente)
    previa = cliente.cookies["hv_sesion"]

    _ingresar(cliente)

    nueva = cliente.cookies["hv_sesion"]
    assert nueva != previa
    assert cliente.get("/api/yo").status_code == 200
    cliente.cookies.set("hv_sesion", previa, domain="testserver.local")
    assert cliente.get("/api/yo").status_code == 401


@pytest.mark.historia(228)
@pytest.mark.parametrize(
    "variables",
    [
        {"HV_AUTH": "dev"},
        {"HV_AUTH": "dev", "HV_ENTORNO": "produccion"},
        {"HV_AUTH": "dev", "HV_ENTORNO": "desarrollo", "HV_OIDC_CLIENTE_ID": "cliente.apps"},
        {"HV_AUTH": "dev", "HV_ENTORNO": "desarrollo", "HV_OIDC_SECRETO": "secreto"},
    ],
    ids=["sin_entorno", "otro_entorno", "con_cliente_de_google", "con_secreto_de_google"],
)
def test_seg_dev_guarda_no_arranca_fuera_de_desarrollo_ni_con_google(variables):
    with pytest.raises(EntornoInvalido, match="HV_AUTH=dev"):
        Entorno.desde_variables(variables)


@pytest.mark.historia(228)
@pytest.mark.parametrize("direccion", ["192.168.0.10", "testclient"])
def test_seg_dev_guarda_rechaza_a_auth_dev_desde_otra_direccion(app, direccion):
    cliente = TestClient(app, headers=CABECERAS, client=(direccion, 50000))

    respuesta = _ingresar(cliente)

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/origen-no-permitido"
    assert "set-cookie" not in respuesta.headers


CADDY = ("192.0.2.80", 50000)
"""El socket de Caddy en el compose: no es loopback, pero Caddy se publica solo en
`127.0.0.1:80`."""


@pytest.mark.historia(228)
@pytest.mark.confirma("`/auth/dev` confirma la sesión y su `sesion.inicio` desde el pool de la app")
def test_seg_dev_guarda_atiende_lo_que_llega_por_caddy(
    url_app, capa_modelos, directorio_originales
):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        proxy=CADDY[0],
    )

    assert _ingresar(TestClient(app, headers=CABECERAS, client=CADDY)).status_code == 204
    otro = TestClient(app, headers=CABECERAS, client=("192.0.2.81", 50000))
    assert _ingresar(otro).status_code == 403


@pytest.mark.historia(228)
@pytest.mark.confirma("si `web` creyera en `X-Forwarded-For`, `/auth/dev` confirmaría la sesión")
def test_seg_dev_guarda_no_cree_en_x_forwarded_for(url_app, tmp_path, monkeypatch):
    """Quien llega a uvicorn sin pasar por Caddy no se hace pasar por la PC con la cabecera."""
    arrancada: dict = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **opciones: arrancada.update(app=app, **opciones)
    )
    for variable, valor in {
        "HV_BD_URL": url_app,
        "HV_ORIGINALES": str(tmp_path),
        "HV_MOTOR_URL": "http://127.0.0.1:1",
        "HV_AUTH": "dev",
        "HV_ENTORNO": "desarrollo",
        "HV_ORIGEN": ORIGEN,
    }.items():
        monkeypatch.setenv(variable, valor)
    assert main(["web", "--host", "0.0.0.0"]) == 0
    # La app como la sirve uvicorn, con el middleware que arma de sus opciones.
    configuracion = uvicorn.Config(**arrancada)
    configuracion.load()
    falsificada = TestClient(
        configuracion.loaded_app,
        headers={**CABECERAS, "X-Forwarded-For": "127.0.0.1"},
        client=("192.168.0.10", 50000),
    )
    try:
        respuesta = _ingresar(falsificada)
    finally:
        arrancada["app"].state.bd.cerrar()

    assert respuesta.status_code == 403
    assert "set-cookie" not in respuesta.headers


@pytest.mark.historia(228)
def test_seg_dev_guarda_sin_dev_auth_dev_no_existe(url_app, capa_modelos, directorio_originales):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        modo_de_ingreso=MetodoAutenticacion.OIDC,
    )

    respuesta = _ingresar(_en_la_pc(app))

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/ruta-inexistente"


@pytest.mark.historia(228)
def test_seg_dev_guarda_el_arranque_advierte(url_app, tmp_path, capsys):
    entorno = Entorno.desde_variables(
        {
            "HV_BD_URL": url_app,
            "HV_ORIGINALES": str(tmp_path),
            "HV_MOTOR_URL": "http://127.0.0.1:1",
            "HV_AUTH": "dev",
            "HV_ENTORNO": "desarrollo",
        }
    )

    componer_web(entorno).bd.cerrar()

    assert "ADVERTENCIA: autenticación de desarrollo activa" in capsys.readouterr().err


@pytest.mark.historia(228)
@pytest.mark.rnf("RNF-13")
@pytest.mark.confirma("`/auth/salir` confirma la marca y `sesion.cierre` desde el pool de la app")
def test_seg_cookie_oidc_con_prefijo_y_secure(url_app, capa_modelos, directorio_originales):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        modo_de_ingreso=MetodoAutenticacion.OIDC,
    )
    cliente = entrar(TestClient(app, headers=CABECERAS), MARIA)
    sesion = cliente.cookies["__Host-hv_sesion"]

    sin_prefijo = TestClient(app, headers=CABECERAS, cookies={"hv_sesion": sesion})
    assert sin_prefijo.get("/api/yo").status_code == 401
    assert cliente.get("/api/yo").json()["modo_auth"] == "oidc"
    salida = cliente.post("/auth/salir")

    assert salida.status_code == 204
    cookie = salida.headers["set-cookie"]
    assert cookie.startswith("__Host-hv_sesion=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/" in cookie


@pytest.mark.confirma("`/auth/salir` confirma la marca y `sesion.cierre` desde el pool de la app")
def test_cerrar_la_sesion_marca_revocada_el_sin_borrarla(app, tx):
    cliente = entrar(TestClient(app, headers=CABECERAS), MARIA)
    sesion = cliente.cookies["hv_sesion"]

    salida = cliente.post("/auth/salir")

    assert salida.status_code == 204
    assert entrar(TestClient(app, headers=CABECERAS), MARIA).get("/api/yo").status_code == 200
    assert TestClient(app, cookies={"hv_sesion": sesion}).get("/api/yo").status_code == 401
    revocada_el = tx.execute(
        "SELECT revocada_el FROM acervo.sesion WHERE sesion_id = %s",
        (hashlib.sha256(sesion.encode()).hexdigest(),),
    ).fetchone()
    assert revocada_el is not None
    assert revocada_el[0] is not None


class Reloj:
    """El reloj de la sesión, que el test adelanta."""

    def __init__(self) -> None:
        self.ahora = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.ahora

    def adelantar(self, **cuanto: float) -> None:
        self.ahora += timedelta(**cuanto)


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def cliente_con_reloj(url_app, capa_modelos, directorio_originales, reloj):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        reloj=reloj,
    )
    return entrar(TestClient(app, headers=CABECERAS), MARIA)


@pytest.mark.rnf("RNF-23")
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_seg_vencimiento_por_inactividad(cliente_con_reloj, reloj):
    reloj.adelantar(minutes=59)
    assert cliente_con_reloj.get("/api/yo").status_code == 200
    # El pedido anterior cuenta como actividad: la inactividad se mide desde ahí.
    reloj.adelantar(minutes=59)
    assert cliente_con_reloj.get("/api/yo").status_code == 200

    reloj.adelantar(minutes=61)

    respuesta = cliente_con_reloj.get("/api/yo")
    assert respuesta.status_code == 401
    assert respuesta.json()["type"] == "/errores/sin-sesion"


@pytest.mark.rnf("RNF-23")
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_seg_vencimiento_absoluto_a_las_ocho_horas(cliente_con_reloj, reloj):
    for _ in range(9):  # siempre activa: un pedido cada 50 minutos
        reloj.adelantar(minutes=50)
        assert cliente_con_reloj.get("/api/yo").status_code == 200

    reloj.adelantar(minutes=30)  # 8 h exactas desde que se abrió

    assert cliente_con_reloj.get("/api/yo").status_code == 401


@pytest.mark.rnf("RNF-23")
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_yo_dice_cuando_vence_la_sesion(cliente_con_reloj, reloj):
    abierta = reloj.ahora
    reloj.adelantar(minutes=5)

    yo = cliente_con_reloj.get("/api/yo").json()

    assert datetime.fromisoformat(yo["sesion_vence_el"]) == abierta + timedelta(hours=8)
    assert yo["inactividad_min"] == 60


@pytest.mark.confirma("la baja se confirma para que la vea la app; se deshace a mano")
def test_seg_baja_de_persona_corta_la_sesion(app, _conexion_del_dueno):
    sesiones = [entrar(TestClient(app, headers=CABECERAS), MARIA) for _ in range(2)]
    desactivar = "UPDATE acervo.usuario SET activo = %s WHERE usuario_id = %s"
    # La app lee desde su propio pool: la baja se confirma y se deshace a mano.
    _conexion_del_dueno.execute(desactivar, (False, MARIA))
    try:
        respuesta = sesiones[0].get("/api/piezas")
    finally:
        _conexion_del_dueno.execute(desactivar, (True, MARIA))

    assert respuesta.status_code == 401
    assert respuesta.json()["type"] == "/errores/sin-sesion"
    # Reactivar no devuelve las sesiones: las dos quedaron revocadas en ese pedido.
    assert [cliente.get("/api/yo").status_code for cliente in sesiones] == [401, 401]


@pytest.mark.rf("RF-34")
def test_sin_columna_de_contrasena(tx):
    columnas = [
        fila[0]
        for fila in tx.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'acervo' AND table_name IN ('usuario', 'sesion')"
        ).fetchall()
    ]

    assert columnas
    assert not [c for c in columnas if re.search("pass|contras|clave|hash_", c)]


@pytest.mark.confirma("`/auth/dev` confirma las sesiones y `sesion.inicio` desde el pool de la app")
def test_la_sexta_sesion_viva_revoca_la_mas_vieja(app):
    clientes = [_en_la_pc(app) for _ in range(6)]
    for cliente in clientes:
        assert _ingresar(cliente).status_code == 204

    assert [cliente.get("/api/yo").status_code for cliente in clientes] == [401] + [200] * 5


@pytest.mark.rnf("RNF-23")
@pytest.mark.confirma("`entrar` abre las sesiones desde el pool de la app")
def test_las_sesiones_vencidas_no_cuentan_para_el_tope(cliente_con_reloj, reloj):
    app = cliente_con_reloj.app
    reloj.adelantar(minutes=1)
    for _ in range(4):  # abiertas después y abandonadas
        entrar(TestClient(app, headers=CABECERAS), MARIA)
    reloj.adelantar(minutes=30)
    assert cliente_con_reloj.get("/api/yo").status_code == 200
    reloj.adelantar(minutes=31)  # las cuatro llevan 61 minutos sin actividad

    entrar(TestClient(app, headers=CABECERAS), MARIA)

    assert cliente_con_reloj.get("/api/yo").status_code == 200


@pytest.mark.rf("RF-45")
@pytest.mark.confirma("`/auth/dev` y `/auth/salir` confirman sus eventos desde el pool de la app")
def test_inicio_cierre_y_rechazo_quedan_auditados_sin_secretos(app):
    cliente = _en_la_pc(app)
    _ingresar(cliente)
    sesion = cliente.cookies["hv_sesion"]
    cliente.post("/auth/salir")
    rechazo = _ingresar(_en_la_pc(app), "Intrusa@Gmail.com ")

    assert rechazo.status_code == 422
    problema = rechazo.json()
    assert (problema["type"], problema["campo"]) == ("/errores/pedido-invalido", "email")
    auditoria = RegistroDeAuditoria(bd=app.state.bd)
    eventos = {e.accion: e for e in auditoria.listar(objeto=ObjetoRef("sesion"))}
    assert set(eventos) == {Accion.SESION_INICIO, Accion.SESION_CIERRE, Accion.SESION_RECHAZO}
    for accion in (Accion.SESION_INICIO, Accion.SESION_CIERRE):
        assert eventos[accion].actor.usuario_id == UUID(MARIA)
    assert eventos[Accion.SESION_INICIO].despues == {"metodo": "dev"}
    rechazado = eventos[Accion.SESION_RECHAZO]
    assert rechazado.motivo == "persona_no_habilitada"
    assert rechazado.actor.etiqueta == (
        "gmail.com#" + hashlib.sha256(b"intrusa@gmail.com").hexdigest()[:12]
    )
    registrado = repr(list(eventos.values())).lower()
    assert sesion.lower() not in registrado
    assert hashlib.sha256(sesion.encode()).hexdigest() not in registrado
    assert "intrusa@" not in registrado
    assert "id_token" not in registrado


def _rutas(app) -> list[tuple[str, str]]:
    """Cada `(método, ruta)` de la API, según su OpenAPI."""
    return [
        (metodo.upper(), ruta)
        for ruta, metodos in app.openapi()["paths"].items()
        for metodo in metodos
    ]


def _mutaciones(app) -> list[tuple[str, str]]:
    """Toda ruta con un método que no es `GET` ni `HEAD`, con sus parámetros llenos."""
    return [
        (metodo, re.sub(r"\{[^}]+\}", str(uuid4()), ruta))
        for metodo, ruta in _rutas(app)
        # `/v1` va con Bearer y sin cookie: el CSRF no le toca (131).
        if metodo not in {"GET", "HEAD"} and not ruta.startswith("/v1/")
    ]


@pytest.mark.rnf("RNF-13")
@pytest.mark.parametrize(
    "cabeceras",
    [{}, {"Origin": ORIGEN}, {"X-HV": "1"}, {"X-HV": "1", "Origin": "http://evil.example"},
     {"X-HV": "0", "Origin": ORIGEN}, {"X-HV": "1", "Origin": "null"}],
    ids=["nada", "sin_x_hv", "sin_origen", "origen_ajeno", "x_hv_distinto", "origen_nulo"],
)
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_seg_csrf_toda_mutacion_exige_el_origen_propio_y_x_hv(app, cabeceras):
    cliente = entrar(TestClient(app, client=LOOPBACK), MARIA)
    mutaciones = _mutaciones(app)

    respuestas = {
        (metodo, ruta): cliente.request(metodo, ruta, headers=cabeceras, json={})
        for metodo, ruta in mutaciones
    }

    assert ("POST", "/auth/dev") in {(m, r) for m, r in mutaciones}
    assert {clave: r.status_code for clave, r in respuestas.items()} == dict.fromkeys(
        respuestas, 403
    )
    assert {r.json()["type"] for r in respuestas.values()} == {"/errores/origen-no-permitido"}


@pytest.mark.rnf("RNF-13")
def test_seg_csrf_se_mira_antes_que_el_cuerpo(app):
    """Un JSON malformado sin `Origin` ni `X-HV` no llega a leerse: 403, no 422."""
    respuesta = TestClient(app, client=LOOPBACK).post(
        "/auth/dev", content=b"{", headers={"Content-Type": "application/json"}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/origen-no-permitido"


@pytest.mark.rnf("RNF-21")
def test_seg_csrf_el_rechazo_queda_en_el_log_sin_el_origen(app, caplog):
    """SEG-07 (165): el 403 deja rastro con método y ruta; el `Origin` lo escribe quien ataca."""
    with caplog.at_level(logging.WARNING, logger="historia_viva.api.sesion"):
        respuesta = TestClient(app, client=LOOPBACK).post(
            "/auth/dev", json={}, headers={"Origin": "http://evil.example", "X-HV": "1"}
        )

    assert respuesta.status_code == 403
    assert [r.getMessage() for r in caplog.records] == ["CSRF rechazado: POST /auth/dev"]
    assert "evil.example" not in caplog.text


def _foto(tx) -> dict[str, str]:
    """Una huella de cada tabla del Acervo; `sesion` no, que anota la actividad, ni los
    `sesion.rechazo` de `auditoria`: un 403 deja constancia (032) y no cambia nada."""
    tablas = [
        fila[0]
        for fila in tx.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'acervo'"
            " AND table_type = 'BASE TABLE' AND table_name <> 'sesion'"
        ).fetchall()
    ]
    return {
        tabla: tx.execute(
            sql.SQL(
                "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), ''))"
                " FROM acervo.{} t{}"
            ).format(
                sql.Identifier(tabla),
                sql.SQL(" WHERE accion <> 'sesion.rechazo'" if tabla == "auditoria" else ""),
            )
        ).fetchone()[0]
        for tabla in tablas
    }


@pytest.mark.rnf("RNF-13")
@pytest.mark.confirma("la Pieza la sube y la procesa la app con su propio pool")
def test_seg_csrf_ningun_get_muta(cliente, pieza_pendiente, tx):
    lote_id = cliente.post("/api/lotes", json=LOTE).json()["lote"]["lote_id"]
    # Los demás parámetros van con un id que no existe: un 404 tampoco puede mutar.
    rutas = [
        re.sub(
            r"\{[^}]+\}",
            str(uuid4()),
            ruta.replace("{pieza_id}", pieza_pendiente).replace("{lote_id}", lote_id),
        )
        for metodo, ruta in _rutas(cliente.app)
        if metodo == "GET"
    ]
    antes = _foto(tx)

    for persona in (MARIA, ADMINISTRADOR_TECNICO):
        entrar(cliente, persona)
        for ruta in rutas:
            cliente.get(ruta)

    assert _foto(tx) == antes


@pytest.mark.rnf("RNF-13")
def test_seg_csrf_sin_cors(app):
    respuesta = TestClient(app).options(
        "/api/piezas",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in respuesta.headers
    assert not [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert respuesta.status_code == 405
    assert respuesta.json()["type"] == "/errores/metodo-no-permitido"
    assert "POST" in respuesta.headers["allow"]


@pytest.mark.rnf("RNF-13")
def test_seg_csrf_no_mira_fuera_de_api_y_auth(app):
    """`/v1` va con Bearer, sin cookie: el CSRF no le corresponde (spec §CSRF)."""
    respuesta = TestClient(app, client=LOOPBACK).post("/v1/cualquiera", json={})

    # Sin `Origin` ni `X-HV` no es el 403 del CSRF: es el 401 de `/v1` sin token (097).
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "token_invalido")


@pytest.mark.rnf("RNF-07")
@pytest.mark.parametrize("metodo", list(MetodoAutenticacion))
@pytest.mark.confirma("la Pieza la sube, la procesa y la firma la app con su propio pool")
def test_seg_firma_ensayo_la_firma_guarda_el_metodo_de_la_sesion(
    cliente, pieza_pendiente, tx, metodo
):
    entrar(cliente, MARIA, metodo)

    respuesta = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    assert respuesta.json()["estado"] == "Validada"
    firmada = tx.execute(
        "SELECT metodo_autenticacion FROM acervo.ficha WHERE pieza_id = %s AND vigente",
        (pieza_pendiente,),
    ).fetchone()
    # Una sesión `dev` deja una firma de ensayo; una OIDC, no (DEC-139).
    assert firmada == (metodo.value,)


@pytest.mark.rnf("RNF-07")
@pytest.mark.parametrize("metodo", list(MetodoAutenticacion))
@pytest.mark.confirma("la Pieza la sube, la procesa y la firma la app con su propio pool")
def test_seg_firma_ensayo_la_ficha_dice_con_que_metodo_se_firmo(cliente, pieza_pendiente, metodo):
    entrar(cliente, MARIA, metodo)
    antes = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    guardado = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA).json()

    # Con esto la pantalla marca «Firma de ensayo, identidad no verificada» (DEC-139).
    despues = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert antes["metodo_autenticacion"] is None  # la v1 la propuso el modelo
    assert despues["metodo_autenticacion"] == guardado["metodo_autenticacion"] == metodo.value


@pytest.mark.historia(228)
def test_la_entrada_pide_el_email_sin_selector_de_personas(app):
    respuesta = TestClient(app).get("/")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert 'type="email"' in respuesta.text
    assert "¿Quién sos?" not in respuesta.text
    assert "/api/personas" not in respuesta.text


@pytest.mark.historia(228)
@pytest.mark.parametrize(("metodo", "ruta"), [("GET", "/api/personas"), ("POST", "/api/sesion")])
def test_quien_sos_ya_no_existe(app, metodo, ruta):
    respuesta = _en_la_pc(app).request(metodo, ruta, json={"persona_id": MARIA})

    assert respuesta.status_code == 404
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    assert respuesta.json()["type"] == "/errores/ruta-inexistente"


@pytest.mark.historia(228)
def test_ingresar_sin_email_responde_422_legible_en_espanol(app):
    respuesta = _en_la_pc(app).post("/auth/dev", json={})

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert (problema["type"], problema["campo"]) == ("/errores/pedido-invalido", "email")
    assert "Field required" not in problema["detail"]
    assert "set-cookie" not in respuesta.headers


@pytest.mark.rnf("RNF-13")
def test_subir_sin_sesion_responde_401_sin_sesion(app):
    respuesta = TestClient(app, headers=CABECERAS).post(
        "/api/piezas", files={"archivo": ("acta.pdf", b"%PDF-1.4", "application/pdf")}
    )

    assert respuesta.status_code == 401
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    assert respuesta.json()["type"] == "/errores/sin-sesion"


@pytest.mark.rf("RF-53")
@pytest.mark.parametrize(
    ("persona", "rol", "permisos"),
    [
        (DIRECTORA_DE_CARRERA, "director_carrera", ["CONSULTAR"]),
        (ADMINISTRADOR_TECNICO, "admin_tecnico", ["CONSULTAR", "OPERAR_SISTEMA"]),
    ],
)
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_yo_trae_el_rol_y_los_permisos_para_que_la_pantalla_oculte_lo_que_no_puede(
    app, persona, rol, permisos
):
    yo = entrar(TestClient(app), persona).get("/api/yo").json()

    assert (yo["usuario_id"], yo["rol"], yo["permisos"], yo["modo_auth"]) == (
        persona, rol, permisos, "dev"
    )


# --- Padrón: quién otorga qué, último titular e interinato (R-01 a R-03), en proceso -----------

DOMINIOS = ("fie.undef.edu.ar",)
OPERADOR = Actor(TipoDeActor.CLI, "operador")
"""Quien corre `alta-persona` en la PC: el comando puede todo, incluso el primer técnico."""


@pytest.fixture
def tx_de_la_app(tx):
    """`tx` con las filas como diccionarios, como las entrega `bd`."""
    tx.row_factory = dict_row
    yield tx
    tx.row_factory = tuple_row


@pytest.fixture
def padron(app):
    return Padron(
        auditoria=app.state.auditoria, sesiones=app.state.sesiones, dominios_permitidos=DOMINIOS
    )


def _principal(usuario_id: str, rol: Rol, *, interino: bool = False) -> Principal:
    return Principal(
        usuario_id=UUID(usuario_id), nombre="", email=f"{usuario_id[:4]}@fie.undef.edu.ar",
        rol=rol, metodo=MetodoAutenticacion.DEV, vence_el=datetime.now(UTC), interino=interino,
    )


@pytest.mark.historia(148)
def test_alta_repetida_u_otro_dominio_se_rechazan(padron, tx_de_la_app):
    with pytest.raises(PersonaExistente):
        padron.alta(tx_de_la_app, " MRossi@fie.undef.edu.ar", "Otra", Rol.COORDINADOR, por=OPERADOR)
    with pytest.raises(DominioNoPermitido):
        padron.alta(tx_de_la_app, "alguien@gmail.com", "Alguien", Rol.COORDINADOR, por=OPERADOR)


@pytest.mark.historia(147)
def test_r02_solo_un_tecnico_otorga_admin_tecnico(padron, tx_de_la_app):
    rgc = _principal(MARIA, Rol.RGC)
    tecnico = _principal(ADMINISTRADOR_TECNICO, Rol.ADMIN_TECNICO, interino=True)

    with pytest.raises(PermisoInsuficiente):
        padron.alta(tx_de_la_app, "otro@fie.undef.edu.ar", "Otro", Rol.ADMIN_TECNICO, por=rgc)
    with pytest.raises(PermisoInsuficiente):
        padron.cambiar_rol(tx_de_la_app, UUID(ADMINISTRADOR_TECNICO), Rol.RGC, por=rgc)
    for numero, rol in enumerate((Rol.RGC, Rol.COORDINADOR, Rol.DIRECTOR_CARRERA)):
        padron.alta(tx_de_la_app, f"p{numero}@fie.undef.edu.ar", "P", rol, por=rgc)
    # Con RGC activo, el técnico no tiene GESTIONAR_PERSONAS: no da de alta a nadie.
    with pytest.raises(PermisoInsuficiente):
        padron.alta(
            tx_de_la_app, "x@fie.undef.edu.ar", "X", Rol.DIRECTOR_CARRERA,
            por=_principal(ADMINISTRADOR_TECNICO, Rol.ADMIN_TECNICO),
        )
    # El interinato se mira en la base (111): sin RGC activo, el técnico interino lo otorga.
    tx_de_la_app.execute("UPDATE acervo.usuario SET activo = false WHERE rol = 'rgc'")
    nuevo = padron.alta(
        tx_de_la_app, "otro@fie.undef.edu.ar", "Otro", Rol.ADMIN_TECNICO, por=tecnico
    )
    assert (nuevo.rol, nuevo.activo) == (Rol.ADMIN_TECNICO, True)


@pytest.mark.historia(147)
def test_r02_el_interinato_se_mira_en_la_transaccion_del_cambio(padron, tx_de_la_app):
    # La sesión se validó con el interinato vigente, pero en la base ya hay RGC activo (María):
    # un pedido en vuelo mientras se daba de alta al primer RGC no otorga `admin_tecnico`.
    tecnico = _principal(ADMINISTRADOR_TECNICO, Rol.ADMIN_TECNICO, interino=True)

    with pytest.raises(PermisoInsuficiente):
        padron.alta(tx_de_la_app, "otro@fie.undef.edu.ar", "Otro", Rol.ADMIN_TECNICO, por=tecnico)
    with pytest.raises(PermisoInsuficiente):
        padron.cambiar_rol(tx_de_la_app, UUID(JORGE), Rol.ADMIN_TECNICO, por=tecnico)


@pytest.mark.historia(150)
@pytest.mark.parametrize(
    ("persona", "cambio", "permiso"),
    [
        (ADMINISTRADOR_TECNICO, None, Permiso.OPERAR_SISTEMA),
        (ADMINISTRADOR_TECNICO, Rol.COORDINADOR, Permiso.OPERAR_SISTEMA),
        (MARIA, None, Permiso.GESTIONAR_PERSONAS),
        (MARIA, Rol.COORDINADOR, Permiso.GESTIONAR_PERSONAS),
    ],
    ids=["baja_tecnico", "degradar_tecnico", "baja_rgc", "degradar_rgc"],
)
def test_r03_el_ultimo_titular_no_se_baja_ni_se_degrada(
    padron, tx_de_la_app, persona, cambio, permiso
):
    with pytest.raises(UltimoTitularDePermiso) as rechazo:
        if cambio is None:
            padron.desactivar(tx_de_la_app, UUID(persona), por=OPERADOR)
        else:
            padron.cambiar_rol(tx_de_la_app, UUID(persona), cambio, por=OPERADOR)

    assert rechazo.value.permiso is permiso


@pytest.mark.historia(150)
def test_r03_con_dos_titulares_la_primera_baja_pasa_y_la_segunda_no(padron, tx_de_la_app):
    otro = padron.alta(
        tx_de_la_app, "otro@fie.undef.edu.ar", "Otro", Rol.ADMIN_TECNICO, por=OPERADOR
    )

    padron.desactivar(tx_de_la_app, UUID(ADMINISTRADOR_TECNICO), por=OPERADOR)
    with pytest.raises(UltimoTitularDePermiso):
        padron.desactivar(tx_de_la_app, otro.usuario_id, por=OPERADOR)


@pytest.mark.historia(281)
def test_r01_el_alta_del_primer_rgc_termina_el_interinato_y_queda_auditado(
    padron, tx_de_la_app
):
    tx_de_la_app.execute("UPDATE acervo.usuario SET activo = false WHERE usuario_id = %s", (MARIA,))

    primera = padron.alta(tx_de_la_app, "rgc@fie.undef.edu.ar", "La RGC", Rol.RGC, por=OPERADOR)
    segunda = padron.alta(tx_de_la_app, "rgc2@fie.undef.edu.ar", "Otra RGC", Rol.RGC, por=OPERADOR)

    despues = {
        fila["objeto_id"]: fila["despues"]
        for fila in tx_de_la_app.execute(
            "SELECT objeto_id, despues FROM acervo.auditoria WHERE accion = 'persona.alta'"
        )
    }
    assert despues[str(primera.usuario_id)]["interinato_terminado"] is True
    assert "interinato_terminado" not in despues[str(segunda.usuario_id)]


# --- `alta-persona`: el comando de quien opera, como `hv_app` (DEC-105, DEC-113) --------------


@pytest.fixture
def alta_persona(monkeypatch):
    """Corre `alta-persona` contra la base que se le diga (un DSN de `hv_app`)."""

    def correr(url_app: str, *argumentos: str) -> int:
        monkeypatch.setenv("HV_BD_URL", url_app)
        return main(["alta-persona", *argumentos])

    return correr


@pytest.mark.historia(147)
def test_alta_persona_crea_el_primer_tecnico_en_una_base_sin_personas(
    alta_persona, base_vacia, capsys
):
    aplicar(base_vacia)  # sin la semilla de desarrollo: como en el servidor (DEC-52)
    app = make_conninfo(base_vacia, user="hv_app", password=CLAVE_APP)
    tecnico = "tecnico@fie.undef.edu.ar"

    creado = alta_persona(
        app, "--email", "Tecnico@fie.undef.edu.ar", "--nombre", "Primer Técnico",
        "--rol", "admin_tecnico",
    )
    repetido = alta_persona(app, "--email", tecnico, "--nombre", "Otra", "--rol", "rgc")
    repetido_err = capsys.readouterr().err
    ajeno = alta_persona(app, "--email", "x@gmail.com", "--nombre", "X", "--rol", "rgc")
    ajeno_err = capsys.readouterr().err
    baja = alta_persona(app, "--email", tecnico, "--inactiva")
    baja_err = capsys.readouterr().err

    assert (creado, repetido, ajeno, baja) == (0, 1, 1, 1)
    assert repetido_err.startswith("persona-existente")
    assert ajeno_err.startswith("dominio-no-permitido")
    # Sin RGC, el técnico es también el único con GESTIONAR_PERSONAS (interino), y se mira antes.
    assert baja_err.startswith("ultimo-titular-de-permiso") and "GESTIONAR_PERSONAS" in baja_err
    with psycopg.connect(base_vacia) as conexion:
        assert conexion.execute(
            "SELECT email, rol, activo FROM acervo.usuario"
        ).fetchall() == [(tecnico, "admin_tecnico", True)]
        assert conexion.execute(
            "SELECT actor_tipo, accion FROM acervo.auditoria"
        ).fetchall() == [("cli", "persona.alta")]


@pytest.mark.historia(149)
@pytest.mark.confirma("el comando confirma la baja y la reactivación desde su propia conexión")
def test_alta_persona_inactiva_corta_el_acceso_y_reactivar_lo_devuelve(
    app, url_app, alta_persona, padron_restaurado
):
    sesion = entrar(TestClient(app, headers=CABECERAS), JORGE)

    baja = alta_persona(url_app, "--email", "jperalta@fie.undef.edu.ar", "--inactiva")
    cortada = sesion.get("/api/yo").status_code
    reactivada = alta_persona(url_app, "--email", "jperalta@fie.undef.edu.ar", "--activa")
    otra_vez = entrar(TestClient(app, headers=CABECERAS), JORGE).get("/api/yo").status_code

    assert (baja, cortada, reactivada, otra_vez) == (0, 401, 0, 200)


@pytest.mark.historia(146)
@pytest.mark.confirma("el comando confirma el alta y el cambio de rol desde su propia conexión")
def test_cambio_de_rol_a_director_carrera_corta_admin_en_el_pedido_siguiente(
    app, url_app, alta_persona, padron_restaurado
):
    email = "otro.tecnico@fie.undef.edu.ar"
    alta_persona(url_app, "--email", email, "--nombre", "Otro Técnico", "--rol", "admin_tecnico")
    with app.state.bd.transaccion() as tx:
        persona = Padron(
            auditoria=app.state.auditoria, sesiones=app.state.sesiones, dominios_permitidos=()
        ).buscar(tx, email)
    assert persona is not None
    cliente = entrar(TestClient(app, headers=CABECERAS), str(persona.usuario_id))
    antes = cliente.get("/api/admin/auditoria").status_code

    cambio = alta_persona(url_app, "--email", email, "--rol", "director_carrera")
    despues = cliente.get("/api/admin/auditoria").status_code
    yo = cliente.get("/api/yo").json()

    assert (antes, cambio, despues) == (200, 0, 403)
    assert (yo["rol"], yo["permisos"]) == ("director_carrera", ["CONSULTAR"])


@pytest.mark.historia(281)
@pytest.mark.confirma("el comando confirma el alta del primer RGC desde su propia conexión")
def test_interinato_el_tecnico_lo_pierde_con_el_alta_del_primer_rgc(
    app, url_app, alta_persona, sin_rgc
):
    tecnico = entrar(TestClient(app, headers=CABECERAS), ADMINISTRADOR_TECNICO)
    interino = set(tecnico.get("/api/yo").json()["permisos"])

    alta = alta_persona(
        url_app, "--email", "rgc@fie.undef.edu.ar", "--nombre", "La RGC", "--rol", "rgc"
    )
    despues = set(tecnico.get("/api/yo").json()["permisos"])
    [evento] = tecnico.get(
        "/api/admin/auditoria", params={"accion": "persona.alta"}
    ).json()["eventos"]

    assert interino == {"CONSULTAR", "OPERAR_SISTEMA", "GESTIONAR_PERSONAS", "RETIRAR_PIEZAS"}
    assert (alta, despues) == (0, {"CONSULTAR", "OPERAR_SISTEMA"})
    assert evento["detalle"]["despues"]["interinato_terminado"] is True


@pytest.mark.historia(280)
def test_nadie_se_cambia_el_rol_ni_se_da_de_baja_a_si_mismo(padron, tx_de_la_app):
    tx_de_la_app.execute("UPDATE acervo.usuario SET activo = false WHERE rol = 'rgc'")
    otro = padron.alta(
        tx_de_la_app, "otro@fie.undef.edu.ar", "Otro", Rol.ADMIN_TECNICO, por=OPERADOR
    )
    tecnico = _principal(ADMINISTRADOR_TECNICO, Rol.ADMIN_TECNICO, interino=True)

    # Con dos técnicos R-03 no lo frenaría: el interino se haría RGC y firmaría.
    with pytest.raises(PermisoInsuficiente):
        padron.cambiar_rol(tx_de_la_app, UUID(ADMINISTRADOR_TECNICO), Rol.RGC, por=tecnico)
    with pytest.raises(PermisoInsuficiente):
        padron.desactivar(tx_de_la_app, UUID(ADMINISTRADOR_TECNICO), por=tecnico)
    # A otro sí: R-02 no lo prohíbe y queda auditado.
    padron.cambiar_rol(tx_de_la_app, otro.usuario_id, Rol.RGC, por=tecnico)


@pytest.mark.historia(148)
def test_alta_rechaza_un_email_con_dos_arrobas(padron, tx_de_la_app):
    with pytest.raises(DominioNoPermitido):
        padron.alta(tx_de_la_app, "a@b@fie.undef.edu.ar", "A", Rol.COORDINADOR, por=OPERADOR)


@pytest.mark.historia(148)
@pytest.mark.parametrize("valor", [None, "", " , "])
def test_dominios_permitidos_vacios_caen_al_de_la_fie(valor):
    variables = {} if valor is None else {"HV_DOMINIOS_PERMITIDOS": valor}

    assert Entorno.desde_variables(variables).dominios_permitidos == ("fie.undef.edu.ar",)


@pytest.mark.historia(148)
def test_dominios_permitidos_separados_por_comas():
    entorno = Entorno.desde_variables({"HV_DOMINIOS_PERMITIDOS": " FIE.undef.edu.ar, ,undef.edu.ar"})

    assert entorno.dominios_permitidos == ("fie.undef.edu.ar", "undef.edu.ar")


@pytest.mark.historia(281)
def test_lo_que_el_tecnico_hace_por_el_interinato_lleva_la_marca_interino(padron, tx_de_la_app):
    tecnico = _principal(ADMINISTRADOR_TECNICO, Rol.ADMIN_TECNICO, interino=True)

    de_la_rgc = padron.alta(
        tx_de_la_app, "b@fie.undef.edu.ar", "B", Rol.COORDINADOR, por=_principal(MARIA, Rol.RGC)
    )
    por_comando = padron.alta(tx_de_la_app, "c@fie.undef.edu.ar", "C", Rol.COORDINADOR, por=OPERADOR)
    tx_de_la_app.execute("UPDATE acervo.usuario SET activo = false WHERE usuario_id = %s", (MARIA,))
    # Sin RGC activo, también lo que hace el comando es interino (runbook R4).
    comando_interino = padron.alta(
        tx_de_la_app, "d@fie.undef.edu.ar", "D", Rol.COORDINADOR, por=OPERADOR
    )
    # El interinato se mira en la base (111): el técnico gestiona recién sin RGC activo.
    interina = padron.alta(tx_de_la_app, "a@fie.undef.edu.ar", "A", Rol.COORDINADOR, por=tecnico)

    despues = {
        fila["objeto_id"]: fila["despues"]
        for fila in tx_de_la_app.execute(
            "SELECT objeto_id, despues FROM acervo.auditoria WHERE accion = 'persona.alta'"
        )
    }
    assert despues[str(interina.usuario_id)]["interino"] is True
    assert "interino" not in despues[str(de_la_rgc.usuario_id)]
    assert "interino" not in despues[str(por_comando.usuario_id)]
    assert despues[str(comando_interino.usuario_id)]["interino"] is True


# --- Ingreso con Google contra el proveedor falso (036; spec, «Pasos del OIDC») ---

EXTRANO = "persona-ajena@gmail.com"


@pytest.fixture(scope="module")
def proveedor() -> ProveedorFalso:
    return ProveedorFalso()


@pytest.fixture
def app_oidc(url_app, capa_modelos, directorio_originales, proveedor, padron_restaurado, reloj):
    return app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        modo_de_ingreso=MetodoAutenticacion.OIDC,
        reloj=reloj,
        oidc=ConfiguracionOIDC(
            emisor=EMISOR,
            cliente_id=CLIENTE_ID,
            secreto=SECRETO,
            redireccion="https://testserver/auth/callback",
            clave_del_intento="clave-de-prueba-del-intento",
            dominios=("fie.undef.edu.ar",),
            http=TestClient(proveedor.app, base_url=EMISOR),
        ),
    )


@pytest.fixture
def navegador(app_oidc) -> TestClient:
    """Por HTTPS: las cookies `__Host-` llevan `Secure` y el navegador no las manda por HTTP."""
    return TestClient(app_oidc, base_url="https://testserver", follow_redirects=False)


def _con_google(navegador: TestClient, proveedor: ProveedorFalso, *, estado=None, **identidad):
    """El ida y vuelta de un ingreso: la app manda al proveedor, la persona elige su cuenta
    (`identidad` es la query de `/autorizar` del doble) y el proveedor vuelve al callback."""
    ida = navegador.get("/auth/google")
    assert ida.status_code == 302, ida.text
    autorizacion = TestClient(proveedor.app, base_url=EMISOR).get(
        str(httpx.URL(ida.headers["location"]).copy_merge_params(identidad)),
        follow_redirects=False,
    )
    callback = httpx.URL(autorizacion.headers["location"])
    if estado is not None:
        callback = callback.copy_set_param("state", estado)
    return navegador.get(str(callback))


def _sub_vinculado(tx, persona: str) -> str | None:
    return tx.execute(
        "SELECT sub_google FROM acervo.usuario WHERE usuario_id = %s", (persona,)
    ).fetchone()[0]


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el callback confirma el vínculo y la sesión desde el pool de la app")
def test_el_primer_ingreso_con_google_vincula_por_email_y_abre_sesion_oidc(
    navegador, proveedor, tx
):
    vuelta = _con_google(navegador, proveedor, email=MARIA_EMAIL, sub="sub-de-maria")

    assert (vuelta.status_code, vuelta.headers["location"]) == (302, "/")
    cookies = vuelta.headers.get_list("set-cookie")
    [sesion] = [c for c in cookies if c.startswith("__Host-hv_sesion=")]
    assert "Secure" in sesion and "HttpOnly" in sesion and "samesite=lax" in sesion.lower()
    # La cookie del intento se borra al volver.
    assert any(c.startswith('__Host-hv_intento=""') or "Max-Age=0" in c
               for c in cookies if c.startswith("__Host-hv_intento="))
    yo = navegador.get("/api/yo").json()
    assert (yo["email"], yo["modo_auth"]) == (MARIA_EMAIL, "oidc")
    assert _sub_vinculado(tx, MARIA) == "sub-de-maria"
    [inicio] = RegistroDeAuditoria(bd=navegador.app.state.bd).listar(accion=Accion.SESION_INICIO)
    assert (inicio.actor.usuario_id, inicio.despues) == (UUID(MARIA), {"metodo": "oidc"})


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el callback confirma el vínculo y la sesión desde el pool de la app")
def test_vinculada_por_sub_entra_aunque_cambie_el_email(navegador, proveedor, tx):
    _con_google(navegador, proveedor, email=MARIA_EMAIL, sub="sub-de-maria")
    navegador.cookies.clear()

    vuelta = _con_google(navegador, proveedor, email="maria.rossi@fie.undef.edu.ar",
                         sub="sub-de-maria")

    assert vuelta.headers["location"] == "/"
    assert navegador.get("/api/yo").json()["email"] == MARIA_EMAIL
    assert _sub_vinculado(tx, MARIA) == "sub-de-maria"


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el primer ingreso confirma el vínculo desde el pool de la app")
def test_seg_sub_distinto(navegador, proveedor, tx):
    _con_google(navegador, proveedor, email=MARIA_EMAIL, sub="sub-de-maria")
    navegador.cookies.clear()

    vuelta = _con_google(navegador, proveedor, email=MARIA_EMAIL, sub="sub-de-otra-cuenta")

    assert vuelta.headers["location"] == "/?error=persona-no-habilitada"
    assert navegador.get("/api/yo").status_code == 401
    assert _sub_vinculado(tx, MARIA) == "sub-de-maria"
    [rechazo] = RegistroDeAuditoria(bd=navegador.app.state.bd).listar(accion=Accion.SESION_RECHAZO)
    assert rechazo.motivo == "sub_distinto"
    assert MARIA_EMAIL not in rechazo.actor.etiqueta


RECHAZOS = {
    "otro_dominio": ({"email": EXTRANO}, "dominio-no-permitido", "dominio_no_permitido"),
    "hd_ajeno": ({"email": MARIA_EMAIL, "hd_reclamado": "otra.edu.ar"}, "dominio-no-permitido",
                 "dominio_no_permitido"),
    "hd_de_la_fie_con_email_ajeno": ({"email": EXTRANO, "hd_reclamado": "fie.undef.edu.ar"},
                                     "dominio-no-permitido", "dominio_no_permitido"),
    "sin_hd": ({"email": MARIA_EMAIL, "hd_reclamado": ""}, "dominio-no-permitido",
               "dominio_no_permitido"),
    "sin_alta": ({"email": "nadie@fie.undef.edu.ar"}, "persona-no-habilitada",
                 "persona_no_habilitada"),
    "email_sin_verificar": ({"email": MARIA_EMAIL, "verificado": "false"}, "cuenta-no-verificada",
                            "cuenta_no_verificada"),
    "nonce_invalido": ({"email": MARIA_EMAIL, "trampa": "nonce"}, "intento-vencido", None),
    "firma_ajena": ({"email": MARIA_EMAIL, "trampa": "firma"}, "intento-vencido", None),
    "iss_ajeno": ({"email": MARIA_EMAIL, "trampa": "iss"}, "intento-vencido", None),
    "aud_ajeno": ({"email": MARIA_EMAIL, "trampa": "aud"}, "intento-vencido", None),
    "vencido": ({"email": MARIA_EMAIL, "trampa": "exp"}, "intento-vencido", None),
    "sin_iat": ({"email": MARIA_EMAIL, "trampa": "sin_iat"}, "intento-vencido", None),
    "aud_lista_sin_azp": ({"email": MARIA_EMAIL, "trampa": "aud_lista"}, "intento-vencido", None),
}
"""Lo que manda la cuenta → el `?error=` de la Entrada (spec §3.3) y el motivo de
`sesion.rechazo` (los de antes de validar el token no se auditan)."""


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("un rechazo confirma su `sesion.rechazo` desde el pool de la app")
@pytest.mark.parametrize("caso", list(RECHAZOS))
def test_seg_oidc_rechazos_no_abren_sesion(navegador, proveedor, tx, caso):
    identidad, codigo, motivo = RECHAZOS[caso]

    vuelta = _con_google(navegador, proveedor, **identidad)

    assert (vuelta.status_code, vuelta.headers["location"]) == (302, f"/?error={codigo}")
    assert not [c for c in vuelta.headers.get_list("set-cookie") if "hv_sesion" in c]
    assert navegador.get("/api/yo").status_code == 401
    assert tx.execute("SELECT count(*) FROM acervo.sesion").fetchone()[0] == 0
    assert _sub_vinculado(tx, MARIA) is None
    rechazos = RegistroDeAuditoria(bd=navegador.app.state.bd).listar(accion=Accion.SESION_RECHAZO)
    assert [r.motivo for r in rechazos] == ([motivo] if motivo else [])
    # La cookie del intento se borra también en un rechazo.
    [intento] = [c for c in vuelta.headers.get_list("set-cookie") if "hv_intento" in c]
    assert "Max-Age=0" in intento


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("si el `state` no se mirara, el callback confirmaría la sesión")
@pytest.mark.parametrize("estado", ["otro-state", "", "ñ"])
def test_seg_oidc_state_invalido(navegador, proveedor, tx, estado):
    vuelta = _con_google(navegador, proveedor, estado=estado, email=MARIA_EMAIL)

    assert vuelta.headers["location"] == "/?error=intento-vencido"
    assert navegador.get("/api/yo").status_code == 401
    assert _sub_vinculado(tx, MARIA) is None


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
def test_seg_oidc_la_cookie_del_intento_es_corta_y_firmada(navegador, proveedor):
    ida = navegador.get("/auth/google")

    cookie = ida.headers["set-cookie"]
    assert cookie.startswith("__Host-hv_intento=")
    for atributo in ("Max-Age=600", "HttpOnly", "Path=/", "Secure"):
        assert atributo in cookie
    autorizacion = httpx.URL(ida.headers["location"])
    assert autorizacion.params["code_challenge_method"] == "S256"
    assert autorizacion.params["hd"] == "fie.undef.edu.ar"
    # Firmada: cambiarle un carácter la invalida.
    valor = navegador.cookies["__Host-hv_intento"]
    navegador.cookies.set("__Host-hv_intento", ("A" if valor[0] != "A" else "B") + valor[1:],
                          domain="testserver.local")
    vuelta = navegador.get(f"/auth/callback?code=x&state={autorizacion.params['state']}")
    assert vuelta.headers["location"] == "/?error=intento-vencido"


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("si el intento no venciera, el callback confirmaría la sesión")
def test_seg_oidc_el_intento_vence_a_los_diez_minutos(navegador, proveedor, reloj, monkeypatch):
    ida_original = navegador.get

    def tarda_once_minutos(url, **kwargs):
        if str(url).startswith("https://testserver/auth/callback"):
            reloj.adelantar(minutes=11)
        return ida_original(url, **kwargs)

    monkeypatch.setattr(navegador, "get", tarda_once_minutos)

    vuelta = _con_google(navegador, proveedor, email=MARIA_EMAIL)

    assert vuelta.headers["location"] == "/?error=intento-vencido"


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
@pytest.mark.parametrize("error", ["access_denied", "<script>alert(1)</script>"])
def test_seg_oidc_el_callback_no_refleja_lo_que_trae(navegador, error):
    vuelta = navegador.get("/auth/callback", params={"error": error, "state": "<b>x</b>"})

    codigo = "acceso-cancelado" if error == "access_denied" else "intento-vencido"
    assert vuelta.headers["location"] == f"/?error={codigo}"
    assert vuelta.content == b""


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el callback confirma la sesión desde el pool de la app")
@pytest.mark.parametrize(
    ("siguiente", "destino"),
    [("/validar.html", "/validar.html"), ("https://ajeno.test/", "/"), ("//ajeno.test", "/")],
)
def test_oidc_vuelve_solo_a_una_ruta_de_la_lista(navegador, proveedor, siguiente, destino):
    ida_original = navegador.get
    navegador.get = lambda url, **kw: ida_original(  # type: ignore[method-assign]
        url, params={"siguiente": siguiente} if url == "/auth/google" else None, **kw
    )

    vuelta = _con_google(navegador, proveedor, email=MARIA_EMAIL)

    assert vuelta.headers["location"] == destino


@pytest.mark.historia(85)
@pytest.mark.rnf("RNF-13")
@pytest.mark.confirma("el callback confirma la sesión desde el pool de la app")
def test_seg_oidc_el_ingreso_descarta_la_sesion_previa(navegador, proveedor):
    entrar(navegador, MARIA)
    previa = navegador.cookies["__Host-hv_sesion"]

    nueva = _con_google(navegador, proveedor, email=MARIA_EMAIL).cookies["__Host-hv_sesion"]

    def yo(sesion: str) -> int:
        return TestClient(navegador.app, base_url="https://testserver",
                          cookies={"__Host-hv_sesion": sesion}).get("/api/yo").status_code

    assert nueva != previa
    assert (yo(previa), yo(nueva)) == (401, 200)


def _caido(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("sin red", request=request)


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
def test_oidc_con_el_proveedor_caido_vuelve_a_la_entrada(
    url_app, capa_modelos, directorio_originales
):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        modo_de_ingreso=MetodoAutenticacion.OIDC,
        oidc=ConfiguracionOIDC(
            emisor=EMISOR, cliente_id=CLIENTE_ID, secreto=SECRETO,
            redireccion="https://testserver/auth/callback", clave_del_intento="clave",
            dominios=("fie.undef.edu.ar",),
            http=httpx.Client(transport=httpx.MockTransport(_caido)),
        ),
    )

    ida = TestClient(app, base_url="https://testserver", follow_redirects=False).get("/auth/google")

    assert ida.headers["location"] == "/?error=proveedor-no-disponible"


GOOGLE = {
    "HV_OIDC_CLIENTE_ID": "cliente-de-it",
    "HV_OIDC_SECRETO": "secreto-de-it",
    "HV_CLAVE_COOKIE_PREVIA": "clave-larga",
    "HV_URL_PUBLICA": "https://historiaviva.fie.undef.edu.ar",
}


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
def test_el_entorno_arma_el_cliente_oidc_de_google_y_el_secreto_no_se_muestra():
    oidc = Entorno.desde_variables(GOOGLE).oidc

    assert oidc == ConfiguracionOIDC(
        emisor="https://accounts.google.com",
        cliente_id="cliente-de-it",
        secreto="secreto-de-it",
        redireccion="https://historiaviva.fie.undef.edu.ar/auth/callback",
        clave_del_intento="clave-larga",
        dominios=("fie.undef.edu.ar",),
    )
    assert "secreto-de-it" not in repr(oidc) and "clave-larga" not in repr(oidc)
    assert Entorno.desde_variables({}).oidc is None
    # La prueba de humo de 121 apunta al proveedor falso.
    assert Entorno.desde_variables({**GOOGLE, "HV_OIDC_EMISOR": EMISOR}).oidc.emisor == EMISOR


@pytest.mark.historia(85)
@pytest.mark.parametrize("falta", ["HV_OIDC_SECRETO", "HV_CLAVE_COOKIE_PREVIA"])
def test_el_cliente_oidc_a_medias_no_arranca(falta):
    with pytest.raises(EntornoInvalido, match=falta):
        Entorno.desde_variables({**GOOGLE, falta: ""})


@pytest.mark.historia(85)
def test_web_con_el_cliente_oidc_atiende_el_ingreso_con_google(url_app, tmp_path):
    variables = {**GOOGLE, "HV_BD_URL": url_app, "HV_ORIGINALES": str(tmp_path),
                 "HV_MOTOR_URL": "http://127.0.0.1:1"}
    grafo = componer_web(Entorno.desde_variables(variables))
    sin_google = componer_web(Entorno.desde_variables({**variables, "HV_OIDC_CLIENTE_ID": ""}))
    def rutas(grafo) -> set[str]:
        return {ruta.path or "" for ruta in iter_route_contexts(crear_app(grafo).routes)}

    try:
        assert {"/auth/google", "/auth/callback"} <= rutas(grafo)
        assert "/auth/google" not in rutas(sin_google)
    finally:
        grafo.bd.cerrar()
        sin_google.bd.cerrar()


@pytest.fixture
def proveedor_aparte(tmp_path):
    """El proveedor falso como servicio de prueba aparte, como lo levanta la prueba de humo
    de 121: otro proceso, en su puerto."""
    with socket.socket() as libre:
        libre.bind(("127.0.0.1", 0))
        puerto = libre.getsockname()[1]
    emisor = f"http://127.0.0.1:{puerto}"
    proceso = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("proveedor_oidc_falso.py")),
         "--puerto", str(puerto), "--emisor", emisor],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                httpx.get(f"{emisor}/.well-known/openid-configuration").raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("el proveedor falso no arrancó")
        yield emisor
    finally:
        proceso.terminate()
        proceso.wait(timeout=10)


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el callback confirma el vínculo y la sesión desde el pool de la app")
def test_el_proveedor_falso_corre_como_servicio_aparte(
    url_app, capa_modelos, directorio_originales, padron_restaurado, proveedor_aparte
):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        modo_de_ingreso=MetodoAutenticacion.OIDC,
        oidc=ConfiguracionOIDC(
            emisor=proveedor_aparte, cliente_id=CLIENTE_ID, secreto=SECRETO,
            redireccion="https://testserver/auth/callback", clave_del_intento="clave",
            dominios=("fie.undef.edu.ar",),
        ),
    )
    navegador = TestClient(app, base_url="https://testserver", follow_redirects=False)

    ida = navegador.get("/auth/google")
    autorizacion = httpx.get(
        httpx.URL(ida.headers["location"]).copy_merge_params({"email": MARIA_EMAIL})
    )
    vuelta = navegador.get(autorizacion.headers["location"])

    assert vuelta.headers["location"] == "/"
    assert navegador.get("/api/yo").json()["modo_auth"] == "oidc"


CODIGOS_DE_LA_ENTRADA = ("dominio-no-permitido", "persona-no-habilitada", "cuenta-no-verificada",
                         "acceso-cancelado", "intento-vencido", "proveedor-no-disponible")
"""Los `?error=` del callback (spec §3.3)."""


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
def test_la_entrada_ofrece_google_y_tiene_un_mensaje_por_codigo(navegador, app):
    con_google = navegador.get("/").text
    con_dev = TestClient(app).get("/").text

    assert '<meta name="hv-ingreso" content="google">' in con_google
    assert '<meta name="hv-ingreso" content="dev">' in con_dev
    assert 'href="/auth/google"' in con_google
    # Los mensajes viven en el script de la Entrada: la CSP no admite scripts en línea (039).
    script = navegador.get("/estaticos/index.js").text
    for codigo in CODIGOS_DE_LA_ENTRADA:
        assert f'"{codigo}":' in script


@pytest.mark.historia(86)
@pytest.mark.rf("RF-34")
@pytest.mark.parametrize("valor", ["x.ñ", "ñ"])
def test_seg_oidc_una_cookie_del_intento_no_ascii_es_intento_vencido(navegador, valor):
    vuelta = navegador.get(
        "/auth/callback?code=x&state=y", headers={"cookie": f"__Host-hv_intento={valor}".encode()}
    )

    assert vuelta.headers["location"] == "/?error=intento-vencido"


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el callback confirma el vínculo y la sesión desde el pool de la app")
def test_oidc_aud_en_lista_entra_si_azp_es_el_cliente(navegador, proveedor):
    vuelta = _con_google(navegador, proveedor, email=MARIA_EMAIL, trampa="aud_lista_con_azp")

    assert vuelta.headers["location"] == "/"


class ProveedorRoto:
    """El proveedor falso con algunas rutas reemplazadas por lo que diga el test."""

    def __init__(self, proveedor: ProveedorFalso) -> None:
        self.proveedor = proveedor
        self._cliente = TestClient(proveedor.app, base_url=EMISOR)
        self.rutas: dict[str, httpx.Response] = {}

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        if pedido.url.path in self.rutas:
            return self.rutas[pedido.url.path]
        respuesta = self._cliente.request(
            pedido.method, str(pedido.url), content=pedido.content,
            headers={"content-type": pedido.headers.get("content-type", "")},
        )
        return httpx.Response(respuesta.status_code, headers=respuesta.headers,
                              content=respuesta.content)


@pytest.fixture
def roto(proveedor) -> ProveedorRoto:
    return ProveedorRoto(proveedor)


@pytest.fixture
def navegador_roto(url_app, capa_modelos, directorio_originales, roto, padron_restaurado):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        modo_de_ingreso=MetodoAutenticacion.OIDC,
        oidc=ConfiguracionOIDC(
            emisor=EMISOR, cliente_id=CLIENTE_ID, secreto=SECRETO,
            redireccion="https://testserver/auth/callback", clave_del_intento="clave",
            dominios=("fie.undef.edu.ar",), http=httpx.Client(transport=httpx.MockTransport(roto)),
        ),
    )
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


DESCUBRIMIENTO = "/.well-known/openid-configuration"
_DESCUBRIMIENTO_SANO = ProveedorFalso.descubrimiento(EMISOR)


def _sin(clave: str) -> httpx.Response:
    return httpx.Response(200, json={k: v for k, v in _DESCUBRIMIENTO_SANO.items() if k != clave})


_EMISOR_AJENO = {**_DESCUBRIMIENTO_SANO, "issuer": "https://otro-emisor.test"}
DESCUBRIMIENTOS_ROTOS = {
    "no_json": lambda: httpx.Response(200, text="<html>mantenimiento</html>"),
    "lista": lambda: httpx.Response(200, json=[]),
    "sin_authorization_endpoint": lambda: _sin("authorization_endpoint"),
    "emisor_ajeno": lambda: httpx.Response(200, json=_EMISOR_AJENO),
}


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.parametrize("caso", list(DESCUBRIMIENTOS_ROTOS))
def test_oidc_un_descubrimiento_roto_al_ir_es_proveedor_no_disponible(navegador_roto, roto, caso):
    roto.rutas[DESCUBRIMIENTO] = DESCUBRIMIENTOS_ROTOS[caso]()

    ida = navegador_roto.get("/auth/google")

    assert ida.headers["location"] == "/?error=proveedor-no-disponible"


ROTOS_AL_VOLVER = {
    "descubrimiento_no_json": (DESCUBRIMIENTO, lambda: httpx.Response(200, text="no")),
    "sin_token_endpoint": (DESCUBRIMIENTO, lambda: _sin("token_endpoint")),
    "sin_jwks_uri": (DESCUBRIMIENTO, lambda: _sin("jwks_uri")),
    "emisor_ajeno": (DESCUBRIMIENTO, lambda: httpx.Response(200, json=_EMISOR_AJENO)),
    "jwks_404": ("/jwks", lambda: httpx.Response(404)),
    "jwks_no_json": ("/jwks", lambda: httpx.Response(200, text="no")),
    "jwks_sin_keys": ("/jwks", lambda: httpx.Response(200, json={"otra": 1})),
    "jwks_keys_no_lista": ("/jwks", lambda: httpx.Response(200, json={"keys": "x"})),
    "jwks_clave_invalida": ("/jwks", lambda: httpx.Response(200, json={"keys": [{"kty": "RSA"}]})),
}


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.parametrize("caso", list(ROTOS_AL_VOLVER))
def test_oidc_un_proveedor_roto_al_volver_es_proveedor_no_disponible(
    navegador_roto, roto, tx, caso
):
    ida = navegador_roto.get("/auth/google")
    autorizacion = TestClient(roto.proveedor.app, base_url=EMISOR).get(
        str(httpx.URL(ida.headers["location"]).copy_merge_params({"email": MARIA_EMAIL})),
        follow_redirects=False,
    )
    ruta, respuesta = ROTOS_AL_VOLVER[caso]
    roto.rutas[ruta] = respuesta()

    vuelta = navegador_roto.get(autorizacion.headers["location"])

    assert vuelta.headers["location"] == "/?error=proveedor-no-disponible"
    assert tx.execute("SELECT count(*) FROM acervo.sesion").fetchone()[0] == 0


@pytest.mark.historia(85)
@pytest.mark.rf("RF-34")
@pytest.mark.confirma("el vínculo concurrente se confirma desde otra conexión")
def test_el_segundo_ingreso_concurrente_del_primer_vinculo_entra(
    app_oidc, url_bd
):
    """Mientras otro ingreso de la misma persona vincula el `sub` (sin confirmar todavía), el
    `UPDATE … WHERE sub_google IS NULL` espera y no devuelve fila: no es un `sub` distinto."""
    sesiones = app_oidc.state.sesiones
    resultado: dict[str, object] = {}

    def ingresar() -> None:
        try:
            resultado["sesion"] = sesiones.ingresar_oidc("sub-de-maria", MARIA_EMAIL, previa=None)
        except Exception as error:  # noqa: BLE001 — el test lo muestra abajo
            resultado["error"] = error

    with psycopg.connect(url_bd) as primero:
        primero.execute(
            "UPDATE acervo.usuario SET sub_google = 'sub-de-maria' WHERE usuario_id = %s", (MARIA,)
        )
        hilo = threading.Thread(target=ingresar)
        hilo.start()
        esperar_detras_del_candado(primero, 1)  # el segundo ingreso espera el candado
        primero.commit()
    hilo.join(timeout=10)

    assert "error" not in resultado, resultado.get("error")
    assert resultado["sesion"]
    rechazos = RegistroDeAuditoria(bd=app_oidc.state.bd).listar(accion=Accion.SESION_RECHAZO)
    assert rechazos == []
