"""Réplica de los originales en la unidad compartida de Drive (DEC-36; `07-drive.md`, DRV-04).

El disco local es el almacén primario siempre: el visor y el trabajador leen de ahí y ningún
original se quita del disco por estar replicado (DRV-01, DRV-05, DRV-07). Drive es una copia
asincrónica que hace el `Replicador` en un hilo del proceso `trabajador`, con su propia tabla
(`replicacion_drive`), que es estado y cola a la vez: replicar no cambia la Pieza, y que Drive
falle no frena ingesta, procesamiento ni validación.

Máquina de la réplica (DRV-14, §3.3):

    sin fila / pendiente --toma--> subiendo --ok y md5 coincide--> replicado
    subiendo --error transitorio--> pendiente (próximo intento en min(2^n min, 6 h))
    subiendo --8 intentos, error permanente o md5 distinto--> fallido
    fallido --UPDATE del Administrador técnico--> pendiente

`ClienteDrive` habla la API REST v3 de Drive con `httpx` y firma el JWT de la cuenta de
servicio con `joserfc`, sin `google-api-python-client`: la suite le pasa el transporte de un
doble (`tests/drive_falso.py`) y no sale a la red.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from joserfc import jwk, jwt

from historia_viva.bd import Bd
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.originales import AlmacenDeOriginales, OriginalNoDisponible

__all__ = [
    "ARRIENDO",
    "ESTADOS",
    "INTENTOS_MAXIMOS",
    "TRAMO_BYTES",
    "ArchivoDrive",
    "ClaveDeDriveInvalida",
    "ClienteDrive",
    "ConfiguracionDrive",
    "ErrorDeDrive",
    "Replicador",
    "leer_clave",
    "resumen",
]

_log = logging.getLogger(__name__)

ARRIENDO = timedelta(minutes=30)
"""Cuánto es de un hilo la fila que tomó; se renueva con cada tramo subido (§3.3)."""

INTENTOS_MAXIMOS = 8
"""Con tantos intentos fallidos la fila queda `fallido` (§3.3)."""

ESPERA_MAXIMA = timedelta(hours=6)

TRAMO_BYTES = 8 * 1024 * 1024
"""El tramo de la subida reanudable: múltiplo de 256 KiB, como pide Drive (DRV-06)."""

INTERVALO_S = 30.0
"""Cada cuánto mira la tabla el hilo cuando no tiene nada que replicar."""

ESTADOS = ("pendiente", "subiendo", "replicado", "fallido")

_ALCANCE = "https://www.googleapis.com/auth/drive"
_API = "https://www.googleapis.com/drive/v3/files"
_SUBIDA = "https://www.googleapis.com/upload/drive/v3/files"
_CAMPOS = "id,md5Checksum,size"
_TRANSITORIOS = frozenset({401, 408, 429})
"""Además de los 5xx. Un 401 es un token vencido: el próximo intento pide otro."""
_RAZONES_DE_CUOTA = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


@dataclass(frozen=True)
class ConfiguracionDrive:
    """`HV_DRIVE=on` con `HV_DRIVE_CLAVE`, `HV_DRIVE_UNIDAD` y `HV_DRIVE_CARPETA` (DRV-08)."""

    clave: Path
    """El JSON de la cuenta de servicio, montado de solo lectura: el entorno lleva la ruta,
    nunca la clave (RNF-14)."""
    unidad: str
    carpeta: str


@dataclass(frozen=True)
class ArchivoDrive:
    file_id: str
    md5: str
    bytes: int


class ErrorDeDrive(Exception):
    def __init__(self, detalle: str, *, permanente: bool) -> None:
        super().__init__(detalle)
        self.permanente = permanente


class ClaveDeDriveInvalida(Exception):
    """La clave de la cuenta de servicio falta o no se entiende: el trabajador no arranca."""


class _ArriendoPerdido(Exception):
    """Otro hilo retomó la fila: este deja de subir y no la toca."""


class _Interrumpida(Exception):
    """El trabajador se apaga a mitad de una subida."""


def leer_clave(ruta: Path) -> dict[str, str]:
    """El JSON de la cuenta de servicio, del archivo montado (DRV-08).

    Raises:
        ClaveDeDriveInvalida: Si el archivo no está o no tiene `client_email` y `private_key`.
    """
    try:
        clave = json.loads(ruta.read_text(encoding="utf-8"))
        jwk.RSAKey.import_key(clave["private_key"])
        return {
            "client_email": clave["client_email"],
            "private_key": clave["private_key"],
            "token_uri": clave.get("token_uri") or "https://oauth2.googleapis.com/token",
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Sin el contenido: el mensaje va al log y la clave no.
        # Sin la ruta: si alguien pegó la clave en la variable, el mensaje la repetiría.
        raise ClaveDeDriveInvalida(
            "HV_DRIVE=on necesita en HV_DRIVE_CLAVE la ruta de la clave JSON de la cuenta de"
            f" servicio, montada de solo lectura; no se pudo leer ({type(error).__name__})."
        ) from error


def _error_de(respuesta: httpx.Response) -> ErrorDeDrive:
    permanente = (
        respuesta.status_code < 500
        and respuesta.status_code not in _TRANSITORIOS
        and not _cuota(respuesta)
    )
    return ErrorDeDrive(f"Drive respondió {respuesta.status_code}.", permanente=permanente)


def _cuota(respuesta: httpx.Response) -> bool:
    """Un 403 por cuota (`rateLimitExceeded`, `userRateLimitExceeded`) es transitorio: Drive
    pide esperar, no que el pedido esté mal."""
    try:
        errores = respuesta.json()["error"]["errors"]
        return any(e.get("reason") in _RAZONES_DE_CUOTA for e in errores)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


class ClienteDrive:
    """Buscar y subir en la unidad compartida, con la identidad de la cuenta de servicio."""

    def __init__(
        self,
        configuracion: ConfiguracionDrive,
        clave: dict[str, str],
        *,
        transporte: httpx.BaseTransport | None = None,
        reloj: Callable[[], float] = time.time,
    ) -> None:
        self._configuracion = configuracion
        self._clave = clave
        self._reloj = reloj
        self._http = httpx.Client(transport=transporte, timeout=60.0)
        self._token: tuple[str, float] | None = None

    def _autorizacion(self) -> dict[str, str]:
        """El token de la cuenta de servicio (JWT bearer, RFC 7523), mientras le quede 1 min."""
        ahora = self._reloj()
        if self._token is None or self._token[1] - 60 < ahora:
            afirmacion = jwt.encode(
                {"alg": "RS256", "typ": "JWT"},
                {
                    "iss": self._clave["client_email"],
                    "scope": _ALCANCE,
                    "aud": self._clave["token_uri"],
                    "iat": int(ahora),
                    "exp": int(ahora) + 3600,
                },
                jwk.RSAKey.import_key(self._clave["private_key"]),
            )
            respuesta = self._pedir(
                "POST",
                self._clave["token_uri"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": afirmacion,
                },
            )
            cuerpo = respuesta.json()
            self._token = (cuerpo["access_token"], ahora + int(cuerpo.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}"}

    def _pedir(self, metodo: str, url: str, **opciones: Any) -> httpx.Response:
        try:
            respuesta = self._http.request(metodo, url, **opciones)
        except httpx.HTTPError as error:  # red caída, DNS, tiempo agotado
            raise ErrorDeDrive(
                f"Drive no respondió ({type(error).__name__}).", permanente=False
            ) from error
        if respuesta.status_code >= 400:
            raise _error_de(respuesta)
        return respuesta

    def buscar(self, pieza_id: UUID, sha256: str) -> ArchivoDrive | None:
        """El archivo de la Pieza ya subido, por sus `appProperties` (DRV-11)."""
        consulta = (
            f"appProperties has {{ key='pieza_id' and value='{pieza_id}' }}"
            f" and appProperties has {{ key='sha256' and value='{sha256}' }}"
            " and trashed = false"
        )
        respuesta = self._pedir(
            "GET",
            _API,
            headers=self._autorizacion(),
            params={
                "q": consulta,
                "corpora": "drive",
                "driveId": self._configuracion.unidad,
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "fields": f"files({_CAMPOS})",
            },
        )
        archivos = respuesta.json().get("files", [])
        return _archivo(archivos[0]) if archivos else None

    def subir(
        self,
        bloques: Iterable[bytes],
        *,
        total: int,
        nombre: str,
        tipo_mime: str,
        pieza_id: UUID,
        sha256: str,
        al_avanzar: Callable[[], None],
    ) -> ArchivoDrive:
        """Sube por la subida reanudable de a `TRAMO_BYTES`, llamando a `al_avanzar` después de
        cada tramo. Si el proceso muere, la próxima vez sube desde cero (DRV-06)."""
        inicio = self._pedir(
            "POST",
            _SUBIDA,
            headers={
                **self._autorizacion(),
                "X-Upload-Content-Type": tipo_mime,
                "X-Upload-Content-Length": str(total),
            },
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": _CAMPOS},
            json={
                "name": f"{pieza_id}-{nombre}",
                "parents": [self._configuracion.carpeta],
                "mimeType": tipo_mime,
                "appProperties": {"pieza_id": str(pieza_id), "sha256": sha256},
            },
        )
        sesion = inicio.headers["Location"]
        enviados = 0
        respuesta = None
        for tramo in _tramos(bloques):
            while tramo:
                fin = enviados + len(tramo) - 1
                respuesta = self._pedir(
                    "PUT",
                    sesion,
                    headers={"Content-Range": f"bytes {enviados}-{fin}/{total}"},
                    content=tramo,
                )
                # Un 308 dice hasta dónde llegó: lo que falta del tramo se reenvía.
                recibido = _recibidos(respuesta) if respuesta.status_code == 308 else fin + 1
                if not enviados < recibido <= fin + 1:  # sin avance: reenviar sería un bucle
                    raise ErrorDeDrive(
                        "Drive no avanzó la subida reanudable.", permanente=False
                    )
                tramo = tramo[recibido - enviados :]
                enviados = recibido
            al_avanzar()
        if total == 0:
            respuesta = self._pedir("PUT", sesion, headers={"Content-Range": "bytes */0"})
        if respuesta is None or respuesta.status_code == 308 or enviados != total:
            raise ErrorDeDrive("La subida terminó incompleta.", permanente=False)
        return _archivo(respuesta.json())


def _archivo(datos: dict[str, Any]) -> ArchivoDrive:
    return ArchivoDrive(
        file_id=datos["id"], md5=datos.get("md5Checksum", ""), bytes=int(datos.get("size", 0))
    )


def _recibidos(respuesta: httpx.Response) -> int:
    """Los bytes que Drive tiene, según la cabecera `Range: bytes=0-N` de un 308."""
    rango = respuesta.headers.get("Range")
    return int(rango.rsplit("-", 1)[1]) + 1 if rango else 0


def _tramos(bloques: Iterable[bytes]) -> Iterator[bytes]:
    """Junta los bloques de 1 MiB del almacén en tramos de `TRAMO_BYTES`."""
    tramo = bytearray()
    for bloque in bloques:
        tramo += bloque
        while len(tramo) >= TRAMO_BYTES:
            yield bytes(tramo[:TRAMO_BYTES])
            del tramo[:TRAMO_BYTES]
    if tramo:
        yield bytes(tramo)


@dataclass(frozen=True)
class _Tomada:
    pieza_id: UUID
    arrendado_hasta: datetime


class Replicador:
    """El hilo que replica de a una fila: la toma con arriendo, sube y la cierra."""

    def __init__(
        self,
        *,
        bd: Bd,
        fichas: ModuloFichasPostgres,
        originales: AlmacenDeOriginales,
        cliente: ClienteDrive,
    ) -> None:
        self._bd = bd
        self._fichas = fichas
        self._originales = originales
        self._cliente = cliente

    def sincronizar(self) -> int:
        """Una fila `pendiente` por cada original que todavía no tiene: encender la réplica
        copia el atraso sin comando aparte (DRV-12). Devuelve cuántas agregó."""
        piezas = self._fichas.piezas_con_original()
        with self._bd.transaccion() as tx:
            return tx.execute(
                "INSERT INTO acervo.replicacion_drive (pieza_id) SELECT unnest(%s::uuid[])"
                " ON CONFLICT (pieza_id) DO NOTHING",
                (piezas,),
            ).rowcount

    def ejecutar_una_vez(self, parar: threading.Event | None = None) -> bool:
        """Replica una fila, si hay alguna para tomar. Devuelve si había."""
        tomada = self._tomar()
        if tomada is None and self.sincronizar():
            tomada = self._tomar()
        if tomada is None:
            return False
        self._replicar(tomada, parar or threading.Event())
        return True

    def bucle(self, parar: threading.Event, intervalo: float = INTERVALO_S) -> None:
        while not parar.is_set():
            try:
                hubo = self.ejecutar_una_vez(parar)
            except Exception:  # noqa: BLE001 — la base caída no mata el hilo: vuelve con ella
                _log.exception("La réplica a Drive no pudo tomar una fila.")
                hubo = False
            if not hubo:
                parar.wait(intervalo)

    def _tomar(self) -> _Tomada | None:
        """Una `pendiente` cuyo turno llegó o una `subiendo` cuyo arriendo venció (el hilo que
        la tenía murió). Cada toma cuenta como intento: una subida que muere siempre a mitad
        llega a `fallido` como cualquier otra."""
        with self._bd.transaccion() as tx:
            tx.execute(
                "UPDATE acervo.replicacion_drive SET estado = 'fallido', arrendado_hasta = NULL,"
                " ultimo_error = 'La subida se cortó en cada uno de sus intentos.'"
                " WHERE estado = 'subiendo' AND arrendado_hasta < now() AND intentos >= %s",
                (INTENTOS_MAXIMOS,),
            )
            fila = tx.execute(
                "UPDATE acervo.replicacion_drive SET estado = 'subiendo',"
                " intentos = intentos + 1, arrendado_hasta = now() + %s"
                " WHERE pieza_id = (SELECT pieza_id FROM acervo.replicacion_drive"
                "  WHERE (estado = 'pendiente'"
                "   AND coalesce(proximo_intento, '-infinity') <= now())"
                "  OR (estado = 'subiendo' AND arrendado_hasta < now())"
                "  ORDER BY proximo_intento NULLS FIRST LIMIT 1 FOR UPDATE SKIP LOCKED)"
                " RETURNING pieza_id, arrendado_hasta",
                (ARRIENDO,),
            ).fetchone()
        return None if fila is None else _Tomada(**fila)

    def _replicar(self, tomada: _Tomada, parar: threading.Event) -> None:
        arriendo = [tomada.arrendado_hasta]

        def renovar() -> None:
            if parar.is_set():
                raise _Interrumpida()
            with self._bd.transaccion() as tx:
                fila = tx.execute(
                    "UPDATE acervo.replicacion_drive SET arrendado_hasta = now() + %s"
                    " WHERE pieza_id = %s AND estado = 'subiendo' AND arrendado_hasta = %s"
                    " RETURNING arrendado_hasta",
                    (ARRIENDO, tomada.pieza_id, arriendo[0]),
                ).fetchone()
            if fila is None:
                raise _ArriendoPerdido()
            arriendo[0] = fila["arrendado_hasta"]

        try:
            original = self._fichas.obtener_original(tomada.pieza_id)
            md5, sha256 = _huellas(self._originales.abrir(original).bloques)
            if original.huella is not None and original.huella != sha256:
                # Replicarlo copiaría a Drive un original que ya no es el que se guardó.
                self._cerrar(
                    tomada, arriendo[0], fallido="El original en disco no coincide con su huella."
                )
                return
            archivo = self._cliente.buscar(tomada.pieza_id, sha256)
            if archivo is None:
                archivo = self._cliente.subir(
                    self._originales.abrir(original).bloques,
                    total=original.tamano_bytes,
                    nombre=original.nombre_archivo,
                    tipo_mime=original.tipo_mime,
                    pieza_id=tomada.pieza_id,
                    sha256=sha256,
                    al_avanzar=renovar,
                )
            if archivo.md5 != md5:
                self._cerrar(
                    tomada, arriendo[0], fallido="El MD5 de Drive no coincide con el original."
                )
            else:
                self._cerrar(tomada, arriendo[0], file_id=archivo.file_id)
        except _ArriendoPerdido:
            _log.warning("Otro hilo retomó la réplica de %s.", tomada.pieza_id)
        except _Interrumpida:
            self._cerrar(tomada, arriendo[0], devolver=True)
        except ErrorDeDrive as error:
            _log.warning("Réplica de %s: %s", tomada.pieza_id, error)
            if error.permanente:
                self._cerrar(tomada, arriendo[0], fallido=str(error))
            else:
                self._cerrar(tomada, arriendo[0], transitorio=str(error))
        except OriginalNoDisponible as error:
            self._cerrar(tomada, arriendo[0], fallido=str(error))
        except Exception as error:  # noqa: BLE001 — el hilo no muere por una fila: cuenta como un intento
            _log.exception("Falló la réplica de %s.", tomada.pieza_id)
            self._cerrar(
                tomada, arriendo[0], transitorio=f"Error interno ({type(error).__name__})."
            )

    def _cerrar(
        self,
        tomada: _Tomada,
        arriendo: datetime,
        *,
        file_id: str | None = None,
        fallido: str | None = None,
        transitorio: str | None = None,
        devolver: bool = False,
    ) -> None:
        """Cierra la fila si sigue siendo de este hilo; si no, no la toca."""
        if file_id is not None:
            cambio = (
                "estado = 'replicado', drive_file_id = %(file_id)s, replicado_el = now(),"
                " ultimo_error = NULL, proximo_intento = NULL"
            )
        elif fallido is not None:
            cambio = "estado = 'fallido', ultimo_error = %(error)s"
        elif transitorio is not None:
            cambio = (
                "estado = CASE WHEN intentos >= %(maximos)s THEN 'fallido' ELSE 'pendiente' END,"
                " ultimo_error = %(error)s,"
                # El exponente se acota antes: 2^n min con n grande sale del rango de interval.
                " proximo_intento = now()"
                " + least(interval '1 minute' * 2 ^ least(intentos, 10), %(tope)s)"
            )
        else:  # devolver: sin espera, el próximo arranque la retoma
            cambio = "estado = 'pendiente', proximo_intento = NULL"
        with self._bd.transaccion() as tx:
            cerrada = tx.execute(
                f"UPDATE acervo.replicacion_drive SET {cambio}, arrendado_hasta = NULL"
                " WHERE pieza_id = %(pieza_id)s AND estado = 'subiendo'"
                " AND arrendado_hasta = %(arriendo)s",
                {
                    "file_id": file_id,
                    "error": fallido or transitorio,
                    "maximos": INTENTOS_MAXIMOS,
                    "tope": ESPERA_MAXIMA,
                    "pieza_id": tomada.pieza_id,
                    "arriendo": arriendo,
                },
            ).rowcount
        if not cerrada:
            _log.warning("Otro hilo retomó la réplica de %s.", tomada.pieza_id)


def _huellas(bloques: Iterable[bytes]) -> tuple[str, str]:
    """MD5 (lo que Drive devuelve) y SHA-256 (lo que la identifica) del original en disco."""
    md5, sha256 = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    for bloque in bloques:
        md5.update(bloque)
        sha256.update(bloque)
    return md5.hexdigest(), sha256.hexdigest()


def resumen(bd: Bd) -> dict[str, int]:
    """Cuántas filas hay en cada estado: lo que muestra la salud. Con la réplica apagada, todo
    en cero."""
    with bd.transaccion() as tx:
        filas = tx.execute(
            "SELECT estado, count(*) AS n FROM acervo.replicacion_drive GROUP BY estado"
        ).fetchall()
    return {estado: 0 for estado in ESTADOS} | {fila["estado"]: fila["n"] for fila in filas}
