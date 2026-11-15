"""La conexión en ejecución (spec, módulo `bd`; DEC-31, DEC-99, DEC-145).

Postgres real y como `hv_app`, el rol con el que conectan `web` y el trabajador.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador
from historia_viva.api.sesion import COOKIE_DE_SESION
from historia_viva.bd import BaseNoDisponible, Bd
from historia_viva.dominio.tipos import MetodoAutenticacion
from validacion import PDF


@pytest.fixture
def bd(url_app: str) -> Iterator[Bd]:
    bd = Bd(url_app, tamano_pool=1)
    yield bd
    bd.cerrar()


@pytest.mark.historia(48)
def test_la_transaccion_corta_las_esperas_de_candados_y_las_transacciones_ociosas(bd):
    with bd.transaccion() as tx:
        ajustes = tx.execute(
            "SELECT current_setting('lock_timeout') AS candado,"
            " current_setting('idle_in_transaction_session_timeout') AS ociosa"
        ).fetchone()

    assert ajustes == {"candado": "5s", "ociosa": "1min"}


@pytest.mark.historia(258)
def test_una_sesion_que_se_corta_sale_como_base_no_disponible_con_el_error_original(bd):
    # La sesión del candado del planificador (058): sin autocommit que restaurar encima.
    with pytest.raises(BaseNoDisponible) as error, bd.sesion() as sesion:
        assert sesion.autocommit
        sesion.execute("SELECT pg_terminate_backend(pg_backend_pid())")

    assert "terminat" in str(error.value.causa)
    with bd.sesion() as otra:  # el pool repone la conexión
        assert otra.execute("SELECT 1 AS uno").fetchone() == {"uno": 1}


@pytest.mark.rnf("RNF-07")
def test_una_excepcion_dentro_de_la_transaccion_revierte_la_pieza_y_su_trabajo(bd, url_bd):
    with pytest.raises(RuntimeError, match="falla inyectada"), bd.transaccion() as tx:
        fila = tx.execute(
            "INSERT INTO acervo.pieza (estado, clase_original, tipo_mime, nombre_archivo,"
            " tamano_bytes) VALUES ('Recibida', 'documento', 'application/pdf', 'acta.pdf', 1)"
            " RETURNING pieza_id"
        ).fetchone()
        assert fila is not None
        tx.execute(
            "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, tiempo_maximo_ms,"
            " configuracion_efectiva) VALUES (%s, 'procesamiento', 390000, '{}')",
            (fila["pieza_id"],),
        )
        raise RuntimeError("falla inyectada")

    with psycopg.connect(url_bd) as conexion:
        quedan = conexion.execute(
            "SELECT (SELECT count(*) FROM acervo.pieza),"
            " (SELECT count(*) FROM acervo.trabajo_procesamiento)"
        ).fetchone()
    assert quedan == (0, 0)


@pytest.mark.historia(54)
def test_un_punto_de_guardado_como_primera_sentencia_no_confirma_si_la_transaccion_revierte(
    bd, url_bd
):
    """Sin una transacción abierta, `tx.transaction()` haría su propio BEGIN/COMMIT y la Pieza
    quedaría confirmada aunque lo que sigue falle."""
    with pytest.raises(RuntimeError, match="falla inyectada"), bd.transaccion() as tx:
        with tx.transaction():
            tx.execute(
                "INSERT INTO acervo.pieza (estado, clase_original, tipo_mime, nombre_archivo,"
                " tamano_bytes) VALUES ('Recibida', 'documento', 'application/pdf', 'acta.pdf', 1)"
            )
        raise RuntimeError("falla inyectada")

    with psycopg.connect(url_bd) as conexion:
        assert conexion.execute("SELECT count(*) FROM acervo.pieza").fetchone() == (0,)


@pytest.mark.historia(55)
def test_el_pool_recupera_la_conexion_ante_cualquier_error(bd):
    for _ in range(3):
        with pytest.raises(RuntimeError), bd.transaccion():
            raise RuntimeError("falla inyectada")
        with pytest.raises(psycopg.errors.DivisionByZero), bd.transaccion() as tx:
            tx.execute("SELECT 1 / 0")

    bd.ping()  # con una sola conexión en el pool: si alguna no volvió, esto espera y falla


@pytest.mark.historia(48)
@pytest.mark.parametrize(
    "sentencia",
    [
        "DELETE FROM acervo.pieza",
        "DELETE FROM acervo.ficha",
        "DELETE FROM acervo.trabajo_procesamiento",
        # La auditoría es append-only: lo que se registró no se borra ni se reescribe.
        pytest.param("DELETE FROM acervo.auditoria", marks=pytest.mark.rnf("RNF-07")),
        pytest.param("UPDATE acervo.auditoria SET accion = ''", marks=pytest.mark.rnf("RNF-07")),
        "TRUNCATE acervo.pieza",
        "CREATE TABLE acervo.intrusa (id int)",
        "CREATE TABLE public.intrusa (id int)",
    ],
)
def test_hv_app_no_borra_ni_reescribe_la_auditoria_ni_cambia_el_esquema(bd, sentencia):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), bd.transaccion() as tx:
        tx.execute(sentencia)


BASE_APAGADA = "postgresql://nadie:nada@127.0.0.1:1/inexistente"


@pytest.mark.historia(55)
def test_ping_responde_con_la_base_en_pie_y_lanza_base_no_disponible_si_no(bd):
    bd.ping()
    apagada = Bd(BASE_APAGADA, tamano_pool=1)
    try:
        with pytest.raises(BaseNoDisponible):
            apagada.ping()
    finally:
        apagada.cerrar()


@pytest.mark.historia(55)
def test_cerrada_la_conexion_no_hay_base(url_app):
    bd = Bd(url_app, tamano_pool=1)
    bd.cerrar()

    with pytest.raises(BaseNoDisponible):
        bd.ping()


@pytest.mark.historia(55)
def test_con_la_base_apagada_todo_pedido_responde_503_sin_tocar_el_motor(
    capa_modelos, directorio_originales, monkeypatch
):
    preguntas_al_motor: list[str] = []

    def verificar_salud() -> bool:
        preguntas_al_motor.append("verificar_salud")
        return True

    monkeypatch.setattr(capa_modelos, "verificar_salud", verificar_salud)
    app = app_sin_trabajador(
        BASE_APAGADA,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )
    with TestClient(app, headers=CABECERAS) as cliente:
        # Validar la sesión ya consulta la base: cualquier id sirve.
        cliente.cookies.set(COOKIE_DE_SESION[MetodoAutenticacion.DEV], "cualquiera")
        respuestas = [
            cliente.get("/api/salud"),
            cliente.get("/api/yo"),
            cliente.get("/api/tipos"),
            cliente.get("/api/piezas"),
            cliente.post("/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}),
        ]
        respuestas.append(cliente.get("/api/mediciones"))

    assert [(r.status_code, r.json()["type"]) for r in respuestas] == [
        (503, "/errores/servicio-no-disponible")
    ] * len(respuestas)
    # DEC-78: por base caída se reintenta a los 30 s.
    assert [r.headers.get("Retry-After") for r in respuestas] == ["30"] * len(respuestas)
    assert {r.json()["title"] for r in respuestas} == {"El servicio no está disponible"}
    assert preguntas_al_motor == []
    assert capa_modelos.invocaciones == []
