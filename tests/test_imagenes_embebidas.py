"""Las imágenes embebidas de un PDF, un Word o una planilla pasan por el OCR (143).

Casi todo corre con `image_to_data` reemplazado: no hace falta Tesseract. El de la captura real
sí lo necesita, con el idioma `spa` (con `HV_TESSERACT_CMD` y `TESSDATA_PREFIX` si no está en el
PATH); sin él se salta.
"""

from __future__ import annotations

import io
import struct
import time
import zipfile
from dataclasses import replace
from uuid import uuid4

import pymupdf
import pytesseract
import pytest
import xlsxwriter
from docx import Document
from ollama_falso import CONFIGURACION_DE_PRUEBA, OllamaFalso
from PIL import ExifTags, Image, ImageDraw, ImageFont, ImageOps

from historia_viva import planillas
from historia_viva.busqueda import ubicar
from historia_viva.capa_modelos import entradas, imagenes, ocr
from historia_viva.capa_modelos.configuracion import validar
from historia_viva.capa_modelos.contrato import ETIQUETA_DOCUMENTO, BloqueBinario, Capacidad
from historia_viva.capa_modelos.local import capa_para
from historia_viva.dominio.cuerpo import CuerpoDeFicha, escapar_marcas, segmentos
from historia_viva.dominio.errores import ConfiguracionInvalida, LimiteExcedido
from historia_viva.dominio.originales import DOCX, XLSX, ClaseDeOriginal
from historia_viva.indexacion import LineaDeContexto, fragmentar

pytestmark = pytest.mark.rf("RF-07")

PDF = "application/pdf"
_LINEA = LineaDeContexto(tipo="planilla", fecha_legible="1975")


def _tesseract_con_espanol() -> bool:
    try:
        return "spa" in pytesseract.get_languages()
    except (pytesseract.TesseractNotFoundError, OSError, RuntimeError):
        return False


def _png(ancho: int = 400, alto: int = 120, color: str = "white") -> bytes:
    salida = io.BytesIO()
    Image.new("RGB", (ancho, alto), color).save(salida, "PNG")
    return salida.getvalue()


