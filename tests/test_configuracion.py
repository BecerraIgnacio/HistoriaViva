"""La configuración vigente de los modelos: una fila por capacidad (013, RF-32, DEC-87).

Contra Postgres real: la aplicación lee las filas como `hv_app`, y el Administrador técnico las
edita por SQL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from pathlib import Path

import psycopg
import pytest
from aplicacion import app_sin_trabajador, entrar
from espera import esperar_estado
from fastapi.testclient import TestClient
from ollama_falso import OllamaFalso
from validacion import ADMINISTRADOR_TECNICO, MARIA, PDF

from historia_viva.bd import Bd
from historia_viva.capa_modelos.configuracion import (
    MOTOR_LOCAL,
    TIEMPO_MAXIMO_DEL_TRABAJO_S,
    ConfiguracionEfectiva,
)
from historia_viva.dominio.errores import ConfiguracionInvalida
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.capa_modelos.local import capa_para
from historia_viva.configuracion import ConfiguracionDeModelos

# En el grupo de `test_admin_modelos`, que confirma cambios en las filas que estas semillas leen.
pytestmark = [pytest.mark.rf("RF-32"), pytest.mark.xdist_group("indice")]


@pytest.fixture
def configuracion(url_app: str) -> Iterator[ConfiguracionDeModelos]:
    bd = Bd(url_app, tamano_pool=1)
    yield ConfiguracionDeModelos(bd=bd, validar=OllamaFalso().validar)
    bd.cerrar()


def test_sin_la_fila_pedida_la_instantanea_dice_que_falta(configuracion, borrar_fila):
    # La fila de `embeber` no es de R1: el conjunto valida sin ella, pero la indexación la pide.
    borrar_fila("embeber")

    with pytest.raises(ConfiguracionInvalida, match="embeber: falta la fila"):
        configuracion.instantanea(capacidades=(Capacidad.EMBEBER,))


@pytest.mark.historia(163)
def test_la_semilla_deja_las_capacidades_de_r1_embeber_y_responder_validas(configuracion):
    vigente = configuracion.vigente()

    assert set(vigente) == {
        Capacidad.DESCRIBIR_IMAGEN,
        Capacidad.TRANSCRIBIR,
        Capacidad.RESUMIR,
        Capacidad.EMBEBER,
        Capacidad.RESPONDER,
        Capacidad.REDACTAR_EFEMERIDE,
    }
    assert vigente[Capacidad.DESCRIBIR_IMAGEN].modelo == "qwen3-vl:2b-instruct"
    assert vigente[Capacidad.TRANSCRIBIR].motor == MOTOR_LOCAL
    assert vigente[Capacidad.TRANSCRIBIR].modelo is None
    assert vigente[Capacidad.RESUMIR].modelo
    capa_para(vigente, OllamaFalso().motores())


@pytest.mark.historia(163, 166)
@pytest.mark.rf("RF-20")
def test_la_semilla_de_embeber_va_al_segundo_ollama_con_dimension_y_prefijos(configuracion):
    embeber = configuracion.vigente()[Capacidad.EMBEBER]

    # DEC-03 y DEC-06: `bge-m3` provisorio, 1024 nativos, en el Ollama de embeddings.
    assert (embeber.motor, embeber.modelo, embeber.instruccion_id) == (
        "ollama_embeddings",
        "bge-m3",
        None,
    )
    assert embeber.parametros["dimension"] == 1024
    # `bge-m3` no lleva prefijos; un e5 pondría `passage: ` y `query: ` en la fila.
    assert (embeber.parametros["prefijo_fragmento"], embeber.parametros["prefijo_consulta"]) == (
        "",
        "",
    )


@pytest.mark.historia(163)
@pytest.mark.rf("RF-47")
def test_la_semilla_de_responder_usa_el_modelo_de_resumir_con_la_instruccion_de_la_conversacion(
    configuracion,
):
    vigente = configuracion.vigente()
    responder, resumir = vigente[Capacidad.RESPONDER], vigente[Capacidad.RESUMIR]

    # Provisorio hasta el banco de `responder` (078): el de `resumir`, con su `num_ctx`.
    assert (responder.motor, responder.modelo) == (resumir.motor, resumir.modelo)
    assert responder.parametros["num_ctx"] == resumir.parametros["num_ctx"]
    assert responder.instruccion_id == "responder_en_conversacion"
    assert {"max_tokens_entrada", "max_tokens_salida", "timeout_s"} <= set(responder.parametros)


@pytest.mark.historia(164)
def test_la_instantanea_tiene_digest_y_vuelve_igual_de_su_serializacion(
    configuracion, editar_fila
):
    instantanea = configuracion.instantanea()
    serializada = json.loads(json.dumps(instantanea.a_json()))

    assert ConfiguracionEfectiva.desde_json(serializada) == instantanea
    assert serializada["digest"] == instantanea.digest
    assert re.fullmatch(r"[0-9a-f]{64}", instantanea.digest)
    assert configuracion.instantanea().digest == instantanea.digest

    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET parametros = parametros || '{\"timeout_s\": 60}'"
        " WHERE capacidad = 'resumir'"
    )

    assert configuracion.instantanea().digest != instantanea.digest


def _subir(cliente: TestClient, n: int) -> str:
    archivo = (f"acta-{n}.pdf", PDF + str(n).encode(), "application/pdf")
    pieza_id: str = cliente.post("/api/piezas", files={"archivo": archivo}).json()["pieza_id"]
    return pieza_id


@pytest.mark.historia(164)
@pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")
def test_un_trabajo_encolado_antes_de_cambiar_la_fila_conserva_su_instantanea(
    cliente, capa_modelos, editar_fila
):
    entrar(cliente, MARIA)
    # El trabajador toma la primera y queda esperando a la capa: la segunda sigue encolada.
    capa_modelos.demorar()
    antes = [_subir(cliente, 1), _subir(cliente, 2)]
    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET parametros = parametros || '{\"timeout_s\": 60}'"
        " WHERE capacidad = 'resumir'"
    )
    despues = _subir(cliente, 3)
    capa_modelos.liberar()
    for pieza_id in (*antes, despues):
        esperar_estado(cliente, pieza_id, "Pendiente")

    assert [
        invocacion.limites.tiempo_maximo_s
        for invocacion in capa_modelos.invocaciones
        if invocacion.capacidad is Capacidad.RESUMIR
    ] == [90, 90, 60]


@pytest.mark.historia(164)
@pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")
def test_el_presupuesto_del_trabajo_sale_de_su_instantanea(cliente, editar_fila, url_bd):
    # 120 s pasa del tope que `resumir` tenía fijo en el código (90 s): ahora manda la fila.
    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET parametros = parametros || '{\"timeout_s\": 120}'"
        " WHERE capacidad = 'resumir'"
    )
    entrar(cliente, MARIA)
    pieza_id = _subir(cliente, 1)
    esperar_estado(cliente, pieza_id, "Pendiente")

    with psycopg.connect(url_bd) as conexion:
        tiempo_maximo_ms, arriendo = conexion.execute(
            "SELECT tiempo_maximo_ms, arriendo_vence_el - iniciado_el"
            " FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()

    # Un documento: `transcribir` (300 s) más `resumir`.
    assert tiempo_maximo_ms == 420_000
    assert arriendo >= timedelta(seconds=420)


def test_el_tope_del_trabajo_es_el_check_de_la_base(tx):
    (definicion,) = tx.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'acervo.trabajo_procesamiento'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) LIKE '%tiempo_maximo_ms%'"
    ).fetchone()
    tope = re.search(r"tiempo_maximo_ms <= (\d+)", definicion)

    assert tope is not None, definicion
    assert int(tope[1]) == 1000 * TIEMPO_MAXIMO_DEL_TRABAJO_S


def _archivos(raiz: Path) -> list[Path]:
    return sorted(ruta for ruta in raiz.rglob("*") if ruta.is_file())


@pytest.mark.historia(165)
@pytest.mark.parametrize(
    "edicion",
    [
        ("UPDATE acervo.configuracion_capacidad SET parametros = parametros - 'timeout_s'"
        " WHERE capacidad = 'resumir'"),
        ("UPDATE acervo.configuracion_capacidad"
        " SET parametros = parametros || '{\"timeout_s\": \"90\"}' WHERE capacidad = 'resumir'"),
        ("UPDATE acervo.configuracion_capacidad"
        " SET parametros = parametros || '{\"timeout_s\": 1700}' WHERE capacidad = 'resumir'"),
    ],
    ids=["sin-timeout", "timeout-no-entero", "suma-mayor-que-un-trabajo"],
)
@pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")
def test_una_fila_invalida_rechaza_la_subida_sin_dejar_pieza_ni_original(
    cliente, editar_fila, directorio_originales, edicion
):
    entrar(cliente, MARIA)
    primera = _subir(cliente, 1)
    originales = _archivos(directorio_originales)
    editar_fila(edicion)

    respuesta = cliente.post(
        "/api/piezas", files={"archivo": ("acta-2.pdf", PDF + b"2", "application/pdf")}
    )
    duplicada = cliente.post(
        "/api/piezas", files={"archivo": ("acta-1.pdf", PDF + b"1", "application/pdf")}
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["type"] == "/errores/configuracion-invalida"
    assert {"capacidad": "resumir", "campo": "timeout_s"}.items() <= next(
        error for error in respuesta.json()["errores"] if error["capacidad"] == "resumir"
    ).items()
    assert [pieza["pieza_id"] for pieza in cliente.get("/api/piezas").json()["piezas"]] == [
        primera
    ]
    assert _archivos(directorio_originales) == originales
    # El duplicado no encola nada: no necesita la configuración y sigue siendo 409.
    assert duplicada.status_code == 409
    assert duplicada.json()["pieza_id"] == primera


@pytest.mark.historia(165)
@pytest.mark.confirma("la sesión del Administrador técnico la confirma la app")
def test_un_modelo_sin_descargar_avisa_en_la_salud_y_no_impide_arrancar(
    configuracion, url_app, tmp_path
):
    motor = OllamaFalso()
    vigente = configuracion.vigente()
    motor.modelos_descargados = [
        vigente[Capacidad.DESCRIBIR_IMAGEN].modelo or "",
        vigente[Capacidad.EMBEBER].modelo or "",
    ]
    app = app_sin_trabajador(
        url_app,
        capa_para=partial(capa_para, motores=motor.motores()),
        validar=motor.validar,
        directorio_originales=tmp_path,
    )

    with TestClient(app) as cliente:
        salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert salud["estado"] == "degradado"
    assert salud["capa_modelos"] == "modelos_faltantes"
    # `responder` usa el modelo de `resumir` (052) y `redactar_efemeride`, el de `responder` (057).
    assert sorted(salud["modelos_faltantes"]) == ["redactar_efemeride", "responder", "resumir"]
    # Una sola consulta a cada motor por salud: el healthcheck del compose corta a los 5 s.
    assert sorted(p.url.host for p in motor.peticiones if p.url.path == "/api/tags") == [
        "motor",
        "motor-embeddings",
    ]


@pytest.mark.historia(165)
@pytest.mark.confirma("la sesión del Administrador técnico la confirma la app")
def test_una_fila_invalida_editada_en_marcha_aparece_en_la_salud(url_app, editar_fila, tmp_path):
    motor = OllamaFalso()
    app = app_sin_trabajador(
        url_app,
        capa_para=partial(capa_para, motores=motor.motores()),
        validar=motor.validar,
        directorio_originales=tmp_path,
    )

    with TestClient(app) as cliente:
        editar_fila(
            "UPDATE acervo.configuracion_capacidad SET motor = 'olama'"
            " WHERE capacidad = 'resumir'"
        )
        salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert salud["estado"] == "degradado"
    assert salud["capa_modelos"] == "configuracion_invalida"
