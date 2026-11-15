"""Transcripción real de documentos con la capa de modelos local (issue 12).

A diferencia del resto de la suite, estos tests no pasan por la API ni por Postgres: ejercitan
la capa real directamente. `capa_para` no arma la capa sin las tres filas del procesamiento, así
que lleva las de prueba con un Ollama falso que `transcribir` nunca llama: el procesamiento
completo con el motor está en `test_motor_local.py` y `test_motor_real.py`.
"""

from __future__ import annotations

import io
import shutil
import sys

import pymupdf
import pytest
import pytesseract
from docx import Document
from ollama_falso import CONFIGURACION_DE_PRUEBA, OllamaFalso
from PIL import Image, ImageDraw, ImageFont

from historia_viva.capa_modelos import local as _local
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_DOCUMENTO,
    ETIQUETA_IMAGEN,
    BloqueBinario,
    CapaModelos,
    Limites,
)
from historia_viva.capa_modelos.local import capa_para
from historia_viva.dominio.errores import LimiteExcedido
from historia_viva.dominio.originales import DOCX, TOPE_BYTES

pytestmark = [
    pytest.mark.rf("RF-07"),
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Necesita el binario de Tesseract instalado (con el idioma 'spa') en el PATH.",
    ),
]

_FUENTE = "arial.ttf" if sys.platform == "win32" else "DejaVuSans.ttf"  # Pillow la busca en el sistema
_ANCHO_PX, _ALTO_PX = 1800, 400
"""Tamaño de la imagen de prueba, elegido para que al insertarla en una página del mismo
tamaño en puntos y renderizarla de nuevo a `_local._DPI_ESCANEO` no haya reescalado: un
reescalado no entero difumina el texto sintético y el OCR lo lee mal (no es una falla de
`transcribir`, es un artefacto de la imagen de prueba)."""


def _pdf_con_texto(texto: str) -> bytes:
    documento = pymupdf.open()
    pagina = documento.new_page()
    pagina.insert_text((72, 72), texto)
    contenido = documento.tobytes()
    documento.close()
    return contenido


def _imagen_con_texto(texto: str) -> bytes:
    """Una imagen con `texto` escrito, para simular una página o una foto escaneada."""
    imagen = Image.new("RGB", (_ANCHO_PX, _ALTO_PX), "white")
    dibujo = ImageDraw.Draw(imagen)
    fuente = ImageFont.truetype(_FUENTE, 96)
    dibujo.text((40, 120), texto, fill="black", font=fuente)
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


def _agregar_pagina_escaneada(documento: pymupdf.Document, texto: str) -> None:
    ancho_pt = _ANCHO_PX * 72 / _local._DPI_ESCANEO
    alto_pt = _ALTO_PX * 72 / _local._DPI_ESCANEO
    pagina = documento.new_page(width=ancho_pt, height=alto_pt)
    pagina.insert_image(pagina.rect, stream=_imagen_con_texto(texto))


def _pdf_escaneado(*textos: str) -> bytes:
    documento = pymupdf.open()
    for texto in textos:
        _agregar_pagina_escaneada(documento, texto)
    contenido = documento.tobytes()
    documento.close()
    return contenido


def _docx_con_texto(*parrafos: str) -> bytes:
    documento = Document()
    for parrafo in parrafos:
        documento.add_paragraph(parrafo)
    buffer = io.BytesIO()
    documento.save(buffer)
    return buffer.getvalue()


def _capa() -> CapaModelos:
    return capa_para(CONFIGURACION_DE_PRUEBA, OllamaFalso().motores())


def _archivo(contenido: bytes, tipo_mime: str) -> BloqueBinario:
    return BloqueBinario(etiqueta=ETIQUETA_DOCUMENTO, contenido=contenido, tipo_mime=tipo_mime)


def test_un_pdf_con_capa_de_texto_se_transcribe_con_marca_de_pagina():
    capa = _capa()

    resultado = capa.transcribir(
        archivo=_archivo(_pdf_con_texto("Acta de fundación."), "application/pdf")
    )

    assert "--- Página 1 ---" in resultado.texto
    assert "Acta de fundación." in resultado.texto


