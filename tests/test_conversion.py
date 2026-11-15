"""Convertir lo que hoy se rechaza: `.doc`, `.xls` y HEIC (153, DEC-41 revisada).

El flujo entero corre con la capa real y el motor falso, y con un conversor falso inyectado: la
suite no depende de LibreOffice. El conversor real se prueba aparte: su HEIC con pillow-heif,
sus topes y su perfil siempre, y su `.doc` solo si LibreOffice está instalado.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import uuid4

import docx
import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from aplicacion import en_marcha, entrar
from capa_modelos_falsa import TEXTO_VISIBLE_FIJO, CapaModelosFalsa
from espera import esperar_error_procesamiento, esperar_estado
from fastapi.testclient import TestClient
from lxml import etree
from ollama_falso import OllamaFalso
from PIL import Image
from test_originales import (
    MACRO_EN_LOS_TIPOS,
    WORD,
    _cfb,
    _ooxml,
    _relacion_externa,
)
from test_planillas import TRES_HOJAS, libro
from validacion import MARIA

from historia_viva import conversion
from historia_viva.capa_modelos.local import capa_para
from historia_viva.dominio.originales import DOC, HEIC, JPEG, XLS, ClaseDeOriginal
from historia_viva.dominio.tipos import OriginalGuardado
from historia_viva.originales import (
    POLITICAS,
    AlmacenDeOriginales,
    ArchivoDemasiadoGrande,
    ConversionFallida,
    PoliticaDeAdmision,
)

pytestmark = [
    pytest.mark.rf("RF-04"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

TEXTO_DEL_ACTA = "Acta de la primera promoción de ingenieros."

DOC_VIEJO = _cfb("Root Entry", "WordDocument")
XLS_VIEJO = _cfb("Root Entry", "Workbook")


def _docx(texto: str) -> bytes:
    documento = docx.Document()
    documento.add_paragraph(texto)
    memoria = io.BytesIO()
    documento.save(memoria)
    return memoria.getvalue()


def _jpeg() -> bytes:
    memoria = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(memoria, format="JPEG")
    return memoria.getvalue()


def _heic() -> bytes:
    pillow_heif = pytest.importorskip("pillow_heif")
    memoria = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (64, 48), "red")).save(memoria, format="HEIF")
    return memoria.getvalue()


class ConversorFalso:
    """Deja el convertido que se le dio y anota qué recibió."""

    def __init__(self, convertidos: dict[str, bytes]) -> None:
        self.formatos = frozenset(convertidos)
        self._convertidos = convertidos
        self.recibidos: list[tuple[bytes, str]] = []

    def convertir(self, origen: Path, tipo_mime: str, destino: Path) -> None:
        self.recibidos.append((origen.read_bytes(), tipo_mime))
        destino.write_bytes(self._convertidos[tipo_mime])


def _convertidos() -> dict[str, bytes]:
    return {DOC: _docx(TEXTO_DEL_ACTA), XLS: libro(TRES_HOJAS), HEIC: _jpeg()}


@contextmanager
def _app(
    url_bd: str,
    originales: Path,
    conversor: ConversorFalso | None,
    capa: CapaModelosFalsa | None = None,
) -> Iterator[TestClient]:
    """Con la capa real y el motor falso: el Word y el libro se leen de verdad. Una foto pide
    Tesseract con `spa`, que no está en toda PC: con `capa`, la falsa."""
    motor = OllamaFalso()
    with en_marcha(
        url_bd,
        capa_para=capa.para if capa else partial(capa_para, motores=motor.motores()),
        validar=capa.validar if capa else motor.validar,
        directorio_originales=originales,
        conversor=conversor,
    ) as cliente:
        cliente.motor = motor  # type: ignore[attr-defined]
        yield entrar(cliente, MARIA)


def _subir(cliente: TestClient, nombre: str, contenido: bytes):
    return cliente.post(
        "/api/piezas", files={"archivo": (nombre, contenido, "application/octet-stream")}
    )


# ── Entran convertidos ──────────────────────────────────────────────────────────────────────


@pytest.mark.rf("RF-05")
@pytest.mark.rf("RF-06")
@pytest.mark.parametrize(
    ("nombre", "tipo_mime"),
    [("acta.doc", DOC), ("notas.xls", XLS), ("foto.heic", HEIC)],
    ids=["doc", "xls", "heic"],
)
def test_entra_como_pieza_y_se_procesa_como_su_convertido(
    url_bd, tmp_path, nombre, tipo_mime
):
    contenido = {DOC: DOC_VIEJO, XLS: XLS_VIEJO, HEIC: _heic()}[tipo_mime]
    conversor = ConversorFalso(_convertidos())
    capa = CapaModelosFalsa() if tipo_mime == HEIC else None
    with _app(url_bd, tmp_path, conversor, capa) as cliente:
        respuesta = _subir(cliente, nombre, contenido)
        assert respuesta.status_code == 201, respuesta.text
        pieza_id = respuesta.json()["pieza_id"]

        detalle = esperar_estado(cliente, pieza_id, "Pendiente")
        cuerpo = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["cuerpo"]
        descargado = cliente.get(f"/api/piezas/{pieza_id}/original?descarga=1")
        visor = cliente.get(f"/api/piezas/{pieza_id}/original")
        planilla = cliente.get(f"/api/piezas/{pieza_id}/planilla")

    # El original se guarda tal como se subió y es lo que se descarga (RF-06).
    assert detalle["tipo_mime"] == tipo_mime
    assert descargado.content == contenido
    assert descargado.headers["content-type"] == tipo_mime
    assert descargado.headers["content-disposition"].startswith("attachment")
    # El conversor recibió el original, en el procesamiento y no en la subida (RF-05).
    assert conversor.recibidos == [(contenido, tipo_mime)]
    if tipo_mime == DOC:
        assert TEXTO_DEL_ACTA in cuerpo
    elif tipo_mime == XLS:
        assert "Ingeniería" in cuerpo
        assert [hoja["nombre"] for hoja in planilla.json()["hojas"]][:1] == ["Ingresantes"]
    else:
        assert capa is not None and cuerpo.endswith(TEXTO_VISIBLE_FIJO)
        # El modelo vio el JPEG, y el visor muestra el JPEG: el navegador no abre un HEIC.
        describir = capa.invocaciones[0]
        assert describir.bloques[0].contenido == f"sha256:{hashlib.sha256(_jpeg()).hexdigest()}"
        assert visor.headers["content-type"] == JPEG
        assert visor.content == _jpeg()
        assert visor.headers["content-disposition"].startswith('inline; filename="foto.jpg"')
        assert visor.headers["etag"] != descargado.headers["etag"]


# ── Siguen rechazados ───────────────────────────────────────────────────────────────────────


@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    ("nombre", "contenido", "que_hacer"),
    [
        pytest.param(
            "acta.doc",
            _cfb("Root Entry", "WordDocument", "Macros", "VBA", "_VBA_PROJECT"),
            "Guardalo como .docx, sin macros",
            id="doc-con-vba",
        ),
        pytest.param(
            "notas.xls",
            _cfb("Root Entry", "Workbook", "_VBA_PROJECT_CUR", "VBA", "_VBA_PROJECT"),
            "Guardala como .xlsx, sin macros",
            id="xls-con-vba",
        ),
        # Los nombres OLE2 no distinguen mayúsculas: Office los lee igual.
        pytest.param(
            "acta.doc",
            _cfb("Root Entry", "WORDDOCUMENT", "Macros", "VBA", "_vba_project"),
            "Guardalo como .docx, sin macros",
            id="doc-con-vba-en-minusculas",
        ),
        pytest.param(
            "acta.docm",
            _ooxml(WORD | {"word/vbaProject.bin": bytes(10)}),
            "Guardalo como .docx, sin macros",
            id="docm",
        ),
        pytest.param(
            "notas.xlsm",
            _ooxml({"[Content_Types].xml": MACRO_EN_LOS_TIPOS, "xl/workbook.xml": "<w/>"}),
            "Guardala como .xlsx, sin macros",
            id="xlsm",
        ),
        pytest.param(
            "notas.xlsb", _ooxml({"xl/workbook.bin": bytes(10)}), "Guardala como .xlsx", id="xlsb"
        ),
    ],
)
def test_con_conversor_lo_que_trae_macros_sigue_dando_415(
    url_bd, tmp_path, nombre, contenido, que_hacer
):
    conversor = ConversorFalso(_convertidos())
    with _app(url_bd, tmp_path, conversor) as cliente:
        respuesta = _subir(cliente, nombre, contenido)
        piezas = cliente.get("/api/piezas").json()

    assert respuesta.status_code == 415
    assert que_hacer in respuesta.json()["detail"]
    assert piezas == {"piezas": []}
    assert conversor.recibidos == []


@pytest.mark.parametrize(
    ("nombre", "tipo_mime", "nombra", "como"),
    [
        ("acta.doc", DOC, ".doc", "Guardalo como .docx"),
        ("notas.xls", XLS, ".xls", "Guardala como .xlsx"),
        ("foto.heic", HEIC, "HEIC", "Exportala como JPEG"),
    ],
    ids=["doc", "xls", "heic"],
)
def test_sin_conversor_es_415_con_el_formato_y_como_convertirlo(
    url_bd, tmp_path, nombre, tipo_mime, nombra, como
):
    contenido = {DOC: DOC_VIEJO, XLS: XLS_VIEJO, HEIC: _heic()}[tipo_mime]
    sin_ese = ConversorFalso({m: b"" for m in _convertidos() if m != tipo_mime})
    with _app(url_bd, tmp_path, sin_ese) as cliente:
        respuesta = _subir(cliente, nombre, contenido)

    assert respuesta.status_code == 415
    detalle = respuesta.json()["detail"]
    assert nombra in detalle and como in detalle and "puede convertir" in detalle
    assert not [ruta for ruta in tmp_path.rglob("*") if ruta.is_file()]


# ── La guarda sobre lo convertido ───────────────────────────────────────────────────────────


@pytest.mark.rnf("RNF-20")
def test_un_convertido_con_partes_externas_no_se_lee_y_el_procesamiento_falla(url_bd, tmp_path):
    vinculado = _ooxml(
        WORD | {"word/_rels/settings.xml.rels": _relacion_externa("attachedTemplate")}
    )
    conversor = ConversorFalso(_convertidos() | {DOC: vinculado})
    with _app(url_bd, tmp_path, conversor) as cliente:
        pieza_id = _subir(cliente, "acta.doc", DOC_VIEJO).json()["pieza_id"]
        detalle = esperar_error_procesamiento(cliente, pieza_id)
        chats = cliente.motor.chats()  # type: ignore[attr-defined]

    assert detalle["estado"] == "Recibida"
    assert "vinculado desde afuera" in detalle["error_procesamiento"]
    assert chats == []  # nada del convertido llegó al motor


def _guardado(raiz: Path, contenido: bytes, tipo_mime: str) -> OriginalGuardado:
    pieza_id = uuid4()
    (raiz / str(pieza_id)).write_bytes(contenido)
    return OriginalGuardado(
        identificador=f"local://originales/{pieza_id}",
        nombre_archivo="foto.heic",
        tipo_mime=tipo_mime,
        tamano_bytes=len(contenido),
        creado_el=datetime.now(UTC),
        huella="abc",
    )


def _con_tope(clase: ClaseDeOriginal, tope: int) -> dict[ClaseDeOriginal, PoliticaDeAdmision]:
    return dict(POLITICAS) | {clase: PoliticaDeAdmision(tope_bytes=tope)}


@pytest.mark.rnf("RNF-20")
def test_el_tope_de_bytes_vale_antes_de_convertir(tmp_path):
    conversor = ConversorFalso(_convertidos())
    planilla_chica = _con_tope(ClaseDeOriginal.PLANILLA, len(XLS_VIEJO) - 1)
    almacen = AlmacenDeOriginales(tmp_path, planilla_chica, conversor=conversor)

    # El `.xls` se escribe con el tope del Word y se mide con el de la planilla al reconocerlo.
    with pytest.raises(ArchivoDemasiadoGrande) as rechazo:
        almacen.guardar_en_streaming(io.BytesIO(XLS_VIEJO))
    with pytest.raises(ArchivoDemasiadoGrande):
        almacen.leer_para_procesar(_guardado(tmp_path, XLS_VIEJO, XLS))

    assert rechazo.value.clase is ClaseDeOriginal.PLANILLA
    assert conversor.recibidos == []


@pytest.mark.rnf("RNF-20")
def test_el_tope_de_bytes_vale_despues_de_convertir(tmp_path):
    conversor = ConversorFalso({HEIC: _jpeg()})
    imagen_chica = _con_tope(ClaseDeOriginal.IMAGEN, len(_jpeg()) - 1)
    almacen = AlmacenDeOriginales(tmp_path, imagen_chica, conversor=conversor)
    original = _guardado(tmp_path, b"HEIC chico", HEIC)

    with pytest.raises(ArchivoDemasiadoGrande):
        almacen.leer_para_procesar(original)

    assert conversor.recibidos == [(b"HEIC chico", HEIC)]
    assert not (tmp_path / ".convertidos").exists()


@pytest.mark.rnf("RNF-20")
def test_un_jpeg_convertido_que_pasa_el_tope_de_pixeles_no_se_lee(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 64 * 48 - 1)
    almacen = AlmacenDeOriginales(tmp_path, conversor=ConversorFalso({HEIC: _jpeg()}))

    with pytest.raises(ConversionFallida, match="píxeles"):
        almacen.leer_para_procesar(_guardado(tmp_path, b"HEIC", HEIC))


@pytest.mark.rf("RF-06")
def test_el_derivado_lo_sirve_el_visor_y_descartar_el_original_lo_borra(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path, conversor=ConversorFalso({HEIC: _jpeg()}))
    original = _guardado(tmp_path, b"HEIC", HEIC)

    assert almacen.leer_para_procesar(original) == (_jpeg(), JPEG)
    visor = almacen.abrir(original, convertido=True)
    assert visor.cabeceras["Content-Type"] == JPEG
    assert b"".join(visor.bloques) == _jpeg()
    # La réplica a Drive y la descarga piden el original, no el derivado.
    assert b"".join(almacen.abrir(original).bloques) == b"HEIC"
    assert almacen.guardados() == {original.identificador}

    almacen.descartar(original.identificador)

    assert not [ruta for ruta in tmp_path.rglob("*") if ruta.is_file()]


@pytest.mark.rnf("RNF-20")
def test_la_miniatura_de_un_doc_sale_del_derivado_sin_convertir_en_web(tmp_path):
    # SEG-14: `web` no corre LibreOffice por un GET; lee el derivado que dejó el procesamiento.
    conversor = ConversorFalso(_convertidos())
    almacen = AlmacenDeOriginales(tmp_path, conversor=conversor)
    original = _guardado(tmp_path, DOC_VIEJO, DOC)

    with pytest.raises(ConversionFallida, match="procesamiento"):
        almacen.leer_convertido(original)
    procesado = almacen.leer_para_procesar(original)

    assert almacen.leer_convertido(original) == procesado
    assert len(conversor.recibidos) == 1
    # El visor sigue sirviendo el `.doc`: el derivado del Word no es para mostrar.
    assert b"".join(almacen.abrir(original, convertido=True).bloques) == DOC_VIEJO
    almacen.descartar(original.identificador)
    assert not [ruta for ruta in tmp_path.rglob("*") if ruta.is_file()]


def test_sin_conversor_el_procesamiento_falla_con_que_hacer(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)

    with pytest.raises(ConversionFallida, match="Guardalo como .docx"):
        almacen.leer_para_procesar(_guardado(tmp_path, DOC_VIEJO, DOC))


# ── El conversor real ───────────────────────────────────────────────────────────────────────


@pytest.mark.rf("RF-04")
def test_el_heic_se_convierte_a_jpeg_en_un_proceso_aparte(tmp_path):
    conversor = conversion.ConversorAislado()
    if HEIC not in conversor.formatos:
        pytest.skip("pillow-heif o prlimit no están instalados")
    origen = tmp_path / "original.heic"
    origen.write_bytes(_heic())

    conversor.convertir(origen, HEIC, tmp_path / "convertido.jpg")

    with Image.open(tmp_path / "convertido.jpg") as foto:
        assert (foto.format, foto.size) == ("JPEG", (64, 48))


@pytest.mark.rnf("RNF-20")
def test_la_conversion_que_no_termina_se_mata_en_su_tope(tmp_path):
    comienzo = time.monotonic()

    with pytest.raises(ConversionFallida, match="no terminó"):
        conversion.correr_aislado(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout_s=0.5
        )

    assert time.monotonic() - comienzo < 10


@pytest.mark.rnf("RNF-20")
def test_el_hijo_corre_sin_red_ni_el_entorno_del_servidor(tmp_path, monkeypatch):
    monkeypatch.setenv("HV_CLAVE_APP", "secreta")
    entorno_limpio = (
        "import os, sys; sys.exit(0 if os.environ['https_proxy'] == 'http://127.0.0.1:9'"
        " and not any(v.startswith('HV_') for v in os.environ) else 1)"
    )
    conectar = "import socket; socket.create_connection(('127.0.0.1', 80), timeout=1)"

    conversion.correr_aislado([sys.executable, "-I", "-c", entorno_limpio], tmp_path, timeout_s=30)
    with pytest.raises(ConversionFallida):
        conversion.correr_aislado(
            [sys.executable, "-I", "-c", conversion._SIN_RED_EN_PYTHON + conectar],
            tmp_path,
            timeout_s=30,
        )


@pytest.mark.rnf("RNF-20")
def test_el_perfil_de_libreoffice_apaga_las_macros_y_manda_la_red_a_un_puerto_muerto():
    ajustes = {
        (item.get("{http://openoffice.org/2001/registry}path"), prop.get(
            "{http://openoffice.org/2001/registry}name"
        )): prop.findtext("value")
        for item in etree.fromstring(conversion.perfil_de_libreoffice().encode())
        for prop in item
    }

    scripting = "/org.openoffice.Office.Common/Security/Scripting"
    red = "/org.openoffice.Inet/Settings"
    assert ajustes[(scripting, "DisableMacrosExecution")] == "true"
    assert ajustes[(scripting, "MacroSecurityLevel")] == "3"
    assert ajustes[(red, "ooInetProxyType")] == "2"
    for protocolo in ("HTTP", "HTTPS", "FTP"):
        assert ajustes[(red, f"ooInet{protocolo}ProxyName")] == "127.0.0.1"
        assert ajustes[(red, f"ooInet{protocolo}ProxyPort")] == "9"
    # Vínculos sin actualizar al abrir: «nunca» es 2 en Writer y 1 en Calc (officecfg).
    assert ajustes[("/org.openoffice.Office.Writer/Content/Update", "Link")] == "2"
    assert ajustes[("/org.openoffice.Office.Writer/Content/Update", "Field")] == "false"
    assert ajustes[("/org.openoffice.Office.Calc/Content/Update", "Link")] == "1"


def test_sin_libreoffice_no_ofrece_doc_ni_xls(monkeypatch):
    monkeypatch.setattr(conversion.shutil, "which", lambda nombre: None)

    formatos = conversion.ConversorAislado().formatos

    assert DOC not in formatos and XLS not in formatos


@pytest.mark.rf("RF-04")
def test_un_doc_real_se_convierte_con_libreoffice(tmp_path):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        pytest.skip("LibreOffice no está instalado en esta PC")
    conversor = conversion.ConversorAislado()
    (tmp_path / "docx").mkdir()
    (tmp_path / "docx" / "acta.docx").write_bytes(_docx(TEXTO_DEL_ACTA))
    # El `.doc` de prueba lo arma el mismo LibreOffice.
    conversion.correr_aislado(
        [soffice, "--headless", "--convert-to", "doc", "--outdir", str(tmp_path / "docx"),
         str(tmp_path / "docx" / "acta.docx")],
        tmp_path / "docx",
        timeout_s=120,
    )  # fmt: skip
    (tmp_path / "originales").mkdir()
    almacen = AlmacenDeOriginales(tmp_path / "originales", conversor=conversor)
    original = _guardado(
        tmp_path / "originales", (tmp_path / "docx" / "acta.doc").read_bytes(), DOC
    )

    contenido, tipo_mime = almacen.leer_para_procesar(original)

    texto = "\n".join(p.text for p in docx.Document(io.BytesIO(contenido)).paragraphs)
    assert TEXTO_DEL_ACTA in texto and tipo_mime.endswith("document")


@pytest.mark.rnf("RNF-20")
def test_el_barrido_borra_la_conversion_de_un_trabajador_que_murio(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    vieja = tmp_path / ".subidas" / "tmpvieja"
    (vieja / "perfil").mkdir(parents=True)
    (vieja / "original.doc").write_bytes(DOC_VIEJO)
    en_curso = tmp_path / ".subidas" / "tmpencurso"
    en_curso.mkdir()
    hace_un_dia = time.time() - 86400
    os.utime(vieja, (hace_un_dia, hace_un_dia))

    assert almacen.barrer_huerfanos(set, mas_viejos_que=timedelta(hours=1)) == 1

    assert not vieja.exists() and en_curso.is_dir()


def _docx_con_includetext(testigo: Path) -> bytes:
    """Un Word con un campo INCLUDETEXT que trae el texto de `testigo` si se actualiza."""
    documento = docx.Document()
    parrafo = documento.add_paragraph(TEXTO_DEL_ACTA)._p
    instruccion = f'INCLUDETEXT "{testigo.as_posix()}"'
    for corrida in (
        '<w:fldChar w:fldCharType="begin"/>',
        f'<w:instrText xml:space="preserve"> {instruccion} </w:instrText>',
        '<w:fldChar w:fldCharType="separate"/>',
        "<w:t>sin incluir</w:t>",
        '<w:fldChar w:fldCharType="end"/>',
    ):
        parrafo.append(parse_xml(f"<w:r {nsdecls('w')}>{corrida}</w:r>"))
    memoria = io.BytesIO()
    documento.save(memoria)
    return memoria.getvalue()


def _convertir_aparte(conversor, origen: Path, tipo_mime: str, destino: str) -> bytes:
    aparte = origen.parent / f"aparte-{uuid4()}"
    aparte.mkdir()
    entrada = aparte / f"original{origen.suffix}"
    shutil.copy(origen, entrada)
    conversor.convertir(entrada, tipo_mime, aparte / destino)
    return (aparte / destino).read_bytes()


@pytest.mark.rnf("RNF-20")
def test_libreoffice_no_sigue_un_includetext_a_un_archivo_del_servidor(tmp_path, monkeypatch):
    """Corre solo donde hay LibreOffice (la imagen). El control prueba que el `.doc` de verdad
    trae el vínculo: con «actualizar siempre» el testigo entra; con el perfil, no."""
    conversor = conversion.ConversorAislado()
    if DOC not in conversor.formatos:
        pytest.skip("LibreOffice no está instalado en esta PC")
    testigo = tmp_path / "testigo.txt"
    testigo.write_text("SECRETO-DEL-TRABAJADOR", encoding="utf-8")
    con_campo = tmp_path / "acta.docx"
    con_campo.write_bytes(_docx_con_includetext(testigo))
    # El `.doc` lo arma el mismo LibreOffice, con el perfil endurecido: guarda el campo sin seguirlo.
    with monkeypatch.context() as parche:
        parche.setitem(conversion._FILTROS, DOC, "doc:MS Word 97")
        viejo = tmp_path / "acta.doc"
        viejo.write_bytes(_convertir_aparte(conversor, con_campo, DOC, "convertido.doc"))

    def texto(contenido: bytes) -> str:
        return "\n".join(p.text for p in docx.Document(io.BytesIO(contenido)).paragraphs)

    with monkeypatch.context() as parche:
        parche.setattr(
            conversion,
            "_AJUSTES",
            tuple(a for a in conversion._AJUSTES if "Content/Update" not in a[0])
            + (("/org.openoffice.Office.Writer/Content/Update", "Link", "0"),),
        )
        control = texto(_convertir_aparte(conversor, viejo, DOC, "convertido.docx"))
    convertido = texto(_convertir_aparte(conversor, viejo, DOC, "convertido.docx"))

    assert TEXTO_DEL_ACTA in convertido
    assert "SECRETO-DEL-TRABAJADOR" not in convertido
    if "SECRETO-DEL-TRABAJADOR" not in control:
        # Sin control no hay prueba: que el perfil no siga el vínculo no dice nada si este
        # LibreOffice no lo sigue nunca (pasa con el de Ubuntu de la CI).
        pytest.skip("este LibreOffice no sigue el vínculo ni con «actualizar siempre»: el .doc"
                    " de prueba no se puede armar")
