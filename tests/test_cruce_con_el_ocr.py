"""El cruce de las Afirmaciones con el OCR y el orden por riesgo (145, ADR-0007).

Una Afirmación queda respaldada solo si todos sus nombres propios, siglas, números y fechas están
en las palabras que leyó el OCR (140), y guarda dónde. El cruce es determinista: no llama a ningún
modelo. Aceptar una sin respaldo pide confirmación donde el riesgo es alto (Buçinca et al., 2021).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from capa_modelos_falsa import imagen_con_afirmaciones
from PIL import Image
from test_afirmaciones import DOS_PERSONAS, EN_LA_APP, LOGO, _decidir, _firmar, _foto_pendiente
from test_ocr import _tesseract_con_espanol

from historia_viva.capa_modelos import ocr
from historia_viva.capa_modelos.contrato import Capacidad, PalabraLeida
from historia_viva.dominio.cruce import (
    MotivoDeConfirmacion,
    Region,
    confirmacion_para_aceptar,
    datos_verificables,
    respaldo,
)

CARTEL = Path(__file__).parent / "corpus_de_ingesta" / "cartel-cyber-ar-2026.jpg"

UNDEF_MAL_EXPANDIDA = "UNDEF = Universidad Nacional de Educación a Distancia"
ROPA_FORMAL = "Personas vestidas con ropa formal"
HACKATHON = "Hackathon Cyber.AR 2026"
MARTES = "Martes 15 · Inauguración y cóctel · 18:30–21:45"
SEDE = "Facultad Militar Conjunta"


def _renglon(arriba: int, *textos: str, izquierda: int = 100) -> list[PalabraLeida]:
    """Palabras seguidas en un renglón, de 80 px de ancho cada una."""
    return [
        PalabraLeida(texto, izquierda + 90 * i, arriba, 80, 20, 90.0)
        for i, texto in enumerate(textos)
    ]


# Lo que leyó Tesseract en el cartel (140), con «Militar» mal leída en el logo y la sigla suelta.
LEIDAS_DEL_CARTEL = [
    *_renglon(100, "CYBER.AR=>"),
    *_renglon(130, "16", "-", "18", izquierda=1400),
    *_renglon(200, "ARGENTINA", "2026"),
    *_renglon(490, "HACKATHON", "-", "CYBER.AR", "2026"),
    *_renglon(570, "MARTES"),
    *_renglon(620, "15"),
    *_renglon(630, "Inauguración", "y", "cóctel", "de", "bienvenida", izquierda=400),
    *_renglon(660, "18:30", "-", "21:45", "hs", izquierda=400),
    *_renglon(700, "MIÉRCOLES"),
    *_renglon(740, "16"),
    *_renglon(860, "Ay.", "Cabildo", "15", "-", "C1426AAA", "CABA", izquierda=800),
    *_renglon(900, "Facultad", "Y", "nitar", "Conjunta"),
    *_renglon(950, "UNDEF"),
    *_renglon(980, "Universidad", "de", "la", "Defensa", "Nacional"),
]


@pytest.mark.rf("RF-08")
def test_el_cartel_de_cyber_ar_respalda_lo_leido_y_no_lo_inventado():
    assert respaldo(UNDEF_MAL_EXPANDIDA, LEIDAS_DEL_CARTEL) is None
    assert respaldo(ROPA_FORMAL, LEIDAS_DEL_CARTEL) is None
    # El OCR leyó «nitar»: la sede del logo no está en lo leído.
    assert respaldo(SEDE, LEIDAS_DEL_CARTEL) is None

    hackathon = respaldo(HACKATHON, LEIDAS_DEL_CARTEL)
    assert hackathon is not None
    # Un solo dato: el número pegado al nombre es parte de él, no el «2026» de «ARGENTINA 2026».
    assert [(r.texto, r.arriba) for r in hackathon] == [("HACKATHON - CYBER.AR 2026", 490)]
    assert (hackathon[0].izquierda, hackathon[0].ancho, hackathon[0].alto) == (100, 350, 20)

    martes = respaldo(MARTES, LEIDAS_DEL_CARTEL)
    assert martes is not None
    assert [r.texto for r in martes] == ["MARTES 15", "Inauguración", "18:30 - 21:45"]


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    "afirmacion",
    ["Martes 16 · Inauguración y cóctel · 18:30–21:45", "Miércoles 15 · Inauguración y cóctel"],
)
def test_el_dia_y_su_numero_se_respaldan_juntos(afirmacion):
    # En el cartel están «MIÉRCOLES 16», «16 - 18» y «Av. Cabildo 15», pero el martes es el 15.
    assert respaldo(afirmacion, LEIDAS_DEL_CARTEL) is None


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    "afirmacion",
    [
        "Macri inaugura el Hackathon Cyber.AR 2026.",
        "Pérez organiza el Hackathon Cyber.AR 2026.",
        "Gonzalez dio la charla de las 18:30–21:45.",
        "Hackathon Cyber.AR 2026 · Macri lo inaugura.",
    ],
)
def test_un_nombre_que_abre_la_oracion_tambien_tiene_que_estar_en_el_ocr(afirmacion):
    assert respaldo(afirmacion, LEIDAS_DEL_CARTEL) is None


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    "afirmacion",
    ["La sede está en Av. Cabildo 16.", "El Hackathon 16 es en septiembre.", "La sede está en CABA 16."],
)
def test_un_numero_pegado_a_un_nombre_se_respalda_con_el_nombre(afirmacion):
    # Hay otros «16» en el cartel («MIÉRCOLES 16», «16 - 18»), pero no junto a esos nombres.
    assert respaldo(afirmacion, LEIDAS_DEL_CARTEL) is None


@pytest.mark.rf("RF-08")
def test_un_numero_pegado_a_un_nombre_que_esta_en_el_ocr_queda_respaldado():
    region = respaldo("La sede está en Cabildo 15.", LEIDAS_DEL_CARTEL)

    assert region is not None
    assert region[0].texto == "Cabildo 15"


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    ("afirmacion", "leidas"),
    [
        ("Se esperan 2000 asistentes.", ["20:00"]),
        ("Se esperan 2000 asistentes.", ["20", ":", "00"]),
        ("Se fundó en 1830.", ["18:30"]),
        ("La charla es a las 0800.", ["08:00", "hs"]),
    ],
    ids=["hora-junta", "hora-partida", "anio", "hora-sin-dos-puntos"],
)
def test_una_hora_no_respalda_un_numero(afirmacion, leidas):
    assert respaldo(afirmacion, _renglon(10, *leidas)) is None


@pytest.mark.rf("RF-08")
def test_un_decimal_no_se_respalda_con_el_entero_pegado():
    assert respaldo("Hay 1,5 kilómetros de recorrido.", _renglon(10, "15")) is None
    assert respaldo("Hay 1,5 kilómetros de recorrido.", _renglon(10, "1.5")) is not None


@pytest.mark.rf("RF-08")
def test_la_sede_queda_respaldada_si_el_ocr_la_leyo():
    leidas = _renglon(900, "FACULTAD", "MILITAR", "CONJUNTA")

    region = respaldo(SEDE, leidas)

    assert region is not None
    assert region[0].texto == "FACULTAD MILITAR CONJUNTA"


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    ("afirmacion", "leidas"),
    [
        ("El día es el miércoles.", ["MIERCOLES"]),
        ("Lo organiza el Instituto de Ciberdefensa.", ["INSTITUTO", "DE", "CIBERDEFENSA."]),
        ("La charla termina a las 21:45.", ["21", ":", "45"]),
        ("El acto es el 15 de marzo.", ["15", "DE", "MARZO"]),
        ("El acto es el 15 de marzo.", ["15", "MARZO"]),
        ("Hay 1.500 inscriptos.", ["1500"]),
        ("El horario es 18:30–21:45.", ["18:30-21:45"]),
        ("La sigla FIE está en el logo.", ["«FIE»"]),
    ],
    ids=["tildes", "mayusculas", "hora-partida", "fecha-con-de", "fecha-sin-de", "miles",
         "rango-junto", "con-comillas"],
)
def test_la_comparacion_tolera_tildes_mayusculas_y_numeros_partidos(afirmacion, leidas):
    assert respaldo(afirmacion, _renglon(10, *leidas)) is not None


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    ("afirmacion", "leidas"),
    [
        ("El año es 15.", ["2015"]),
        ("La sigla es UNDEF.", ["info@fie.undef.edu.ar"]),
        ("Lo organiza la Universidad Nacional de Educación a Distancia.",
         ["Universidad", "de", "la", "Defensa", "Nacional"]),
        ("Es el Hackathon Cyber.AR 2026.", ["HACKATHON", "CYBER.AR"]),
        ("Hay un cartel azul con letras blancas.", ["cartel", "azul", "letras", "blancas"]),
    ],
    ids=["numero-dentro-de-otro", "sigla-dentro-de-otra-palabra", "coincidencia-parcial",
         "falta-un-dato", "sin-datos"],
)
def test_una_coincidencia_parcial_o_sin_datos_queda_sin_respaldo(afirmacion, leidas):
    assert respaldo(afirmacion, _renglon(10, *leidas)) is None


@pytest.mark.rf("RF-08")
@pytest.mark.parametrize(
    ("afirmacion", "datos"),
    [
        (UNDEF_MAL_EXPANDIDA, ("UNDEF", "Universidad Nacional de Educación a Distancia")),
        (ROPA_FORMAL, ("Personas",)),
        (HACKATHON, ("Hackathon Cyber.AR 2026",)),
        (MARTES, ("Martes 15", "Inauguración", "18:30–21:45")),
        ("El acto fue el 15 de marzo de 2026, a las 18:30.", ("15 de marzo de 2026", "18:30")),
        ("El evento lo organiza la Universidad de la Defensa Nacional (UNDEF).",
         ("Universidad de la Defensa Nacional", "UNDEF")),
        ("Abajo a la izquierda hay un logo con la sigla «UNDEF».", ("UNDEF",)),
        ("El contacto es info.cyber.ar@fie.undef.edu.ar", ("info.cyber.ar@fie.undef.edu.ar",)),
        ("Hay dos personas frente a un atril.", ()),
    ],
)
def test_los_datos_verificables_son_nombres_siglas_numeros_y_fechas(afirmacion, datos):
    assert datos_verificables(afirmacion) == datos


@pytest.mark.rf("RF-11")
def test_un_respaldo_guardado_con_pagina_se_sigue_leyendo():
    viejo = {"texto": "1975", "izquierda": 1, "arriba": 2, "ancho": 3, "alto": 4, "pagina": 1}

    assert Region.de_json(viejo) == Region("1975", 1, 2, 3, 4)


@pytest.mark.rf("RF-11")
def test_para_la_confirmacion_la_palabra_que_abre_la_oracion_no_es_un_nombre():
    assert datos_verificables(ROPA_FORMAL, contar_el_comienzo=False) == ()
    assert datos_verificables("Macri inaugura.", contar_el_comienzo=False) == ()


@pytest.mark.rf("RF-11")
@pytest.mark.parametrize(
    ("afirmacion", "respaldada", "tipo_imagen", "motivo"),
    [
        ("La sigla UNDEF está en el logo.", False, "Fotografía", MotivoDeConfirmacion.DATOS),
        ("La foto es del 15 de septiembre.", False, "Fotografía", MotivoDeConfirmacion.DATOS),
        (SEDE, False, "Cartel o gráfico", MotivoDeConfirmacion.DATOS),
        (ROPA_FORMAL, False, "Cartel o gráfico", MotivoDeConfirmacion.PERSONAS),
        ("Hay dos personas frente a un atril.", False, "Página impresa",
         MotivoDeConfirmacion.PERSONAS),
        # En una foto las personas se ven y no se leen: nunca tienen respaldo en el OCR.
        ("Hay dos personas frente a un atril.", False, "Fotografía", None),
        ("El fondo es azul.", False, "Cartel o gráfico", None),
        (HACKATHON, True, "Cartel o gráfico", None),
    ],
    ids=["sigla", "fecha", "nombre-propio", "personas-en-cartel", "personas-en-pagina",
         "personas-en-foto", "lo-demas", "respaldada"],
)
def test_aceptar_sin_respaldo_pide_confirmacion_solo_si_el_riesgo_es_alto(
    afirmacion, respaldada, tipo_imagen, motivo
):
    assert confirmacion_para_aceptar(
        afirmacion, respaldada=respaldada, tipo_imagen=tipo_imagen
    ) is motivo


@pytest.mark.rf("RF-08")
@pytest.mark.skipif(not _tesseract_con_espanol(), reason="Necesita Tesseract con 'spa'.")
def test_el_cartel_real_leido_por_tesseract():
    with Image.open(CARTEL) as imagen:
        palabras = ocr.leer(imagen, ocr.AjustesOcr(), tiempo_maximo_s=120).palabras
    afirmaciones = (UNDEF_MAL_EXPANDIDA, ROPA_FORMAL, HACKATHON, MARTES)

    inicio = time.perf_counter()
    respaldos = [respaldo(a, palabras) for a in afirmaciones]
    assert time.perf_counter() - inicio < 1

    assert [r is not None for r in respaldos] == [False, False, True, True]
    # «Facultad Militar Conjunta» está en un logo: respaldada solo si el OCR la leyó entera.
    leyo_la_sede = "facultad militar conjunta" in " ".join(p.texto for p in palabras).casefold()
    assert (respaldo(SEDE, palabras) is not None) is leyo_la_sede


# --- Por la API, con la capa falsa ------------------------------------------------------------

SELLO = "El sello dice Facultad de Ingeniería del Ejército, 1975."
"""Respaldada por `TEXTO_VISIBLE_FIJO`, «Facultad de Ingeniería del Ejército — 1975»."""


@EN_LA_APP
@pytest.mark.rf("RF-08", "RF-11")
def test_cada_afirmacion_llega_cruzada_con_su_region_y_sin_otra_llamada_al_modelo(
    cliente, capa_modelos
):
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, LOGO, SELLO))

    afirmaciones = cliente.get(f"/api/piezas/{pieza_id}/afirmaciones").json()["afirmaciones"]

    assert [(a["orden"], a["respaldada"], a["confirmar"]) for a in afirmaciones] == [
        (1, False, None),  # personas en una Fotografía: un clic
        (2, False, "datos"),  # la sigla UNDEF no está en lo leído
        (3, True, None),
    ]
    assert afirmaciones[0]["region"] is None
    assert [r["texto"] for r in afirmaciones[2]["region"]] == [
        "Facultad de Ingeniería del Ejército", "1975"
    ]
    assert set(afirmaciones[2]["region"][0]) == {
        "texto", "izquierda", "arriba", "ancho", "alto"
    }
    # Describir, leer y resumir: el cruce no agrega ninguna llamada.
    assert [i.capacidad for i in capa_modelos.invocaciones] == [
        Capacidad.DESCRIBIR_IMAGEN, Capacidad.TRANSCRIBIR, Capacidad.RESUMIR
    ]

    decidida = _decidir(cliente, pieza_id, 3, "aceptada").json()
    assert (decidida["estado"], decidida["respaldada"], decidida["confirmar"]) == (
        "aceptada", True, None
    )
    assert decidida["region"] == afirmaciones[2]["region"]


@EN_LA_APP
@pytest.mark.rf("RF-08", "RF-11")
def test_aceptar_sin_respaldo_por_la_api_pide_la_misma_confirmacion_que_la_pantalla(cliente):
    # SEG-21: el PUT exige `confirmada` cuando `confirmar` no es `None`, no solo la pantalla.
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, LOGO))
    ruta = f"/api/piezas/{pieza_id}/afirmaciones/2"

    sin_confirmar = cliente.put(ruta, json={"decision": "aceptada"})

    assert sin_confirmar.status_code == 409
    assert sin_confirmar.json()["confirmar"] == "datos"
    estados = [a["estado"] for a in cliente.get(f"/api/piezas/{pieza_id}/afirmaciones").json()[
        "afirmaciones"
    ]]
    assert estados == ["sin_decidir", "sin_decidir"]
    # Rechazarla, o aceptar la que no pide confirmación, sigue siendo un clic.
    assert cliente.put(ruta, json={"decision": "rechazada"}).status_code == 200
    assert _decidir(cliente, pieza_id, 1, "aceptada").status_code == 200
    confirmada = cliente.put(ruta, json={"decision": "aceptada", "confirmada": True})
    assert (confirmada.status_code, confirmada.json()["estado"]) == (200, "aceptada")
    # Con la Pieza Validada manda el estado, no la confirmación.
    assert _firmar(cliente, pieza_id).json()["estado"] == "Validada"
    validada = cliente.put(ruta, json={"decision": "aceptada"})
    assert (validada.status_code, validada.json()["type"]) == (409, "/errores/transicion-invalida")
