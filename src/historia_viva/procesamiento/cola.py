"""La cola de procesamiento: una tabla, sin broker (consulta C2).

`acervo.trabajo_procesamiento` es la única fuente de verdad del avance. No hay Celery ni
Redis: el consumidor es el proceso `trabajador`, aparte de `web` (historia 50), y la toma de
trabajo se hace con `SELECT ... FOR UPDATE SKIP LOCKED`, así que dos procesos no toman el
mismo trabajo.

**Cada trabajo nace con tope** (ADR-0004, límite duro 3). El presupuesto no se inventa acá:
es la suma de los topes de las capacidades que el procesamiento va a invocar, según la
instantánea de sus filas que el trabajo congela al encolar. Los `CHECK` de la tabla lo vuelven
a exigir contra la base, porque un límite duro que solo existe en un `if` de Python no es un
límite duro.

Este módulo toca **una sola tabla** y ninguna de Piezas ni de Fichas: lo de la Pieza se le
pide al Módulo de Fichas (RNF-09).
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from historia_viva.bd import Bd, Tx
from historia_viva.capa_modelos.configuracion import ConfiguracionEfectiva
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.dominio.errores import TopeDeReprocesos, TransicionInvalida
from historia_viva.dominio.lote import ContextoDeLote
from historia_viva.dominio.originales import es_imagen, es_texto

__all__ = [
    "CAPACIDADES_DE_DOCUMENTO",
    "CAPACIDADES_DE_IMAGEN",
    "CAPACIDADES_DE_TEXTO",
    "REPROCESABLES",
    "TOPE_DE_REPROCESOS",
    "Cola",
    "EstadoDeTrabajo",
    "ResultadoDeReproceso",
    "ResumenDeCola",
    "TipoDeTrabajo",
    "TrabajoNoTomado",
    "TrabajoTomado",
    "capacidades_del_procesamiento",
    "presupuesto",
]


class TipoDeTrabajo(StrEnum):
    """Los tipos de trabajo de la cola; cada trabajador toma solo los suyos (DEC-08). El
    `resumen` rehecho (146) y la `descripcion` de una imagen embebida (148) los toma el
    trabajador del procesamiento: usan el mismo motor."""

    PROCESAMIENTO = "procesamiento"
    INDEXACION = "indexacion"
    RESUMEN = "resumen"
    DESCRIPCION = "descripcion"
    """La de una imagen embebida sin texto (148), una por trabajo."""


REPROCESABLES = (TipoDeTrabajo.PROCESAMIENTO, TipoDeTrabajo.INDEXACION)
"""Lo que `reprocesar` recupera. Un `resumen` fallido no: lo vuelve a encolar el próximo cambio
de decisión, y la pantalla deja firmar con el resumen que hay (146). Una `descripcion` fallida se
vuelve a pedir con «Describir esta imagen» (148)."""


class EstadoDeTrabajo(StrEnum):
    ENCOLADO = "encolado"
    EN_CURSO = "en_curso"
    COMPLETADO = "completado"
    FALLIDO = "fallido"
    CANCELADO = "cancelado"


TIEMPO_MAXIMO_INDEXACION_MS = 1_800_000
"""El tope de la tabla. El de cada lote de `embeber` lo aplica la capa; el del trabajo es para
el arriendo, y la Ficha más larga (80 páginas, unos 200 fragmentos) entra."""


class TrabajoNoTomado(Exception):
    """Se quiso cerrar un trabajo que este trabajador ya no tiene `en_curso`.

    Venció su arriendo y lo recuperó la cola, lo tomó otro, o se canceló porque se retiró la
    Pieza. Su resultado se descarta: quien lo tiene ahora (o nadie) decide su cierre.
    """

    def __init__(self, trabajo_id: UUID) -> None:
        super().__init__(f"El trabajo {trabajo_id} no está tomado por este trabajador.")
        self.trabajo_id = trabajo_id


_MARGEN_ARRIENDO = timedelta(seconds=30)
"""Cuánto más allá de `tiempo_maximo_ms` se le da a un trabajo antes de considerarlo colgado.

