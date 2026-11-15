"""Supresión efectiva: borrar una Pieza de verdad por un pedido de datos personales (runbook R14;
historia 243, RF-42, R-DP2).

No es el retiro. El retiro (`retiro`, DEC-29) la saca de todo y conserva todo; la supresión borra
la Pieza, todas las versiones de su Ficha, sus fragmentos y su original, y deja un registro sin
contenido ni nombre: el evento `pieza.supresion` de `auditoria` con quién, cuándo, el SHA-256 y el
motivo. La aplicación no puede hacerlo: `hv_app` no tiene DELETE (ADR-0004 control 1, DEC-143).
Lo hace este módulo con `hv_migrador`, desde el comando `suprimir`, llamando a la función
`acervo.suprimir_pieza` de la migración 0038, y queda apagado hasta C31 como el resto de los
controles de datos personales.

El original se borra después del COMMIT: si se borrara antes y la transacción fallara, quedaría una
Pieza sin original. Si el borrado del archivo falla, la supresión ya está registrada y el resultado
lo dice para que se borre a mano. Los backups conservan lo suprimido hasta que vencen (R-DP2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd
from historia_viva.dominio.datos_personales import detectar_datos_personales
from historia_viva.dominio.errores import ErrorHistoriaViva, PiezaNoEncontrada
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.originales import AlmacenDeOriginales

__all__ = [
    "MotivoDeSupresionInvalido",
    "PiezaEnProceso",
    "PiezaSuprimida",
    "Supresion",
    "SupresionDeshabilitada",
]

MOTIVO_MINIMO = 5
MOTIVO_MAXIMO = 500


class SupresionDeshabilitada(ErrorHistoriaViva):
    """`HV_DATOS_PERSONALES_HABILITADOS` en falso: la supresión espera a C31."""


class MotivoDeSupresionInvalido(ErrorHistoriaViva):
    """El motivo mide de 5 a 500 caracteres y no lleva un DNI, un CUIL ni un CBU: el registro
    es sin contenido. El nombre de quien pidió tampoco va; eso no se puede detectar."""


class PiezaEnProceso(ErrorHistoriaViva):
    """Tiene un trabajo `en_curso`: retirarla primero lo cancela."""


@dataclass(frozen=True)
class PiezaSuprimida:
    pieza_id: UUID
    huella_original: str | None
    suprimida_el: datetime | None
    """`None` en la previa: no se confirmó nada."""
    filas: dict[str, int]
    drive_file_id: str | None
    """El archivo de la réplica en Drive, si se había replicado: se borra en Drive a mano (124)."""
    original_borrado: bool
    otras_con_la_misma_huella: tuple[UUID, ...]
    """Otras Piezas con el mismo original (una retirada y la que se volvió a subir): el pedido
    seguramente también las alcanza."""
    error_del_original: str | None = None


class Supresion:
    def __init__(
        self,
        *,
        bd: Bd,
        fichas: ModuloFichasPostgres,
        originales: AlmacenDeOriginales,
        auditoria: RegistroDeAuditoria,
        habilitada: bool,
    ) -> None:
        """`bd` conecta como `hv_migrador` (HV_BD_URL_MIGRADOR): con `hv_app` falla."""
        self._bd = bd
        self._fichas = fichas
        self._originales = originales
        self._auditoria = auditoria
        self._habilitada = habilitada

    def suprimir(
        self, pieza_id: UUID, *, motivo: str, por: Actor, previa: bool = False
    ) -> PiezaSuprimida:
        """Borra la Pieza y todo lo suyo en una transacción, registra la supresión y, después del
        COMMIT, borra el original. Con `previa`, hace lo mismo y lo revierte: dice qué se
        borraría sin borrar nada.

        Raises:
            SupresionDeshabilitada: Antes de C31.
            MotivoDeSupresionInvalido: Si el motivo es corto, largo o trae un número personal.
            PiezaNoEncontrada: Si no existe (o ya se suprimió).
            PiezaEnProceso: Si tiene un trabajo `en_curso`.
        """
        if not self._habilitada:
            raise SupresionDeshabilitada(
                "La supresión se habilita cuando la SEU y la asesoría jurídica decidan (C31):"
                " HV_DATOS_PERSONALES_HABILITADOS=true."
            )
        motivo = motivo.strip()
        if not MOTIVO_MINIMO <= len(motivo) <= MOTIVO_MAXIMO:
            raise MotivoDeSupresionInvalido(
                f"El motivo mide de {MOTIVO_MINIMO} a {MOTIVO_MAXIMO} caracteres."
            )
        if detectar_datos_personales(motivo):
            raise MotivoDeSupresionInvalido(
                "El motivo no lleva DNI, CUIL ni CBU: el registro es sin contenido. Poné la"
                " referencia del pedido (número de nota o de expediente)."
            )
        try:
            # La previa corre todo en un punto de guardado que se revierte siempre.
            with self._bd.transaccion() as tx, tx.transaction(force_rollback=previa):
                fila = tx.execute(
                    "SELECT acervo.suprimir_pieza(%s) AS resultado, now() AS ahora", (pieza_id,)
                ).fetchone()
                assert fila is not None
                resultado: dict[str, Any] = fila["resultado"]
                huella: str | None = resultado["huella_original"]
                filas: dict[str, int] = resultado["filas"]
                otras = tuple(self._fichas.con_la_huella(tx, huella))
                self._auditoria.registrar(
                    tx,
                    actor=por,
                    accion=Accion.PIEZA_SUPRESION,
                    objeto=ObjetoRef("pieza", str(pieza_id)),
                    despues={
                        "huella_original": huella,
                        "via_ingreso": resultado["via_ingreso"],
                        "filas": filas,
                    },
                    motivo=motivo,
                )
        except psycopg.errors.NoDataFound as error:
            raise PiezaNoEncontrada(pieza_id) from error
        except psycopg.errors.ObjectInUse as error:
            raise PiezaEnProceso(
                "La Pieza tiene un trabajo en curso: retirala primero (cancela sus trabajos)."
            ) from error
        original_borrado, error_del_original = False, None
        archivo: str | None = resultado["archivo_original"]
        if not previa and archivo is not None:
            original_borrado, error_del_original = self._borrar_original(archivo)
        return PiezaSuprimida(
            pieza_id=pieza_id,
            huella_original=huella,
            suprimida_el=None if previa else fila["ahora"],
            filas=filas,
            drive_file_id=resultado["drive_file_id"],
            original_borrado=original_borrado,
            otras_con_la_misma_huella=otras,
            error_del_original=error_del_original,
        )

    def _borrar_original(self, archivo: str) -> tuple[bool, str | None]:
        try:
            self._originales.descartar(archivo)  # dos veces no es un error
        except OSError as error:
            return False, f"No se pudo borrar el original {archivo}: {error}"
        return True, None
