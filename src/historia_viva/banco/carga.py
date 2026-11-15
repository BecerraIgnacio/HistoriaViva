"""La prueba de carga de `GET /v1/fragmentos` (165): N usuarios a la vez durante T segundos.

Cada usuario es un hilo que pide una consulta tras otra con el token de un Agente, como el
Agente 1, y ante un 429 espera el `Retry-After`, como pide el contrato. Con `--salud` pide
además `GET /api/salud` sin sesión después de cada consulta. El informe da, por ruta, los
pedidos, los códigos (los 429 de los cupos de `/v1` son esperables), p50, p95 y máximo de las
respuestas 200 y los pedidos por segundo. El guion es `docs/mediciones/carga.md`.

    HV_CARGA_TOKEN=hv_...[,hv_...] python -m historia_viva.banco.carga --url http://web:8000 \\
        --usuarios 5 --duracion 60 [--consultas consultas.csv] [--salud] [--csv carga.csv]

Solo la biblioteca estándar: `httpx` es de la capa de modelos (`test_arquitectura`).
"""

from __future__ import annotations

import argparse
import http.client
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from historia_viva.banco.rnf10 import resumir
from historia_viva.exportacion import escribir_csv
from historia_viva.planillas import leer_registros

__all__ = ["CONSULTAS", "Pedido", "correr", "informe", "main"]

CONSULTAS = (
    "laboratorio de ensayos de materiales",
    "convenio",
    "acta del consejo",
    "ropa formal en una oficina",
)
"""Las de los tests y los runbooks; con el conjunto de referencia, `--consultas consultas.csv`."""

TIEMPO_MAXIMO_S = 60
"""Un pedido que tarda más cuenta como sin respuesta. shortcut: es el tope de `urllib` por
operación de socket, no por pedido entero; una respuesta que gotea puede durar más. Si hiciera
falta, medir con `http.client` y cortar por reloj."""

_FRAGMENTOS = "/v1/fragmentos"
_SALUD = "/api/salud"

# Sin proxy: el blanco es la pila local, y en Windows `urllib` leería el del registro.
_abrir = build_opener(ProxyHandler({})).open


@dataclass(frozen=True)
class Pedido:
    ruta: str
    estado: int | None
    """`None`: sin respuesta (conexión rechazada, cortada o pasado `TIEMPO_MAXIMO_S`)."""
    ms: int


def _segundos(valor: str | None) -> float:
    try:
        return max(float(valor or 1), 1)
    except ValueError:  # una fecha HTTP: nuestra API no la manda
        return 1


def _pedir(url: str, cabeceras: dict[str, str]) -> tuple[int | None, float]:
    """`(estado, segundos a esperar antes del próximo pedido)`."""
    try:
        with _abrir(Request(url, headers=cabeceras), timeout=TIEMPO_MAXIMO_S) as respuesta:
            respuesta.read()
            return respuesta.status, 0
    except HTTPError as error:
        try:
            error.read()
        except (OSError, http.client.HTTPException):
            return None, 1  # el cuerpo del error se cortó: sin respuesta, como abajo
        return error.code, _segundos(error.headers.get("Retry-After")) if error.code == 429 else 0
    except (OSError, http.client.HTTPException):
        return None, 1  # sin esperar, una pila caída daría miles de pedidos por segundo


def _usuario(
    n: int, url: str, token: str, consultas: Sequence[str], hasta: float, salud: bool,
    pedidos: list[Pedido],
) -> None:
    autorizacion = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < hasta:
        q = consultas[n % len(consultas)]
        n += 1
        rutas = [(f"{_FRAGMENTOS}?{urlencode({'q': q})}", autorizacion)]
        if salud:
            rutas.append((_SALUD, {}))
        for ruta, cabeceras in rutas:
            inicio = time.monotonic()
            estado, espera = _pedir(url + ruta, cabeceras)
            # `list.append` es atómico: los hilos no necesitan candado.
            ms = round((time.monotonic() - inicio) * 1000)
            pedidos.append(Pedido(ruta.partition("?")[0], estado, ms))
            if espera:
                time.sleep(max(min(espera, hasta - time.monotonic()), 0))


