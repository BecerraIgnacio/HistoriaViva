"""Admitir, guardar y servir los bytes de un original, siempre en streaming (DEC-47, DEC-48).

Es el único lugar que conoce firmas, topes por clase y temporales: quien sube entrega un
flujo y recibe un `OriginalTemporal` ya admitido, con su clase y su SHA-256. El original nunca
está entero en memoria: se lee de a 1 MiB a un temporal en el mismo volumen que los originales,
así `confirmar` es un `os.replace` atómico.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import re
import shutil
import tempfile
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, BinaryIO, Literal, Protocol
from urllib.parse import quote
from uuid import UUID, uuid4

from lxml import etree
from PIL import Image

from historia_viva.dominio.errores import ErrorHistoriaViva
from historia_viva.dominio.originales import (
    CONVERTIDO_DE,
    CSV,
    DOC,
    DOCX,
    HEIC,
    JPEG,
    TEXTO_PLANO,
    TOPE_BYTES,
    XLS,
    XLSX,
    ClaseDeOriginal,
)
from historia_viva.dominio.tipos import OriginalGuardado
from historia_viva.planillas import clasificar

__all__ = [
    "BLOQUE_BYTES",
    "MARGEN_LIBRE_BYTES",
    "POLITICAS",
    "AlmacenDeOriginales",
    "AlmacenamientoInsuficiente",
    "ArchivoDemasiadoGrande",
    "ArchivoIlegible",
    "ConversionFallida",
    "Conversor",
    "OriginalNoDisponible",
    "OriginalTemporal",
    "PoliticaDeAdmision",
    "RechazoDeAdmision",
    "RespuestaDeOriginal",
    "TOPES_DE_ZIP",
    "TipoNoAdmitido",
    "TopesDeZip",
]

BLOQUE_BYTES = 1024 * 1024
"""Lo máximo que se le pide al flujo por lectura (RNF-15)."""

FIRMA_BYTES = 4 * 1024
"""La clase y el MIME se deciden por estos primeros bytes, nunca por el nombre ni el
`Content-Type` del cliente."""

MARGEN_LIBRE_BYTES = 2 * 1024**3
"""Lo que tiene que sobrar en el volumen además del archivo (DEC-112)."""

_PREFIJO = "local://originales/"
_SUBIDAS = ".subidas"
_MARCA = ".confirmando"
_ADMITIDOS = (
    "Historia Viva acepta fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv) y texto (.txt)."
)

_FIRMAS: tuple[tuple[bytes, str, ClaseDeOriginal], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", ClaseDeOriginal.IMAGEN),
    (b"\x89PNG\r\n\x1a\n", "image/png", ClaseDeOriginal.IMAGEN),
    (b"II*\x00", "image/tiff", ClaseDeOriginal.IMAGEN),
    (b"MM\x00*", "image/tiff", ClaseDeOriginal.IMAGEN),
    (b"%PDF-", "application/pdf", ClaseDeOriginal.DOCUMENTO),
    # Un ZIP es Word solo si trae `word/document.xml` y pasa la guarda: eso lo dice el
    # directorio, al final del archivo, y se mira sobre el temporal ya escrito.
    (b"PK\x03\x04", DOCX, ClaseDeOriginal.DOCUMENTO),
)

_FIRMA_EBML = b"\x1a\x45\xdf\xa3"
"""WebM y MKV: el DocType del encabezado EBML los distingue."""
_MARCAS_DE_IMAGEN = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
)
"""Las marcas `ftyp` de HEIF y AVIF: son fotos, no videos."""
_MARCAS_HEIC = _MARCAS_DE_IMAGEN - {b"avif", b"avis"}
"""Las de HEIF, la foto del iPhone: entra convertida a JPEG (153). AVIF sigue sin entrar."""
_VIDEOS = ("video/mp4", "video/quicktime", "video/webm")
_VIDEOS_ADMITIDOS = "Historia Viva acepta videos MP4, MOV y WebM."
_SOLO_VIDEO = f"Por la carga de video solo entran videos. {_VIDEOS_ADMITIDOS}"
_VIDEO_POR_SU_PUERTA = "Los videos se suben por la carga de video, no por la de archivos."

_MIMES_ADMITIDOS = (
    *dict.fromkeys(tipo_mime for _, tipo_mime, _ in _FIRMAS),
    XLSX,
    CSV,
    TEXTO_PLANO,
)
"""Lo que dice `admitidos` en un 415: encender una clase en `POLITICAS` es sumar acá sus MIME."""

_APAGADA = {
    ClaseDeOriginal.PLANILLA: (
        "Las planillas (.xlsx, .csv, .tsv) todavía no se pueden cargar; se habilitan más adelante."
    ),
}
"""El aviso de una clase que la política apaga, dicho a la persona."""
_MotivoCsv = Literal["sin-hojas", "csv-binario", "codificacion-desconocida", "fuera-de-topes"]
"""Los motivos de `planillas.clasificar`: si suma uno, mypy rechaza indexar `_CSV_ILEGIBLE`."""
_CSV_ILEGIBLE: dict[_MotivoCsv, str] = {
    "sin-hojas": "El CSV está vacío: no trae ningún registro.",
    "csv-binario": (
        "El archivo dice ser un CSV pero no es texto. Exportalo de nuevo como CSV y subilo."
    ),
    "codificacion-desconocida": (
        "No se reconoce la codificación del CSV. Guardalo como «CSV UTF-8» y subilo de nuevo."
    ),
    "fuera-de-topes": (
        "El CSV tiene un campo de más de 32 KB. Partí ese texto en varias celdas o subilo"
        " como documento."
    ),
}
"""Lo que `planillas.clasificar` rechaza (motivos de `archivo-ilegible`), dicho a la persona."""
_CHARSET = {"utf-8-sig": "utf-8", "cp1252": "windows-1252", "latin-1": "iso-8859-1"}
"""Del códec de Python al nombre IANA del `charset`; `utf-8` y `utf-16` ya lo son."""

_FIRMA_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CFB = "application/x-cfb"
"""OLE2: `.xls`, `.doc` u Office cifrado. Se escribe y su directorio dice qué es: el `.doc` y el
`.xls` entran si el servidor los convierte (153, DEC-41 revisada); el resto no entra nunca."""
_EXTENSION = {DOC: ".doc", XLS: ".xls", HEIC: ".heic", DOCX: ".docx", XLSX: ".xlsx", JPEG: ".jpg"}
"""LibreOffice elige el filtro por la extensión: los temporales de la conversión la llevan."""
_CLASE_CONVERTIBLE = {
    DOC: ClaseDeOriginal.DOCUMENTO,
    XLS: ClaseDeOriginal.PLANILLA,
    HEIC: ClaseDeOriginal.IMAGEN,
}
_CONVERTIDOS = ".convertidos"
"""El JPEG de una foto HEIC, que es lo que muestra el visor: un derivado, no un original. No va
a Drive y la supresión lo borra con el original (runbook R14)."""
_SIN_CONVERSOR = {
    DOC: (
        "Es un documento de Word de formato viejo (.doc) y este servidor no lo puede convertir. "
        "Guardalo como .docx desde Word o LibreOffice y subilo de nuevo."
    ),
    XLS: (
        "Es una planilla de Excel en formato binario (.xls) y este servidor no la puede "
        "convertir. Guardala como .xlsx desde Excel o LibreOffice y subila de nuevo."
    ),
    HEIC: (
        "Es una foto HEIC (la del iPhone) y este servidor no la puede convertir. Exportala como "
        "JPEG (en el iPhone: Ajustes > Cámara > Formatos > Más compatible) y subila de nuevo."
    ),
}
_VBA_VIEJO = {
    ClaseDeOriginal.DOCUMENTO: (
        "Es un documento de Word (.doc) con macros. Guardalo como .docx, sin macros, y subilo "
        "de nuevo."
    ),
    ClaseDeOriginal.PLANILLA: (
        "Es una planilla de Excel (.xls) con macros. Guardala como .xlsx, sin macros, y subila "
        "de nuevo."
    ),
}

_CIFRADO = (
    "El archivo está protegido con contraseña. "
    "Quitale la contraseña desde Word o Excel y subilo de nuevo."
)
_PARTES_EXTERNAS = (
    "El documento de Word trae contenido vinculado desde afuera (una plantilla, un objeto, "
    "un marco o una imagen). Quitale los vínculos, guardalo como .docx y subilo de nuevo."
)
_XLS_BINARIO = (
    "Es una planilla de Excel en formato binario (.xls o .xlsb). "
    "Guardala como .xlsx y subila de nuevo."
)
_CON_MACROS = {
    ClaseDeOriginal.DOCUMENTO: (
        "Es un documento de Word con macros (.docm). "
        "Guardalo como .docx, sin macros, y subilo de nuevo."
    ),
    ClaseDeOriginal.PLANILLA: (
        "Es una planilla de Excel con macros (.xlsm). "
        "Guardala como .xlsx, sin macros, y subila de nuevo."
    ),
}


@dataclass(frozen=True)
class PoliticaDeAdmision:
    tope_bytes: int
    """Exactamente el tope se acepta; un byte más es 413."""
    admitida: bool = True
    """Una clase apagada se reconoce y se rechaza con 415, sin escribir nada."""


POLITICAS: Mapping[ClaseDeOriginal, PoliticaDeAdmision] = {
    ClaseDeOriginal.IMAGEN: PoliticaDeAdmision(tope_bytes=TOPE_BYTES),
    ClaseDeOriginal.DOCUMENTO: PoliticaDeAdmision(tope_bytes=TOPE_BYTES),
    ClaseDeOriginal.PLANILLA: PoliticaDeAdmision(tope_bytes=10 * 1000 * 1000),
    ClaseDeOriginal.TEXTO: PoliticaDeAdmision(tope_bytes=256 * 1024),
    ClaseDeOriginal.VIDEO: PoliticaDeAdmision(tope_bytes=2 * 1024**3),
}
"""Topes por clase (DEC-15, DEC-42, DEC-145): la planilla en MB, el resto en binario."""


@dataclass(frozen=True)
class TopesDeZip:
    miembros: int
    descomprimido_bytes: int
    """Contados leyendo, nunca creyendo lo que el ZIP declara."""
    razon: int
    """Bytes descomprimidos por byte del ZIP."""


TOPES_DE_ZIP: Mapping[ClaseDeOriginal, TopesDeZip] = {
    ClaseDeOriginal.DOCUMENTO: TopesDeZip(
        miembros=2000, descomprimido_bytes=150_000_000, razon=200
    ),
    ClaseDeOriginal.PLANILLA: TopesDeZip(miembros=1000, descomprimido_bytes=100_000_000, razon=100),
}
"""La guarda de ZIP por clase (DEC-95): el documento es el Word."""

_TIPO_WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_TIPO_LIBRO = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_OPC = "{http://schemas.openxmlformats.org/package/2006/"
_HIPERVINCULOS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink",  # OOXML strict
)
_MIEMBROS_MAXIMOS = max(topes.miembros for topes in TOPES_DE_ZIP.values())
_BLOQUE_ZIP = 64 * 1024
_SIN_TOPE = 2**62


@dataclass(frozen=True)
class OriginalTemporal:
    """Un original admitido y escrito entero, que todavía no tiene Pieza."""

    ruta: Path
    tamano_bytes: int
    sha256: str
    tipo_mime: str
    clase: ClaseDeOriginal


@dataclass(frozen=True)
class RespuestaDeOriginal:
    """Lo que `abrir` sirve: 200, 206 o 416, sus cabeceras y los bytes de a 1 MiB."""

    estado: int
    cabeceras: dict[str, str]
    bloques: Iterator[bytes]


class RechazoDeAdmision(ErrorHistoriaViva):
    """Un original que no se admite, con lo que se llegó a saber de él para `archivo.rechazo`
    (DEC-106): la clase si se reconoció, y tamaño y SHA-256 si se leyó entero. `motivo`, si lo
    hay, es el del catálogo."""

    def __init__(self, detalle: str, *, clase: ClaseDeOriginal | None = None) -> None:
        super().__init__(detalle)
        self.motivo: str | None = None
        self.clase = clase
        self.tamano_bytes: int | None = None
        self.sha256: str | None = None


class TipoNoAdmitido(RechazoDeAdmision):
    """El contenido no es de una clase admitida; el texto es para la persona.

    `motivo` sale de la lista cerrada del catálogo (`xls-binario`, `con-macros`, `cifrada`,
    `contenedor-no-admitido`); un formato desconocido no lleva ninguno.
    """

    def __init__(
        self,
        detalle: str,
        *,
        motivo: str | None = None,
        clase: ClaseDeOriginal | None = None,
        admitidos: tuple[str, ...] = _MIMES_ADMITIDOS,
    ) -> None:
        super().__init__(detalle, clase=clase)
        self.motivo = motivo
        self.admitidos = admitidos


class ArchivoDemasiadoGrande(RechazoDeAdmision):
    def __init__(self, clase: ClaseDeOriginal, tope_bytes: int) -> None:
        super().__init__(
            f"El archivo supera el tope de {_legible(tope_bytes)} para la clase {clase}.",
            clase=clase,
        )
        self.tope_bytes = tope_bytes


class ArchivoIlegible(RechazoDeAdmision):
    """La firma es admitida pero el contenido no se puede leer; el texto es para la persona.

    `motivo` sale de la lista cerrada del catálogo (`zip-corrupto`, `bomba-de-compresion`, …).
    """

    def __init__(self, detalle: str, *, motivo: str) -> None:
        super().__init__(detalle)
        self.motivo = motivo


class AlmacenamientoInsuficiente(ErrorHistoriaViva):
    def __init__(self) -> None:
        super().__init__(
            "No queda espacio en el disco de los originales para guardar este archivo."
        )


class OriginalNoDisponible(ErrorHistoriaViva):
    """La Pieza existe y su archivo no está en disco (DEC-112): se puede validar igual."""

    def __init__(self, archivo_original: str) -> None:
        super().__init__(
            f"El archivo original de esta Pieza no está en el disco ({archivo_original}). "
            "Podés validar igual, pero sin verlo."
        )
        self.archivo_original = archivo_original


class ConversionFallida(ErrorHistoriaViva):
    """El conversor no dejó un convertido: falló, se pasó de su tope o no está instalado. El
    texto es para quien subió la Pieza: el procesamiento falla con él."""


class Conversor(Protocol):
    """Convierte un `.doc`, un `.xls` o un HEIC aislado (153); lo inyecta `composicion`."""

    formatos: frozenset[str]
    """Los MIME de `CONVERTIDO_DE` que puede convertir en este servidor; vacío, ninguno."""

    def convertir(self, origen: Path, tipo_mime: str, destino: Path) -> None:
        """Escribe en `destino` el convertido de `origen`, en un proceso aparte.

        Raises:
            ConversionFallida: Si no lo pudo convertir dentro de sus topes.
        """
        ...


class AlmacenDeOriginales:
    def __init__(
        self,
        raiz: Path,
        politicas: Mapping[ClaseDeOriginal, PoliticaDeAdmision] = POLITICAS,
        conversor: Conversor | None = None,
    ) -> None:
        """Sin `conversor`, el `.doc`, el `.xls` y el HEIC siguen dando 415 con qué hacer."""
        self._raiz = raiz
        self._subidas = raiz / _SUBIDAS
        self._politicas = politicas
        self._conversor = conversor

    def _convierte(self, tipo_mime: str) -> bool:
        return self._conversor is not None and tipo_mime in self._conversor.formatos

    def guardar_en_streaming(
        self,
        flujo: BinaryIO,
        *,
        nombre: str = "",
        tamano_minimo: int = 0,
        solo_video: bool = False,
    ) -> OriginalTemporal:
        """Admite el original por su contenido y lo escribe a un temporal de a 1 MiB.

        `nombre` solo cuenta sin firma: un CSV o TSV se reconoce por su extensión y su
        contenido (DEC-43).

        `tamano_minimo` es lo que el pedido declara que trae como mínimo (su `Content-Length`
        menos el sobre): si ya supera el tope de la clase, se rechaza sin escribir nada.

        El video tiene su propia puerta (`solo_video`) y no entra por la de archivos; la
        puerta equivocada se rechaza por el primer bloque, sin escribir nada.

        Raises:
            TipoNoAdmitido: Si la firma no es de una clase admitida, su clase está apagada en
                la política, es Office con macros o cifrado (DEC-41), es un `.doc`, un `.xls`
                o un HEIC y no hay conversor (153) o es un `.txt` sin texto.
            ArchivoDemasiadoGrande: Si lo declarado o lo leído pasa el tope de su clase.
            ArchivoIlegible: Si es un ZIP dañado, una bomba de descompresión (DEC-95) o un CSV
                vacío, binario o sin codificación, también pasada la muestra (DEC-43).
        """
        bloque = primero = flujo.read(BLOQUE_BYTES)
        if solo_video:
            video = _detectar_video(bloque[:FIRMA_BYTES])
            if video is None:
                raise TipoNoAdmitido(_SOLO_VIDEO, admitidos=_VIDEOS)
            tipo_mime, clase = video, ClaseDeOriginal.VIDEO
        else:
            tipo_mime, clase = _detectar(bloque, nombre)
            if clase is ClaseDeOriginal.VIDEO:
                raise TipoNoAdmitido(_VIDEO_POR_SU_PUERTA, clase=clase)
            if tipo_mime == HEIC and not self._convierte(HEIC):
                raise TipoNoAdmitido(_SIN_CONVERSOR[HEIC], clase=clase)
        self._exigir_admitida(clase)
        tope = self._politicas[clase].tope_bytes
        if tamano_minimo > tope:
            raise ArchivoDemasiadoGrande(clase, tope)
        self._subidas.mkdir(parents=True, exist_ok=True)
        ruta = self._subidas / f"{uuid4()}.parcial"
        huella = hashlib.sha256()
        total = 0
        leido = False
        try:
            with ruta.open("xb") as destino:
                while bloque:
                    total += len(bloque)
                    if total > tope:
                        raise ArchivoDemasiadoGrande(clase, tope)
                    huella.update(bloque)
                    destino.write(bloque)
                    bloque = flujo.read(BLOQUE_BYTES)
                destino.flush()
                os.fsync(destino.fileno())
            leido = True
            if tipo_mime in (DOCX, _CFB):
                if tipo_mime == DOCX:
                    clase = _admitir_zip(ruta, total)
                    tipo_mime = XLSX if clase is ClaseDeOriginal.PLANILLA else DOCX
                else:
                    tipo_mime = _admitir_cfb(ruta, self._convierte)
                    clase = _CLASE_CONVERTIBLE[tipo_mime]
                self._exigir_admitida(clase)
                # La firma ZIP y la OLE2 se escriben con el tope del Word: el del libro se mide
                # ahora.
                if total > self._politicas[clase].tope_bytes:
                    raise ArchivoDemasiadoGrande(clase, self._politicas[clase].tope_bytes)
            elif tipo_mime == CSV and total > len(primero):
                # El primer bloque no era el archivo entero: lo que siguió cumple la
                # codificación de la muestra o el CSV se rechaza (DEC-43).
                with ruta.open("rb") as origen:
                    _exigir_csv_legible(clasificar(origen, nombre).motivo_rechazo)
        except BaseException as error:
            ruta.unlink(missing_ok=True)
            # Sin `Content-Length` el disco se puede llenar recién al escribir.
            if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                raise AlmacenamientoInsuficiente() from error
            if isinstance(error, RechazoDeAdmision) and leido:
                error.tamano_bytes, error.sha256 = total, huella.hexdigest()
            raise
        return OriginalTemporal(ruta, total, huella.hexdigest(), tipo_mime, clase)

    def guardar_texto(self, texto: str) -> OriginalTemporal:
        """El texto de un Aporte como original `text/plain; charset=utf-8`, a un temporal.

        No pasa por la detección: lo mandó un Agente, no es un archivo, y un texto que empezara
        con una firma (`%PDF-`) no puede volverse otra clase. El tope lo puso quien lo recibió.
        """
        contenido = texto.encode("utf-8")
        self._subidas.mkdir(parents=True, exist_ok=True)
        ruta = self._subidas / f"{uuid4()}.parcial"
        try:
            with ruta.open("xb") as destino:
                destino.write(contenido)
                destino.flush()
                os.fsync(destino.fileno())
        except BaseException as error:
            ruta.unlink(missing_ok=True)
            if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                raise AlmacenamientoInsuficiente() from error
            raise
        return OriginalTemporal(
            ruta,
            len(contenido),
            hashlib.sha256(contenido).hexdigest(),
            f"{TEXTO_PLANO}; charset=utf-8",
            ClaseDeOriginal.TEXTO,
        )

    def _exigir_admitida(self, clase: ClaseDeOriginal) -> None:
        if not self._politicas[clase].admitida:
            aviso = _APAGADA.get(clase, "Ese archivo todavía no se puede cargar.")
            raise TipoNoAdmitido(f"{aviso} {_ADMITIDOS}", clase=clase)

    def confirmar(self, temporal: OriginalTemporal, *, pieza_id: UUID) -> str:
        """Lleva el temporal a su lugar definitivo de un solo paso y devuelve su identificador.

        El original queda marcado hasta que `asentar` diga que su Pieza tiene `COMMIT`: la
        marca es lo único que distingue un corte entre `os.replace` y el `COMMIT` de un
        original sin Pieza por otra causa, que el barrido no puede tocar.
        """
        identificador = f"{_PREFIJO}{pieza_id}"
        try:
            self._marca(identificador).touch()
            _sincronizar_directorio(self._subidas)
            os.replace(temporal.ruta, self._ruta(identificador))
            _sincronizar_directorio(self._raiz)
        except BaseException:
            # Quien llama no recibe el identificador y solo descarta el temporal: lo que ya se
            # movió se borra acá. Todavía no hay `COMMIT`, así que no hay Pieza que lo pierda.
            self.descartar(identificador)
            raise
        return identificador

    def asentar(self, identificador: str) -> None:
        """Quita la marca de un original confirmado cuya Pieza ya tiene `COMMIT`."""
        self._marca(identificador).unlink(missing_ok=True)

    def descartar(self, temporal_o_identificador: OriginalTemporal | str) -> None:
        """Borra un temporal o un original confirmado. Dos veces no es un error."""
        if isinstance(temporal_o_identificador, OriginalTemporal):
            temporal_o_identificador.ruta.unlink(missing_ok=True)
        else:
            self._ruta(temporal_o_identificador).unlink(missing_ok=True)
            self._convertido(temporal_o_identificador).unlink(missing_ok=True)
            self.asentar(temporal_o_identificador)

    def leer_para_procesar(self, original: OriginalGuardado) -> tuple[bytes, str]:
        """Los bytes que lee el procesamiento y su MIME (RF-05): los del original o, si es un
        `.doc`, un `.xls` o un HEIC, los de su convertido, que pasa por la misma guarda que
        uno subido: `_admitir_zip` el `.docx` y el `.xlsx`, el tope de píxeles de Pillow el
        JPEG, y el tope de bytes de su clase antes y después de convertir (153).

        El original no se toca. El convertido queda como derivado: el JPEG, para el visor
        (`abrir`); el `.docx` y el `.xlsx`, para la miniatura de `web` (`leer_convertido`).

        Raises:
            OriginalNoDisponible: Si el archivo no está en disco (DEC-112).
            ConversionFallida: Si el conversor no está o no pudo convertirlo.
            RechazoDeAdmision: Si el convertido no pasa la guarda.
        """
        destino = CONVERTIDO_DE.get(original.tipo_mime)
        if destino is None:
            return b"".join(self.abrir(original).bloques), original.tipo_mime
        if self._conversor is None or not self._convierte(original.tipo_mime):
            raise ConversionFallida(_SIN_CONVERSOR[original.tipo_mime])
        clase = _CLASE_CONVERTIBLE[original.tipo_mime]
        tope = self._politicas[clase].tope_bytes
        self._subidas.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._subidas, ignore_cleanup_errors=True) as aparte:
            entrada = Path(aparte) / f"original{_EXTENSION[original.tipo_mime]}"
            salida = Path(aparte) / f"convertido{_EXTENSION[destino]}"
            try:
                shutil.copyfile(self._ruta(original.identificador), entrada)
            except FileNotFoundError as error:
                raise OriginalNoDisponible(original.identificador) from error
            if entrada.stat().st_size > tope:
                raise ArchivoDemasiadoGrande(clase, tope)
            self._conversor.convertir(entrada, original.tipo_mime, salida)
            if not salida.is_file():
                raise ConversionFallida("El conversor terminó sin dejar el archivo convertido.")
            tamano = salida.stat().st_size
            if tamano > tope:
                raise ArchivoDemasiadoGrande(clase, tope)
            if destino == JPEG:
                _exigir_jpeg(salida)
            elif _admitir_zip(salida, tamano) is not clase:
                raise ConversionFallida("El convertido no es de la clase del original.")
            contenido = salida.read_bytes()
            derivado = self._convertido(original.identificador)
            derivado.parent.mkdir(exist_ok=True)
            os.replace(salida, derivado)
        return contenido, destino

    def leer_convertido(self, original: OriginalGuardado) -> tuple[bytes, str]:
        """Lo mismo que `leer_para_procesar`, sin convertir: el derivado que dejó el
        procesamiento. `web` no corre LibreOffice por un `GET` (SEG-14).

        Raises:
            OriginalNoDisponible: Si el original no está en disco (DEC-112).
            ConversionFallida: Si el procesamiento todavía no dejó el derivado.
        """
        destino = CONVERTIDO_DE.get(original.tipo_mime)
        if destino is None:
            return b"".join(self.abrir(original).bloques), original.tipo_mime
        try:
            return self._convertido(original.identificador).read_bytes(), destino
        except FileNotFoundError as error:
            raise ConversionFallida(
                "Todavía no está el convertido que deja el procesamiento."
            ) from error

    def _convertido(self, identificador: str) -> Path:
        return self._raiz / _CONVERTIDOS / identificador.removeprefix(_PREFIJO)

    def abrir(
        self,
        original: OriginalGuardado,
        rango: str | None = None,
        *,
        si_rango: str | None = None,
        descarga: bool = False,
        convertido: bool = False,
    ) -> RespuestaDeOriginal:
        """Sirve el original entero o el tramo de `rango` (la cabecera `Range`), de a 1 MiB.

        Un `Range` que no se entiende o con varios tramos se ignora y va el original entero
        (RFC 9110 §14.2); uno que el original no tiene es 416. Con `si_rango` (`If-Range`) el
        tramo va solo si es el `ETag`; una fecha nunca coincide, porque no hay
        `Last-Modified`. Con `descarga`, el original va como adjunto aunque se vea en línea.
        Con `convertido`, si hay un derivado (el JPEG de un HEIC, 153) va ese, que es lo que el
        navegador muestra; la réplica y la descarga piden siempre el original.

        Raises:
            OriginalNoDisponible: Si el archivo no está en disco (DEC-112).
        """
        ruta, nombre = self._ruta(original.identificador), original.nombre_archivo
        tipo_mime, huella = original.tipo_mime, original.huella
        derivado = self._convertido(original.identificador)
        if convertido and CONVERTIDO_DE.get(tipo_mime) == JPEG and derivado.is_file():
            tipo_mime = CONVERTIDO_DE[tipo_mime]
            ruta, nombre = derivado, f"{Path(nombre).stem}{_EXTENSION[tipo_mime]}"
            if huella:  # otros bytes, otro `ETag`
                huella = f"{huella}-convertido"
        cabeceras = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": _disposicion(nombre, tipo_mime, adjunto=descarga),
            "X-Content-Type-Options": "nosniff",
        }
        if huella:  # un original no cambia nunca (ADR-0001): `ETag` fuerte
            cabeceras["ETag"] = f'"{huella}"'
        if si_rango is not None and si_rango.strip() != cabeceras.get("ETag"):
            rango = None  # RFC 9110 §13.1.5: lo que tiene el cliente no es este original
        try:
            archivo = ruta.open("rb")
        except FileNotFoundError as error:
            raise OriginalNoDisponible(original.identificador) from error
        try:
            total = os.fstat(archivo.fileno()).st_size
            tramo = _resolver_rango(rango, total)
        except _RangoNoSatisfacible:
            archivo.close()
            cabeceras |= {"Content-Range": f"bytes */{total}", "Content-Length": "0"}
            return RespuestaDeOriginal(416, cabeceras, iter(()))
        except BaseException:
            archivo.close()  # abierto, Windows no deja borrar el original
            raise
        inicio, fin = tramo or (0, total - 1)
        cabeceras["Content-Length"] = str(fin + 1 - inicio)
        cabeceras["Content-Type"] = tipo_mime
        if tramo is not None:
            cabeceras["Content-Range"] = f"bytes {inicio}-{fin}/{total}"
        return RespuestaDeOriginal(
            200 if tramo is None else 206, cabeceras, _bloques(archivo, inicio, fin + 1 - inicio)
        )

    def _ruta(self, identificador: str) -> Path:
        return self._raiz / identificador.removeprefix(_PREFIJO)

    def guardados(self) -> set[str]:
        """Los identificadores de los originales en disco, sin las subidas en curso ni sus
        marcas: lo que `verificar-originales` cruza con la base (spec §2.3)."""
        if not self._raiz.is_dir():
            return set()
        return {f"{_PREFIJO}{ruta.name}" for ruta in self._raiz.iterdir() if ruta.is_file()}

    def barrer_huerfanos(
        self, existentes: Callable[[], set[str]], *, mas_viejos_que: timedelta
    ) -> int:
        """Borra lo de `.subidas/` cuya última escritura es anterior al plazo (DEC-106).

        Mira el `mtime`, no la creación: una subida lenta que sigue escribiendo no se toca. Un
        original confirmado se borra solo si su marca quedó vieja y no tiene Pieza: un corte
        entre `os.replace` y el `COMMIT`. Uno sin marca nunca se toca aunque no tenga Pieza:
        con la base vacía o restaurada más vieja que los originales, es lo único que queda.
        Lo registrado se pide después de listar, así una Pieza asentada entre medio ya figura.
        Un directorio viejo es el de una conversión cuyo trabajador murió a mitad (153): se
        borra entero. Devuelve cuántos huérfanos borró: temporales, conversiones y originales;
        las marcas no cuentan.
        """
        if not self._subidas.is_dir():
            return 0
        limite = time.time() - mas_viejos_que.total_seconds()
        viejos = [
            ruta
            for ruta in self._subidas.iterdir()
            if (ruta.is_file() or ruta.is_dir()) and ruta.stat().st_mtime < limite
        ]
        registrados = existentes()
        borrados = 0
        for ruta in viejos:
            if ruta.is_dir():
                shutil.rmtree(ruta, ignore_errors=True)
                borrados += 1
                continue
            identificador = f"{_PREFIJO}{ruta.stem}"
            if ruta.suffix != _MARCA:
                borrados += 1
            elif identificador not in registrados:
                self._ruta(identificador).unlink(missing_ok=True)
                borrados += 1
            ruta.unlink(missing_ok=True)
        return borrados

    def _marca(self, identificador: str) -> Path:
        return self._subidas / f"{identificador.removeprefix(_PREFIJO)}{_MARCA}"

    def espacio_libre(self) -> int:
        self._raiz.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self._raiz).free

    def exigir_espacio(self, content_length: int) -> None:
        """Raises: AlmacenamientoInsuficiente si no sobran 2 GiB además de lo que llega."""
        if self.espacio_libre() < content_length + MARGEN_LIBRE_BYTES:
            raise AlmacenamientoInsuficiente()


def _bloques(archivo: BinaryIO, inicio: int, largo: int) -> Iterator[bytes]:
    """El tramo de a 1 MiB: el original nunca está entero en memoria (RNF-15)."""
    with archivo:
        archivo.seek(inicio)
        while largo > 0 and (bloque := archivo.read(min(BLOQUE_BYTES, largo))):
            largo -= len(bloque)
            yield bloque


_EN_LINEA = ("image/", "video/", "application/pdf")
"""Lo que el navegador muestra sin ejecutar; el resto se baja (DEC-45)."""

_NOMBRE_MAXIMO = 255


def _disposicion(nombre: str, tipo_mime: str, *, adjunto: bool = False) -> str:
    """`Content-Disposition` con el nombre saneado (RFC 6266 y 8187): el nombre lo eligió
    quien subió el archivo y no puede inyectar cabeceras ni rutas."""
    limpio = _sin_peligro(nombre)[-_NOMBRE_MAXIMO:] or "archivo"  # la cola trae la extensión
    # NFKD antes de sanear: una comilla de ancho completo se vuelve `"` al pasar a ASCII.
    ascii_ = unicodedata.normalize("NFKD", limpio).encode("ascii", "ignore").decode()
    tipo = "inline" if tipo_mime.startswith(_EN_LINEA) and not adjunto else "attachment"
    return (
        f'{tipo}; filename="{_sin_peligro(ascii_) or "archivo"}"; '
        f"filename*=UTF-8''{quote(limpio, safe='')}"
    )


def _sin_peligro(nombre: str) -> str:
    """Sin control ni formato (NUL, saltos, RTL), sin separadores de ruta ni comillas."""
    return "".join(
        "_" if caracter in '/\\"' else caracter
        for caracter in nombre
        if unicodedata.category(caracter)[0] != "C"
        and unicodedata.category(caracter) not in ("Zl", "Zp")
    ).strip()


class _RangoNoSatisfacible(Exception):
    pass


# Hasta 19 dígitos ASCII: con más `int()` falla (pasados los 4300) y ningún original los tiene.
_RANGO = re.compile(
    r"^\s*bytes\s*=\s*([0-9]{0,19})\s*-\s*([0-9]{0,19})\s*$", re.IGNORECASE | re.ASCII
)


def _resolver_rango(cabecera: str | None, total: int) -> tuple[int, int] | None:
    """Los dos bytes, ambos incluidos, que delimitan un `Range`; `None` si no nombra uno que
    se sepa servir (no hay, otra unidad, varios tramos o mal escrito). El fin se recorta al
    último byte: un visor que pide de más recibe lo que hay.

    Raises:
        _RangoNoSatisfacible: Si se entiende pero empieza pasado el final, es `-0` o el
            original está vacío.
    """
    partes = _RANGO.match(cabecera or "")
    if partes is None or partes.group(1) == partes.group(2) == "":
        return None
    desde, hasta = partes.group(1), partes.group(2)
    if not desde:
        if int(hasta) == 0 or total == 0:  # cero bytes de cola, o de original, no nombran nada
            raise _RangoNoSatisfacible()
        return max(0, total - int(hasta)), total - 1
    inicio = int(desde)
    if inicio >= total:
        raise _RangoNoSatisfacible()
    if not hasta:
        return inicio, total - 1
    if int(hasta) < inicio:  # dado vuelta: se ignora como cualquier otro mal escrito
        return None
    return inicio, min(int(hasta), total - 1)


def _sincronizar_directorio(directorio: Path) -> None:
    """Asienta en disco el renombre: sin esto, un corte de luz después del `COMMIT` puede
    dejar la Pieza confirmada sin su original (historia 231)."""
    if not hasattr(os, "O_DIRECTORY"):  # Windows no abre directorios; NTFS registra el renombre
        return
    descriptor = os.open(directorio, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _detectar(bloque: bytes, nombre: str) -> tuple[str, ClaseDeOriginal]:
    cabecera = bloque[:FIRMA_BYTES]
    for firma, tipo_mime, clase in _FIRMAS:
        if cabecera.startswith(firma):
            return tipo_mime, clase
    if cabecera[4:8] == b"ftyp" and cabecera[8:12] in _MARCAS_HEIC:
        return HEIC, ClaseDeOriginal.IMAGEN
    video = _detectar_video(cabecera)
    if video is not None:
        return video, ClaseDeOriginal.VIDEO
    if cabecera.startswith(_FIRMA_CFB):
        return _CFB, ClaseDeOriginal.DOCUMENTO
    # Sin firma propia, el texto delimitado lo reconoce `planillas` (DEC-43, DEC-94). Su muestra
    # es menor que el bloque: así sabe si corta el archivo. Un bloque lleno puede no ser el
    # archivo entero y partir una letra al final: eso lo mira la segunda pasada, ya escrito.
    completo = len(bloque) < BLOQUE_BYTES
    clasificacion = clasificar(io.BytesIO(bloque), nombre, completo=completo)
    _exigir_csv_legible(clasificacion.motivo_rechazo)
    if clasificacion.vacio:
        raise TipoNoAdmitido("El archivo de texto está vacío: no trae nada para leer.")
    if clasificacion.tipo_mime == CSV:
        # Un CSV o TSV es una planilla de una hoja; el trabajador vuelve a detectar su
        # codificación y su separador con `planillas` (DEC-43).
        return CSV, ClaseDeOriginal.PLANILLA
    if clasificacion.tipo_mime == TEXTO_PLANO and clasificacion.codificacion is not None:
        # El `.txt` que sube una persona es una Pieza `texto` (decisión del usuario, 028); el
        # `charset` le dice al trabajador cómo leerla.
        charset = _CHARSET.get(clasificacion.codificacion, clasificacion.codificacion)
        return f"{TEXTO_PLANO}; charset={charset}", ClaseDeOriginal.TEXTO
    raise TipoNoAdmitido(f"Ese archivo no se puede procesar. {_ADMITIDOS}")


def _exigir_csv_legible(motivo: _MotivoCsv | None) -> None:
    if motivo is not None:
        raise ArchivoIlegible(_CSV_ILEGIBLE[motivo], motivo=motivo)


def _detectar_video(cabecera: bytes) -> str | None:
    """El MIME de un MP4, MOV o WebM (DEC-15); `None` si la firma no es de video.

    Raises:
        TipoNoAdmitido: Si es un AVI o un MKV.
    """
    if cabecera[4:8] == b"ftyp":
        marca = cabecera[8:12]
        if marca in _MARCAS_DE_IMAGEN:
            return None
        return "video/quicktime" if marca == b"qt  " else "video/mp4"
    if cabecera.startswith(_FIRMA_EBML):
        # shortcut: el DocType se busca con el largo de un byte que escriben ffmpeg y
        # mkvmerge; un WebM con otro largo se rechaza como MKV.
        if b"\x42\x82\x84webm" in cabecera[:64]:
            return "video/webm"
        raise _convertir_a_mp4("MKV")
    if cabecera.startswith(b"RIFF") and cabecera[8:12] == b"AVI ":
        raise _convertir_a_mp4("AVI")
    return None


def _convertir_a_mp4(formato: str) -> TipoNoAdmitido:
    return TipoNoAdmitido(
        f"Es un video {formato}: convertilo a MP4 y subilo de nuevo. {_VIDEOS_ADMITIDOS}",
        motivo="contenedor-no-admitido",
        clase=ClaseDeOriginal.VIDEO,
        admitidos=_VIDEOS,
    )


def _admitir_zip(ruta: Path, tamano: int) -> ClaseDeOriginal:
    """Un ZIP entra solo si es un Word sin macros, cifrado ni partes externas, o un libro de
    Excel sin macros ni cifrado, y pasa la guarda (DEC-95). Devuelve cuál de los dos es.

    Raises:
        TipoNoAdmitido: Si no es un Word o trae macros, cifrado o partes externas.
        ArchivoIlegible: Si el ZIP está dañado o es una bomba de descompresión.
    """
    with ruta.open("rb") as origen:
        # Cada miembro del directorio ocupa ~0,5 KB al abrirlo: se cuentan antes sus firmas,
        # una cota de arriba que solo pide 64 KiB de memoria.
        if _contar(origen, b"PK\x01\x02") > _MIEMBROS_MAXIMOS:
            raise _bomba()
        try:
            with zipfile.ZipFile(origen) as comprimido:
                return _clasificar_y_verificar(comprimido, tamano)
        except _ZIP_DANADO as error:
            raise _zip_corrupto(None) from error


# Un directorio o un miembro dañados no dan solo `BadZipFile`: un nombre marcado UTF-8 que no
# lo es da `UnicodeDecodeError` (un `ValueError`), un deflate roto `zlib.error`, un método o un
# desplazamiento imposibles, `NotImplementedError`, `OSError` o `EOFError`, y una parte XML con
# DTD, `ValueError`.
_ZIP_DANADO = (
    zipfile.BadZipFile,
    zlib.error,
    ValueError,
    OSError,
    EOFError,
    NotImplementedError,
    etree.XMLSyntaxError,
)


def _zip_corrupto(clase: ClaseDeOriginal | None) -> ArchivoIlegible:
    ilegible = ArchivoIlegible(
        "El archivo comprimido está dañado y no se puede abrir. "
        "Volvé a guardarlo desde Word y subilo de nuevo.",
        motivo="zip-corrupto",
    )
    ilegible.clase = clase
    return ilegible


def _clasificar_y_verificar(comprimido: zipfile.ZipFile, tamano: int) -> ClaseDeOriginal:
    """La clasificación de contenedores OOXML: por sus partes, nunca por la extensión."""
    nombres = set(comprimido.namelist())
    no_admitido = TipoNoAdmitido(
        f"Es un archivo comprimido y no un documento de Word. {_ADMITIDOS}",
        motivo="contenedor-no-admitido",
    )
    if "word/document.xml" in nombres:
        clase, principal, tipo = ClaseDeOriginal.DOCUMENTO, "/word/document.xml", _TIPO_WORD
    elif nombres & {"xl/workbook.xml", "xl/workbook.bin"}:
        clase, principal, tipo = ClaseDeOriginal.PLANILLA, "/xl/workbook.xml", _TIPO_LIBRO
        no_admitido = TipoNoAdmitido(
            "No es un libro de Excel (.xlsx). "
            "Guardalo como .xlsx desde Excel y subilo de nuevo.",
            motivo="contenedor-no-admitido",
        )
    else:
        raise no_admitido
    # Decidida la clase, todo rechazo la lleva a `archivo.rechazo`: un `.docm` es un documento.
    try:
        # OPC prohíbe nombres equivalentes sin distinguir mayúsculas: de dos, zipfile lee el
        # último y otro lector puede leer el primero, el que nadie revisó.
        if len(comprimido.infolist()) != len({nombre.lower() for nombre in nombres}):
            raise zipfile.BadZipFile("nombres de miembro repetidos")
        # Lo cifrado no se puede descomprimir: se rechaza antes de la guarda.
        if any(miembro.flag_bits & 0x1 for miembro in comprimido.infolist()):
            raise TipoNoAdmitido(_CIFRADO, motivo="cifrada")
        if "xl/workbook.bin" in nombres:
            raise TipoNoAdmitido(_XLS_BINARIO, motivo="xls-binario")
        _verificar_zip(comprimido, TOPES_DE_ZIP[clase], tamano)
        if _con_macros(comprimido, nombres):
            raise TipoNoAdmitido(_CON_MACROS[clase], motivo="con-macros")
        # Un `.dotx`, un `.xltx` o un libro sin su tipo principal no son lo que dicen ser.
        if "[Content_Types].xml" not in nombres or not any(
            declarado.get("PartName", "").lower() == principal
            and declarado.get("ContentType", "").lower() == tipo
            for declarado in _elementos(
                comprimido, "[Content_Types].xml", f"{_OPC}content-types}}Override"
            )
        ):
            raise no_admitido
        if clase is ClaseDeOriginal.PLANILLA:
            return clase
        # Un hipervínculo no trae nada al documento; una plantilla, un objeto, un marco o una
        # imagen vinculados sí, y Word los busca al abrir el original descargado.
        if any(
            relacion.get("TargetMode") == "External"
            and relacion.get("Type") not in _HIPERVINCULOS
            for nombre in nombres
            if nombre.lower().endswith(".rels")
            for relacion in _elementos(comprimido, nombre, f"{_OPC}relationships}}Relationship")
        ):
            raise TipoNoAdmitido(_PARTES_EXTERNAS, motivo="contenedor-no-admitido")
    except _ZIP_DANADO as error:
        raise _zip_corrupto(clase) from error
    except RechazoDeAdmision as rechazo:
        # Sin su tipo en `[Content_Types].xml` no es un Word ni un libro: no hay clase.
        if rechazo is not no_admitido:
            rechazo.clase = clase
        raise
    return clase


class _Atributos:
    """Destino del parser de lxml: sin árbol, guarda solo los atributos de cada `etiqueta`;
    texto, comentarios e instrucciones se descartan al leerlos."""

    def __init__(self, etiqueta: str) -> None:
        self.etiqueta = etiqueta
        self.leidos: list[dict[str, str]] = []

    def start(self, etiqueta: str, atributos: Mapping[str, str]) -> None:
        if etiqueta == self.etiqueta:
            self.leidos.append(dict(atributos))

    def doctype(self, *_: object) -> None:
        # OPC (ECMA-376 parte 2) prohíbe las DTD: sin DTD no hay entidades, ni XXE ni billion
        # laughs. lxml propaga esto desde `feed`, antes de leer el subconjunto interno.
        raise ValueError("una parte XML con DTD")

    def close(self) -> None:
        pass


# Un `.rels` real con miles de vínculos pesa cientos de KiB; libxml2 es cuadrático en los
# atributos de un elemento (1 MiB de atributos: 3 s de CPU; 512 KiB: menos de 1 s).
_TOPE_PARTE_XML = 512 * 1024


def _elementos(comprimido: zipfile.ZipFile, parte: str, etiqueta: str) -> Iterator[dict[str, str]]:
    """Los atributos de cada elemento `etiqueta` de una parte XML ya medida por la guarda.

    Se lee de a 64 KiB con un parser sin árbol: la memoria no crece con la parte. Una DTD o
    una parte mal formada dan `ValueError` o `XMLSyntaxError`.
    """
    destino = _Atributos(etiqueta)
    parser = etree.XMLParser(target=destino, resolve_entities=False, no_network=True)
    leidos = 0
    with comprimido.open(parte) as xml:
        while bloque := xml.read(_BLOQUE_ZIP):
            leidos += len(bloque)
            if leidos > _TOPE_PARTE_XML:
                raise ValueError(f"la parte {parte} supera {_TOPE_PARTE_XML} bytes")
            parser.feed(bloque)
            yield from destino.leidos
            destino.leidos.clear()
    parser.close()
    yield from destino.leidos


def _verificar_zip(comprimido: zipfile.ZipFile, topes: TopesDeZip, tamano: int) -> None:
    """Descomprime cada miembro de a 64 KiB contando los bytes reales, no los declarados.

    Raises: ArchivoIlegible con `motivo = bomba-de-compresion`.
    """
    miembros = comprimido.infolist()
    if len(miembros) > topes.miembros:
        raise _bomba()
    tope = min(topes.descomprimido_bytes, topes.razon * tamano)
    total = 0
    for miembro in miembros:
        if miembro.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            # bzip2 y LZMA se descomprimen sin cota por lectura; Office no los escribe.
            raise NotImplementedError(f"método de compresión {miembro.compress_type}")
        # zipfile corta en el tamaño declarado: una bomba que declara 1 KiB pasaría.
        miembro.file_size = _SIN_TOPE
        with comprimido.open(miembro) as descomprimido:
            while bloque := descomprimido.read(_BLOQUE_ZIP):
                total += len(bloque)
                if total > tope:
                    raise _bomba()


def _con_macros(comprimido: zipfile.ZipFile, nombres: set[str]) -> bool:
    """`.docm` y `.xlsm`: el original se conserva y se serviría de vuelta con sus macros."""
    if any(nombre.lower().endswith("vbaproject.bin") for nombre in nombres):
        return True
    if "[Content_Types].xml" not in nombres:
        return False
    with comprimido.open("[Content_Types].xml") as tipos:
        return _contar(tipos, b"macroEnabled") > 0


def _admitir_cfb(ruta: Path, convierte: Callable[[str], bool]) -> str:
    """Un contenedor OLE2 entra solo si es un `.doc` o un `.xls` sin cifrar, sin proyecto VBA
    y el servidor lo convierte (DEC-41 revisada, 153); por su directorio se sabe qué es y, si
    no entra, qué hacer. Devuelve su MIME.

    shortcut: el cifrado viejo (RC4 del `.doc` y el `FILEPASS` del `.xls`) no se ve en el
    directorio: lo descubre la conversión, que falla y deja el procesamiento fallido con qué
    hacer. Si hace falta rechazarlo en la subida, leer el FIB y el primer registro del libro.

    Raises:
        TipoNoAdmitido: Si está cifrado, trae macros, no es Word ni Excel o no se convierte.
    """
    with ruta.open("rb") as origen:
        if _nombra(origen, "EncryptedPackage"):
            raise TipoNoAdmitido(_CIFRADO, motivo="cifrada")
        if _nombra(origen, "WordDocument"):
            tipo_mime = DOC
        elif _nombra(origen, "Workbook") or _nombra(origen, "Book"):  # `Book`: Excel 5 y 95
            tipo_mime = XLS
        else:
            # PowerPoint, Outlook y el resto de OLE2 no son de una clase admitida: sin motivo.
            raise TipoNoAdmitido(f"Ese archivo no se puede procesar. {_ADMITIDOS}")
        clase = _CLASE_CONVERTIBLE[tipo_mime]
        # Sin extensión aparte para las macros, se rechaza como un `.docm`. El nombre puede
        # estar en el texto del documento: rechazar de más es el lado seguro.
        if _nombra(origen, "_VBA_PROJECT"):
            raise TipoNoAdmitido(_VBA_VIEJO[clase], motivo="con-macros", clase=clase)
    if not convierte(tipo_mime):
        # El catálogo no tiene motivo para el Word viejo: va sin motivo, con qué hacer.
        raise TipoNoAdmitido(
            _SIN_CONVERSOR[tipo_mime], motivo="xls-binario" if tipo_mime == XLS else None
        )
    return tipo_mime


def _nombra(origen: IO[bytes], entrada: str) -> bool:
    """Si el directorio OLE2 tiene una entrada con ese nombre, que va en UTF-16 y no distingue
    mayúsculas."""
    origen.seek(0)
    return _contar(origen, entrada.lower().encode("utf-16-le"), minusculas=True) > 0


def _contar(origen: IO[bytes], marca: bytes, *, minusculas: bool = False) -> int:
    """Cuántas veces aparece `marca` en el flujo, leído de a 64 KiB. Con `minusculas`, contra
    el flujo con sus bytes ASCII en minúscula (`marca` ya en minúscula): en UTF-16 eso también
    toca bytes que no son letras, lo que solo puede dar de más."""
    total = 0
    cola = b""
    while bloque := origen.read(_BLOQUE_ZIP):
        ventana = cola + bloque
        total += (ventana.lower() if minusculas else ventana).count(marca)
        cola = ventana[len(ventana) - len(marca) + 1 :]
    return total


def _exigir_jpeg(ruta: Path) -> None:
    """El JPEG convertido de un HEIC, medido por el tope de píxeles de Pillow sin decodificarlo.

    Raises:
        ConversionFallida: Si no es un JPEG o pasa de `Image.MAX_IMAGE_PIXELS`.
    """
    try:
        with Image.open(ruta) as imagen:
            formato, pixeles = imagen.format, imagen.width * imagen.height
    except (OSError, Image.DecompressionBombError) as error:
        raise ConversionFallida("La foto convertida no se puede abrir.") from error
    if formato != "JPEG":
        raise ConversionFallida("La foto convertida no es un JPEG.")
    if Image.MAX_IMAGE_PIXELS is not None and pixeles > Image.MAX_IMAGE_PIXELS:
        raise ConversionFallida(
            f"La foto tiene {pixeles} píxeles: pasa el tope de {Image.MAX_IMAGE_PIXELS}."
        )


def _bomba() -> ArchivoIlegible:
    return ArchivoIlegible(
        "El archivo se descomprime a mucho más de lo que ocupa un documento real; "
        "por seguridad no se abre.",
        motivo="bomba-de-compresion",
    )


def _legible(tope_bytes: int) -> str:
    for unidad, tamano in (("GiB", 1024**3), ("MiB", 1024**2), ("MB", 1000**2), ("KiB", 1024)):
        if tope_bytes % tamano == 0:
            return f"{tope_bytes // tamano} {unidad}"
    return f"{tope_bytes} bytes"