def test_un_pdf_escaneado_se_transcribe_por_ocr_con_marca_de_pagina():
    capa = _capa()

    resultado = capa.transcribir(
        # Con tilde, como se escribe: el diccionario de Tesseract para "spa" corrige un
        # "FUNDACION" sin tilde al reconocerlo, y esta prueba no es sobre esa corrección.
        archivo=_archivo(_pdf_escaneado("ACTA DE FUNDACIÓN"), "application/pdf")
    )

    assert "--- Página 1 ---" in resultado.texto
    assert "ACTA DE FUNDACIÓN" in resultado.texto.upper()


def test_un_pdf_con_paginas_mixtas_solo_hace_ocr_de_la_que_no_tiene_texto():
    documento = pymupdf.open()
    pagina_con_texto = documento.new_page()
    pagina_con_texto.insert_text((72, 72), "Página con capa de texto.")
    _agregar_pagina_escaneada(documento, "PAGINA ESCANEADA")
    contenido = documento.tobytes()
    documento.close()
    capa = _capa()

    resultado = capa.transcribir(archivo=_archivo(contenido, "application/pdf"))

    assert "--- Página 1 ---" in resultado.texto
    assert "Página con capa de texto." in resultado.texto
    assert "--- Página 2 ---" in resultado.texto
    assert "PAGINA ESCANEADA" in resultado.texto.upper()


def test_un_docx_se_transcribe_con_marca_de_pagina():
    capa = _capa()

    resultado = capa.transcribir(
        archivo=_archivo(
            _docx_con_texto("Acta de fundación.", "Se resolvió crear la Facultad."),
            DOCX,
        )
    )

    assert resultado.texto == (
        "--- Página 1 ---\n\nActa de fundación.\nSe resolvió crear la Facultad."
    )


def test_una_fotografia_se_transcribe_sin_marca_de_pagina():
    """El OCR de una foto va directo bajo `## Texto visible`; una marca de página ahí sobra."""
    capa = _capa()

    resultado = capa.transcribir(
        archivo=BloqueBinario(
            etiqueta=ETIQUETA_IMAGEN,
            contenido=_imagen_con_texto("FACULTAD DE INGENIERIA"),
            tipo_mime="image/png",
        )
    )

    assert "Página" not in resultado.texto
    assert "FACULTAD DE INGENIERIA" in resultado.texto.upper()


def test_un_documento_de_mas_de_80_paginas_termina_en_limite_excedido_sin_hacer_ocr():
    documento = pymupdf.open()
    for _ in range(81):
        documento.new_page()
    contenido = documento.tobytes()
    documento.close()
    capa = _capa()

    with pytest.raises(LimiteExcedido) as excinfo:
        capa.transcribir(archivo=_archivo(contenido, "application/pdf"))

    assert excinfo.value.limite == "páginas"
    assert excinfo.value.tope == 80


def test_un_documento_de_mas_de_40_mb_termina_en_limite_excedido():
    contenido = b"%PDF-1.4" + b"0" * (TOPE_BYTES + 1)
    capa = _capa()

    with pytest.raises(LimiteExcedido) as excinfo:
        capa.transcribir(archivo=_archivo(contenido, "application/pdf"))

    assert excinfo.value.limite == "tamaño"


def test_una_transcripcion_que_agota_su_tiempo_termina_en_limite_excedido_y_no_cuelga():
    capa = _capa()
    sin_tiempo = Limites(tiempo_maximo_s=-1, tope_tokens_entrada=None, tope_tokens_salida=None)

    with pytest.raises(LimiteExcedido) as excinfo:
        capa.transcribir(
            archivo=_archivo(_pdf_con_texto("Acta de fundación."), "application/pdf"),
            limites=sin_tiempo,
        )

    assert excinfo.value.limite == "tiempo"


def test_un_ocr_que_no_termina_dentro_del_tope_de_tiempo_se_cancela_en_vez_de_colgar(
    monkeypatch,
):
    """El tope se le aplica al proceso de Tesseract mismo, no después de que termine.

    `pytesseract` mata el proceso y levanta `RuntimeError` cuando lo vence; acá se simula
    ese vencimiento sin depender de que la máquina de pruebas sea lo bastante lenta.
    """

    def _tesseract_colgado(*args, **kwargs):
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(pytesseract, "image_to_data", _tesseract_colgado)
    capa = _capa()

    with pytest.raises(LimiteExcedido) as excinfo:
        capa.transcribir(
            archivo=BloqueBinario(
                etiqueta=ETIQUETA_IMAGEN,
                contenido=_imagen_con_texto("FACULTAD DE INGENIERIA"),
                tipo_mime="image/png",
            )
        )

    assert excinfo.value.limite == "tiempo"
