"""El procesamiento completo con la capa real (`capa_para`) y el motor en su borde HTTP (issue 13).

El resto de la suite sustituye la capa entera por `CapaModelosFalsa`. Acá la capa es la real
—PyMuPDF para el PDF, plantillas, delimitación, topes— y solo el motor es un
`httpx.MockTransport`. Es la prueba de que el trabajador y la capa real encajan, y de que con
el motor apagado la aplicación entra en el modo sin modelos del issue 11 sin caerse.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pymupdf
import pytest
from aplicacion import CABECERAS, app_sin_trabajador, en_marcha, entrar
from espera import esperar_estado
from fastapi.testclient import TestClient
from ollama_falso import OllamaFalso
from PIL import Image, ImageDraw, ImageFont
from validacion import ADMINISTRADOR_TECNICO, MARIA

from historia_viva.bd import Bd
from historia_viva.capa_modelos.local import capa_para
from historia_viva.composicion import armar_trabajador
from historia_viva.procesamiento.manejador import ENCABEZADO_TEXTO_VISIBLE

pytestmark = [
    pytest.mark.rf("RF-05"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

_TEXTO_DEL_ACTA = "Acta de fundacion. Se resolvio crear la Facultad."
_RESUMEN_DEL_MOTOR = "Acta que registra la creacion de la Facultad."


def _pdf_con_texto(texto: str) -> bytes:
    documento = pymupdf.open()
    documento.new_page().insert_text((72, 72), texto)
    contenido = documento.tobytes()
    documento.close()
    return contenido


@pytest.fixture
def motor() -> OllamaFalso:
    motor = OllamaFalso()
    motor.texto = _RESUMEN_DEL_MOTOR
    return motor


@pytest.fixture
def cliente(url_bd: str, motor: OllamaFalso, tmp_path: Path) -> Iterator[TestClient]:
    with en_marcha(
        url_bd,
        capa_para=partial(capa_para, motores=motor.motores()),
        validar=motor.validar,
        directorio_originales=tmp_path,
    ) as cliente:
        entrar(cliente, MARIA)
        yield cliente


def _subir_acta(cliente: TestClient) -> str:
    return cliente.post(
        "/api/piezas",
        files={"archivo": ("acta.pdf", _pdf_con_texto(_TEXTO_DEL_ACTA), "application/pdf")},
    ).json()["pieza_id"]


def test_un_documento_real_queda_pendiente_con_el_resumen_propuesto_por_el_motor(
    cliente, motor
):
    pieza_id = _subir_acta(cliente)

    esperar_estado(cliente, pieza_id, "Pendiente")
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert _TEXTO_DEL_ACTA in ficha["cuerpo"]
    assert ficha["frontmatter"]["resumen"] == _RESUMEN_DEL_MOTOR
    # El acta viajó al motor por el canal de datos, nunca por el de instrucciones.
    (chat,) = motor.chats()
    sistema, usuario = chat["messages"]
    assert _TEXTO_DEL_ACTA not in sistema["content"]
    assert _TEXTO_DEL_ACTA in usuario["content"]


@pytest.mark.rnf("RNF-05")
def test_con_el_motor_apagado_la_pieza_espera_recibida_y_la_aplicacion_sigue_andando(
    url_bd, motor, tmp_path
):
    # Apagado desde antes de arrancar: el cortacircuito ya lo vio caído y no toma (DEC-86).
    motor.prendido = False
    with en_marcha(
        url_bd,
        capa_para=partial(capa_para, motores=motor.motores()),
        validar=motor.validar,
        directorio_originales=tmp_path,
    ) as cliente:
        entrar(cliente, MARIA)
        pieza_id = _subir_acta(cliente)

        detalle = cliente.get(f"/api/piezas/{pieza_id}").json()
        salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()
        assert cliente.get("/api/piezas").status_code == 200

    assert (detalle["estado"], detalle["error_procesamiento"]) == ("Recibida", None)
    assert motor.chats() == []
    assert (salud["estado"], salud["capa_modelos"]) == ("degradado", "sin_respuesta")


@pytest.mark.rnf("RNF-05")
def test_al_volver_el_motor_lo_encolado_se_procesa_solo(url_bd, motor, tmp_path):
    capa = partial(capa_para, motores=motor.motores())
    web = TestClient(
        app_sin_trabajador(
            url_bd, capa_para=capa, validar=motor.validar, directorio_originales=tmp_path
        ),
        headers=CABECERAS,
    )
    entrar(web, MARIA)
    pieza_id = _subir_acta(web)
    ahora = [0.0]
    bd = Bd(url_bd, tamano_pool=2)
    trabajador = armar_trabajador(
        bd=bd, capa_para=capa, directorio_originales=tmp_path, reloj=lambda: ahora[0]
    )
    motor.prendido = False
    try:
        assert trabajador.procesar_siguiente() is False
        motor.prendido = True
        ahora[0] += 10  # la salud del motor se cachea 10 s (DEC-86)
        assert trabajador.procesar_siguiente() is True
    finally:
        bd.cerrar()

    assert web.get(f"/api/piezas/{pieza_id}").json()["estado"] == "Pendiente"


@pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="Necesita el binario de Tesseract instalado (con el idioma 'spa') en el PATH.",
)
def test_una_foto_real_queda_pendiente_con_descripcion_texto_visible_y_resumen(
    cliente, motor
):
    """El flujo completo de una fotografía con la capa real: describir en el motor, leer el
    texto visible con Tesseract y resumir los dos juntos (user stories 7 y 8). La fila vigente
    pide el esquema (144): la descripción son Afirmaciones y el cuerpo de la v1, solo lo leído."""
    motor.texto = json.dumps({
        "tipo_imagen": "Cartel o gráfico", "afirmaciones": ["Un cartel sobre fondo blanco."],
        "no_determinable": [],
    })
    foto = Image.new("RGB", (1800, 400), "white")
    fuente = ImageFont.truetype("arial.ttf" if sys.platform == "win32" else "DejaVuSans.ttf", 96)
    ImageDraw.Draw(foto).text((40, 120), "FACULTAD 1975", fill="black", font=fuente)
    buffer = io.BytesIO()
    foto.save(buffer, format="PNG")

    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ("cartel.png", buffer.getvalue(), "image/png")}
    ).json()["pieza_id"]

    esperar_estado(cliente, pieza_id, "Pendiente")
    cuerpo = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["cuerpo"]
    assert cuerpo.startswith(ENCABEZADO_TEXTO_VISIBLE)
    assert "FACULTAD" in cuerpo
    describir, resumir = motor.chats()
    # Los modelos de las filas vigentes: las de la semilla (0003).
    assert describir["model"] == "qwen3-vl:2b-instruct"
    assert resumir["model"] == "qwen2.5:3b-instruct-q4_K_M"
    assert "Un cartel sobre fondo blanco." in resumir["messages"][1]["content"]
    assert "FACULTAD" in resumir["messages"][1]["content"]
