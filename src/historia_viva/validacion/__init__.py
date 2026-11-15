"""Validación: firmar y reflejarlo en el índice en la misma transacción (DEC-21, DEC-91).

`fichas` es dueña de la Pieza y de la Ficha pero no conoce el índice; `indexacion` no decide
qué significa firmar; `api` solo traduce. Este módulo orquesta: abre la transacción, guarda la
Ficha y refleja el cambio de indexabilidad. Si reflejarlo falla, la firma se revierte: nunca
queda una Validada sin su indexación encolada (ADR-0002).

La aprobación en bloque (CU-07) también firma acá, en dos pasos: la previa evalúa y abre la fila
de `aprobacion_en_bloque`; la confirmación bloquea las Piezas, reevalúa con la misma regla de
`dominio` y las firma en una transacción. «Entra; se activa con C29» (DEC-135): con
`HV_BLOQUE_HABILITADO` en falso, el valor por defecto, los dos pasos levantan
`BloqueDeshabilitado` sin tocar nada (R-28).

Los controles de datos personales (125, historia 243, RF-42) también entran apagados hasta C31
(`HV_DATOS_PERSONALES_HABILITADOS`): encendidos, el RGC cambia `publicable` al guardar la Ficha,
con una versión nueva que reindexa como cualquier edición, y la pantalla recibe el aviso de DNI,
CUIL o CBU de `dominio`, que no se guarda ni bloquea. Apagados, ni el campo ni el aviso existen.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from historia_viva.auditoria import Accion, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd, Tx
from historia_viva.catalogos import validar_tipo_actividad, validar_tipo_de_pieza
from historia_viva.dominio.afirmaciones import Afirmacion, EstadoAfirmacion
from historia_viva.dominio.bloque import Candidata, Rechazo, evaluar_aprobacion
from historia_viva.dominio.datos_personales import Hallazgo, detectar_datos_personales
from historia_viva.dominio.errores import ConsultaInvalida, ErrorHistoriaViva, PermisoInsuficiente
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.permisos import Permiso, puede_marcar_publicable
from historia_viva.dominio.tipos import CamposDuros, DetallePieza, EdicionFicha, ResultadoDeEdicion
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal

__all__ = [
    "AprobacionNoAbierta",
    "BloqueDeshabilitado",
    "CambioDeIndexabilidad",
    "ControlesDeDatosPersonalesApagados",
    "Indexador",
    "PiezaVista",
    "Previa",
    "ResultadoDeBloque",
    "Validacion",
]


@dataclass(frozen=True)
class CambioDeIndexabilidad:
    pieza_id: UUID
    ficha_id: UUID
    era_indexable: bool
    es_indexable: bool


class Indexador(Protocol):
    """Lo que `validacion` le pide a `indexacion`, en la transacción de la firma."""

    def desindexar(self, tx: Tx, pieza_id: UUID) -> object:
        """Quita los fragmentos de la Pieza."""
        ...

    def encolar(self, tx: Tx, pieza_id: UUID) -> object:
        """Encola la indexación de la Ficha vigente, o reusa el trabajo activo."""
        ...


class ExisteLote(Protocol):
    def obtener(self, tx: Tx, lote_id: UUID) -> object:
        """Raises: LoteNoEncontrado."""
        ...


class BloqueDeshabilitado(ErrorHistoriaViva):
    """`HV_BLOQUE_HABILITADO` en falso: la SEU todavía no aceptó firmar sin abrir cada Pieza
    (C29, R-28)."""


class ControlesDeDatosPersonalesApagados(ErrorHistoriaViva):
    """`HV_DATOS_PERSONALES_HABILITADOS` en falso: `publicable` no se ofrece hasta C31 (125)."""


class AprobacionNoAbierta(ErrorHistoriaViva):
    """La aprobación no existe, es de otro Lote o ya se confirmó."""


@dataclass(frozen=True)
class PiezaVista:
    """Una Pieza con la versión que la persona vio en la previa."""

    pieza_id: UUID
    version: int


@dataclass(frozen=True)
class Previa:
    aprobacion_id: UUID
    abierta_el: datetime
    elegibles: tuple[Candidata, ...]
    rechazadas: tuple[Rechazo, ...]


@dataclass(frozen=True)
class ResultadoDeBloque:
    aprobadas: tuple[UUID, ...]
    rechazadas: tuple[Rechazo, ...]
    duracion_s: int | None


class Validacion:
    def __init__(
        self,
        *,
        bd: Bd,
        fichas: ModuloFichasPostgres,
        indexador: Indexador,
        lotes: ExisteLote,
        auditoria: RegistroDeAuditoria,
        marcar_fuente_retirada: Callable[[Tx, UUID], object],
        rehacer_resumen: Callable[[Tx, UUID], object] = lambda tx, pieza_id: None,
        avisar: Callable[[], None] = lambda: None,
        bloque_habilitado: bool = False,
        datos_personales_habilitados: bool = False,
        reloj: Callable[[], datetime] | None = None,
    ) -> None:
        self._bd = bd
        self._fichas = fichas
        self._indexador = indexador
        self._lotes = lotes
        self._auditoria = auditoria
        self._marcar_fuente_retirada = marcar_fuente_retirada
        # El rehecho del resumen lo encola la cola con la instantánea de `resumir` (146); `avisar`
        # despierta al trabajador, siempre después del `COMMIT`.
        self._rehacer_resumen = rehacer_resumen
        self._avisar = avisar
        self._bloque_habilitado = bloque_habilitado
        self._datos_personales_habilitados = datos_personales_habilitados
        self._reloj = reloj or (lambda: datetime.now(UTC))

    @property
    def bloque_habilitado(self) -> bool:
        """`HV_BLOQUE_HABILITADO` (R-28): en falso, la pantalla no muestra el bloque."""
        return self._bloque_habilitado

    def puede_marcar_publicable(self, principal: Principal) -> bool:
        """Con los controles de C31 encendidos, el RGC (que además firma) cambia `publicable`."""
        return (
            self._datos_personales_habilitados
            and principal.rol.puede(Permiso.VALIDAR)
            and puede_marcar_publicable(principal.rol)
        )

    def avisos_de_datos_personales(
        self, **textos: str | None
    ) -> list[tuple[str, Hallazgo]] | None:
        """Los DNI, CUIL y CBU de cada texto, con el nombre del campo; `None` con los controles
        apagados, para que la pantalla no muestre nada (no es «ninguno»)."""
        if not self._datos_personales_habilitados:
            return None
        return [
            (campo, hallazgo)
            for campo, texto in textos.items()
            for hallazgo in detectar_datos_personales(texto or "")
        ]

    def guardar_ficha(
        self,
        *,
        pieza_id: UUID,
        version_base: int,
        edicion: EdicionFicha,
        principal: Principal,
    ) -> ResultadoDeEdicion:
        """Guarda lo editado: `Validada` a nombre de `principal` con los nueve obligatorios,
        `Incompleta` sin ellos (RF-14). Una Validada que deja de serlo marca «fuente retirada»
        en lo que la cita con `marcar_fuente_retirada`, que `composicion` toma de `efemerides`
        (R-12, 063).

        Raises:
            PermisoInsuficiente: Si el rol de `principal` no tiene VALIDAR (DEC-124): lo
                verifica este módulo, no solo la ruta. También si trae `publicable` y no es
                del RGC (125).
            ControlesDeDatosPersonalesApagados: Si trae `publicable` antes de C31.
            Además, los de `ModuloFichas.guardar_edicion`.
        """
        if not principal.rol.puede(Permiso.VALIDAR):
            raise PermisoInsuficiente("Tu rol no firma Fichas.")
        if edicion.publicable is not None and not self.puede_marcar_publicable(principal):
            if not self._datos_personales_habilitados:
                raise ControlesDeDatosPersonalesApagados(
                    "«Publicable» se habilita cuando la SEU y la asesoría jurídica decidan (C31)."
                )
            raise PermisoInsuficiente("«Publicable» lo cambia el RGC.")
        with self._bd.transaccion() as tx:
            resultado = self._fichas.guardar_edicion(
                tx,
                pieza_id=pieza_id,
                version_base=version_base,
                edicion=edicion,
                editada_por=principal.usuario_id,
                metodo=principal.metodo,
            )
            self.reflejar(
                tx,
                CambioDeIndexabilidad(
                    pieza_id=pieza_id,
                    ficha_id=resultado.ficha.ficha_id,
                    era_indexable=resultado.estado_anterior is Estado.VALIDADA,
                    es_indexable=resultado.estado_nuevo is Estado.VALIDADA,
                ),
            )
            if (
                resultado.estado_anterior is Estado.VALIDADA
                and resultado.estado_nuevo is not Estado.VALIDADA
            ):
                # R-12: lo que la citaba queda marcado en esta misma transacción (decisión 11).
                self._marcar_fuente_retirada(tx, pieza_id)
        return resultado

    def completar_en_recibida(
        self, *, pieza_id: UUID, duros: CamposDuros, principal: Principal
    ) -> DetallePieza:
        """Guarda los campos duros y el Contexto de una Pieza Recibida (152): sin versión, sin
        firma y sin tocar el índice, que no tiene nada de ella. Audita
        `pieza.completar_en_recibida`.

        Raises:
            PermisoInsuficiente: Si el rol de `principal` no tiene VALIDAR (DEC-124).
            Además, los de `ModuloFichasPostgres.completar_en_recibida`.
        """
        if not principal.rol.puede(Permiso.VALIDAR):
            raise PermisoInsuficiente("Tu rol no completa Fichas.")
        with self._bd.transaccion() as tx:
            detalle, escritos, cambio_contexto = self._fichas.completar_en_recibida(
                tx, pieza_id=pieza_id, duros=duros
            )
            self._auditoria.registrar(
                tx,
                actor=principal.actor,
                accion=Accion.PIEZA_COMPLETAR_EN_RECIBIDA,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                despues={"campos": escritos, "contexto": cambio_contexto},
            )
        return detalle

    def decidir_afirmacion(
        self, *, pieza_id: UUID, orden: int, estado: EstadoAfirmacion, principal: Principal
    ) -> Afirmacion:
        """Acepta o rechaza una Afirmación a nombre de `principal` y audita
        `afirmacion.decision` en la misma transacción (ADR-0007). No crea versión ni toca el
        índice: lo decidido entra al cuerpo al firmar. Si cambia qué Afirmaciones están
        rechazadas, encola rehacer el resumen sin ellas, en segundo plano (146).

        Raises:
            PermisoInsuficiente: Si el rol de `principal` no tiene VALIDAR (DEC-124).
            Además, los de `ModuloFichasPostgres.decidir_afirmacion`.
        """
        if not principal.rol.puede(Permiso.VALIDAR):
            raise PermisoInsuficiente("Tu rol no decide Afirmaciones.")
        with self._bd.transaccion() as tx:
            antes, despues = self._fichas.decidir_afirmacion(
                tx, pieza_id=pieza_id, orden=orden, estado=estado, por=principal.usuario_id
            )
            self._auditoria.registrar(
                tx,
                actor=principal.actor,
                accion=Accion.AFIRMACION_DECISION,
                objeto=ObjetoRef("pieza", str(pieza_id)),
                antes={"orden": orden, "estado": antes.estado.value},
                despues={"orden": orden, "estado": despues.estado.value},
            )
            # Solo las de la foto entera entraron al resumen de la v1: las de una imagen embebida
            # (148) llegaron después, y el resumen de un documento sale de su texto.
            rehacer = despues.imagen == 0 and (antes.estado is EstadoAfirmacion.RECHAZADA) != (
                despues.estado is EstadoAfirmacion.RECHAZADA
            )
            if rehacer:
                self._rehacer_resumen(tx, pieza_id)
        if rehacer:
            self._avisar()
        return despues

    def reflejar(self, tx: Tx, cambio: CambioDeIndexabilidad) -> None:
        """La regla única (DEC-72, DEC-91): lo que era indexable sale del índice en esta
        transacción y lo que es indexable se encola. Editar una Validada hace las dos cosas."""
        if cambio.era_indexable:
            self._indexador.desindexar(tx, cambio.pieza_id)
        if cambio.es_indexable:
            self._indexador.encolar(tx, cambio.pieza_id)

    def previa_en_bloque(
        self, *, lote_id: UUID, clase: ClaseDeOriginal, principal: Principal
    ) -> Previa:
        """Paso 1: qué Piezas del Lote de esa clase se firmarían y cuáles no, con su motivo.
        No firma nada; abre la aprobación con el reloj del servidor.

        Raises:
            BloqueDeshabilitado: Con `HV_BLOQUE_HABILITADO` en falso.
            PermisoInsuficiente: Si el rol de `principal` no tiene VALIDAR.
            LoteNoEncontrado: Si el Lote no existe.
        """
        self._exigir_bloque(principal)
        abierta_el = self._reloj()
        with self._bd.transaccion() as tx:
            self._lotes.obtener(tx, lote_id)
            evaluacion = evaluar_aprobacion(
                self._fichas.candidatas_en_bloque(tx, lote_id=lote_id, clase=clase)
            )
            fila = tx.execute(
                "INSERT INTO acervo.aprobacion_en_bloque (lote_id, clase_original, abierta_por,"
                " abierta_el, vistas) VALUES (%s, %s, %s, %s, %s) RETURNING aprobacion_id",
                (
                    lote_id,
                    clase.value,
                    principal.usuario_id,
                    abierta_el,
                    # Lo que la confirmación puede firmar: lo que la persona vio como elegible.
                    Jsonb({str(c.pieza_id): c.version for c in evaluacion.elegibles}),
                ),
            ).fetchone()
        assert fila is not None
        return Previa(
            aprobacion_id=fila["aprobacion_id"],
            abierta_el=abierta_el,
            elegibles=evaluacion.elegibles,
            rechazadas=evaluacion.rechazadas,
        )

    def confirmar_en_bloque(
        self,
        *,
        aprobacion_id: UUID,
        lote_id: UUID,
        principal: Principal,
        tipo: str,
        tipo_actividad: str,
        contexto_adicional: str | None,
        piezas: Sequence[PiezaVista],
    ) -> ResultadoDeBloque:
        """Paso 2: firma en **una** transacción cada Pieza que la previa mostró como elegible,
        en la versión que mostró y que la persona mandó, si sigue cumpliendo la regla, y encola
        su indexación; las demás quedan como estaban. Cualquier error revierte todo.

        Raises:
            BloqueDeshabilitado: Con `HV_BLOQUE_HABILITADO` en falso.
            PermisoInsuficiente: Si el rol de `principal` no tiene VALIDAR.
            AprobacionNoAbierta: Si no existe, es de otro Lote, la abrió otra persona o ya se
                confirmó.
            ConsultaInvalida: Si una Pieza no es del Lote o de la clase de la previa.
            ValorFueraDeCatalogo: Si `tipo` o `tipo_actividad` no están en su catálogo.
        """
        self._exigir_bloque(principal)
        with self._bd.transaccion() as tx:
            # Bloqueada: de dos confirmaciones de la misma aprobación, la segunda no la ve abierta.
            aprobacion = tx.execute(
                "SELECT clase_original, vistas FROM acervo.aprobacion_en_bloque"
                " WHERE aprobacion_id = %s AND lote_id = %s AND abierta_por = %s"
                " AND confirmada_el IS NULL FOR UPDATE",
                (aprobacion_id, lote_id, principal.usuario_id),
            ).fetchone()
            if aprobacion is None:
                raise AprobacionNoAbierta("La aprobación ya no está abierta: volvé a la previa.")
            en_previa: dict[str, int] = aprobacion["vistas"]
            # La versión la fija la previa; la que manda la pantalla tiene que coincidir.
            vistas: dict[UUID, int | None] = {
                v.pieza_id: v.version if en_previa.get(str(v.pieza_id)) == v.version else None
                for v in piezas
            }
            validar_tipo_de_pieza(tx, tipo)
            validar_tipo_actividad(tx, tipo_actividad)
            candidatas = self._fichas.candidatas_en_bloque(
                tx,
                lote_id=lote_id,
                clase=ClaseDeOriginal(aprobacion["clase_original"]),
                piezas=list(vistas),
            )
            if ajenas := set(vistas) - {c.pieza_id for c in candidatas}:
                raise ConsultaInvalida(
                    f"{len(ajenas)} de las Piezas no son de este Lote o de esta clase.",
                    campo="piezas",
                )
            evaluacion = evaluar_aprobacion(candidatas, versiones_vistas=vistas)
            for candidata in evaluacion.elegibles:
                ficha = self._fichas.firmar_en_bloque(
                    tx,
                    pieza_id=candidata.pieza_id,
                    tipo=tipo,
                    tipo_actividad=tipo_actividad,
                    contexto_adicional=contexto_adicional,
                    firmada_por=principal.usuario_id,
                    metodo=principal.metodo,
                    aprobacion_id=aprobacion_id,
                )
                self.reflejar(
                    tx,
                    CambioDeIndexabilidad(
                        candidata.pieza_id, ficha.ficha_id, era_indexable=False, es_indexable=True
                    ),
                )
            aprobadas = tuple(c.pieza_id for c in evaluacion.elegibles)
            cerrada = tx.execute(
                "UPDATE acervo.aprobacion_en_bloque SET confirmada_el = %s, n_aprobadas = %s,"
                " n_rechazadas = %s WHERE aprobacion_id = %s RETURNING duracion_s",
                (self._reloj(), len(aprobadas), len(evaluacion.rechazadas), aprobacion_id),
            ).fetchone()
            assert cerrada is not None
            self._auditoria.registrar(
                tx,
                actor=principal.actor,
                accion=Accion.BLOQUE_APROBACION,
                objeto=ObjetoRef("aprobacion_en_bloque", str(aprobacion_id)),
                despues={
                    "lote_id": str(lote_id),
                    "clase": aprobacion["clase_original"],
                    "tipo": tipo,
                    "tipo_actividad": tipo_actividad,
                    "aprobadas": [str(p) for p in aprobadas],
                    "rechazadas": len(evaluacion.rechazadas),
                    "duracion_s": cerrada["duracion_s"],
                },
            )
        return ResultadoDeBloque(
            aprobadas=aprobadas,
            rechazadas=evaluacion.rechazadas,
            duracion_s=cerrada["duracion_s"],
        )

    def _exigir_bloque(self, principal: Principal) -> None:
        if not self._bloque_habilitado:
            raise BloqueDeshabilitado(
                "Deshabilitada hasta que la SEU acepte firmar sin abrir cada Pieza (C29)."
            )
        if not principal.rol.puede(Permiso.VALIDAR):
            raise PermisoInsuficiente("Tu rol no firma Fichas.")