def correr(
    url: str, tokens: Sequence[str], *, usuarios: int, duracion_s: float,
    consultas: Sequence[str] = CONSULTAS, salud: bool = False,
) -> list[Pedido]:
    """Los pedidos de `usuarios` hilos durante `duracion_s`; el usuario `n` usa `tokens[n % k]`.
    El último pedido de cada uno puede terminar después."""
    pedidos: list[Pedido] = []
    hasta = time.monotonic() + duracion_s
    with ThreadPoolExecutor(usuarios) as hilos:
        en_curso = [
            hilos.submit(
                _usuario, n, url.rstrip("/"), tokens[n % len(tokens)], consultas, hasta, salud,
                pedidos,
            )
            for n in range(usuarios)
        ]
        for usuario in en_curso:
            usuario.result()
    return pedidos


def _ms(valor: int | None) -> str:
    return "—" if valor is None else str(valor)


def informe(
    pedidos: Sequence[Pedido], *, segundos: float, usuarios: int, url: str, tokens: int
) -> str:
    lineas = [
        (
            f"Prueba de carga: {usuarios} usuario(s), {tokens} token(s),"
            f" {segundos:.1f} s contra {url}."
        ),
        "",
        "| Ruta | n | Códigos | p50 (ms) | p95 (ms) | Máximo (ms) | Pedidos/s |",
        "|---|---|---|---|---|---|---|",
    ]
    for ruta in sorted({p.ruta for p in pedidos}):
        de_la_ruta = [p for p in pedidos if p.ruta == ruta]
        codigos = Counter(
            "sin respuesta" if p.estado is None else str(p.estado) for p in de_la_ruta
        )
        listos = resumir(p.ms for p in de_la_ruta if p.estado == 200)
        lineas.append(
            f"| `{ruta}` | {len(de_la_ruta)} |"
            f" {', '.join(f'{c}: {k}' for c, k in sorted(codigos.items()))} |"
            f" {_ms(listos.p50)} | {_ms(listos.p95)} | {_ms(listos.maximo)} |"
            f" {f'{len(de_la_ruta) / segundos:.1f}'.replace('.', ',')} |"
        )
    lineas += ["", "p50, p95 y máximo, de las respuestas 200 (percentil por rango más cercano)."]
    return "\n".join(lineas)


def _consultas(archivo: Path) -> list[str]:
    """La columna `texto` de un CSV, como `consultas.csv` del conjunto de referencia, leído
    como lo lee `referencia`."""
    with archivo.open("rb") as entrada:
        cabecera, *filas = leer_registros(entrada, archivo.name) or [[]]
    columnas = [c.strip().lower() for c in cabecera]
    textos = [dict(zip(columnas, fila, strict=False)).get("texto", "").strip() for fila in filas]
    return [t for t in textos if len(t) >= 3]  # `/v1` rechaza con 400 las de menos de 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m historia_viva.banco.carga",
        description="Prueba de carga de GET /v1/fragmentos. El token va en HV_CARGA_TOKEN"
        " (varios, separados por coma): en la línea de comandos quedaría en el historial.",
    )
    parser.add_argument("--url", default="http://localhost")
    parser.add_argument("--usuarios", type=int, default=1)
    parser.add_argument("--duracion", type=float, default=60, help="segundos")
    parser.add_argument("--consultas", type=Path, help="CSV con una columna «texto»")
    parser.add_argument("--salud", action="store_true", help="también GET /api/salud sin sesión")
    parser.add_argument("--csv", type=Path, help="un renglón por pedido: ruta, estado, ms")
    args = parser.parse_args(argv)
    tokens = [t.strip() for t in os.environ.get("HV_CARGA_TOKEN", "").split(",") if t.strip()]
    if not tokens:
        parser.error("Falta HV_CARGA_TOKEN con el token de un Agente.")
    if args.usuarios < 1 or args.duracion <= 0:
        parser.error("--usuarios y --duracion tienen que ser positivos.")
    try:
        consultas = _consultas(args.consultas) if args.consultas else list(CONSULTAS)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not consultas:
        parser.error(f"{args.consultas} no tiene ninguna consulta en la columna «texto».")

    inicio = time.monotonic()
    pedidos = correr(
        args.url, tokens, usuarios=args.usuarios, duracion_s=args.duracion,
        consultas=consultas, salud=args.salud,
    )
    segundos = time.monotonic() - inicio
    print(informe(
        pedidos, segundos=segundos, usuarios=args.usuarios, url=args.url, tokens=len(tokens)
    ))
    if args.csv:
        filas = ((p.ruta, p.estado, p.ms) for p in pedidos)
        args.csv.write_bytes(escribir_csv(("ruta", "estado", "ms"), filas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
