"""El reloj de la pantalla partida y lo que queda registrado de cada validación.

Una medición nace cuando alguien abre una Ficha y muere cuando la guarda. Entre las dos
puntas queda lo que la Fase 1 tiene que responder con datos: cuánto tardó, con qué estado
cerró, qué campos del frontmatter se corrigieron respecto de lo que propuso el modelo, si
el cuerpo se editó y cuánto, y qué obligatorios quedaron vacíos.

**El reloj lo lleva el servidor.** `abrir` y `cerrar` toman el momento del reloj del servidor
(inyectable, el mismo con que vence el aviso «abierta por»), no del navegador: una medición que dependa del reloj de la máquina de quien valida no es un dato,
es una impresión con decimales.

**Esto no puede tumbar la sesión.** Cerrar una medición que no existe, o una que ya estaba
cerrada, no es un error: no se registra nada y el guardado de la Ficha sigue su camino. Un
prototipo que pierde la validación de una Pieza porque su instrumento falló quema la sesión
del 5-oct, que es lo único que no se puede repetir.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from historia_viva.bd import Bd
from historia_viva.dominio.cuerpo import CuerpoDeFicha
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.tipos import (
    CAMPOS_OBLIGATORIOS,
    CamposFicha,
    DetallePieza,
    EdicionFicha,
    FichaVigente,
    MetodoAutenticacion,
    ModoValidacion,
    ResultadoDeEdicion,
)

__all__ = ["COLUMNAS", "Apertura", "Cierre", "Medicion", "RegistroMediciones", "medir_cierre"]

VENCE_EL_AVISO = timedelta(minutes=30)
"""Después de esto una apertura sin guardar ya no avisa «abierta por» (DEC-145, DEC-146 p. 14)."""

_EDITABLES = {campo.name for campo in fields(EdicionFicha)}

CAMPOS_COMPARADOS: tuple[str, ...] = (
    *(campo for campo in CAMPOS_OBLIGATORIOS if campo in _EDITABLES),
    "autor",
    "carreras",
)
"""Los campos del frontmatter cuya diferencia con la propuesta significa algo.

