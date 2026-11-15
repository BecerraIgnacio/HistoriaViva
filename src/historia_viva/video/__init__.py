"""Leer los datos técnicos de un video con `ffprobe` endurecido (DEC-16, ADR-0005).

Informa y no decide: el tope de 3 h lo aplica `ingesta`, que recibe `analizar` como callable
inyectado por `composicion` (DEC-94). Es el único módulo que corre `ffprobe`. Nada de lo que
sale del archivo va a un modelo: el video lo analiza una persona.

Lista blanca: solo números, nombres de códec y la fecha de creación parseada. Las etiquetas del
contenedor (`title`, `comment`, `encoder`) son texto de quien armó el archivo y se descartan.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from historia_viva.dominio.originales import Contenedor

__all__ = [
    "FfprobeNoDisponible",
    "FlujoDeAudio",
    "FlujoDeVideo",
    "MetadatosVideo",
    "VideoIlegible",
    "analizar",
    "disponible",
    "interpretar",
]

_FFPROBE = "ffprobe"
# Los topes de memoria y CPU del hijo los pone `prlimit` (util-linux); Windows no lo tiene.
_PRLIMIT: str | None = None if sys.platform == "win32" else "prlimit"
_CODEC = re.compile(r"^[a-z0-9_]{1,32}$")
_PERFIL = re.compile(r"^[A-Za-z0-9 ._-]{1,32}$")
_FLUJOS_MAXIMOS = 16
_SALIDA_MAXIMA = 256 * 1024
_MEMORIA_MAXIMA = 512 * 1024 * 1024
_CPU_MAXIMA_S = 10
_VIDEO_REPRODUCIBLE = frozenset({"h264", "vp8", "vp9", "av1"})
_AUDIO_REPRODUCIBLE = frozenset({"aac", "mp3", "opus", "vorbis"})


class VideoIlegible(Exception):
    """`ffprobe` no pudo leer el archivo o no trae un flujo de video real."""


class FfprobeNoDisponible(Exception):
    """`ffprobe` no se pudo ejecutar: el problema es del servidor, no del archivo."""


@dataclass(frozen=True)
class FlujoDeVideo:
    codec: str
    perfil: str | None
    ancho: int | None
    alto: int | None
    fps: float | None
    pix_fmt: str | None


@dataclass(frozen=True)
class FlujoDeAudio:
    codec: str
    canales: int | None
    hz: int | None


@dataclass(frozen=True)
class MetadatosVideo:
    contenedor: Contenedor
    duracion_s: float
    bit_rate: int | None
    video: FlujoDeVideo
    audio: FlujoDeAudio | None
    creado_segun_archivo: datetime | None
    """Una pista para el campo Fecha, no un valor precargado."""
    reproducible_probable: bool
    """Si el navegador lo reproduce casi seguro; si no, se ofrece descargarlo."""

    def a_json(self) -> dict[str, Any]:
        """Lo que se guarda en `pieza.metadata_original`."""
        datos = asdict(self)
        fecha = self.creado_segun_archivo
        datos["creado_segun_archivo"] = None if fecha is None else fecha.isoformat()
        return datos


def disponible() -> bool:
    """Si `ffprobe` se puede correr endurecido: sin él, la subida de video responde 503.

    Pide `ffprobe` y, en POSIX, `prlimit` en el PATH, y que el servidor no corra como root.
    """
    binarios = [_FFPROBE] if _PRLIMIT is None else [_FFPROBE, _PRLIMIT]
    return not _es_root() and all(shutil.which(b) is not None for b in binarios)


def analizar(ruta: Path, contenedor: Contenedor, *, timeout_s: float = 10) -> MetadatosVideo:
    """Los datos técnicos del video en `ruta`, una ruta que generó el servidor.

    `ffprobe` corre como proceso hijo endurecido (DEC-16): sin shell, solo el protocolo `file`,
    el demuxer forzado y un hilo, entrada cerrada, entorno con solo `PATH`, 512 MiB de memoria y
    10 s de CPU con `prlimit` (POSIX), `timeout_s` de reloj y la salida cortada a 256 KiB. No
    corre como root.

    Raises:
        VideoIlegible: Si `ffprobe` falla, se pasa de `timeout_s`, imprime más de 256 KiB o el
            archivo no tiene un flujo de video real.
        FfprobeNoDisponible: Si `ffprobe` no se puede ejecutar o el servidor corre como root.
    """
    if _es_root():
        raise FfprobeNoDisponible("ffprobe no corre como root: el servidor va sin privilegios.")
    # `prlimit` hace `exec` sobre el mismo PID: `kill()` le llega a `ffprobe`.
    # shortcut: en Windows el hijo corre sin topes de memoria ni CPU; el despliegue es Linux.
    topes = (
        []
        if _PRLIMIT is None
        else [_PRLIMIT, f"--as={_MEMORIA_MAXIMA}", f"--cpu={_CPU_MAXIMA_S}", "--"]
    )
    try:
        proceso = subprocess.Popen(
            [
                *topes, _FFPROBE, "-v", "error",
                # Solo archivos locales y el demuxer forzado: no se disfraza de playlist ni de
                # `concat` para leer otros archivos o abrir red.
                "-protocol_whitelist", "file", "-f", contenedor, "-threads", "1",
                "-probesize", "50M", "-analyzeduration", "5M",
                "-show_format", "-show_streams", "-of", "json",
                "-i", str(ruta),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except OSError as error:
        raise FfprobeNoDisponible(f"No se pudo ejecutar ffprobe: {error.strerror}.") from error
    with proceso:
        salida, vencido = _leer_con_topes(proceso, timeout_s)
    if len(salida) > _SALIDA_MAXIMA:
        raise VideoIlegible(f"La salida de ffprobe pasa de {_SALIDA_MAXIMA // 1024} KiB.")
    if vencido:
        raise VideoIlegible(f"ffprobe no terminó en {timeout_s:g} s.")
    # Con `prlimit` delante, un `ffprobe` que no se puede ejecutar no es un `OSError`: `prlimit`
    # sale con 126 o 127 (util-linux), códigos que `ffprobe` no usa.
    if topes and proceso.returncode in (126, 127):
        raise FfprobeNoDisponible("No se pudo ejecutar ffprobe.")
    if proceso.returncode != 0:
        raise VideoIlegible("ffprobe no pudo leer el archivo.")
    return interpretar(salida, contenedor)


def _leer_con_topes(proceso: subprocess.Popen[bytes], timeout_s: float) -> tuple[bytes, bool]:
    """Hasta `_SALIDA_MAXIMA` + 1 bytes de la salida y si venció `timeout_s`.

    Un hilo lee; este espera y mata: así `kill()` nunca llega a un PID ya recogido.
    """
    leido: list[bytes] = []
    assert proceso.stdout is not None
    salida_del_hijo = proceso.stdout
    lector = threading.Thread(
        target=lambda: leido.append(salida_del_hijo.read(_SALIDA_MAXIMA + 1)), daemon=True
    )
    fin = time.monotonic() + timeout_s
    lector.start()
    lector.join(timeout_s)
    if leido and len(leido[0]) > _SALIDA_MAXIMA:
        proceso.kill()
    try:
        proceso.wait(max(0.0, fin - time.monotonic()))
        vencido = False
    except subprocess.TimeoutExpired:
        proceso.kill()
        proceso.wait()
        vencido = True
    lector.join()
    return (leido[0] if leido else b""), vencido


def _es_root() -> bool:
    return bool(getattr(os, "geteuid", lambda: -1)() == 0)


def interpretar(salida: bytes, contenedor: Contenedor) -> MetadatosVideo:
    """Los datos técnicos que pasan la lista blanca, desde el JSON que imprime `ffprobe`.

    Raises:
        VideoIlegible: Si el JSON no es válido, trae más de 16 flujos, no tiene un flujo de
            video que no sea una carátula o la duración falta, es cero o no es un número.
    """
    try:
        datos = json.loads(salida)
        flujos = datos["streams"]
        formato = datos["format"]
    except (ValueError, TypeError, KeyError) as error:
        raise VideoIlegible("La salida de ffprobe no es válida.") from error
    if not isinstance(flujos, list) or not isinstance(formato, dict):
        raise VideoIlegible("La salida de ffprobe no es válida.")
    if len(flujos) > _FLUJOS_MAXIMOS:
        raise VideoIlegible(f"El archivo trae más de {_FLUJOS_MAXIMOS} flujos.")
    # La carátula de un M4A es un flujo de video de un solo fotograma: no es video.
    videos = [
        f for f in flujos
        if f.get("codec_type") == "video" and not (f.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        raise VideoIlegible("El archivo no tiene un flujo de video.")
    duracion = _numero(formato.get("duration"), float)
    if duracion is None or not math.isfinite(duracion) or duracion <= 0:
        raise VideoIlegible("ffprobe no informa la duración del video.")
    v = videos[0]
    video = FlujoDeVideo(
        codec=_codec(v),
        perfil=_texto(v.get("profile"), _PERFIL),
        ancho=_numero(v.get("width"), int),
        alto=_numero(v.get("height"), int),
        fps=_fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
        pix_fmt=_texto(v.get("pix_fmt"), _CODEC),
    )
    a = next((f for f in flujos if f.get("codec_type") == "audio"), None)
    audio = (
        None
        if a is None
        else FlujoDeAudio(
            codec=_codec(a),
            canales=_numero(a.get("channels"), int),
            hz=_numero(a.get("sample_rate"), int),
        )
    )
    return MetadatosVideo(
        contenedor=contenedor,
        duracion_s=duracion,
        bit_rate=_numero(formato.get("bit_rate"), int),
        video=video,
        audio=audio,
        creado_segun_archivo=_fecha((formato.get("tags") or {}).get("creation_time")),
        reproducible_probable=(
            video.codec in _VIDEO_REPRODUCIBLE
            and video.pix_fmt == "yuv420p"
            and (audio is None or audio.codec in _AUDIO_REPRODUCIBLE)
        ),
    )


def _codec(flujo: dict[str, Any]) -> str:
    codec = _texto(flujo.get("codec_name"), _CODEC)
    if codec is None:
        raise VideoIlegible("ffprobe informa un códec que no se reconoce.")
    return codec


def _texto(valor: object, patron: re.Pattern[str]) -> str | None:
    return valor if isinstance(valor, str) and patron.fullmatch(valor) else None


def _numero[N: (int, float)](valor: object, tipo: type[N]) -> N | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    try:
        return tipo(valor)
    except ValueError:
        return None


def _fps(valor: object) -> float | None:
    if not isinstance(valor, str) or "/" not in valor:
        return None
    numerador, _, denominador = valor.partition("/")
    n, d = _numero(numerador, int), _numero(denominador, int)
    return None if not n or not d else round(n / d, 3)


def _fecha(valor: object) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None
