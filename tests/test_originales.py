"""Admitir, guardar y servir originales en streaming (`originales`, DEC-45, DEC-47, DEC-48).

Los tests de memoria y de lectura van directo sobre `AlmacenDeOriginales`: por HTTP el
`TestClient` entrega el cuerpo entero de una vez y no deja medir el streaming (spec,
`test_originales`). Lo demás va por HTTP contra Postgres real.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import random
import re
import shutil
import subprocess
import sys
import time
import tracemalloc
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from test_ingesta import MP4
from test_planillas import TRES_HOJAS, libro
from validacion import EDICION_COMPLETA

from historia_viva.dominio.originales import DOCX, ClaseDeOriginal
from historia_viva.dominio.tipos import OriginalGuardado
from historia_viva.originales import (
    BLOQUE_BYTES,
    MARGEN_LIBRE_BYTES,
    POLITICAS,
    AlmacenamientoInsuficiente,
    AlmacenDeOriginales,
    ArchivoDemasiadoGrande,
    ArchivoIlegible,
    TOPES_DE_ZIP,
    PoliticaDeAdmision,
    TipoNoAdmitido,
    TopesDeZip,
)

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")

MiB = 1024 * 1024
MARIA = "11111111-1111-4111-8111-111111111111"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def archivos(raiz):
    """Lo que quedó en disco: los directorios vacíos no cuentan."""
    return sorted(ruta for ruta in raiz.rglob("*") if ruta.is_file())


def en_disco(raiz, identificador):
    """Dónde queda un original confirmado: `local://originales/<pieza_id>` bajo la raíz."""
    return raiz / identificador.rsplit("/", 1)[1]


class FlujoSintetico:
    """Un archivo que se fabrica a medida que se lee: nunca está entero en memoria.

    Anota cuánto le pidió cada `read`, que es lo que la subida promete acotar.
    """

    def __init__(self, cabecera: bytes, total: int) -> None:
        self.pedidos: list[int] = []
        self._cabecera = cabecera
        self._restante = total
        self._entregado = 0

    def read(self, tamano: int = -1) -> bytes:
        self.pedidos.append(tamano)
        if tamano < 0:
            tamano = self._restante
        tamano = min(tamano, self._restante)
        inicio = self._entregado
        bloque = (self._cabecera[inicio : inicio + tamano]).ljust(tamano, b"\x00")
        self._entregado += tamano
        self._restante -= tamano
        return bloque


@pytest.mark.rnf("RNF-15")
@pytest.mark.historia(216)
def test_subir_40_mib_lee_de_a_1_mib_y_no_pasa_de_8_mib_de_memoria(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    flujo = FlujoSintetico(PDF, 40 * MiB)

    tracemalloc.start()
    try:
        temporal = almacen.guardar_en_streaming(flujo)
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert temporal.tamano_bytes == 40 * MiB
    assert all(0 < pedido <= MiB for pedido in flujo.pedidos)
    assert pico < 8 * MiB


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(99)
def test_seg_subida_en_streaming_con_tope(tmp_path):
    """Un video sintético de 2 GiB, casi todo ceros (disperso): se fabrica a medida que se lee."""
    almacen = AlmacenDeOriginales(tmp_path)
    flujo = FlujoSintetico(MP4, POLITICAS[ClaseDeOriginal.VIDEO].tope_bytes)
    referencia = hashlib.sha256()
    copia = FlujoSintetico(MP4, 2 * 1024**3)
    while bloque := copia.read(MiB):
        referencia.update(bloque)

    tracemalloc.start()
    try:
        temporal = almacen.guardar_en_streaming(flujo, solo_video=True)
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    try:
        assert (temporal.tamano_bytes, temporal.clase) == (2 * 1024**3, ClaseDeOriginal.VIDEO)
        assert temporal.sha256 == referencia.hexdigest()
        assert all(0 < pedido <= MiB for pedido in flujo.pedidos)
        assert pico < 8 * MiB
    finally:
        almacen.descartar(temporal)
    assert archivos(tmp_path) == []


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(11, 231)
def test_el_sha256_es_el_del_archivo_y_confirmar_lo_deja_entero_de_un_paso(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    contenido = PDF + os.urandom(3 * MiB)

    temporal = almacen.guardar_en_streaming(io.BytesIO(contenido))
    # Mismo volumen que los originales: `os.replace` es atómico solo dentro de un volumen.
    assert temporal.ruta.is_relative_to(tmp_path)
    identificador = almacen.confirmar(temporal, pieza_id=uuid4())

    guardado = en_disco(tmp_path, identificador).read_bytes()
    assert guardado == contenido
    assert temporal.sha256 == hashlib.sha256(guardado).hexdigest()
    assert not temporal.ruta.exists()


@pytest.mark.historia(231)
def test_confirmar_sincroniza_la_marca_y_el_directorio_despues_del_reemplazo(
    tmp_path, monkeypatch
):
    """Sin el `fsync` del directorio, un corte de luz después del `COMMIT` puede perder el
    renombre: la Pieza quedaría confirmada sin su original. La marca se asienta antes del
    reemplazo: sin ella, el barrido no puede borrar un original que quedó sin Pieza."""
    almacen = AlmacenDeOriginales(tmp_path)
    temporal = almacen.guardar_en_streaming(io.BytesIO(PDF))
    pasos = []
    # Windows no abre directorios: los descriptores de los directorios son falsos.
    DIRECTORIOS = {10**6: "fsync del directorio", 10**6 + 1: "fsync de .subidas"}
    descriptores = {tmp_path: 10**6, tmp_path / ".subidas": 10**6 + 1}
    reemplazar, abrir, sincronizar, cerrar = os.replace, os.open, os.fsync, os.close

    def reemplazar_anotando(origen, destino):
        pasos.append("os.replace")
        reemplazar(origen, destino)

    def abrir_anotando(ruta, *args, **kwargs):
        if ruta in descriptores:
            return descriptores[ruta]
        return abrir(ruta, *args, **kwargs)

    def sincronizar_anotando(descriptor):
        if descriptor in DIRECTORIOS:
            pasos.append(DIRECTORIOS[descriptor])
        else:
            sincronizar(descriptor)

    monkeypatch.setattr(os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(os, "replace", reemplazar_anotando)
    monkeypatch.setattr(os, "open", abrir_anotando)
    monkeypatch.setattr(os, "fsync", sincronizar_anotando)
    monkeypatch.setattr(os, "close", lambda d: None if d in DIRECTORIOS else cerrar(d))

    almacen.confirmar(temporal, pieza_id=uuid4())

    assert pasos == ["fsync de .subidas", "os.replace", "fsync del directorio"]


@pytest.mark.historia(231)
def test_descartar_dos_veces_no_es_un_error(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    temporal = almacen.guardar_en_streaming(io.BytesIO(PDF))
    almacen.descartar(temporal)
    almacen.descartar(temporal)
    confirmado = almacen.confirmar(
        almacen.guardar_en_streaming(io.BytesIO(PDF)), pieza_id=uuid4()
    )
    almacen.descartar(confirmado)
    almacen.descartar(confirmado)

    assert archivos(tmp_path) == []


def _envejecer(ruta, horas):
    hace = time.time() - horas * 3600
    os.utime(ruta, (hace, hace))


@pytest.mark.historia(231)
def test_el_barrido_borra_lo_viejo_sin_pieza_y_no_toca_una_subida_que_sigue_escribiendo(
    tmp_path,
):
    almacen = AlmacenDeOriginales(tmp_path)

    def confirmado_sin_commit():
        return almacen.confirmar(almacen.guardar_en_streaming(io.BytesIO(PDF)), pieza_id=uuid4())

    registrado = confirmado_sin_commit()  # cortado entre el `COMMIT` y `asentar`
    huerfano = confirmado_sin_commit()  # cortado entre `os.replace` y el `COMMIT`
    abandonado = almacen.guardar_en_streaming(io.BytesIO(PDF)).ruta
    for viejo in archivos(tmp_path):
        _envejecer(viejo, horas=2)
    reciente = confirmado_sin_commit()
    barridos = []

    class SubidaLenta(FlujoSintetico):
        def read(self, tamano=-1):
            if len(self.pedidos) == 3:
                barridos.append(
                    almacen.barrer_huerfanos(
                        lambda: {registrado}, mas_viejos_que=timedelta(hours=1)
                    )
                )
            return super().read(tamano)

    en_curso = almacen.guardar_en_streaming(SubidaLenta(PDF, 5 * MiB))

    # El temporal abandonado y el original cortado; la marca vieja de `registrado` no cuenta.
    assert barridos == [2]
    assert archivos(tmp_path) == sorted(
        [
            en_disco(tmp_path, registrado),
            en_disco(tmp_path, reciente),
            tmp_path / ".subidas" / f"{en_disco(tmp_path, reciente).name}.confirmando",
            en_curso.ruta,
        ]
    )
    assert en_curso.ruta.stat().st_size == 5 * MiB
    assert not abandonado.exists() and not en_disco(tmp_path, huerfano).exists()


@pytest.mark.historia(231)
def test_el_barrido_no_toca_un_original_sin_pieza_que_no_viene_de_un_corte(tmp_path):
    """Una base vacía o restaurada más vieja que los originales deja originales sin Pieza.

    No son huérfanos de un corte entre `os.replace` y el `COMMIT`: son el único rastro de una
    Pieza que hay que volver a cargar, y restic conserva su `mtime`.
    """
    almacen = AlmacenDeOriginales(tmp_path)
    restaurado = almacen.confirmar(almacen.guardar_en_streaming(io.BytesIO(PDF)), pieza_id=uuid4())
    almacen.asentar(restaurado)
    copiado_de_un_respaldo = tmp_path / str(uuid4())
    copiado_de_un_respaldo.write_bytes(PDF)
    for viejo in archivos(tmp_path):
        _envejecer(viejo, horas=48)

    assert almacen.barrer_huerfanos(set, mas_viejos_que=timedelta(hours=1)) == 0
    assert archivos(tmp_path) == sorted([en_disco(tmp_path, restaurado), copiado_de_un_respaldo])


def _zip_con_nombre_utf8_invalido():
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as comprimido:
        comprimido.writestr("x", "hola")
    datos = bytearray(memoria.getvalue())
    central = datos.index(b"PK\x01\x02")
    datos[central + 8 : central + 10] = (0x800).to_bytes(2, "little")  # nombre en UTF-8
    datos[central + 46] = 0xFF  # ... que no es UTF-8
    return bytes(datos)


def _docx_con_el_directorio_danado():
    """Un Word con cada byte de su directorio central y su cierre pisado, de a uno."""
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as comprimido:
        comprimido.writestr("word/document.xml", "<w:document/>")
    datos = memoria.getvalue()
    for posicion in range(datos.index(b"PK\x01\x02"), len(datos)):
        for valor in (0x00, 0xFF):
            danado = bytearray(datos)
            danado[posicion] = valor
            yield bytes(danado)


@pytest.mark.rf("RF-04")
def test_un_zip_con_el_directorio_danado_es_un_archivo_ilegible(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    ilegibles = 0

    with pytest.raises(ArchivoIlegible) as rechazo:
        almacen.guardar_en_streaming(io.BytesIO(_zip_con_nombre_utf8_invalido()))
    assert rechazo.value.motivo == "zip-corrupto"
    for contenido in _docx_con_el_directorio_danado():
        try:
            almacen.descartar(almacen.guardar_en_streaming(io.BytesIO(contenido)))
        except ArchivoIlegible as ilegible:
            assert ilegible.motivo == "zip-corrupto"
            ilegibles += 1
        except TipoNoAdmitido as ajeno:
            # Un byte pisado en el nombre deja un ZIP sano sin `word/document.xml`; en las
            # banderas, un miembro cifrado.
            assert ajeno.motivo in {"contenedor-no-admitido", "cifrada"}

    assert ilegibles > 1
    assert archivos(tmp_path) == []


@pytest.mark.rf("RF-04")
def test_sin_content_length_el_disco_lleno_al_escribir_es_almacenamiento_insuficiente(
    tmp_path, monkeypatch
):
    almacen = AlmacenDeOriginales(tmp_path)

    def disco_lleno(_):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", disco_lleno)

    with pytest.raises(AlmacenamientoInsuficiente):
        almacen.guardar_en_streaming(io.BytesIO(PDF))
    assert archivos(tmp_path) == []


@pytest.mark.rf("RF-04")
def test_la_politica_por_clase_trae_los_topes_del_spec():
    assert {clase: politica.tope_bytes for clase, politica in POLITICAS.items()} == {
        ClaseDeOriginal.IMAGEN: 40 * MiB,
        ClaseDeOriginal.DOCUMENTO: 40 * MiB,
        ClaseDeOriginal.PLANILLA: 10_000_000,
        ClaseDeOriginal.TEXTO: 256 * 1024,
        ClaseDeOriginal.VIDEO: 2 * 1024**3,
    }


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
TOPES_CHICOS = {
    ClaseDeOriginal.IMAGEN: PoliticaDeAdmision(tope_bytes=1 * MiB),
    ClaseDeOriginal.DOCUMENTO: PoliticaDeAdmision(tope_bytes=3 * MiB),
}


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
def test_cada_clase_corta_en_su_tope_mientras_llegan_los_bytes(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path, TOPES_CHICOS)

    with pytest.raises(ArchivoDemasiadoGrande) as rechazo:
        almacen.guardar_en_streaming(FlujoSintetico(JPEG, 1 * MiB + 1))
    assert (rechazo.value.clase, rechazo.value.tope_bytes) == (ClaseDeOriginal.IMAGEN, MiB)
    assert archivos(tmp_path) == []

    # El mismo tamaño es un documento chico, y exactamente el tope se acepta.
    assert almacen.guardar_en_streaming(FlujoSintetico(PDF, 1 * MiB + 1)).clase == "documento"
    assert almacen.guardar_en_streaming(FlujoSintetico(JPEG, 1 * MiB)).clase == "imagen"


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
def test_lo_declarado_sobre_el_tope_se_rechaza_sin_escribir_nada(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path, TOPES_CHICOS)
    flujo = FlujoSintetico(PDF, 4 * MiB)

    with pytest.raises(ArchivoDemasiadoGrande):
        almacen.guardar_en_streaming(flujo, tamano_minimo=3 * MiB + 1)

    assert flujo.pedidos == [MiB]
    assert archivos(tmp_path) == []


# --- Por HTTP ------------------------------------------------------------------------------


def _con_sesion(cliente):
    entrar(cliente, MARIA)
    return cliente


def _subir(cliente, nombre, contenido, tipo="application/octet-stream"):
    return cliente.post("/api/piezas", files={"archivo": (nombre, contenido, tipo)})


def _zip():
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as comprimido:
        comprimido.writestr("fotos/leeme.txt", "no soy un Word")
    return memoria.getvalue()


FRONTERA = "frontera-de-prueba"


def _multipart(nombre, contenido):
    return (
        f"--{FRONTERA}\r\nContent-Disposition: form-data; name=\"archivo\"; "
        f"filename=\"{nombre}\"\r\nContent-Type: application/pdf\r\n\r\n"
    ).encode() + contenido + f"\r\n--{FRONTERA}--\r\n".encode()


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(10)
def test_40_mib_y_un_byte_responde_413_sin_pieza_ni_temporal(cliente, directorio_originales):
    _con_sesion(cliente)
    contenido = PDF + b"\x00" * (40 * MiB + 1 - len(PDF))

    respuesta = _subir(cliente, "enorme.pdf", contenido, "application/pdf")

    assert respuesta.status_code == 413
    problema = respuesta.json()
    assert problema["type"] == "/errores/archivo-demasiado-grande"
    assert problema["tope_bytes"] == 40 * MiB
    assert problema["clase_original"] == "documento"
    assert problema["detail"] == "El archivo supera el tope de 40 MiB para la clase documento."
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.rnf("RNF-20")
def test_un_content_length_sobre_el_tope_responde_413_sin_leer_el_resto(
    cliente, directorio_originales
):
    _con_sesion(cliente)

    respuesta = cliente.post(
        "/api/piezas",
        content=_multipart("acta.pdf", PDF),
        headers={
            "content-type": f"multipart/form-data; boundary={FRONTERA}",
            "content-length": str(41 * MiB),
        },
    )

    assert respuesta.status_code == 413
    assert respuesta.json()["clase_original"] == "documento"
    assert archivos(directorio_originales) == []


@pytest.mark.rf("RF-04")
@pytest.mark.historia(9)
def test_un_zip_con_extension_pdf_se_clasifica_por_contenido_y_responde_415_con_motivo(
    cliente, directorio_originales
):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "acta.pdf", _zip(), "application/pdf")

    assert respuesta.status_code == 415
    problema = respuesta.json()
    assert problema["type"] == "/errores/tipo-no-admitido"
    assert problema["motivo"] == "contenedor-no-admitido"
    assert DOCX in problema["admitidos"]
    assert problema["detail"] == (
        "Es un archivo comprimido y no un documento de Word. "
        "Historia Viva acepta fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv) y texto (.txt)."
    )
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.rf("RF-04")
def test_un_zip_corrupto_responde_422_archivo_ilegible_sin_pieza_ni_temporal(
    cliente, directorio_originales
):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "acta.docx", _zip()[:-10], DOCX)

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/archivo-ilegible"
    assert problema["title"] == "No se pudo leer el archivo"
    assert problema["motivo"] == "zip-corrupto"
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.rf("RF-04")
@pytest.mark.historia(7)
def test_el_content_type_y_la_extension_del_cliente_no_deciden_la_clase(
    cliente, url_bd, capa_modelos
):
    capa_modelos.demorar()
    _con_sesion(cliente)

    subida = _subir(cliente, "acta.pdf", JPEG, "application/pdf")

    assert subida.status_code == 201
    assert cliente.get(subida.headers["location"]).json()["tipo_mime"] == "image/jpeg"
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT clase_original FROM acervo.pieza WHERE pieza_id = %s",
            (subida.json()["pieza_id"],),
        ).fetchone()
    assert fila == ("imagen",)


@pytest.mark.rf("RF-04")
@pytest.mark.historia(81)
def test_sin_espacio_libre_responde_507_antes_de_aceptar_bytes(
    cliente, directorio_originales, monkeypatch
):
    _con_sesion(cliente)
    disco = shutil.disk_usage(directorio_originales.parent)
    cuerpo = _multipart("acta.pdf", PDF)
    # Libre justo un byte menos que lo que pide la regla: el cuerpo más 2 GiB.
    libre = len(cuerpo) + MARGEN_LIBRE_BYTES - 1
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: type(disco)(disco.total, disco.total - libre, libre)
    )

    # El cuerpo ni siquiera es un multipart válido: si se leyera, la respuesta sería otra.
    respuesta = cliente.post(
        "/api/piezas",
        content=b"x" * len(cuerpo),
        headers={"content-type": f"multipart/form-data; boundary={FRONTERA}"},
    )

    assert respuesta.status_code == 507
    assert respuesta.json()["type"] == "/errores/almacenamiento-insuficiente"
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.historia(231)
def test_al_arrancar_se_barren_los_originales_viejos_que_quedaron_sin_pieza(
    url_app, capa_modelos, directorio_originales
):
    def arrancar():
        return TestClient(
            app_sin_trabajador(
                url_app,
                capa_para=capa_modelos.para,
                validar=capa_modelos.validar,
                directorio_originales=directorio_originales,
            ),
            headers=CABECERAS,
        )

    capa_modelos.demorar()
    with arrancar() as cliente:
        _con_sesion(cliente)
        registrado = directorio_originales / _subir(cliente, "acta.pdf", PDF).json()["pieza_id"]
        capa_modelos.liberar()
    assert archivos(directorio_originales) == [registrado]  # sin temporal ni marca
    # Un corte entre `os.replace` y el `COMMIT` deja un original sin Pieza.
    almacen = AlmacenDeOriginales(directorio_originales)
    huerfano = en_disco(
        directorio_originales,
        almacen.confirmar(almacen.guardar_en_streaming(io.BytesIO(PDF)), pieza_id=uuid4()),
    )
    # Un original restaurado de un respaldo más nuevo que la base tampoco tiene Pieza.
    restaurado = directorio_originales / str(uuid4())
    restaurado.write_bytes(PDF)
    for viejo in archivos(directorio_originales):
        _envejecer(viejo, horas=2)

    with arrancar():
        pass

    assert archivos(directorio_originales) == sorted([registrado, restaurado])
    assert not huerfano.exists()


def _pedido_crudo(cliente, cuerpo):
    return cliente.post(
        "/api/piezas",
        content=cuerpo,
        headers={"content-type": f"multipart/form-data; boundary={FRONTERA}"},
    )


@pytest.mark.rf("RF-04")
@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param(PDF, id="sin-frontera"),
        pytest.param(
            f"--{FRONTERA}\r\nContent-Disposition form-data\r\n\r\n".encode() + PDF,
            id="encabezado-invalido",
        ),
        pytest.param(
            f"--{FRONTERA}\r\nX-Relleno: {'a' * 5000}\r\n\r\n".encode() + PDF,
            id="encabezado-de-mas-de-4-kib",
        ),
    ],
)
def test_un_multipart_malformado_responde_422_sin_pieza_ni_temporal(
    cliente, directorio_originales, cuerpo
):
    _con_sesion(cliente)

    respuesta = _pedido_crudo(cliente, cuerpo)

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/pedido-invalido"
    assert problema["campo"] == "archivo"
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


def _ooxml(miembros, compresion=zipfile.ZIP_DEFLATED):
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", compresion) as comprimido:
        for nombre, contenido in miembros.items():
            comprimido.writestr(nombre, contenido)
    return memoria.getvalue()


def _con_ceros(principal, megabytes):
    """Un contenedor con su parte principal y un miembro de ceros escrito de a 1 MB."""
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as comprimido:
        comprimido.writestr(principal, "<raiz/>")
        with comprimido.open("media/ceros.bin", "w") as ceros:
            for _ in range(megabytes):
                ceros.write(bytes(1_000_000))
    return bytearray(memoria.getvalue())


def _docx_que_declara_1_kib_y_descomprime_200_mb():
    datos = _con_ceros("word/document.xml", 200)
    # El tamaño declarado del último miembro, en su encabezado local y en el directorio.
    for firma, desplazamiento in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        posicion = datos.rindex(firma) + desplazamiento
        datos[posicion : posicion + 4] = (1024).to_bytes(4, "little")
    return bytes(datos)


TIPOS_WORD = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
WORD = {"[Content_Types].xml": TIPOS_WORD, "word/document.xml": "<w:document/>"}


RELACIONES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _relacion_externa(tipo):
    """`tipo` es el nombre de una relación transitional o un URI entero."""
    uri = tipo if "://" in tipo else RELACIONES + tipo
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{uri}" Target="file:///C:/plantillas/acta.dotm" '
        'TargetMode="External"/></Relationships>'
    )


def _docx_con_miembros(cantidad):
    return _ooxml(WORD | {f"word/{i}.xml": "" for i in range(cantidad)})


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_la_guarda_de_zip_tiene_topes_por_clase():
    assert TOPES_DE_ZIP == {
        ClaseDeOriginal.DOCUMENTO: TopesDeZip(
            miembros=2000, descomprimido_bytes=150_000_000, razon=200
        ),
        ClaseDeOriginal.PLANILLA: TopesDeZip(
            miembros=1000, descomprimido_bytes=100_000_000, razon=100
        ),
    }


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_1500_miembros_entran_en_un_word_y_son_bomba_en_una_planilla(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    vacios = {f"xl/{i}.xml": "" for i in range(1500)}

    almacen.guardar_en_streaming(io.BytesIO(_docx_con_miembros(1500)))
    with pytest.raises(ArchivoIlegible) as rechazo:
        almacen.guardar_en_streaming(io.BytesIO(_ooxml({"xl/workbook.xml": "<w/>"} | vacios)))

    assert rechazo.value.motivo == "bomba-de-compresion"


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_la_guarda_descomprime_la_bomba_con_memoria_acotada(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    bomba = io.BytesIO(_docx_que_declara_1_kib_y_descomprime_200_mb())

    tracemalloc.start()
    try:
        with pytest.raises(ArchivoIlegible):
            almacen.guardar_en_streaming(bomba)
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert pico < 8 * MiB
    assert archivos(tmp_path) == []


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
@pytest.mark.parametrize("compresion", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA], ids=["bzip2", "lzma"])
def test_un_miembro_comprimido_como_office_no_lo_hace_no_se_descomprime(tmp_path, compresion):
    """zipfile descomprime bzip2 y LZMA sin cota por lectura: unos KiB pueden ser GiB."""
    word = _ooxml({"word/document.xml": "<w:document/>"}, compresion)

    with pytest.raises(ArchivoIlegible) as rechazo:
        AlmacenDeOriginales(tmp_path).guardar_en_streaming(io.BytesIO(word))

    assert rechazo.value.motivo == "zip-corrupto"
    assert archivos(tmp_path) == []


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
@pytest.mark.parametrize(
    "bomba",
    [
        pytest.param(_docx_que_declara_1_kib_y_descomprime_200_mb, id="declara-1-kib-y-son-200-mb"),
        pytest.param(lambda: _docx_con_miembros(5000), id="5000-miembros"),
        pytest.param(lambda: bytes(_con_ceros("word/document.xml", 20)), id="razon-1000-a-1"),
    ],
)
def test_seg_zip_bomba_en_word(cliente, directorio_originales, bomba):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "acta.docx", bomba(), DOCX)

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/archivo-ilegible"
    assert problema["motivo"] == "bomba-de-compresion"
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _cfb(*flujos):
    """Una cabecera OLE2 y las entradas de su directorio, con el nombre en UTF-16."""
    return CFB + bytes(504) + b"".join(f.encode("utf-16-le").ljust(128, b"\x00") for f in flujos)


def _zip_cifrado(miembros):
    datos = bytearray(_ooxml(miembros))
    for firma, desplazamiento in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        datos[datos.index(firma) + desplazamiento] |= 0x01  # bit de cifrado
    return bytes(datos)


MACRO_EN_LOS_TIPOS = (
    '<Types><Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/></Types>'
)


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(98)
@pytest.mark.parametrize(
    ("nombre", "contenido", "motivo", "que_hacer"),
    [
        pytest.param(
            "notas.xls",
            _cfb("Root Entry", "Workbook"),
            "xls-binario",
            "Guardala como .xlsx",
            id="xls",
        ),
        pytest.param(
            "notas.xlsb",
            _ooxml({"xl/workbook.bin": bytes(10)}),
            "xls-binario",
            "Guardala como .xlsx",
            id="xlsb",
        ),
        pytest.param(
            "notas.xlsm",
            _ooxml({"[Content_Types].xml": MACRO_EN_LOS_TIPOS, "xl/workbook.xml": "<w/>"}),
            "con-macros",
            "Guardala como .xlsx, sin macros",
            id="xlsm",
        ),
        pytest.param(
            "acta.docm",
            _ooxml({"word/document.xml": "<w:document/>", "word/vbaProject.bin": bytes(10)}),
            "con-macros",
            "Guardalo como .docx, sin macros",
            id="docm",
        ),
        pytest.param(
            "notas.xlsx",
            _cfb("Root Entry", "EncryptionInfo", "EncryptedPackage"),
            "cifrada",
            "Quitale la contraseña",
            id="ooxml-cifrado",
        ),
        pytest.param(
            "acta.docx", _zip_cifrado(WORD), "cifrada", "Quitale la contraseña", id="zip-cifrado"
        ),
        # Cifrado sin ser Office: no hay contraseña que quitar desde Word o Excel.
        pytest.param(
            "fotos.zip",
            _zip_cifrado({"fotos/leeme.txt": "hola"}),
            "contenedor-no-admitido",
            "Historia Viva acepta fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv) y texto",
            id="zip-cifrado-que-no-es-office",
        ),
        # OLE2 que no es ni Word ni Excel: no hay qué hacer con él más que decir qué se acepta.
        pytest.param(
            "clase.ppt",
            _cfb("Root Entry", "PowerPoint Document"),
            None,
            "Historia Viva acepta fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv) y texto",
            id="ppt",
        ),
        # Una plantilla (.dotx) trae `word/document.xml`, pero su tipo principal no es el de Word.
        pytest.param(
            "acta.dotx",
            _ooxml(WORD | {"[Content_Types].xml": TIPOS_WORD.replace(".document.", ".template.")}),
            "contenedor-no-admitido",
            "Historia Viva acepta fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv) y texto",
            id="dotx",
        ),
        pytest.param(
            "acta.docx",
            _ooxml(WORD | {"word/_rels/settings.xml.rels": _relacion_externa("attachedTemplate")}),
            "contenedor-no-admitido",
            "Quitale los vínculos",
            id="parte-externa",
        ),
        # Un Word viejo no tiene motivo en el catálogo, pero sí qué hacer.
        pytest.param(
            "acta.doc", _cfb("Root Entry", "WordDocument"), None, "Guardalo como .docx", id="doc"
        ),
    ],
)
def test_seg_formatos_rechazados(
    cliente, directorio_originales, nombre, contenido, motivo, que_hacer
):
    _con_sesion(cliente)

    respuesta = _subir(cliente, nombre, contenido)

    assert respuesta.status_code == 415
    problema = respuesta.json()
    assert problema["type"] == "/errores/tipo-no-admitido"
    assert problema.get("motivo") == motivo
    assert que_hacer in problema["detail"]
    assert DOCX in problema["admitidos"]
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LIBRO = {"xl/workbook.xml": "<workbook/>"}
"""Un libro sin `[Content_Types].xml`: no se admite (090); `LIBRO_CON_TIPOS` sí."""
TIPO_LIBRO = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


def _tipos_libro(tipo=TIPO_LIBRO, parte="/xl/workbook.xml"):
    return (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="{parte}" ContentType="{tipo}"/></Types>'
    )


LIBRO_CON_TIPOS = LIBRO | {"[Content_Types].xml": _tipos_libro()}
CSV_EXCEL = "Año;Carrera\r\n2024;Ingeniería\r\n".encode("cp1252")


@pytest.mark.rf("RF-38")
@pytest.mark.historia(91)
def test_admitir_planillas_es_un_dato_de_la_politica_por_clase(tmp_path):
    planilla = POLITICAS[ClaseDeOriginal.PLANILLA]
    apagada = dict(POLITICAS) | {ClaseDeOriginal.PLANILLA: replace(planilla, admitida=False)}

    libro = AlmacenDeOriginales(tmp_path).guardar_en_streaming(
        io.BytesIO(_ooxml(LIBRO_CON_TIPOS)), nombre="notas.xlsx"
    )
    with pytest.raises(TipoNoAdmitido) as rechazo:
        AlmacenDeOriginales(tmp_path, apagada).guardar_en_streaming(
            io.BytesIO(_ooxml(LIBRO_CON_TIPOS)), nombre="notas.xlsx"
        )

    assert (libro.clase, libro.tipo_mime) == (ClaseDeOriginal.PLANILLA, XLSX)
    assert XLSX in TipoNoAdmitido("").admitidos
    assert rechazo.value.clase is ClaseDeOriginal.PLANILLA


@pytest.mark.rf("RF-38")
@pytest.mark.historia(97)
@pytest.mark.parametrize(
    ("contenido", "motivo"),
    [
        (b"", "sin-hojas"),
        (b"a;b\r\n\x00", "csv-binario"),
        (b"\xff\xfea", "codificacion-desconocida"),
    ],
)
def test_un_csv_ilegible_se_rechaza_con_su_motivo_antes_de_escribir(tmp_path, contenido, motivo):
    with pytest.raises(ArchivoIlegible) as rechazo:
        AlmacenDeOriginales(tmp_path).guardar_en_streaming(io.BytesIO(contenido), nombre="a.csv")

    assert rechazo.value.motivo == motivo
    assert archivos(tmp_path) == []


@pytest.mark.rf("RF-38")
@pytest.mark.historia(97)
@pytest.mark.parametrize(
    ("nombre", "contenido"),
    [
        pytest.param("notas.csv", CSV_EXCEL, id="csv"),
        pytest.param("notas.tsv", "Año\tCarrera\r\n2024\tDiseño\r\n".encode(), id="tsv"),
    ],
)
def test_un_csv_o_un_tsv_se_admite_como_planilla_con_el_mime_text_csv(
    cliente, nombre, contenido
):
    _con_sesion(cliente)

    respuesta = _subir(cliente, nombre, contenido)

    assert respuesta.status_code == 201, respuesta.text
    pieza_id = respuesta.json()["pieza_id"]
    assert cliente.get(f"/api/piezas/{pieza_id}").json()["tipo_mime"] == "text/csv"
    assert "text/csv" in TipoNoAdmitido("").admitidos


def _csv_largo(tamano: int, final: bytes) -> bytes:
    """Un CSV UTF-8 de `tamano` bytes con tildes, que termina en `final`."""
    registro = "2024;Ingeniería\r\n".encode()
    cuerpo = "Año;Carrera\r\n".encode() + registro * (tamano // len(registro))
    return cuerpo + final


@pytest.mark.rf("RF-38")
@pytest.mark.historia(289)
@pytest.mark.parametrize(
    ("final", "motivo"),
    [
        pytest.param(b"\xff;x\r\n", "codificacion-desconocida", id="byte-invalido"),
        pytest.param(b"\x00;x\r\n", "csv-binario", id="nul"),
        # La «Ã» sin su segundo byte: el archivo termina en el medio de una letra.
        pytest.param(b"\xc3", "codificacion-desconocida", id="letra-partida-al-final"),
    ],
)
@pytest.mark.parametrize(
    "tamano",
    [
        # Dentro del primer bloque de 1 MiB: se rechaza antes de escribir.
        pytest.param(700 * 1024, id="dentro-del-primer-bloque"),
        # Más allá: se rechaza recién escrito el archivo entero, y el temporal se borra.
        pytest.param(3 * 1024 * 1024, id="despues-del-primer-bloque"),
    ],
)
def test_un_byte_ilegible_despues_de_la_muestra_responde_422_sin_pieza_ni_temporal(
    cliente, directorio_originales, final, motivo, tamano
):
    """Atajo de 028: la muestra de 512 KiB decide la codificación; lo que sigue la cumple o el
    CSV entero se rechaza (DEC-43)."""
    _con_sesion(cliente)

    respuesta = _subir(cliente, "inscriptos.csv", _csv_largo(tamano, final))

    assert respuesta.status_code == 422, respuesta.text
    problema = respuesta.json()
    assert (problema["type"], problema["motivo"]) == ("/errores/archivo-ilegible", motivo)
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


@pytest.mark.rf("RF-38")
@pytest.mark.historia(289)
def test_una_letra_partida_por_el_primer_bloque_no_rechaza_el_csv(tmp_path):
    # La «í» empieza en el último byte del primer bloque de 1 MiB que mira la admisión.
    relleno = b"a;b\r\n" * (BLOQUE_BYTES // 5)
    contenido = relleno + b"x" * (BLOQUE_BYTES - len(relleno) - 1) + "í;b\r\n".encode() * 10

    temporal = AlmacenDeOriginales(tmp_path).guardar_en_streaming(
        io.BytesIO(contenido), nombre="a.csv"
    )

    assert (temporal.clase, temporal.tipo_mime) == (ClaseDeOriginal.PLANILLA, "text/csv")


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize("tipo", ["attachedTemplate", "subDocument", "oleObject", "frame", "image"])
def test_un_word_que_trae_contenido_de_afuera_se_rechaza(tmp_path, tipo):
    word = _ooxml(WORD | {"word/_rels/document.xml.rels": _relacion_externa(tipo)})

    with pytest.raises(TipoNoAdmitido) as rechazo:
        AlmacenDeOriginales(tmp_path).guardar_en_streaming(io.BytesIO(word))

    assert rechazo.value.motivo == "contenedor-no-admitido"
    assert archivos(tmp_path) == []


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
def test_un_word_con_hipervinculos_y_sin_tipos_declarados(tmp_path):
    """Un hipervínculo no trae contenido al documento; sin `[Content_Types].xml` no es Word."""
    almacen = AlmacenDeOriginales(tmp_path)
    con_hipervinculo = WORD | {"word/_rels/document.xml.rels": _relacion_externa("hyperlink")}

    admitido = almacen.guardar_en_streaming(io.BytesIO(_ooxml(con_hipervinculo)))
    with pytest.raises(TipoNoAdmitido) as rechazo:
        almacen.guardar_en_streaming(io.BytesIO(_ooxml({"word/document.xml": "<w:document/>"})))

    assert admitido.tipo_mime == DOCX
    assert rechazo.value.motivo == "contenedor-no-admitido"


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    ("tipo", "admitido"),
    [
        pytest.param(
            "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink", True, id="strict"
        ),
        pytest.param("http://cualquiera/hyperlink", False, id="uri-que-termina-igual"),
    ],
)
def test_solo_un_hipervinculo_con_su_uri_oficial_puede_apuntar_afuera(tmp_path, tipo, admitido):
    word = _ooxml(WORD | {"word/_rels/document.xml.rels": _relacion_externa(tipo)})

    try:
        resultado = AlmacenDeOriginales(tmp_path).guardar_en_streaming(io.BytesIO(word)).tipo_mime
    except TipoNoAdmitido as rechazo:
        resultado = rechazo.motivo

    assert resultado == (DOCX if admitido else "contenedor-no-admitido")


def _con_repetidos(*miembros):
    """Un ZIP que puede traer dos veces el mismo nombre: `zipfile` lee siempre el último."""
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as comprimido:
        for nombre, contenido in miembros:
            comprimido.writestr(nombre, contenido)
    return memoria.getvalue()


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
@pytest.mark.filterwarnings("ignore:Duplicate name")
@pytest.mark.parametrize(
    "repetido",
    [
        pytest.param("word/_rels/settings.xml.rels", id="mismo-nombre"),
        pytest.param("WORD/_rels/settings.xml.rels", id="otras-mayusculas"),
    ],
)
def test_un_zip_con_nombres_repetidos_responde_422_aunque_el_ultimo_este_limpio(
    cliente, directorio_originales, repetido
):
    """OPC prohíbe dos partes con nombres equivalentes sin distinguir mayúsculas."""
    _con_sesion(cliente)
    externa = ("word/_rels/settings.xml.rels", _relacion_externa("attachedTemplate"))
    limpia = (repetido, _relacion_externa("hyperlink"))

    respuesta = _subir(cliente, "acta.docx", _con_repetidos(*WORD.items(), externa, limpia), DOCX)

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/archivo-ilegible"
    assert problema["motivo"] == "zip-corrupto"
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


RISAS = (
    "<!DOCTYPE Relationships [<!ENTITY e0 'ja'>"
    + "".join(f"<!ENTITY e{i} '{f'&e{i - 1};' * 10}'>" for i in range(1, 10))
    + "]><Relationships>&e9;</Relationships>"
)
XXE = "<!DOCTYPE Types [<!ENTITY e SYSTEM 'file:///C:/Windows/win.ini'>]>" + TIPOS_WORD.replace(
    "</Types>", "&e;</Types>"
)


@pytest.mark.rf("RF-04")
@pytest.mark.rnf("RNF-20")
@pytest.mark.parametrize(
    "parte",
    [
        pytest.param({"[Content_Types].xml": XXE}, id="xxe"),
        pytest.param({"word/_rels/document.xml.rels": RISAS}, id="billion-laughs"),
        pytest.param(
            {"word/_rels/document.xml.rels": _relacion_externa("hyperlink")[:-20]},
            id="rels-malformado",
        ),
        pytest.param(
            # Hex de bytes al azar: no se comprime tanto como para que lo corte la guarda.
            {
                "word/_rels/document.xml.rels": "<Relationships><!--"
                + random.Random(0).randbytes(300_000).hex()
                + "--></Relationships>"
            },
            id="parte-de-mas-de-512-kib",
        ),
    ],
)
def test_una_parte_xml_con_dtd_o_malformada_responde_422(cliente, directorio_originales, parte):
    """OPC prohíbe las DTD (ECMA-376 parte 2): sin DTD no hay entidades que resolver."""
    _con_sesion(cliente)

    respuesta = _subir(cliente, "acta.docx", _ooxml(WORD | parte), DOCX)

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/archivo-ilegible"
    assert problema["motivo"] == "zip-corrupto"
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


# `tracemalloc` no ve lo que reserva libxml2: se mide el pico del proceso, en uno nuevo.
_PICO_DE_LA_ADMISION = """
import ctypes, sys
from pathlib import Path
from historia_viva.dominio.errores import ErrorHistoriaViva
from historia_viva.originales import AlmacenDeOriginales

