"""Procesar una fotografía: la Ficha propuesta trae la descripción y el texto visible.

Es el mismo flujo que un documento (`test_procesar_pieza.py`), con una diferencia: de una
imagen no sale una transcripción sino una descripción propuesta más el OCR de lo que esté
escrito en ella (user story 7, RF-08). Lo que se prueba acá es esa diferencia y que el
camino hasta Validada no cambie por ser una foto.
"""

from __future__ import annotations

import psycopg
import pytest
from capa_modelos_falsa import (
    IMAGEN,
    IMAGEN_SIN_TEXTO,
    RESUMEN_FIJO,
    TEXTO_VISIBLE_FIJO,
    TOKENS_DE_DESCRIPCION,
    TOKENS_DE_RESUMEN,
    imagen_con_afirmaciones,
)
from espera import esperar_estado
from ollama_falso import CONFIGURACION_DE_PRUEBA
from validacion import CONTEXTO, EDICION_COMPLETA, MARIA

from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CUERPO_FICHA,
    ETIQUETA_IMAGEN,
    Plantilla,
)
from historia_viva.procesamiento.cola import CAPACIDADES_DE_IMAGEN
from historia_viva.procesamiento.manejador import ENCABEZADO_TEXTO_VISIBLE
from aplicacion import entrar

