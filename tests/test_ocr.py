"""El OCR preparado y con confianza (140): preparación, filtro, palabras y orden de lectura.

Casi todo corre con un motor falso o con `image_to_data` reemplazado: no hace falta Tesseract.
El de la imagen oscura sí lo necesita, con el idioma `spa` (con `HV_TESSERACT_CMD` y
`TESSDATA_PREFIX` si no está en el PATH); sin él se salta.
"""

from __future__ import annotations

import io
from dataclasses import replace

import pytesseract
import pytest
from ollama_falso import CONFIGURACION_DE_PRUEBA, OllamaFalso
from PIL import Image, ImageDraw, ImageFont, ImageStat

from historia_viva.capa_modelos import ocr
from historia_viva.capa_modelos.configuracion import validar
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_IMAGEN,
    BloqueBinario,
    Capacidad,
    PalabraLeida,
)
from historia_viva.capa_modelos.local import capa_para
from historia_viva.dominio.errores import ConfiguracionInvalida

pytestmark = pytest.mark.rf("RF-07")


def _tesseract_con_espanol() -> bool:
    try:
        return "spa" in pytesseract.get_languages()
    except (pytesseract.TesseractNotFoundError, OSError, RuntimeError):
        return False


def _palabra(texto: str, x: int, y: int, renglon: int, *, ancho: int = 0, conf: float = 95):
    return ocr.PalabraDelMotor(
        texto=texto,
        izquierda=x,
        arriba=y,
        ancho=ancho or 12 * len(texto),
        alto=20,
        confianza=conf,
        renglon=(renglon,),
    )


class _MotorFalso:
    def __init__(self, *palabras: ocr.PalabraDelMotor) -> None:
        self._palabras = list(palabras)
        self.recibida: Image.Image | None = None

    def palabras(self, imagen: Image.Image, *, tiempo_maximo_s: float):
        self.recibida = imagen
        return self._palabras


def _leer(*palabras: ocr.PalabraDelMotor, ajustes: ocr.AjustesOcr | None = None) -> ocr.Lectura:
    imagen = Image.new("RGB", (400, 300), "white")
    sin_escala = ocr.AjustesOcr(escala=1)
    return ocr.leer(
        imagen, ajustes or sin_escala, tiempo_maximo_s=5, motor=_MotorFalso(*palabras)
    )


def _png(imagen: Image.Image) -> bytes:
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


def test_la_imagen_se_pasa_a_gris_se_escala_y_se_invierte_si_el_fondo_es_oscuro():
    oscura = Image.new("RGB", (300, 100), "#0b1530")

    preparada, factor = ocr.preparar(oscura, ocr.AjustesOcr())

    assert preparada.mode == "L"
    assert (factor, preparada.size) == (2.0, (600, 200))
    assert ImageStat.Stat(preparada).mean[0] > 128
    sin_invertir, _ = ocr.preparar(oscura, ocr.AjustesOcr(invertir=False))
    assert ImageStat.Stat(sin_invertir).mean[0] < 128


def test_la_escala_no_pasa_del_lado_maximo_ni_achica_una_imagen_grande():
    mediana, _ = ocr.preparar(Image.new("L", (3000, 1000), 255), ocr.AjustesOcr())
    grande, factor = ocr.preparar(Image.new("L", (5000, 1000), 255), ocr.AjustesOcr())

    assert mediana.size == (ocr.LADO_MAXIMO_PX, 1333)
    assert (grande.size, factor) == ((5000, 1000), 1.0)


@pytest.mark.parametrize("modo", ["RGBA", "LA", "P"])
def test_el_texto_negro_sobre_transparente_queda_negro_sobre_blanco(modo):
    if modo == "P":
        # Paleta con el índice 0 (magenta) transparente: sin aplanar quedaría gris.
        imagen = Image.new("P", (100, 40), 0)
        imagen.putpalette([255, 0, 255, 0, 0, 0])
        imagen.info["transparency"] = 0
        ImageDraw.Draw(imagen).rectangle((10, 10, 30, 30), fill=1)
    else:
        imagen = Image.new("RGBA", (100, 40), (0, 0, 0, 0))
        ImageDraw.Draw(imagen).rectangle((10, 10, 30, 30), fill=(0, 0, 0, 255))
        imagen = imagen.convert(modo)

    preparada, _ = ocr.preparar(imagen, ocr.AjustesOcr(escala=1))

    assert (preparada.getpixel((60, 20)), preparada.getpixel((20, 20))) == (255, 0)