def pico():
    if sys.platform == "win32":
        from ctypes import wintypes
        class Contadores(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("fallos", wintypes.DWORD),
                        ("pico", ctypes.c_size_t), ("resto", ctypes.c_size_t * 7)]
        contadores = Contadores(cb=ctypes.sizeof(Contadores))
        ctypes.WinDLL("kernel32").K32GetProcessMemoryInfo(
            wintypes.HANDLE(-1), ctypes.byref(contadores), contadores.cb)
        return contadores.pico
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

antes = pico()
with open(sys.argv[1], "rb") as flujo:
    try:
        resultado = AlmacenDeOriginales(Path(sys.argv[2])).guardar_en_streaming(flujo).tipo_mime
    except ErrorHistoriaViva as error:
        resultado = getattr(error, "motivo", type(error).__name__)
print(resultado, pico() - antes)
"""


def _docx_con_tipos_rellenos(prologo, relleno):
    """Un Word cuyo `[Content_Types].xml` trae 10 MB de `relleno` antes de su `Override`."""
    tipos = TIPOS_WORD.encode()
    override = tipos.index(b"<Override")
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as comprimido:
        with comprimido.open("[Content_Types].xml", "w") as parte:
            parte.write(prologo + tipos[:override])
            for _ in range(10):
                parte.write(relleno * (1_000_000 // len(relleno)))
            parte.write(tipos[override:])
        comprimido.writestr("word/document.xml", "<w:document/>")
        # Lo incompresible agranda el ZIP para que la razón no lo corte como bomba.
        comprimido.writestr("word/media/ruido.bin", os.urandom(256 * 1024), zipfile.ZIP_STORED)
    return memoria.getvalue()


@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
@pytest.mark.parametrize(
    ("prologo", "relleno", "resultado"),
    [
        pytest.param(b"", b"<!---->", "zip-corrupto", id="comentarios"),
        pytest.param(b"", b"<?a?>", "zip-corrupto", id="instrucciones"),
        # Cinco bytes por referencia: menos, libxml2 lo corta por amplificación de entidades.
        pytest.param(
            b'<!DOCTYPE Types [<!ENTITY e "">]>', b"&e;  ", "zip-corrupto", id="entidades"
        ),
    ],
)
def test_leer_las_partes_xml_no_arma_un_arbol(tmp_path, prologo, relleno, resultado):
    """Comentarios, instrucciones y referencias a entidades son nodos que no cierran un
    elemento: un lector que suelta lo leído al cerrar cada elemento los acumula. Con 10 MB
    de relleno la parte supera los 512 KiB y se corta sin que la memoria crezca."""
    docx = tmp_path / "acta.docx"
    docx.write_bytes(_docx_con_tipos_rellenos(prologo, relleno))

    salida = subprocess.run(
        [sys.executable, "-c", _PICO_DE_LA_ADMISION, str(docx), str(tmp_path / "originales")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert salida[0] == resultado
    assert int(salida[1]) < 16 * MiB


@pytest.mark.rf("RF-04")
def test_un_formulario_sin_archivo_elegido_responde_que_falta_el_campo(
    cliente, directorio_originales
):
    _con_sesion(cliente)
    # Lo que manda un navegador cuando el `<input type="file">` quedó vacío.
    cuerpo = (
        f"--{FRONTERA}\r\nContent-Disposition: form-data; name=\"archivo\"; filename=\"\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n\r\n--{FRONTERA}--\r\n"
    ).encode()

    respuesta = _pedido_crudo(cliente, cuerpo)

    assert respuesta.status_code == 422
    assert respuesta.json()["detail"] == "Falta el campo archivo."
    assert archivos(directorio_originales) == []


# --- Ver el original: `abrir` lo sirve por tramos (010, DEC-45, DEC-112) --------------------

CUERPO = PDF + bytes(range(256)) * 4
"""Un PDF con cuerpo de sobra para cortar tramos."""


def _guardado(almacen, *, nombre="acta.pdf", flujo=None):
    """Un original confirmado y lo que la base sabe de él."""
    temporal = almacen.guardar_en_streaming(flujo or io.BytesIO(CUERPO))
    return OriginalGuardado(
        identificador=almacen.confirmar(temporal, pieza_id=uuid4()),
        nombre_archivo=nombre,
        tipo_mime=temporal.tipo_mime,
        tamano_bytes=temporal.tamano_bytes,
        creado_el=datetime.now(UTC),
        huella=temporal.sha256,
    )


@pytest.mark.rf("RF-06")
@pytest.mark.historia(11)
@pytest.mark.parametrize(
    ("rango", "estado", "tramo"),
    [
        (None, 200, slice(None)),
        ("bytes=0-0", 206, slice(0, 1)),
        ("bytes=10-19", 206, slice(10, 20)),
        ("bytes=10-", 206, slice(10, None)),
        ("bytes=-10", 206, slice(-10, None)),
        ("bytes=-50000", 206, slice(None)),  # Más cola que original: es el original entero.
        ("bytes=90-50000", 206, slice(90, None)),  # El fin se recorta al último byte.
        ("bytes= 10 - 19 ", 206, slice(10, 20)),  # Espacios alrededor: los navegadores los mandan.
        # RFC 9110 §14.2: un `Range` que no se entiende se ignora, nunca falla.
        ("bytes=0-9, 20-29", 200, slice(None)),  # Varios rangos: el original entero.
        ("paginas=1-2", 200, slice(None)),
        ("bytes=abc", 200, slice(None)),
        ("bytes=20-10", 200, slice(None)),
        ("bytes=", 200, slice(None)),
        ("bytes=-", 200, slice(None)),
        ("", 200, slice(None)),
        # Más de 4300 dígitos hacen fallar a `int()`: un número así no es un tramo de nadie.
        pytest.param("bytes=" + "9" * 5000 + "-", 200, slice(None), id="5000-digitos-inicio"),
        pytest.param("bytes=0-" + "9" * 5000, 200, slice(None), id="5000-digitos-fin"),
        pytest.param("bytes=-" + "9" * 5000, 200, slice(None), id="5000-digitos-cola"),
        pytest.param("bytes=0-" + "9" * 20, 200, slice(None), id="20-digitos"),
        # Dígitos que no son ASCII: `int()` los entiende, HTTP no.
        pytest.param(
            "bytes=\N{ARABIC-INDIC DIGIT ONE}-\N{ARABIC-INDIC DIGIT TWO}",
            200,
            slice(None),
            id="digitos-arabes",
        ),
    ],
)
def test_abrir_sirve_el_tramo_que_se_entiende_y_si_no_el_original_entero(
    tmp_path, rango, estado, tramo
):
    almacen = AlmacenDeOriginales(tmp_path)

    respuesta = almacen.abrir(_guardado(almacen), rango)

    esperado = CUERPO[tramo]
    assert respuesta.estado == estado
    assert b"".join(respuesta.bloques) == esperado
    assert respuesta.cabeceras["Content-Length"] == str(len(esperado))
    assert respuesta.cabeceras["Accept-Ranges"] == "bytes"
    if estado == 206:
        inicio, fin, _ = tramo.indices(len(CUERPO))
        assert respuesta.cabeceras["Content-Range"] == f"bytes {inicio}-{fin - 1}/{len(CUERPO)}"


@pytest.mark.rnf("RNF-15")
@pytest.mark.historia(216)
def test_abrir_40_mib_entrega_de_a_1_mib_y_no_pasa_de_8_mib_de_memoria(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)
    original = _guardado(almacen, flujo=FlujoSintetico(PDF, 40 * MiB))

    tracemalloc.start()
    try:
        tamanos = [len(bloque) for bloque in almacen.abrir(original).bloques]
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sum(tamanos) == 40 * MiB
    assert max(tamanos) <= MiB
    assert pico < 8 * MiB


@pytest.mark.rf("RF-06")
@pytest.mark.historia(11)
@pytest.mark.parametrize("rango", ["bytes=1000000-", "bytes=1000000-1000010", "bytes=-0"])
def test_un_tramo_fuera_del_original_es_416_con_su_tamano(tmp_path, rango):
    almacen = AlmacenDeOriginales(tmp_path)

    respuesta = almacen.abrir(_guardado(almacen), rango)

    assert respuesta.estado == 416
    # Con el tamaño el visor se corrige; el cuerpo va vacío (no es RFC 9457).
    assert respuesta.cabeceras["Content-Range"] == f"bytes */{len(CUERPO)}"
    assert b"".join(respuesta.bloques) == b""


@pytest.mark.rf("RF-06")
@pytest.mark.parametrize("rango", ["bytes=-10", "bytes=0-"])
def test_un_original_vacio_no_tiene_ningun_tramo(tmp_path, rango):
    almacen = AlmacenDeOriginales(tmp_path)
    original = _guardado(almacen)
    en_disco(tmp_path, original.identificador).write_bytes(b"")

    respuesta = almacen.abrir(original, rango)

    assert respuesta.estado == 416
    assert respuesta.cabeceras["Content-Range"] == "bytes */0"


def _anotar_aperturas(monkeypatch):
    """Los archivos que se abran desde acá: para ver que `abrir` no deja ninguno abierto."""
    lista = []
    abrir = Path.open

    def abrir_anotando(ruta, *args, **kwargs):
        lista.append(abrir(ruta, *args, **kwargs))
        return lista[-1]

    monkeypatch.setattr(Path, "open", abrir_anotando)
    return lista


@pytest.mark.rnf("RNF-15")
def test_si_el_cliente_corta_la_descarga_el_original_se_cierra(tmp_path, monkeypatch):
    almacen = AlmacenDeOriginales(tmp_path)
    original = _guardado(almacen, flujo=FlujoSintetico(PDF, 3 * MiB))
    archivos_abiertos = _anotar_aperturas(monkeypatch)
    bloques = almacen.abrir(original).bloques

    next(bloques)
    bloques.close()  # Lo que hace el servidor cuando el cliente se va a mitad.

    assert [archivo.closed for archivo in archivos_abiertos] == [True]


@pytest.mark.rf("RF-06")
def test_si_armar_la_respuesta_falla_el_original_se_cierra(tmp_path, monkeypatch):
    almacen = AlmacenDeOriginales(tmp_path)
    original = _guardado(almacen)
    archivos_abiertos = _anotar_aperturas(monkeypatch)

    def falla(*_):
        raise ValueError("un número que no entra en un int")

    monkeypatch.setattr("historia_viva.originales._resolver_rango", falla)

    with pytest.raises(ValueError):
        almacen.abrir(original, "bytes=0-0")
    # En Windows un archivo abierto no se puede borrar.
    assert [archivo.closed for archivo in archivos_abiertos] == [True]


@pytest.fixture
def ruta_del_original(cliente):
    """Una Pieza con su original ya guardado, y la ruta para pedirlo."""
    subida = _subir(_con_sesion(cliente), "acta.bin", CUERPO)
    return f"{subida.headers['location']}/original"


@pytest.mark.rf("RF-06")
@pytest.mark.historia(11)
def test_el_original_vuelve_con_sus_bytes_su_mime_real_etag_y_cache_privada(
    cliente, ruta_del_original
):
    respuesta = cliente.get(ruta_del_original)

    assert respuesta.status_code == 200
    assert respuesta.content == CUERPO
    # El MIME es el que se detectó por contenido, no el del nombre ni el del cliente.
    assert respuesta.headers["content-type"] == "application/pdf"
    assert respuesta.headers["etag"] == f'"{hashlib.sha256(CUERPO).hexdigest()}"'
    assert respuesta.headers["cache-control"] == "private, max-age=86400"
    # Sin `Accept-Ranges` el visor ni intenta pedir por tramos.
    assert respuesta.headers["accept-ranges"] == "bytes"
    assert respuesta.headers["content-length"] == str(len(CUERPO))


@pytest.mark.rf("RF-06")
@pytest.mark.historia(11)
@pytest.mark.parametrize(
    ("rango", "estado", "cuerpo", "content_range"),
    [
        # La pantalla pregunta con un byte si el original está: un `<iframe>` no avisa.
        ("bytes=0-0", 206, CUERPO[:1], f"bytes 0-0/{len(CUERPO)}"),
        ("bytes=10-19", 206, CUERPO[10:20], f"bytes 10-19/{len(CUERPO)}"),
        ("bytes=900-", 206, CUERPO[900:], f"bytes 900-{len(CUERPO) - 1}/{len(CUERPO)}"),
        ("bytes=-8", 206, CUERPO[-8:], f"bytes {len(CUERPO) - 8}-{len(CUERPO) - 1}/{len(CUERPO)}"),
        (f"bytes={len(CUERPO)}-", 416, b"", f"bytes */{len(CUERPO)}"),
        ("bytes=0-9, 20-29", 200, CUERPO, None),
    ],
    ids=["un-byte", "inicial", "final", "cola", "fuera-de-rango", "varios"],
)
def test_el_original_se_sirve_por_tramos(
    cliente, ruta_del_original, rango, estado, cuerpo, content_range
):
    respuesta = cliente.get(ruta_del_original, headers={"Range": rango})

    assert respuesta.status_code == estado
    assert respuesta.content == cuerpo
    assert respuesta.headers.get("content-range") == content_range
    assert respuesta.headers["content-length"] == str(len(cuerpo))


@pytest.mark.rf("RF-06")
@pytest.mark.historia(112)
@pytest.mark.parametrize(
    ("nombre", "contenido", "disposicion"),
    [
        ("patio.jpg", JPEG, "inline; filename=\"patio.jpg\"; filename*=UTF-8''patio.jpg"),
        ("acta.pdf", PDF, "inline; filename=\"acta.pdf\"; filename*=UTF-8''acta.pdf"),
        # Un Word abierto por el navegador no se ve: se baja, y nunca se interpreta.
        (
            "convenio.docx",
            _ooxml(WORD),
            "attachment; filename=\"convenio.docx\"; filename*=UTF-8''convenio.docx",
        ),
        # Una planilla tampoco: Excel la abriría con sus vínculos (DEC-45).
        (
            "notas.xlsx",
            libro(TRES_HOJAS),
            "attachment; filename=\"notas.xlsx\"; filename*=UTF-8''notas.xlsx",
        ),
        (
            "acta fundación.pdf",
            PDF,
            "inline; filename=\"acta fundacion.pdf\"; filename*=UTF-8''acta%20fundaci%C3%B3n.pdf",
        ),
    ],
    ids=["imagen", "pdf", "word", "planilla", "acentos"],
)
def test_la_disposicion_va_por_clase_y_siempre_con_nosniff(
    cliente, nombre, contenido, disposicion
):
    subida = _subir(_con_sesion(cliente), nombre, contenido)

    respuesta = cliente.get(f"{subida.headers['location']}/original")

    assert respuesta.headers["content-disposition"] == disposicion
    assert respuesta.headers["x-content-type-options"] == "nosniff"


RTL = "\N{RIGHT-TO-LEFT OVERRIDE}"
ANCHO_CERO = "\N{ZERO WIDTH SPACE}"
HOSTILES = {
    "ruta-windows": r"..\..\x.pdf",
    "ruta-unix": "/etc/passwd",
    "nul": "acta\x00.pdf",
    "rtl": f"acta{RTL}fdp.exe",
    "5000-caracteres": "a" * 5000 + ".pdf",
    "comillas": "x\"; filename*=UTF-8''malo.html",
    "comilla-ancha": "x\N{FULLWIDTH QUOTATION MARK}; filename*=UTF-8''malo.html",
    "salto": "x\r\nSet-Cookie: sesion=robada",
    "vacio": "",
    "solo-formato": RTL + ANCHO_CERO,
}

_DISPOSICION_SANA = re.compile(
    r"inline; filename=\"([\x20-\x21\x23-\x2e\x30-\x5b\x5d-\x7e]{1,255})\"; "
    r"filename\*=UTF-8''([A-Za-z0-9%._~-]+)"
)
"""Un solo `filename` ASCII sin comillas, barras ni control, y un `filename*` codificado."""


def _nombre_sano(disposicion):
    partes = _DISPOSICION_SANA.fullmatch(disposicion)
    assert partes is not None, disposicion
    nombre = unquote(partes.group(2), errors="strict")
    assert 0 < len(nombre) <= 255
    assert not any(c in f'/\\"\x00\r\n{RTL}{ANCHO_CERO}' for c in nombre)
    return partes.group(1), nombre


@pytest.mark.rf("RF-06")
@pytest.mark.parametrize("nombre", HOSTILES.values(), ids=HOSTILES.keys())
def test_un_nombre_hostil_se_sanea_sin_inyectar_cabeceras(tmp_path, nombre):
    almacen = AlmacenDeOriginales(tmp_path)

    respuesta = almacen.abrir(_guardado(almacen, nombre=nombre))

    _, saneado = _nombre_sano(respuesta.cabeceras["Content-Disposition"])
    if nombre.endswith(".pdf"):
        assert saneado.endswith(".pdf")  # acotado por la cola: la extensión queda
    if not nombre.strip(RTL + ANCHO_CERO):
        assert saneado == "archivo"


@pytest.mark.rf("RF-06")
# Los 5000 caracteres no llegan por HTTP: el multipart los rechaza antes (422 `pedido-invalido`).
@pytest.mark.parametrize("nombre", [r"..\x.pdf", f"acta{RTL}fdp.pdf"], ids=["ruta", "rtl"])
def test_por_http_un_nombre_hostil_llega_saneado_a_la_cabecera(cliente, nombre):
    subida = _subir(_con_sesion(cliente), nombre, PDF)

    respuesta = cliente.get(f"{subida.headers['location']}/original")

    assert respuesta.status_code == 200
    _nombre_sano(respuesta.headers["content-disposition"])
    assert respuesta.headers["x-content-type-options"] == "nosniff"


@pytest.mark.rf("RF-06")
@pytest.mark.historia(11, 130)
def test_original_no_disponible_informa(cliente, pieza_pendiente, directorio_originales):
    (directorio_originales / pieza_pendiente).unlink()

    respuesta = cliente.get(f"/api/piezas/{pieza_pendiente}/original")

    assert respuesta.status_code == 404
    assert respuesta.headers["content-type"] == "application/problem+json"
    problema = respuesta.json()
    assert problema["type"] == "/errores/original-no-disponible"
    assert problema["archivo_original"] == f"local://originales/{pieza_pendiente}"
    # La pantalla avisa y deja validar igual (DEC-112): sin el archivo, la Ficha se firma.
    validada = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)
    assert validada.json()["estado"] == "Validada"


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


@pytest.mark.historia(100)
@pytest.mark.parametrize(
    ("cabecera", "solo_video"),
    [pytest.param(MP4, False, id="video-por-archivos"), pytest.param(PDF, True, id="pdf-por-video")],
)
def test_la_puerta_equivocada_se_rechaza_por_el_primer_bloque(tmp_path, cabecera, solo_video):
    almacen = AlmacenDeOriginales(tmp_path)
    flujo = FlujoSintetico(cabecera, 2 * 1024**3)

    with pytest.raises(TipoNoAdmitido):
        almacen.guardar_en_streaming(flujo, solo_video=solo_video)

    assert len(flujo.pedidos) == 1
    assert not any(ruta.is_file() for ruta in tmp_path.rglob("*"))


# --- 090: planilla hostil en la admisión ----------------------------------------------------

@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(98)
@pytest.mark.parametrize(
    "miembros",
    [
        pytest.param(LIBRO, id="sin-content-types"),
        pytest.param(
            LIBRO | {"[Content_Types].xml": _tipos_libro(parte="/xl/otro.xml")},
            id="tipo-de-otra-parte",
        ),
        pytest.param(
            LIBRO
            | {
                "[Content_Types].xml": _tipos_libro(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml"
                )
            },
            id="plantilla-xltx",
        ),
    ],
)
def test_un_xlsx_sin_el_tipo_principal_del_libro_responde_415(cliente, miembros):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "notas.xlsx", _ooxml(miembros), XLSX)

    assert respuesta.status_code == 415, respuesta.text
    problema = respuesta.json()
    assert (problema["type"], problema["motivo"]) == (
        "/errores/tipo-no-admitido",
        "contenedor-no-admitido",
    )
    assert "Excel" in problema["detail"]
    assert cliente.get("/api/piezas").json() == {"piezas": []}


def _xlsx_de(tamano):
    """Un libro de exactamente `tamano` bytes: un miembro que no se comprime y el comentario
    del ZIP para el ajuste fino."""
    base = len(_ooxml(LIBRO_CON_TIPOS | {"xl/media/foto.bin": b""}, zipfile.ZIP_STORED))
    relleno = random.Random(90).randbytes(tamano - base - 1000)
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_STORED) as comprimido:
        for nombre, contenido in (LIBRO_CON_TIPOS | {"xl/media/foto.bin": relleno}).items():
            comprimido.writestr(nombre, contenido)
        comprimido.comment = b"x" * (tamano - base - len(relleno))
    assert len(memoria.getvalue()) == tamano
    return memoria.getvalue()


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_un_xlsx_se_admite_hasta_10_mb_contados_sobre_lo_escrito(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)

    justo = almacen.guardar_en_streaming(io.BytesIO(_xlsx_de(10_000_000)), nombre="a.xlsx")
    with pytest.raises(ArchivoDemasiadoGrande) as rechazo:
        almacen.guardar_en_streaming(io.BytesIO(_xlsx_de(10_000_001)), nombre="a.xlsx")

    assert justo.clase is ClaseDeOriginal.PLANILLA
    assert (rechazo.value.clase, rechazo.value.tope_bytes) == (ClaseDeOriginal.PLANILLA, 10**7)
    assert archivos(tmp_path) == [justo.ruta]


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_seg_un_xlsx_de_mas_de_10_mb_responde_413(cliente, directorio_originales):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "notas.xlsx", _xlsx_de(10_000_001), XLSX)

    assert respuesta.status_code == 413, respuesta.text
    problema = respuesta.json()
    assert problema["type"] == "/errores/archivo-demasiado-grande"
    assert (problema["tope_bytes"], problema["clase_original"]) == (10_000_000, "planilla")
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


def _libro_con_miembros(cantidad):
    """Un libro con su `[Content_Types].xml`, su `workbook.xml` y miembros vacíos hasta sumar
    `cantidad`."""
    vacios = {f"xl/{i}.xml": "" for i in range(cantidad - len(LIBRO_CON_TIPOS))}
    return _ooxml(LIBRO_CON_TIPOS | vacios)


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
def test_un_libro_entra_con_1000_miembros_y_con_1001_es_bomba(tmp_path):
    almacen = AlmacenDeOriginales(tmp_path)

    justo = almacen.guardar_en_streaming(io.BytesIO(_libro_con_miembros(1000)), nombre="a.xlsx")
    with pytest.raises(ArchivoIlegible) as rechazo:
        almacen.guardar_en_streaming(io.BytesIO(_libro_con_miembros(1001)), nombre="a.xlsx")

    assert justo.clase is ClaseDeOriginal.PLANILLA
    assert rechazo.value.motivo == "bomba-de-compresion"


def _libro_que_declara_1_kib_y_descomprime_200_mb():
    datos = _con_ceros("xl/workbook.xml", 200)
    for firma, desplazamiento in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        posicion = datos.rindex(firma) + desplazamiento
        datos[posicion : posicion + 4] = (1024).to_bytes(4, "little")
    return bytes(datos)


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(94)
@pytest.mark.historia(219)
@pytest.mark.parametrize(
    "bomba",
    [
        pytest.param(
            _libro_que_declara_1_kib_y_descomprime_200_mb, id="declara-1-kib-y-son-200-mb"
        ),
        pytest.param(lambda: _libro_con_miembros(5000), id="5000-miembros"),
        pytest.param(lambda: bytes(_con_ceros("xl/workbook.xml", 20)), id="razon-1000-a-1"),
    ],
)
def test_seg_zip_bomba_en_planilla(cliente, directorio_originales, bomba):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "notas.xlsx", bomba(), XLSX)

    assert respuesta.status_code == 422, respuesta.text
    problema = respuesta.json()
    assert (problema["type"], problema["motivo"]) == (
        "/errores/archivo-ilegible",
        "bomba-de-compresion",
    )
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []


ENTIDADES_ANIDADAS = "".join(
    f'<!ENTITY e{n} "{f"&e{n - 1};" * 10 if n else "a"}">' for n in range(10)
)
"""Mil millones de «a» si alguien expandiera `&e9;`."""


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(219)
def test_seg_un_xlsx_con_entidades_anidadas_en_sus_tipos_responde_422(cliente):
    tipos = f"<!DOCTYPE Types [{ENTIDADES_ANIDADAS}]>" + _tipos_libro(parte="&e9;")
    _con_sesion(cliente)

    respuesta = _subir(cliente, "notas.xlsx", _ooxml(LIBRO | {"[Content_Types].xml": tipos}), XLSX)

    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["type"] == "/errores/archivo-ilegible"


def _csv_con_campo(largo, *, desde=0):
    """Un CSV con un campo de `largo` caracteres después de unos `desde` bytes de registros."""
    return b"a;b\r\n" * (1 + desde // 5) + b'x;"' + b"y" * largo + b'"\r\n'


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(97)
@pytest.mark.parametrize(
    "desde",
    [
        pytest.param(0, id="dentro-del-primer-bloque"),
        pytest.param(BLOQUE_BYTES - 1000, id="cruza-el-primer-bloque"),
        pytest.param(2 * BLOQUE_BYTES, id="despues-del-primer-bloque"),
    ],
)
def test_un_csv_con_un_campo_de_mas_de_32_kb_se_rechaza_en_la_admision(tmp_path, desde):
    almacen = AlmacenDeOriginales(tmp_path)

    justo = almacen.guardar_en_streaming(
        io.BytesIO(_csv_con_campo(32_000, desde=desde)), nombre="a.csv"
    )
    with pytest.raises(ArchivoIlegible) as rechazo:
        almacen.guardar_en_streaming(
            io.BytesIO(_csv_con_campo(32_001, desde=desde)), nombre="a.csv"
        )

    assert justo.clase is ClaseDeOriginal.PLANILLA
    assert rechazo.value.motivo == "fuera-de-topes"
    assert "32 KB" in str(rechazo.value)
    assert archivos(tmp_path) == [justo.ruta]


@pytest.mark.rf("RF-38")
@pytest.mark.rnf("RNF-20")
@pytest.mark.historia(97)
def test_seg_un_csv_con_un_campo_de_mas_de_32_kb_responde_422(cliente, directorio_originales):
    _con_sesion(cliente)

    respuesta = _subir(cliente, "enorme.csv", _csv_con_campo(200_000), "text/csv")

    assert respuesta.status_code == 422, respuesta.text
    problema = respuesta.json()
    assert (problema["type"], problema["motivo"]) == ("/errores/archivo-ilegible", "fuera-de-topes")
    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert archivos(directorio_originales) == []
