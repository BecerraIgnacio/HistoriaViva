"""Autor y Carreras: opcionales, no cuentan para Validar (DEC-126).

El Autor es texto libre y admite «Desconocido»; las Carreras son varias de la lista oficial y
viven en `pieza_carrera`, proyección del frontmatter que escribe `fichas` sin borrar: quitar una
marca `quitada_el` (DEC-143). La Carrera es un filtro, no un permiso (DEC-125). Por HTTP contra
Postgres real.
"""

from __future__ import annotations

import psycopg
import pytest

from espera import esperar_estado
from historia_viva.composicion import main
from test_fichas import divergencias
from validacion import EDICION_COMPLETA, PDF, abrir, mediciones

pytestmark = [
    pytest.mark.rf("RF-50", "RF-52"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


def _guardar(cliente, pieza_id, **cambios):
    return cliente.put(f"/api/piezas/{pieza_id}/ficha", json=EDICION_COMPLETA | cambios)


@pytest.mark.historia(283, 287)
def test_sin_autor_ni_carreras_queda_validada_y_ninguno_falta(cliente, pieza_pendiente):
    resultado = _guardar(cliente, pieza_pendiente).json()
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    assert (resultado["estado"], resultado["campos_faltantes"]) == ("Validada", [])
    assert ficha["faltantes"] == []
    assert (ficha["frontmatter"]["autor"], ficha["frontmatter"]["carreras"]) == (None, [])


@pytest.mark.historia(287)
def test_desconocido_es_un_autor_valido(cliente, pieza_pendiente):
    resultado = _guardar(cliente, pieza_pendiente, autor="Desconocido").json()
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    assert resultado["estado"] == "Validada"
    assert ficha["frontmatter"]["autor"] == "Desconocido"


@pytest.fixture
def carreras_restauradas(_conexion_del_dueno):
    """Deja el catálogo de Carreras como estaba: la app lee las bajas desde su pool, así que se
    confirman, y la base del proceso de xdist la usan los tests que siguen."""
    filas = _conexion_del_dueno.execute(
        "SELECT activo, nombre, orden, carrera_id FROM acervo.carrera"
    ).fetchall()
    yield
    with _conexion_del_dueno.transaction():
        _conexion_del_dueno.cursor().executemany(
            "UPDATE acervo.carrera SET activo = %s, nombre = %s, orden = %s WHERE carrera_id = %s",
            filas,
        )


@pytest.mark.historia(283)
@pytest.mark.parametrize(
    "carreras",
    [["ing-civil", "ing-civil"], ["ing-naval"], ["ing-mecanica"]],
    ids=["repetida", "inexistente", "dada-de-baja"],
)
def test_una_carrera_fuera_de_la_lista_responde_422_y_la_pieza_no_cambia(
    cliente, pieza_pendiente, _conexion_del_dueno, carreras_restauradas, carreras
):
    _conexion_del_dueno.execute(
        "UPDATE acervo.carrera SET activo = false WHERE carrera_id = 'ing-mecanica'"
    )

    respuesta = _guardar(cliente, pieza_pendiente, carreras=carreras)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (
        "/errores/carrera-desconocida",
        "carreras",
    )
    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Pendiente"


def _filas_de_carrera(conexion, pieza_id):
    return conexion.execute(
        "SELECT carrera_id, quitada_el IS NOT NULL, asignada_el FROM acervo.pieza_carrera"
        " WHERE pieza_id = %s ORDER BY carrera_id",
        (pieza_id,),
    ).fetchall()


@pytest.mark.historia(283)
@pytest.mark.rnf("RNF-09")
def test_quitar_una_carrera_la_marca_y_volver_a_ponerla_limpia_la_marca(
    cliente, pieza_pendiente, url_bd
):
    url = f"/api/piezas/{pieza_pendiente}/ficha"
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        _guardar(
            cliente, pieza_pendiente, autor="Ana Pérez", carreras=["ing-mecanica", "ing-civil"]
        )
        puestas = _filas_de_carrera(conexion, pieza_pendiente)
        assert cliente.get(url).json()["frontmatter"]["carreras"] == ["ing-civil", "ing-mecanica"]
        assert divergencias(conexion) == []

        _guardar(cliente, pieza_pendiente, version_base=2, carreras=["ing-civil"])
        assert [fila[:2] for fila in _filas_de_carrera(conexion, pieza_pendiente)] == [
            ("ing-civil", False),
            ("ing-mecanica", True),
        ]
        assert cliente.get(url).json()["frontmatter"]["carreras"] == ["ing-civil"]
        assert divergencias(conexion) == []

        _guardar(cliente, pieza_pendiente, version_base=3, carreras=["ing-civil", "ing-mecanica"])
        # La fila es la misma: limpiar la marca no reasigna.
        assert _filas_de_carrera(conexion, pieza_pendiente) == puestas
        assert divergencias(conexion) == []


@pytest.mark.historia(283)
def test_hv_app_no_borra_filas_de_pieza_carrera(url_app):
    with (
        psycopg.connect(url_app) as conexion,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conexion.execute("DELETE FROM acervo.pieza_carrera")


@pytest.mark.historia(283)
def test_una_carrera_dada_de_baja_por_la_carga_la_conserva_solo_la_ficha_que_la_tenia(
    cliente, pieza_pendiente, url_app, monkeypatch, tmp_path, carreras_restauradas
):
    assert _guardar(cliente, pieza_pendiente, carreras=["ing-mecanica"]).status_code == 200
    otra = cliente.post(
        "/api/piezas", files={"archivo": ("otra.pdf", PDF + b"%otra\n", "application/pdf")}
    ).json()["pieza_id"]
    esperar_estado(cliente, otra, "Pendiente")
    # La carga deja las tres primeras como estaban y desactiva `ing-mecanica` (022).
    csv = tmp_path / "carreras.csv"
    csv.write_text(
        "carrera_id,nombre\ning-civil,Ingeniería Civil\ning-electronica,Ingeniería Electrónica\n"
        "ing-informatica,Ingeniería en Informática\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HV_BD_URL", url_app)
    assert main(["catalogo", "carreras", "--cargar", str(csv)]) == 0

    de_nuevo = _guardar(cliente, pieza_pendiente, version_base=2, carreras=["ing-mecanica"])
    en_otra = _guardar(cliente, otra, carreras=["ing-mecanica"])

    assert de_nuevo.status_code == 200
    assert (en_otra.status_code, en_otra.json()["type"]) == (422, "/errores/carrera-desconocida")


@pytest.mark.historia(283)
def test_ninguna_tabla_ni_vista_restringe_filas_por_carrera(tx):
    # DEC-125: todo rol que consulta ve todas las Piezas Validadas; la Carrera filtra, no oculta.
    con_politicas = tx.execute(
        "SELECT relname FROM pg_class WHERE relnamespace = 'acervo'::regnamespace"
        " AND (relrowsecurity OR relforcerowsecurity)"
    ).fetchall()
    politicas = tx.execute("SELECT policyname FROM pg_policies").fetchall()
    vistas = tx.execute(
        "SELECT viewname FROM pg_views WHERE schemaname = 'acervo'"
        r" AND definition ~* '\m(pieza_carrera|carrera)\M'"
    ).fetchall()

    assert (con_politicas, politicas, vistas) == ([], [], [])


@pytest.mark.historia(283, 287)
def test_el_detalle_de_la_pieza_trae_autor_y_carreras(cliente, pieza_pendiente):
    _guardar(cliente, pieza_pendiente, autor="Desconocido", carreras=["ing-mecanica", "ing-civil"])

    detalle = cliente.get(f"/api/piezas/{pieza_pendiente}").json()

    assert (detalle["autor"], detalle["carreras"]) == (
        "Desconocido",
        ["ing-civil", "ing-mecanica"],
    )


@pytest.mark.historia(283, 287)
@pytest.mark.parametrize(
    ("cambios", "modificado"),
    [({"autor": "Ana Pérez"}, "autor"), ({"carreras": ["ing-civil"]}, "carreras")],
    ids=["autor", "carrera"],
)
def test_agregar_un_autor_o_una_carrera_cuenta_como_campo_modificado(
    cliente, pieza_pendiente, cambios, modificado
):
    apertura = abrir(cliente, pieza_pendiente)

    _guardar(cliente, pieza_pendiente, medicion_id=apertura["medicion_id"], **cambios)

    assert modificado in mediciones(cliente)[0]["campos_modificados"]
