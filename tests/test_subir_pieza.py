"""Subir una Pieza: el original queda guardado íntegro y la Pieza aparece Recibida en la lista.

Los tests que miran a la Pieza todavía `Recibida` empiezan con `capa_modelos.demorar()`: el
procesamiento arranca solo apenas el archivo llega (issue 03), y sin colgar la capa de
modelos la Pieza pasaría a `Pendiente` en cualquier momento.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import psycopg
import pytest
from aplicacion import entrar

pytestmark = [
    pytest.mark.rf("RF-02", "RF-04", "RF-06"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

MARIA = "11111111-1111-4111-8111-111111111111"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def test_subir_un_documento_deja_la_pieza_recibida_y_firmada_por_quien_la_subio(
    cliente, url_bd, capa_modelos
):
    capa_modelos.demorar()
    entrar(cliente, MARIA)

    respuesta = cliente.post(
        "/api/piezas", files={"archivo": ("acta-fundacion.pdf", PDF, "application/pdf")}
    )

    assert respuesta.status_code == 201
    pieza = respuesta.json()
    assert pieza["estado"] == "Recibida"
    assert pieza["nombre_archivo"] == "acta-fundacion.pdf"
    assert pieza["creada_el"]
    assert respuesta.headers["location"] == f"/api/piezas/{pieza['pieza_id']}"

    # `creada_por` no viaja en la API: se verifica en la base, que es donde queda la firma.
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT estado, creada_por::text FROM acervo.pieza WHERE pieza_id = %s",
            (pieza["pieza_id"],),
        ).fetchone()
    assert fila == ("Recibida", MARIA)


def _docx() -> bytes:
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as comprimido:
        comprimido.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        comprimido.writestr("word/document.xml", "<w:document/>")
    return memoria.getvalue()


def _con_sesion(cliente):
    entrar(cliente, MARIA)
    return cliente


def _subir(cliente, nombre, contenido, tipo="application/octet-stream"):
    return cliente.post("/api/piezas", files={"archivo": (nombre, contenido, tipo)})


def test_el_original_se_devuelve_integro_con_su_mime_real_y_su_nombre(cliente):
    _con_sesion(cliente)
    subida = _subir(cliente, "acta fundación.pdf", PDF, "application/octet-stream")

    respuesta = cliente.get(f"{subida.headers['location']}/original")

    assert respuesta.status_code == 200
    assert respuesta.content == PDF
    assert respuesta.headers["content-type"] == "application/pdf"
    assert respuesta.headers["content-disposition"] == (
        "inline; filename=\"acta fundacion.pdf\"; filename*=UTF-8''acta%20fundaci%C3%B3n.pdf"
    )


@pytest.mark.parametrize(
    ("nombre", "contenido", "tipo_mime"),
    [
        ("patio.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "image/jpeg"),
        ("sello.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png"),
        ("plano.tif", b"II*\x00\x08\x00\x00\x00", "image/tiff"),
        ("acta.pdf", PDF, "application/pdf"),
        (
            "convenio.docx",
            _docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ],
    # El zip lleva la hora en que se armó: sin ids fijos, cada proceso de xdist ve otro test.
    ids=["patio.jpg", "sello.png", "plano.tif", "acta.pdf", "convenio.docx"],
)
def test_se_aceptan_fotografias_pdf_y_word(cliente, nombre, contenido, tipo_mime):
    _con_sesion(cliente)

    subida = _subir(cliente, nombre, contenido)

    assert subida.status_code == 201
    detalle = cliente.get(subida.headers["location"]).json()
    assert detalle["tipo_mime"] == tipo_mime
    assert detalle["tamano_bytes"] == len(contenido)


@pytest.mark.parametrize(
    ("nombre", "contenido", "tipo"),
    [
        ("pagina.html", b"<html>hola</html>", "text/html"),
        ("disfrazado.pdf", b"MZ\x90\x00", "application/pdf"),
    ],
)
def test_otro_tipo_de_archivo_responde_415_con_el_texto_de_la_pantalla(
    cliente, directorio_originales, nombre, contenido, tipo
):
    _con_sesion(cliente)

    respuesta = _subir(cliente, nombre, contenido, tipo)

    assert respuesta.status_code == 415
    assert respuesta.headers["content-type"].startswith("application/problem+json")
    problema = respuesta.json()
    assert problema["type"] == "/errores/tipo-no-admitido"
    assert problema["detail"] == (
        "Ese archivo no se puede procesar. "
        "Historia Viva acepta fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv) y texto (.txt)."
    )
    # Un formato desconocido no tiene `motivo`: la lista del catálogo es cerrada.
    assert problema["admitidos"] == [
        "image/jpeg",
        "image/png",
        "image/tiff",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "text/plain",
    ]
    assert "motivo" not in problema
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert not directorio_originales.exists() or not any(directorio_originales.iterdir())


@pytest.mark.historia(289)
@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param(b"", id="vacio"),
        # Sin sus caracteres de control (R-36) quedaría con el cuerpo vacío.
        pytest.param(b"\x01\x02\r\n", id="solo-caracteres-de-control"),
        pytest.param("\u200b⁠\r\n".encode(), id="solo-caracteres-de-formato"),
    ],
)
def test_un_txt_sin_texto_responde_415_diciendo_que_esta_vacio(
    cliente, directorio_originales, contenido
):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "nota.txt", contenido, "text/plain")

    assert respuesta.status_code == 415
    problema = respuesta.json()
    assert problema["type"] == "/errores/tipo-no-admitido"
    assert problema["detail"] == "El archivo de texto está vacío: no trae nada para leer."
    assert "motivo" not in problema
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert not directorio_originales.exists() or not any(directorio_originales.iterdir())


def test_un_archivo_de_exactamente_40_mb_se_acepta(cliente):
    _con_sesion(cliente)
    contenido = PDF + b"\x00" * (40 * 1024 * 1024 - len(PDF))

    assert _subir(cliente, "justo.pdf", contenido).status_code == 201


FALTANTES_DE_UNA_RECIBIDA = [
    "tipo",
    "tipo_actividad",
    "fecha_inicio",
    "fecha_fin",
    "precision_fecha",
    "origen",
    "propietario",
    "resumen",
]


def test_la_lista_trae_lo_subido_de_lo_mas_reciente_a_lo_mas_viejo(cliente, capa_modelos):
    capa_modelos.demorar()
    _con_sesion(cliente)
    primera = _subir(cliente, "acta.pdf", PDF).json()
    segunda = _subir(cliente, "convenio.docx", _docx()).json()

    respuesta = cliente.get("/api/piezas")

    assert respuesta.status_code == 200
    piezas = respuesta.json()["piezas"]
    assert [pieza["pieza_id"] for pieza in piezas] == [
        segunda["pieza_id"],
        primera["pieza_id"],
    ]
    assert piezas[1] == {
        "pieza_id": primera["pieza_id"],
        "estado": "Recibida",
        "tipo": None,
        "resumen": None,
        "nombre_archivo": "acta.pdf",
        "creada_el": primera["creada_el"],
        "actualizada_el": piezas[1]["actualizada_el"],
        "campos_faltantes": FALTANTES_DE_UNA_RECIBIDA,
        "lote_id": None,
        "requiere_analisis_humano": False,
        "fallida": False,
        "no_indexada": False,
        "alerta_inyeccion": False,
        "creada_por": MARIA,
    }


def test_la_lista_se_acota_por_estado(cliente, capa_modelos):
    capa_modelos.demorar()
    _con_sesion(cliente)
    _subir(cliente, "acta.pdf", PDF)

    assert len(cliente.get("/api/piezas?estado=Recibida").json()["piezas"]) == 1
    assert cliente.get("/api/piezas?estado=Pendiente&estado=Incompleta").json() == {
        "piezas": []
    }


def test_el_detalle_trae_los_datos_del_original(cliente, capa_modelos):
    capa_modelos.demorar()
    _con_sesion(cliente)
    subida = _subir(cliente, "acta.pdf", PDF)

    detalle = cliente.get(subida.headers["location"]).json()

    assert detalle == {
        "pieza_id": subida.json()["pieza_id"],
        "estado": "Recibida",
        "tipo": None,
        "resumen": None,
        "nombre_archivo": "acta.pdf",
        "tipo_mime": "application/pdf",
        "tamano_bytes": len(PDF),
        "tipo_actividad": None,
        "propietario": None,
        "autor": None,
        "carreras": [],
        "creada_el": subida.json()["creada_el"],
        "actualizada_el": detalle["actualizada_el"],
        "campos_faltantes": FALTANTES_DE_UNA_RECIBIDA,
        "alerta_inyeccion": False,
        "via_ingreso": "archivo",
        "error_procesamiento": None,
        "metadata_original": None,
        "lote_id": None,
        # El trabajador puede haberla tomado o no: depende de cuándo corre.
        "estado_procesamiento": detalle["estado_procesamiento"],
        "procesamiento_pausado": False,
        "en_edicion_por": None,
        "aporte": None,
    }
    assert detalle["estado_procesamiento"] in {"encolado", "en_curso"}


@pytest.mark.parametrize("sufijo", ["", "/original"])
def test_una_pieza_que_no_existe_responde_404(cliente, sufijo):
    _con_sesion(cliente)

    respuesta = cliente.get(f"/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9{sufijo}")

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/pieza-no-encontrada"


@pytest.mark.parametrize(
    "ruta", ["/api/piezas", "/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/original"]
)
def test_consultar_piezas_sin_sesion_responde_401(cliente, ruta):
    assert cliente.get(ruta).status_code == 401


def test_la_pantalla_de_carga_muestra_la_politica_de_admision_de_la_guia(cliente):
    # Política de admisión (spec §2.7, sin historia propia: la cubre RF-02 del módulo). La misma
    # regla en la pantalla y en la guía de una página (117): si una cambia, cambia la otra.
    regla = "material disciplinario, datos de salud ni listados con sueldos"
    guia = Path(__file__).resolve().parents[1] / "docs" / "guia-de-una-pagina.html"

    pantalla = cliente.get("/").text

    assert 'id="politica-admision"' in pantalla
    assert regla in " ".join(pantalla.split())
    assert regla in " ".join(guia.read_text(encoding="utf-8").split())
