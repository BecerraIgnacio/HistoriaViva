"""Un Google de mentira: el proveedor OIDC contra el que se prueba el ingreso (036, DEC-146
punto 15) hasta que IT entregue el cliente real (C18, ticket 127).

Habla OIDC de verdad: descubrimiento, autorización con PKCE S256, canje del `code` con el
secreto del cliente y un ID token RS256 firmado con una clave propia que publica por JWKS. La
suite lo inyecta en proceso (es una app ASGI: `TestClient(proveedor.app)` es el `httpx.Client`
de `identidad`); la prueba de humo de 121 lo levanta como servicio aparte:

    uv run python tests/proveedor_oidc_falso.py --puerto 9000 --emisor http://127.0.0.1:9000 \\
        --cliente-id humo --secreto humo

`/autorizar` decide quién entra por su query, que la suite agrega a la URL que arma la app:
`email`, `sub` (por defecto, uno derivado del email), `verificado` (`false` para un correo sin
verificar), `hd_reclamado` (por defecto, el dominio del email; vacío no manda `hd`) y `trampa`
(`nonce`, `firma`, `iss`, `aud`, `exp`, `sin_iat`, `aud_lista` o `aud_lista_con_azp`:
el token sale así). Sin `email` muestra un
formulario para escribirlo a mano.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc import jwk, jwt

EMISOR = "https://proveedor-falso.test"
CLIENTE_ID = "cliente-de-prueba"
SECRETO = "secreto-de-prueba"


@dataclass
class ProveedorFalso:
    emisor: str = EMISOR
    cliente_id: str = CLIENTE_ID
    secreto: str = SECRETO
    clave: jwk.RSAKey = field(
        default_factory=lambda: jwk.RSAKey.generate_key(2048, {"kid": "falsa-1", "use": "sig"})
    )
    _codigos: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def app(self) -> FastAPI:
        app = FastAPI()
        app.get("/.well-known/openid-configuration")(self._descubrimiento)
        app.get("/jwks")(self._jwks)
        app.get("/autorizar")(self._autorizar)
        app.post("/token")(self._token)
        return app

    @staticmethod
    def descubrimiento(emisor: str) -> dict[str, str]:
        return {
            "issuer": emisor,
            "authorization_endpoint": f"{emisor}/autorizar",
            "token_endpoint": f"{emisor}/token",
            "jwks_uri": f"{emisor}/jwks",
        }

    def _descubrimiento(self) -> dict[str, str]:
        return self.descubrimiento(self.emisor)

    def _jwks(self) -> dict[str, Any]:
        return {"keys": [self.clave.as_dict(private=False)]}

    def _autorizar(self, request: Request) -> Any:
        q = request.query_params
        if q.get("client_id") != self.cliente_id or q.get("code_challenge_method") != "S256":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        email = q.get("email")
        if not email:
            ocultos = "".join(
                f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                for k, v in q.items()
            )
            return HTMLResponse(
                f'<form method="get">{ocultos}<label>Email <input name="email"></label>'
                '<label>sub <input name="sub"></label><button>Entrar</button></form>'
            )
        dominio = email.rpartition("@")[2]
        codigo = secrets.token_urlsafe(16)
        self._codigos[codigo] = {
            "redirect_uri": q["redirect_uri"],
            "code_challenge": q["code_challenge"],
            "nonce": q.get("nonce"),
            "email": email,
            "sub": q.get("sub") or "sub-" + hashlib.sha256(email.encode()).hexdigest()[:16],
            "verificado": q.get("verificado", "true") == "true",
            "hd": q.get("hd_reclamado", dominio),
            "trampa": q.get("trampa"),
        }
        vuelta = urlencode({"code": codigo, "state": q.get("state", "")})
        return RedirectResponse(f"{q['redirect_uri']}?{vuelta}", status_code=302)

    def _token(
        self,
        grant_type: str = Form(),
        code: str = Form(),
        redirect_uri: str = Form(),
        client_id: str = Form(),
        client_secret: str = Form(),
        code_verifier: str = Form(""),
    ) -> JSONResponse:
        datos = self._codigos.pop(code, None)  # un `code` sirve una sola vez
        reto = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        if (
            grant_type != "authorization_code"
            or (client_id, client_secret) != (self.cliente_id, self.secreto)
            or datos is None
            or datos["redirect_uri"] != redirect_uri
            or reto.rstrip(b"=").decode() != datos["code_challenge"]
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse({"id_token": self._id_token(datos), "token_type": "Bearer"})

    def _id_token(self, datos: dict[str, Any]) -> str:
        ahora = int(time.time())
        trampa = datos["trampa"]
        claims: dict[str, Any] = {
            "iss": "https://otro-emisor.test" if trampa == "iss" else self.emisor,
            "aud": {
                "aud": "otro-cliente",
                "aud_lista": [self.cliente_id, "otro-cliente"],
                "aud_lista_con_azp": [self.cliente_id, "otro-cliente"],
            }.get(trampa or "", self.cliente_id),
            "iat": ahora - 7200 if trampa == "exp" else ahora,
            "exp": ahora - 3600 if trampa == "exp" else ahora + 3600,
            "nonce": "otro-nonce" if trampa == "nonce" else datos["nonce"],
            "sub": datos["sub"],
            "email": datos["email"],
            "email_verified": datos["verificado"],
        }
        if datos["hd"]:
            claims["hd"] = datos["hd"]
        if trampa == "aud_lista_con_azp":
            claims["azp"] = self.cliente_id
        if trampa == "sin_iat":
            del claims["iat"]
        clave = self.clave
        if trampa == "firma":
            clave = jwk.RSAKey.generate_key(2048, {"kid": "falsa-1"})
        return jwt.encode({"alg": "RS256", "kid": "falsa-1"}, claims, clave)


def main() -> None:
    import uvicorn

    argumentos = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # En el contenedor de la prueba de humo (`compose.humo.yml`), `0.0.0.0`: lo llaman `web` y
    # `humo` por la red del compose.
    argumentos.add_argument("--host", default="127.0.0.1")
    argumentos.add_argument("--puerto", type=int, default=9000)
    argumentos.add_argument("--emisor", default="http://127.0.0.1:9000")
    argumentos.add_argument("--cliente-id", default=CLIENTE_ID)
    argumentos.add_argument("--secreto", default=SECRETO)
    leidos = argumentos.parse_args()
    proveedor = ProveedorFalso(
        emisor=leidos.emisor, cliente_id=leidos.cliente_id, secreto=leidos.secreto
    )
    uvicorn.run(proveedor.app, host=leidos.host, port=leidos.puerto)


if __name__ == "__main__":
    main()
