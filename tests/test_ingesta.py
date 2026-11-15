"""Ingresar una Pieza suelta: la Pieza y su trabajo se crean juntos o no se crean.

Estos tests no arrancan el trabajador (`app_sin_trabajador`): así los trabajos quedan
`encolado` y lo único que confirma en la base es el pedido que se está mirando.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import chain, repeat
from typing import Any, cast
from urllib.parse import urlencode
from uuid import uuid4

import anyio
import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from historia_viva import originales
from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.dominio.tipos import EtiquetaHallada
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.ingesta import AporteRechazado, AporteRecibido, Ingesta
from historia_viva.procesamiento.cola import Cola
from historia_viva.video import FfprobeNoDisponible, MetadatosVideo, VideoIlegible, interpretar
from test_fichas import divergencias
from test_video import GRABADO, INYECCION
from validacion import ADMINISTRADOR_TECNICO, JORGE, MARIA, PDF

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")


def archivos(raiz):
    """Lo que quedó en disco: los directorios vacíos no cuentan."""
    return sorted(ruta for ruta in raiz.rglob("*") if ruta.is_file())


@pytest.fixture
def cliente(url_app, capa_modelos, directorio_originales) -> Iterator[TestClient]:
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )
    cliente = TestClient(app, headers=CABECERAS)
    entrar(cliente, MARIA)
    yield cliente
    app.state.bd.cerrar()


def _subir(cliente, nombre, contenido):
    return cliente.post(
        "/api/piezas", files={"archivo": (nombre, contenido, "application/octet-stream")}
    )


@pytest.mark.historia(54)
def test_una_falla_al_encolar_no_deja_pieza_ni_original(
    cliente, directorio_originales, monkeypatch
):
    def encolar_que_falla(*args, **kwargs):
        raise RuntimeError("falla inyectada al encolar")

    monkeypatch.setattr(Cola, "encolar", encolar_que_falla)

    with pytest.raises(RuntimeError, match="falla inyectada"):
        _subir(cliente, "acta.pdf", PDF)

    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.historia(231)
def test_una_falla_despues_del_reemplazo_no_deja_original_ni_marca(
    cliente, directorio_originales, monkeypatch
):
    """El `os.replace` ya ocurrió y falla el `fsync` del directorio: todavía no hay `COMMIT`,
    así que el original y su marca se borran ya, sin esperar al barrido."""
    sincronizar = originales._sincronizar_directorio

    def sincronizar_que_falla(directorio):
        if directorio == directorio_originales:
            raise OSError(errno.EIO, "falla inyectada en el fsync del directorio")
        sincronizar(directorio)

    monkeypatch.setattr(originales, "_sincronizar_directorio", sincronizar_que_falla)

    with pytest.raises(OSError, match="falla inyectada"):
        _subir(cliente, "acta.pdf", PDF)

    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.historia(231)
def test_un_corte_durante_el_commit_no_borra_el_original_de_una_pieza_confirmada(
    cliente, monkeypatch
):
    """El servidor confirmó pero la respuesta del `COMMIT` no llegó: no se sabe si la Pieza
    existe, así que el original no se toca (el barrido decide después, con la base a la vista).
    """
    encolar, commit = Cola.encolar, psycopg.Connection.commit
    cortar = []

    def encolar_y_preparar_el_corte(self, *args, **kwargs):
        cortar.append(True)
        return encolar(self, *args, **kwargs)

    def commit_que_se_corta(self):
        commit(self)
        if cortar:
            cortar.clear()
            raise psycopg.OperationalError("se cortó la conexión")

    monkeypatch.setattr(Cola, "encolar", encolar_y_preparar_el_corte)
    monkeypatch.setattr(psycopg.Connection, "commit", commit_que_se_corta)

    assert _subir(cliente, "acta.pdf", PDF).status_code == 503

    [pieza] = cliente.get("/api/piezas").json()["piezas"]
    assert cliente.get(f"/api/piezas/{pieza['pieza_id']}/original").content == PDF


def _trabajos(url_bd):
    with psycopg.connect(url_bd) as conexion:
        return conexion.execute(
            "SELECT estado, count(*) FROM acervo.trabajo_procesamiento GROUP BY estado"
        ).fetchall()


def _duplicada(respuesta, pieza_id):
    assert respuesta.status_code == 409
    problema = respuesta.json()
    assert problema["type"] == "/errores/pieza-duplicada"
    assert problema["title"] == "La Pieza ya está cargada"
    # Sin Lotes todavía, la Pieza previa siempre es suelta.
    assert {k: problema[k] for k in ("pieza_id", "lote_id", "estado")} == {
        "pieza_id": pieza_id,
        "lote_id": None,
        "estado": "Recibida",
    }


@pytest.mark.historia(80)
def test_el_mismo_archivo_suelto_dos_veces_es_un_duplicado_de_la_primera_pieza(
    cliente, url_bd, directorio_originales
):
    primera = _subir(cliente, "acta.pdf", PDF).json()["pieza_id"]

    _duplicada(_subir(cliente, "otra copia del acta.pdf", PDF), primera)

    assert [p["pieza_id"] for p in cliente.get("/api/piezas").json()["piezas"]] == [primera]
    assert archivos(directorio_originales) == [directorio_originales / primera]
    assert _trabajos(url_bd) == [("encolado", 1)]


@pytest.mark.historia(80)
def test_de_dos_subidas_simultaneas_del_mismo_archivo_una_crea_y_la_otra_es_duplicado(
    cliente, url_bd, directorio_originales, monkeypatch
):
    """Las dos buscan la huella antes de que ninguna cree su Pieza: la segunda choca con
    `pieza_huella_unica` y responde lo mismo que un duplicado común (DEC-106)."""
    buscar = ModuloFichasPostgres.buscar_por_huella
    las_dos_buscaron = threading.Barrier(2)

    def buscar_a_la_vez(self, tx, sha256):
        previa = buscar(self, tx, sha256)
        if previa is None:
            las_dos_buscaron.wait(timeout=10)
        return previa

    monkeypatch.setattr(ModuloFichasPostgres, "buscar_por_huella", buscar_a_la_vez)

    with ThreadPoolExecutor(2) as hilos:
        creada, repetida = sorted(
            hilos.map(lambda _: _subir(cliente, "acta.pdf", PDF), range(2)),
            key=lambda respuesta: respuesta.status_code,
        )

    assert creada.status_code == 201
    _duplicada(repetida, creada.json()["pieza_id"])
    assert archivos(directorio_originales) == [directorio_originales / creada.json()["pieza_id"]]
    assert _trabajos(url_bd) == [("encolado", 1)]


@pytest.mark.rf("RF-02")
def test_50_archivos_de_a_uno_con_uno_invalido_dejan_49_piezas_y_49_trabajos(cliente, url_bd):
    respuestas = [
        _subir(cliente, "notas.pdf", b"no soy un PDF")
        if i == 23
        else _subir(cliente, f"acta-{i}.pdf", PDF + str(i).encode())
        for i in range(50)
    ]

    assert [r.status_code for r in respuestas].count(201) == 49
    assert respuestas[23].status_code == 415
    piezas = cliente.get("/api/piezas").json()["piezas"]
    assert [p["estado"] for p in piezas] == ["Recibida"] * 49
    assert _trabajos(url_bd) == [("encolado", 49)]


@pytest.mark.rf("RF-02")
def test_el_nombre_se_guarda_en_nfc_y_sin_caracteres_de_control_y_el_original_no_se_toca(
    cliente,
):
    # Crudo: `httpx` codificaría el carácter de control antes de mandarlo.
    # «é» descompuesta con un BEL entre la «e» y su tilde, y otro BEL suelto.
    nombre = "Re\x07\u0301gimen\x07 de la FIE.pdf"
    cuerpo = (
        f'--frontera\r\nContent-Disposition: form-data; name="archivo"; filename="{nombre}"'
        "\r\n\r\n"
    ).encode() + PDF + b"\r\n--frontera--\r\n"

    subida = cliente.post(
        "/api/piezas",
        content=cuerpo,
        headers={"content-type": "multipart/form-data; boundary=frontera"},
    )

    assert subida.status_code == 201
    assert subida.json()["nombre_archivo"] == "R\u00e9gimen de la FIE.pdf"
    assert cliente.get(f"{subida.headers['location']}/original").content == PDF


# --- Video (092, ADR-0005): nace Pendiente con Ficha v1 `sistema`, sin cola ni modelos. ---

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm\x42\x87\x81\x04" + b"\x00" * 64
MKV = b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\x82\x88matroska\x42\x87\x81\x04" + b"\x00" * 64
AVI = b"RIFF\x00\x10\x00\x00AVI LIST" + b"\x00" * 64
TRES_HORAS_S = 3 * 60 * 60


class AnalizadorFalso:
    """El `video.analizar` que inyecta `composicion`: registra con qu\u00e9 se lo llam\u00f3."""

    def __init__(self) -> None:
        self.llamadas: list[str] = []
        self.resultado: MetadatosVideo | Exception = interpretar(
            json.dumps(GRABADO).encode(), "mov"
        )

    def __call__(self, ruta, contenedor):
        self.llamadas.append(contenedor)
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return replace(self.resultado, contenedor=contenedor)


@pytest.fixture
def analizador() -> AnalizadorFalso:
    return AnalizadorFalso()


@pytest.fixture
def cliente_video(url_app, capa_modelos, directorio_originales, analizador, request):
    """Con `@pytest.mark.parametrize("cliente_video", [None], indirect=True)`, sin `ffprobe`."""
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        analizar_video=getattr(request, "param", analizador),
    )
    cliente = TestClient(app, headers=CABECERAS)
    entrar(cliente, MARIA)
    yield cliente
    app.state.bd.cerrar()


def _subir_video(cliente, nombre, contenido):
    return cliente.post("/api/piezas/video", params={"nombre": nombre}, content=contenido)


def _metadata_original(url_bd, pieza_id):
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT metadata_original FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
        ).fetchone()
    assert fila is not None
    return fila[0]


@pytest.mark.historia(101)
@pytest.mark.historia(103)
def test_un_video_chico_nace_pendiente_con_ficha_de_sistema_datos_tecnicos_y_sin_trabajo(
    cliente_video, analizador, capa_modelos, url_bd
):
    subida = _subir_video(cliente_video, "entrevista-rector.mp4", MP4)

    assert subida.status_code == 201
    pieza = subida.json()
    assert subida.headers["location"] == f"/api/piezas/{pieza['pieza_id']}"
    assert {k: pieza[k] for k in ("estado", "clase_original", "requiere_analisis_humano")} == {
        "estado": "Pendiente",
        "clase_original": "video",
        "requiere_analisis_humano": True,
    }
    assert pieza["nombre_archivo"] == "entrevista-rector.mp4"
    assert pieza["huella"] == hashlib.sha256(MP4).hexdigest()
    assert pieza["lote_id"] is None
    ficha = cliente_video.get(f"/api/piezas/{pieza['pieza_id']}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (1, "sistema")
    assert (ficha["cuerpo"], ficha["frontmatter"]["resumen"]) == ("", None)
    datos = _metadata_original(url_bd, pieza["pieza_id"])
    assert (datos["duracion_s"], datos["video"]["codec"]) == (754.32, "h264")
    assert (datos["video"]["ancho"], datos["video"]["alto"]) == (1280, 720)
    assert datos["creado_segun_archivo"] == "2019-08-14T10:31:07+00:00"
    assert _trabajos(url_bd) == []
    # Ning\u00fan modelo recibe el video, sus datos ni su nombre (ADR-0005).
    assert capa_modelos.invocaciones == []
    assert analizador.llamadas == ["mov"]
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


@pytest.mark.historia(103)
def test_el_texto_inyectado_en_las_etiquetas_del_video_no_aparece_en_ninguna_respuesta(
    cliente_video, url_bd
):
    # `GRABADO` trae el texto inyectado en `title`, `comment` y `handler_name` (093).
    subida = _subir_video(cliente_video, "acto.mp4", MP4)
    pieza_id = subida.json()["pieza_id"]

    respuestas = [
        subida,
        cliente_video.get(f"/api/piezas/{pieza_id}"),
        cliente_video.get(f"/api/piezas/{pieza_id}/ficha"),
    ]
    assert [r.status_code for r in respuestas] == [201, 200, 200]
    # Un fragmento sin tildes: con `\u00e1` escapado la comparación pasaría sola.
    inyectado = "instrucciones anteriores"
    assert inyectado in INYECCION
    assert all(inyectado not in r.text for r in respuestas)
    assert inyectado not in json.dumps(_metadata_original(url_bd, pieza_id))


@pytest.mark.historia(101)
def test_la_base_rechaza_un_trabajo_de_procesamiento_para_un_video(cliente_video, url_bd):
    pieza_id = _subir_video(cliente_video, "acto.mp4", MP4).json()["pieza_id"]

    with psycopg.connect(url_bd) as conexion, pytest.raises(
        psycopg.errors.CheckViolation, match="trabajo_sin_plan"
    ):
        conexion.execute(
            "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, estado,"
            " tiempo_maximo_ms, configuracion_efectiva) VALUES (%s, 'procesamiento',"
            " 'encolado', 1000, '{}')",
            (pieza_id,),
        )


@pytest.mark.rf("RF-50")
@pytest.mark.historia(69)
@pytest.mark.historia(284)
@pytest.mark.historia(99)
def test_un_video_de_un_lote_nace_con_propietario_y_carreras_en_su_v1(cliente_video, url_bd):
    """La v1 `sistema` del video se escribe al nacer: tiene que leer las Carreras ya copiadas."""
    lote = cliente_video.post(
        "/api/lotes",
        json={
            "nombre": "Entrevistas",
            "origen": "Archivo SEU",
            "fecha_inicio": "1990-01-01",
            "precision_fecha": "anio",
            "fecha_fin": "1990-12-31",
            "propietario": "Biblioteca FIE",
            "carreras": ["ing-civil"],
        },
    ).json()["lote"]

    pieza_id = cliente_video.post(
        "/api/piezas/video",
        params={"nombre": "acto.mp4", "lote_id": lote["lote_id"]},
        content=MP4,
    ).json()["pieza_id"]

    frontmatter = cliente_video.get(f"/api/piezas/{pieza_id}/ficha").json()["frontmatter"]
    assert (frontmatter["propietario"], frontmatter["carreras"]) == ("Biblioteca FIE", ["ing-civil"])
    # También el origen y la Fecha del Lote, y sin pasar por la cola (094, historia 99).
    assert (frontmatter["origen"], frontmatter["precision_fecha"]) == ("Archivo SEU", "anio")
    assert (frontmatter["fecha_inicio"], frontmatter["fecha_fin"]) == ("1990-01-01", "1990-12-31")
    assert _trabajos(url_bd) == []
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


@pytest.mark.historia(100)
@pytest.mark.parametrize(
    ("contenido", "tipo_mime", "contenedor"),
    [
        pytest.param(MP4, "video/mp4", "mov", id="mp4"),
        pytest.param(MOV, "video/quicktime", "mov", id="mov"),
        pytest.param(WEBM, "video/webm", "matroska,webm", id="webm"),
    ],
)
def test_mp4_mov_y_webm_entran_y_el_demuxer_sale_de_la_firma(
    cliente_video, analizador, contenido, tipo_mime, contenedor
):
    # El nombre dice otra cosa: decide el contenido.
    subida = _subir_video(cliente_video, "video.avi", contenido)

    assert subida.status_code == 201
    assert cliente_video.get(subida.headers["location"]).json()["tipo_mime"] == tipo_mime
    assert analizador.llamadas == [contenedor]


@pytest.mark.historia(100)
@pytest.mark.parametrize("contenido", [AVI, MKV], ids=["avi", "mkv"])
@pytest.mark.parametrize("puerta", ["video", "archivo"])
def test_avi_y_mkv_responden_415_con_convertilo_a_mp4(
    cliente_video, analizador, directorio_originales, contenido, puerta
):
    if puerta == "video":
        rechazo = _subir_video(cliente_video, "acto.mp4", contenido)
    else:
        rechazo = _subir(cliente_video, "acto.mp4", contenido)

    assert rechazo.status_code == 415
    problema = rechazo.json()
    assert problema["type"] == "/errores/tipo-no-admitido"
    assert problema["motivo"] == "contenedor-no-admitido"
    assert "convertilo a MP4" in problema["detail"]
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []
    assert analizador.llamadas == []


VIDEOS = ["video/mp4", "video/quicktime", "video/webm"]


@pytest.mark.historia(100)
def test_por_la_puerta_del_video_solo_entra_video(cliente_video, directorio_originales):
    rechazo = _subir_video(cliente_video, "acta.mp4", PDF)

    assert rechazo.status_code == 415
    assert rechazo.json()["type"] == "/errores/tipo-no-admitido"
    assert rechazo.json()["admitidos"] == VIDEOS
    assert archivos(directorio_originales) == []


@pytest.mark.historia(100)
def test_un_video_por_la_puerta_de_archivos_responde_415_y_va_por_la_carga_de_video(
    cliente_video, analizador, directorio_originales, url_bd
):
    rechazo = _subir(cliente_video, "acto.mp4", MP4)

    assert rechazo.status_code == 415
    problema = rechazo.json()
    assert problema["type"] == "/errores/tipo-no-admitido"
    assert "carga de video" in problema["detail"]
    assert not set(VIDEOS) & set(problema["admitidos"])
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []
    assert analizador.llamadas == []
    assert _trabajos(url_bd) == []


@pytest.mark.historia(102)
def test_un_video_de_mas_de_3_horas_responde_422_sin_dejar_pieza_ni_temporal(
    cliente_video, analizador, directorio_originales
):
    analizador.resultado = replace(analizador.resultado, duracion_s=TRES_HORAS_S + 0.5)

    rechazo = _subir_video(cliente_video, "congreso.mp4", MP4)

    assert rechazo.status_code == 422
    problema = rechazo.json()
    assert problema["type"] == "/errores/video-demasiado-largo"
    assert problema["tope_segundos"] == TRES_HORAS_S
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.historia(102)
def test_exactamente_3_horas_entra(cliente_video, analizador):
    analizador.resultado = replace(analizador.resultado, duracion_s=float(TRES_HORAS_S))

    assert _subir_video(cliente_video, "congreso.mp4", MP4).status_code == 201


@pytest.mark.historia(102)
def test_un_video_que_ffprobe_no_lee_responde_422_archivo_ilegible(
    cliente_video, analizador, directorio_originales
):
    analizador.resultado = VideoIlegible("El archivo no tiene un flujo de video.")

    rechazo = _subir_video(cliente_video, "truncado.mp4", MP4)

    assert rechazo.status_code == 422
    assert rechazo.json()["type"] == "/errores/archivo-ilegible"
    assert rechazo.json()["motivo"] == "sin-video"
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.historia(103)
@pytest.mark.parametrize("cliente_video", [None], indirect=True)
def test_sin_ffprobe_la_subida_de_video_responde_503_y_la_salud_queda_degradada(
    cliente_video, directorio_originales, monkeypatch
):
    # Sin `ffprobe` no importa el disco: el 503 va antes que el 507.
    monkeypatch.setattr(originales.AlmacenDeOriginales, "espacio_libre", lambda self: 0)

    rechazo = _subir_video(cliente_video, "acto.mp4", MP4)

    assert rechazo.status_code == 503
    # Mismo `type` que la base caída; el `motivo` le dice a la pantalla que reintentar no sirve.
    assert (rechazo.json()["type"], rechazo.json()["motivo"]) == (
        "/errores/servicio-no-disponible",
        "sin-ffprobe",
    )
    assert archivos(directorio_originales) == []
    salud = entrar(cliente_video, ADMINISTRADOR_TECNICO).get("/api/salud").json()
    assert (salud["estado"], salud["video"]) == ("degradado", "sin_ffprobe")


@pytest.mark.historia(103)
def test_si_ffprobe_no_se_puede_ejecutar_la_subida_responde_503(
    cliente_video, analizador, directorio_originales
):
    analizador.resultado = FfprobeNoDisponible("No se pudo ejecutar ffprobe.")

    rechazo = _subir_video(cliente_video, "acto.mp4", MP4)

    assert rechazo.status_code == 503
    # Mismo `type` que la base caída; el `motivo` le dice a la pantalla que reintentar no sirve.
    assert (rechazo.json()["type"], rechazo.json()["motivo"]) == (
        "/errores/servicio-no-disponible",
        "sin-ffprobe",
    )
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.historia(103)
def test_con_ffprobe_la_salud_informa_el_video_ok(cliente_video):
    salud = entrar(cliente_video, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert (salud["estado"], salud["video"]) == ("ok", "ok")


# --- Subidas en vivo (094): el cuerpo llega de a pedazos, como desde el navegador. ---

MiB = 1024 * 1024

CORTE = None
"""Lo que manda `SubidaEnVivo` cuando quien sube cierra la conexión a mitad del cuerpo."""


class SubidaEnVivo:
    """Un pedido por ASGI cuyo cuerpo llega cuando el test lo manda (`mandar`). El `TestClient`
    entrega el cuerpo entero de una vez: no deja tener dos subidas a medias ni cortar una."""

    def __init__(self, cliente, ruta, *, params=None, cabeceras=(), cuerpo=None):
        """Con `cuerpo`, los pedazos salen de ese iterable a medida que el servidor los pide."""
        self._app = cliente.app
        self._cuerpo_fijo = None if cuerpo is None else iter(cuerpo)
        cookies = "; ".join(f"{k}={v}" for k, v in cliente.cookies.items())
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": ruta,
            "raw_path": ruta.encode(),
            "query_string": urlencode(params or {}).encode(),
            "headers": [
                (b"host", b"testserver"),
                *((k.lower().encode(), v.encode()) for k, v in CABECERAS.items()),
                (b"cookie", cookies.encode()),
                *((k.encode(), v.encode()) for k, v in cabeceras),
            ],
        }
        self._pedazos: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.leidos = 0
        """Cuántas veces el servidor pidió cuerpo."""
        self.leyendo = asyncio.Event()
        self.estado: int | None = None
        self.cabeceras: dict[str, str] = {}
        self._cuerpo = bytearray()

    def mandar(self, *pedazos):
        """`b""` termina el cuerpo; `CORTE` cierra la conexión."""
        for pedazo in pedazos:
            self._pedazos.put_nowait(pedazo)

    def json(self):
        return json.loads(self._cuerpo)

    async def correr(self):
        async def receive():
            if self._cuerpo_fijo is None:
                pedazo = await self._pedazos.get()
            else:
                pedazo = next(self._cuerpo_fijo, b"")
            if pedazo is CORTE:
                return {"type": "http.disconnect"}
            self.leidos += 1
            self.leyendo.set()
            return {"type": "http.request", "body": pedazo, "more_body": pedazo != b""}

        async def send(mensaje):
            if mensaje["type"] == "http.response.start":
                self.estado = mensaje["status"]
                self.cabeceras = {k.decode(): v.decode() for k, v in mensaje["headers"]}
            elif mensaje["type"] == "http.response.body":
                self._cuerpo += mensaje.get("body", b"")

        await self._app(self._scope, receive, send)


def _video_en_vivo(cliente, nombre):
    return SubidaEnVivo(cliente, "/api/piezas/video", params={"nombre": nombre})


def _video_distinto(i):
    """Otro MP4 por cada `i`: el mismo original dos veces sería un duplicado."""
    return MP4 + bytes([i]) * 1024


@pytest.mark.rnf("RNF-22")
@pytest.mark.historia(90)
def test_tercera_subida_de_video_en_paralelo_429_demasiadas_subidas(
    cliente_video, url_app, directorio_originales
):
    jorge = entrar(TestClient(cliente_video.app, headers=CABECERAS), JORGE)
    frontera = "frontera"
    multipart = (
        f'--{frontera}\r\nContent-Disposition: form-data; name="archivo"; filename="acta.pdf"'
        "\r\n\r\n"
    ).encode() + PDF + f"\r\n--{frontera}--\r\n".encode()

    async def escena():
        primera, segunda = (_video_en_vivo(cliente_video, f"{n}.mp4") for n in "ab")
        async with anyio.create_task_group() as tareas:
            for i, subida in enumerate((primera, segunda)):
                subida.mandar(_video_distinto(i)[:64])
                tareas.start_soon(subida.correr)
            await primera.leyendo.wait()
            await segunda.leyendo.wait()
            # Las dos de María están a mitad del cuerpo.
            tercera = _video_en_vivo(cliente_video, "c.mp4")
            tercera.mandar(_video_distinto(2), b"")
            await tercera.correr()
            # Las demás clases no tienen tope del servidor; el tope es por persona.
            documento = SubidaEnVivo(
                cliente_video,
                "/api/piezas",
                cabeceras=[("content-type", f"multipart/form-data; boundary={frontera}")],
            )
            documento.mandar(multipart, b"")
            await documento.correr()
            de_jorge = _video_en_vivo(jorge, "d.mp4")
            de_jorge.mandar(_video_distinto(3), b"")
            await de_jorge.correr()
            for i, subida in enumerate((primera, segunda)):
                subida.mandar(_video_distinto(i)[64:], b"")
        return primera, segunda, tercera, documento, de_jorge

    primera, segunda, tercera, documento, de_jorge = anyio.run(escena)

    assert tercera.estado == 429
    problema = tercera.json()
    assert (problema["type"], problema["maximo"]) == ("/errores/demasiadas-subidas", 2)
    assert int(tercera.cabeceras["retry-after"]) > 0
    assert tercera.leidos == 0  # se rechazó sin leer un byte del cuerpo
    assert [primera.estado, segunda.estado, documento.estado, de_jorge.estado] == [201] * 4
    # Terminadas las dos, el cupo volvió: la siguiente entra.
    assert _subir_video(cliente_video, "c.mp4", _video_distinto(2)).status_code == 201


@pytest.mark.rnf("RNF-22")
@pytest.mark.historia(90)
def test_el_cupo_de_subidas_se_libera_cuando_la_subida_falla(cliente_video, analizador):
    analizador.resultado = VideoIlegible("El archivo no tiene un flujo de video.")
    for i in range(3):
        assert _subir_video(cliente_video, "roto.mp4", _video_distinto(i)).status_code == 422
    assert _subir_video(cliente_video, "roto.mp4", PDF).status_code == 415

    analizador.resultado = AnalizadorFalso().resultado
    assert _subir_video(cliente_video, "sano.mp4", MP4).status_code == 201


@pytest.mark.rnf("RNF-22")
@pytest.mark.historia(99)
def test_un_corte_a_mitad_de_la_subida_no_deja_pieza_ni_original_y_libera_el_cupo(
    cliente_video, analizador, directorio_originales, url_bd, caplog
):
    subidas = [_video_en_vivo(cliente_video, f"{i}.mp4") for i in range(2)]

    async def escena():
        for i, subida in enumerate(subidas):
            # Más de un bloque: el temporal ya existe cuando llega el corte.
            subida.mandar(_video_distinto(i), b"\x00" * (3 * 1024 * 1024), CORTE)
        for subida in subidas:
            await subida.correr()

    with caplog.at_level(logging.INFO):
        anyio.run(escena)

    # Quien cortó ya no está: ni un 500 que nadie lee ni una traza de error en el log.
    assert [s.estado for s in subidas] == [None, None]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    assert analizador.llamadas == []
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []
    assert _trabajos(url_bd) == []
    # Los dos cortes devolvieron su cupo.
    assert _subir_video(cliente_video, "entero.mp4", MP4).status_code == 201


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(99)
def test_un_video_de_2_gib_mas_1_byte_responde_413_mientras_llega_sin_temporal(
    cliente_video, analizador, directorio_originales
):
    """Sin `Content-Length`: el tope lo controla el servidor contando lo que llega. Quien sube
    sigue mandando después del byte 2 GiB + 1, y el servidor no lo lee."""
    bloque = bytes(MiB)
    cuerpo = chain([MP4.ljust(MiB, b"\x00")], repeat(bloque, 2047), [b"\x00"], repeat(bloque, 8))
    subida = SubidaEnVivo(
        cliente_video, "/api/piezas/video", params={"nombre": "congreso.mp4"}, cuerpo=cuerpo
    )

    anyio.run(subida.correr)

    assert subida.estado == 413
    problema = subida.json()
    assert (problema["type"], problema["tope_bytes"]) == (
        "/errores/archivo-demasiado-grande",
        2 * 1024**3,
    )
    assert subida.leidos < 2049 + 8  # cortó en el byte que sobra, no al final del cuerpo
    assert analizador.llamadas == []
    assert cliente_video.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


# ------------------------------------------------- aporte con etiquetas de control (101) ---


class _BdCaida:
    def transaccion(self) -> Any:
        raise RuntimeError("sin base")


@pytest.mark.historia(191)
@pytest.mark.rf("RF-10")
def test_la_verificacion_de_etiquetas_llega_inyectada_y_rechaza_antes_de_tocar_la_base(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # `ingesta` no importa `chequeo_inyeccion` (`test_arquitectura`): decide lo que le inyectan,
    # aunque el texto sea inocente. Sin base, el 422 sale igual y el evento queda en el log.
    revisadas: list[Mapping[str, str]] = []

    def etiquetas(secciones: Mapping[str, str]) -> list[EtiquetaHallada]:
        revisadas.append(secciones)
        # Líneas del original: la 1 es el título, la 2 la línea en blanco, la 3 la primera
        # del cuerpo.
        return [EtiquetaHallada("[INST]", "aporte", 1), EtiquetaHallada("[INST]", "aporte", 5)]

    nada = cast(Any, None)
    ingesta = Ingesta(
        bd=cast(Any, _BdCaida()), originales=nada, fichas=nada, cola=nada, configuracion=nada,
        auditoria=nada, lotes=nada, avisar=lambda: None, analizar_video=None,
        etiquetas_de_control=etiquetas,
    )
    aporte = AporteRecibido(
        titulo="Gacetilla", cuerpo="Texto inocente.", tipo_contenido="gacetilla", canal=None,
        piezas_fuente=(), fragmentos_fuente=(), fecha_publicacion=None, url_publicacion=None,
    )

    with pytest.raises(AporteRechazado) as rechazo:
        ingesta.recibir_aporte(
            agente_id=uuid4(), clave="clave-0001", aporte=aporte,
            por=Actor(TipoDeActor.AGENTE, "hv_0123abcd"),
        )

    # El texto entero, como se guarda: una etiqueta partida entre título y cuerpo no se escapa.
    assert revisadas == [{"aporte": "Gacetilla\n\nTexto inocente."}]
    assert rechazo.value.etiquetas == (
        EtiquetaHallada("[INST]", "titulo", 1), EtiquetaHallada("[INST]", "cuerpo", 3)
    )
    assert "`aporte.rechazo` no quedó." in caplog.text
