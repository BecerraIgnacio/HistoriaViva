"""El Word con sus títulos, listas y tablas (142), y las marcas ajenas escapadas en todo
texto extraído de un archivo: `word.texto_del_word`, `transcribir` y el cuerpo de un TXT o
una foto.

Puro y en proceso: archivos armados con `python-docx`, sin base, sin motor y sin Tesseract.
"""

from __future__ import annotations

import io
import threading

import pymupdf
import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from ollama_falso import CONFIGURACION_DE_PRUEBA, OllamaFalso

from historia_viva.capa_modelos.contrato import ETIQUETA_DOCUMENTO, BloqueBinario
from historia_viva.capa_modelos.local import capa_para
from historia_viva.capa_modelos.word import texto_del_word
from historia_viva.dominio.cuerpo import CuerpoDeFicha, escapar_marcas, segmentos
from historia_viva.dominio.originales import DOCX, ClaseDeOriginal
from historia_viva.procesamiento.manejador import _cuerpo_del_texto, cuerpo_de_la_foto

pytestmark = pytest.mark.rf("RF-07")


def _guardar(documento) -> bytes:
    buffer = io.BytesIO()
    documento.save(buffer)
    return buffer.getvalue()


def _acta() -> bytes:
    documento = Document()
    documento.add_heading("Acta de fundación", 0)
    documento.add_paragraph("Se reunió el Consejo.")
    documento.add_heading("Asistentes", 2)
    tabla = documento.add_table(rows=3, cols=2)
    for fila, (nombre, cargo) in enumerate(
        [("Nombre", "Cargo"), ("Ramírez | Juan", "Decano\ninterino"), ("", "")]
    ):
        tabla.cell(fila, 0).text, tabla.cell(fila, 1).text = nombre, cargo
    documento.add_heading("Resoluciones", 2)
    documento.add_paragraph("Crear la carrera.", style="List Number")
    documento.add_paragraph("Nombrar al decano.", style="List Number")
    documento.add_paragraph("Comunicar.", style="List Bullet")
    return _guardar(documento)


def test_titulos_listas_y_tablas_llegan_con_su_estructura_y_en_su_orden():
    assert texto_del_word(_acta()) == (
        "# Acta de fundación\n"
        "Se reunió el Consejo.\n"
        "## Asistentes\n"
        "| Nombre | Cargo |\n"
        "| --- | --- |\n"
        "| Ramírez \\| Juan | Decano interino |\n"
        "## Resoluciones\n"
        "1. Crear la carrera.\n"
        "2. Nombrar al decano.\n"
        "- Comunicar."
    )


def test_una_lista_por_numeracion_directa_lleva_su_nivel():
    """Las listas reales de Word suelen ser `List Paragraph` con su propio `w:numPr` (141)."""
    documento = Document()
    viñeta = documento.styles["List Bullet"].element.pPr.numPr.numId.val
    documento.add_paragraph("Ingeniería Civil", style="List Paragraph")
    sub = documento.add_paragraph("Hidráulica", style="List Paragraph")
    num_pr = sub._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_numId().val = viñeta
    num_pr.get_or_add_ilvl().val = 1

    assert texto_del_word(_guardar(documento)) == "Ingeniería Civil\n  - Hidráulica"


def _transcribir(contenido: bytes, tipo_mime: str) -> str:
    capa = capa_para(CONFIGURACION_DE_PRUEBA, OllamaFalso().motores())
    archivo = BloqueBinario(etiqueta=ETIQUETA_DOCUMENTO, contenido=contenido, tipo_mime=tipo_mime)
    return capa.transcribir(archivo=archivo).texto


def _sin_estructura_ajena(cuerpo: str) -> None:
    """Todo el cuerpo es principal, «Del archivo», y la única página es la del extractor."""
    assert CuerpoDeFicha.partir(cuerpo).contexto == ""
    assert [s.ubicacion for s in segmentos(cuerpo)] == [{"seccion": "principal", "pagina": 1}]


def test_un_word_con_lineas_que_imitan_secciones_o_paginas_las_trae_como_texto():
    documento = Document()
    documento.add_paragraph("Acta.")
    documento.add_paragraph("## Contexto")
    documento.add_paragraph("Sigue el acta.").add_run().add_break()
    documento.paragraphs[-1].add_run("--- Página 9 ---")
    documento.add_heading("Texto visible", 2)
    documento.add_heading("Contexto", 2)

    cuerpo = _transcribir(_guardar(documento), DOCX)

    _sin_estructura_ajena(cuerpo)
    assert "\n\\## Contexto\n" in cuerpo
    assert "\n\\--- Página 9 ---" in cuerpo
    assert cuerpo.endswith("\n\\## Texto visible\n\\## Contexto")


