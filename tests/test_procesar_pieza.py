"""Procesar un documento: la Pieza pasa sola a Pendiente con su Ficha propuesta."""

from __future__ import annotations

import psycopg
import pytest
from capa_modelos_falsa import RESUMEN_FIJO, TRANSCRIPCION_FIJA
from espera import esperar_estado
from ollama_falso import CONFIGURACION_DE_PRUEBA
from test_fichas import divergencias

from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CUERPO_FICHA,
    ETIQUETA_DOCUMENTO,
    Plantilla,
)
from historia_viva.procesamiento.cola import CAPACIDADES_DE_DOCUMENTO, CAPACIDADES_DE_TEXTO
from aplicacion import entrar

pytestmark = [
    pytest.mark.rf("RF-05", "RF-07", "RF-09"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

MARIA = "11111111-1111-4111-8111-111111111111"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"

OBLIGATORIOS = [
    "tipo",
    "tipo_actividad",
    "fecha_inicio",
    "fecha_fin",
    "precision_fecha",
    "origen",
    "propietario",
    "resumen",
    "archivo_original",
]
"""Los nueve de DEC-126, iguales para toda clase de Pieza."""


def _subir(cliente, nombre="acta-fundacion.pdf", contenido=PDF):
    entrar(cliente, MARIA)
    return cliente.post("/api/piezas", files={"archivo": (nombre, contenido, "application/pdf")})


def _procesar(cliente):
    """Sube un documento y espera a que la Pieza llegue a Pendiente."""
    pieza_id = _subir(cliente).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    return pieza_id


def test_al_terminar_el_procesamiento_la_pieza_queda_pendiente_con_cuerpo_y_resumen(cliente):
    pieza_id = _procesar(cliente)

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    assert ficha["version"] == 1
    assert ficha["origen_version"] == "modelo"
    assert ficha["cuerpo"] == TRANSCRIPCION_FIJA
    assert ficha["frontmatter"]["resumen"] == RESUMEN_FIJO
    assert ficha["frontmatter"]["estado"] == "Pendiente"
    assert ficha["frontmatter"]["archivo_original"]
    assert ficha["frontmatter"]["id"] == pieza_id


def test_la_fila_de_la_lista_pasa_sola_de_recibida_a_pendiente(cliente, capa_modelos):
    capa_modelos.demorar()
    pieza_id = _subir(cliente).json()["pieza_id"]

    assert cliente.get("/api/piezas?estado=Recibida").json()["piezas"][0]["pieza_id"] == pieza_id

    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")
    pendientes = cliente.get("/api/piezas?estado=Pendiente").json()["piezas"]
    assert [pieza["pieza_id"] for pieza in pendientes] == [pieza_id]
    assert pendientes[0]["resumen"] == RESUMEN_FIJO


def test_la_respuesta_de_la_carga_vuelve_con_la_pieza_todavia_recibida(cliente, capa_modelos):
    capa_modelos.demorar()

    respuesta = _subir(cliente)

    # La carga contestó mientras la capa de modelos sigue colgada: el 201 no espera al
    # procesamiento (user story 1, RF-05).
    assert respuesta.status_code == 201
    assert respuesta.json()["estado"] == "Recibida"
    pieza_id = respuesta.json()["pieza_id"]
    assert cliente.get(f"/api/piezas/{pieza_id}").json()["estado"] == "Recibida"
    assert cliente.get(f"/api/piezas/{pieza_id}/ficha").status_code == 404

    capa_modelos.liberar()
    assert esperar_estado(cliente, pieza_id, "Pendiente")["estado"] == "Pendiente"


def test_el_archivo_le_llega_a_la_capa_como_bloque_de_datos_y_nunca_como_instruccion(
    cliente, capa_modelos
):
    _procesar(cliente)

    transcribir, resumir = capa_modelos.invocaciones
    # La plantilla es un identificador del enum cerrado, no texto compuesto (ADR-0003, capa 1).
    assert transcribir.plantilla is Plantilla.TRANSCRIBIR_DOCUMENTO
    assert resumir.plantilla is Plantilla.RESUMIR_FICHA
    # El contenido del archivo viajó solo por el canal de datos, etiquetado, y registrado
    # por su huella: no hay un solo lugar donde pudiera haberse concatenado a una instrucción.
    (bloque_archivo,) = transcribir.bloques
    assert bloque_archivo.etiqueta == ETIQUETA_DOCUMENTO
    assert bloque_archivo.contenido.startswith("sha256:")
    (bloque_cuerpo,) = resumir.bloques
    assert bloque_cuerpo.etiqueta == ETIQUETA_CUERPO_FICHA
    assert bloque_cuerpo.contenido == TRANSCRIPCION_FIJA


def test_ninguna_invocacion_sale_sin_los_topes_de_su_capacidad(cliente, capa_modelos):
    _procesar(cliente)

    # Se compara contra el plan con el que la cola calculó el tope, no contra una lista
    # escrita a mano: si el procesamiento invocara algo que el plan no previó, el trabajo
    # tendría un tope que no cubre lo que ejecuta (ADR-0004, límite duro 3).
    assert [
        invocacion.capacidad for invocacion in capa_modelos.invocaciones
    ] == list(CAPACIDADES_DE_DOCUMENTO)
    for invocacion in capa_modelos.invocaciones:
        # Los topes de la fila de la semilla (`CONFIGURACION_DE_PRUEBA` los repite).
        assert invocacion.limites == CONFIGURACION_DE_PRUEBA[invocacion.capacidad].limites
        assert invocacion.limites.tiempo_maximo_s > 0


def test_la_propuesta_no_pre_llena_los_campos_duros(cliente):
    pieza_id = _procesar(cliente)

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    frontmatter = ficha["frontmatter"]
    assert frontmatter["tipo"] is None
    assert frontmatter["origen"] is None
    assert frontmatter["fecha_inicio"] is None
    assert frontmatter["fecha_fin"] is None
    assert frontmatter["precision_fecha"] is None
    assert ficha["obligatorios"] == OBLIGATORIOS
    assert ficha["faltantes"] == [
        "tipo",
        "tipo_actividad",
        "fecha_inicio",
        "fecha_fin",
        "precision_fecha",
        "origen",
        "propietario",
    ]


def test_la_propuesta_es_la_version_1_y_viaja_junto_con_la_vigente(cliente):
    pieza_id = _procesar(cliente)

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    assert ficha["propuesta"] == {
        "frontmatter": ficha["frontmatter"],
        "cuerpo": ficha["cuerpo"],
        "contexto": "",
    }


def test_la_ficha_de_una_pieza_que_no_existe_responde_404(cliente):
    entrar(cliente, MARIA)

    respuesta = cliente.get("/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/ficha")

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/pieza-no-encontrada"


def test_consultar_la_ficha_sin_sesion_responde_401(cliente):
    respuesta = cliente.get("/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/ficha")

    assert respuesta.status_code == 401


def test_el_trabajo_queda_completado_y_con_su_tope_en_la_cola(cliente, url_bd):
    pieza_id = _procesar(cliente)

    # La cola es la única fuente de verdad del avance: no viaja por la API, se mira acá.
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT estado, generaciones, tiempo_maximo_ms"
            " FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
    estado, generaciones, tiempo_maximo_ms = fila
    # La generación se cuenta al tomar el trabajo (DEC-30): un intento, una generación.
    assert (estado, generaciones) == ("completado", 1)
    # Todo trabajo nace con tope, y el suyo es el de las capacidades que invoca (ADR-0004):
    # `transcribir` y `resumir` de la semilla, 300 s + 90 s (tabla de valores operativos).
    assert tiempo_maximo_ms == 390_000


ACTA_TXT = "Acta del Consejo.\r\nSe aprobó el convenio con la Peña.\r\n".encode("cp1252")


@pytest.mark.rf("RF-04")
@pytest.mark.historia(289)
def test_un_txt_es_una_pieza_de_texto_que_se_resume_y_llega_a_pendiente(
    cliente, capa_modelos, url_bd
):
    """Decisión del usuario (028): una persona sube texto plano y es una Pieza `texto`."""
    pieza_id = _subir(cliente, "acta.txt", ACTA_TXT).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    # El texto es el cuerpo, decodificado con lo que detectó la admisión, en NFC y sin `\r`
    # (R-36); de él solo se pide el resumen.
    assert ficha["cuerpo"] == "Acta del Consejo.\nSe aprobó el convenio con la Peña."
    assert ficha["frontmatter"]["resumen"] == RESUMEN_FIJO
    assert [i.capacidad for i in capa_modelos.invocaciones] == list(CAPACIDADES_DE_TEXTO)
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        assert conexion.execute(
            "SELECT clase_original, via_ingreso, tipo_mime FROM acervo.pieza"
            " WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone() == ("texto", "archivo", "text/plain; charset=windows-1252")
        assert divergencias(conexion) == []


@pytest.mark.rf("RF-04")
@pytest.mark.historia(289)
def test_un_txt_con_fin_de_linea_solo_cr_conserva_sus_lineas(cliente):
    """Un `.txt` de Mac clásico corta las líneas con `\r` suelto: quitarlo como control las une."""
    pieza_id = _subir(cliente, "nota.txt", b"linea1\rlinea2\r\n\rlinea3\r").json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert ficha["cuerpo"] == "linea1\nlinea2\n\nlinea3"


@pytest.mark.rf("RF-04")
@pytest.mark.historia(289)
def test_un_txt_con_un_control_dentro_de_una_letra_queda_en_nfc(cliente):
    """Un control entre la «e» y su tilde combinante: al quitarlo, la letra se compone (R-36)."""
    pieza_id = _subir(cliente, "nota.txt", b"Pe\x01\xcc\x81a").json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")

    assert cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["cuerpo"] == "Péa"
