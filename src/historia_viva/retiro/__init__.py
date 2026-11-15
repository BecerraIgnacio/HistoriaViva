"""Retiro: sacar una Pieza del Acervo sin borrar nada (spec, módulo 18; DEC-29, RF-29, R-07).

`fichas` no conoce el índice ni la cola, así que retirar lo orquesta este módulo, como
`validacion` orquesta la firma. Todo va en una transacción y en este orden: bloquear la Pieza,
desindexar, cancelar sus trabajos `encolado` y `en_curso` (DEC-146 punto 1), marcarla y marcar
«fuente retirada» en las efemérides y los Relatos que la citan (R-12, con la función que inyecta
`composicion`, como en `validacion`). Si algo
falla en el medio se revierte entero; y si no se revirtiera, desindexar antes de marcar deja el
error del lado seguro: una Validada sin fragmentos se reindexa, contenido retirado buscable no.

No hay `DELETE` sobre el Acervo: solo sobre el índice, que es derivado (ADR-0004 control 1,
DEC-143). Con el procesamiento frenado el retiro sigue: lo que se frena es producir.

Restaurar no quita esa marca (R-12): lo que citaba la Pieza no vuelve al Agente 1.

Restaurar (DEC-138) comprueba antes de quitar la marca que el original no sea ya otra Pieza
(`pieza-duplicada`) y que al Lote le quede lugar (`lote-lleno`); el índice único por SHA-256
respalda la carrera entre dos restauraciones del mismo original. Una Validada vuelve a encolar
su indexación; una Recibida, un procesamiento nuevo (el anterior quedó `cancelado`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from historia_viva.auditoria import Accion, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd, Tx
from historia_viva.capa_modelos.configuracion import ConfiguracionEfectiva
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.lote import ContextoDeLote
from historia_viva.dominio.permisos import Permiso
from historia_viva.dominio.tipos import ViaDeIngreso
from historia_viva.fichas.postgres import FilaDeRetirada, ModuloFichasPostgres
from historia_viva.identidad import Principal
from historia_viva.lotes import Lotes

__all__ = [
    "CancelaTrabajos",
    "Desindexa",
    "PiezaRestaurada",
    "PiezaRetirada",
    "Retirada",
    "Retiro",
]


class Desindexa(Protocol):
    def desindexar(self, tx: Tx, pieza_id: UUID) -> int: ...

    def encolar(self, tx: Tx, pieza_id: UUID) -> UUID | None: ...


class CancelaTrabajos(Protocol):
    def cancelar_de_pieza(self, tx: Tx, pieza_id: UUID) -> int: ...

    def encolar(
        self,
        tx: Tx,
        pieza_id: UUID,
        *,
        tipo_mime: str,
        configuracion_efectiva: ConfiguracionEfectiva,
        contexto_lote: ContextoDeLote | None = None,
        prioridad: int = 0,
    ) -> UUID: ...


@dataclass(frozen=True)
class PiezaRetirada:
    pieza_id: UUID
    borrada_el: datetime
    borrada_por: UUID
    motivo: str


@dataclass(frozen=True)
class PiezaRestaurada:
    pieza_id: UUID
    estado: Estado
    restaurada_el: datetime
    indexacion_encolada: bool
    procesamiento_encolado: bool


@dataclass(frozen=True)
class Retirada:
    """Una fila de Administración › Retiradas, con lo que hoy impediría restaurarla."""

    fila: FilaDeRetirada
    lote_nombre: str | None
    conflicto_de_restauracion: str | None
    """`pieza-duplicada`, `lote-lleno` o nada: la pantalla apaga «Restaurar» (DEC-138)."""


class Retiro:
    def __init__(
        self,
        *,
        bd: Bd,
        fichas: ModuloFichasPostgres,
        indexacion: Desindexa,
        cola: CancelaTrabajos,
        lotes: Lotes,
        configuracion: ConfiguracionDeModelos,
        auditoria: RegistroDeAuditoria,
        marcar_fuente_retirada: Callable[[Tx, UUID], object],
    ) -> None:
        self._bd = bd
        self._fichas = fichas
        self._indexacion = indexacion
        self._cola = cola
        self._lotes = lotes
        self._configuracion = configuracion
        self._auditoria = auditoria
        self._marcar_fuente_retirada = marcar_fuente_retirada

    def retirar_pieza(self, *, pieza_id: UUID, principal: Principal, motivo: str) -> PiezaRetirada:
        """Retira la Pieza en el estado en que esté. El permiso (RETIRAR_PIEZAS) lo exige la
        ruta; el motivo de 5 a 500 caracteres, el borde HTTP y el `CHECK` de la columna.

        Raises:
            PiezaNoEncontrada: Si no existe.
            TransicionInvalida: Si ya está retirada (R-43 d).
        """
        with self._bd.transaccion() as tx:
            pieza = self._fichas.bloquear_pieza(tx, pieza_id)
            self._indexacion.desindexar(tx, pieza_id)
            cancelados = self._cola.cancelar_de_pieza(tx, pieza_id)
            borrada_el = self._fichas.marcar_borrada(
                tx, pieza_id, por=principal.usuario_id, motivo=motivo
            )
            self._marcar_fuente_retirada(tx, pieza_id)
            despues: dict[str, object] = {"retirada": True, "trabajos_cancelados": cancelados}
            _marcar_interino(despues, principal)
            self._auditoria.registrar(
                tx,
                actor=principal.actor,
                accion=Accion.PIEZA_RETIRO,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                antes={"estado": pieza.fila.estado.value, "retirada": False},
                despues=despues,
                motivo=motivo,
            )
        return PiezaRetirada(
            pieza_id=pieza_id,
            borrada_el=borrada_el,
            borrada_por=principal.usuario_id,
            motivo=motivo,
        )

    def restaurar_pieza(
        self, *, pieza_id: UUID, principal: Principal, motivo: str
    ) -> PiezaRestaurada:
        """Devuelve la retirada al Acervo en el estado en que estaba (DEC-138).

        Raises:
            PiezaNoEncontrada: Si no existe.
            TransicionInvalida: Si no está retirada (R-43 d).
            PiezaDuplicada: Si su original ya es otra Pieza no retirada.
            LoteLleno: Si su Lote llegó al tope (las retiradas no cuentan).
        """
        with self._bd.transaccion() as tx:
            pieza = self._fichas.bloquear_pieza(tx, pieza_id, retirada=True)
            # Un Aporte no entra en `pieza_huella_unica`: dos con el mismo texto pueden convivir.
            if pieza.via_ingreso is ViaDeIngreso.ARCHIVO and pieza.huella is not None:
                self._fichas.exigir_huella_libre(tx, pieza.huella)
            lote = None if pieza.lote_id is None else self._lotes.hay_lugar(tx, pieza.lote_id)
            restaurada_el = self._fichas.desmarcar_borrada(tx, pieza_id)
            estado = pieza.fila.estado
            # Lo que su Lote completó mientras estaba retirada le llega ahora, solo lo vacío (151).
            heredados = (
                {}
                if lote is None
                else self._fichas.heredar_tarde(
                    tx, lote_id=lote.lote_id, herencia=lote.herencia(), piezas=[pieza_id]
                )
            )
            indexacion_encolada = (
                estado is Estado.VALIDADA and self._indexacion.encolar(tx, pieza_id) is not None
            )
            if estado is Estado.RECIBIDA:
                # Como al subirla: la configuración vigente y, si es de Lote, detrás de la suelta.
                self._cola.encolar(
                    tx,
                    pieza_id,
                    tipo_mime=pieza.tipo_mime,
                    configuracion_efectiva=self._configuracion.instantanea(tx),
                    contexto_lote=None if lote is None else lote.contexto,
                    prioridad=0 if lote is None else 1,
                )
            despues: dict[str, object] = {
                "retirada": False,
                "indexacion_encolada": indexacion_encolada,
                "procesamiento_encolado": estado is Estado.RECIBIDA,
            }
            if heredados:
                despues["heredados"] = heredados[pieza_id]
            _marcar_interino(despues, principal)
            self._auditoria.registrar(
                tx,
                actor=principal.actor,
                accion=Accion.PIEZA_RESTAURACION,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                antes={"estado": estado.value, "retirada": True},
                despues=despues,
                motivo=motivo,
            )
        return PiezaRestaurada(
            pieza_id=pieza_id,
            estado=estado,
            restaurada_el=restaurada_el,
            indexacion_encolada=indexacion_encolada,
            procesamiento_encolado=estado is Estado.RECIBIDA,
        )

    def retiradas(
        self, tx: Tx, *, despues_de: tuple[datetime, UUID] | None, limite: int
    ) -> list[Retirada]:
        """Las retiradas, de la más reciente a la más antigua, con su conflicto de
        restauración: el que daría restaurarla ahora, sin bloquear nada."""
        filas = self._fichas.listar_retiradas(tx, despues_de=despues_de, limite=limite)
        lote_ids = list({f.lote_id for f in filas if f.lote_id is not None})
        conteos = self._fichas.contar_por_lotes(tx, lote_ids)
        # shortcut: un `obtener` por Lote distinto de la página (hasta 200); pasar a una lectura
        # en bloque de `lotes` si la lista se vuelve lenta.
        nombres = {lote_id: self._lotes.obtener(tx, lote_id).nombre for lote_id in lote_ids}
        lleno = {
            lote_id: sum(c.por_estado.values()) >= Lotes.TOPE for lote_id, c in conteos.items()
        }
        return [
            Retirada(
                fila=fila,
                lote_nombre=None if fila.lote_id is None else nombres[fila.lote_id],
                conflicto_de_restauracion=_conflicto(fila, lleno),
            )
            for fila in filas
        ]


def _conflicto(fila: FilaDeRetirada, lleno: dict[UUID, bool]) -> str | None:
    if fila.duplicada:
        return "pieza-duplicada"
    if fila.lote_id is not None and lleno[fila.lote_id]:
        return "lote-lleno"
    return None


def _marcar_interino(despues: dict[str, object], principal: Principal) -> None:
    # Toda acción hecha por el interinato lo dice (DEC-124).
    if not principal.rol.puede(Permiso.RETIRAR_PIEZAS):
        despues["interino"] = True