Son los que una persona edita en el formulario. Quedan afuera los que escribe el Módulo de
Fichas —`estado`, `validada_por`, `validada_el`, `modo_validacion`— porque cambian en todo
guardado: contarlos como corregidos ahogaría en ruido la única pregunta que la medición
hace, que es si el modelo ayuda o estorba (user story 23). Van en el orden de
`CAMPOS_OBLIGATORIOS` para que las dos listas de una misma fila —lo modificado y lo
faltante— se lean con el mismo orden de campos; al final, los dos opcionales que también se
editan, Autor y Carreras (DEC-126).
"""

COLUMNAS = (
    "medicion_id",
    "pieza_id",
    "persona_id",
    "nombre_archivo",
    "abierta_el",
    "guardada_el",
    "duracion_s",
    "estado_final",
    "campos_modificados",
    "campos_faltantes",
    "cuerpo_editado",
    "caracteres_cuerpo_propuesta",
    "caracteres_cuerpo_guardado",
    "clase_original",
    "metodo_autenticacion",
    "modo_validacion",
)
"""Las 16 columnas de una Medición, en el orden del CSV: las 13 del Prototipo primero y en
su orden, después las de DEC-92."""

_SELECT_MEDICION = f"SELECT {', '.join(COLUMNAS)} FROM acervo.medicion_validacion"


@dataclass(frozen=True, kw_only=True)
class Apertura:
    """El reloj recién arrancado.

    La pantalla se guarda el `medicion_id` y lo devuelve al guardar; `abierta_el` se
    muestra para que quien observa la sesión sepa desde cuándo corre.
    """

    medicion_id: UUID
    abierta_el: datetime


@dataclass(frozen=True, kw_only=True)
class Cierre:
    """Lo que se sabe de una validación una vez guardada.

    Lo arma `medir_cierre` comparando lo que firmó la persona contra la versión 1 de la
    Ficha. Existe como tipo propio, y no como una lista de argumentos sueltos, para que la
    comparación se pueda mirar sin una base de datos delante.
    """

    estado_final: Estado
    campos_modificados: list[str]
    campos_faltantes: list[str]
    cuerpo_editado: bool
    caracteres_cuerpo_guardado: int
    metodo_autenticacion: MetodoAutenticacion
    """El de la sesión que guardó: `dev` es una firma de ensayo (DEC-92, DEC-139)."""
    modo_validacion: ModoValidacion | None
    """El de la Ficha guardada: nulo si quedó Incompleta."""


@dataclass(frozen=True, kw_only=True)
class Medicion:
    """Una fila de la tabla de la sesión: una Ficha abierta, y lo que pasó con ella.

    `guardada_el`, `duracion_s`, `estado_final` y `caracteres_cuerpo_guardado` son nulos
    mientras la medición siga abierta. Abrir una Ficha y no guardarla también es un dato de
    la sesión: es una Pieza que alguien miró y abandonó.
    """

    medicion_id: UUID
    pieza_id: UUID
    persona_id: UUID
    nombre_archivo: str
    abierta_el: datetime
    guardada_el: datetime | None
    duracion_s: int | None
    estado_final: Estado | None
    campos_modificados: list[str]
    campos_faltantes: list[str]
    cuerpo_editado: bool
    caracteres_cuerpo_propuesta: int
    caracteres_cuerpo_guardado: int | None
    clase_original: str
    metodo_autenticacion: str | None
    modo_validacion: str | None


def _campos_corregidos(
    propuesta: CamposFicha, guardada: CamposFicha, heredados: Collection[str]
) -> list[str]:
    """Qué campos editables difieren de lo que propuso el modelo (user story 23). Lo heredado
    del Lote no lo propuso el modelo: editarlo no es corregirlo (M1)."""
    return [
        campo
        for campo in CAMPOS_COMPARADOS
        if campo not in heredados and getattr(propuesta, campo) != getattr(guardada, campo)
    ]


def medir_cierre(
    *,
    propuesta: FichaVigente,
    guardada: FichaVigente,
    resultado: ResultadoDeEdicion,
    heredados: Collection[str],
    metodo: MetodoAutenticacion,
) -> Cierre:
    """Compara lo que firmó la persona contra la versión 1 y arma el cierre; `heredados` son
    los `campos_heredados` de la Pieza.

    Se compara contra la Ficha que quedó guardada y no contra lo que la pantalla mandó: lo
    guardado ya pasó por la derivación de la Fecha, así que `fecha_fin` aparece corregida
    solo si el rango que quedó escrito difiere del propuesto, y no por cómo lo tipeó la
    persona.
    """
    return Cierre(
        estado_final=resultado.estado_nuevo,
        campos_modificados=_campos_corregidos(propuesta.campos, guardada.campos, heredados),
        campos_faltantes=list(resultado.campos_faltantes),
        # El cuerpo se compara entero: agregar el Contexto, corregir una palabra o reordenar
        # un párrafo son todos "la persona tuvo que tocar lo que el modelo escribió". La
        # propuesta pasa por la misma normalización que lo guardado: los blancos de los
        # bordes que recorta `CuerpoDeFicha` no son una edición.
        cuerpo_editado=guardada.cuerpo != CuerpoDeFicha.partir(propuesta.cuerpo).armar(),
        caracteres_cuerpo_guardado=len(guardada.cuerpo),
        metodo_autenticacion=metodo,
        modo_validacion=guardada.campos.modo_validacion,
    )


class RegistroMediciones:
    """Las mediciones de la sesión, en `acervo.medicion_validacion`.

    Es el único lugar del código que conoce esa tabla, del mismo modo que el Módulo de
    Fichas es el único que conoce las de Piezas y Fichas. La diferencia es que acá la forma
    de la tabla no está congelada: lo que está congelado es `GET /api/mediciones`
    (`docs/prototipo/contratos.md` §11).
    """

    def __init__(self, *, bd: Bd, reloj: Callable[[], datetime] | None = None) -> None:
        self._bd = bd
        self._reloj = reloj or (lambda: datetime.now(UTC))

    def abrir(
        self, *, pieza: DetallePieza, propuesta: FichaVigente, persona_id: UUID
    ) -> Apertura:
        """Arranca el reloj de una validación.

        Abrir dos veces la misma Ficha crea dos mediciones y no pisa la anterior: reabrir
        una `Incompleta` para completarla es trabajo real y se mide aparte (user story 20).

        Args:
            pieza: La Pieza que se está por validar. De acá sale el nombre del archivo, que
                se copia a la medición: la instrumentación no consulta las tablas del
                Módulo de Fichas (RNF-09) y la fila tiene que ser legible sola en el CSV.
            propuesta: La versión 1 de la Ficha. Es contra ella que se mide, y no cambia
                nunca, así que cuántos caracteres traía se anota ya.
            persona_id: Quién está validando.
        """
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                "INSERT INTO acervo.medicion_validacion (pieza_id, persona_id,"
                " nombre_archivo, clase_original, caracteres_cuerpo_propuesta, abierta_el)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING medicion_id, abierta_el",
                (
                    pieza.fila.pieza_id,
                    persona_id,
                    pieza.fila.nombre_archivo,
                    pieza.clase_original,
                    len(propuesta.cuerpo),
                    self._reloj(),
                ),
            ).fetchone()
        assert fila is not None
        return Apertura(medicion_id=fila["medicion_id"], abierta_el=fila["abierta_el"])

    def cerrar(self, *, medicion_id: UUID | None, pieza_id: UUID, cierre: Cierre) -> None:
        """Cierra el reloj de una validación, si hay uno abierto para esa Pieza.

        No falla nunca, y esa es una decisión y no un descuido. Un `medicion_id` nulo
        —la Ficha se guardó sin haber pasado por la pantalla—, uno de otra Pieza o uno ya
        cerrado dejan la fila como está y el guardado sigue. La medición existe para
        observar la sesión, no para poder arruinarla.
        """
        if medicion_id is None:
            return
        with self._bd.transaccion() as conexion:
            conexion.execute(
                "UPDATE acervo.medicion_validacion SET guardada_el = %s,"
                " estado_final = %s, campos_modificados = %s, campos_faltantes = %s,"
                " cuerpo_editado = %s, caracteres_cuerpo_guardado = %s,"
                " metodo_autenticacion = %s, modo_validacion = %s"
                " WHERE medicion_id = %s AND pieza_id = %s AND guardada_el IS NULL",
                (
                    self._reloj(),
                    cierre.estado_final.value,
                    cierre.campos_modificados,
                    cierre.campos_faltantes,
                    cierre.cuerpo_editado,
                    cierre.caracteres_cuerpo_guardado,
                    cierre.metodo_autenticacion.value,
                    cierre.modo_validacion,
                    medicion_id,
                    pieza_id,
                ),
            )

    def abierta_por(self, pieza_id: UUID, *, excepto: UUID) -> Medicion | None:
        """La última apertura sin guardar de otra persona en los últimos 30 minutos: de ahí sale
        el aviso «abierta por X» (DEC-106). No reserva nada; una pestaña olvidada deja de avisar
        sola."""
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                f"{_SELECT_MEDICION} WHERE pieza_id = %s AND persona_id <> %s"
                " AND guardada_el IS NULL AND abierta_el > %s"
                " ORDER BY abierta_el DESC, medicion_id DESC LIMIT 1",
                (pieza_id, excepto, self._reloj() - VENCE_EL_AVISO),
            ).fetchone()
        return None if fila is None else _medicion(fila)

    def listar(self) -> tuple[Medicion, ...]:
        """Todas las mediciones de la sesión, de la más reciente a la más antigua."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                f"{_SELECT_MEDICION} ORDER BY abierta_el DESC, medicion_id DESC"
            ).fetchall()
        return tuple(_medicion(fila) for fila in filas)


def _medicion(fila: dict[str, Any]) -> Medicion:
    estado_final = fila["estado_final"]
    return Medicion(
        medicion_id=fila["medicion_id"],
        pieza_id=fila["pieza_id"],
        persona_id=fila["persona_id"],
        nombre_archivo=fila["nombre_archivo"],
        abierta_el=fila["abierta_el"],
        guardada_el=fila["guardada_el"],
        duracion_s=fila["duracion_s"],
        estado_final=None if estado_final is None else Estado(estado_final),
        campos_modificados=list(fila["campos_modificados"]),
        campos_faltantes=list(fila["campos_faltantes"]),
        cuerpo_editado=fila["cuerpo_editado"],
        caracteres_cuerpo_propuesta=fila["caracteres_cuerpo_propuesta"],
        caracteres_cuerpo_guardado=fila["caracteres_cuerpo_guardado"],
        clase_original=fila["clase_original"],
        metodo_autenticacion=fila["metodo_autenticacion"],
        modo_validacion=fila["modo_validacion"],
    )
