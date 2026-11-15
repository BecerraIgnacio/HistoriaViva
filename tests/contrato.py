"""El OpenAPI `/v1` congelado con Juan (002) como verificador de las respuestas reales (104).

`errores` valida estado, cabeceras declaradas (`RateLimit-*`, si vienen) y cuerpo con
`jsonschema` (Draft 2020-12, con formatos): como todo objeto del contrato es
`additionalProperties: false`, un campo de más falla.
`verificar` es el gancho de httpx que lo corre sobre cada respuesta de `/v1` de un `TestClient`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator

ARCHIVO = Path(__file__).resolve().parents[1] / "docs" / "openapi-agentes.yaml"
CONTRATO: dict[str, Any] = yaml.safe_load(ARCHIVO.read_text(encoding="utf-8"))
COMPONENTES = CONTRATO["components"]

OPERACIONES: dict[tuple[str, str], dict[str, Any]] = {
    (metodo.upper(), ruta): op
    for ruta, item in CONTRATO["paths"].items()
    for metodo, op in item.items()
    if isinstance(op, dict) and "responses" in op
}
"""`(método, plantilla de la ruta)` → la operación del contrato."""

_PATRONES = {
    clave: re.compile(re.sub(r"\{[^}]+\}", "[^/]+", clave[1]) + "$") for clave in OPERACIONES
}

# Una ruta o un método que el contrato no tiene llega a la comodín: autentica, mira el freno y
# el cupo, y responde 404 `ruta_inexistente` (098).
_COMODIN = {
    estado: {"$ref": f"#/components/responses/{nombre}"}
    for estado, nombre in {
        "401": "V1NoAutenticado",
        "404": "V1RutaInexistente",
        "429": "V1DemasiadasConsultas",
        "503": "V1NoDisponible",
    }.items()
}


def _resolver(nodo: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in nodo:
        seccion, nombre = nodo["$ref"].removeprefix("#/components/").split("/")
        nodo = COMPONENTES[seccion][nombre]
    return nodo


def _validador(esquema: dict[str, Any]) -> Draft202012Validator:
    # El esquema viaja con los componentes para que sus `$ref` locales resuelvan.
    return Draft202012Validator(
        {**esquema, "components": COMPONENTES},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def respuestas(metodo: str, ruta: str) -> dict[str, Any]:
    """Las respuestas que el contrato declara para un pedido concreto (`/v1/piezas/pza_…`)."""
    for clave, patron in _PATRONES.items():
        if clave[0] == metodo.upper() and patron.match(ruta):
            return dict(OPERACIONES[clave]["responses"])
    return _COMODIN


def errores_del_cuerpo(metodo: str, ruta: str, estado: int, cuerpo: Any) -> list[str]:
    declaradas = respuestas(metodo, ruta)
    if str(estado) not in declaradas:
        return [f"el {estado} no está declarado"]
    esquema = _resolver(declaradas[str(estado)])["content"]["application/json"]["schema"]
    return [
        f"{'/'.join(map(str, e.absolute_path)) or 'cuerpo'}: {e.message}"
        for e in _validador(esquema).iter_errors(cuerpo)
    ]


def errores(
    metodo: str, ruta: str, estado: int, cabeceras: Mapping[str, str], cuerpo: Any
) -> list[str]:
    """Todo lo que la respuesta no cumple del contrato; vacío si lo cumple."""
    fallas = errores_del_cuerpo(metodo, ruta, estado, cuerpo)
    if str(estado) not in respuestas(metodo, ruta):
        return fallas
    declarada = _resolver(respuestas(metodo, ruta)[str(estado)])
    for nombre, cabecera in declarada.get("headers", {}).items():
        valor: Any = cabeceras.get(nombre)
        if valor is None:
            # `RateLimit-*` va con todo token válido (098), y eso lo prueba `test_api_agentes`;
            # un 503 por una falla al autenticar no tiene token ni ventana que informar.
            if not nombre.startswith("RateLimit-"):
                fallas.append(f"falta la cabecera {nombre}")
            continue
        esquema = _resolver(cabecera)["schema"]
        if esquema.get("type") == "integer" and re.fullmatch(r"-?\d+", valor):
            valor = int(valor)
        fallas += [f"{nombre}: {e.message}" for e in _validador(esquema).iter_errors(valor)]
    if (
        isinstance(cuerpo, dict) and "solicitud_id" in cuerpo
        and cuerpo["solicitud_id"] != cabeceras.get("X-Solicitud-Id")
    ):
        fallas.append("el solicitud_id del cuerpo no es el de X-Solicitud-Id")
    return fallas


def verificar(respuesta: httpx.Response) -> None:
    """Gancho `response` de httpx: falla el test si una respuesta de `/v1` se sale del contrato."""
    ruta = respuesta.request.url.path
    if not (ruta == "/v1" or ruta.startswith("/v1/")):
        return
    respuesta.read()
    metodo = respuesta.request.method
    fallas = errores(metodo, ruta, respuesta.status_code, respuesta.headers, respuesta.json())
    assert not fallas, f"{metodo} {ruta} {respuesta.status_code} no cumple el contrato: {fallas}"
