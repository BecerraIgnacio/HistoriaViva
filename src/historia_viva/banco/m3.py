"""M3: los tiempos de la Conversación (074; spec, módulo 27 `banco`; RNF-10, DEC-90, DEC-128, R-20).

Con el reloj del banco, desde que pide el turno: hasta el evento `fuentes` (M3), hasta el primer
`token` y hasta el último evento. Pide cada turno con `enviar_mensaje(persistir=False)`, que no
deja Conversaciones, mensajes ni auditoría. Tres condiciones: A en caliente, B en frío (los
modelos descargados antes de cada turno) y C con una foto describiéndose en bucle en el mismo
Ollama (`CargaDeFoto`). El primer turno y los turnos con historial (de 1 a 6 previos, que
reformulan antes de las fuentes, R-20) se informan por separado. El p95 hasta `fuentes` de A y C
se compara con el objetivo de 15 s y B se informa sin comparar: no hay pasa o no pasa (DEC-128).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from historia_viva.banco import celda_md, percentil, rutas_libres
from historia_viva.banco.referencia import ConsultaDeReferencia
from historia_viva.conversacion import TurnoEnMarcha, Turno
from historia_viva.dominio.tipos import FiltroDeBusqueda
from historia_viva.exportacion import escribir_csv

__all__ = [
    "CONDICIONES",
    "OBJETIVO_MS",
    "CargaDeFoto",
    "CargaNoArranca",
    "ResultadoM3",
    "Resumen",
    "TurnoMedido",
    "escribir_m3",
    "medir",
    "resumir",
    "tabla_m3",
]

_log = logging.getLogger(__name__)

OBJETIVO_MS = 15_000
"""DIR-32, RNF-10: las fuentes a la vista en menos de 15 s. Objetivo, no corte (DEC-128)."""

CONDICIONES = {"A": "en caliente", "B": "en frío", "C": "con una foto describiéndose"}
CONSULTAS = {"A": 30, "B": 5, "C": 30}
"""Las de primer turno por condición: B, 5 repeticiones en frío (spec, M3)."""
TURNOS_PREVIOS = 6
"""La serie con historial va de 1 a 6 turnos previos: los que lee `reformular` (054)."""

Enviar = Callable[[str, FiltroDeBusqueda, Sequence[Turno]], TurnoEnMarcha]


@dataclass(frozen=True)
class TurnoMedido:
    consulta_id: str
    turnos_previos: int
    fuentes_ms: float
    primer_token_ms: float | None
    """`None` sin redacción (sin evidencia o sin semántica) o si falló antes del primer token."""
    total_ms: float
    final: str
    """El último evento: `fin` o `error`. Los `error` no entran en los percentiles."""


@dataclass(frozen=True)
class Resumen:
    n: int
    p50: float | None
    p95: float | None
    maximo: float | None
    bajo_objetivo: float | None
    """La parte por debajo de 15 s."""


def resumir(valores: Sequence[float | None]) -> Resumen:
    """p50 y p95 con `percentil` (interpolación lineal, como M1)."""
    medidos = [v for v in valores if v is not None]
    if not medidos:
        return Resumen(0, None, None, None, None)
    return Resumen(
        len(medidos), percentil(medidos, 50), percentil(medidos, 95), max(medidos),
        sum(v < OBJETIVO_MS for v in medidos) / len(medidos),
    )


@dataclass(frozen=True)
class ResultadoM3:
    condicion: str
    muestras: tuple[TurnoMedido, ...]
    cpu: str
    num_thread: Mapping[str, int | None]
    """El de cada capacidad en la configuración efectiva; `None` si lo elige Ollama."""
    carga: Mapping[str, int] | None = None
    """En C, las vueltas y los errores de `describir_imagen` mientras se medía."""

    def turnos(self, *, con_historial: bool) -> list[TurnoMedido]:
        return [
            m for m in self.muestras
            if m.final == "fin" and (m.turnos_previos > 0) == con_historial
        ]

    def a_fuentes(self, *, con_historial: bool) -> Resumen:
        return resumir([m.fuentes_ms for m in self.turnos(con_historial=con_historial)])

    def a_primer_token(self, *, con_historial: bool) -> Resumen:
        return resumir([m.primer_token_ms for m in self.turnos(con_historial=con_historial)])

    def completa(self, *, con_historial: bool) -> Resumen:
        return resumir([m.total_ms for m in self.turnos(con_historial=con_historial)])

    @property
    def errores(self) -> int:
        return sum(m.final != "fin" for m in self.muestras)

    def bajo_el_objetivo(self, *, con_historial: bool) -> bool | None:
        """Si el p95 hasta `fuentes` queda por debajo de 15 s; `None` en B, que solo se informa
        (DEC-90), o sin turnos. Se informa: no corta nada (DEC-128)."""
        p95 = self.a_fuentes(con_historial=con_historial).p95
        return None if self.condicion == "B" or p95 is None else p95 < OBJETIVO_MS


def _turno(
    enviar: Enviar,
    consulta: ConsultaDeReferencia,
    historial: Sequence[Turno],
    reloj: Callable[[], float],
) -> tuple[TurnoMedido, str | None]:
    """Un turno medido y la respuesta que vio la persona (`None` si falló)."""
    comienzo = reloj()
    fuentes: float | None = None
    primer_token: float | None = None
    partes: list[str] = []
    final = ""
    with closing(enviar(consulta.texto, consulta.filtro, historial)) as eventos:
        for evento in eventos:
            ms = (reloj() - comienzo) * 1000
            if evento.tipo == "fuentes" and fuentes is None:
                fuentes = ms  # M3 termina acá, no en el último token
            elif evento.tipo == "token":
                primer_token = ms if primer_token is None else primer_token
                partes.append(evento.datos["texto"])
            final = evento.tipo
    total = (reloj() - comienzo) * 1000
    medido = TurnoMedido(
        consulta.consulta_id, len(historial), total if fuentes is None else fuentes,
        primer_token, total, final if fuentes is not None else "error",
    )
    return medido, "".join(partes) if medido.final == "fin" else None


def medir(
    enviar: Enviar,
    consultas: Sequence[ConsultaDeReferencia],
    *,
    n: int,
    reloj: Callable[[], float],
    antes_de_cada_turno: Callable[[], None] = lambda: None,
    turnos_previos: int = TURNOS_PREVIOS,
    calentar: bool = False,
) -> list[TurnoMedido]:
    """Las `n` primeras consultas como primer turno y, después, una serie con historial que
    sigue la Conversación de la primera con las siguientes (si no alcanzan, vuelve a empezar):
    de 1 a `turnos_previos` turnos previos, con las respuestas que dio la serie. `calentar`
    corre antes una consulta descartable, para que los modelos queden cargados (A y C)."""
    if not consultas:
        return []
    if calentar:
        _turno(enviar, consultas[0], (), reloj)
    muestras: list[TurnoMedido] = []
    respuestas: dict[str, str | None] = {}
    for consulta in consultas[:n]:
        antes_de_cada_turno()
        medido, respuesta = _turno(enviar, consulta, (), reloj)
        muestras.append(medido)
        respuestas.setdefault(consulta.consulta_id, respuesta)
    if consultas[0].consulta_id not in respuestas:  # n = 0: la primera igual abre la serie
        respuestas[consultas[0].consulta_id] = _turno(enviar, consultas[0], (), reloj)[1]
    historial = [Turno(consultas[0].texto, respuestas[consultas[0].consulta_id])]
    for previos in range(1, turnos_previos + 1):
        consulta = consultas[previos % len(consultas)]
        antes_de_cada_turno()
        medido, respuesta = _turno(enviar, consulta, tuple(historial), reloj)
        muestras.append(medido)
        historial.append(Turno(consulta.texto, respuesta))
    return muestras


class CargaNoArranca(Exception):
    """La primera descripción falló: sin la foto describiéndose, C no es C."""


class CargaDeFoto:
    """`describir` en bucle, en otro hilo, mientras dura el `with` (condición C). Entra recién
    cuando terminó la primera descripción (el modelo de visión ya está cargado) y al salir
    espera a que termine la que está en curso."""

    def __init__(self, describir: Callable[[], object]) -> None:
        self._describir = describir
        self._parar = threading.Event()
        self._primera = threading.Event()
        self._hilo = threading.Thread(target=self._bucle, name="carga-de-foto", daemon=True)
        self.vueltas = 0
        self.errores = 0

    @property
    def activa(self) -> bool:
        return self._hilo.is_alive()

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:
                self._describir()
                self.vueltas += 1
            except Exception as error:  # noqa: BLE001 — la carga sigue: se cuenta y se informa
                if not self.errores:
                    _log.warning("Falló describir_imagen en la carga de C: %s", error)
                self.errores += 1
            finally:
                self._primera.set()

    def __enter__(self) -> Self:
        self._hilo.start()
        self._primera.wait()
        if not self.vueltas:
            self.__exit__()
            raise CargaNoArranca("La primera descripción de la foto falló: C no se mide.")
        return self

    def __exit__(self, *_: object) -> None:
        self._parar.set()
        self._hilo.join()

    def informe(self) -> dict[str, int]:
        return {"vueltas": self.vueltas, "errores": self.errores}


# ----------------------------------------------------------------------------- salida ---


def _s(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} s".replace(".", ",")


_SI_NO = {True: "sí", False: "no", None: "—"}


def tabla_m3(resultado: ResultadoM3, *, hash_referencia: str) -> str:
    c = resultado.condicion
    lineas = [
        (f"M3, condición {c} ({CONDICIONES[c]}): tiempo hasta que la Conversación muestra las"
        " fuentes."),
        "",
        ("| Turno | n | p50 fuentes | p95 fuentes | máx. fuentes | fuentes < 15 s"
        " | p95 primer token | p95 respuesta completa | p95 < 15 s |"),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for nombre, historial in (("primer turno", False), ("con historial", True)):
        fuentes = resultado.a_fuentes(con_historial=historial)
        parte = (
            "—" if fuentes.bajo_objetivo is None
            else f"{fuentes.bajo_objetivo:.0%}".replace("%", " %")
        )
        comparado = "se informa" if c == "B" else _SI_NO[
            resultado.bajo_el_objetivo(con_historial=historial)
        ]
        lineas.append(
            f"| {nombre} | {fuentes.n} | {_s(fuentes.p50)} | {_s(fuentes.p95)}"
            f" | {_s(fuentes.maximo)} | {parte}"
            f" | {_s(resultado.a_primer_token(con_historial=historial).p95)}"
            f" | {_s(resultado.completa(con_historial=historial).p95)} | {comparado} |"
        )
    lineas += [
        "",
        (f"Objetivo: fuentes en menos de {OBJETIVO_MS // 1000} s (DIR-32). Se mide y se informa:"
        " no es un corte de pasa o no pasa (DEC-128). El p95 de A y C se compara con el objetivo;"
        " B se informa sin comparar (DEC-90)."),
        "Con historial: de 1 a 6 turnos previos; reformula antes de las fuentes (R-20).",
        f"CPU: {celda_md(resultado.cpu)}. num_thread: "
        + (", ".join(
            f"{celda_md(capacidad)} {'sin fijar' if hilos is None else hilos}"
            for capacidad, hilos in sorted(resultado.num_thread.items())
        ) or "—") + ".",
        f"hash_referencia: {celda_md(hash_referencia)}.",
    ]
    if resultado.carga is not None:
        lineas.append(
            f"Carga de describir_imagen: {resultado.carga['vueltas']} descripción(es) terminadas"
            f" y {resultado.carga['errores']} con error mientras se medía."
        )
    if resultado.errores:
        lineas.append(f"**Aviso:** {resultado.errores} turno(s) terminaron en `error` y no cuentan.")
    return "\n".join(lineas) + "\n"


_COLUMNAS = ("consulta_id", "turnos_previos", "fuentes_ms", "primer_token_ms", "total_ms", "final")


def escribir_m3(
    directorio: Path,
    resultado: ResultadoM3,
    *,
    hash_referencia: str,
    configuracion_efectiva: Mapping[str, Any],
    momento: datetime,
) -> list[Path]:
    """El JSON, el CSV (un turno por fila) y la tabla de la corrida."""
    nombre = f"m3-{resultado.condicion}-{momento:%Y%m%dT%H%M%S}"
    directorio.mkdir(parents=True, exist_ok=True)

    def series(resumen: Callable[..., Resumen]) -> dict[str, Any]:
        return {
            nombre: vars(resumen(con_historial=historial))
            for nombre, historial in (("primer_turno", False), ("con_historial", True))
        }

    filas = [[getattr(m, c) for c in _COLUMNAS] for m in resultado.muestras]
    datos = {
        "medida": "m3",
        "momento": momento.isoformat(),
        "condicion": resultado.condicion,
        "hash_referencia": hash_referencia,
        "configuracion_efectiva": configuracion_efectiva,
        "cpu": resultado.cpu,
        "num_thread": dict(resultado.num_thread),
        "carga": None if resultado.carga is None else dict(resultado.carga),
        "objetivo_ms": OBJETIVO_MS,
        "a_fuentes": series(resultado.a_fuentes),
        "a_primer_token": series(resultado.a_primer_token),
        "completa": series(resultado.completa),
        "p95_bajo_el_objetivo": {
            "primer_turno": resultado.bajo_el_objetivo(con_historial=False),
            "con_historial": resultado.bajo_el_objetivo(con_historial=True),
        },
        "errores": resultado.errores,
        "muestras": [dict(zip(_COLUMNAS, f, strict=True)) for f in filas],
    }
    rutas = rutas_libres(directorio, nombre)
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(_COLUMNAS, filas))
    rutas[2].write_text(tabla_m3(resultado, hash_referencia=hash_referencia), encoding="utf-8")
    return rutas
