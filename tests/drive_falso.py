"""El doble de Drive (DEC-146 punto 15; `07-drive.md` §3.8): lo justo de la API REST v3.

Atiende, por un `httpx.MockTransport`, el canje del JWT de la cuenta de servicio, la búsqueda
por `appProperties` y la subida reanudable, con las mismas exigencias que Drive: token vigente
y `supportsAllDrives=true` (sin él, 404). La suite no sale a la red (historia 211).

Perillas: `caido` (la conexión falla), `responder` (códigos para los próximos pedidos),
`md5_corrupto` (Drive devuelve otro MD5) y `aceptar_hasta` (un 308 que recibió menos bytes).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
from joserfc import jwk, jwt

from historia_viva.drive import ClienteDrive, ConfiguracionDrive

TOKEN_URI = "https://oauth2.googleapis.com/token"
CONFIGURACION = ConfiguracionDrive(
    clave=Path("drive_sa.json"), unidad="unidad-1", carpeta="carpeta-1"
)


def clave_de_prueba(clave: jwk.RSAKey) -> dict[str, str]:
    """El JSON de una cuenta de servicio, con una clave generada para la prueba."""
    return {
        "type": "service_account",
        "client_email": "hv-originales@proyecto.iam.gserviceaccount.com",
        "private_key": clave.as_pem(private=True).decode(),
        "token_uri": TOKEN_URI,
    }


@dataclass
class DriveFalso:
    clave: jwk.RSAKey = field(default_factory=lambda: jwk.RSAKey.generate_key(2048))
    archivos: dict[str, dict] = field(default_factory=dict)
    sesiones: dict[str, dict] = field(default_factory=dict)
    caido: bool = False
    responder: list[int | tuple[int, str]] = field(default_factory=list)
    """Códigos para los próximos pedidos, o `(código, reason)` como los 403 de cuota de Drive."""
    md5_corrupto: bool = False
    aceptar_hasta: int | None = None
    subidas: int = 0
    """Cuántas subidas se iniciaron: una réplica idempotente no suma."""
    tramos: int = 0
    al_recibir_tramo: Callable[[], None] | None = None
    """Corre con cada tramo, antes de guardarlo: ahí un test mira o pisa el arriendo."""

    def cliente(self) -> ClienteDrive:
        return ClienteDrive(
            CONFIGURACION, clave_de_prueba(self.clave), transporte=httpx.MockTransport(self)
        )

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        if self.caido:
            raise httpx.ConnectError("Drive no responde", request=pedido)
        if self.responder:
            codigo, razon = self.responder.pop(0), "falso"
            if isinstance(codigo, tuple):
                codigo, razon = codigo
            return httpx.Response(
                codigo, json={"error": {"code": codigo, "errors": [{"reason": razon}]}}
            )
        url = pedido.url
        if str(url) == TOKEN_URI:
            return self._token(pedido)
        # La URI de la sesión reanudable es la credencial de la subida: los tramos van sin token.
        if url.path.startswith("/upload/") and pedido.method == "PUT":
            return self._tramo(pedido)
        if pedido.headers.get("Authorization") != "Bearer token-falso":
            return httpx.Response(401)
        if url.params.get("supportsAllDrives") != "true":
            return httpx.Response(404)
        if url.path == "/drive/v3/files" and pedido.method == "GET":
            return self._buscar(url.params["q"], url.params.get("driveId"))
        if url.path == "/upload/drive/v3/files" and pedido.method == "POST":
            return self._iniciar(pedido)
        return httpx.Response(404)

    def _token(self, pedido: httpx.Request) -> httpx.Response:
        datos = parse_qs(pedido.content.decode())
        assert datos["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        claims = jwt.decode(datos["assertion"][0], self.clave, algorithms=["RS256"]).claims
        assert claims["aud"] == TOKEN_URI
        assert claims["scope"] == "https://www.googleapis.com/auth/drive"
        return httpx.Response(200, json={"access_token": "token-falso", "expires_in": 3600})

    def _buscar(self, consulta: str, unidad: str | None) -> httpx.Response:
        buscadas = dict(re.findall(r"key='(\w+)' and value='([^']*)'", consulta))
        encontrados = [
            {"id": file_id, "md5Checksum": a["md5"], "size": str(len(a["bytes"]))}
            for file_id, a in self.archivos.items()
            if unidad == CONFIGURACION.unidad
            and all(a["appProperties"].get(k) == v for k, v in buscadas.items())
        ]
        return httpx.Response(200, json={"files": encontrados})

    def _iniciar(self, pedido: httpx.Request) -> httpx.Response:
        self.subidas += 1
        sesion = uuid4().hex
        self.sesiones[sesion] = {
            "metadatos": json.loads(pedido.content),
            "total": int(pedido.headers["X-Upload-Content-Length"]),
            "bytes": bytearray(),
        }
        return httpx.Response(
            200,
            headers={
                "Location": f"https://www.googleapis.com/upload/drive/v3/files?upload_id={sesion}"
            },
        )

    def _tramo(self, pedido: httpx.Request) -> httpx.Response:
        sesion = self.sesiones[pedido.url.params["upload_id"]]
        self.tramos += 1
        if self.al_recibir_tramo is not None:
            self.al_recibir_tramo()
        rango = pedido.headers["Content-Range"]
        if rango != "bytes */0":
            inicio = int(rango.split()[1].split("-")[0])
            assert inicio == len(sesion["bytes"]), "el cliente tiene que seguir donde quedó"
            contenido = pedido.content
            if self.aceptar_hasta is not None:
                contenido, self.aceptar_hasta = contenido[: self.aceptar_hasta], None
            sesion["bytes"] += contenido
        if not sesion["bytes"] and sesion["total"]:
            return httpx.Response(308)  # como Drive: sin nada recibido, el 308 no trae `Range`
        if len(sesion["bytes"]) < sesion["total"]:
            return httpx.Response(308, headers={"Range": f"bytes=0-{len(sesion['bytes']) - 1}"})
        file_id = uuid4().hex
        md5 = hashlib.md5(bytes(sesion["bytes"])).hexdigest()
        self.archivos[file_id] = {
            **sesion["metadatos"],
            "bytes": bytes(sesion["bytes"]),
            "md5": "0" * 32 if self.md5_corrupto else md5,
        }
        archivo = self.archivos[file_id]
        return httpx.Response(
            200,
            json={"id": file_id, "md5Checksum": archivo["md5"], "size": str(len(archivo["bytes"]))},
        )
