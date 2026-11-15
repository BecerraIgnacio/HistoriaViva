"""El reproductor de video de Validar (095): el original se sirve por tramos, con `If-Range` y
`?descarga=1`, y el detalle trae los datos técnicos que leyó `ffprobe` (DEC-16, ADR-0005).

No hay `ffprobe` en la PC: el análisis es el doble de `test_ingesta`.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from test_ingesta import MP4, AnalizadorFalso
from test_originales import CUERPO, _con_sesion, _subir
from validacion import EDICION_COMPLETA, MARIA, abrir, mediciones

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool")

GiB = 1024**3


@pytest.fixture
def cliente_video(url_app, capa_modelos, directorio_originales):
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        analizar_video=AnalizadorFalso(),
    )
    cliente = TestClient(app, headers=CABECERAS)
    entrar(cliente, MARIA)
    yield cliente
    app.state.bd.cerrar()


def _video(cliente) -> str:
    subida = cliente.post("/api/piezas/video", params={"nombre": "rector.mp4"}, content=MP4)
    assert subida.status_code == 201, subida.text
    return str(subida.json()["pieza_id"])


@pytest.mark.rf("RF-11")
@pytest.mark.historia(114)
def test_el_video_de_2_gib_salta_a_cualquier_minuto_con_range(
    cliente_video, directorio_originales
):
    pieza_id = _video(cliente_video)
    # El original de 2 GiB de 094, sin escribir 2 GiB: el archivo se estira en disco (ceros).
    en_disco = directorio_originales / pieza_id
    with en_disco.open("r+b") as archivo:
        archivo.truncate(2 * GiB)
    url = f"/api/piezas/{pieza_id}/original"

    try:
        primero = cliente_video.get(url, headers={"Range": "bytes=0-1023"})
        # Un salto: el reproductor pide desde el byte del minuto elegido.
        medio = cliente_video.get(url, headers={"Range": f"bytes={GiB}-{GiB + 1023}"})
        final = cliente_video.get(url, headers={"Range": "bytes=-512"})
    finally:
        en_disco.unlink()

    assert primero.status_code == 206
    assert primero.headers["content-range"] == f"bytes 0-1023/{2 * GiB}"
    assert primero.headers["content-type"] == "video/mp4"
    assert primero.headers["accept-ranges"] == "bytes"
    assert (medio.status_code, len(medio.content)) == (206, 1024)
    assert medio.headers["content-range"] == f"bytes {GiB}-{GiB + 1023}/{2 * GiB}"
    assert final.headers["content-range"] == f"bytes {2 * GiB - 512}-{2 * GiB - 1}/{2 * GiB}"
    assert final.content == b"\x00" * 512


@pytest.fixture
def ruta_del_pdf(cliente):
    subida = _subir(_con_sesion(cliente), "acta.pdf", CUERPO)
    return f"{subida.headers['location']}/original"


@pytest.mark.rf("RF-11")
@pytest.mark.historia(114)
def test_if_range_con_el_etag_sirve_el_tramo_y_con_otro_el_original_entero(
    cliente, ruta_del_pdf
):
    etag = f'"{hashlib.sha256(CUERPO).hexdigest()}"'

    vigente = cliente.get(ruta_del_pdf, headers={"Range": "bytes=10-19", "If-Range": etag})
    cambiado = cliente.get(ruta_del_pdf, headers={"Range": "bytes=10-19", "If-Range": '"otro"'})
    # Una fecha en `If-Range` no se compara: sin `Last-Modified`, va el original entero.
    fecha = cliente.get(
        ruta_del_pdf,
        headers={"Range": "bytes=10-19", "If-Range": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )

    assert (vigente.status_code, vigente.content) == (206, CUERPO[10:20])
    assert (cambiado.status_code, cambiado.content) == (200, CUERPO)
    assert "content-range" not in cambiado.headers
    assert (fecha.status_code, fecha.content) == (200, CUERPO)


@pytest.mark.rf("RF-11")
@pytest.mark.historia(116)
def test_descarga_1_manda_el_original_como_adjunto(cliente, ruta_del_pdf):
    en_linea = cliente.get(ruta_del_pdf)
    adjunto = cliente.get(ruta_del_pdf, params={"descarga": "1"})

    assert en_linea.headers["content-disposition"].startswith("inline; ")
    assert adjunto.headers["content-disposition"] == (
        "attachment; filename=\"acta.pdf\"; filename*=UTF-8''acta.pdf"
    )
    assert adjunto.content == CUERPO


@pytest.mark.rf("RF-39")
@pytest.mark.historia(114)
def test_el_detalle_del_video_trae_sus_datos_tecnicos_de_la_lista_blanca(cliente_video):
    pieza_id = _video(cliente_video)

    datos = cliente_video.get(f"/api/piezas/{pieza_id}").json()["metadata_original"]

    assert (datos["duracion_s"], datos["video"]["codec"], datos["audio"]["codec"]) == (
        754.32,
        "h264",
        "aac",
    )
    assert (datos["video"]["ancho"], datos["video"]["alto"]) == (1280, 720)
    # La fecha es una pista para el campo Fecha, no un valor de la Ficha (DEC-16).
    assert datos["creado_segun_archivo"] == "2019-08-14T10:31:07+00:00"
    assert datos["reproducible_probable"] is True
    ficha = cliente_video.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert ficha["frontmatter"]["fecha_inicio"] is None


@pytest.mark.rf("RF-41")
@pytest.mark.historia(207)
def test_la_medicion_de_un_video_dice_que_es_video_y_cuanto_llevo_analizarlo(cliente_video):
    pieza_id = _video(cliente_video)
    ficha = cliente_video.get(f"/api/piezas/{pieza_id}/ficha").json()
    # Nadie propuso nada (ADR-0005): la Pieza espera análisis humano.
    assert ficha["requiere_analisis_humano"] is True
    apertura = abrir(cliente_video, pieza_id)

    firmada = cliente_video.put(
        f"/api/piezas/{pieza_id}/ficha",
        json=EDICION_COMPLETA | {"version_base": ficha["version"], "medicion_id": apertura["medicion_id"]},
    )

    assert firmada.status_code == 200, firmada.text
    # Contar las mediciones de clase `video` dimensiona ese trabajo; `duracion_s` dice cuánto llevó.
    (medicion,) = mediciones(cliente_video)
    assert (medicion["pieza_id"], medicion["clase_original"]) == (pieza_id, "video")
    assert medicion["estado_final"] == "Validada"
    assert medicion["duracion_s"] is not None and medicion["duracion_s"] >= 0
