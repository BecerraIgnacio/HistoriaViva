"""El ingreso con Google (spec, «Pasos del OIDC»; RF-34, CU-15, DEC-37).

`iniciar` arma la ida al proveedor con PKCE S256, `state` y `nonce`, que viajan en la cookie del
intento: firmada, de 10 minutos y previa a la sesión. `completar` exige esa cookie, compara el
`state`, canjea el `code` con el verificador, valida el ID token (firma por JWKS, `iss`, `aud`,
`exp` y `nonce`) y la identidad (`email_verified`, `hd` y el dominio del email), y entrega la
persona a `Sesiones.ingresar_oidc`. Todo rechazo es un `RechazoDeIngreso` con un código cerrado
(spec §3.3): nada de lo que trae el pedido vuelve a la pantalla.

Se construye y se prueba contra un proveedor falso (DEC-146 punto 15); contra Google funciona
cuando IT entregue el cliente (C18, ticket 127). Con `httpx`, que solo usan este módulo y la capa
de modelos (`test_arquitectura`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from historia_viva.identidad import PersonaNoHabilitada, Sesiones

__all__ = ["DESTINOS", "VIDA_DEL_INTENTO", "ConfiguracionOIDC", "IngresoOIDC", "RechazoDeIngreso"]

VIDA_DEL_INTENTO = 600
"""Segundos que vale la cookie del intento: lo que tarda una persona en elegir su cuenta."""
DESTINOS = ("/", "/validar.html")
"""Adonde puede volver la persona después de entrar: rutas relativas de una lista cerrada."""
_DESCUBRIMIENTO = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
"""Lo que se usa del documento de descubrimiento; si falta algo, el proveedor está roto."""


class RechazoDeIngreso(Exception):
    """El ingreso no se completa; `codigo` es el `?error=` de la Entrada (spec §3.3)."""

    def __init__(self, codigo: str) -> None:
        super().__init__(codigo)
        self.codigo = codigo


@dataclass(frozen=True)
class ConfiguracionOIDC:
    """`HV_OIDC_*` y `HV_CLAVE_COOKIE_PREVIA`. `http` es el cliente hacia el proveedor: la suite
    pasa el del proveedor falso; sin él, uno con timeout."""

    emisor: str
    cliente_id: str
    secreto: str = field(repr=False)
    redireccion: str
    """La URI de retorno registrada en el cliente: `<HV_URL_PUBLICA>/auth/callback`."""
    clave_del_intento: str = field(repr=False)
    dominios: tuple[str, ...]
    http: httpx.Client | None = field(default=None, compare=False, repr=False)


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _de_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


class IngresoOIDC:
    def __init__(
        self,
        configuracion: ConfiguracionOIDC,
        *,
        sesiones: Sesiones,
        reloj: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = configuracion
        self._http = configuracion.http or httpx.Client(timeout=10)
        self._clave = configuracion.clave_del_intento.encode()
        self._sesiones = sesiones
        self._reloj = reloj or (lambda: datetime.now(UTC))

    def iniciar(self, siguiente: str | None) -> tuple[str, str]:
        """La URL del proveedor y el valor de la cookie del intento.

        Raises:
            RechazoDeIngreso: `proveedor-no-disponible`.
        """
        intento: dict[str, Any] = {
            "estado": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "verificador": secrets.token_urlsafe(64),
            "siguiente": siguiente if siguiente in DESTINOS else "/",
            "desde": self._reloj().timestamp(),
        }
        query = {
            "response_type": "code",
            "client_id": self._config.cliente_id,
            "redirect_uri": self._config.redireccion,
            "scope": "openid email",
            "state": intento["estado"],
            "nonce": intento["nonce"],
            "code_challenge": _b64(hashlib.sha256(intento["verificador"].encode()).digest()),
            "code_challenge_method": "S256",
            # Pista de interfaz: la cuenta se verifica igual al volver.
            "hd": self._config.dominios[0],
        }
        url = self._descubrir()["authorization_endpoint"]
        return f"{url}?{urlencode(query)}", self._firmar(intento)

    def completar(
        self,
        *,
        codigo: str | None,
        estado: str | None,
        error: str | None,
        intento: str | None,
        previa: str | None,
    ) -> tuple[str, str]:
        """La sesión nueva y adónde volver.

        Raises:
            RechazoDeIngreso: Con uno de los códigos de §3.3.
        """
        if error:
            raise RechazoDeIngreso(
                "acceso-cancelado" if error == "access_denied" else "intento-vencido"
            )
        datos = self._leer(intento)
        # En bytes: `compare_digest` con un `str` no ASCII lanza `TypeError`.
        estado_esperado = datos["estado"].encode()
        if not codigo or not hmac.compare_digest((estado or "").encode(), estado_esperado):
            raise RechazoDeIngreso("intento-vencido")
        claims = self._canjear(codigo, datos)
        email = str(claims.get("email", ""))
        # `hd` solo no alcanza: también el dominio del email (spec, «Pasos del OIDC» 3).
        dominio = email.rpartition("@")[2].lower()
        if claims.get("email_verified") is not True:
            motivo = "cuenta-no-verificada"
        elif claims.get("hd") != dominio or dominio not in self._config.dominios:
            motivo = "dominio-no-permitido"
        else:
            motivo = None
        if motivo:
            self._sesiones.rechazar_ingreso(email, motivo.replace("-", "_"))
            raise RechazoDeIngreso(motivo)
        try:
            sesion = self._sesiones.ingresar_oidc(str(claims["sub"]), email, previa=previa)
        except PersonaNoHabilitada as rechazo:
            raise RechazoDeIngreso("persona-no-habilitada") from rechazo
        return sesion, datos["siguiente"]

    def _pedir(self, metodo: str, url: str, **kwargs: Any) -> httpx.Response:
        """Un pedido al proveedor; caído o con 5xx, `proveedor-no-disponible`."""
        try:
            respuesta = self._http.request(metodo, url, **kwargs)
        except httpx.HTTPError as error:
            raise RechazoDeIngreso("proveedor-no-disponible") from error
        if respuesta.is_server_error:
            raise RechazoDeIngreso("proveedor-no-disponible")
        return respuesta

    def _descubrir(self) -> dict[str, Any]:
        # shortcut: descubrimiento y JWKS se piden en cada ingreso, sin caché; alcanza para
        # unos pocos ingresos por día. Cachearlos si el ingreso se vuelve frecuente.
        respuesta = self._pedir("GET", f"{self._config.emisor}/.well-known/openid-configuration")
        try:
            datos = respuesta.json()
            # OIDC Discovery §4.3: el emisor publicado es el configurado.
            if respuesta.status_code != 200 or datos["issuer"] != self._config.emisor:
                raise ValueError(datos)
            return {clave: str(datos[clave]) for clave in _DESCUBRIMIENTO}
        except (ValueError, KeyError, TypeError) as error:
            raise RechazoDeIngreso("proveedor-no-disponible") from error

    def _canjear(self, codigo: str, datos: dict[str, Any]) -> dict[str, Any]:
        descubrimiento = self._descubrir()
        respuesta = self._pedir(
            "POST",
            descubrimiento["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": codigo,
                "redirect_uri": self._config.redireccion,
                "client_id": self._config.cliente_id,
                "client_secret": self._config.secreto,
                "code_verifier": datos["verificador"],
            },
        )
        if respuesta.status_code != 200:
            raise RechazoDeIngreso("intento-vencido")
        jwks = self._pedir("GET", descubrimiento["jwks_uri"])
        try:
            if jwks.status_code != 200:
                raise ValueError(jwks.status_code)
            claves = KeySet.import_key_set(jwks.json())
        except (ValueError, KeyError, TypeError, JoseError) as error:
            raise RechazoDeIngreso("proveedor-no-disponible") from error
        emisor = descubrimiento["issuer"]
        exigidos = jwt.JWTClaimsRegistry(
            leeway=60,
            # Google firma con o sin el esquema (su documentación de OIDC).
            iss={"essential": True, "values": [emisor, emisor.removeprefix("https://")]},
            aud={"essential": True, "value": self._config.cliente_id},
            exp={"essential": True},
            iat={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": datos["nonce"]},
        )
        try:
            token = jwt.decode(respuesta.json()["id_token"], claves, algorithms=["RS256"])
            exigidos.validate(token.claims)
            # Con varias audiencias, el token tiene que ser para este cliente (OIDC Core 3.1.3.7).
            if isinstance(token.claims["aud"], list) and (
                token.claims.get("azp") != self._config.cliente_id
            ):
                raise ValueError(token.claims["aud"])
        except (JoseError, ValueError, KeyError) as error:
            raise RechazoDeIngreso("intento-vencido") from error
        return dict(token.claims)

    def _firmar(self, intento: dict[str, Any]) -> str:
        cuerpo = _b64(json.dumps(intento).encode())
        return f"{cuerpo}.{_b64(hmac.digest(self._clave, cuerpo.encode(), 'sha256'))}"

    def _leer(self, intento: str | None) -> dict[str, Any]:
        cuerpo, _, firma = (intento or "").partition(".")
        esperada = _b64(hmac.digest(self._clave, cuerpo.encode(), "sha256"))
        if not hmac.compare_digest(firma.encode(), esperada.encode()):
            raise RechazoDeIngreso("intento-vencido")
        datos = dict(json.loads(_de_b64(cuerpo)))
        if self._reloj().timestamp() - datos["desde"] > VIDA_DEL_INTENTO:
            raise RechazoDeIngreso("intento-vencido")
        return datos
