"""El planificador del trabajador: tareas periódicas que no son filas de la cola (R-08).

Lo arma `composicion` con `trabajador --tipos efemeride`; hoy la única tarea es la generación de
efemérides. Despierta por sondeo y, antes de cada vuelta, consulta el mismo `habilitado()` que el
trabajador de la cola: el interruptor sin caché y el cortacircuito del motor (DEC-86, DEC-136).
Cada tarea corre con un candado consultivo de sesión (`pg_try_advisory_lock`), así dos
planificadores no corren la misma a la vez; el que no lo consigue sigue de largo.

La tarea recibe `habilitado` para consultarlo entre paso y paso: frenar a mitad de una corrida la
corta antes del paso siguiente, no en medio de uno.

Mientras corre su bucle, late como un trabajador del tipo `efemeride` (DEC-106, 059): un hilo
propio, para que una corrida larga no lo calle, que se apaga con el bucle, también si el bucle
muere. Sin latido, la salud lo da por caído y «Generar ahora» puede reemplazar el pedido que
quedó sin atender.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from uuid import uuid4

from historia_viva.bd import Bd
from historia_viva.procesamiento.latido import INTERVALO_DE_LATIDO_S, latir

__all__ = ["INTERVALO_DE_SONDEO_S", "TIPO", "Planificador", "TareaPeriodica"]

_log = logging.getLogger(__name__)

INTERVALO_DE_SONDEO_S = 5.0
"""Cada cuánto mira si hay algo que hacer: «Generar ahora» se atiende en esa demora."""

TIPO = "efemeride"
"""Con qué tipo late el planificador: el de `trabajador --tipos efemeride`."""

TareaPeriodica = Callable[[Callable[[], bool]], int]
"""Recibe `habilitado` y devuelve cuánto hizo."""


class Planificador:
    def __init__(
        self,
        *,
        bd: Bd,
        tareas: Mapping[str, TareaPeriodica],
        habilitado: Callable[[], bool],
        intervalo: float = INTERVALO_DE_SONDEO_S,
        intervalo_de_latido: float = INTERVALO_DE_LATIDO_S,
    ) -> None:
        """`tareas` va por nombre: el nombre es la clave de su candado consultivo."""
        self._bd = bd
        self._tareas = tareas
        self._habilitado = habilitado
        self._intervalo = intervalo
        self._intervalo_de_latido = intervalo_de_latido
        self.identificador = f"planificador-{os.getpid()}-{uuid4().hex[:8]}"

    def ejecutar_una_vez(self, parar: threading.Event | None = None) -> int:
        """Corre las tareas cuyo candado consigue; devuelve cuánto hicieron entre todas.
        Frenado, o con el motor caído, no corre ninguna."""

        def habilitado() -> bool:
            return not (parar and parar.is_set()) and self._habilitado()

        if not habilitado():
            return 0
        hechas = 0
        for nombre, tarea in self._tareas.items():
            with self._bd.sesion() as sesion:
                candado = sesion.execute(
                    "SELECT pg_try_advisory_lock(hashtext('planificador'), hashtext(%s)) AS tomado",
                    (nombre,),
                ).fetchone()
                if candado is None or not candado["tomado"]:
                    _log.info("La tarea %s ya corre en otro planificador.", nombre)
                    continue
                try:
                    hechas += tarea(habilitado)
                finally:
                    sesion.execute(
                        "SELECT pg_advisory_unlock(hashtext('planificador'), hashtext(%s))",
                        (nombre,),
                    )
        return hechas

    def bucle(self, parar: threading.Event) -> None:
        """Hasta que `parar` se encienda; una vuelta que falla no lo apaga. Late mientras
        corre."""
        parar_latido = threading.Event()
        latido = threading.Thread(
            target=self._latir, args=(parar_latido,), name="latido-planificador", daemon=True
        )
        latido.start()
        try:
            while not parar.is_set():
                try:
                    self.ejecutar_una_vez(parar)
                except Exception:  # noqa: BLE001 — la base caída o una fila inválida: reintenta la siguiente
                    _log.exception("El planificador no pudo completar su vuelta.")
                parar.wait(self._intervalo)
        finally:
            parar_latido.set()
            latido.join()

    def _latir(self, parar: threading.Event) -> None:
        while True:
            try:
                latir(self._bd, self.identificador, [TIPO])
            except Exception:  # noqa: BLE001 — con la base caída se sigue intentando: el latido vuelve con ella
                _log.exception("El planificador no pudo anotar su latido.")
            if parar.wait(self._intervalo_de_latido):
                return
