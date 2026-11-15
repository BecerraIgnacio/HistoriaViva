"""Los catálogos de la Ficha: tipos de actividad, tipos de Pieza y Carreras (DEC-127).

Los siembra una migración y `hv_app` solo los lee (DEC-105); los sirve `catalogos`, que es la
única puerta a sus tres tablas (R-25).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row, tuple_row

from aplicacion import app_sin_trabajador, entrar
from base_de_pruebas import CLAVE_APP
from capa_modelos_falsa import CapaModelosFalsa
from historia_viva.auditoria import Accion, Evento, ObjetoRef, RegistroDeAuditoria, TipoDeActor
from historia_viva.bd import Bd
from historia_viva.catalogos import (
    CarreraDesconocida,
    TipoDeActividadDesconocido,
    es_actividad,
    tipos_actividad,
    validar_carreras,
    validar_tipo_actividad,
)
from historia_viva.composicion import main
from historia_viva.migraciones import aplicar, sembrar_desarrollo
from test_permisos import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA
from validacion import EDICION_COMPLETA, JORGE, MARIA

pytestmark = pytest.mark.rf("RF-51")

TIPOS_DE_ACTIVIDAD = [
    ("Transferencia", True),
    ("Consultoría", True),
    ("Asistencia Técnica", True),
    ("Proyecto UNDEX", True),
    ("Evento Socio-Cultural", True),
    ("Programa/Curso/Diplomatura", True),
    ("Evento Académico", True),
    ("No es una actividad", False),
]
"""Los siete del Director (DIR-23) y el que el informe excluye (DEC-131)."""

TIPOS_DE_PIEZA = [
    ("fotografia", False), ("acta", False), ("resolucion", False), ("convenio", False),
    ("memoria", False), ("nota", False), ("plano", False), ("planilla", True), ("video", True),
]
"""Los siete del Prototipo y los dos provisorios hasta que responda C4."""

CARRERAS_DE_PRUEBA = ["ing-civil", "ing-electronica", "ing-informatica", "ing-mecanica"]
"""Las de `semilla_desarrollo.sql`, las mismas que usan los Fragmentos de `test_indice`."""


@pytest.mark.historia(285, 286)
def test_la_migracion_siembra_los_tipos_de_actividad_y_los_tipos_de_pieza_provisorios(tx):
    actividades = tx.execute(
        "SELECT nombre, es_actividad FROM acervo.tipo_actividad ORDER BY orden"
    ).fetchall()
    tipos = tx.execute("SELECT nombre, provisorio FROM acervo.tipo_pieza ORDER BY orden").fetchall()

    assert actividades == TIPOS_DE_ACTIVIDAD
    assert tipos == TIPOS_DE_PIEZA


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_las_carreras_nacen_vacias_y_el_corpus_de_prueba_tiene_las_suyas(base_vacia):
    aplicar(base_vacia)
    with psycopg.connect(base_vacia) as conexion:
        sin_semilla = conexion.execute("SELECT count(*) FROM acervo.carrera").fetchone()
    sembrar_desarrollo(base_vacia)
    sembrar_desarrollo(base_vacia)

    # La lista oficial la entrega la SEU (C45): fuera de desarrollo no hay ninguna.
    assert sin_semilla == (0,)
    with psycopg.connect(base_vacia) as conexion:
        carreras = conexion.execute("SELECT carrera_id FROM acervo.carrera ORDER BY orden")
        assert [carrera_id for (carrera_id,) in carreras] == CARRERAS_DE_PRUEBA


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283, 285, 286)
@pytest.mark.parametrize(
    "persona", [MARIA, JORGE, DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO],
    ids=["rgc", "coordinador", "director_carrera", "admin_tecnico"],
)
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_todo_rol_que_consulta_obtiene_los_tres_catalogos_en_su_orden(cliente, persona):
    entrar(cliente, persona)

    carreras = cliente.get("/api/carreras")
    actividades = cliente.get("/api/tipos-actividad")
    tipos = cliente.get("/api/tipos")

    assert [carrera["carrera_id"] for carrera in carreras.json()["carreras"]] == CARRERAS_DE_PRUEBA
    assert carreras.json()["carreras"][2] == {
        "carrera_id": "ing-informatica", "nombre": "Ingeniería en Informática",
    }
    assert actividades.json() == {"tipos_actividad": [nombre for nombre, _ in TIPOS_DE_ACTIVIDAD]}
    # `provisorios` se suma a la forma anterior, que la pantalla de validar ya lee.
    assert tipos.json() == {
        "tipos": [nombre for nombre, _ in TIPOS_DE_PIEZA], "provisorios": ["planilla", "video"],
    }


@pytest.fixture
def dar_de_baja(_conexion_del_dueno):
    """Desactiva una fila como lo haría `hv_migrador`, confirmada para que la vea la app, y al
    terminar la reactiva: la base de la suite se comparte."""
    conexion = _conexion_del_dueno
    hechas: list[tuple[LiteralString, str]] = []

    def dar(tabla: str, valor: str) -> None:
        sentencia = _BAJA[tabla]
        conexion.execute(sentencia, (False, valor))
        hechas.append((sentencia, valor))

    yield dar
    for sentencia, valor in hechas:
        conexion.execute(sentencia, (True, valor))


_BAJA: dict[str, LiteralString] = {
    "carrera": "UPDATE acervo.carrera SET activo = %s WHERE carrera_id = %s",
    "tipo_actividad": "UPDATE acervo.tipo_actividad SET activo = %s WHERE nombre = %s",
    "tipo_pieza": "UPDATE acervo.tipo_pieza SET activo = %s WHERE nombre = %s",
}


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283, 285, 286)
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_lo_dado_de_baja_no_se_ofrece(cliente, dar_de_baja):
    dar_de_baja("carrera", "ing-electronica")
    dar_de_baja("tipo_actividad", "Consultoría")
    dar_de_baja("tipo_pieza", "planilla")
    entrar(cliente, DIRECTORA_DE_CARRERA)

    carreras = cliente.get("/api/carreras").json()["carreras"]
    actividades = cliente.get("/api/tipos-actividad").json()["tipos_actividad"]
    tipos = cliente.get("/api/tipos").json()

    assert [carrera["carrera_id"] for carrera in carreras] == [
        "ing-civil", "ing-informatica", "ing-mecanica",
    ]
    assert "Consultoría" not in actividades
    assert len(actividades) == 7
    assert "planilla" not in tipos["tipos"]
    assert tipos["provisorios"] == ["video"]


@pytest.mark.rf("RF-50")
def test_el_carrera_id_es_un_slug(tx):
    tx.execute(
        "INSERT INTO acervo.carrera (carrera_id, nombre, orden) VALUES ('ing-naval', 'Naval', 9)"
    )

    with pytest.raises(psycopg.errors.CheckViolation, match="carrera_carrera_id_check"):
        tx.execute(
            "INSERT INTO acervo.carrera (carrera_id, nombre, orden) VALUES ('Ing Aero', 'Aero', 10)"
        )


@pytest.mark.rf("RF-50")
@pytest.mark.parametrize("ruta",["/api/carreras", "/api/tipos-actividad", "/api/tipos"])
def test_sin_sesion_los_catalogos_responden_401(cliente, ruta):
    respuesta = cliente.get(ruta)

    assert respuesta.status_code == 401
    assert respuesta.json()["type"] == "/errores/sin-sesion"


@pytest.mark.historia(286)
@pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")
def test_dar_de_baja_un_tipo_de_pieza_no_invalida_la_ficha_que_ya_lo_tiene(
    cliente, pieza_pendiente, dar_de_baja
):
    ruta = f"/api/piezas/{pieza_pendiente}/ficha"
    cliente.put(ruta, json=EDICION_COMPLETA | {"tipo": "planilla"})
    # La SEU rechaza el provisorio (C4): se desactiva, no se borra (DEC-127).
    dar_de_baja("tipo_pieza", "planilla")

    conservada = cliente.put(ruta, json=EDICION_COMPLETA | {"version_base": 2, "tipo": "planilla"})
    cambiada = cliente.put(ruta, json=EDICION_COMPLETA | {"version_base": 3, "tipo": "acta"})
    vuelta = cliente.put(ruta, json=EDICION_COMPLETA | {"version_base": 4, "tipo": "planilla"})

    assert (conservada.status_code, conservada.json()["estado"]) == (200, "Validada")
    assert cambiada.status_code == 200
    # Ya no es el valor que la Ficha tenía: es elegir un tipo dado de baja.
    assert vuelta.status_code == 422
    assert (vuelta.json()["type"], vuelta.json()["campo"]) == ("/errores/tipo-desconocido", "tipo")


@pytest.mark.historia(285)
@pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")
def test_dar_de_baja_un_tipo_de_actividad_no_invalida_la_ficha_que_ya_lo_tiene(
    cliente, pieza_pendiente, dar_de_baja
):
    ruta = f"/api/piezas/{pieza_pendiente}/ficha"
    cliente.put(ruta, json=EDICION_COMPLETA | {"tipo_actividad": "Consultoría"})
    dar_de_baja("tipo_actividad", "Consultoría")

    conservada = cliente.put(
        ruta, json=EDICION_COMPLETA | {"version_base": 2, "tipo_actividad": "Consultoría"}
    )
    cambiada = cliente.put(ruta, json=EDICION_COMPLETA | {"version_base": 3})
    vuelta = cliente.put(
        ruta, json=EDICION_COMPLETA | {"version_base": 4, "tipo_actividad": "Consultoría"}
    )

    assert (conservada.status_code, conservada.json()["estado"]) == (200, "Validada")
    assert cambiada.status_code == 200
    assert vuelta.status_code == 422
    assert (vuelta.json()["type"], vuelta.json()["campo"]) == (
        "/errores/tipo-actividad-desconocido",
        "tipo_actividad",
    )


@pytest.fixture
def tx_de_la_app(tx):
    """`tx` con las filas como diccionarios, como las entrega `bd`."""
    tx.row_factory = dict_row
    yield tx
    tx.row_factory = tuple_row


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
@pytest.mark.parametrize(
    "otras",
    [["ing-civil", "ing-civil"], ["ing-naval"], ["ing-electronica"]],
    ids=["repetida", "inexistente", "dada-de-baja"],
)
def test_validar_carreras_rechaza_una_repetida_inexistente_o_dada_de_baja(tx_de_la_app, otras):
    tx_de_la_app.execute(
        "UPDATE acervo.carrera SET activo = false WHERE carrera_id = 'ing-electronica'"
    )

    with pytest.raises(CarreraDesconocida) as rechazo:
        validar_carreras(tx_de_la_app, ["ing-informatica", *otras])

    # El borde HTTP lo traduce a 422 `carrera-desconocida` con `campo: carreras`.
    assert (rechazo.value.codigo, rechazo.value.campo) == ("carrera-desconocida", "carreras")


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_las_carreras_son_opcionales_y_multivalor(tx_de_la_app):
    validar_carreras(tx_de_la_app, [])
    validar_carreras(tx_de_la_app, ["ing-civil", "ing-mecanica"])


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_una_carrera_dada_de_baja_solo_la_conserva_la_ficha_que_ya_la_tenia(tx_de_la_app):
    tx_de_la_app.execute(
        "UPDATE acervo.carrera SET activo = false WHERE carrera_id = 'ing-electronica'"
    )

    validar_carreras(
        tx_de_la_app, ["ing-electronica", "ing-civil"], conserva=["ing-electronica"]
    )
    with pytest.raises(CarreraDesconocida):
        validar_carreras(tx_de_la_app, ["ing-electronica"], conserva=["ing-civil"])
    with pytest.raises(CarreraDesconocida):
        validar_carreras(tx_de_la_app, ["ing-naval"], conserva=["ing-naval"])


@pytest.mark.historia(285)
def test_un_tipo_de_actividad_dado_de_baja_solo_lo_conserva_la_ficha_que_ya_lo_tenia(
    tx_de_la_app,
):
    tx_de_la_app.execute(
        "UPDATE acervo.tipo_actividad SET activo = false WHERE nombre = 'Consultoría'"
    )

    validar_tipo_actividad(tx_de_la_app, "Consultoría", conserva="Consultoría")
    validar_tipo_actividad(tx_de_la_app, "No es una actividad")
    with pytest.raises(TipoDeActividadDesconocido):
        validar_tipo_actividad(tx_de_la_app, "Consultoría", conserva="Transferencia")
    with pytest.raises(TipoDeActividadDesconocido):
        validar_tipo_actividad(tx_de_la_app, "Pasantía")


@pytest.mark.rf("RF-49")
@pytest.mark.historia(270, 285)
def test_los_tipos_de_actividad_traen_su_orden_y_si_son_actividad_para_el_informe(
    tx_de_la_app,
):
    tx_de_la_app.execute(
        "UPDATE acervo.tipo_actividad SET activo = false WHERE nombre = 'Consultoría'"
    )

    activos = tipos_actividad(tx_de_la_app)
    todos = tipos_actividad(tx_de_la_app, solo_activos=False)

    assert [t.nombre for t in activos] == [
        nombre for nombre, _ in TIPOS_DE_ACTIVIDAD if nombre != "Consultoría"
    ]
    # El informe ordena también por los dados de baja: la Ficha que ya los tiene los conserva.
    assert [(t.nombre, t.es_actividad, t.activo) for t in todos] == [
        (nombre, es, nombre != "Consultoría") for nombre, es in TIPOS_DE_ACTIVIDAD
    ]
    assert [t.orden for t in todos] == list(range(1, 9))
    assert [es_actividad(nombre) for nombre, _ in TIPOS_DE_ACTIVIDAD] == [
        es for _, es in TIPOS_DE_ACTIVIDAD
    ]


# 022: `catalogo carreras --cargar <csv>` (R-25), hasta que C45 permita sembrarlas por migración.
TRES_CARRERAS = (
    "carrera_id;nombre\r\n"
    "ing-naval;Ingeniería Naval\r\n"
    "lic-quimica;Licenciatura en Química\r\n"
    "ing-civil;Ingeniería Civil\r\n"
).encode("cp1252")
"""Como lo guarda Excel en español: cp1252 y punto y coma. El orden del archivo es el de la
lista."""


@pytest.fixture
def cargar(base_vacia, monkeypatch, tmp_path):
    """El comando como `hv_app` sobre una base aparte con las Carreras de prueba: la carga
    confirma y desactivaría las que comparte la suite."""
    aplicar(base_vacia)
    sembrar_desarrollo(base_vacia)
    monkeypatch.setenv("HV_BD_URL", make_conninfo(base_vacia, user="hv_app", password=CLAVE_APP))

    def cargar_(contenido: bytes) -> int:
        ruta = tmp_path / "carreras.csv"
        ruta.write_bytes(contenido)
        return main(["catalogo", "carreras", "--cargar", str(ruta)])

    return cargar_


def _filas_de_carrera(base: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(base) as conexion:
        return conexion.execute(
            "SELECT carrera_id, nombre, orden, activo FROM acervo.carrera ORDER BY carrera_id"
        ).fetchall()


def _ofrecidas(base: str, directorio: Path) -> list[dict[str, str]]:
    """Lo que responde `GET /api/carreras` sobre `base`."""
    capa = CapaModelosFalsa()
    app = app_sin_trabajador(
        make_conninfo(base, user="hv_app", password=CLAVE_APP),
        capa_para=capa.para,
        validar=capa.validar,
        directorio_originales=directorio,
    )
    with TestClient(app) as cliente:
        entrar(cliente, MARIA)
        carreras: list[dict[str, str]] = cliente.get("/api/carreras").json()["carreras"]
    return carreras


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_cargar_un_csv_deja_sus_carreras_y_cargarlo_otra_vez_no_cambia_nada(
    cargar, base_vacia, tmp_path
):
    primera = cargar(TRES_CARRERAS)
    cargadas = _filas_de_carrera(base_vacia)
    segunda = cargar(TRES_CARRERAS)

    assert (primera, segunda) == (0, 0)
    assert _filas_de_carrera(base_vacia) == cargadas
    assert _ofrecidas(base_vacia, tmp_path) == [
        {"carrera_id": "ing-naval", "nombre": "Ingeniería Naval"},
        {"carrera_id": "lic-quimica", "nombre": "Licenciatura en Química"},
        {"carrera_id": "ing-civil", "nombre": "Ingeniería Civil"},
    ]


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_una_carrera_ausente_del_csv_se_desactiva_sin_borrarse(cargar, base_vacia, tmp_path):
    cargar(TRES_CARRERAS)
    # Otra codificación y otro separador: UTF-8 y coma.
    sin_naval = "carrera_id,nombre\nlic-quimica,Licenciatura en Química\ning-civil,Ingeniería Civil"

    salida = cargar(sin_naval.encode("utf-8"))

    assert salida == 0
    # Sigue en la tabla: una Ficha que la tenga la sigue nombrando (DEC-127).
    assert ("ing-naval", "Ingeniería Naval", 1, False) in _filas_de_carrera(base_vacia)
    assert [c["carrera_id"] for c in _ofrecidas(base_vacia, tmp_path)] == [
        "lic-quimica", "ing-civil",
    ]


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_una_carrera_desactivada_que_vuelve_al_csv_se_reactiva(cargar, base_vacia, tmp_path):
    cargar(TRES_CARRERAS)
    cargar(b"carrera_id;nombre\nlic-quimica;Licenciatura en Qu\xedmica\n")

    salida = cargar(TRES_CARRERAS)

    assert salida == 0
    assert ("ing-naval", "Ingeniería Naval", 1, True) in _filas_de_carrera(base_vacia)
    assert [c["carrera_id"] for c in _ofrecidas(base_vacia, tmp_path)] == [
        "ing-naval", "lic-quimica", "ing-civil",
    ]


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
@pytest.mark.parametrize(
    ("csv", "esperadas"),
    [
        pytest.param(
            "carrera_id;nombre\ning-naval;Ingeniería Civil\ning-civil;Ingeniería Naval\n",
            [("ing-civil", "Ingeniería Naval", 2), ("ing-naval", "Ingeniería Civil", 1)],
            id="intercambio",
        ),
        # Fila por fila, ing-naval tomaría un nombre que ing-civil todavía tiene.
        pytest.param(
            "carrera_id;nombre\ning-naval;Ingeniería Civil\ning-civil;Ingeniería Civil y Vial\n",
            [("ing-civil", "Ingeniería Civil y Vial", 2), ("ing-naval", "Ingeniería Civil", 1)],
            id="corrimiento",
        ),
    ],
)
def test_los_nombres_se_validan_sobre_la_lista_final(cargar, base_vacia, csv, esperadas):
    cargar("carrera_id;nombre\ning-naval;Ingeniería Naval\ning-civil;Ingeniería Civil\n".encode())

    salida = cargar(csv.encode())

    assert salida == 0
    assert [
        (carrera_id, nombre, orden)
        for carrera_id, nombre, orden, activo in _filas_de_carrera(base_vacia)
        if activo
    ] == esperadas


def _cargas_auditadas(base: str) -> list[Evento]:
    bd = Bd(base, tamano_pool=1)
    try:
        return RegistroDeAuditoria(bd=bd).listar(accion=Accion.CATALOGO_CARGA)
    finally:
        bd.cerrar()


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
def test_la_carga_se_audita_como_catalogo_carga_con_actor_cli(cargar, base_vacia):
    cargar(TRES_CARRERAS)

    [evento] = _cargas_auditadas(base_vacia)
    assert evento.actor.tipo is TipoDeActor.CLI
    assert evento.objeto == ObjetoRef("carrera")
    # Las de prueba salen de la lista: ing-civil queda, con otro orden.
    assert evento.despues == {
        "insertadas": ["ing-naval", "lic-quimica"],
        "actualizadas": ["ing-civil"],
        "desactivadas": ["ing-electronica", "ing-informatica", "ing-mecanica"],
    }


@pytest.mark.rf("RF-50")
@pytest.mark.historia(283)
@pytest.mark.parametrize(
    "contenido",
    [
        b"carrera_id;nombre\ning-naval;Naval\nIng Aero;Aeron\xe1utica\n",
        b"carrera_id;nombre\ning-naval;Naval\ning-aero\n",
        b"carrera_id;nombre\ning-naval;Naval\ning-aero;  \n",
        b"carrera_id;nombre\ning-naval;Naval\ning-naval;Otra\n",
        b"carrera_id;nombre\ning-naval;Naval\ning-aero;Naval\n",
        b"id;nombre\ning-naval;Naval\n",
        b"carrera_id;nombre\n;\n",
        b'carrera_id;nombre\ning-naval;"Naval\n',
        b"carrera_id;nombre\ning-naval;Naval\x00\n",
        # Ya existe con ese nombre: la desactivación de las de prueba ya corrió y se revierte.
        "carrera_id;nombre\ning-civil-2;Ingeniería Civil\n".encode(),
    ],
    ids=[
        "slug-invalido", "falta-el-nombre", "nombre-vacio", "carrera-repetida", "nombre-repetido",
        "sin-cabecera", "sin-carreras", "comilla-sin-cerrar", "binario", "nombre-de-otra",
    ],
)
def test_un_csv_malformado_se_rechaza_entero(cargar, base_vacia, capsys, contenido):
    antes = _filas_de_carrera(base_vacia)

    salida = cargar(contenido)

    assert salida == 1
    assert "No se cargó ninguna Carrera" in capsys.readouterr().err
    assert _filas_de_carrera(base_vacia) == antes
    assert _cargas_auditadas(base_vacia) == []


@pytest.mark.rf("RF-50")
def test_hv_app_carga_carreras_pero_no_las_borra(url_app):
    with psycopg.connect(url_app) as conexion, conexion.transaction(force_rollback=True):
        conexion.execute(
            "INSERT INTO acervo.carrera (carrera_id, nombre, orden)"
            " VALUES ('ing-naval', 'Naval', 9)"
        )
        conexion.execute("UPDATE acervo.carrera SET activo = false WHERE carrera_id = 'ing-naval'")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conexion.execute("DELETE FROM acervo.carrera WHERE carrera_id = 'ing-naval'")