def test_la_foto_se_endereza_con_su_exif_antes_de_leer():
    # Guardada apaisada con «rotar 90°» en el EXIF (orientación 6): el navegador la muestra
    # parada, y las coordenadas tienen que ser las de la imagen parada.
    imagen = Image.new("RGB", (300, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    imagen.save(buffer, format="JPEG", exif=exif)

    preparada, _ = ocr.preparar(Image.open(buffer), ocr.AjustesOcr(escala=1))

    assert preparada.size == (100, 300)


def test_la_binarizacion_deja_solo_tinta_y_fondo():
    imagen = Image.new("L", (200, 80), 200)
    ImageDraw.Draw(imagen).rectangle((50, 30, 60, 50), fill=40)

    preparada, _ = ocr.preparar(imagen, ocr.AjustesOcr(escala=1, binarizar=True))

    assert {valor for _, valor in preparada.getcolors()} == {0, 255}
    assert preparada.getpixel((55, 40)) == 0
    assert preparada.getpixel((150, 40)) == 255


def test_las_palabras_dudosas_y_los_renglones_sin_letras_no_llegan_al_texto():
    lectura = _leer(
        _palabra("Inauguración", 10, 10, 1),
        _palabra("AAA", 200, 10, 1, conf=30),
        _palabra("——", 10, 50, 2),
        _palabra("==", 60, 50, 2),
        _palabra("15", 10, 90, 3),
        ajustes=ocr.AjustesOcr(escala=1, confianza_minima=40),
    )

    # Una cifra sola es contenido (el día del cronograma); una raya, no.
    assert lectura.texto == "Inauguración\n15"
    assert [p.texto for p in lectura.palabras] == ["Inauguración", "15"]


def test_con_umbral_cero_la_palabra_de_confianza_baja_queda():
    lectura = _leer(_palabra("Cabildo", 10, 10, 1, conf=0))

    assert lectura.texto == "Cabildo"


def test_devuelve_las_palabras_con_coordenadas_de_la_original_y_confianza():
    motor = _MotorFalso(_palabra("Acta", 100, 60, 1, ancho=80, conf=91.5))
    imagen = Image.new("RGB", (400, 300), "white")

    lectura = ocr.leer(imagen, ocr.AjustesOcr(escala=2), tiempo_maximo_s=5, motor=motor)

    assert motor.recibida is not None and motor.recibida.size == (800, 600)
    assert lectura.palabras == (PalabraLeida("Acta", 50, 30, 40, 10, 91.5),)


def test_un_cronograma_conserva_dia_actividad_y_horario_juntos():
    # Dos bandas (martes y miércoles) con dos columnas: el día a la izquierda y la actividad
    # con su horario a la derecha. El motor devuelve primero toda la columna de días.
    lectura = _leer(
        _palabra("MARTES", 10, 100, 1),
        _palabra("15", 10, 130, 2),
        _palabra("MIÉRCOLES", 10, 300, 3),
        _palabra("16", 10, 330, 4),
        _palabra("Inauguración", 200, 100, 5),
        _palabra("18:30 hs", 200, 130, 6),
        _palabra("Desarrollo", 200, 300, 7),
        _palabra("08:00 hs", 200, 330, 8),
    )

    assert lectura.texto.splitlines() == [
        "MARTES", "15", "Inauguración", "18:30 hs",
        "MIÉRCOLES", "16", "Desarrollo", "08:00 hs",
    ]


def test_entre_dos_parrafos_del_motor_va_una_linea_en_blanco():
    lectura = _leer(
        replace(_palabra("Estado", 10, 10, 1), parrafo=(1,)),
        replace(_palabra("Mayor", 90, 10, 1), parrafo=(1,)),
        replace(_palabra("del", 10, 35, 2), parrafo=(1,)),
        replace(_palabra("La", 10, 60, 3), parrafo=(2,)),
    )

    assert lectura.texto == "Estado Mayor\ndel\n\nLa"


def test_un_icono_lejano_en_el_renglon_no_junta_dos_columnas():
    lectura = _leer(
        _palabra("Facultad", 10, 100, 1),
        _palabra("Militar", 10, 130, 2),
        _palabra("O", 10, 160, 3),
        _palabra("CONTACTO", 300, 160, 3),
        _palabra("Av.", 300, 100, 4),
        _palabra("Cabildo", 340, 100, 4),
    )

    assert lectura.texto.splitlines() == ["Facultad", "Militar", "O", "Av. Cabildo", "CONTACTO"]


def _datos_de_tesseract(*palabras: tuple[str, int, int, float]) -> dict[str, list]:
    """Lo que devuelve `image_to_data` con `Output.DICT`: una fila de página y una por palabra,
    cada palabra en su propio renglón."""
    filas = [("", 0, 0, -1, 0)] + [(t, x, y, c, i) for i, (t, x, y, c) in enumerate(palabras, 1)]
    return {
        "text": [f[0] for f in filas],
        "left": [f[1] for f in filas],
        "top": [f[2] for f in filas],
        "width": [10 * len(f[0]) for f in filas],
        "height": [16 for _ in filas],
        "conf": [f[3] for f in filas],
        "block_num": [1 for _ in filas],
        "par_num": [1 for _ in filas],
        "line_num": [f[4] for f in filas],
    }


def test_la_preparacion_y_el_umbral_se_cambian_en_la_fila_sin_tocar_codigo(monkeypatch):
    recibidas: list[Image.Image] = []

    def _image_to_data(imagen, **kwargs):
        recibidas.append(imagen)
        return _datos_de_tesseract(("Acta", 20, 20, 90), ("AAA", 20, 200, 35))

    monkeypatch.setattr(pytesseract, "image_to_data", _image_to_data)
    foto = BloqueBinario(
        etiqueta=ETIQUETA_IMAGEN,
        contenido=_png(Image.new("RGB", (300, 200), "white")),
        tipo_mime="image/png",
    )

    def _transcribir(**parametros):
        fila = CONFIGURACION_DE_PRUEBA[Capacidad.TRANSCRIBIR]
        fila = replace(fila, parametros={**fila.parametros, **parametros})
        configuracion = {**CONFIGURACION_DE_PRUEBA, Capacidad.TRANSCRIBIR: fila}
        return capa_para(configuracion, OllamaFalso().motores()).transcribir(archivo=foto)

    por_omision = _transcribir()
    cambiada = _transcribir(ocr_escala=1, ocr_confianza_minima=50)

    assert por_omision.texto == "Acta\nAAA"
    assert recibidas[0].size == (600, 400)
    assert por_omision.palabras[0] == PalabraLeida("Acta", 10, 10, 20, 8, 90.0)
    assert cambiada.texto == "Acta"
    assert recibidas[1].size == (300, 200)
    assert cambiada.configuracion_efectiva.por_capacidad[Capacidad.TRANSCRIBIR].parametros[
        "ocr_confianza_minima"
    ] == 50


@pytest.mark.parametrize(
    ("capacidad", "parametros", "campo"),
    [
        (Capacidad.TRANSCRIBIR, {"ocr_escala": 8}, "ocr_escala"),
        (Capacidad.TRANSCRIBIR, {"ocr_invertir": 1}, "ocr_invertir"),
        (Capacidad.TRANSCRIBIR, {"ocr_binarizar": "no"}, "ocr_binarizar"),
        (Capacidad.TRANSCRIBIR, {"ocr_confianza_minima": 101}, "ocr_confianza_minima"),
        (Capacidad.RESUMIR, {"ocr_escala": 2}, "ocr_escala"),
    ],
)
def test_un_parametro_de_ocr_mal_puesto_no_valida(capacidad, parametros, campo):
    fila = CONFIGURACION_DE_PRUEBA[capacidad]
    fila = replace(fila, parametros={**fila.parametros, **parametros})

    with pytest.raises(ConfiguracionInvalida) as error:
        validar({**CONFIGURACION_DE_PRUEBA, capacidad: fila}, OllamaFalso().motores())

    assert (capacidad.value, campo) in {(e.capacidad, e.campo) for e in error.value.errores}


def test_la_fila_sembrada_con_los_valores_medidos_valida():
    fila = CONFIGURACION_DE_PRUEBA[Capacidad.TRANSCRIBIR]
    medidos = {"ocr_escala": 2, "ocr_invertir": True, "ocr_binarizar": False,
               "ocr_confianza_minima": 0}
    fila = replace(fila, parametros={**fila.parametros, **medidos})

    validar({**CONFIGURACION_DE_PRUEBA, Capacidad.TRANSCRIBIR: fila}, OllamaFalso().motores())
    assert ocr.AjustesOcr.de(fila.parametros) == ocr.AjustesOcr()


_TEXTO_OSCURO = ("CONGRESO DE CIBERDEFENSA", "Instituto de las Fuerzas Armadas")


def _cartel_oscuro() -> Image.Image:
    """Texto celeste chico sobre azul noche, como las franjas del cartel de Cyber.AR."""
    imagen = Image.new("RGB", (500, 120), "#0b1530")
    dibujo = ImageDraw.Draw(imagen)
    fuente = ImageFont.load_default(size=10)  # la de Pillow: no depende del sistema
    for i, renglon in enumerate(_TEXTO_OSCURO):
        dibujo.text((20, 30 + 40 * i), renglon, fill="#9cc3e6", font=fuente)
    return imagen


@pytest.mark.skipif(not _tesseract_con_espanol(), reason="Necesita Tesseract con 'spa'.")
def test_una_imagen_de_fondo_oscuro_que_antes_daba_basura_ahora_da_el_texto():
    imagen = _cartel_oscuro()
    antes = pytesseract.image_to_string(imagen, lang="spa").split()
    foto = BloqueBinario(etiqueta=ETIQUETA_IMAGEN, contenido=_png(imagen), tipo_mime="image/png")

    resultado = capa_para(CONFIGURACION_DE_PRUEBA, OllamaFalso().motores()).transcribir(
        archivo=foto
    )

    assert antes != " ".join(_TEXTO_OSCURO).split()
    assert [renglon for renglon in resultado.texto.splitlines() if renglon] == list(_TEXTO_OSCURO)
    assert [p.texto for p in resultado.palabras] == " ".join(_TEXTO_OSCURO).split()


@pytest.mark.rnf("RNF-18")
def test_tesseract_no_hereda_la_clave_de_la_base_ni_otros_secretos():
    # SEG-18: Tesseract lee archivos ajenos; su entorno es una lista corta, no el del trabajador.
    entorno = pytesseract.pytesseract.environ

    assert "PATH" in entorno
    assert not [variable for variable in entorno if variable.startswith("HV_")]
    assert set(entorno) <= set(ocr._ENTORNO_DE_TESSERACT)