pytestmark = [
    pytest.mark.rf("RF-08"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

CUERPO_DE_LA_FOTO = f"{ENCABEZADO_TEXTO_VISIBLE}\n\n{TEXTO_VISIBLE_FIJO}"
"""El cuerpo de la v1: lo que la foto tiene escrito, leído por el OCR. La descripción llega como
Afirmaciones aparte y entra al cuerpo al firmar (144). De `IMAGEN` la capa falsa no propone
ninguna, así que queda con el aviso de la descripción fallida y se firma sin decidir nada."""


def _subir_foto(cliente, contenido=IMAGEN, nombre="acto-de-egreso.jpg"):
    entrar(cliente, MARIA)
    return cliente.post("/api/piezas", files={"archivo": (nombre, contenido, "image/jpeg")})


def _procesar_foto(cliente, contenido=IMAGEN):
    """Sube una fotografía y espera a que su Ficha propuesta esté lista."""
    pieza_id = _subir_foto(cliente, contenido).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    return pieza_id


def test_una_imagen_queda_pendiente_con_su_texto_visible(cliente):
    pieza_id = _procesar_foto(cliente)

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    assert ficha["version"] == 1
    assert ficha["origen_version"] == "modelo"
    assert ficha["cuerpo"] == CUERPO_DE_LA_FOTO
    assert ficha["frontmatter"]["estado"] == "Pendiente"
    assert ficha["frontmatter"]["resumen"] == RESUMEN_FIJO
    assert ficha["frontmatter"]["archivo_original"]


def test_una_imagen_sin_texto_visible_queda_pendiente_sin_encabezado_vacio(cliente):
    pieza_id = _procesar_foto(cliente, IMAGEN_SIN_TEXTO)

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    # El OCR no aportó nada, y lo que no aporta nada no deja un encabezado vacío en el
    # cuerpo que alguien tenga que borrar a mano (`casos-de-uso.md`, flujo 2a).
    assert ficha["cuerpo"] == ""
    assert ENCABEZADO_TEXTO_VISIBLE not in ficha["cuerpo"]
    assert ficha["frontmatter"]["estado"] == "Pendiente"
    assert ficha["frontmatter"]["resumen"] == RESUMEN_FIJO


def test_de_una_foto_se_describe_y_se_transcribe_y_de_las_dos_sale_el_resumen(
    cliente, capa_modelos
):
    _procesar_foto(cliente)

    # Se compara contra el plan con el que la cola calculó el tope: describir una foto es un
    # paso más, y un paso que el plan no previera dejaría al trabajo con un tope que no cubre
    # lo que ejecuta (ADR-0004, límite duro 3).
    assert [
        invocacion.capacidad for invocacion in capa_modelos.invocaciones
    ] == list(CAPACIDADES_DE_IMAGEN)
    describir, transcribir, resumir = capa_modelos.invocaciones
    # A una foto se le pide una descripción; el OCR es otra plantilla que la del documento.
    assert describir.plantilla is Plantilla.DESCRIBIR_IMAGEN_EN_AFIRMACIONES  # la fila de 144
    assert transcribir.plantilla is Plantilla.TRANSCRIBIR_IMAGEN
    assert resumir.plantilla is Plantilla.RESUMIR_FICHA
    (bloque_cuerpo,) = resumir.bloques
    assert bloque_cuerpo.etiqueta == ETIQUETA_CUERPO_FICHA
    # Sin Afirmaciones válidas (ver `CUERPO_DE_LA_FOTO`), el resumen sale de lo leído.
    assert bloque_cuerpo.contenido == CUERPO_DE_LA_FOTO


def test_el_binario_de_la_foto_viaja_como_bloque_de_datos_y_se_registra_por_su_huella(
    cliente, capa_modelos
):
    _procesar_foto(cliente)

    describir, transcribir, _ = capa_modelos.invocaciones
    for invocacion in (describir, transcribir):
        # La imagen entró por el canal de datos, etiquetada, y el registro guarda su huella
        # en lugar del binario: no hay dónde concatenarla a una instrucción (ADR-0003).
        (bloque,) = invocacion.bloques
        assert bloque.etiqueta == ETIQUETA_IMAGEN
        assert bloque.contenido.startswith("sha256:")


def test_ninguna_invocacion_sobre_una_foto_sale_sin_los_topes_de_su_capacidad(
    cliente, capa_modelos
):
    _procesar_foto(cliente)

    for invocacion in capa_modelos.invocaciones:
        assert invocacion.limites == CONFIGURACION_DE_PRUEBA[invocacion.capacidad].limites
        assert invocacion.limites.tiempo_maximo_s > 0


def test_el_trabajo_de_una_foto_nace_con_el_tope_de_las_tres_capacidades(cliente, url_bd):
    pieza_id = _procesar_foto(cliente)

    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT estado, tiempo_maximo_ms FROM acervo.trabajo_procesamiento"
            " WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
    estado, tiempo_maximo_ms = fila
    # Describir una imagen es un paso más, y un trabajo no puede durar menos que la suma de
    # los topes de lo que va a invocar (ADR-0004).
    assert estado == "completado"
    # 180 s + 300 s + 90 s de la semilla (tabla de valores operativos).
    assert tiempo_maximo_ms == 570_000


def test_el_trabajo_de_una_foto_mide_los_tokens_de_entrada_y_de_salida(cliente, url_bd):
    # Con Afirmaciones: la respuesta fuera del esquema no informa sus tokens.
    pieza_id = _procesar_foto(cliente, imagen_con_afirmaciones("Hay un cartel."))

    with psycopg.connect(url_bd) as conexion:
        tokens = conexion.execute(
            "SELECT tokens_entrada, tokens_salida FROM acervo.trabajo_procesamiento"
            " WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
    # Se miden sin tope (el tope es por llamada): el trabajo suma los de cada capacidad, y
    # `transcribir` no es un modelo de lenguaje.
    assert tokens == (
        TOKENS_DE_DESCRIPCION[0] + TOKENS_DE_RESUMEN[0],
        TOKENS_DE_DESCRIPCION[1] + TOKENS_DE_RESUMEN[1],
    )


def test_validar_una_foto_la_deja_validada_por_el_mismo_camino_que_un_documento(cliente):
    pieza_id = _procesar_foto(cliente)
    edicion = EDICION_COMPLETA | {
        "tipo": "fotografia",
        "cuerpo": CUERPO_DE_LA_FOTO,
        "contexto": CONTEXTO,
    }

    guardado = cliente.put(f"/api/piezas/{pieza_id}/ficha", json=edicion)

    assert guardado.status_code == 200
    assert guardado.json()["estado"] == "Validada"
    assert guardado.json()["campos_faltantes"] == []
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert ficha["frontmatter"]["tipo"] == "fotografia"
    assert ficha["frontmatter"]["validada_por"] == MARIA
    assert (ficha["cuerpo"], ficha["contexto"]) == (CUERPO_DE_LA_FOTO, CONTEXTO)
    # La propuesta sigue siendo la del modelo: es contra ella que se mide qué corrigió la
    # persona (user story 23).
    assert ficha["propuesta"]["cuerpo"] == CUERPO_DE_LA_FOTO
