"""La configuración vigente de los modelos: una fila por capacidad (spec, módulo `configuracion`).

Es la única puerta a `configuracion_capacidad`. El Administrador técnico edita la fila desde la
pantalla de Modelos (`actualizar`, CU-19) o por SQL (DEC-87), y rige para los trabajos que se
encolan después: cada trabajo congela su `instantanea` al encolar. Qué es una fila válida no se
decide acá: lo decide el `validar` de `capa_modelos`, que este módulo recibe con los motores del
entorno y corre al tomar la instantánea y al guardar.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from historia_viva.auditoria import Accion, Actor, Evento, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd, Tx
from historia_viva.capa_modelos.configuracion import (
    ConfiguracionDeCapacidad,
    ConfiguracionEfectiva,
)
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.dominio.errores import (
    ConfiguracionInvalida,
    ErrorDeConfiguracion,
    ErrorHistoriaViva,
)

__all__ = [
    "OBJETO",
    "Cambio",
    "ConfiguracionDeModelos",
    "ConfiguracionDesactualizada",
    "FilaVigente",
    "ResultadoDeCambio",
]

OBJETO = "configuracion_capacidad"
"""El `objeto_tipo` de `modelo.cambio`; el `objeto_id` es la capacidad."""


@dataclass(frozen=True)
class FilaVigente:
    config: ConfiguracionDeCapacidad
    version: int
    """La `version_base` de la próxima edición."""
    actualizada_por: UUID | None
    actualizada_el: datetime


@dataclass(frozen=True)
class Cambio:
    capacidad: Capacidad
    config: ConfiguracionDeCapacidad
    version_base: int


@dataclass(frozen=True)
class ResultadoDeCambio:
    anterior: dict[Capacidad, ConfiguracionDeCapacidad]
    """Las filas antes del cambio: contra ellas se avisa si hay que reindexar."""
    filas: dict[Capacidad, FilaVigente]
    """Como quedaron."""


class ConfiguracionDesactualizada(ErrorHistoriaViva):
    """Otra persona guardó la fila después de la versión que se editó (`version_base`)."""

    def __init__(self, capacidad: Capacidad, version_base: int, actual: FilaVigente) -> None:
        self.capacidad = capacidad
        self.version_base = version_base
        self.actual = actual
        super().__init__(
            f"La fila de {capacidad} está en la versión {actual.version} y se editó la"
            f" {version_base}."
        )


_COLUMNAS = (
    "SELECT capacidad, motor, modelo, instruccion_id, parametros, version, actualizada_por,"
    " actualizada_el FROM acervo.configuracion_capacidad"
)


class ConfiguracionDeModelos:
    def __init__(
        self,
        *,
        bd: Bd,
        validar: Callable[[Mapping[Capacidad, ConfiguracionDeCapacidad]], None],
        auditoria: RegistroDeAuditoria | None = None,
    ) -> None:
        """`auditoria` solo la necesita quien edita (`actualizar`, `historial`): `web`."""
        self._bd = bd
        self._validar = validar
        self._auditoria = auditoria

    def vigente(self, tx: Tx | None = None) -> dict[Capacidad, ConfiguracionDeCapacidad]:
        return {capacidad: fila.config for capacidad, fila in self.filas(tx).items()}

    def filas(self, tx: Tx | None = None, *, bloquear: bool = False) -> dict[Capacidad, FilaVigente]:
        """Las filas con su versión. `bloquear` las toma todas, siempre en el mismo orden: dos
        ediciones a la vez no se interbloquean y la segunda valida sobre lo que dejó la primera."""
        if tx is None:
            with self._bd.transaccion() as propia:
                return self.filas(propia, bloquear=bloquear)
        sentencia = _COLUMNAS + (" ORDER BY capacidad FOR UPDATE" if bloquear else "")
        return {
            Capacidad(fila["capacidad"]): FilaVigente(
                config=ConfiguracionDeCapacidad(
                    motor=fila["motor"],
                    modelo=fila["modelo"],
                    instruccion_id=fila["instruccion_id"],
                    parametros=fila["parametros"],
                ),
                version=fila["version"],
                actualizada_por=fila["actualizada_por"],
                actualizada_el=fila["actualizada_el"],
            )
            for fila in tx.execute(sentencia).fetchall()
        }

    def instantanea(
        self, tx: Tx | None = None, capacidades: Collection[Capacidad] | None = None
    ) -> ConfiguracionEfectiva:
        """Lo que congela un trabajo al encolar: un cambio de fila rige para los nuevos. Con
        `capacidades`, solo esas filas (la indexación, `embeber`); sin ellas, todas.

        Raises:
            ConfiguracionInvalida: Si una fila editada por SQL no pasa `validar` (DEC-87): un
                trabajo no nace con una fila que la capa no podría correr.
        """
        vigente = self.vigente(tx)
        # Se valida el conjunto entero (DEC-87): `validar` exige todas las filas.
        self._validar(vigente)
        if capacidades is None:
            return ConfiguracionEfectiva(vigente)
        faltantes = [c for c in capacidades if c not in vigente]
        if faltantes:
            raise ConfiguracionInvalida(
                [ErrorDeConfiguracion(c.value, "", "falta la fila") for c in faltantes]
            )
        return ConfiguracionEfectiva({c: vigente[c] for c in capacidades})

    def con_cambios(
        self, anterior: Mapping[Capacidad, ConfiguracionDeCapacidad], cambios: Sequence[Cambio]
    ) -> dict[Capacidad, ConfiguracionDeCapacidad]:
        """El conjunto que quedaría, validado (la verificación de la pantalla, sin guardar).

        Raises:
            ConfiguracionInvalida: Con todos los errores del conjunto.
        """
        sin_fila = [c for c in cambios if c.capacidad not in anterior]
        if sin_fila:
            raise ConfiguracionInvalida(
                [ErrorDeConfiguracion(c.capacidad.value, "", "falta la fila") for c in sin_fila]
            )
        nuevo = {**anterior, **{c.capacidad: c.config for c in cambios}}
        self._validar(nuevo)
        return nuevo

    def actualizar(
        self, tx: Tx, cambios: Sequence[Cambio], por: Actor
    ) -> ResultadoDeCambio:
        """Guarda los cambios juntos o ninguno (DEC-53) y deja un `modelo.cambio` por capacidad
        que cambió. Un cambio igual a la fila no sube la versión ni audita. Rige para los
        trabajos nuevos.

        Raises:
            ConfiguracionDesactualizada: Si una `version_base` no es la de la fila.
            ConfiguracionInvalida: Si el conjunto resultante no valida.
        """
        assert self._auditoria is not None, "quien edita arma el módulo con la auditoría"
        filas = self.filas(tx, bloquear=True)
        for cambio in cambios:
            fila = filas.get(cambio.capacidad)
            if fila is not None and fila.version != cambio.version_base:
                raise ConfiguracionDesactualizada(cambio.capacidad, cambio.version_base, fila)
        anterior = {capacidad: fila.config for capacidad, fila in filas.items()}
        self.con_cambios(anterior, cambios)
        for cambio in cambios:
            antes, despues = _diferencias(anterior[cambio.capacidad], cambio.config)
            if not antes and not despues:
                continue
            tx.execute(
                "UPDATE acervo.configuracion_capacidad SET motor = %s, modelo = %s,"
                " instruccion_id = %s, parametros = %s, version = version + 1,"
                " actualizada_por = %s, actualizada_el = now() WHERE capacidad = %s",
                (
                    cambio.config.motor,
                    cambio.config.modelo,
                    cambio.config.instruccion_id,
                    Jsonb(dict(cambio.config.parametros)),
                    por.usuario_id,
                    cambio.capacidad.value,
                ),
            )
            self._auditoria.registrar(
                tx,
                actor=por,
                accion=Accion.MODELO_CAMBIO,
                objeto=ObjetoRef(OBJETO, cambio.capacidad.value),
                antes=antes,
                despues=despues,
            )
        return ResultadoDeCambio(anterior, self.filas(tx))

    def historial(
        self, capacidad: Capacidad, *, limite: int = 50, antes_de: int | None = None
    ) -> list[Evento]:
        """Los `modelo.cambio` de la capacidad, del más nuevo al más viejo."""
        assert self._auditoria is not None, "quien edita arma el módulo con la auditoría"
        return self._auditoria.listar(
            objeto=ObjetoRef(OBJETO, capacidad.value),
            accion=Accion.MODELO_CAMBIO,
            limite=limite,
            antes_de=antes_de,
        )


def _diferencias(
    antes: ConfiguracionDeCapacidad, despues: ConfiguracionDeCapacidad
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Solo lo que cambió, campo por campo; de `parametros`, clave por clave (ausente: nulo)."""
    viejo: dict[str, Any] = {}
    nuevo: dict[str, Any] = {}
    for campo in ("motor", "modelo", "instruccion_id"):
        if getattr(antes, campo) != getattr(despues, campo):
            viejo[campo], nuevo[campo] = getattr(antes, campo), getattr(despues, campo)
    claves = sorted(antes.parametros.keys() | despues.parametros.keys())
    distintas = [c for c in claves if antes.parametros.get(c) != despues.parametros.get(c)]
    if distintas:
        viejo["parametros"] = {c: antes.parametros.get(c) for c in distintas}
        nuevo["parametros"] = {c: despues.parametros.get(c) for c in distintas}
    return viejo, nuevo