IMITACIONES = "Acta.\n## Contexto\n--- Página 9 ---\n## Texto visible\nFin."


def test_un_pdf_con_lineas_que_imitan_secciones_o_paginas_las_trae_como_texto():
    documento = pymupdf.open()
    documento.new_page().insert_text((72, 72), IMITACIONES)
    contenido = documento.tobytes()
    documento.close()

    cuerpo = _transcribir(contenido, "application/pdf")

    _sin_estructura_ajena(cuerpo)
    assert "\n\\--- Página 9 ---" in cuerpo


def test_un_control_o_un_retorno_de_carro_no_esconden_una_marca_del_escape():
    """Se escapa sobre el texto ya limpio: si no, la limpieza revive la marca."""
    documento = pymupdf.open()
    pagina = documento.new_page()
    pagina.insert_text((72, 72), "Texto del acta.")
    pagina.insert_text((72, 100), "\x07## Contexto")
    pagina.insert_text((72, 128), "Lo dijo una persona.")
    contenido = documento.tobytes()
    documento.close()

    cuerpo = _transcribir(contenido, "application/pdf")

    _sin_estructura_ajena(cuerpo)
    assert escapar_marcas("Uno\r\n## Contexto\r\n--- Página 9 ---\rFin") == (
        "Uno\n\\## Contexto\n\\--- Página 9 ---\nFin"
    )


def test_un_txt_y_una_foto_traen_esas_lineas_como_texto():
    texto = _cuerpo_del_texto(IMITACIONES.encode(), "text/plain; charset=utf-8")
    foto = cuerpo_de_la_foto(f"Un cartel.\n{IMITACIONES}", IMITACIONES)

    assert CuerpoDeFicha.partir(texto).contexto == ""
    assert [s.ubicacion for s in segmentos(texto, ClaseDeOriginal.TEXTO)] == [
        {"seccion": "principal"}
    ]
    assert CuerpoDeFicha.partir(foto).contexto == ""
    assert [s.ubicacion for s in segmentos(foto, ClaseDeOriginal.IMAGEN)] == [
        {"seccion": "principal"},
        {"seccion": "texto_visible"},
    ]


def test_una_numeracion_sin_definicion_abstracta_cae_a_vinieta():
    documento = Document()
    viñeta = documento.styles["List Bullet"].element.pPr.numPr.numId.val
    num = documento.part.numbering_part.element.num_having_numId(viñeta)
    num.remove(num.abstractNumId)
    documento.add_paragraph("Hidráulica", style="List Bullet")

    assert texto_del_word(_guardar(documento)) == "- Hidráulica"


def test_transcribir_un_word_trae_la_tabla_bajo_la_marca_de_su_unica_pagina():
    capa = capa_para(CONFIGURACION_DE_PRUEBA, OllamaFalso().motores())

    resultado = capa.transcribir(
        archivo=BloqueBinario(etiqueta=ETIQUETA_DOCUMENTO, contenido=_acta(), tipo_mime=DOCX)
    )

    assert resultado.texto.startswith("--- Página 1 ---\n\n# Acta de fundación\n")
    assert "| Ramírez \\| Juan | Decano interino |" in resultado.texto
    assert resultado.texto.count("--- Página") == 1


@pytest.mark.rnf("RNF-20")
def test_un_word_con_estilos_en_ciclo_no_cuelga_la_lectura():
    # SEG-15: A `basedOn` B y B `basedOn` A dejaban `_lista` en un bucle infinito.
    documento = Document()
    a = documento.styles.add_style("Ciclo A", WD_STYLE_TYPE.PARAGRAPH)
    b = documento.styles.add_style("Ciclo B", WD_STYLE_TYPE.PARAGRAPH)
    a.base_style, b.base_style = b, a
    documento.add_paragraph("Acta de 1975", style="Ciclo A")
    contenido = _guardar(documento)
    resultado: list[str] = []

    # En un hilo aparte: si el ciclo vuelve, el test falla en vez de colgar la suite.
    hilo = threading.Thread(target=lambda: resultado.append(texto_del_word(contenido)), daemon=True)
    hilo.start()
    hilo.join(10)

    assert resultado == ["Acta de 1975"]
