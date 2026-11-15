"""El proceso `trabajador`: toma de la cola los trabajos de sus tipos y los corre.

Sin Celery ni Redis (`.scratch/prototipo/spec.md`, "Qué se resuelve barato"): el consumidor
de la cola es el proceso `trabajador`, aparte de `web` (historia 50), que se entera por sondeo
(DEC-106). Se lanza uno por tipo (`--tipos procesamiento`, `--tipos indexacion`) con el mapa
tipo → manejador que arma `composicion` (DEC-08): así indexar no espera a la descripción de
una foto. El trabajador no sabe qué hace un trabajo; el manejador no sabe de la cola.

El plan del manejador —minutos de modelo— corre **sin transacción abierta**; solo su resultado
y el cierre del trabajo van en una, así un éxito nunca termina `fallido` y un trabajo que ya
no es de este trabajador no deja nada (DEC-99). Corre en un hilo aparte: si corriera en el
bucle de eventos, la aplicación dejaría de contestar justamente mientras procesa.

El latido va en otro hilo, propio: un plan de minutos no lo calla y la salud no da por caído a
un trabajador ocupado (DEC-106).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from historia_viva.bd import Bd, Tx
from historia_viva.dominio.errores import ErrorHistoriaViva, FichaCambio, MotorNoAlcanzable
from historia_viva.procesamiento.cola import (
    Cola,
    TipoDeTrabajo,
    TrabajoNoTomado,
    TrabajoTomado,
)
from historia_viva.procesamiento.latido import INTERVALO_DE_LATIDO_S, latir

__all__ = ["Cortacircuito", "Manejador", "ResultadoDeTrabajo", "Trabajador"]

_log = logging.getLogger(__name__)

_ESPERA_AL_DETENER_S = 30.0
"""Cuánto se le da al trabajo en curso para terminar cuando se apaga la aplicación."""

_INTERVALO_DE_SONDEO_S = 2.0
"""Cada cuánto mira la cola cuando nadie lo despertó. El aviso de `despertar` hace que el
caso normal no espere nada; esto es la red por si un aviso se pierde."""

ESPERA_TRAS_MOTOR_NO_ALCANZABLE_S = 5.0
"""Cuánto no se toma nada después de devolver un trabajo que no llegó al motor, para no
martillar a un motor caído (DEC-146 punto 2; tabla de DEC-145)."""

INTENTOS_ANTE_FICHA_CAMBIO = 2
"""Una toma corre el trabajo una vez y lo reintenta una sola vez si la Ficha cambió; si vuelve a
cambiar, no hay tercera: el trabajo vuelve `encolado` sin gastar generación (DEC-146 punto 4)."""

CACHE_DE_SALUD_S = 10.0
"""Cuánto vale la última respuesta del motor para el cortacircuito (DEC-86; tabla de DEC-145).
La espera de cada consulta, 3 s, es la de `verificar_salud`."""


@dataclass(frozen=True, kw_only=True)
class ResultadoDeTrabajo:
    """Lo que el plan midió; cada manejador lo extiende con lo que guarda."""

    tokens_entrada: int = 0
    tokens_salida: int = 0


class Manejador[R: ResultadoDeTrabajo](Protocol):
    """Lo que hace un tipo de trabajo (Strategy): uno por tipo, inyectado por `composicion`."""

    def ejecutar(self, trabajo: TrabajoTomado) -> R:
        """Corre el plan sin transacción abierta (DEC-99)."""
        ...

    def guardar(self, tx: Tx, trabajo: TrabajoTomado, resultado: R) -> None:
        """Guarda el resultado en la transacción en la que se cierra el trabajo."""
        ...

    def despues_de_completar(self, tx: Tx, trabajo: TrabajoTomado, resultado: R) -> None:
        """Gancho en la misma transacción, después de `completar` (DEC-146 punto 5): la
        indexación reencola si cambió el modelo; el resto no hace nada."""
        ...


def _mensaje_para_la_persona(error: Exception) -> str:
    """El porqué que ve quien subió la Pieza; el detalle técnico queda en el log."""
    if isinstance(error, ErrorHistoriaViva):
        return str(error)
    return f"Error interno del procesamiento ({type(error).__name__})."


class Cortacircuito:
    """Si el motor del tipo responde, preguntado a lo sumo cada `CACHE_DE_SALUD_S` (DEC-86).

    Con el motor caído el trabajador no toma: un Ollama caído no deja fallido un Lote entero.
    Falla cerrado: si no se puede saber, el motor se da por caído.
    """

    def __init__(
        self, motor_vivo: Callable[[], bool], *, reloj: Callable[[], float] = time.monotonic
    ) -> None:
        self._motor_vivo = motor_vivo
        self._reloj = reloj
        self._vivo = False
        self._vence = -math.inf

    def __call__(self) -> bool:
        ahora = self._reloj()
        if ahora >= self._vence:
            try:
                self._vivo = self._motor_vivo()
            except Exception:  # noqa: BLE001 — la base o la configuración: sin saberlo, no se toma
                _log.exception("No se pudo saber si el motor responde; se da por caído.")
                self._vivo = False
            self._vence = ahora + CACHE_DE_SALUD_S
        return self._vivo


class Trabajador:
    """Toma los trabajos de los tipos de `manejadores` y los corre de a uno.

    `habilitado` es el freno que arma `composicion`: el interruptor (ADR-0004, DEC-88), leído
    sin caché, y el `Cortacircuito` del motor del tipo (DEC-86). Se consulta antes de cada
    toma y, si dice que no, no se toma nada; el trabajo en curso termina su plan.
    """

    def __init__(
        self,
        *,
        bd: Bd,
        cola: Cola,
        manejadores: Mapping[TipoDeTrabajo, Manejador[Any]],
        habilitado: Callable[[], bool],
        reloj: Callable[[], float] = time.monotonic,
        intervalo_de_latido: float = INTERVALO_DE_LATIDO_S,
    ) -> None:
        self._bd = bd
        self._cola = cola
        self._manejadores = manejadores
        self._habilitado = habilitado
        self._reloj = reloj
        self._intervalo_de_latido = intervalo_de_latido
        self._parar_latido = threading.Event()
        self._latido: threading.Thread | None = None
        self._no_tomar_hasta = -math.inf
        self._hay_trabajo = asyncio.Event()
        self._detenido = False
        self._tarea: asyncio.Task[None] | None = None
        self._bucle_de_eventos: asyncio.AbstractEventLoop | None = None

    async def iniciar(self) -> None:
        self._detenido = False
        self._bucle_de_eventos = asyncio.get_running_loop()
        self._parar_latido.clear()
        self._latido = threading.Thread(target=self._latir, name="latido", daemon=True)
        self._latido.start()
        self._tarea = asyncio.create_task(self._bucle())

    async def detener(self) -> None:
        """Corta el bucle y espera a que termine el trabajo en curso, si lo hay."""
        self._detenido = True
        self._hay_trabajo.set()
        self._parar_latido.set()
        if self._latido is not None:
            await asyncio.to_thread(self._latido.join)
            self._latido = None
        if self._tarea is None:
            return
        try:
            await asyncio.wait_for(self._tarea, timeout=_ESPERA_AL_DETENER_S)
        except (TimeoutError, asyncio.CancelledError):
            # Se apaga igual: el trabajo queda `en_curso` en la base y vuelve solo a la cola
            # cuando vence su arriendo (`Cola.tomar` lo reclama al arrancar de nuevo). Acá lo
            # único que importa es que apagar la aplicación no se quede esperando.
            _log.warning("El trabajador se apagó con un trabajo todavía en curso.")
        self._tarea = None

    def despertar(self) -> None:
        """Avisa que hay trabajo. Lo llama `ingesta` después del `COMMIT`, desde el hilo que
        atiende el pedido: antes, el trabajo todavía no se ve en la cola."""
        bucle = self._bucle_de_eventos
        if bucle is not None and not bucle.is_closed():
            bucle.call_soon_threadsafe(self._hay_trabajo.set)

    def procesar_siguiente(self) -> bool:
        """Toma un trabajo de sus tipos y lo corre. Devuelve si había alguno.

        Un fallo deja el trabajo `fallido` en esa toma, sin reintento automático (DEC-113),
        salvo un pedido que no llegó al motor: vuelve a la cola sin gastar la generación y no
        se toma nada por `ESPERA_TRAS_MOTOR_NO_ALCANZABLE_S` (DEC-86).

        Es sincrónico porque todo lo que hace lo es —la capa de modelos y la base— y el bucle
        lo corre en un hilo.
        """
        if self._reloj() < self._no_tomar_hasta or not self._habilitado():
            return False
        trabajo = self._cola.tomar(self._manejadores.keys())
        if trabajo is None:
            return False
        manejador = self._manejadores[trabajo.tipo]
        comenzado_en = time.monotonic()
        try:
            for intento in range(1, INTENTOS_ANTE_FICHA_CAMBIO + 1):
                try:
                    self._ejecutar_y_guardar(manejador, trabajo, comenzado_en)
                    break
                except FichaCambio:
                    if intento == INTENTOS_ANTE_FICHA_CAMBIO:
                        raise
                    _log.info("La Ficha de %s cambió al indexar: se reintenta.", trabajo.pieza_id)
        except TrabajoNoTomado:
            _log.warning("El trabajo %s ya no era de este trabajador.", trabajo.trabajo_id)
        except FichaCambio:
            # La Ficha se sigue editando: vuelve a la cola sin gastar la generación (DEC-146
            # punto 4) y la próxima toma indexa la que esté vigente.
            _log.warning("La Ficha de %s sigue cambiando: vuelve a la cola.", trabajo.pieza_id)
            # Sin espera, la vuelta siguiente lo retomaría al instante, en plena edición.
            self._no_tomar_hasta = self._reloj() + ESPERA_TRAS_MOTOR_NO_ALCANZABLE_S
            with contextlib.suppress(TrabajoNoTomado):
                self._cola.devolver(trabajo)
        except MotorNoAlcanzable:
            _log.warning("El trabajo %s no llegó al motor: vuelve a la cola.", trabajo.trabajo_id)
            self._no_tomar_hasta = self._reloj() + ESPERA_TRAS_MOTOR_NO_ALCANZABLE_S
            with contextlib.suppress(TrabajoNoTomado):
                self._cola.devolver(trabajo)
        except Exception as error:  # noqa: BLE001 — el bucle no puede morir por un trabajo que falla
            _log.exception("Falló el trabajo %s de la Pieza %s.", trabajo.tipo, trabajo.pieza_id)
            try:
                self._cola.fallar(trabajo, error=_mensaje_para_la_persona(error))
            except TrabajoNoTomado:
                _log.warning("El trabajo %s ya no era de este trabajador.", trabajo.trabajo_id)
        return True

    def _ejecutar_y_guardar(
        self, manejador: Manejador[Any], trabajo: TrabajoTomado, comenzado_en: float
    ) -> None:
        resultado = manejador.ejecutar(trabajo)
        with self._bd.transaccion() as tx:
            manejador.guardar(tx, trabajo, resultado)
            self._cola.completar(
                tx,
                trabajo,
                duracion_ms=int((time.monotonic() - comenzado_en) * 1000),
                tokens_entrada=resultado.tokens_entrada,
                tokens_salida=resultado.tokens_salida,
            )
            manejador.despues_de_completar(tx, trabajo, resultado)

    def _latir(self) -> None:
        """Anota el latido cada `intervalo_de_latido` hasta que se detiene el trabajador."""
        tipos = [tipo.value for tipo in self._manejadores]
        while True:
            try:
                latir(self._bd, self._cola.identificador, tipos)
            except Exception:  # noqa: BLE001 — con la base caída se sigue intentando: el latido vuelve con ella
                _log.exception("El trabajador no pudo anotar su latido.")
            if self._parar_latido.wait(self._intervalo_de_latido):
                return

    async def _bucle(self) -> None:
        while not self._detenido:
            # Se limpia antes de mirar la cola: un aviso que llegue mientras se procesa no
            # se pierde, a lo sumo provoca una vuelta de más.
            self._hay_trabajo.clear()
            try:
                habia_trabajo = await asyncio.to_thread(self.procesar_siguiente)
            except Exception:  # noqa: BLE001 — una base momentáneamente caída no apaga al trabajador
                _log.exception("El trabajador no pudo tomar trabajo de la cola.")
                habia_trabajo = False
            if habia_trabajo:
                continue
            if self._detenido:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._hay_trabajo.wait(), _INTERVALO_DE_SONDEO_S)