El tope de tiempo de la capa de modelos ya debería cortar la invocación antes; el margen es
para lo que hay alrededor de esa invocación (leer el original, guardar la Ficha) y no para
disimular un tope que no se está respetando."""

_ERROR_GENERACIONES_AGOTADAS = "generaciones agotadas"
"""Un `encolado` que ya no se puede tomar (DEC-146 punto 2)."""

_ERROR_ARRIENDO_AGOTADO = (
    "El trabajo quedó colgado por un reinicio del proceso y agotó su tope de reintentos "
    "automáticos."
)

CAPACIDADES_DE_DOCUMENTO: tuple[Capacidad, ...] = (
    Capacidad.TRANSCRIBIR,
    Capacidad.RESUMIR,
)
"""De un PDF o un DOCX sale su transcripción íntegra, y de ella el resumen."""

CAPACIDADES_DE_IMAGEN: tuple[Capacidad, ...] = (
    Capacidad.DESCRIBIR_IMAGEN,
    Capacidad.TRANSCRIBIR,
    Capacidad.RESUMIR,
)
"""De una fotografía sale una descripción propuesta **más** el OCR de su texto visible, y de
las dos cosas juntas el resumen (`casos-de-uso.md` CU-03, paso 2)."""

CAPACIDADES_DE_TEXTO: tuple[Capacidad, ...] = (Capacidad.RESUMIR,)
"""Un texto plano ya es su cuerpo: solo se pide el resumen (decisión del usuario, 028)."""


def capacidades_del_procesamiento(tipo_mime: str) -> tuple[Capacidad, ...]:
    """Qué va a invocar el procesamiento de este original, y por lo tanto qué tope le toca.

    Una foto cuesta un paso más que un documento, y el presupuesto del trabajo tiene que
    saberlo antes de encolarlo: el tope se escribe al encolar, no al procesar.
    """
    if es_imagen(tipo_mime):
        return CAPACIDADES_DE_IMAGEN
    if es_texto(tipo_mime):
        return CAPACIDADES_DE_TEXTO
    return CAPACIDADES_DE_DOCUMENTO


def presupuesto(configuracion_efectiva: ConfiguracionEfectiva, tipo_mime: str) -> int:
    """El tope de tiempo (ms) del trabajo de procesamiento de este original.

    Es la suma de los `timeout_s` de las capacidades que va a invocar, en la instantánea con
    la que corre: el arriendo no vence antes que el tope de la capa en ninguno de sus pasos.
    Los tokens no tienen tope por trabajo: el tope es por llamada y lo aplica la capa; el
    trabajo los mide al completarse (`completar`).
    """
    return 1000 * sum(
        configuracion_efectiva.por_capacidad[capacidad].limites.tiempo_maximo_s
        for capacidad in capacidades_del_procesamiento(tipo_mime)
    )


RESUMENES_SEGUIDOS = 3
"""Cuántos trabajos de quien valida (`DE_QUIEN_VALIDA`: el resumen rehecho, 146, y la
descripción de una imagen embebida, 148) pueden pasar delante de un procesamiento que espera. Se
toman antes porque la persona está validando y la cola es una sola y de CPU (ADR-0005): detrás de
un Lote de fotos esperarían horas. Pero varias personas no pueden frenar el Lote: después de tres
seguidos, pasa un procesamiento."""

DE_QUIEN_VALIDA = (TipoDeTrabajo.RESUMEN, TipoDeTrabajo.DESCRIPCION)
"""Los trabajos de una Pieza que ya es Pendiente: la persona que la valida los espera."""
_DE_QUIEN_VALIDA = ", ".join(f"'{tipo.value}'" for tipo in DE_QUIEN_VALIDA)

TOPE_DE_REPROCESOS = 6
"""Reprocesos de una Pieza en un tipo de trabajo, sin contar el intento original; el siguiente
exige `--forzar`, para todos los roles y también para `reprocesar --fallidos` (DEC-137, ADR-0004
control 2)."""


@dataclass(frozen=True)
class ResultadoDeReproceso:
    trabajo_id: UUID
    forzado: bool
    """Se encoló por encima del tope: la auditoría lo marca con `forzar`."""


def _ultimo_procesamiento(tx: Tx, pieza_id: UUID) -> dict[str, Any] | None:
    """Estado y error del último trabajo de procesamiento de la Pieza: el último intento manda."""
    fila: dict[str, Any] | None = tx.execute(
        "SELECT estado, error FROM acervo.trabajo_procesamiento"
        " WHERE pieza_id = %s AND tipo = %s ORDER BY encolado_el DESC LIMIT 1",
        (pieza_id, TipoDeTrabajo.PROCESAMIENTO.value),
    ).fetchone()
    return fila


@dataclass(frozen=True)
class ResumenDeCola:
    """Lo que la salud muestra de la cola (spec §2.4): no alerta por antigüedad, solo la dice."""

    encolados: int
    en_curso: int
    fallidos: int
    """Piezas cuyo último trabajo de un tipo terminó `fallido`: lo que `reprocesar --fallidos`
    recuperaría. Un fallido ya reprocesado es historial y no cuenta."""
    mas_viejo_el: datetime | None
    """Cuándo se encoló el `encolado` más viejo; `None` si no hay ninguno."""


@dataclass(frozen=True)
class TrabajoTomado:
    """Un trabajo que este proceso tomó para sí."""

    trabajo_id: UUID
    pieza_id: UUID
    tipo: TipoDeTrabajo
    configuracion_efectiva: ConfiguracionEfectiva
    """La que congeló al encolar: con ella se arma la capa, no con la fila de hoy."""
    contexto_lote: ContextoDeLote | None = None
    """El Contexto de su Lote, congelado al encolar (DEC-19); `None` en una Pieza suelta."""
    imagen: int | None = None
    """La imagen que describe un trabajo de descripción (148)."""


class Cola:
    """La cola de trabajos de procesamiento sobre PostgreSQL."""

    def __init__(self, *, bd: Bd) -> None:
        self._bd = bd
        # Sin broker, quién tomó el trabajo es una columna; el latido lleva el mismo id (035).
        self.identificador = f"trabajador-{os.getpid()}-{uuid4().hex[:8]}"

    def encolar(
        self,
        tx: Tx,
        pieza_id: UUID,
        *,
        tipo_mime: str,
        configuracion_efectiva: ConfiguracionEfectiva,
        contexto_lote: ContextoDeLote | None = None,
        prioridad: int = 0,
    ) -> UUID:
        """Encola el procesamiento de una Pieza en la transacción de quien llama.

        No abre conexión: la Pieza y su trabajo se confirman juntos o no se confirman
        (historia 54).

        Args:
            tipo_mime: El MIME real del original. Decide qué capacidades va a invocar el
                trabajo y, con ellas, el tope con el que nace.
            configuracion_efectiva: La instantánea que congela el trabajo: un cambio de fila
                rige solo para los trabajos nuevos (DEC-87).
            contexto_lote: El Contexto del Lote de la Pieza, que el trabajo copia: lo que
                cambie el Lote después no lo alcanza (DEC-19).
            prioridad: 1 para la Pieza de un Lote, 0 para la suelta: se toma antes la menor
                (DEC-25).

        Raises:
            TransicionInvalida: Si esa Pieza ya tiene un trabajo encolado, en curso o
                completado (invariante 8: reprocesar no genera contenido nuevo).
        """
        try:
            fila = tx.execute(
                "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo,"
                " tiempo_maximo_ms, configuracion_efectiva, contexto_lote, prioridad)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING trabajo_id",
                (
                    pieza_id,
                    TipoDeTrabajo.PROCESAMIENTO.value,
                    presupuesto(configuracion_efectiva, tipo_mime),
                    Jsonb(configuracion_efectiva.a_json()),
                    None if contexto_lote is None else Jsonb(contexto_lote.a_json()),
                    prioridad,
                ),
            ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            raise TransicionInvalida(pieza_id, "Recibida", "Recibida") from error
        assert fila is not None
        trabajo_id: UUID = fila["trabajo_id"]
        return trabajo_id

    def reprocesar(
        self,
        tx: Tx,
        pieza_id: UUID,
        *,
        tipo: TipoDeTrabajo = TipoDeTrabajo.PROCESAMIENTO,
        tipo_mime: str,
        configuracion_efectiva: ConfiguracionEfectiva,
        solicitado_por: UUID | None,
        forzar: bool = False,
        prioridad: int | None = 0,
    ) -> ResultadoDeReproceso:
        """Encola un trabajo nuevo de `tipo` para una Pieza cuyo último trabajo de ese tipo
        terminó `fallido` (DEC-86), en la transacción de quien llama.

        El trabajo nuevo corre con `configuracion_efectiva` (una fila arreglada rige para el
        reintento, DEC-87) y copia el Contexto de Lote y la versión de Ficha del fallido: el
        Contexto de un Lote con Piezas ya no cambia. Si con esto se calculara si el
        procesamiento está frenado, `procesamiento` importaría `interruptor`: lo calcula quien
        llama (018).

        Args:
            tipo_mime: El MIME del original; decide el tope de tiempo del procesamiento.
            solicitado_por: La persona que lo pidió; `None` si lo pidió el comando.
            forzar: Supera el tope de reprocesos. Solo lo pasa el comando (R-30).
            prioridad: 0 para el reproceso individual, aunque la Pieza sea de un Lote; `None`
                repite la del trabajo fallido (`reprocesar --fallidos`, DEC-25).

        Raises:
            TransicionInvalida: Si el último trabajo de `tipo` no terminó `fallido`, o si otro
                reproceso de la misma Pieza ganó la carrera (el índice único de trabajo activo).
            TopeDeReprocesos: Si la Pieza ya tuvo `TOPE_DE_REPROCESOS` reprocesos en `tipo` y no
                se pasó `forzar` (DEC-137).
        """
        # Los fallidos se cuentan por versión de Ficha: cada versión tiene su indexación original.
        # En el procesamiento `ficha_id` es nula y cuentan todos.
        # shortcut: los fallos de `reindexar` sobre la misma versión cuentan como reprocesos;
        # separar cuando `reindexar` marque su trabajo.
        ultimo = tx.execute(
            "SELECT u.estado, u.ficha_id, u.contexto_lote, u.prioridad,"
            " (SELECT count(*) FROM acervo.trabajo_procesamiento t"
            "  WHERE t.pieza_id = u.pieza_id AND t.tipo = u.tipo AND t.estado = 'fallido'"
            "  AND t.ficha_id IS NOT DISTINCT FROM u.ficha_id) AS fallidos"
            " FROM acervo.trabajo_procesamiento u WHERE u.pieza_id = %s AND u.tipo = %s"
            " ORDER BY u.encolado_el DESC LIMIT 1",
            (pieza_id, tipo.value),
        ).fetchone()
        if ultimo is None or ultimo["estado"] != "fallido":
            raise TransicionInvalida(
                pieza_id,
                "Recibida",
                "Recibida",
                detalle=f"La Pieza {pieza_id} no tiene un trabajo de {tipo.value} fallido para"
                " reintentar.",
            )
        # Todo reproceso nace de un fallido: el primero de la versión es el intento original.
        reprocesos: int = ultimo["fallidos"] - 1
        if reprocesos >= TOPE_DE_REPROCESOS and not forzar:
            raise TopeDeReprocesos(pieza_id, reprocesos, TOPE_DE_REPROCESOS)
        try:
            fila = tx.execute(
                "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, ficha_id,"
                " tiempo_maximo_ms, configuracion_efectiva, contexto_lote, solicitado_por,"
                " prioridad) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING trabajo_id",
                (
                    pieza_id,
                    tipo.value,
                    ultimo["ficha_id"],
                    presupuesto(configuracion_efectiva, tipo_mime)
                    if tipo is TipoDeTrabajo.PROCESAMIENTO
                    else TIEMPO_MAXIMO_INDEXACION_MS,
                    Jsonb(configuracion_efectiva.a_json()),
                    None if ultimo["contexto_lote"] is None else Jsonb(ultimo["contexto_lote"]),
                    solicitado_por,
                    ultimo["prioridad"] if prioridad is None else prioridad,
                ),
            ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            raise TransicionInvalida(
                pieza_id,
                "Recibida",
                "Recibida",
                detalle=f"La Pieza {pieza_id} ya tiene un trabajo de {tipo.value} activo: otro"
                " reproceso llegó antes.",
            ) from error
        assert fila is not None
        return ResultadoDeReproceso(
            trabajo_id=fila["trabajo_id"], forzado=reprocesos >= TOPE_DE_REPROCESOS
        )

    def fallidos(
        self, tx: Tx, tipos: Collection[TipoDeTrabajo]
    ) -> list[tuple[UUID, TipoDeTrabajo]]:
        """Las Piezas cuyo último trabajo de cada uno de `tipos` terminó `fallido`: lo que
        `reprocesar --fallidos` recupera (DEC-86). Las retiradas y el Lote los filtra `fichas`
        (`Ingesta.reprocesar_fallidos`): la tabla `pieza` no se lee desde acá (RNF-09)."""
        filas = tx.execute(
            "SELECT pieza_id, tipo FROM ("
            "  SELECT DISTINCT ON (pieza_id, tipo) pieza_id, tipo, estado"
            "  FROM acervo.trabajo_procesamiento WHERE tipo = ANY(%s)"
            "  ORDER BY pieza_id, tipo, encolado_el DESC"
            ") AS ultimos WHERE estado = 'fallido'",
            ([tipo.value for tipo in tipos],),
        ).fetchall()
        return [(fila["pieza_id"], TipoDeTrabajo(fila["tipo"])) for fila in filas]

    def encolar_indexacion(
        self,
        tx: Tx,
        pieza_id: UUID,
        *,
        ficha_id: UUID,
        configuracion_efectiva: ConfiguracionEfectiva,
    ) -> UUID:
        """Encola la indexación de la Pieza en la transacción de quien llama, o devuelve el
        trabajo `encolado` o `en_curso` que ya tiene (DEC-09): reindexar es legítimo y la
        idempotencia la da el reemplazo de los fragmentos (DEC-84).

        Uno `encolado` toma la `ficha_id` y la configuración nuevas; uno `en_curso` no se toca:
        si terminó con otro modelo, lo reencola el manejador al completarse (DEC-146 punto 5).
        """
        fila = tx.execute(
            "INSERT INTO acervo.trabajo_procesamiento AS t (pieza_id, tipo, tiempo_maximo_ms,"
            " configuracion_efectiva, ficha_id) VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (pieza_id)"
            " WHERE tipo = 'indexacion' AND estado IN ('encolado','en_curso')"
            # `DO NOTHING` no devolvería el trabajo que ya estaba: se actualiza siempre.
            " DO UPDATE SET"
            " configuracion_efectiva = CASE WHEN t.estado = 'encolado'"
            "  THEN EXCLUDED.configuracion_efectiva ELSE t.configuracion_efectiva END,"
            " ficha_id = CASE WHEN t.estado = 'encolado' THEN EXCLUDED.ficha_id ELSE t.ficha_id END"
            " RETURNING trabajo_id",
            (
                pieza_id,
                TipoDeTrabajo.INDEXACION.value,
                TIEMPO_MAXIMO_INDEXACION_MS,
                Jsonb(configuracion_efectiva.a_json()),
                ficha_id,
            ),
        ).fetchone()
        assert fila is not None
        trabajo_id: UUID = fila["trabajo_id"]
        return trabajo_id

    def encolar_resumen(
        self, tx: Tx, pieza_id: UUID, *, configuracion_efectiva: ConfiguracionEfectiva
    ) -> None:
        """Encola rehacer el resumen de la Pieza en la transacción de quien llama, salvo que ya
        tenga uno encolado: ese, al correr, lee las decisiones de ese momento, así que el último
        rechazo gana sin un trabajo por rechazo (146). Uno `en_curso` leyó las de antes: el nuevo
        se encola igual. Copia el Contexto de Lote del procesamiento, el que vio el de la v1."""
        tx.execute(
            "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, tiempo_maximo_ms,"
            " configuracion_efectiva, contexto_lote)"
            " SELECT %(pieza)s, %(tipo)s, %(tope)s, %(config)s, (SELECT contexto_lote"
            "  FROM acervo.trabajo_procesamiento WHERE pieza_id = %(pieza)s"
            "  AND tipo = 'procesamiento' AND estado = 'completado')"
            " ON CONFLICT (pieza_id) WHERE tipo = 'resumen' AND estado = 'encolado' DO NOTHING",
            {
                "pieza": pieza_id,
                "tipo": TipoDeTrabajo.RESUMEN.value,
                "tope": 1000
                * configuracion_efectiva.por_capacidad[Capacidad.RESUMIR].limites.tiempo_maximo_s,
                "config": Jsonb(configuracion_efectiva.a_json()),
            },
        )

    def estado_del_resumen(self, pieza_id: UUID) -> EstadoDeTrabajo | None:
        """`encolado` o `en_curso` si se está rehaciendo el resumen de la Pieza; si no, cómo
        terminó el último; `None` si nunca se rehízo (146)."""
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                "SELECT estado FROM acervo.trabajo_procesamiento WHERE pieza_id = %s"
                " AND tipo = %s ORDER BY estado IN ('encolado','en_curso') DESC, encolado_el DESC"
                " LIMIT 1",
                (pieza_id, TipoDeTrabajo.RESUMEN.value),
            ).fetchone()
        return None if fila is None else EstadoDeTrabajo(fila["estado"])
    def encolar_descripcion(
        self,
        tx: Tx,
        pieza_id: UUID,
        *,
        imagen: int,
        configuracion_efectiva: ConfiguracionEfectiva,
        solicitado_por: UUID | None = None,
    ) -> UUID:
        """Encola la descripción de una imagen embebida de la Pieza (148), en la transacción de
        quien llama. Nace con el tope de `describir_imagen` (ADR-0004). Se toma como el resumen
        rehecho, delante de los Lotes hasta la racha (`DE_QUIEN_VALIDA`, `RESUMENES_SEGUIDOS`).

        Args:
            solicitado_por: Quien tocó «Describir esta imagen»; `None` si se encoló sola.

        Raises:
            TransicionInvalida: Si esa imagen ya tiene una descripción encolada, en curso o
                completada: una por imagen.
        """
        try:
            fila = tx.execute(
                "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, imagen,"
                " tiempo_maximo_ms, configuracion_efectiva, solicitado_por)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING trabajo_id",
                (
                    pieza_id,
                    TipoDeTrabajo.DESCRIPCION.value,
                    imagen,
                    1000
                    * configuracion_efectiva.por_capacidad[
                        Capacidad.DESCRIBIR_IMAGEN
                    ].limites.tiempo_maximo_s,
                    Jsonb(configuracion_efectiva.a_json()),
                    solicitado_por,
                ),
            ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            raise TransicionInvalida(
                pieza_id,
                "Pendiente",
                "Pendiente",
                detalle=f"La imagen {imagen} ya tiene su descripción encolada o hecha.",
            ) from error
        assert fila is not None
        trabajo_id: UUID = fila["trabajo_id"]
        return trabajo_id

    def estados_de_descripciones(self, pieza_id: UUID) -> Mapping[int, EstadoDeTrabajo]:
        """El estado del último trabajo de descripción de cada imagen de la Pieza (148)."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                "SELECT DISTINCT ON (imagen) imagen, estado FROM acervo.trabajo_procesamiento"
                " WHERE pieza_id = %s AND tipo = %s ORDER BY imagen, encolado_el DESC, trabajo_id",
                (pieza_id, TipoDeTrabajo.DESCRIPCION.value),
            ).fetchall()
        return {fila["imagen"]: EstadoDeTrabajo(fila["estado"]) for fila in filas}

    def estado_de_trabajos(
        self, tx: Tx, pieza_ids: Sequence[UUID], tipo: TipoDeTrabajo | None = None
    ) -> Mapping[UUID, EstadoDeTrabajo]:
        """El estado del último trabajo (de `tipo`, si se da) de cada Pieza; la que no tiene,
        no aparece."""
        filas = tx.execute(
            "SELECT DISTINCT ON (pieza_id) pieza_id, estado FROM acervo.trabajo_procesamiento"
            " WHERE pieza_id = ANY(%(piezas)s) AND (%(tipo)s::text IS NULL OR tipo = %(tipo)s)"
            " ORDER BY pieza_id, encolado_el DESC, trabajo_id",
            {"piezas": list(pieza_ids), "tipo": None if tipo is None else tipo.value},
        ).fetchall()
        return {fila["pieza_id"]: EstadoDeTrabajo(fila["estado"]) for fila in filas}

    def duraciones_recientes(self, tx: Tx, limite: int = 100) -> list[int]:
        """`duracion_ms` de los últimos `limite` procesamientos completados: de ellos sale la
        espera de un Lote (DEC-25). Los últimos, porque la velocidad que vale es la del motor de
        hoy."""
        # shortcut: sin índice, ordena todos los procesamientos; cuando la tabla crezca, un
        # índice parcial `(terminado_el DESC) WHERE tipo = 'procesamiento' AND estado =
        # 'completado'`. Quien llama la pide solo si hay Recibidas esperando.
        filas = tx.execute(
            "SELECT duracion_ms FROM acervo.trabajo_procesamiento"
            " WHERE tipo = %s AND estado = 'completado' AND duracion_ms IS NOT NULL"
            " ORDER BY terminado_el DESC LIMIT %s",
            (TipoDeTrabajo.PROCESAMIENTO.value, limite),
        ).fetchall()
        return [fila["duracion_ms"] for fila in filas]

    def resumen(self) -> ResumenDeCola:
        """Encolados, en curso, fallidos y el encolado más viejo, de todos los tipos."""
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                "SELECT count(*) FILTER (WHERE estado = 'encolado') AS encolados,"
                " count(*) FILTER (WHERE estado = 'en_curso') AS en_curso,"
                # Los que `reprocesar --fallidos` recupera: ni el resumen ni la descripción.
                " count(*) FILTER (WHERE estado = 'fallido' AND ultimo AND tipo = ANY(%s))"
                " AS fallidos,"
                " min(encolado_el) FILTER (WHERE estado = 'encolado') AS mas_viejo_el"
                " FROM (SELECT tipo, estado, encolado_el, row_number() OVER ("
                "  PARTITION BY pieza_id, tipo ORDER BY encolado_el DESC) = 1 AS ultimo"
                "  FROM acervo.trabajo_procesamiento) AS t",
                ([tipo.value for tipo in REPROCESABLES],),
            ).fetchone()
        assert fila is not None
        return ResumenDeCola(**fila)

    def tomar(self, tipos: Collection[TipoDeTrabajo]) -> TrabajoTomado | None:
        """Toma el siguiente trabajo de `tipos`, o devuelve `None` si no hay ninguno.

        El siguiente es el de menor prioridad y, entre iguales, el que llegó antes (DEC-25). Un
        trabajo de quien valida (`DE_QUIEN_VALIDA`: el resumen rehecho, 146, o la descripción de
        una imagen embebida, 148) pasa delante de todo procesamiento, salvo que ya hayan pasado
        `RESUMENES_SEGUIDOS` desde que espera el procesamiento más viejo y desde que se tomó el
        último: entonces va detrás, y el Lote no se queda sin turno.
        `SKIP LOCKED` es lo que hace que dos consumidores no se pisen. La toma suma la
        generación y se confirma antes de empezar el trabajo: un corte a mitad ya quedó
        contado (DEC-30). Nace con un arriendo (`arriendo_vence_el`): si el proceso que lo
        tomó muere antes de cerrarlo, la próxima toma lo recupera sola.

        Antes de buscar trabajo nuevo, reclama el que quedó `en_curso` con el arriendo
        vencido y da por fallido el `encolado` que ya no se puede tomar: es la primera
        oportunidad que tiene la base de notarlo, y no hace falta ningún proceso aparte.
        """
        self.recuperar_vencidos(tipos)
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                # shortcut: la racha se cuenta recorriendo la tabla; con muchos trabajos, un
                # índice parcial por `iniciado_el` de los `resumen`.
                "WITH racha AS ("
                "  SELECT count(*) >= %s AS agotada FROM acervo.trabajo_procesamiento"
                f"  WHERE tipo IN ({_DE_QUIEN_VALIDA}) AND iniciado_el > GREATEST("
                "   (SELECT max(iniciado_el) FROM acervo.trabajo_procesamiento"
                "    WHERE tipo = 'procesamiento'),"
                "   (SELECT min(encolado_el) FROM acervo.trabajo_procesamiento"
                "    WHERE tipo = 'procesamiento' AND estado = 'encolado'))"
                "), siguiente AS ("
                "  SELECT trabajo_id FROM acervo.trabajo_procesamiento, racha"
                "  WHERE estado = 'encolado' AND tipo = ANY(%s)"
                "  AND generaciones < tope_generaciones"
                f"  ORDER BY CASE WHEN tipo NOT IN ({_DE_QUIEN_VALIDA}) THEN 0"
                "  WHEN racha.agotada THEN 1 ELSE -1"
                "  END, prioridad, encolado_el, trabajo_id"
                "  FOR UPDATE OF trabajo_procesamiento SKIP LOCKED LIMIT 1"
                ")"
                " UPDATE acervo.trabajo_procesamiento AS t"
                " SET estado = 'en_curso', generaciones = t.generaciones + 1,"
                "     tomado_por = %s, iniciado_el = now(),"
                "     arriendo_vence_el = now() + (t.tiempo_maximo_ms * interval"
                "     '1 millisecond') + %s"
                " FROM siguiente"
                " WHERE t.trabajo_id = siguiente.trabajo_id"
                " RETURNING t.trabajo_id, t.pieza_id, t.tipo, t.configuracion_efectiva,"
                " t.contexto_lote, t.imagen",
                (
                    RESUMENES_SEGUIDOS,
                    [tipo.value for tipo in tipos],
                    self.identificador,
                    _MARGEN_ARRIENDO,
                ),
            ).fetchone()
        if fila is None:
            return None
        return TrabajoTomado(
            trabajo_id=fila["trabajo_id"],
            pieza_id=fila["pieza_id"],
            tipo=TipoDeTrabajo(fila["tipo"]),
            configuracion_efectiva=ConfiguracionEfectiva.desde_json(
                fila["configuracion_efectiva"]
            ),
            contexto_lote=(
                None
                if fila["contexto_lote"] is None
                else ContextoDeLote.desde_json(fila["contexto_lote"])
            ),
            imagen=fila["imagen"],
        )

    def recuperar_vencidos(self, tipos: Collection[TipoDeTrabajo]) -> None:
        """Reclama los trabajos `en_curso` de `tipos` cuyo arriendo venció y da por fallidos
        los `encolado` que ya no se pueden tomar.

        La base es la única fuente de verdad del avance: no hay aviso de que un proceso
        murió, solo el hecho de que su arriendo venció. Si todavía le quedan generaciones,
        vuelve a `encolado` **sin sumar** (la generación ya se contó al tomar) y la próxima
        toma lo reintenta; si ya agotó su tope, queda `fallido` sin reintentarse solo, igual
        que cualquier otro fallo (ADR-0004, límite duro 2; DEC-146 punto 2). Un `encolado`
        agotado nadie lo podría tomar: sin esto quedaría encolado para siempre.
        """
        tipos_ = [tipo.value for tipo in tipos]
        with self._bd.transaccion() as conexion:
            conexion.execute(
                "UPDATE acervo.trabajo_procesamiento SET estado = 'encolado',"
                " tomado_por = NULL, arriendo_vence_el = NULL"
                " WHERE tipo = ANY(%s) AND estado = 'en_curso' AND arriendo_vence_el < now()"
                " AND generaciones < tope_generaciones",
                (tipos_,),
            )
            # Lo que la actualización de arriba no tocó porque ya no le quedaban generaciones
            # es exactamente lo que sigue `en_curso` con el arriendo vencido acá.
            conexion.execute(
                "UPDATE acervo.trabajo_procesamiento SET estado = 'fallido',"
                " terminado_el = now(), error = %s"
                " WHERE tipo = ANY(%s) AND estado = 'en_curso' AND arriendo_vence_el < now()",
                (_ERROR_ARRIENDO_AGOTADO, tipos_),
            )
            conexion.execute(
                "UPDATE acervo.trabajo_procesamiento SET estado = 'fallido',"
                " terminado_el = now(), error = %s"
                " WHERE tipo = ANY(%s) AND estado = 'encolado'"
                " AND generaciones >= tope_generaciones",
                (_ERROR_GENERACIONES_AGOTADAS, tipos_),
            )

    def error_del_procesamiento(self, pieza_id: UUID) -> str | None:
        """Por qué falló el último procesamiento de la Pieza, o `None` si no falló.

        Un fallo no es un estado: la Pieza sigue `Recibida` y el porqué vive acá (`spec.md`
        §3). El último intento manda: si se reencoló después de fallar, el error del intento
        anterior no tiene por qué seguir en pantalla.
        """
        with self._bd.transaccion() as conexion:
            fila = _ultimo_procesamiento(conexion, pieza_id)
        if fila is None or fila["estado"] != "fallido":
            return None
        error: str = fila["error"]
        return error

    def estado_del_procesamiento(self, pieza_id: UUID) -> str | None:
        """El estado del último trabajo de procesamiento de la Pieza, o `None` si no tiene.

        Con él la pantalla distingue «esperando al motor» (`encolado`) de «falló» (`fallido`)
        (DEC-146 punto 16).
        """
        with self._bd.transaccion() as conexion:
            fila = _ultimo_procesamiento(conexion, pieza_id)
        return None if fila is None else str(fila["estado"])

    def fallo_el_ultimo(self, tx: Tx, pieza_id: UUID) -> bool:
        """El último procesamiento de la Pieza terminó `fallido`: no hay uno encolado, en curso
        ni completado después (DEC-122)."""
        fila = _ultimo_procesamiento(tx, pieza_id)
        return fila is not None and fila["estado"] == "fallido"

    def completar(
        self,
        tx: Tx,
        trabajo: TrabajoTomado,
        *,
        duracion_ms: int,
        tokens_entrada: int,
        tokens_salida: int,
    ) -> None:
        """Cierra el trabajo en la transacción de quien guardó su resultado (DEC-99).

        Raises:
            TrabajoNoTomado: Si ya no lo tiene este trabajador: el resultado se descarta con
                la transacción entera, así un éxito tardío no pisa a quien lo retomó.
        """
        cerrado = tx.execute(
            "UPDATE acervo.trabajo_procesamiento SET estado = 'completado',"
            " terminado_el = now(), duracion_ms = %s, tokens_entrada = %s, tokens_salida = %s"
            # Quien cierra tiene que ser quien lo tiene `en_curso`: la columna es el arriendo.
            " WHERE trabajo_id = %s AND estado = 'en_curso' AND tomado_por = %s",
            (duracion_ms, tokens_entrada, tokens_salida, trabajo.trabajo_id, self.identificador),
        ).rowcount
        if cerrado != 1:
            raise TrabajoNoTomado(trabajo.trabajo_id)

    def cancelar_de_pieza(self, tx: Tx, pieza_id: UUID) -> int:
        """Cancela los trabajos `encolado` y `en_curso` de la Pieza, de los dos tipos, en la
        transacción del retiro (DEC-91, DEC-146 punto 1): el trabajador que tenía uno en curso
        no lo puede cerrar (`TrabajoNoTomado`) y su resultado se descarta. Solo lo usa `retiro`."""
        cancelados: int = tx.execute(
            "UPDATE acervo.trabajo_procesamiento SET estado = 'cancelado', terminado_el = now()"
            " WHERE pieza_id = %s AND estado IN ('encolado','en_curso')",
            (pieza_id,),
        ).rowcount
        return cancelados

    def devolver(self, trabajo: TrabajoTomado) -> None:
        """Vuelve el trabajo a `encolado` con la generación descontada: el pedido nunca llegó
        al motor (`MotorNoAlcanzable`), así que esa toma no gastó nada (DEC-86).

        Raises:
            TrabajoNoTomado: Si ya no lo tiene este trabajador.
        """
        with self._bd.transaccion() as conexion:
            devuelto = conexion.execute(
                "UPDATE acervo.trabajo_procesamiento SET estado = 'encolado',"
                " generaciones = generaciones - 1, tomado_por = NULL, arriendo_vence_el = NULL"
                " WHERE trabajo_id = %s AND estado = 'en_curso' AND tomado_por = %s",
                (trabajo.trabajo_id, self.identificador),
            ).rowcount
        if devuelto != 1:
            raise TrabajoNoTomado(trabajo.trabajo_id)

    def fallar(self, trabajo: TrabajoTomado, *, error: str) -> None:
        """Marca el trabajo fallido, sin reintento automático (DEC-113). La Pieza no cambia de
        estado: sigue `Recibida`, y el fallo vive acá.

        Raises:
            TrabajoNoTomado: Si ya no lo tiene este trabajador.
        """
        with self._bd.transaccion() as conexion:
            cerrado = conexion.execute(
                "UPDATE acervo.trabajo_procesamiento SET estado = 'fallido',"
                " terminado_el = now(), error = %s"
                " WHERE trabajo_id = %s AND estado = 'en_curso' AND tomado_por = %s",
                (error[:2000], trabajo.trabajo_id, self.identificador),
            ).rowcount
        if cerrado != 1:
            raise TrabajoNoTomado(trabajo.trabajo_id)