class _OcrFalso:
    """`image_to_data` reemplazado: devuelve `texto` en un renglón y cuenta las imágenes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, texto: str) -> None:
        self.recibidas: list[tuple[int, int]] = []
        self._palabras = texto.split()
        monkeypatch.setattr(pytesseract, "image_to_data", self)

    def __call__(self, imagen: Image.Image, **_: object) -> dict[str, list]:
        self.recibidas.append(imagen.size)
        filas = [(p, 20 + 60 * i) for i, p in enumerate(self._palabras)]
        return {
            "text": [p for p, _ in filas],
            "left": [x for _, x in filas],
            "top": [20 for _ in filas],
            "width": [50 for _ in filas],
            "height": [16 for _ in filas],
            "conf": [90.0 for _ in filas],
            "block_num": [1 for _ in filas],
            "par_num": [1 for _ in filas],
            "line_num": [1 for _ in filas],
        }


def _transcribir(contenido: bytes, tipo_mime: str, **parametros: object):
    fila = CONFIGURACION_DE_PRUEBA[Capacidad.TRANSCRIBIR]
    fila = replace(fila, parametros={**fila.parametros, **parametros})
    configuracion = {**CONFIGURACION_DE_PRUEBA, Capacidad.TRANSCRIBIR: fila}
    return capa_para(configuracion, OllamaFalso().motores()).transcribir(
        archivo=BloqueBinario(
            etiqueta=ETIQUETA_DOCUMENTO, contenido=contenido, tipo_mime=tipo_mime
        )
    )


def _word(*imagenes_png: bytes, texto: str = "Acta de la sesión del Consejo.") -> bytes:
    documento = Document()
    documento.add_paragraph(texto)
    for png in imagenes_png:
        documento.add_picture(io.BytesIO(png))
    salida = io.BytesIO()
    documento.save(salida)
    return salida.getvalue()


# ------------------------------------------------------------------------------------ Word ---


def test_el_texto_de_una_captura_pegada_en_un_word_llega_como_bloque_imagen_m(monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "Resolución 45 del Rectorado")

    cuerpo = _transcribir(_word(_png()), DOCX).texto

    assert cuerpo == (
        "--- Página 1 ---\n\nActa de la sesión del Consejo.\n\n"
        "--- Lectura automática · Imagen 1 ---\n\nResolución 45 del Rectorado"
    )
    assert len(ocr_falso.recibidas) == 1
    *_, bloque = segmentos(cuerpo)
    assert bloque.ubicacion == {"seccion": "principal", "imagen": 1}
    assert bloque.texto.endswith("Resolución 45 del Rectorado")


def test_la_imagen_de_una_celda_de_tabla_del_word_tambien_se_lee(monkeypatch):
    _OcrFalso(monkeypatch, "Firma del Decano")
    documento = Document()
    tabla = documento.add_table(rows=1, cols=2)
    tabla.cell(0, 0).text = "Firma"
    tabla.cell(0, 1).paragraphs[0].add_run().add_picture(io.BytesIO(_png()))
    salida = io.BytesIO()
    documento.save(salida)

    cuerpo = _transcribir(salida.getvalue(), DOCX).texto

    assert cuerpo.endswith("--- Lectura automática · Imagen 1 ---\n\nFirma del Decano")


@pytest.mark.skipif(not _tesseract_con_espanol(), reason="Necesita Tesseract con 'spa'.")
def test_con_tesseract_real_el_texto_de_la_captura_se_lee():
    captura = Image.new("RGB", (900, 120), "white")
    fuente = ImageFont.load_default(size=40)  # la de Pillow: no depende del sistema
    ImageDraw.Draw(captura).text((20, 30), "Instituto de las Fuerzas Armadas", "black", fuente)
    png = io.BytesIO()
    captura.save(png, "PNG")

    cuerpo = _transcribir(_word(png.getvalue()), DOCX).texto

    _, bloque = cuerpo.split("--- Lectura automática · Imagen 1 ---\n\n")
    assert "Fuerzas Armadas" in bloque


# ------------------------------------------------------------------------------------- PDF ---


def _pdf_con_logo_en_cada_pagina(paginas: int = 3) -> bytes:
    logo = _png(300, 100, "navy")
    documento = pymupdf.open()
    for numero in range(1, paginas + 1):
        pagina = documento.new_page()
        pagina.insert_text((72, 72), f"Texto de la página {numero}.")
        pagina.insert_image(pymupdf.Rect(400, 20, 550, 70), stream=logo)
        if numero == 2:
            pagina.insert_image(pymupdf.Rect(72, 200, 472, 320), stream=_png(400, 120))
    return documento.tobytes()


def test_un_logo_repetido_en_todas_las_paginas_se_descarta_y_no_ensucia_el_cuerpo(monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "Plano del cuartel")

    resultado = _transcribir(_pdf_con_logo_en_cada_pagina(), PDF)

    # El logo no pasa por el OCR ni una vez: solo la imagen única de la página 2.
    assert ocr_falso.recibidas == [(800, 240)]
    assert resultado.texto.count("Lectura automática") == 1
    assert (
        "--- Página 2 ---\n\nTexto de la página 2.\n\n"
        "--- Lectura automática · Página 2 · Imagen 2 ---\n\nPlano del cuartel\n\n--- Página 3 ---"
    ) in resultado.texto
    assert resultado.avisos == ()
    [bloque] = [s for s in segmentos(resultado.texto) if "imagen" in s.ubicacion]
    assert bloque.ubicacion == {"seccion": "principal", "pagina": 2, "imagen": 2}


def test_el_escaneo_de_una_pagina_con_capa_de_texto_y_los_iconos_no_se_leen(monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "no debería leerse")
    documento = pymupdf.open()
    pagina = documento.new_page()
    pagina.insert_image(pagina.rect, stream=_png(1700, 2200))  # el escaneo
    pagina.insert_image(pymupdf.Rect(20, 20, 36, 36), stream=_png(16, 16, "red"))  # un ícono
    pagina.insert_text((72, 72), "La capa de texto del escaneo.")

    cuerpo = _transcribir(documento.tobytes(), PDF).texto

    assert ocr_falso.recibidas == []
    assert cuerpo == "--- Página 1 ---\n\nLa capa de texto del escaneo."


# -------------------------------------------------------------------------------- planilla ---


def _planilla(*, oculta: bool = False) -> bytes:
    salida = io.BytesIO()
    libro = xlsxwriter.Workbook(salida, {"in_memory": True})
    hoja = libro.add_worksheet("Datos")
    hoja.write_row(0, 0, ["Año", "Egresados"])
    hoja.write_row(1, 0, [1975, 40])
    hoja.write_row(2, 0, [1976, 52])
    hoja.insert_image("D2", "captura.png", {"image_data": io.BytesIO(_png())})
    grafico = libro.add_chart({"type": "column"})
    grafico.add_series({"values": "=Datos!$B$2:$B$3"})
    hoja.insert_chart("D12", grafico)
    if oculta:
        escondida = libro.add_worksheet("Notas")
        escondida.write(0, 0, "x")
        escondida.insert_image("B2", "otra.png", {"image_data": io.BytesIO(_png(500, 200))})
        escondida.hide()
    libro.close()
    return salida.getvalue()


def test_una_imagen_de_una_planilla_queda_como_hoja_imagen_y_un_grafico_no(monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "Promoción 1975")

    cuerpo = _transcribir(_planilla(oculta=True), XLSX).texto

    # Solo la captura de la hoja visible: ni el gráfico ni la imagen de la hoja oculta.
    assert len(ocr_falso.recibidas) == 1
    assert cuerpo.endswith("--- Lectura automática · Hoja «Datos» · Imagen 1 ---\n\nPromoción 1975")
    [hoja] = CuerpoDeFicha.parsear_cuerpo(cuerpo)
    assert hoja.filas == [["1975", "40"], ["1976", "52"]]
    *_, bloque = segmentos(cuerpo, ClaseDeOriginal.PLANILLA)
    assert bloque.ubicacion == {"seccion": "principal", "hoja": "Datos", "imagen": 1}
    # El bloque se fragmenta como texto, no en filas de la hoja.
    *_, ultimo = fragmentar(uuid4(), segmentos(cuerpo, ClaseDeOriginal.PLANILLA), _LINEA)
    assert ultimo.ubicacion == bloque.ubicacion


def test_las_imagenes_de_la_planilla_salen_de_xl_media_por_hoja():
    [(hoja, numero, contenido)] = planillas.imagenes(_planilla(oculta=True))

    assert (hoja, numero) == ("Datos", 1)
    assert Image.open(io.BytesIO(contenido)).size == (400, 120)


# --------------------------------------------------------------------- omitidas y topes ---


def _wmf() -> bytes:
    """Un WMF colocable mínimo de 500 × 500 px: Pillow lo identifica pero no lo dibuja fuera de
    Windows."""
    cabecera = struct.pack("<IHhhhhHI", 0x9AC6CDD7, 0, 0, 0, 10000, 10000, 1440, 0)
    suma = 0
    for i in range(0, 20, 2):
        suma ^= struct.unpack("<H", cabecera[i : i + 2])[0]
    return cabecera + struct.pack("<H", suma) + b"\x01\x00\x09\x00\x00\x03" + b"\x00" * 12


def _embebida(numero: int, contenido: bytes, **campos: object) -> imagenes.Embebida:
    return imagenes.Embebida(
        numero, f"huella-{numero}", lambda: contenido, **campos  # type: ignore[arg-type]
    )


class _Motor:
    def __init__(self, texto: str = "Texto leído", *, falla: bool = False) -> None:
        self.llamadas = 0
        self._texto, self._falla = texto, falla

    def palabras(self, imagen: Image.Image, *, tiempo_maximo_s: float):
        self.llamadas += 1
        if self._falla:
            raise LimiteExcedido(Capacidad.TRANSCRIBIR, "tiempo", 1, 2)
        return [ocr.PalabraDelMotor(self._texto, 10, 10, 100, 20, 90.0, (1,))]


def _leer(candidatas, motor, *, maximo: int = 30, fin: float | None = None):
    return imagenes.leer(
        candidatas,
        ocr.AjustesOcr(),
        imagenes.TopesDeImagenes(maximo=maximo),
        fin=time.monotonic() + 60 if fin is None else fin,
        motor=motor,
    )


def test_una_imagen_sobre_el_tope_de_pixeles_o_que_pillow_no_abre_se_omite_con_aviso(monkeypatch):
    monkeypatch.setattr(imagenes, "TOPE_PIXELES", 100_000)
    motor = _Motor()
    candidatas = [
        _embebida(1, _png(400, 400)),  # 160 000 píxeles
        _embebida(2, b"no es una imagen"),
        _embebida(3, _wmf()),
        _embebida(4, _png(), pagina=3, tamano=(5000, 5000)),  # se mide sin decodificar
        _embebida(5, _png()),
    ]

    leidas = _leer(candidatas, motor)

    assert motor.llamadas == 1
    assert [imagen.numero for imagen, _ in leidas.bloques] == [5]
    assert leidas.avisos == (
        "Se omitió la imagen «Imagen 1»: pasa el tope de 100.000 píxeles.",
        "Se omitió la imagen «Imagen 2»: no se pudo abrir.",
        "Se omitió la imagen «Imagen 3»: su formato (WMF) no se puede leer.",
        "Se omitió la imagen «Página 3 · Imagen 4»: pasa el tope de 100.000 píxeles.",
    )
    assert leidas.truncada is False


def test_una_imagen_que_no_abre_no_hace_fallar_la_pieza_y_el_aviso_queda_en_la_ficha(
    monkeypatch,
):
    _OcrFalso(monkeypatch, "Segunda captura")
    contenido = _word(_png(), _png(500, 200))
    # La primera imagen del Word queda rota: sus bytes ya no son un PNG.
    roto = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(contenido)) as origen, zipfile.ZipFile(roto, "w") as copia:
        for miembro in origen.infolist():
            datos = origen.read(miembro)
            copia.writestr(miembro, b"\x89PNG roto" if datos == _png() else datos)

    resultado = _transcribir(roto.getvalue(), DOCX)

    aviso = "Se omitió la imagen «Imagen 1»: no se pudo abrir."
    assert resultado.avisos == (aviso,)
    assert resultado.texto.startswith(f"{aviso}\n\n--- Página 1 ---")
    assert resultado.texto.endswith("--- Lectura automática · Imagen 2 ---\n\nSegunda captura")


def test_el_tope_de_cantidad_es_configurable_y_corta_con_aviso_en_la_ficha(monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "Captura")
    contenido = _word(_png(400, 120), _png(400, 130), _png(400, 140))

    resultado = _transcribir(contenido, DOCX, imagenes_maximo=1)
    ninguna = _transcribir(contenido, DOCX, imagenes_maximo=0)

    aviso = "Quedaron 2 imágenes sin leer (1 leídas): el tope es de 1 por Pieza."
    assert resultado.avisos == (aviso,)
    assert resultado.truncada is True
    assert resultado.texto.startswith(f"{aviso}\n\n--- Página 1 ---")
    assert resultado.texto.count("Lectura automática") == 1
    assert len(ocr_falso.recibidas) == 1
    assert "Lectura automática" not in ninguna.texto
    assert ninguna.avisos == ("Quedaron 3 imágenes sin leer (0 leídas): el tope es de 0 por Pieza.",)


def test_el_tope_de_tiempo_corta_con_aviso_sin_pasarse_del_de_transcribir():
    agotado = _leer([_embebida(1, _png()), _embebida(2, _png())], _Motor(), fin=time.monotonic())
    vencido = _leer([_embebida(1, _png()), _embebida(2, _png())], _Motor(falla=True))

    aviso = "Quedaron 2 imágenes sin leer (0 leídas): se agotó el tiempo para leerlas."
    assert (agotado.avisos, agotado.truncada, agotado.bloques) == ((aviso,), True, ())
    assert (vencido.avisos, vencido.truncada, vencido.bloques) == ((aviso,), True, ())


def test_el_tiempo_de_las_imagenes_sale_de_lo_que_queda_del_tope_de_transcribir(monkeypatch):
    recibido: list[float] = []

    def _leer_espiado(candidatas, ajustes, topes, *, fin):
        recibido.append(fin - time.monotonic())
        return imagenes.ImagenesLeidas((), (), truncada=False)

    monkeypatch.setattr(imagenes, "leer", _leer_espiado)
    fila = CONFIGURACION_DE_PRUEBA[Capacidad.TRANSCRIBIR]
    tope = fila.limites.tiempo_maximo_s

    _transcribir(_word(_png()), DOCX, imagenes_tiempo_maximo_s=10_000)
    _transcribir(_word(_png()), DOCX, imagenes_tiempo_maximo_s=7)

    assert tope - 6 < recibido[0] <= tope - 5
    assert 6 < recibido[1] <= 7


def test_las_imagenes_chicas_y_las_repetidas_se_descartan_sin_aviso():
    motor = _Motor()
    logo = _png(300, 100, "navy")
    candidatas = [
        _embebida(1, _png(16, 16)),
        _embebida(2, _png(1000, 8)),  # un filete
        *(imagenes.Embebida(1, "logo", lambda: logo, pagina=n) for n in (1, 2, 3)),
        _embebida(5, _png(), tamano=(50, 50)),
    ]

    leidas = _leer(candidatas, motor)

    assert (motor.llamadas, leidas.bloques, leidas.avisos) == (0, (), ())


def test_la_misma_captura_pegada_dos_veces_se_lee_una_vez(monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "Captura repetida")
    documento = Document()
    documento.add_picture(io.BytesIO(_png()))
    documento.add_paragraph("Entre las dos.")
    documento.add_picture(io.BytesIO(_png()))
    salida = io.BytesIO()
    documento.save(salida)

    cuerpo = _transcribir(salida.getvalue(), DOCX).texto

    assert len(ocr_falso.recibidas) == 1
    assert cuerpo.endswith("--- Lectura automática · Imagen 1 ---\n\nCaptura repetida")


def test_si_abrir_la_imagen_agota_el_tiempo_se_corta_con_aviso_sin_leerla():
    motor = _Motor()

    def _lenta() -> bytes:
        time.sleep(0.3)
        return _png()

    leidas = _leer([imagenes.Embebida(1, "lenta", _lenta)], motor, fin=time.monotonic() + 0.2)

    assert motor.llamadas == 0
    assert leidas.avisos == (
        "Quedó 1 imagen sin leer (0 leídas): se agotó el tiempo para leerlas.",
    )
    assert leidas.truncada is True


def test_un_eps_embebido_no_se_abre_ni_llama_a_ghostscript():
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 500 500\n"

    leidas = _leer([_embebida(1, eps)], _Motor())

    assert leidas.avisos == ("Se omitió la imagen «Imagen 1»: no se pudo abrir.",)


def test_una_imagen_sin_texto_no_deja_bloque():
    leidas = _leer([_embebida(1, _png())], _Motor(texto="—"))

    assert leidas.bloques == ()


def test_el_texto_de_la_imagen_se_escapa_y_no_finge_marcas(monkeypatch):
    texto = "--- Página 9 ---\n--- Hoja «X» (filas 1–2 de 2) ---\n--- Lectura automática · Imagen 7 ---"

    escapado = escapar_marcas(texto)

    assert escapado.splitlines() == ["\\" + linea for linea in texto.splitlines()]
    leidas = _leer([_embebida(1, _png())], _Motor(texto="--- Página 9 ---"))
    [(_, bloque)] = leidas.bloques
    assert bloque == "--- Lectura automática · Imagen 1 ---\n\n\\--- Página 9 ---"
    assert [s.ubicacion for s in segmentos(f"--- Página 1 ---\n\nTexto.\n\n{bloque}")] == [
        {"seccion": "principal", "pagina": 1},
        {"seccion": "principal", "imagen": 1},
    ]


# ------------------------------------------------------------------------------ las Citas ---


@pytest.mark.rf("RF-19")
@pytest.mark.parametrize(
    ("ubicacion", "legible", "ancla"),
    [
        ({"seccion": "principal", "pagina": 2, "imagen": 2}, "p. 2 · Imagen 2", "#page=2"),
        ({"seccion": "principal", "hoja": "Datos", "imagen": 1}, "Hoja Datos · Imagen 1", ""),
        ({"seccion": "principal", "imagen": 3}, "Imagen 3", ""),
    ],
    ids=["pdf", "planilla", "word"],
)
def test_la_cita_de_una_imagen_lleva_a_su_pagina_a_su_hoja_o_a_la_pieza(ubicacion, legible, ancla):
    pieza = uuid4()

    assert ubicar(pieza, ubicacion) == (legible, f"/api/piezas/{pieza}/original{ancla}")


# ------------------------------------------------------------------------------ la fila ---


@pytest.mark.parametrize(
    ("parametros", "campo"),
    [
        ({"imagenes_maximo": -1}, "imagenes_maximo"),
        ({"imagenes_maximo": "30"}, "imagenes_maximo"),
        ({"imagenes_tiempo_maximo_s": 0}, "imagenes_tiempo_maximo_s"),
        ({"imagenes_tiempo_maximo_s": 1.5}, "imagenes_tiempo_maximo_s"),
    ],
)
def test_la_fila_rechaza_topes_de_imagenes_invalidos(parametros, campo):
    fila = CONFIGURACION_DE_PRUEBA[Capacidad.TRANSCRIBIR]
    fila = replace(fila, parametros={**fila.parametros, **parametros})

    with pytest.raises(ConfiguracionInvalida) as error:
        validar({**CONFIGURACION_DE_PRUEBA, Capacidad.TRANSCRIBIR: fila}, OllamaFalso().motores())

    assert ("transcribir", campo) in {(e.capacidad, e.campo) for e in error.value.errores}


def test_la_fila_sembrada_con_los_topes_valida():
    fila = CONFIGURACION_DE_PRUEBA[Capacidad.TRANSCRIBIR]
    fila = replace(
        fila,
        parametros={**fila.parametros, "imagenes_maximo": 30, "imagenes_tiempo_maximo_s": 120},
    )

    validar({**CONFIGURACION_DE_PRUEBA, Capacidad.TRANSCRIBIR: fila}, OllamaFalso().motores())
    assert imagenes.TopesDeImagenes.de(fila.parametros) == imagenes.TopesDeImagenes()


# ------------------------------------------------- archivos hostiles (auditoría delta, 166) ---


def _planilla_con_imagenes(veces: int, distintas: int = 0) -> bytes:
    """SEG-16, escalado: la misma imagen referida `veces` veces desde el dibujo de una hoja, y
    después `distintas` imágenes distintas."""
    salida = io.BytesIO()
    libro = xlsxwriter.Workbook(salida, {"in_memory": True})
    hoja = libro.add_worksheet("Datos")
    png = _png(600, 600)
    for fila in range(veces):
        hoja.insert_image(fila, 0, "captura.png", {"image_data": io.BytesIO(png)})
    for fila in range(distintas):
        otra = _png(300 + fila, 200)
        hoja.insert_image(veces + fila, 0, "otra.png", {"image_data": io.BytesIO(otra)})
    libro.close()
    return salida.getvalue()


@pytest.mark.rnf("RNF-20")
def test_una_imagen_referida_muchas_veces_en_la_planilla_se_lee_una_vez():
    # SEG-16: mil referencias a una imagen de 1 MB ocupaban 1 GB. Las repeticiones no cuentan
    # para el tope: las imágenes de después siguen llegando.
    contenido = _planilla_con_imagenes(200, distintas=3)

    encontradas = planillas.imagenes(contenido, topes=planillas.Topes(imagenes_distintas=10))

    assert len(encontradas) == 203
    assert len({id(blob) for _, _, blob in encontradas}) == 4
    assert len({c.huella for c in imagenes.de_la_planilla(encontradas)}) == 4


@pytest.mark.rnf("RNF-20")
def test_la_planilla_lee_hasta_el_tope_de_imagenes_distintas_y_una_de_mas():
    contenido = _planilla_con_imagenes(0, distintas=15)

    encontradas = planillas.imagenes(contenido, topes=planillas.Topes(imagenes_distintas=10))

    # Una de más, para que `leer` avise el corte.
    assert len(encontradas) == 11


@pytest.mark.rnf("RNF-20")
def test_una_imagen_referida_muchas_veces_en_el_word_se_mide_una_vez(monkeypatch):
    monkeypatch.setattr(imagenes, "_IMAGENES_DISTINTAS", 10)
    word = _word(*[_png()] * 200, *[_png(300 + n, 200) for n in range(12)])
    huellas = []
    original = imagenes.hashlib.sha256
    monkeypatch.setattr(
        imagenes.hashlib, "sha256", lambda datos: huellas.append(1) or original(datos)
    )

    candidatas = imagenes.del_word(word)

    # Las 200 del logo y 10 distintas más (una de más que el tope): cada parte se mide una vez.
    assert (len(candidatas), len(huellas)) == (210, 11)


@pytest.mark.rnf("RNF-20")
def test_el_tope_cuenta_imagenes_distintas_y_no_las_repeticiones_de_un_logo(monkeypatch):
    monkeypatch.setattr(imagenes, "_IMAGENES_DISTINTAS", 3)
    motor = _Motor()
    # Un logo en 50 páginas (membrete) y después tres imágenes reales.
    logo = [
        imagenes.Embebida(1, "logo", lambda: _png(), pagina=n)  # type: ignore[arg-type]
        for n in range(1, 51)
    ]
    reales = [_embebida(n, _png(), pagina=51) for n in range(1, 4)]

    leidas = _leer([*logo, *reales[:2]], motor)
    assert (motor.llamadas, leidas.truncada, leidas.avisos) == (2, False, ())

    leidas = _leer([*logo, *reales, _embebida(9, _png(), pagina=52)], motor)
    assert leidas.truncada and motor.llamadas == 2 + 2
    assert leidas.avisos[0] == (
        "El documento tiene más de 3 imágenes distintas: se miraron las primeras 3."
    )


@pytest.mark.rnf("RNF-20")
def test_la_pagina_escaneada_enorme_se_renderiza_dentro_del_tope_de_pixeles(monkeypatch):
    # SEG-17, escalado: 2000 × 2000 pt a 200 dpi pedían 30,9 M de píxeles; el tope acá es 1 M.
    monkeypatch.setattr(imagenes, "TOPE_PIXELES", 1_000_000)
    ocr_falso = _OcrFalso(monkeypatch, "Acta")
    documento = pymupdf.open()
    documento.new_page(width=2000, height=2000)
    pdf = documento.tobytes()

    _transcribir(pdf, PDF)

    [(ancho, alto)] = ocr_falso.recibidas
    assert 900_000 < ancho * alto <= 1_000_000


def _foto(formato: str, modo: str, lado: int, **opciones: object) -> bytes:
    salida = io.BytesIO()
    Image.new(modo, (lado, lado // 2)).save(salida, formato, **opciones)
    return salida.getvalue()


@pytest.fixture
def topes_chicos(monkeypatch):
    """SEG-17, escalado: 25 Mpx y 500 MB pasan a 10 000 píxeles y 200 KB."""
    monkeypatch.setattr(imagenes, "TOPE_PIXELES", 10_000)
    monkeypatch.setattr(imagenes, "TOPE_DE_DECODIFICACION_BYTES", 200_000)


def _cerca_del_tope(imagen: Image.Image) -> bool:
    return 9_500 <= imagen.width * imagen.height <= 10_000


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    ("formato", "modo"),
    [("PNG", "RGB"), ("PNG", "RGBA"), ("PNG", "L"), ("PNG", "P"), ("PNG", "1"),
     ("TIFF", "RGB"), ("TIFF", "I;16"), ("TIFF", "CMYK")],
)  # fmt: skip
def test_una_foto_grande_sin_draft_se_decodifica_entera_y_se_achica_al_tope(
    topes_chicos, formato, modo
):
    # Un escaneo A3 a 600 dpi (70 Mpx) es material real de la SEU: no se rechaza.
    achicada = imagenes.abrir_fotografia(_foto(formato, modo, 200), Capacidad.TRANSCRIBIR)

    assert _cerca_del_tope(achicada) and achicada.size == (141, 70)


@pytest.mark.rnf("RNF-20")
def test_una_foto_que_no_entra_decodificada_entera_se_rechaza_con_el_motivo(topes_chicos):
    # 400 × 200 en RGB son 320 KB decodificada: pasa los 200 KB.
    with pytest.raises(LimiteExcedido, match="píxeles"):
        imagenes.abrir_fotografia(_foto("PNG", "RGB", 400), Capacidad.TRANSCRIBIR)
    # En gris ocupa un byte por píxel: la misma medida entra.
    gris = imagenes.abrir_fotografia(_foto("PNG", "L", 400), Capacidad.TRANSCRIBIR)
    assert _cerca_del_tope(gris)


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    ("formato", "opciones"),
    [("TIFF", {}), ("TIFF", {"compression": "tiff_lzw"}), ("TIFF", {"compression": "jpeg"}),
     ("PNG", {}), ("JPEG", {})],
)  # fmt: skip
def test_la_foto_grande_se_achica_sin_perder_su_orientacion(topes_chicos, formato, opciones):
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6  # rotada 90°: apaisada en el archivo, vertical a la vista
    foto = _foto(formato, "RGB", 200, exif=exif, **opciones)

    derecha = ImageOps.exif_transpose(imagenes.abrir_fotografia(foto, Capacidad.TRANSCRIBIR))

    assert derecha.size == (70, 141)


@pytest.mark.rnf("RNF-20")
def test_la_foto_grande_se_lee_y_se_describe_achicada_y_derecha(topes_chicos, monkeypatch):
    ocr_falso = _OcrFalso(monkeypatch, "Acta")
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    tiff = _foto("TIFF", "RGB", 200, exif=exif)

    _transcribir(tiff, "image/tiff")
    descrita = Image.open(io.BytesIO(entradas.normalizar_imagen(tiff)))

    # El OCR la escala ×2 después de achicarla.
    assert ocr_falso.recibidas == [(140, 282)]
    assert descrita.size == (70, 141)


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize("formato", ["JPEG", "MPO"])
def test_un_jpeg_grande_se_decodifica_ya_achicado_sin_bajar_del_tope(topes_chicos, formato):
    # Una foto de celular (suele abrir como MPO) de 400 × 200: a 1/2 da 20 000 píxeles, todavía
    # sobre el tope, y a 1/4 quedaría en 5 000. Entera, en RGB, no entra en los 200 KB.
    salida = io.BytesIO()
    cuadros = [Image.new("RGB", (400, 200)), Image.new("RGB", (400, 200))]
    varios = {"save_all": True, "append_images": cuadros[1:]} if formato == "MPO" else {}
    cuadros[0].save(salida, formato, **varios)
    assert Image.open(io.BytesIO(salida.getvalue())).format == formato

    achicada = imagenes.abrir_fotografia(salida.getvalue(), Capacidad.TRANSCRIBIR)

    assert _cerca_del_tope(achicada)
    assert Image.open(io.BytesIO(entradas.normalizar_imagen(salida.getvalue()))).size == (141, 70)


@pytest.mark.rnf("RNF-20")
def test_un_jpeg_apenas_sobre_el_tope_queda_cerca_del_tope_y_no_en_un_cuarto(topes_chicos):
    # 26 Mpx a 1/2 serían 6,5 Mpx: se decodifica entero y se lleva al tope.
    achicada = imagenes.abrir_fotografia(_foto("JPEG", "RGB", 144), Capacidad.TRANSCRIBIR)

    assert (144 * 72, achicada.size) == (10_368, (141, 70))


@pytest.mark.rnf("RNF-20")
def test_solo_entran_los_formatos_de_la_subida(topes_chicos):
    # Un GIF no llega a decodificarse como foto.
    with pytest.raises(OSError):
        imagenes.abrir_fotografia(_foto("GIF", "RGB", 40), Capacidad.TRANSCRIBIR)
