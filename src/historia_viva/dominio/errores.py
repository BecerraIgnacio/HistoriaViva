"""Errores del dominio de Historia Viva.

Todos derivan de `ErrorHistoriaViva` para que la API pueda mapearlos a codigos HTTP en un
solo manejador. Los mensajes estan en espanol y usan el vocabulario canonico: los ve una
persona de la SEU en pantalla, no solo un desarrollador en un log.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from historia_viva.dominio.estados import Estado

if TYPE_CHECKING:
    # Solo en anotaciones: así `tipos` puede importar estos errores.
    from historia_viva.dominio.tipos import FichaVigente

__all__ = [
    "AfirmacionNoEncontrada",
    "ConfiguracionInvalida",
    "ConsultaInvalida",
    "ContextoCongelado",
    "DescripcionFueraDeEsquema",
    "ErrorDeConfiguracion",
    "ErrorHistoriaViva",
    "FichaCambio",
    "InstruccionContaminada",
    "LimiteExcedido",
    "LoteLleno",
    "LoteNoEncontrado",
    "MotorInterrumpido",
    "MotorNoAlcanzable",
    "MotorNoDisponible",
    "PermisoInsuficiente",
    "PiezaDuplicada",
    "PiezaNoEncontrada",
    "TransicionInvalida",
    "VersionDesactualizada",
]


class ErrorHistoriaViva(Exception):
    """Raiz de todos los errores propios del dominio."""


class TransicionInvalida(ErrorHistoriaViva):
    """Se pidio un cambio de estado que la maquina de estados de la Pieza no admite.

    Ejemplo: llevar una Pieza `Recibida` a `Validada` sin pasar por `Pendiente`, o firmar
    sin persona identificada (ADR-0002).
    """

    def __init__(
        self,
        pieza_id: UUID,
        estado_actual: str,
        estado_pedido: str,
        *,
        detalle: str | None = None,
    ) -> None:
        """`detalle` reemplaza el mensaje cuando lo que falta no es un cambio de estado (el
        reproceso de una Recibida cuyo trabajo no falló, 033)."""
        self.pieza_id = pieza_id
        self.estado_actual = estado_actual
        self.estado_pedido = estado_pedido
        super().__init__(
            detalle
            or f"La Pieza {pieza_id} esta en estado {estado_actual} y no admite la "
            f"transicion a {estado_pedido}."
        )


class PiezaNoEncontrada(ErrorHistoriaViva):
    """No existe una Pieza con ese identificador."""

    def __init__(self, pieza_id: UUID) -> None:
        self.pieza_id = pieza_id
        super().__init__(f"No existe una Pieza con el identificador {pieza_id}.")


class PiezaDuplicada(ErrorHistoriaViva):
    """El archivo ya está en el Acervo: mismo SHA-256 que otra Pieza (DEC-20).

    `estado` y `lote_id` son los de la Pieza previa; `lote_id` es `None` si es suelta.
    """

    def __init__(self, pieza_id: UUID, estado: Estado, lote_id: UUID | None = None) -> None:
        self.pieza_id = pieza_id
        self.estado = estado
        self.lote_id = lote_id
        super().__init__(f"Ese archivo ya está en el Acervo: es la Pieza {pieza_id}.")


class LoteNoEncontrado(ErrorHistoriaViva):
    """No existe un Lote con ese identificador."""

    def __init__(self, lote_id: UUID) -> None:
        self.lote_id = lote_id
        super().__init__(f"No existe un Lote con el identificador {lote_id}.")


class LoteLleno(ErrorHistoriaViva):
    """El Lote llegó a su tope de Piezas, fijo (R-27)."""

    def __init__(self, lote_id: UUID, tope: int) -> None:
        self.lote_id = lote_id
        self.tope = tope
        super().__init__(f"Este Lote ya tiene {tope} Piezas. Creá otro Lote para el resto.")


class ContextoCongelado(ErrorHistoriaViva):
    """El Lote ya tiene Piezas: heredaron su Contexto, que no cambia a mitad de camino (h. 74).
    Lo que el Lote tenía vacío sí se completa (151)."""

    def __init__(self, lote_id: UUID, piezas: int) -> None:
        self.lote_id = lote_id
        self.piezas = piezas
        super().__init__(
            "El Lote ya tiene Piezas, que heredaron su Contexto: se puede completar lo que"
            " estaba vacío, no cambiar lo que ya tenía."
        )


class TopeDeReprocesos(ErrorHistoriaViva):
    """La Pieza ya falló `tope` veces: otro intento solo por comando, con `--forzar` (DEC-137)."""

    def __init__(self, pieza_id: UUID, reprocesos: int, tope: int) -> None:
        self.pieza_id = pieza_id
        self.reprocesos = reprocesos
        self.tope = tope
        super().__init__(
            f"Esta Pieza falló {reprocesos} veces. Avisá al Administrador técnico: solo él puede"
            " forzar un intento más, por comando, y queda auditado."
        )


class PermisoInsuficiente(ErrorHistoriaViva):
    """Quien pide la acción no tiene permiso para hacerla sobre ese objeto (R-06)."""


class VersionDesactualizada(ErrorHistoriaViva):
    """Otra persona guardó la Ficha después de la versión que se editó (`version_base`).

    Lleva la Ficha vigente para que quien perdió la carrera vea qué cambió (RF-16).
    """

    def __init__(self, version_base: int, vigente: FichaVigente) -> None:
        self.version_base = version_base
        self.vigente = vigente
        super().__init__(
            f"La Ficha está en la versión {vigente.version} y se editó la {version_base}."
        )


class ConsultaInvalida(ErrorHistoriaViva):
    """Una consulta de búsqueda que no se puede hacer; `campo` es el parámetro del pedido que
    la invalida (`q`, `desde`, `tipo`…) o `None` si falta todo criterio. La API la traduce a
    422 `pedido-invalido`."""

    def __init__(self, mensaje: str, *, campo: str | None) -> None:
        super().__init__(mensaje)
        self.campo = campo


class FichaCambio(ErrorHistoriaViva):
    """La Ficha vigente ya no es la que se indexó: se editó entre `ejecutar` y `guardar`
    (DEC-146 punto 4). El trabajador reintenta con la nueva."""

    def __init__(self, pieza_id: UUID) -> None:
        super().__init__(f"La Ficha de la Pieza {pieza_id} cambió mientras se indexaba.")


class ErrorDeConfiguracion(NamedTuple):
    """Un problema de una fila de `configuracion_capacidad`: qué capacidad, qué campo y qué."""

    capacidad: str
    campo: str
    """Vacío cuando falta la fila entera."""
    mensaje: str


class ConfiguracionInvalida(ErrorHistoriaViva):
    """El conjunto de filas de `configuracion_capacidad` no sirve para construir la capa.

    Trae todos los errores a la vez: quien editó la fila por SQL los corrige de una vuelta.
    Al arrancar, impide que `web` levante con una capacidad mal configurada (DEC-87).
    """

    def __init__(self, errores: Sequence[ErrorDeConfiguracion]) -> None:
        self.errores = tuple(errores)
        detalle = "; ".join(
            f"{'.'.join(filter(None, (e.capacidad, e.campo)))}: {e.mensaje}" for e in self.errores
        )
        super().__init__(
            f"La configuración de los modelos (tabla configuracion_capacidad) no es válida: "
            f"{detalle}."
        )


class InstruccionContaminada(ErrorHistoriaViva):
    """Se intento construir una invocacion violando la capa 1 de ADR-0003.

    El canal de instrucciones se arma exclusivamente con constantes del codigo. Si un
    implementador de la capa de modelos recibe una plantilla que no es un identificador
    conocido, o detecta contenido del Acervo dentro del canal de instrucciones, levanta
    este error en vez de invocar al motor.
    """

    def __init__(self, detalle: str) -> None:
        self.detalle = detalle
        super().__init__(
            f"Se intento armar el canal de instrucciones con contenido que no es una "
            f"plantilla constante del codigo (ADR-0003, capa 1): {detalle}"
        )


class LimiteExcedido(ErrorHistoriaViva):
    """Una invocacion supero su tope de tiempo o de tokens (ADR-0004, limite duro 3).

    Ninguna operacion de modelo puede quedar ejecutando indefinidamente: se cancela y el
    trabajador falla el trabajo como cualquier otro error, con este mensaje.
    """

    def __init__(self, capacidad: str, limite: str, tope: int, alcanzado: int) -> None:
        self.capacidad = capacidad
        self.limite = limite
        self.tope = tope
        self.alcanzado = alcanzado
        super().__init__(
            f"La capacidad '{capacidad}' supero su tope de {limite}: el maximo es {tope} "
            f"y se alcanzo {alcanzado}. La invocacion se cancelo (ADR-0004)."
        )


class MotorNoDisponible(ErrorHistoriaViva):
    """El motor local de modelos no contesto, o contesto con un error.

    Es el caso de ADR-0004 en que el motor esta apagado o le falta un modelo: la invocacion
    falla, la Pieza queda `Recibida` con este mensaje como `error_procesamiento` y
    `GET /api/salud` reporta `capa_modelos: sin_respuesta` (issue 11), o `modelos_faltantes`
    si lo que falta es el modelo (historia 165). Distinto de
    `LimiteExcedido`: aca el motor no llego a trabajar, no se paso de su tope.
    """

    def __init__(self, detalle: str) -> None:
        self.detalle = detalle
        super().__init__(f"El motor de modelos no esta disponible: {detalle}")


class MotorNoAlcanzable(MotorNoDisponible):
    """El pedido nunca llegó al motor: conexión rechazada o sin conexión a tiempo.

    El trabajo vuelve a `encolado` con la generación descontada, porque el motor no trabajó
    (DEC-86), y el trabajador espera antes de volver a tomar (DEC-146 punto 2).
    """


class DescripcionFueraDeEsquema(ErrorHistoriaViva):
    """La respuesta de `describir_imagen` no cumple su esquema JSON (144). No se reintenta: la
    Pieza pasa a Pendiente sin Afirmaciones y la persona escribe la descripción. El mensaje no
    copia la respuesta: puede traer lo que la imagen tenía escrito."""


class AfirmacionNoEncontrada(ErrorHistoriaViva):
    """La Pieza no tiene una Afirmación con ese número (144)."""

    def __init__(self, pieza_id: UUID, orden: int) -> None:
        self.pieza_id = pieza_id
        self.orden = orden
        super().__init__(f"La Pieza {pieza_id} no tiene la Afirmación {orden}.")


class ImagenNoEncontrada(ErrorHistoriaViva):
    """La Pieza no tiene una imagen embebida sin texto con ese número (148)."""

    def __init__(self, pieza_id: UUID, imagen: int) -> None:
        self.pieza_id = pieza_id
        self.imagen = imagen
        super().__init__(f"La Pieza {pieza_id} no tiene la imagen {imagen}.")


class MotorInterrumpido(MotorNoDisponible):
    """El motor recibió el pedido y falló a mitad: HTTP de error, corte o respuesta que no es
    de Ollama. La generación cuenta y el trabajo queda `fallido`, sin reintento (DEC-113)."""
