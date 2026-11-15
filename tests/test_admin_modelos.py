"""La administración de los modelos desde la pantalla (spec, `/api/admin/modelos`, P7a; CU-19,
RF-32, DEC-53, DEC-87, DEC-132).

Postgres real a través de la app: la API confirma desde su pool. Cada test deja las filas de
`configuracion_capacidad` como estaban, versión incluida: la base de la suite se comparte.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from aplicacion import CABECERAS, app_sin_trabajador, entrar
from espera import esperar_detras_del_candado, esperar_estado
from fastapi.testclient import TestClient
from ollama_falso import OllamaFalso
from psycopg.types.json import Jsonb
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA, PDF

from historia_viva.banco.comparacion import (
    CandidatoDeReferencia,
    Evaluacion,
    Medicion,
    Puntaje,
    eleccion_del_banco,
    escribir_comparacion,
)
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.capa_modelos.local import capa_para

pytestmark = [
    pytest.mark.rf("RF-32"),
    pytest.mark.confirma("la app confirma la sesión y los cambios de la fila desde su pool"),
    # Los cambios confirmados de `configuracion_capacidad` los ven los otros workers mientras el
    # test corre: con la búsqueda, la indexación y las semillas de `test_configuracion`, en serie.
    pytest.mark.xdist_group("indice"),
]

MODELOS = "/api/admin/modelos"


@pytest.fixture(autouse=True)
def filas_restauradas(_conexion_del_dueno: psycopg.Connection[Any]) -> Iterator[None]:
    conexion = _conexion_del_dueno
    filas = conexion.execute("SELECT * FROM acervo.configuracion_capacidad").fetchall()
    yield
    with conexion.transaction():
        for fila in filas:
            conexion.execute(
                "UPDATE acervo.configuracion_capacidad SET motor = %s, modelo = %s,"
                " instruccion_id = %s, parametros = %s, version = %s, actualizada_por = %s,"
                " actualizada_el = %s WHERE capacidad = %s",
                (fila[1], fila[2], fila[3], Jsonb(fila[4]), *fila[5:8], fila[0]),
            )


def _filas(cliente: TestClient) -> dict[str, dict[str, Any]]:
    respuesta = cliente.get(MODELOS)
    assert respuesta.status_code == 200, respuesta.text
    return {f["capacidad"]: f for f in respuesta.json()["capacidades"]}


def _cambio(fila: dict[str, Any], **parametros: Any) -> dict[str, Any]:
    """La fila tal como la leyó la pantalla, con `parametros` encima."""
    config = {k: fila[k] for k in ("motor", "modelo", "instruccion_id")}
    config["parametros"] = {**fila["parametros"], **parametros}
    return {"capacidad": fila["capacidad"], "version_base": fila["version"], "config": config}


def _guardar(cliente: TestClient, *cambios: dict[str, Any]) -> Any:
    return cliente.put(MODELOS, json={"cambios": list(cambios)})


@pytest.mark.historia(163)
def test_la_lectura_trae_las_seis_con_su_estado_y_las_instrucciones(cliente):
    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get(MODELOS)
    cuerpo = respuesta.json()

    assert respuesta.status_code == 200
    assert [f["capacidad"] for f in cuerpo["capacidades"]] == [c.value for c in Capacidad]
    filas = {f["capacidad"]: f for f in cuerpo["capacidades"]}
    # La capa falsa responde y no le falta ningún modelo; `transcribir` es el OCR local.
    assert filas["transcribir"]["descargado"] is None
    assert filas["transcribir"]["disponibilidad"] == "no_aplica"
    assert {f["disponibilidad"] for c, f in filas.items() if c != "transcribir"} == {"descargado"}
    assert filas["resumir"]["version"] >= 1
    assert filas["resumir"]["banco"] is None  # sin corrida del banco
    assert set(filas["resumir"]) == {
        "capacidad", "motor", "modelo", "instruccion_id", "parametros", "version",
        "actualizada_por", "actualizada_el", "descargado", "disponibilidad", "banco",
    }
    assert cuerpo["instrucciones"] == {
        "describir_imagen": [
            "describir_fotografia",
            "describir_imagen_por_clase",
            "describir_imagen_en_afirmaciones",
        ],
        "transcribir": [],
        "resumir": ["resumir_ficha"],
        "embeber": [],
        "responder": ["responder_en_conversacion", "reformular_con_historial"],
        "redactar_efemeride": ["redactar_efemeride_dia", "redactar_efemeride_mes"],
    }


@pytest.mark.historia(164)
@pytest.mark.parametrize("persona", [MARIA, JORGE, DIRECTORA_DE_CARRERA])
def test_sin_operar_sistema_toda_ruta_da_403(cliente, persona):
    entrar(cliente, persona)
    pedidos = [
        cliente.get(MODELOS),
        cliente.put(MODELOS, json={"cambios": []}),
        cliente.post(f"{MODELOS}/verificacion", json={"cambios": []}),
        cliente.get(f"{MODELOS}/resumir/historial"),
    ]

    assert [p.status_code for p in pedidos] == [403] * 4


@pytest.mark.historia(164)
def test_guardar_sube_la_version_audita_lo_que_cambio_y_el_historial_lo_lista(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    resumir = _filas(cliente)["resumir"]

    respuesta = _guardar(cliente, _cambio(resumir, timeout_s=60))
    cuerpo = respuesta.json()
    historial = cliente.get(f"{MODELOS}/resumir/historial").json()

    assert respuesta.status_code == 200, respuesta.text
    assert cuerpo["aplica_a"] == "trabajos nuevos"
    assert (cuerpo["avisos"], cuerpo["requiere_reindexado"]) == ([], False)
    nueva = {f["capacidad"]: f for f in cuerpo["filas"]}["resumir"]
    assert nueva["version"] == resumir["version"] + 1
    assert nueva["parametros"]["timeout_s"] == 60
    assert nueva["actualizada_por"] == ADMINISTRADOR_TECNICO
    [evento] = historial["eventos"]
    assert evento["persona"] == "tibarra@fie.undef.edu.ar"
    # Solo lo que cambió, clave por clave.
    assert (evento["antes"], evento["despues"]) == (
        {"parametros": {"timeout_s": resumir["parametros"]["timeout_s"]}},
        {"parametros": {"timeout_s": 60}},
    )
    assert historial["siguiente"] is None
    assert cliente.get(f"{MODELOS}/responder/historial").json()["eventos"] == []


@pytest.mark.historia(164)
def test_una_version_base_vieja_da_409_con_la_fila_actual_y_no_guarda_nada(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    filas = _filas(cliente)
    assert _guardar(cliente, _cambio(filas["resumir"], timeout_s=60)).status_code == 200

    # El segundo Administrador técnico leyó antes del primer guardado; también cambia `describir`.
    respuesta = _guardar(
        cliente, _cambio(filas["describir_imagen"], timeout_s=100),
        _cambio(filas["resumir"], timeout_s=70),
    )
    despues = _filas(cliente)

    assert respuesta.status_code == 409
    cuerpo = respuesta.json()
    assert cuerpo["type"] == "/errores/version-desactualizada"
    assert (cuerpo["capacidad"], cuerpo["version_base"]) == ("resumir", filas["resumir"]["version"])
    assert cuerpo["fila_actual"]["parametros"]["timeout_s"] == 60
    assert despues["resumir"]["parametros"]["timeout_s"] == 60
    assert despues["describir_imagen"] == filas["describir_imagen"]


@pytest.mark.historia(164)
def test_guardar_sin_diferencias_no_sube_la_version_ni_audita(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    resumir = _filas(cliente)["resumir"]

    respuesta = _guardar(cliente, _cambio(resumir))

    assert respuesta.status_code == 200
    assert _filas(cliente)["resumir"]["version"] == resumir["version"]
    assert cliente.get(f"{MODELOS}/resumir/historial").json()["eventos"] == []


@pytest.mark.historia(164)
@pytest.mark.rnf("RNF-02")
@pytest.mark.parametrize("modelo", ["gemma4:cloud", "gpt-oss:120b-cloud", "Qwen3-Coder:480B-Cloud"])
def test_un_modelo_cloud_de_ollama_se_rechaza_porque_corre_fuera_de_la_pc(cliente, modelo):
    # S-MOD-05: la etiqueta `cloud` (o `-cloud`) de Ollama corre en ollama.com, contra RNF-02.
    entrar(cliente, ADMINISTRADOR_TECNICO)
    filas = _filas(cliente)
    nube = _cambio(filas["resumir"])
    nube["config"]["modelo"] = modelo

    respuesta = _guardar(cliente, nube)

    assert respuesta.status_code == 422
    [error] = respuesta.json()["errores"]
    assert (error["capacidad"], error["campo"]) == ("resumir", "modelo")
    assert "nube" in error["mensaje"] and "RNF-02" in error["mensaje"]
    assert _filas(cliente) == filas
    # Un nombre local con «cloud» en otro lado no es de la nube.
    local = _cambio(filas["resumir"])
    local["config"]["modelo"] = "cloudy-llama:3b"
    verificado = cliente.post(f"{MODELOS}/verificacion", json={"cambios": [local]}).json()
    assert verificado["ok"] is True


@pytest.mark.historia(164)
def test_un_conjunto_invalido_da_422_con_todos_los_errores_y_no_guarda_ninguna_fila(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    filas = _filas(cliente)
    valido = _cambio(filas["describir_imagen"], timeout_s=100)
    invalido = _cambio(filas["resumir"])
    invalido["config"] |= {"motor": "olama", "instruccion_id": "resumir_ficha_v2"}

    respuesta = _guardar(cliente, valido, invalido)

    assert respuesta.status_code == 422
    cuerpo = respuesta.json()
    assert cuerpo["type"] == "/errores/configuracion-invalida"
    assert {(e["capacidad"], e["campo"]) for e in cuerpo["errores"]} >= {
        ("resumir", "motor"), ("resumir", "instruccion_id")
    }
    assert _filas(cliente) == filas


@pytest.mark.historia(164)
def test_el_num_ctx_de_un_modelo_compartido_se_cambia_en_todas_sus_filas_juntas(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    filas = _filas(cliente)
    # `responder` y `redactar_efemeride` usan el modelo de `resumir` (052, 057).
    compartidas = ("resumir", "responder", "redactar_efemeride")
    num_ctx = filas["resumir"]["parametros"]["num_ctx"] + 1024

    sola = _guardar(cliente, _cambio(filas["resumir"], num_ctx=num_ctx))
    juntas = _guardar(cliente, *(_cambio(filas[c], num_ctx=num_ctx) for c in compartidas))

    assert sola.status_code == 422
    assert {e["campo"] for e in sola.json()["errores"]} == {"num_ctx"}
    assert juntas.status_code == 200, juntas.text
    assert {_filas(cliente)[c]["parametros"]["num_ctx"] for c in compartidas} == {num_ctx}


@pytest.mark.historia(164)
def test_el_cambio_de_redactar_efemeride_queda_auditado_como_cualquier_otro(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    fila = _filas(cliente)["redactar_efemeride"]
    cambio = _cambio(fila)
    cambio["config"]["instruccion_id"] = "redactar_efemeride_mes"

    assert _guardar(cliente, cambio).status_code == 200
    [evento] = cliente.get(f"{MODELOS}/redactar_efemeride/historial").json()["eventos"]
    assert evento["despues"] == {"instruccion_id": "redactar_efemeride_mes"}


@pytest.mark.historia(164)
def test_dos_guardados_a_la_vez_que_juntos_no_validan_nunca_dejan_el_conjunto_invalido(
    cliente, url_bd
):
    filas = _filas(entrar(cliente, ADMINISTRADOR_TECNICO))
    num_ctx = filas["resumir"]["parametros"]["num_ctx"]
    # Cada uno valida solo; juntos dejan un modelo con dos `num_ctx` (DEC-06).
    resumir = _cambio(filas["resumir"])
    resumir["config"]["modelo"] = "compartido-de-prueba"
    describir = _cambio(filas["describir_imagen"], num_ctx=num_ctx + 1024)
    describir["config"]["modelo"] = "compartido-de-prueba"
    uno, otro = (entrar(TestClient(cliente.app, headers=CABECERAS), ADMINISTRADOR_TECNICO)
                 for _ in range(2))

    with psycopg.connect(url_bd) as candado:
        candado.execute("SELECT 1 FROM acervo.configuracion_capacidad FOR UPDATE")
        with ThreadPoolExecutor(2) as hilos:
            guardados = [
                hilos.submit(_guardar, uno, resumir),
                hilos.submit(_guardar, otro, describir),
            ]
            esperar_detras_del_candado(candado, 2)
            candado.commit()
            respuestas = [g.result() for g in guardados]

    assert sorted(r.status_code for r in respuestas) == [200, 422]
    [rechazo] = [r.json() for r in respuestas if r.status_code == 422]
    assert {e["campo"] for e in rechazo["errores"]} == {"num_ctx"}


def test_una_capacidad_repetida_o_un_historial_inexistente_se_rechazan(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    resumir = _filas(cliente)["resumir"]

    repetida = _guardar(cliente, _cambio(resumir), _cambio(resumir, timeout_s=60))
    historial = cliente.get(f"{MODELOS}/resumen/historial")

    assert (repetida.status_code, repetida.json()["type"]) == (422, "/errores/pedido-invalido")
    assert (historial.status_code, historial.json()["type"]) == (
        404, "/errores/recurso-no-encontrado"
    )


@pytest.mark.historia(164)
def test_la_verificacion_valida_sin_guardar(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    filas = _filas(cliente)
    invalido = _cambio(filas["embeber"], dimension=2048)

    con_error = cliente.post(f"{MODELOS}/verificacion", json={"cambios": [invalido]}).json()
    sin_error = cliente.post(
        f"{MODELOS}/verificacion", json={"cambios": [_cambio(filas["resumir"], timeout_s=60)]}
    ).json()

    assert con_error["ok"] is False
    assert [(e["capacidad"], e["campo"]) for e in con_error["errores"]] == [
        ("embeber", "dimension")
    ]
    assert sin_error == {
        "ok": True,
        "errores": [],
        "avisos": [],
        "disponibilidad": {
            f["capacidad"]: f["disponibilidad"] for f in filas.values()
        },
    }
    assert _filas(cliente) == filas


@pytest.mark.historia(166)
def test_cambiar_embeber_devuelve_requiere_reindexado_y_nombra_el_comando(cliente):
    entrar(cliente, ADMINISTRADOR_TECNICO)
    embeber = _filas(cliente)["embeber"]
    cambio = _cambio(embeber, prefijo_consulta="query: ")

    verificado = cliente.post(f"{MODELOS}/verificacion", json={"cambios": [cambio]}).json()
    guardado = _guardar(cliente, cambio).json()

    assert [a["codigo"] for a in verificado["avisos"]] == ["requiere_reindexado"]
    assert guardado["requiere_reindexado"] is True
    [aviso] = guardado["avisos"]
    assert (aviso["capacidad"], aviso["codigo"]) == ("embeber", "requiere_reindexado")
    assert "`reindexar`" in aviso["mensaje"]


def _app_con_ollama(url_app: str, motor: OllamaFalso, tmp_path: Path) -> TestClient:
    return TestClient(
        app_sin_trabajador(
            url_app,
            capa_para=partial(capa_para, motores=motor.motores()),
            validar=motor.validar,
            directorio_originales=tmp_path,
        ),
        headers=CABECERAS,
    )


@pytest.mark.historia(165)
def test_un_modelo_no_descargado_avisa_y_se_guarda_igual(url_app, tmp_path):
    motor = OllamaFalso()
    with _app_con_ollama(url_app, motor, tmp_path) as cliente:
        entrar(cliente, ADMINISTRADOR_TECNICO)
        resumir = _filas(cliente)["resumir"]
        motor.modelos_descargados = [resumir["modelo"]]
        cambio = _cambio(resumir)
        cambio["config"]["modelo"] = "otro-modelo-de-prueba"

        verificado = cliente.post(f"{MODELOS}/verificacion", json={"cambios": [cambio]}).json()
        guardado = _guardar(cliente, cambio)
        filas = _filas(cliente)

    assert verificado["disponibilidad"]["resumir"] == "no_descargado"
    assert guardado.status_code == 200
    [aviso] = guardado.json()["avisos"]
    assert (aviso["capacidad"], aviso["codigo"]) == ("resumir", "modelo_no_descargado")
    assert (filas["resumir"]["modelo"], filas["resumir"]["descargado"]) == (
        "otro-modelo-de-prueba", False
    )
    # `responder` sigue con el modelo de antes, que sí está.
    assert filas["responder"]["descargado"] is True


@pytest.mark.historia(165)
def test_con_el_motor_apagado_la_lectura_dice_que_no_responde(url_app, tmp_path):
    motor = OllamaFalso()
    motor.prendido = False
    with _app_con_ollama(url_app, motor, tmp_path) as cliente:
        filas = _filas(entrar(cliente, ADMINISTRADOR_TECNICO))

    assert {f["disponibilidad"] for c, f in filas.items() if c != "transcribir"} == {
        "motor_no_responde"
    }
    assert {f["descargado"] for f in filas.values()} == {None}


def _corrida(directorio: Path, capacidad: Capacidad, *candidatos: tuple[str, bool, int]) -> None:
    """Una corrida de `banco comparar` con el escritor real: (modelo, origen_chino, aciertos)."""
    evaluaciones = [
        Evaluacion(
            capacidad=capacidad,
            candidato=CandidatoDeReferencia(capacidad, modelo, chino, {}),
            configuracion_efectiva={
                "digest": modelo,
                "por_capacidad": {capacidad.value: {"parametros": {"timeout_s": 90}}},
            },
            corrida="2026-10-19T10:00:00",
            mediciones=tuple(
                Medicion("p1", n, 1000, 10, 10, "texto",
                         Puntaje(("hecho",), (), ()) if n < aciertos else Puntaje((), ("hecho",), ()))
                for n in range(3)
            ),
        )
        for modelo, chino, aciertos in candidatos
    ]
    escribir_comparacion(directorio, capacidad, evaluaciones, hash_referencia="h",
                         momento=datetime(2026, 10, 19, 10))


@pytest.mark.historia(169)
def test_con_una_corrida_del_banco_la_fila_muestra_el_ganador_y_el_mejor_no_chino(
    cliente, tmp_path
):
    _corrida(tmp_path, Capacidad.RESUMIR, ("modelo-chino", True, 3), ("modelo-local", False, 1))
    # La app de la suite no lee HV_REFERENCIA: se le da el directorio como `componer_web`.
    cliente.app.state.eleccion_del_banco = partial(eleccion_del_banco, tmp_path)  # type: ignore[attr-defined]

    filas = _filas(entrar(cliente, ADMINISTRADOR_TECNICO))

    assert filas["resumir"]["banco"] == {
        "ganador": "modelo-chino", "mejor_no_chino": "modelo-local"
    }
    assert filas["describir_imagen"]["banco"] is None


def test_la_eleccion_del_banco_es_la_de_la_ultima_corrida_legible(tmp_path):
    assert eleccion_del_banco(tmp_path, "resumir") is None
    _corrida(tmp_path, Capacidad.RESUMIR, ("modelo-local", False, 3))
    (tmp_path / "comparar-resumir-20991231T000000.json").write_text("{", encoding="utf-8")

    assert eleccion_del_banco(tmp_path, "resumir") == ("modelo-local", "modelo-local")


def _subir(cliente: TestClient, n: int) -> str:
    archivo = (f"acta-{n}.pdf", PDF + str(n).encode(), "application/pdf")
    pieza_id: str = cliente.post("/api/piezas", files={"archivo": archivo}).json()["pieza_id"]
    return pieza_id


@pytest.mark.historia(164)
def test_un_trabajo_encolado_antes_de_guardar_desde_la_pantalla_conserva_su_instantanea(
    cliente, capa_modelos, url_bd
):
    entrar(cliente, MARIA)
    # El trabajador toma la primera y queda esperando a la capa: la segunda sigue encolada.
    capa_modelos.demorar()
    antes = [_subir(cliente, 1), _subir(cliente, 2)]
    entrar(cliente, ADMINISTRADOR_TECNICO)
    assert _guardar(cliente, _cambio(_filas(cliente)["resumir"], timeout_s=60)).status_code == 200
    entrar(cliente, MARIA)
    despues = _subir(cliente, 3)
    capa_modelos.liberar()
    for pieza_id in (*antes, despues):
        esperar_estado(cliente, pieza_id, "Pendiente")

    # Se mira lo que congeló cada trabajo, no las invocaciones: el doble es uno solo y la
    # pestaña lo rearma con las filas que mira (`capa_para`), cosa que la capa real no comparte.
    with psycopg.connect(url_bd) as conexion:
        tiempos = dict(conexion.execute(
            "SELECT pieza_id::text,"
            " configuracion_efectiva->'por_capacidad'->'resumir'->'parametros'->'timeout_s'"
            " FROM acervo.trabajo_procesamiento"
        ).fetchall())
    assert [tiempos[p] for p in (*antes, despues)] == [90, 90, 60]
