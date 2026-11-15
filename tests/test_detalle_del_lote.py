"""El detalle del Lote (084, P4): su Contexto, el progreso derivado de sus Piezas y su estado.

Por HTTP contra Postgres real, con el trabajador de procesamiento de la app y la capa falsa; la
indexación la corre el trabajador aparte de `test_indexacion`, para ver la «No indexada».
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from aplicacion import entrar
from espera import esperar_error_procesamiento, esperar_estado
from historia_viva.dominio.errores import LimiteExcedido
from test_indexacion import bd, cliente_video, trabajador, validar  # noqa: F401 (fixtures)
from test_ingesta import MP4
from test_lotes import LOTE, _llenar
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA, PDF

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")

SIN_PIEZAS = {"Recibida": 0, "Pendiente": 0, "Incompleta": 0, "Validada": 0}


def _crear(cliente, persona=MARIA) -> str:
    entrar(cliente, persona)
    lote_id: str = cliente.post("/api/lotes", json=LOTE).json()["lote"]["lote_id"]
    return lote_id


def _detalle(cliente, lote_id):
    respuesta = cliente.get(f"/api/lotes/{lote_id}")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


@pytest.mark.historia(72)
def test_un_lote_sin_piezas_esta_vacio_y_su_contexto_se_puede_editar(cliente):
    lote_id = _crear(cliente)

    detalle = _detalle(cliente, lote_id)

    assert detalle["lote"]["contexto_editable"] is True
    assert detalle["progreso"] == {
        "total": 0,
        "estado": "Vacío",
        "por_estado": SIN_PIEZAS,
        "fallidas": 0,
        "para_analisis_humano": 0,
        "sin_indexar": 0,
        "estimacion": None,
    }
    assert detalle["puede_cargar"] is True


def _subir(cliente, lote_id, nombre="acta.pdf"):
    respuesta = cliente.post(
        "/api/piezas",
        data={"lote_id": lote_id},
        files={"archivo": (nombre, PDF + nombre.encode(), "application/pdf")},
    )
    assert respuesta.status_code == 201, respuesta.text
    pieza_id: str = respuesta.json()["pieza_id"]
    return pieza_id


def _progreso(cliente, lote_id):
    return _detalle(cliente, lote_id)["progreso"]


@pytest.mark.historia(72)
def test_el_lote_pasa_de_vacio_a_procesando_al_cargar_y_a_en_validacion_cuando_termina(
    cliente, capa_modelos
):
    lote_id = _crear(cliente)
    capa_modelos.demorar()

    pieza_id = _subir(cliente, lote_id)

    detalle = _detalle(cliente, lote_id)
    assert detalle["lote"]["contexto_editable"] is False
    assert (detalle["progreso"]["estado"], detalle["progreso"]["total"]) == ("Procesando", 1)
    assert detalle["progreso"]["por_estado"] == SIN_PIEZAS | {"Recibida": 1}
    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")
    progreso = _progreso(cliente, lote_id)
    assert (progreso["estado"], progreso["por_estado"]) == (
        "En validación",
        SIN_PIEZAS | {"Pendiente": 1},
    )


@pytest.mark.historia(72, 82, 105)
def test_una_fallida_suma_aparte_y_completada_a_mano_deja_de_contar_y_destraba_el_lote(
    cliente, capa_modelos
):
    lote_id = _crear(cliente)
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    pieza_id = _subir(cliente, lote_id)
    esperar_error_procesamiento(cliente, pieza_id)

    fallida = _progreso(cliente, lote_id)
    assert (fallida["estado"], fallida["fallidas"], fallida["para_analisis_humano"]) == (
        "Procesando",
        1,
        0,
    )
    assert cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano").status_code == 200
    completada = _progreso(cliente, lote_id)
    assert (completada["estado"], completada["fallidas"], completada["para_analisis_humano"]) == (
        "En validación",
        0,
        1,
    )


@pytest.mark.historia(72)
def test_un_video_entra_pendiente_y_suma_en_analisis_humano(cliente_video):
    lote_id = cliente_video.post("/api/lotes", json=LOTE).json()["lote"]["lote_id"]

    subida = cliente_video.post(
        "/api/piezas/video", params={"nombre": "colacion.mp4", "lote_id": lote_id}, content=MP4
    )

    assert subida.status_code == 201, subida.text
    progreso = _progreso(cliente_video, lote_id)
    assert (progreso["estado"], progreso["para_analisis_humano"], progreso["fallidas"]) == (
        "En validación",
        1,
        0,
    )


def _filas(cliente, lote_id):
    respuesta = cliente.get("/api/piezas", params={"lote_id": lote_id})
    assert respuesta.status_code == 200, respuesta.text
    return {fila["nombre_archivo"]: fila for fila in respuesta.json()["piezas"]}


@pytest.mark.historia(296)
@pytest.mark.xdist_group("indice")  # embeber carga llama-index: un solo proceso lo paga (130)
def test_una_validada_sin_fragmentos_figura_no_indexada_hasta_que_corre_la_indexacion(
    cliente, trabajador
):
    lote_id = _crear(cliente)
    pieza_id = _subir(cliente, lote_id)
    esperar_estado(cliente, pieza_id, "Pendiente")
    validar(cliente, pieza_id)

    progreso = _progreso(cliente, lote_id)
    assert (progreso["estado"], progreso["sin_indexar"]) == ("Validado", 1)
    assert _filas(cliente, lote_id)["acta.pdf"]["no_indexada"] is True
    # La cola puede traer indexaciones de otros tests: se vacía entera.
    while trabajador.procesar_siguiente():
        pass
    assert _progreso(cliente, lote_id)["sin_indexar"] == 0
    assert _filas(cliente, lote_id)["acta.pdf"]["no_indexada"] is False


@pytest.mark.historia(72)
def test_las_retiradas_no_cuentan_y_leer_el_lote_es_abierto_a_todo_rol_que_consulta(
    cliente, capa_modelos, url_bd
):
    lote_id = _crear(cliente)
    capa_modelos.demorar()
    retirada, sigue = _subir(cliente, lote_id, "retirada.pdf"), _subir(cliente, lote_id)
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
            " borrada_motivo = 'Subida por error' WHERE pieza_id = %s",
            (MARIA, retirada),
        )

    lecturas = {}
    for persona in (MARIA, JORGE, DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO):
        entrar(cliente, persona)
        respuesta = cliente.get(f"/api/lotes/{lote_id}")
        lecturas[persona] = (respuesta.status_code, respuesta.json()["progreso"]["total"])

    assert lecturas == dict.fromkeys(
        (MARIA, JORGE, DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO), (200, 1)
    )
    entrar(cliente, MARIA)
    assert list(_filas(cliente, lote_id)) == ["acta.pdf"]
    capa_modelos.liberar()
    esperar_estado(cliente, sigue, "Pendiente")


@pytest.mark.historia(75)
def test_con_cien_piezas_el_lote_ya_no_admite_cargar(cliente, url_bd):
    lote_id = _crear(cliente)
    _llenar(url_bd, lote_id, 100)

    detalle = _detalle(cliente, lote_id)

    assert (detalle["progreso"]["total"], detalle["puede_cargar"]) == (100, False)


@pytest.mark.historia(105)
def test_la_lista_del_lote_marca_la_fallida_y_la_que_requiere_analisis_humano(
    cliente, capa_modelos
):
    lote_id = _crear(cliente)
    suelta = cliente.post(
        "/api/piezas", files={"archivo": ("suelta.pdf", PDF + b"suelta", "application/pdf")}
    ).json()["pieza_id"]
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    fallida = _subir(cliente, lote_id, "fallida.pdf")
    a_mano = _subir(cliente, lote_id, "a-mano.pdf")
    esperar_error_procesamiento(cliente, fallida)
    esperar_error_procesamiento(cliente, a_mano)
    assert cliente.post(f"/api/piezas/{a_mano}/completar-a-mano").status_code == 200

    filas = _filas(cliente, lote_id)

    assert {
        nombre: (f["estado"], f["fallida"], f["requiere_analisis_humano"], f["lote_id"])
        for nombre, f in filas.items()
    } == {
        "fallida.pdf": ("Recibida", True, False, lote_id),
        "a-mano.pdf": ("Pendiente", False, True, lote_id),
    }
    assert suelta not in {f["pieza_id"] for f in filas.values()}


@pytest.mark.historia(72)
def test_la_pantalla_del_detalle_del_lote_se_sirve_con_su_script(cliente):
    pantalla = cliente.get("/lote.html")

    assert pantalla.status_code == 200
    assert '<script src="/estaticos/lote.js"></script>' in pantalla.text
    assert cliente.get("/estaticos/lote.js").status_code == 200


LISTAR_EN_OTRO_PROCESO = """
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from aplicacion import CABECERAS, app_sin_trabajador, entrar
from capa_modelos_falsa import CapaModelosFalsa
capa = CapaModelosFalsa()
app = app_sin_trabajador(
    {url_app!r}, capa_para=capa.para, validar=capa.validar,
    directorio_originales=Path({originales!r}),
)
cliente = entrar(TestClient(app, headers=CABECERAS), {persona!r})
filas = cliente.get("/api/piezas", params={{"lote_id": {lote_id!r}}}).json()["piezas"]
progreso = cliente.get("/api/lotes/{lote_id}").json()["progreso"]
cargada = any(m.split(".")[0] == "llama_index" for m in sys.modules)
print([f["no_indexada"] for f in filas], progreso["sin_indexar"], cargada)
"""


@pytest.mark.historia(296)
def test_listar_piezas_y_ver_el_lote_con_validadas_no_carga_llama_index(
    cliente, url_app, directorio_originales
):
    """El sondeo de la lista y del Lote pregunta por los fragmentos sin armar el `Indice`: si lo
    armara, `web` y cada proceso de la suite cargarían llama-index (ticket 130)."""
    lote_id = _crear(cliente)
    pieza_id = _subir(cliente, lote_id)
    esperar_estado(cliente, pieza_id, "Pendiente")
    validar(cliente, pieza_id)
    codigo = LISTAR_EN_OTRO_PROCESO.format(
        url_app=url_app, originales=str(directorio_originales), persona=MARIA, lote_id=lote_id
    )

    salida = subprocess.run(
        [sys.executable, "-c", codigo], cwd=Path(__file__).parent, capture_output=True, text=True,
        check=False,
    )

    assert salida.stdout.splitlines()[-1:] == ["[True] 1 False"], salida.stderr


@pytest.mark.historia(72)
def test_la_lista_del_lote_para_quien_solo_consulta_trae_solo_las_validadas(cliente):
    """R-05: el progreso cuenta todo, pero la lista no le muestra lo no Validado."""
    lote_id = _crear(cliente)
    validada, pendiente = _subir(cliente, lote_id), _subir(cliente, lote_id, "pendiente.pdf")
    for pieza_id in (validada, pendiente):
        esperar_estado(cliente, pieza_id, "Pendiente")
    validar(cliente, validada)

    entrar(cliente, DIRECTORA_DE_CARRERA)

    assert {n: f["estado"] for n, f in _filas(cliente, lote_id).items()} == {"acta.pdf": "Validada"}
    assert _progreso(cliente, lote_id)["total"] == 2
