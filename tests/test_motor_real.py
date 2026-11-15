"""Descripción, resumen y embeddings contra un Ollama real (issue 13, criterio 1; 045, S-15).

Corren con la configuración vigente de la base de la suite (la semilla) y se saltean si el
motor de `HV_MOTOR_URL` (por defecto `localhost:11434`) no contesta o le faltan esos modelos:
un Ollama local (el del compose no publica puertos). Lo que verifican es el contrato con la
persona que valida —que llegue una propuesta en español—, no la calidad de la propuesta: eso
lo mide el banco de pruebas.
"""

from __future__ import annotations

import io
import os
import platform
import re
from functools import partial

import httpx
import pytest
from PIL import Image, ImageDraw

from historia_viva.bd import Bd
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONSULTA,
    ETIQUETA_CUERPO_FICHA,
    ETIQUETA_IMAGEN,
    BloqueBinario,
    BloqueDatos,
    CapaModelos,
    Capacidad,
    UsoEmbedding,
)
from historia_viva.capa_modelos.configuracion import validar
from historia_viva.capa_modelos.local import capa_para
from historia_viva.composicion import Entorno, motores
from historia_viva.configuracion import ConfiguracionDeModelos

pytestmark = pytest.mark.rf("RF-08", "RF-09")


_LOCAL = "http://localhost:11434"


@pytest.fixture(scope="module")
def capa(url_app: str) -> CapaModelos:
    bd = Bd(url_app, tamano_pool=1)
    # Un Ollama local: los del compose no publican puertos. Uno solo atiende a los dos ids.
    variables = {"HV_MOTOR_URL": _LOCAL, "HV_MOTOR_EMBEDDINGS_URL": _LOCAL, **os.environ}
    del_entorno = motores(Entorno.desde_variables(variables))
    try:
        configuracion = ConfiguracionDeModelos(bd=bd, validar=partial(validar, motores=del_entorno))
        capa = capa_para(configuracion.vigente(), del_entorno)
    finally:
        bd.cerrar()
    if not capa.verificar_salud() or capa.modelos_faltantes():
        pytest.skip("Necesita el motor local prendido y con sus modelos descargados.")
    return capa

_PALABRAS_DEL_ESPANOL = re.compile(r"\b(de|la|el|en|una|con|los|las|que)\b", re.I)
_PALABRAS_DEL_INGLES = re.compile(r"\b(the|with|and|of|is)\b", re.I)


def _en_espanol(texto: str) -> bool:
    return len(_PALABRAS_DEL_ESPANOL.findall(texto)) > len(_PALABRAS_DEL_INGLES.findall(texto))


def _foto() -> BloqueBinario:
    """Una escena simple y reconocible: una casa con techo rojo, un árbol y el sol."""
    imagen = Image.new("RGB", (1024, 768), "skyblue")
    dibujo = ImageDraw.Draw(imagen)
    dibujo.rectangle((0, 560, 1024, 768), fill="forestgreen")
    dibujo.rectangle((300, 360, 600, 600), fill="beige", outline="black", width=4)
    dibujo.polygon([(270, 360), (450, 220), (630, 360)], fill="firebrick")
    dibujo.rectangle((420, 480, 490, 600), fill="saddlebrown")
    dibujo.rectangle((760, 400, 800, 600), fill="saddlebrown")
    dibujo.ellipse((680, 260, 880, 440), fill="darkgreen")
    dibujo.ellipse((80, 60, 220, 200), fill="gold")
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    return BloqueBinario(etiqueta=ETIQUETA_IMAGEN, contenido=buffer.getvalue(), tipo_mime="image/png")


_ACTA = """--- Página 1 ---

ACTA N.º 1. En la ciudad de Buenos Aires, a los diez días del mes de marzo de mil
novecientos setenta y cinco, reunidos en la sede del Instituto el señor Director, el
Secretario Académico y los jefes de los departamentos de Electrónica y de Construcciones,
se trató la creación de la Facultad de Ingeniería del Ejército. Luego de un intercambio de
opiniones, se resolvió elevar al Comando la propuesta de plan de estudios, fijar el inicio
de las clases para el primero de abril y designar una comisión que redacte el reglamento
interno. No habiendo más asuntos que tratar, se levantó la sesión a las doce horas."""


def test_una_foto_produce_una_descripcion_en_espanol(capa):
    resultado = capa.describir_imagen(imagen=_foto())

    assert resultado.texto
    assert _en_espanol(resultado.texto), resultado.texto
    assert resultado.tokens_entrada and resultado.tokens_salida


def test_un_documento_produce_un_resumen_en_espanol(capa):
    resultado = capa.resumir(cuerpo=BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=_ACTA))

    assert resultado.texto
    assert _en_espanol(resultado.texto), resultado.texto
    assert len(resultado.texto) < len(_ACTA)


@pytest.mark.rf("RF-20")
def test_s15_latencia_de_embeber_una_consulta_en_frio_y_en_caliente(capa, url_app, capsys):
    """S-15: el objetivo es menos de 1 s en CPU. Se mide e informa, no corta (DEC-128)."""
    bd = Bd(url_app, tamano_pool=1)
    try:
        fila = ConfiguracionDeModelos(bd=bd, validar=lambda _: None).vigente()[Capacidad.EMBEBER]
    finally:
        bd.cerrar()
    consulta = (BloqueDatos(ETIQUETA_CONSULTA, "¿Cuándo se fundó la Facultad?"),)
    # En frío: `keep_alive: 0` lo saca de memoria al terminar esta llamada.
    url = os.environ.get("HV_MOTOR_EMBEDDINGS_URL", _LOCAL)
    descarga = {"model": fila.modelo, "input": "x", "keep_alive": 0}
    httpx.post(f"{url}/api/embed", json=descarga, timeout=300).raise_for_status()

    frio = capa.embeber(textos=consulta, uso=UsoEmbedding.CONSULTA)
    calientes = [capa.embeber(textos=consulta, uso=UsoEmbedding.CONSULTA) for _ in range(5)]

    assert all(len(r.vectores) == 1 for r in (frio, *calientes))
    caliente_ms = sorted(r.duracion_ms for r in calientes)[2]
    hilos = fila.parametros.get("num_thread", "sin fijar (lo elige Ollama)")
    with capsys.disabled():
        print(
            f"\nS-15 {frio.modelo}: frío {frio.duracion_ms} ms, caliente {caliente_ms} ms"
            f" (mediana de 5). CPU {platform.processor() or platform.machine()},"
            f" {os.cpu_count()} hilos lógicos, num_thread {hilos}."
        )
