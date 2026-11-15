"""El Lote: el Contexto común que heredan sus Piezas y su tope de 100 (spec, módulo 16).

Es la única puerta a las tablas `lote` y `lote_carrera` (`test_arquitectura`, R-25). Mínimo de R1
(DEC-118): crear, obtener y `con_lugar`, que la carga pide antes de crear la Pieza; el propietario y
las Carreras heredables los suma 082; `ver`, con el progreso, 084; `listar`, `actualizar` (el
Contexto, solo sin Piezas) y `hay_lugar`, para la restauración, 085. 151 suma `crear_implicito` (el
Lote de una carga de varios archivos, sin Contexto) y, con Piezas, completar lo vacío en
`actualizar`.
Sin estado guardado: el del Lote se deriva de sus Piezas (DEC-18).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Tx
from historia_viva.catalogos import validar_carreras
from historia_viva.dominio.errores import (
    ContextoCongelado,
    LoteLleno,
    LoteNoEncontrado,
    PermisoInsuficiente,
)
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.lote import (
    CAMPOS_DE_LA_FECHA,
    COMPLETABLES,
    ContextoDeLote,
    EstadoDeLote,
    Herencia,
    estado_de_lote,
    estimar_espera,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.indexacion import Indexacion
from historia_viva.procesamiento.cola import Cola, EstadoDeTrabajo, TipoDeTrabajo

__all__ = ["Lote", "Lotes", "ProgresoDeLote", "ResumenDeLote", "VistaDeLote"]

_COLUMNAS = (
    "lote_id, nombre, origen, fecha_inicio, fecha_fin, precision_fecha, evento, lugar,"
    " creado_por, creado_el, propietario,"
    # Las vigentes, en el orden por bytes de `pieza_carrera` (`fichas`).
    " ARRAY(SELECT lc.carrera_id FROM acervo.lote_carrera lc"
    " WHERE lc.lote_id = lote.lote_id AND lc.quitada_el IS NULL"
    ' ORDER BY lc.carrera_id COLLATE "C") AS carreras'
)


@dataclass(frozen=True, kw_only=True)
class Lote:
    lote_id: UUID
    nombre: str
    contexto: ContextoDeLote
    creado_por: UUID
    creado_el: datetime
    propietario: str | None = None
    carreras: tuple[str, ...] = ()

    def herencia(self) -> Herencia:
        return Herencia(
            lote_id=self.lote_id,
            contexto=self.contexto,
            propietario=self.propietario,
            carreras=self.carreras,
        )


@dataclass(frozen=True, kw_only=True)
class ProgresoDeLote:
    """Todo sobre las Piezas no retiradas (decisión 12 de la auditoría)."""

    total: int
    por_estado: dict[Estado, int]
    fallidas: int
    """Recibidas cuyo último procesamiento falló: se muestran aparte, no son un estado (DEC-110)."""
    para_analisis_humano: int
    sin_indexar: int
    """Validadas sin fragmentos: «Validada · No indexada» (DEC-146 punto 6)."""
    estado: EstadoDeLote
    estimacion: timedelta | None = None
    """La espera de sus Recibidas no fallidas (DEC-25); `None` sin historial o sin ellas."""


@dataclass(frozen=True, kw_only=True)
class VistaDeLote:
    lote: Lote
    progreso: ProgresoDeLote
    puede_cargar: bool
    """Le queda lugar: el total no llegó al tope (R-27)."""
    contexto_editable: bool
    """Sin Piezas: con una, el Contexto ya se heredó y queda congelado."""
    por_completar: tuple[str, ...] = ()
    """Con Piezas, lo de `COMPLETABLES` que el Lote todavía tiene vacío y se puede completar
    (151); sin Piezas todo es editable y queda vacío."""


@dataclass(frozen=True, kw_only=True)
class ResumenDeLote:
    """Una fila de la lista de Lotes (P10): los conteos sin retiradas y el estado derivado."""

    lote: Lote
    creado_por_nombre: str
    por_estado: dict[Estado, int]
    total: int
    estado: EstadoDeLote
    puede_cargar: bool
    fallidas: int = 0
    """Recibidas cuyo último procesamiento falló (DEC-110)."""
    estimacion: timedelta | None = None


class Lotes:
    TOPE = 100
    """Piezas por Lote, fijo: no hay variable de entorno que lo cambie (R-27)."""

    def __init__(
        self,
        *,
        fichas: ModuloFichasPostgres,
        cola: Cola,
        indexacion: Indexacion,
        auditoria: RegistroDeAuditoria,
    ) -> None:
        self._fichas = fichas
        self._cola = cola
        self._indexacion = indexacion
        self._auditoria = auditoria

    def crear_implicito(self, tx: Tx, *, nombre: str, por: Actor) -> Lote:
        """El Lote que nace al arrastrar varios archivos (151): un nombre provisorio y nada más.
        Origen, Fecha y Propietario se completan después, mientras las Piezas se procesan.
        Audita `lote.creacion_implicita`."""
        assert por.usuario_id is not None
        lote = self.crear(tx, nombre=nombre, contexto=ContextoDeLote(), creado_por=por.usuario_id)
        self._auditoria.registrar(
            tx,
            actor=por,
            accion=Accion.LOTE_CREACION_IMPLICITA,
            objeto=ObjetoRef("lote", str(lote.lote_id)),
            despues={"nombre": nombre},
        )
        return lote

    def crear(
        self,
        tx: Tx,
        *,
        nombre: str,
        contexto: ContextoDeLote,
        creado_por: UUID,
        propietario: str | None = None,
        carreras: tuple[str, ...] = (),
    ) -> Lote:
        """Raises:
        CarreraDesconocida: Si una Carrera está repetida, no existe o está dada de baja.
        """
        validar_carreras(tx, carreras)
        fila = tx.execute(
            "INSERT INTO acervo.lote (nombre, origen, fecha_inicio, fecha_fin, precision_fecha,"
            " evento, lugar, creado_por, propietario) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING lote_id",
            (
                nombre,
                contexto.origen,
                contexto.fecha_inicio,
                contexto.fecha_fin,
                _precision(contexto),
                contexto.evento,
                contexto.lugar,
                creado_por,
                propietario,
            ),
        ).fetchone()
        assert fila is not None
        tx.execute(
            "INSERT INTO acervo.lote_carrera (lote_id, carrera_id) SELECT %s, unnest(%s::text[])",
            (fila["lote_id"], list(carreras)),
        )
        return self.obtener(tx, fila["lote_id"])

    def obtener(self, tx: Tx, lote_id: UUID, *, bloquear: bool = False) -> Lote:
        """Raises:
        LoteNoEncontrado: Si no existe.
        """
        fila = tx.execute(
            f"SELECT {_COLUMNAS} FROM acervo.lote WHERE lote_id = %s"
            + (" FOR UPDATE" if bloquear else ""),
            (lote_id,),
        ).fetchone()
        if fila is None:
            raise LoteNoEncontrado(lote_id)
        return _lote(fila)

    def actualizar(
        self, tx: Tx, lote_id: UUID, *, editar: Callable[[Lote], Lote], por: Actor
    ) -> Lote:
        """El Lote como lo deja `editar` (nombre, Contexto, propietario y Carreras): `editar`
        recibe el Lote bloqueado y devuelve el nuevo. Quitar una Carrera marca su `quitada_el`;
        volver a ponerla la deja vigente.

        Con Piezas el Contexto ya se heredó (historia 74): solo se completa lo de `COMPLETABLES`
        que el Lote tenía vacío, nunca se cambia lo que ya tenía, y lo completado se copia a sus
        Recibidas y Pendientes que no lo tienen (`heredar_tarde`, 151). El nombre no se hereda:
        se cambia siempre. Audita `lote.herencia_tardia`.

        Raises:
            LoteNoEncontrado: Si no existe.
            ContextoCongelado: Si ya tiene Piezas (las retiradas no cuentan) y el cambio toca
                algo que no estaba vacío, o el evento, el lugar o las Carreras.
            CarreraDesconocida: Si una Carrera está repetida, no existe o está dada de baja.
        """
        lote = self.obtener(tx, lote_id, bloquear=True)
        piezas = self._fichas.contar_del_lote(tx, lote_id)
        nuevo = editar(lote)
        antes, despues = _completables(lote), _completables(nuevo)
        cambiados = [campo for campo in COMPLETABLES if despues[campo] != antes[campo]]
        completados = [campo for campo in cambiados if antes[campo] is None]
        fijos = (lote.contexto.evento, lote.contexto.lugar, frozenset(lote.carreras))
        if piezas and (
            completados != cambiados
            or fijos != (nuevo.contexto.evento, nuevo.contexto.lugar, frozenset(nuevo.carreras))
        ):
            raise ContextoCongelado(lote_id, piezas)
        validar_carreras(tx, nuevo.carreras)
        contexto = nuevo.contexto
        tx.execute(
            "UPDATE acervo.lote SET nombre = %s, origen = %s, fecha_inicio = %s, fecha_fin = %s,"
            " precision_fecha = %s, evento = %s, lugar = %s, propietario = %s WHERE lote_id = %s",
            (
                nuevo.nombre,
                contexto.origen,
                contexto.fecha_inicio,
                contexto.fecha_fin,
                _precision(contexto),
                contexto.evento,
                contexto.lugar,
                nuevo.propietario,
                lote_id,
            ),
        )
        tx.execute(
            "UPDATE acervo.lote_carrera SET quitada_el = now() WHERE lote_id = %s"
            " AND quitada_el IS NULL AND carrera_id <> ALL(%s::text[])",
            (lote_id, list(nuevo.carreras)),
        )
        tx.execute(
            "INSERT INTO acervo.lote_carrera (lote_id, carrera_id) SELECT %s, unnest(%s::text[])"
            " ON CONFLICT (lote_id, carrera_id) DO UPDATE SET quitada_el = NULL"
            " WHERE lote_carrera.quitada_el IS NOT NULL",
            (lote_id, list(nuevo.carreras)),
        )
        if piezas and completados:
            copiados = self._fichas.heredar_tarde(
                tx, lote_id=lote_id, herencia=_solo(nuevo, completados).herencia()
            )
            self._auditoria.registrar(
                tx,
                actor=por,
                accion=Accion.LOTE_HERENCIA_TARDIA,
                objeto=ObjetoRef("lote", str(lote_id)),
                despues={
                    "campos": completados,
                    "piezas": {str(pieza): campos for pieza, campos in copiados.items()},
                },
            )
        return self.obtener(tx, lote_id)

    def listar(
        self,
        tx: Tx,
        *,
        creado_por: UUID | None = None,
        estado: EstadoDeLote | None = None,
        despues_de: tuple[datetime, UUID] | None = None,
        limite: int = 100,
    ) -> list[ResumenDeLote]:
        """Del más nuevo al más viejo; `despues_de` es el `(creado_el, lote_id)` de la última
        fila de la página anterior (keyset)."""
        condiciones: list[str] = ["TRUE"]
        parametros: list[object] = []
        if creado_por is not None:
            condiciones.append("creado_por = %s")
            parametros.append(creado_por)
        if despues_de is not None:
            condiciones.append("(creado_el, lote_id) < (%s, %s)")
            parametros.extend(despues_de)
        # shortcut: el estado se deriva en `dominio` (DEC-18) y no hay columna para filtrarlo en
        # SQL; con filtro de estado se leen todos los Lotes que siguen. Alcanza para cientos.
        limitar = "" if estado is not None else f" LIMIT {int(limite)}"
        filas = tx.execute(
            f"SELECT {_COLUMNAS}, (SELECT u.nombre FROM acervo.usuario u"
            " WHERE u.usuario_id = lote.creado_por) AS creado_por_nombre"
            f" FROM acervo.lote WHERE {' AND '.join(condiciones)}"
            f" ORDER BY creado_el DESC, lote_id DESC{limitar}",
            parametros,
        ).fetchall()
        conteos = self._fichas.contar_por_lotes(tx, [fila["lote_id"] for fila in filas])
        # shortcut: las fallidas se leen Lote por Lote, solo en los que tienen Recibidas;
        # alcanza mientras sean pocos los Lotes procesándose a la vez.
        fallidas = {
            fila["lote_id"]: self._fallidas(tx, fila["lote_id"])
            for fila in filas
            if conteos[fila["lote_id"]].por_estado[Estado.RECIBIDA]
        }
        hay_pendientes = any(
            conteos[lote_id].por_estado[Estado.RECIBIDA] > n for lote_id, n in fallidas.items()
        )
        duraciones = self._cola.duraciones_recientes(tx) if hay_pendientes else []
        resumenes = []
        for fila in filas:
            por_estado = conteos[fila["lote_id"]].por_estado
            total = sum(por_estado.values())
            fallidas_del_lote = fallidas.get(fila["lote_id"], 0)
            pendientes = por_estado[Estado.RECIBIDA] - fallidas_del_lote
            resumenes.append(
                ResumenDeLote(
                    lote=_lote(fila),
                    creado_por_nombre=fila["creado_por_nombre"],
                    por_estado=por_estado,
                    total=total,
                    estado=estado_de_lote(por_estado),
                    puede_cargar=total < self.TOPE,
                    fallidas=fallidas_del_lote,
                    estimacion=estimar_espera(duraciones, pendientes),
                )
            )
        return [r for r in resumenes if estado in (None, r.estado)][:limite]

    def ver(self, tx: Tx, lote_id: UUID) -> VistaDeLote:
        """El Lote con su progreso derivado (DEC-18): el estado lo calcula `dominio` sobre los
        conteos y las fallidas las da la cola (DEC-110).

        Raises:
            LoteNoEncontrado: Si no existe.
        """
        lote = self.obtener(tx, lote_id)
        conteos = self._fichas.contar_por_estado(tx, lote_id)
        total = sum(conteos.por_estado.values())
        fallidas = self._fallidas(tx, lote_id)
        pendientes = conteos.por_estado[Estado.RECIBIDA] - fallidas
        validadas = self._fichas.ids_del_lote(tx, lote_id, Estado.VALIDADA)
        sin_indexar = len(self._indexacion.no_indexadas(tx, validadas))
        return VistaDeLote(
            lote=lote,
            progreso=ProgresoDeLote(
                total=total,
                por_estado=conteos.por_estado,
                fallidas=fallidas,
                para_analisis_humano=conteos.para_analisis_humano,
                sin_indexar=sin_indexar,
                estado=estado_de_lote(conteos.por_estado),
                estimacion=(
                    estimar_espera(self._cola.duraciones_recientes(tx), pendientes)
                    if pendientes > 0
                    else None
                ),
            ),
            puede_cargar=total < self.TOPE,
            contexto_editable=total == 0,
            por_completar=(
                ()
                if total == 0
                else tuple(c for c, valor in _completables(lote).items() if valor is None)
            ),
        )

    def _fallidas(self, tx: Tx, lote_id: UUID) -> int:
        """Recibidas del Lote cuyo último procesamiento falló (DEC-110)."""
        recibidas = self._fichas.ids_del_lote(tx, lote_id, Estado.RECIBIDA)
        procesamientos = self._cola.estado_de_trabajos(
            tx, recibidas, TipoDeTrabajo.PROCESAMIENTO
        )
        return sum(e is EstadoDeTrabajo.FALLIDO for e in procesamientos.values())

    def con_lugar(
        self, tx: Tx, lote_id: UUID, *, cargante: UUID, puede_en_lote_ajeno: bool
    ) -> Lote:
        """El Lote en que `cargante` va a cargar una Pieza, bloqueado hasta el `COMMIT`: de dos
        cargas con 99 Piezas, la segunda espera y cuenta 100.

        Raises:
            LoteNoEncontrado: Si no existe.
            PermisoInsuficiente: Si no lo creó y su rol no carga en un Lote ajeno (R-06).
            LoteLleno: Si ya tiene `TOPE` Piezas.
        """
        lote = self.obtener(tx, lote_id, bloquear=True)
        if cargante != lote.creado_por and not puede_en_lote_ajeno:
            raise PermisoInsuficiente(
                "En este Lote cargan quien lo creó, el RGC o el Coordinador de Extensión."
            )
        self._exigir_lugar(tx, lote_id)
        return lote

    def hay_lugar(self, tx: Tx, lote_id: UUID) -> Lote:
        """Que al Lote le quepa una Pieza más, con el Lote bloqueado hasta el `COMMIT`. Lo usa la
        restauración de una retirada, que lo traduce a 409 (DEC-138), y le devuelve el Lote.

        Raises:
            LoteNoEncontrado: Si no existe.
            LoteLleno: Si ya tiene `TOPE` Piezas (las retiradas no cuentan).
        """
        lote = self.obtener(tx, lote_id, bloquear=True)
        self._exigir_lugar(tx, lote_id)
        return lote

    def _exigir_lugar(self, tx: Tx, lote_id: UUID) -> None:
        if self._fichas.contar_del_lote(tx, lote_id) >= self.TOPE:
            raise LoteLleno(lote_id, self.TOPE)


def _precision(contexto: ContextoDeLote) -> str | None:
    return None if contexto.precision_fecha is None else contexto.precision_fecha.value


def _completables(lote: Lote) -> dict[str, object]:
    contexto = lote.contexto
    return {campo: getattr(contexto, campo, None) for campo in COMPLETABLES} | {
        "propietario": lote.propietario
    }


def _solo(lote: Lote, campos: list[str]) -> Lote:
    """El Lote con solo `campos` de lo completable: lo que ya tenía no se vuelve a copiar a una
    Pieza a la que una persona se lo sacó."""
    contexto = lote.contexto
    fecha = CAMPOS_DE_LA_FECHA[0] in campos
    return replace(
        lote,
        contexto=replace(
            contexto,
            origen=contexto.origen if "origen" in campos else None,
            fecha_inicio=contexto.fecha_inicio if fecha else None,
            fecha_fin=contexto.fecha_fin if fecha else None,
            precision_fecha=contexto.precision_fecha if fecha else None,
        ),
        propietario=lote.propietario if "propietario" in campos else None,
    )


def _lote(fila: dict[str, Any]) -> Lote:
    return Lote(
        lote_id=fila["lote_id"],
        nombre=fila["nombre"],
        contexto=ContextoDeLote(
            origen=fila["origen"],
            fecha_inicio=fila["fecha_inicio"],
            fecha_fin=fila["fecha_fin"],
            precision_fecha=(
                None if fila["precision_fecha"] is None else PrecisionFecha(fila["precision_fecha"])
            ),
            evento=fila["evento"],
            lugar=fila["lugar"],
        ),
        creado_por=fila["creado_por"],
        creado_el=fila["creado_el"],
        propietario=fila["propietario"],
        carreras=tuple(fila["carreras"]),
    )
