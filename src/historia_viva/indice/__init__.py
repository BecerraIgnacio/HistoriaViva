"""El índice vectorial: la única puerta a `PGVectorStore` y a su tabla (spec, módulo `indice`).

Escribe con SQL propio en la transacción de quien llama, así indexar y desindexar son atómicos
con la transición de la Pieza (DEC-73, DEC-84), y consulta con `PGVectorStore`, que solo lee
(DEC-01). La forma de las filas la da la biblioteca (`node_to_metadata_dict`), así que la tienda
lee lo que escribe `reemplazar`: es el spike S-16, resuelto en el ticket 044. Búsqueda exacta con
filtro previo (DEC-07, RF-20). Esconde LlamaIndex, la forma de los filtros y el relleno a 1024.

Los filtros no usan `MetadataFilters`: la biblioteca escribe sus valores dentro del SQL sin
escaparlos. Van por `customize_query_fn`, como parámetros de SQLAlchemy.

LlamaIndex se importa adentro de lo que lo usa: importar este módulo, preguntar qué Piezas tienen
fragmentos (`piezas_con_fragmentos`) o desindexar (`quitar_fragmentos`) no lo carga (ticket 130,
084). `precargar` lo trae antes de atender.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy.exc
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from sqlalchemy import Integer, Select, column, func, select, table
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, array

from historia_viva.bd import AJUSTES_DE_SESION, ESPERA_S, BaseNoDisponible, Bd, Tx
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import FiltroDeBusqueda, ViaDeIngreso

if TYPE_CHECKING:
    from llama_index.core.schema import BaseNode

__all__ = [
    "DIMENSION",
    "TABLA",
    "EstadoDelIndice",
    "Fragmento",
    "FragmentoHallado",
    "FragmentoIndexado",
    "FilaDeFragmento",
    "Indice",
    "filas_de_fragmentos",
    "ids_de_piezas",
    "modelo_mayoritario",
    "piezas_con_fragmentos",
    "precargar",
    "quitar_fragmentos",
]

TABLA = "acervo.data_fragmento"
"""La única tabla donde `hv_app` borra: el índice es derivado (DEC-143)."""

DIMENSION = 1024
"""La de la columna `embedding` (DEC-03): un modelo de dimensión menor se rellena con ceros."""

_FRAGMENTOS = table(
    "data_fragmento", column("node_id"), column("text"), column("metadata_", JSONB),
    schema="acervo",
)
"""Las columnas de la tabla de la biblioteca que lee `listar`."""

_CANDADO = 44
"""Primera clave del candado consultivo por Pieza. Las claves de dos enteros no se cruzan con la de
un `bigint` que usa `migrar` (manual de Postgres, «Advisory Locks»)."""


@dataclass(frozen=True)
class Fragmento:
    """Un trozo de una Ficha Validada con lo que lo filtra y lo ubica (spec §1.8)."""

    fragmento_id: UUID
    pieza_id: UUID
    ficha_id: UUID
    tipo: str
    tipo_actividad: str
    carreras: tuple[str, ...]
    fecha_inicio: date
    fecha_fin: date
    clase_original: ClaseDeOriginal
    ubicacion: Mapping[str, Any]
    inicio: int
    fin: int
    texto: str
    modelo_embedding: str
    via_ingreso: ViaDeIngreso


@dataclass(frozen=True)
class FragmentoIndexado(Fragmento):
    vector: Sequence[float]


@dataclass(frozen=True)
class FragmentoHallado(Fragmento):
    puntaje: float
    """Similitud coseno: 1 es el mismo sentido."""


@dataclass(frozen=True)
class EstadoDelIndice:
    fragmentos: int
    de_otro_modelo: int

    @property
    def incompleto(self) -> bool:
        """Hay fragmentos que la búsqueda no ve: los embebió otro modelo (DEC-02)."""
        return self.de_otro_modelo > 0


def precargar() -> None:
    """Carga LlamaIndex: lo paga el arranque y no el primer pedido que indexa o busca."""
    import llama_index.core.schema  # noqa: F401
    import llama_index.vector_stores.postgres  # noqa: F401


def quitar_fragmentos(tx: Tx, pieza_id: UUID) -> int:
    """Desindexa la Pieza en la transacción de quien llama (DEC-73), sin armar `Indice`: quien
    desindexa tiene la Pieza bloqueada, y cargar llama-index ahí la retiene por segundos."""
    # Hasta el COMMIT, otro `reemplazar` o `quitar` de la misma Pieza espera aquí: sin esto, su
    # DELETE no ve las filas recién insertadas y su INSERT choca en `node_id`.
    tx.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_CANDADO, str(pieza_id)))
    return tx.execute(
        "DELETE FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = %s", (str(pieza_id),)
    ).rowcount


def piezas_con_fragmentos(tx: Tx, pieza_ids: Collection[UUID]) -> set[UUID]:
    """De `pieza_ids`, las que tienen algún fragmento, por el índice `fragmento_pieza`. Sin
    armar `Indice`: lo pregunta la lista de Piezas en cada sondeo (084)."""
    filas = tx.execute(
        "SELECT DISTINCT metadata_->>'pieza_id' AS pieza_id FROM acervo.data_fragmento"
        " WHERE metadata_->>'pieza_id' = ANY(%s)",
        ([str(pieza_id) for pieza_id in pieza_ids],),
    ).fetchall()
    return {UUID(f["pieza_id"]) for f in filas}


@dataclass(frozen=True)
class FilaDeFragmento:
    """Una fila del índice tal como está, sin leer: M2 no confía en sus metadatos."""

    pieza_id: UUID
    node_id: str
    texto: str
    metadata: Any

    def leer(self) -> Fragmento:
        """Raises:
            ValueError, KeyError, TypeError: Los metadatos no tienen la forma de `reemplazar`.
        """
        fragmento = Fragmento(**_campos(self.node_id, self.texto, self.metadata))
        if not (
            isinstance(fragmento.inicio, int) and isinstance(fragmento.fin, int)
            and isinstance(fragmento.ubicacion, Mapping)
        ):
            raise TypeError(f"Fragmento {self.node_id} con `inicio`, `fin` o `ubicacion` ilegible.")
        return fragmento


def filas_de_fragmentos(tx: Tx, pieza_ids: Collection[UUID]) -> list[FilaDeFragmento]:
    """Las filas de cada Pieza pedida, de cualquier modelo y sin ordenar ni leer, en la
    transacción de quien llama y sin armar `Indice`: lo que M2 compara con la Ficha."""
    filas = tx.execute(
        "SELECT metadata_->>'pieza_id' AS pieza_id, node_id, text, metadata_"
        " FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = ANY(%s)",
        ([str(pieza_id) for pieza_id in pieza_ids],),
    ).fetchall()
    return [
        FilaDeFragmento(UUID(f["pieza_id"]), f["node_id"], f["text"], f["metadata_"])
        for f in filas
    ]


def ids_de_piezas(tx: Tx) -> set[UUID]:
    """Las Piezas con al menos un fragmento, de cualquier modelo, sin armar `Indice`: `banco m2`
    marca las que no son Validadas no retiradas (violación de RF-15)."""
    filas = tx.execute(
        "SELECT DISTINCT metadata_->>'pieza_id' AS pieza_id FROM acervo.data_fragmento"
    ).fetchall()
    return {UUID(f["pieza_id"]) for f in filas}


def modelo_mayoritario(tx: Tx, prefijo: str) -> str | None:
    """El `modelo_embedding` que empieza con `prefijo` (`<modelo>@`) y tiene más fragmentos:
    el vigente, sin preguntarle el digest al motor. `None` si ninguno empieza así."""
    fila = tx.execute(
        "SELECT metadata_->>'modelo_embedding' AS modelo FROM acervo.data_fragmento"
        " WHERE left(metadata_->>'modelo_embedding', length(%s)) = %s"
        " GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1",
        (prefijo, prefijo),
    ).fetchone()
    return None if fila is None else str(fila["modelo"])


class Indice:
    def __init__(self, *, bd: Bd, dsn: str) -> None:
        from llama_index.vector_stores.postgres import PGVectorStore

        self._bd = bd
        # psycopg 3 por el dialecto `postgresql+psycopg`, con el DSN de `hv_app`. La tabla la crea
        # la migración: `perform_setup=False` no deja que la biblioteca cree nada.
        url = "postgresql+psycopg://"
        self._tienda = PGVectorStore(
            connection_string=url,
            # La biblioteca arma también un motor asíncrono, que no conecta hasta usarse.
            async_connection_string=url,
            schema_name="acervo",
            table_name="fragmento",
            embed_dim=DIMENSION,
            use_jsonb=True,
            perform_setup=False,
            # Los mismos límites que `bd`: con la base caída, buscar falla en 3 s (historia 55).
            create_engine_kwargs={
                "connect_args": {
                    **conninfo_to_dict(dsn),
                    "connect_timeout": ESPERA_S,
                    "options": AJUSTES_DE_SESION,
                },
                "pool_pre_ping": True,
                # Sin lugar en el pool, falla a los 3 s como `bd`, no a los 30 (visto en 044).
                "pool_timeout": ESPERA_S,
            },
            customize_query_fn=_filtrar,
        )

    def reemplazar(self, tx: Tx, pieza_id: UUID, fragmentos: Sequence[FragmentoIndexado]) -> None:
        """Los fragmentos de la Pieza pasan a ser estos. Otra transacción ve los de antes hasta el
        `COMMIT` de quien llama, nunca la Pieza sin fragmentos; repetirlo no duplica."""
        ajenos = {f.pieza_id for f in fragmentos} - {pieza_id}
        if ajenos:
            raise ValueError(f"Fragmentos de otra Pieza al reemplazar {pieza_id}: {sorted(ajenos)}.")
        self.quitar(tx, pieza_id)
        tx.cursor().executemany(
            "INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)"
            " VALUES (%s, %s, %s, %s::vector)",
            [
                (str(f.fragmento_id), f.texto, Jsonb(_fila(f)), str(_rellenar(f.vector)))
                for f in fragmentos
            ],
        )

    def quitar(self, tx: Tx, pieza_id: UUID) -> int:
        """Desindexa la Pieza en la transacción de quien llama (DEC-73)."""
        return quitar_fragmentos(tx, pieza_id)

    def buscar(
        self,
        vector: Sequence[float],
        *,
        modelo_embedding: str,
        filtro: FiltroDeBusqueda,
        k: int,
    ) -> list[FragmentoHallado]:
        """Los `k` más parecidos entre los que pasan el filtro y son del modelo vigente."""
        from llama_index.core.vector_stores.types import VectorStoreQuery

        consulta = VectorStoreQuery(query_embedding=_rellenar(vector), similarity_top_k=k)
        try:
            # `query` le pasa los argumentos con nombre a `customize_query_fn` (`_filtrar`).
            resultado = self._tienda.query(consulta, pedido=(filtro, modelo_embedding))
        except (sqlalchemy.exc.OperationalError, sqlalchemy.exc.TimeoutError) as error:
            raise BaseNoDisponible(error) from error
        nodos, puntajes = resultado.nodes or [], resultado.similarities or []
        return [_hallado(n, p) for n, p in zip(nodos, puntajes, strict=True)]

    def listar(
        self,
        *,
        filtro: FiltroDeBusqueda,
        modelo_embedding: str,
        limite: int,
        por_pieza: int | None = None,
    ) -> list[FragmentoHallado]:
        """Sin vector: los que pasan el filtro y son del modelo vigente, por `fecha_inicio`,
        Pieza y posición en el cuerpo, hasta `limite`. Con `por_pieza`, solo los primeros de cada Pieza en el
        orden del cuerpo: una Pieza larga no ocupa toda la lista. El puntaje es 0: sin consulta
        no hay similitud."""
        m = _FRAGMENTOS.c.metadata_
        orden = func.row_number().over(
            partition_by=m["pieza_id"].astext, order_by=m["inicio"].astext.cast(Integer)
        )
        filtrados = _filtrar(
            select(_FRAGMENTOS.c.node_id, _FRAGMENTOS.c.text, m, orden.label("orden")),
            _FRAGMENTOS.c,
            (filtro, modelo_embedding),
        ).subquery()
        consulta = (
            select(filtrados.c.node_id, filtrados.c.text, filtrados.c.metadata_)
            # Las Citas de una Pieza, juntas y en el orden del cuerpo: el `node_id` es un UUIDv5.
            .order_by(
                filtrados.c.metadata_["fecha_inicio"].astext,
                filtrados.c.metadata_["pieza_id"].astext,
                filtrados.c.orden,
            )
            .limit(limite)
        )
        if por_pieza is not None:
            consulta = consulta.where(filtrados.c.orden <= por_pieza)
        # El mismo `_filtrar` que `buscar`, compilado a parámetros de psycopg y corrido por `bd`.
        compilada = consulta.compile(
            dialect=postgresql.psycopg.dialect(), compile_kwargs={"render_postcompile": True}
        )
        with self._bd.transaccion() as tx:
            filas = tx.execute(str(compilada), compilada.params).fetchall()
        return [
            FragmentoHallado(**_campos(f["node_id"], f["text"], f["metadata_"]), puntaje=0.0)
            for f in filas
        ]

    def modelo_mayoritario(self, prefijo: str) -> str | None:
        """El `modelo_embedding` que empieza con `prefijo` (`<modelo>@`) y tiene más fragmentos:
        el vigente para buscar sin texto, sin preguntarle el digest al motor. `None` si
        ninguno empieza así."""
        with self._bd.transaccion() as tx:
            return modelo_mayoritario(tx, prefijo)

    def de_pieza(self, pieza_id: UUID) -> list[Fragmento]:
        """Todos los fragmentos de la Pieza, de cualquier modelo, en el orden del cuerpo: lo que
        `indexacion.inspeccionar` compara con la Ficha (M2)."""
        with self._bd.transaccion() as tx:
            filas = tx.execute(
                "SELECT node_id, text, metadata_ FROM acervo.data_fragmento"
                " WHERE metadata_->>'pieza_id' = %s"
                " ORDER BY (metadata_->>'inicio')::int, node_id",
                (str(pieza_id),),
            ).fetchall()
        return [Fragmento(**_campos(f["node_id"], f["text"], f["metadata_"])) for f in filas]

    def cobertura(self, tx: Tx, modelo_embedding_vigente: str) -> dict[UUID, int]:
        """Cada Pieza con fragmentos y cuántos de ellos embebió otro modelo (0: todos del
        vigente). Lo que `indexacion.progreso` cruza con las Validadas (DEC-146 punto 6)."""
        filas = tx.execute(
            "SELECT metadata_->>'pieza_id' AS pieza_id, count(*) FILTER"
            " (WHERE metadata_->>'modelo_embedding' <> %s) AS de_otro_modelo"
            " FROM acervo.data_fragmento GROUP BY 1",
            (modelo_embedding_vigente,),
        ).fetchall()
        return {UUID(f["pieza_id"]): f["de_otro_modelo"] for f in filas}

    def estado(self, modelo_embedding_vigente: str) -> EstadoDelIndice:
        with self._bd.transaccion() as tx:
            fila = tx.execute(
                "SELECT count(*) AS fragmentos, count(*) FILTER"
                " (WHERE metadata_->>'modelo_embedding' <> %s) AS de_otro_modelo"
                " FROM acervo.data_fragmento",
                (modelo_embedding_vigente,),
            ).fetchone()
        assert fila is not None
        return EstadoDelIndice(fragmentos=fila["fragmentos"], de_otro_modelo=fila["de_otro_modelo"])


def _rellenar(vector: Sequence[float]) -> list[float]:
    """Ceros a la derecha hasta 1024: el coseno no cambia y el orden de similitud tampoco."""
    if len(vector) > DIMENSION:
        raise ValueError(
            f"El vector tiene {len(vector)} dimensiones y el índice admite hasta {DIMENSION}."
        )
    return [*vector, *[0.0] * (DIMENSION - len(vector))]


def _fila(fragmento: FragmentoIndexado) -> dict[str, Any]:
    """`metadata_` en el formato de la biblioteca, el que `PGVectorStore.query` sabe leer."""
    from llama_index.core.schema import TextNode
    from llama_index.core.vector_stores.utils import node_to_metadata_dict

    f = fragmento
    metadata = {
        "pieza_id": str(f.pieza_id),
        "ficha_id": str(f.ficha_id),
        "tipo": f.tipo,
        "tipo_actividad": f.tipo_actividad,
        "carreras": sorted(f.carreras),
        # ISO completo: comparadas como texto, ordenan igual que las fechas.
        "fecha_inicio": f.fecha_inicio.isoformat(),
        "fecha_fin": f.fecha_fin.isoformat(),
        "clase_original": f.clase_original.value,
        "ubicacion": dict(f.ubicacion),
        "inicio": f.inicio,
        "fin": f.fin,
        "modelo_embedding": f.modelo_embedding,
        "via_ingreso": f.via_ingreso.value,
    }
    nodo = TextNode(id_=str(f.fragmento_id), text=f.texto, metadata=metadata)
    return node_to_metadata_dict(nodo, remove_text=True, flat_metadata=False)


def _hallado(nodo: BaseNode, puntaje: float) -> FragmentoHallado:
    return FragmentoHallado(
        **_campos(nodo.node_id, nodo.get_content(), nodo.metadata), puntaje=puntaje
    )


def _campos(node_id: str, texto: str, m: Mapping[str, Any]) -> dict[str, Any]:
    return dict(
        fragmento_id=UUID(node_id),
        pieza_id=UUID(m["pieza_id"]),
        ficha_id=UUID(m["ficha_id"]),
        tipo=m["tipo"],
        tipo_actividad=m["tipo_actividad"],
        carreras=tuple(m["carreras"]),
        fecha_inicio=date.fromisoformat(m["fecha_inicio"]),
        fecha_fin=date.fromisoformat(m["fecha_fin"]),
        clase_original=ClaseDeOriginal(m["clase_original"]),
        ubicacion=m["ubicacion"],
        inicio=m["inicio"],
        fin=m["fin"],
        texto=texto,
        modelo_embedding=m["modelo_embedding"],
        via_ingreso=ViaDeIngreso(m["via_ingreso"]),
    )


def _filtrar(consulta: Select, tabla: Any, pedido: tuple[FiltroDeBusqueda, str]) -> Select:
    """Todos van al `WHERE`, antes del `ORDER BY embedding <=> …` (DEC-07): Postgres recorre solo
    las filas que pasan. Las Fechas se solapan: el rango de la Pieza toca `[desde, hasta]`; en ISO
    completo, comparadas como texto, ordenan igual que las fechas."""
    filtro, modelo_embedding = pedido
    m = tabla.metadata_
    condiciones = [m["modelo_embedding"].astext == modelo_embedding]
    if filtro.hasta:
        condiciones.append(m["fecha_inicio"].astext <= filtro.hasta.isoformat())
    if filtro.desde:
        condiciones.append(m["fecha_fin"].astext >= filtro.desde.isoformat())
    if filtro.tipos:
        condiciones.append(m["tipo"].astext.in_(filtro.tipos))
    if filtro.tipos_actividad:
        condiciones.append(m["tipo_actividad"].astext.in_(filtro.tipos_actividad))
    if filtro.carreras:
        # `?|`: la lista de la Pieza tiene alguna de las pedidas; una lista vacía no tiene ninguna.
        condiciones.append(m["carreras"].has_any(array(filtro.carreras)))
    if filtro.via_ingreso:
        condiciones.append(m["via_ingreso"].astext == filtro.via_ingreso.value)
    return consulta.where(*condiciones)
