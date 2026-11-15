"""Contrato del Modulo de Fichas: la unica puerta al almacenamiento (RNF-09).

Es el unico componente que sabe **como** se guarda una Ficha. Hoy la guarda como fila en
PostgreSQL; manana puede versionarla en Git o dejarla en el Drive junto al original, y
ningun otro componente se entera. Eso es lo que mantiene barata la decision diferida D5
(`arquitectura.md` §3.1, §7.5).

**Regla de arquitectura.** Ni la pantalla, ni el procesamiento, ni la instrumentacion
consultan la tabla de Fichas: le piden a este modulo. Una sentencia SQL sobre esas tablas
fuera de este paquete es un defecto de arquitectura, no un atajo.

**La maquina de estados vive aca y en ningun otro lado.** Hace solo las transiciones de
`dominio.estados.TRANSICIONES`, que la base tambien impone. No existe transicion hacia `Validada` sin
`validada_por`: es ADR-0002 expresado como precondicion del metodo, no como advertencia en
un comentario.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from historia_viva.bd import Tx
from historia_viva.capa_modelos.configuracion import ConfiguracionEfectiva
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.tipos import (
    DetallePieza,
    EdicionFicha,
    FichaVigente,
    MetodoAutenticacion,
    OriginalGuardado,
    ResultadoDeEdicion,
    ResumenPieza,
    ViaDeIngreso,
)
from historia_viva.originales import OriginalTemporal

__all__ = ["ModuloFichas"]


class ModuloFichas(Protocol):
    """Operaciones del Modulo de Fichas, en terminos de dominio y no de almacenamiento.

    Ningun metodo menciona tablas, rutas ni conexiones: si alguno lo hiciera, D5 dejaria de
    ser un cambio de modulo y pasaria a ser una refactorizacion del sistema.
    """

    def crear_pieza(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        nombre_archivo: str,
        original: OriginalTemporal,
        archivo_original: str,
        creada_por: UUID,
        via_ingreso: ViaDeIngreso,
    ) -> DetallePieza:
        """Crea la Pieza en estado `Recibida` dentro de la transaccion de quien llama.

        Quien orquesta (`ingesta`) confirma el original y encola el trabajo en esa misma
        transaccion: Pieza, original y trabajo quedan juntos o ninguno (historia 54).

        Args:
            pieza_id: El de la Pieza; tambien el `id` del frontmatter.
            nombre_archivo: Nombre con el que la persona subio el archivo, ya normalizado.
            original: El archivo ya admitido por `originales`, con su MIME, su clase y su
                SHA-256 detectados del contenido (user story 5, ADR-0001).
            archivo_original: El identificador del original ya confirmado.
            creada_por: Persona de la sesion.
            via_ingreso: Por donde entro la Pieza (DEC-140).

        Raises:
            PiezaDuplicada: Si otra Pieza tiene el mismo original (DEC-20, DEC-106).
        """
        ...

    def buscar_por_huella(self, tx: Tx, sha256: str) -> DetallePieza | None:
        """La Pieza subida con ese mismo original, si la hay, con su Lote (DEC-20)."""
        ...

    def exigir_huella_libre(self, tx: Tx, sha256: str) -> None:
        """Raises:
        PiezaDuplicada: Con la Pieza previa y su Lote, si ese original ya es una Pieza (DEC-20).
        """
        ...

    def obtener_pieza(self, pieza_id: UUID) -> DetallePieza:
        """Devuelve el estado de una Pieza y los datos de su original (user story 3).

        Raises:
            PiezaNoEncontrada: Si no existe esa Pieza.
        """
        ...

    def guardar_ficha_propuesta(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        resumen: str,
        cuerpo: str,
        configuracion_efectiva: ConfiguracionEfectiva,
    ) -> FichaVigente:
        """Guarda la Ficha propuesta por el modelo y lleva la Pieza de `Recibida` a
        `Pendiente`.

        Va en la transaccion de quien llama: el trabajador cierra su trabajo en la misma, asi
        la propuesta y el cierre quedan juntos o ninguno (DEC-99).

        Crea la **version 1** de la Ficha con `origen_version = 'modelo'`. Esa version se
        conserva para siempre aunque despues se edite: es contra ella que la instrumentacion
        compara que campos y que partes del cuerpo se corrigieron (user story 23).

        Args:
            pieza_id: La Pieza, que debe estar en `Recibida`.
            resumen: Lo unico del frontmatter que propone el modelo. Los demas campos son
                los que ya traia la Pieza (los heredados de su Lote): la propuesta nunca
                los pisa (DEC-19).
            cuerpo: La transcripcion integra del documento, o la descripcion de la imagen
                mas el OCR de su texto visible (user stories 6 y 7).
            configuracion_efectiva: Con que modelo, instruccion y topes se genero: la
                instantanea del trabajo, que la version 1 guarda (RNF-16).

        Returns:
            La Ficha vigente recien creada, en version 1.

        Raises:
            PiezaNoEncontrada: Si no existe esa Pieza.
            TransicionInvalida: Si la Pieza no esta en `Recibida`.
        """
        ...

    def obtener_ficha(self, pieza_id: UUID) -> FichaVigente:
        """Devuelve la version vigente de la Ficha de una Pieza.

        Es lo que abre la pantalla partida, y tambien lo que se relee al volver sobre una
        Ficha `Incompleta` para seguir editandola (user story 20).

        Raises:
            PiezaNoEncontrada: Si no existe esa Pieza, o si todavia esta `Recibida` y no
                tiene Ficha propuesta.
        """
        ...

    def obtener_propuesta(self, pieza_id: UUID) -> FichaVigente:
        """Devuelve la **version 1** de la Ficha, la propuesta por el modelo.

        Existe para que la instrumentacion tenga contra que comparar lo guardado. Sin ella,
        "que campos se corrigieron" no es medible y la user story 23 no se puede responder.

        Raises:
            PiezaNoEncontrada: Si no existe esa Pieza o todavia no tiene Ficha propuesta.
        """
        ...

    def listar_por_estado(
        self,
        *,
        estados: Sequence[Estado] | None = None,
        lote_id: UUID | None = None,
        mias: UUID | None = None,
    ) -> tuple[ResumenPieza, ...]:
        """Devuelve la cola de trabajo: que hay cargado y en que estado (user story 21).

        Args:
            estados: Estados a incluir. `None` los incluye todos, que es lo que pinta la
                pantalla de lista del prototipo.
            lote_id: Solo las de ese Lote (el detalle del Lote, 084).
            mias: Solo las que subió esa persona, en cualquier estado, y las Incompletas que
                trabajó (Mis Piezas, DEC-123). Cuáles de sus Validadas siguen sin indexar no lo
                sabe `fichas`: lo cruza quien llama (R-43 h).

        Returns:
            Las Piezas, de la mas recientemente actualizada a la mas antigua.
        """
        ...

    def guardar_edicion(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        version_base: int,
        edicion: EdicionFicha,
        editada_por: UUID,
        metodo: MetodoAutenticacion,
    ) -> ResultadoDeEdicion:
        """Guarda lo editado y decide entre `Validada` e `Incompleta`.

        Crea una version nueva de la Ficha con `origen_version = 'persona'` y
        `editada_por` no nulo (invariante 11), deriva el rango de fechas a partir de
        `precision_fecha` con `derivar_rango`, y escribe la firma —`validada_por`,
        `validada_el` y `modo_validacion = 'individual'`— cuando el resultado es
        `Validada` (RNF-07, ADR-0002).

        Va en la transaccion de quien llama: `validacion` refleja el cambio de
        indexabilidad en la misma, asi la firma y su efecto en el indice quedan juntos o
        ninguno (DEC-91).

        **Guardar incompleto siempre se permite.** Si falta un campo obligatorio, la Ficha
        queda `Incompleta` con el trabajo conservado y la respuesta dice que falta; no se
        pierde nada ni hay que inventar el dato (user story 18).

        Args:
            pieza_id: La Pieza, que debe estar en `Pendiente`, `Incompleta` o `Validada`.
            version_base: La `version` de la Ficha que se edito.
            edicion: Lo que mando la pantalla.
            editada_por: Persona de la sesion. Es quien firma: sin ella no hay transicion
                a `Validada`.
            metodo: Con que sesion firmo; queda en la version (RNF-07).

        Returns:
            La version nueva, el estado anterior, el nuevo y que obligatorios faltaron.

        Raises:
            PiezaNoEncontrada: Si no existe esa Pieza.
            TransicionInvalida: Si la Pieza todavia esta `Recibida`.
            VersionDesactualizada: Si la vigente ya no es `version_base` (RF-16).
            TipoDesconocido: Si el `tipo` no está en el catálogo; uno dado de baja que la
                Pieza ya tenía se conserva (DEC-127).
            TipoDeActividadDesconocido: Lo mismo para el `tipo_actividad`.
            ValueError: Si el rango de fechas es incoherente con su precision, o si el fin
                precede al inicio. Ningun camino guarda un rango invertido.
        """
        ...

    def obtener_original(self, pieza_id: UUID) -> OriginalGuardado:
        """Devuelve lo que la base sabe del original de una Pieza: dónde está, su nombre, su
        MIME y su huella. Los bytes los sirve `AlmacenDeOriginales.abrir`, por tramos.

        Raises:
            PiezaNoEncontrada: Si no existe esa Pieza.
        """
        ...
