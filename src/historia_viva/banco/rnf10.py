"""El informe del guion manual de RNF-10 (056; S-14, S-15, S-24; DEC-128).

Una persona corre las consultas en P6 siguiendo `docs/mediciones/rnf-10.md`, anota la ventana de
cada condición (A caliente, B frío, C con una foto describiéndose) y este comando lee los tiempos
que guardó cada respuesta. Compara con el objetivo de 15 s sin cortar: no hay pasa o no pasa.
M3 formal, con `banco m3`, es de 074.

    HV_BD_URL=... uv run python -m historia_viva.banco.rnf10 --cpu "i7-1255U, 12 hilos" \\
        --condicion A 2026-10-20T10:00-03:00 2026-10-20T10:40-03:00 --condicion B ...
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from historia_viva.bd import Bd
from historia_viva.conversacion import TiempoGuardado, tiempos_guardados

__all__ = ["OBJETIVO_MS", "Condicion", "Resumen", "informe", "main", "resumir"]

OBJETIVO_MS = 15_000
"""RNF-10: las fuentes a la vista en menos de 15 s. Objetivo, no corte (DEC-128)."""

_SIN_REDACCION_COMPLETA = frozenset(("error", "interrumpida"))
"""Sus tiempos no son de un turno entero: se cuentan aparte y no entran."""


@dataclass(frozen=True)
class Condicion:
    nombre: str
    tiempos: tuple[TiempoGuardado, ...]


@dataclass(frozen=True)
class Resumen:
    n: int
    p50: int | None
    p95: int | None
    maximo: int | None
    bajo_objetivo: float | None
    """La parte de los valores por debajo de 15 s."""


def resumir(valores: Iterable[int | None]) -> Resumen:
    """Percentiles por rango más cercano: con pocas consultas el p95 es un valor medido."""
    orden = sorted(v for v in valores if v is not None)
    if not orden:
        return Resumen(0, None, None, None, None)

    def rango(p: float) -> int:
        return orden[math.ceil(p * len(orden)) - 1]

    bajo = sum(v < OBJETIVO_MS for v in orden) / len(orden)
    return Resumen(len(orden), rango(0.5), rango(0.95), orden[-1], bajo)


def _s(ms: int | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} s".replace(".", ",")


def _fila(condicion: str, serie: str, tiempos: Sequence[TiempoGuardado]) -> str:
    fuentes = resumir(t.fuentes_ms for t in tiempos)
    primer_token = resumir(t.primer_token_ms for t in tiempos)
    total = resumir(t.total_ms for t in tiempos)
    bajo = "—" if fuentes.bajo_objetivo is None else f"{fuentes.bajo_objetivo:.0%}".replace("%", " %")
    return (
        f"| {condicion} | {serie} | {fuentes.n} | {_s(fuentes.p50)} | {_s(fuentes.p95)} |"
        f" {_s(fuentes.maximo)} | {bajo} | {_s(primer_token.p95)} | {_s(total.p95)} |"
    )


def _num_thread(nombre: str, tiempos: Sequence[TiempoGuardado]) -> tuple[str, bool]:
    """La línea de `num_thread` por capacidad y si quedó fijo en toda la condición."""
    vistos: dict[str, set[int | None]] = {}
    for t in tiempos:
        for capacidad, hilos in t.num_thread.items():
            vistos.setdefault(capacidad, set()).add(hilos)
    partes = [
        f"{capacidad} "
        + " / ".join([str(h) for h in sorted(h for h in hilos if h is not None)]
                     + (["sin fijar"] if None in hilos else []))
        for capacidad, hilos in sorted(vistos.items())
    ]
    fijo = all(len(h) == 1 and None not in h for h in vistos.values())
    return f"num_thread {nombre}: {', '.join(partes) or 'sin redacciones'}", fijo


def informe(condiciones: Sequence[Condicion], *, cpu: str) -> str:
    lineas = [
        f"CPU: {cpu}",
        "",
        (f"Objetivo: fuentes en menos de {OBJETIVO_MS // 1000} s (RNF-10). Es un objetivo, no corte"
        " (DEC-128)."),
        "",
        ("| Condición | Turno | n | p50 fuentes | p95 fuentes | máx. fuentes | fuentes < 15 s"
        " | p95 primer token | p95 fin |"),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    notas: list[str] = []
    for condicion in condiciones:
        cuentan = [t for t in condicion.tiempos if t.estado not in _SIN_REDACCION_COMPLETA]
        lineas.append(_fila(condicion.nombre, "primer turno", [t for t in cuentan if not t.con_historial]))
        lineas.append(_fila(condicion.nombre, "con historial", [t for t in cuentan if t.con_historial]))
        if fuera := len(condicion.tiempos) - len(cuentan):
            notas.append(
                f"{condicion.nombre}: {fuera} respuesta(s) en `error` o `interrumpida` no cuentan."
            )
        linea, fijo = _num_thread(condicion.nombre, cuentan)
        notas.append(linea)
        if not fijo:
            notas.append(
                f"Atención: `num_thread` no quedó fijo en {condicion.nombre}; el guion pide fijarlo"
                " en la fila de cada capacidad antes de medir."
            )
    return "\n".join([*lineas, "", *notas, ""])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="historia_viva.banco.rnf10",
        description="El informe del guion manual de RNF-10 con los tiempos guardados.",
    )
    parser.add_argument("--cpu", required=True, help="modelo de CPU e hilos, como lo anotó el guion")
    parser.add_argument(
        "--condicion", nargs=3, action="append", required=True,
        metavar=("NOMBRE", "DESDE", "HASTA"),
        help="la ventana de una condición, en ISO 8601 con zona horaria",
    )
    argumentos = parser.parse_args(argv)
    dsn = os.environ.get("HV_BD_URL")
    if not dsn:
        print("Falta HV_BD_URL.", file=sys.stderr)
        return 2
    ventanas = [
        (nombre, datetime.fromisoformat(desde), datetime.fromisoformat(hasta))
        for nombre, desde, hasta in argumentos.condicion
    ]
    if any(momento.tzinfo is None for _, *bordes in ventanas for momento in bordes):
        # Sin zona, Postgres la leería en la de la sesión y la ventana se correría.
        print("Cada hora va con zona horaria, como da `Get-Date -Format o`.", file=sys.stderr)
        return 2
    bd = Bd(dsn, tamano_pool=1)
    try:
        with bd.transaccion() as tx:
            condiciones = [
                Condicion(nombre, tiempos_guardados(tx, desde=desde, hasta=hasta))
                for nombre, desde, hasta in ventanas
            ]
    finally:
        bd.cerrar()
    print(informe(condiciones, cpu=argumentos.cpu))
    return 0


if __name__ == "__main__":
    sys.exit(main())
