"""Módulo de Fichas sobre PostgreSQL; los bytes de los originales los guarda `originales`.

Es el único lugar del código que conoce las tablas de Piezas y Fichas (RNF-09). Implementa
las operaciones del contrato a medida que los issues las ejercitan: hoy, cargar una Pieza y
consultarla (issue 02), guardar la Ficha propuesta que la lleva de `Recibida` a `Pendiente`
(issue 03) y guardar lo que editó una persona, que la deja `Validada` o `Incompleta`
(issue 04).

**La máquina de estados vive acá.** `guardar_ficha_propuesta` es la única transición
`Recibida -> Pendiente` del sistema, y `guardar_edicion` la única hacia `Validada` o
`Incompleta`, salvo la firma en bloque (`firmar_en_bloque`, solo desde `Pendiente`). Las dos
verifican el estado contra la fila bloqueada, no contra lo que crea
quien llama.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from historia_viva.bd import Bd, Tx
from historia_viva.capa_modelos.configuracion import ConfiguracionEfectiva
from historia_viva.catalogos import (
    validar_carreras,
    validar_tipo_actividad,
    validar_tipo_de_pieza,
)
from historia_viva.chequeo_inyeccion import (
    ETIQUETA_DEL_PATRON,
    EtiquetaDeControl,
    Modo,
    revisar,
)
from historia_viva.dominio.afirmaciones import (
    FALTAN_AFIRMACIONES,
    FALTAN_DESCRIPCIONES,
    Afirmacion,
    DescripcionPropuesta,
    EstadoAfirmacion,
    EstadoDeImagen,
    ImagenDescribible,
    ParaRehacerResumen,
    ResumenRehecho,
    UbicacionDeImagen,
)
from historia_viva.dominio.bloque import Candidata
from historia_viva.dominio.cruce import Region
from historia_viva.dominio.cuerpo import (
    CuerpoDeFicha,
    escapar_marcas,
    escribir_afirmaciones,
    tiene_afirmaciones,
)
from historia_viva.dominio.errores import (
    AfirmacionNoEncontrada,
    ImagenNoEncontrada,
    PiezaDuplicada,
    PiezaNoEncontrada,
    TransicionInvalida,
    VersionDesactualizada,
)
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha, derivar_rango
from historia_viva.dominio.lote import Herencia
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import (
    CAMPOS_DUROS,
    CAMPOS_OBLIGATORIOS,
    CamposDuros,
    CamposFicha,
    DetallePieza,
    EdicionFicha,
    EtiquetaHallada,
    FichaIndexable,
    FichaVigente,
    FuenteDeFecha,
    MetodoAutenticacion,
    OrigenVersion,
    OriginalGuardado,
    CambioPosterior,
    PiezaBuscable,
    PiezaDelPeriodo,
    PiezaMedible,
    PiezaPublicada,
    ResultadoDeEdicion,
    ResumenPieza,
    ViaDeIngreso,
)
from historia_viva.fichas.markdown import analizar, serializar
from historia_viva.originales import OriginalTemporal

__all__ = [
    "ConteosDeLote",
    "FilaDeRetirada",
    "ModuloFichasPostgres",
    "alerta_de_afirmaciones",
    "contar_firmas_de_ensayo",
    "contar_piezas",
    "etiquetas_de_control",
]


@dataclass(frozen=True)
class ConteosDeLote:
    """Lo que `v_lote_resumen` dice de las Piezas no retiradas de un Lote (084)."""

    por_estado: dict[Estado, int]
    para_analisis_humano: int
    """Con la Ficha vigente `sistema`: un video o una completada a mano (034)."""


@dataclass(frozen=True, kw_only=True)
class FilaDeRetirada:
    """Una Pieza retirada como la lista Administración › Retiradas (106)."""

    pieza_id: UUID
    nombre_archivo: str
    estado: Estado
    """El que tenía al retirarse: el retiro no lo cambia."""
    tipo: str | None
    lote_id: UUID | None
    borrada_el: datetime
    retirada_por: str
    """El nombre de quien la retiró."""
    motivo: str
    duplicada: bool
    """Su original ya es otra Pieza no retirada: restaurarla da `pieza-duplicada`."""

# Columna de `acervo.pieza` que responde por cada campo obligatorio de la Ficha.
_COLUMNA_DE_CAMPO = {campo: campo for campo in CAMPOS_OBLIGATORIOS} | {
    "archivo_original": "archivo_original_id"
}

_CAMPOS_PIEZA = (
    "pieza_id",
    "estado",
    "tipo",
    "resumen",
    "nombre_archivo",
    "creada_por",
    "tipo_mime",
    "tamano_bytes",
    "creada_el",
    "actualizada_el",
    "fecha_inicio",
    "fecha_fin",
    "precision_fecha",
    "origen",
    "archivo_original_id",
    "validada_por",
    "validada_el",
    "modo_validacion",
    "alerta_inyeccion",
    "lote_id",
    "campos_heredados",
    "contexto_heredado",
    "campos_de_la_persona",
    "contexto_de_la_persona",
    "tipo_actividad",
    "propietario",
    "autor",
    "clase_original",
    "huella_original",
    "via_ingreso",
    "metadata_original",
    "publicable",
)
# Las Carreras vigentes de la Pieza (DEC-143): `quitada_el` nula, en orden de `carrera_id` por
# bytes (`COLLATE "C"`), el mismo que `sorted` en `guardar_edicion`: glibc ignora el guion.
_CARRERAS_VIGENTES = (
    "ARRAY(SELECT pc.carrera_id FROM acervo.pieza_carrera pc"
    " WHERE pc.pieza_id = pieza.pieza_id AND pc.quitada_el IS NULL"
    " ORDER BY pc.carrera_id COLLATE \"C\") AS carreras"
)
_COLUMNAS_PIEZA = ", ".join((*_CAMPOS_PIEZA, _CARRERAS_VIGENTES))
# Derivada (DEC-122, 034): todas sus versiones son `sistema`, así que ni un modelo ni una persona
# la leyeron. La versión `sistema` de la herencia tardía del Lote (151) no la vuelve tal.
_REQUIERE_ANALISIS_HUMANO = (
    "(EXISTS (SELECT 1 FROM acervo.ficha f WHERE f.pieza_id = pieza.pieza_id)"
    " AND NOT EXISTS (SELECT 1 FROM acervo.ficha f WHERE f.pieza_id = pieza.pieza_id"
    " AND f.origen_version <> 'sistema')) AS requiere_analisis_humano"
)
_SELECT_PIEZA = f"SELECT {_COLUMNAS_PIEZA} FROM acervo.pieza"

# Lo que `buscables` y `publicada` leen de una Pieza con su Ficha vigente.
_SELECT_BUSCABLE = (
    "SELECT pieza.pieza_id, f.ficha_id, pieza.tipo, pieza.tipo_actividad, pieza.fecha_inicio,"
    " pieza.fecha_fin, pieza.precision_fecha, pieza.clase_original, pieza.via_ingreso,"
    f" pieza.resumen, pieza.nombre_archivo, pieza.validada_el, {_CARRERAS_VIGENTES}"
)
_DESDE_BUSCABLE = (
    "FROM acervo.pieza JOIN acervo.ficha f ON f.pieza_id = pieza.pieza_id AND f.vigente"
)

# Lo que `validadas_con_fecha` y `fuentes` leen de una Pieza con su Ficha vigente.
_VALIDADA_SIN_RETIRAR = f"(pieza.estado = '{Estado.VALIDADA.value}' AND pieza.borrada_el IS NULL)"
# Lo que puede ser fuente de una efeméride o un Relato nuevos (DEC-143).
_CANDIDATA = f"({_VALIDADA_SIN_RETIRAR} AND pieza.publicable)"
_SELECT_FUENTE = (
    "SELECT pieza.pieza_id, f.ficha_id, pieza.tipo, pieza.fecha_inicio, pieza.fecha_fin,"
    " pieza.precision_fecha, pieza.resumen, pieza.alerta_inyeccion, pieza.publicable,"
    f" {_VALIDADA_SIN_RETIRAR} AS validada {_DESDE_BUSCABLE}"
)

# Las no retiradas cuya Fecha solapa `[desde, hasta]` y, si se piden Carreras, con alguna.
_DEL_PERIODO = (
    "pieza.borrada_el IS NULL AND pieza.fecha_inicio <= %s"
    " AND coalesce(pieza.fecha_fin, pieza.fecha_inicio) >= %s"
    " AND (cardinality(%s::text[]) = 0 OR EXISTS (SELECT 1 FROM acervo.pieza_carrera pc"
    " WHERE pc.pieza_id = pieza.pieza_id AND pc.quitada_el IS NULL"
    " AND pc.carrera_id = ANY(%s::text[])))"
)


def _parametros_del_periodo(
    desde: date, hasta: date, carreras: Sequence[str]
) -> tuple[date, date, list[str], list[str]]:
    return (hasta, desde, list(carreras), list(carreras))


_SELECT_VERSION = (
    "SELECT ficha_id, version, origen_version, contenido_markdown, editada_por, editada_el,"
    " metodo_autenticacion, informe_inyeccion FROM acervo.ficha WHERE pieza_id = %s"
)
_SELECT_FICHA_VIGENTE = f"{_SELECT_VERSION} AND vigente"
_SELECT_PROPUESTA = f"{_SELECT_VERSION} AND version = 1"

_SELECT_ORIGINAL = (
    "SELECT archivo_original_id, nombre_archivo, tipo_mime, tamano_bytes, creada_el,"
    " huella_original FROM acervo.pieza"
)

SQL_CONTEOS = (
    "SELECT json_build_object("
    "'piezas_por_estado', (SELECT coalesce(json_object_agg(estado, n ORDER BY estado), '{}')"
    " FROM (SELECT estado, count(*) AS n FROM acervo.pieza GROUP BY estado) AS c),"
    "'fichas_por_version', (SELECT coalesce(json_object_agg(version, n ORDER BY version), '{}')"
    " FROM (SELECT version, count(*) AS n FROM acervo.ficha GROUP BY version) AS c))"
)
"""Piezas por estado y versiones de Ficha por número: lo que la restauración probada compara
con el origen (spec §2.3). Un solo valor JSON, para `psql` y para psycopg."""


class ModuloFichasPostgres:
    """Módulo de Fichas: filas en `acervo`; los bytes de los originales, en `originales`."""

    def __init__(self, *, bd: Bd) -> None:
        self._bd = bd

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
        herencia: Herencia | None = None,
        metadata_original: dict[str, Any] | None = None,
    ) -> DetallePieza:
        """Crea la Pieza `Recibida` en la transacción de quien llama.

        Con `herencia`, la Pieza copia de su Lote el origen, la Fecha, el propietario y las
        Carreras, y guarda el evento y el lugar hasta que la v1 los ponga en su Contexto
        (DEC-113, DEC-126).

        Un video no tiene plan de modelos (ADR-0005): nace `Pendiente` con la Ficha v1
        `sistema` vacía, que lleva el Contexto del Lote si lo hay.

        Raises:
            PiezaDuplicada: Si otra subida del mismo original ganó la carrera (DEC-106).
        """
        sin_plan = original.clase is ClaseDeOriginal.VIDEO
        contexto = None if herencia is None else herencia.contexto
        try:
            # Punto de guardado: si choca con `pieza_huella_unica`, la transacción sigue
            # usable para releer la Pieza que ganó.
            with tx.transaction():
                fila = tx.execute(
                    "INSERT INTO acervo.pieza (pieza_id, estado, creada_por, archivo_original_id,"
                    " nombre_archivo, tipo_mime, tamano_bytes, clase_original, via_ingreso,"
                    " huella_original, lote_id, origen, fecha_inicio, fecha_fin, precision_fecha,"
                    " propietario, campos_heredados, contexto_heredado, metadata_original)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    f" %s, %s) RETURNING {_COLUMNAS_PIEZA}",
                    (
                        pieza_id,
                        (Estado.PENDIENTE if sin_plan else Estado.RECIBIDA).value,
                        creada_por,
                        archivo_original,
                        nombre_archivo,
                        original.tipo_mime,
                        original.tamano_bytes,
                        original.clase.value,
                        via_ingreso.value,
                        original.sha256,
                        None if herencia is None else herencia.lote_id,
                        None if contexto is None else contexto.origen,
                        None if contexto is None else contexto.fecha_inicio,
                        None if contexto is None else contexto.fecha_fin,
                        None
                        if contexto is None or contexto.precision_fecha is None
                        else contexto.precision_fecha.value,
                        None if herencia is None else herencia.propietario,
                        [] if herencia is None else list(herencia.campos),
                        None if contexto is None else contexto.contexto_de_la_ficha(),
                        None if metadata_original is None else Jsonb(metadata_original),
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            self.exigir_huella_libre(tx, original.sha256)
            raise
        assert fila is not None
        if herencia is not None and herencia.carreras:
            # Antes de la v1: `_insertar_version` quitaría las que no lea en la fila.
            tx.execute(
                "INSERT INTO acervo.pieza_carrera (pieza_id, carrera_id)"
                " SELECT %s, unnest(%s::text[])",
                (pieza_id, list(herencia.carreras)),
            )
            fila = tx.execute(f"{_SELECT_PIEZA} WHERE pieza_id = %s", (pieza_id,)).fetchone()
            assert fila is not None
        if sin_plan:
            _insertar_v1_sistema(tx, fila)
            fila = tx.execute(f"{_SELECT_PIEZA} WHERE pieza_id = %s", (pieza_id,)).fetchone()
            assert fila is not None
        return _detalle(fila)

    def crear_aporte(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        nombre: str,
        original: OriginalTemporal,
        archivo_original: str,
        texto: str,
    ) -> DetallePieza:
        """Crea la Pieza de un Aporte en la transacción de quien llama: clase `texto`, sin
        persona que la creó, `via_ingreso = aporte` y `Pendiente`, con la v1 `agente_externo`
        cuyo cuerpo es `texto`. No tiene plan de modelos: nadie la encola (DEC-10)."""
        fila = tx.execute(
            "INSERT INTO acervo.pieza (pieza_id, estado, archivo_original_id, nombre_archivo,"
            " tipo_mime, tamano_bytes, clase_original, via_ingreso, huella_original)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNAS_PIEZA}",
            (
                pieza_id,
                Estado.PENDIENTE.value,
                archivo_original,
                nombre,
                original.tipo_mime,
                original.tamano_bytes,
                ClaseDeOriginal.TEXTO.value,
                ViaDeIngreso.APORTE.value,
                original.sha256,
            ),
        ).fetchone()
        assert fila is not None
        # Todo al cuerpo: lo que escribió un Agente no es Contexto de una persona.
        cuerpo = CuerpoDeFicha(escapar_marcas(texto), "").armar()
        _insertar_version(
            tx, pieza_id, _campos(fila), cuerpo, version=1, origen_version="agente_externo"
        )
        fila = tx.execute(f"{_SELECT_PIEZA} WHERE pieza_id = %s", (pieza_id,)).fetchone()
        assert fila is not None
        return _detalle(fila)

    def validadas(self, tx: Tx, piezas: Collection[UUID]) -> set[UUID]:
        """Cuáles de `piezas` están Validadas, no retiradas y publicables: las que valen como
        fuente de un Aporte (un Aporte Validado también, DEC-134). Una no publicable se rechaza
        como una inexistente: `/v1` no la muestra (DEC-143)."""
        filas = tx.execute(
            "SELECT pieza_id FROM acervo.pieza WHERE estado = %s AND borrada_el IS NULL"
            " AND publicable AND pieza_id = ANY(%s)",
            (Estado.VALIDADA.value, list(piezas)),
        ).fetchall()
        return {fila["pieza_id"] for fila in filas}

    def buscar_por_huella(self, tx: Tx, sha256: str) -> DetallePieza | None:
        """La Pieza subida con ese mismo original, si la hay, con su Lote (DEC-20)."""
        fila = tx.execute(
            f"{_SELECT_PIEZA} WHERE huella_original = %s AND via_ingreso = %s"
            " AND borrada_el IS NULL",
            (sha256, ViaDeIngreso.ARCHIVO.value),
        ).fetchone()
        return None if fila is None else _detalle(fila)

    def con_la_huella(self, tx: Tx, sha256: str | None) -> list[UUID]:
        """Todas las Piezas con ese original, retiradas incluidas (la supresión, 125)."""
        filas = tx.execute(
            "SELECT pieza_id FROM acervo.pieza WHERE huella_original = %s ORDER BY pieza_id",
            (sha256,),
        ).fetchall()
        return [fila["pieza_id"] for fila in filas]

    def exigir_huella_libre(self, tx: Tx, sha256: str) -> None:
        """Raises:
        PiezaDuplicada: Con la Pieza previa y su Lote, si ese original ya es una Pieza (DEC-20).
        """
        previa = self.buscar_por_huella(tx, sha256)
        if previa is not None:
            raise PiezaDuplicada(previa.fila.pieza_id, previa.fila.estado, previa.lote_id)

    def contar_del_lote(self, tx: Tx, lote_id: UUID) -> int:
        """Cuántas Piezas tiene el Lote; lo cuenta `lotes` con el Lote ya bloqueado (R-27)."""
        fila = tx.execute(
            # Una retirada no ocupa lugar en el Lote.
            "SELECT count(*) AS total FROM acervo.pieza WHERE lote_id = %s AND borrada_el IS NULL",
            (lote_id,),
        ).fetchone()
        assert fila is not None
        total: int = fila["total"]
        return total

    def contar_por_estado(self, tx: Tx, lote_id: UUID) -> ConteosDeLote:
        """Los conteos de `v_lote_resumen`, sin las retiradas; un Lote sin Piezas da ceros."""
        return self.contar_por_lotes(tx, [lote_id])[lote_id]

    def contar_por_lotes(self, tx: Tx, lote_ids: Sequence[UUID]) -> dict[UUID, ConteosDeLote]:
        """`contar_por_estado` de varios Lotes en una consulta (la lista de Lotes, 085)."""
        filas = tx.execute(
            "SELECT lote_id, n_recibida, n_pendiente, n_incompleta, n_validada, n_analisis_humano"
            " FROM acervo.v_lote_resumen WHERE lote_id = ANY(%s)",
            (list(lote_ids),),
        ).fetchall()
        conteos = {
            lote_id: ConteosDeLote(por_estado=dict.fromkeys(Estado, 0), para_analisis_humano=0)
            for lote_id in lote_ids
        }
        for fila in filas:
            conteos[fila["lote_id"]] = ConteosDeLote(
                por_estado={estado: fila[f"n_{estado.value.lower()}"] for estado in Estado},
                para_analisis_humano=fila["n_analisis_humano"],
            )
        return conteos

    def ids_del_lote(self, tx: Tx, lote_id: UUID, estado: Estado) -> list[UUID]:
        """Las Piezas no retiradas del Lote en `estado`."""
        filas = tx.execute(
            "SELECT pieza_id FROM acervo.pieza WHERE lote_id = %s AND estado = %s"
            " AND borrada_el IS NULL",
            (lote_id, estado.value),
        ).fetchall()
        return [fila["pieza_id"] for fila in filas]

    def bloquear_vigente(self, tx: Tx, pieza_id: UUID) -> tuple[DetallePieza, UUID]:
        """La Pieza no retirada y quien la subió, bloqueada hasta el `COMMIT`: el estado que
        decide un reproceso es el de la base, no uno leído antes (033).

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
        """
        fila = tx.execute(
            f"SELECT {_COLUMNAS_PIEZA} FROM acervo.pieza"
            " WHERE pieza_id = %s AND borrada_el IS NULL FOR UPDATE",
            (pieza_id,),
        ).fetchone()
        if fila is None:
            raise PiezaNoEncontrada(pieza_id)
        creada_por: UUID = fila["creada_por"]
        return _detalle(fila), creada_por

    def vigentes(
        self, tx: Tx, pieza_ids: Collection[UUID], *, lote_id: UUID | None = None
    ) -> set[UUID]:
        """De `pieza_ids`, las no retiradas y, con `lote_id`, solo las de ese Lote."""
        filas = tx.execute(
            "SELECT pieza_id FROM acervo.pieza WHERE pieza_id = ANY(%s) AND borrada_el IS NULL"
            " AND (%s::uuid IS NULL OR lote_id = %s)",
            (list(pieza_ids), lote_id, lote_id),
        ).fetchall()
        return {fila["pieza_id"] for fila in filas}

    def bloquear_pieza(self, tx: Tx, pieza_id: UUID, *, retirada: bool = False) -> DetallePieza:
        """La Pieza bloqueada hasta el `COMMIT`, para retirarla o, con `retirada`, para
        restaurarla (spec, módulo 18).

        Raises:
            PiezaNoEncontrada: Si no existe.
            TransicionInvalida: Si ya está retirada, o con `retirada` si no lo está (R-43 d).
        """
        fila = tx.execute(
            f"SELECT {_COLUMNAS_PIEZA}, borrada_el FROM acervo.pieza"
            " WHERE pieza_id = %s FOR UPDATE",
            (pieza_id,),
        ).fetchone()
        if fila is None:
            raise PiezaNoEncontrada(pieza_id)
        if (fila["borrada_el"] is not None) != retirada:
            raise TransicionInvalida(
                pieza_id,
                fila["estado"],
                "restaurada" if retirada else "retirada",
                detalle="La Pieza no está retirada." if retirada else "La Pieza ya está retirada.",
            )
        return _detalle(fila)

    def desmarcar_borrada(self, tx: Tx, pieza_id: UUID) -> datetime:
        """Quita la marca del retiro, con la Pieza ya bloqueada: reencolar es de `retiro`.
        Devuelve cuándo.

        Raises:
            PiezaDuplicada: Si otra restauración del mismo original ganó la carrera
                (`pieza_huella_unica`, DEC-138).
        """
        try:
            # Punto de guardado: si choca con `pieza_huella_unica`, se relee la que ganó.
            with tx.transaction():
                fila = tx.execute(
                    "UPDATE acervo.pieza SET borrada_el = NULL, borrada_por = NULL,"
                    " borrada_motivo = NULL WHERE pieza_id = %s AND borrada_el IS NOT NULL"
                    " RETURNING now() AS restaurada_el, huella_original",
                    (pieza_id,),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            huella = tx.execute(
                "SELECT huella_original FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
            ).fetchone()
            assert huella is not None
            self.exigir_huella_libre(tx, huella["huella_original"])
            raise
        assert fila is not None
        restaurada_el: datetime = fila["restaurada_el"]
        return restaurada_el

    def listar_retiradas(
        self, tx: Tx, *, despues_de: tuple[datetime, UUID] | None, limite: int
    ) -> list[FilaDeRetirada]:
        """Las retiradas, de la más reciente a la más antigua, después de `despues_de`
        (`borrada_el`, `pieza_id`), con quién la retiró y si su original ya es otra Pieza."""
        borrada_el, desde = despues_de or (None, None)
        filas = tx.execute(
            "SELECT pieza.pieza_id, pieza.nombre_archivo, pieza.estado, pieza.tipo,"
            " pieza.lote_id, pieza.borrada_el, pieza.borrada_motivo, u.nombre AS retirada_por,"
            " (pieza.via_ingreso = %s AND EXISTS (SELECT 1 FROM acervo.pieza o"
            "  WHERE o.huella_original = pieza.huella_original AND o.via_ingreso = %s"
            "  AND o.borrada_el IS NULL)) AS duplicada"
            " FROM acervo.pieza JOIN acervo.usuario u ON u.usuario_id = pieza.borrada_por"
            " WHERE pieza.borrada_el IS NOT NULL"
            " AND (%s::timestamptz IS NULL OR (pieza.borrada_el, pieza.pieza_id) < (%s, %s))"
            " ORDER BY pieza.borrada_el DESC, pieza.pieza_id DESC LIMIT %s",
            (
                ViaDeIngreso.ARCHIVO.value,
                ViaDeIngreso.ARCHIVO.value,
                borrada_el,
                borrada_el,
                desde,
                limite,
            ),
        ).fetchall()
        return [
            FilaDeRetirada(
                pieza_id=fila["pieza_id"],
                nombre_archivo=fila["nombre_archivo"],
                estado=Estado(fila["estado"]),
                tipo=fila["tipo"],
                lote_id=fila["lote_id"],
                borrada_el=fila["borrada_el"],
                retirada_por=fila["retirada_por"],
                motivo=fila["borrada_motivo"],
                duplicada=fila["duplicada"],
            )
            for fila in filas
        ]

    def marcar_borrada(self, tx: Tx, pieza_id: UUID, *, por: UUID, motivo: str) -> datetime:
        """Solo la marca del retiro (`borrada_*`), en la transacción de quien llama y con la
        Pieza ya bloqueada: desindexar y cancelar los trabajos es de `retiro`. Devuelve cuándo."""
        fila = tx.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s, borrada_motivo = %s"
            " WHERE pieza_id = %s AND borrada_el IS NULL RETURNING borrada_el",
            (por, motivo, pieza_id),
        ).fetchone()
        assert fila is not None
        borrada_el: datetime = fila["borrada_el"]
        return borrada_el

    def obtener_pieza(self, pieza_id: UUID) -> DetallePieza:
        """La Pieza no retirada: una retirada es un 404 en el detalle, la Ficha y el original
        (DEC-29)."""
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                f"SELECT {_COLUMNAS_PIEZA}, {_REQUIERE_ANALISIS_HUMANO} FROM acervo.pieza"
                " WHERE pieza_id = %s AND borrada_el IS NULL",
                (pieza_id,),
            ).fetchone()
        if fila is None:
            raise PiezaNoEncontrada(pieza_id)
        detalle = _detalle(fila)
        return replace(
            detalle,
            fila=replace(detalle.fila, requiere_analisis_humano=fila["requiere_analisis_humano"]),
        )

    def guardar_ficha_propuesta(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        resumen: str,
        cuerpo: str,
        configuracion_efectiva: ConfiguracionEfectiva,
        descripcion: DescripcionPropuesta | None = None,
        imagenes: Sequence[UbicacionDeImagen] = (),
        a_describir: int = 0,
    ) -> FichaVigente:
        """La v1 que propuso el procesamiento. `imagenes` son las embebidas sin texto (148): se
        guardan con la v1, numeradas desde 1, y las primeras `a_describir` quedan encoladas (sus
        trabajos los encola quien llama, en esta transacción)."""
        # La fila se bloquea antes de mirar el estado: el estado que decide la transición es
        # el de la base, no el que traía quien llamó.
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] != Estado.RECIBIDA.value:
            raise TransicionInvalida(pieza_id, actual["estado"], Estado.PENDIENTE.value)
        # Solo el resumen es del modelo: lo que ya traía la Pieza no se pisa (DEC-19), ni lo que
        # escribió una persona mientras se leía (152), y el Contexto de esa persona o, si no
        # escribió, el del Lote pasa a la sección Contexto de la v1 (DEC-113).
        heredado = _contexto_previo(actual)
        if heredado is not None:
            # Como en una suelta, lo que sigue a un `## Contexto` del texto ya es Contexto.
            partido = CuerpoDeFicha.partir(cuerpo)
            contexto = "\n\n".join(filter(None, (partido.contexto, heredado)))
            cuerpo = CuerpoDeFicha(partido.principal, contexto).armar()
        ficha = _insertar_version(
            tx,
            pieza_id,
            replace(_campos(actual), resumen=resumen, estado=Estado.PENDIENTE),
            cuerpo,
            version=1,
            origen_version="modelo",
            configuracion_efectiva=configuracion_efectiva,
            descripcion=descripcion,
        )
        tx.cursor().executemany(
            "INSERT INTO acervo.descripcion_de_imagen (ficha_id, imagen, numero, pagina, hoja,"
            " estado) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (
                    ficha.ficha_id,
                    imagen,
                    ubicacion.numero,
                    ubicacion.pagina,
                    ubicacion.hoja,
                    (
                        EstadoDeImagen.ENCOLADA
                        if imagen <= a_describir
                        else EstadoDeImagen.SIN_DESCRIBIR
                    ).value,
                )
                for imagen, ubicacion in enumerate(imagenes, start=1)
            ],
        )
        return ficha

    def completar_a_mano(self, tx: Tx, pieza_id: UUID) -> FichaVigente:
        """Pasa una `Recibida` a `Pendiente` con la Ficha v1 `sistema` vacía, la marca
        «Requiere análisis humano» (DEC-122), en la transacción de quien llama. Que su trabajo
        haya fallado y quién lo pide lo verifica quien llama: este módulo no conoce la cola.

        Raises:
            PiezaNoEncontrada: Si no existe.
            TransicionInvalida: Si no está `Recibida`.
        """
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] != Estado.RECIBIDA.value:
            raise TransicionInvalida(pieza_id, actual["estado"], Estado.PENDIENTE.value)
        return _insertar_v1_sistema(tx, actual)

    def completar_en_recibida(
        self, tx: Tx, *, pieza_id: UUID, duros: CamposDuros, del_lote: bool = False
    ) -> tuple[DetallePieza, list[str], bool]:
        """Escribe los campos duros y el Contexto de una Pieza Recibida mientras se lee (152).

        Con `del_lote` es la herencia tardía del Lote (151): solo llena lo que la Pieza tiene
        vacío (la Fecha, entera), lo suma a `campos_heredados` y no toca el Contexto.

        Nunca firma ni crea una versión: escribe en la fila de la Pieza, como la herencia del
        Lote, y anota en `campos_de_la_persona` cuáles cambió. La v1 los toma de la fila bajo el
        mismo `FOR UPDATE` (`guardar_ficha_propuesta`, `completar_a_mano`), así que de dos
        guardados cruzados ninguno pisa al otro: el segundo espera y, si ya hay v1, rebota.
        Valida como `guardar_edicion`.

        Returns:
            La Pieza, los campos duros que cambiaron y si cambió el Contexto.

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            TransicionInvalida: Si ya no está Recibida: la lectura terminó y se guarda la Ficha.
            TipoDesconocido, TipoDeActividadDesconocido, CarreraDesconocida: Como en
                `guardar_edicion`.
            ValueError: Si la Fecha es incoherente con su precisión.
        """
        inicio, fin = _rango_de(duros)
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s AND borrada_el IS NULL FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] != Estado.RECIBIDA.value:
            raise TransicionInvalida(
                pieza_id,
                actual["estado"],
                Estado.RECIBIDA.value,
                detalle=f"La lectura ya terminó: la Pieza está {actual['estado']}.",
            )
        if duros.tipo is not None:
            validar_tipo_de_pieza(tx, duros.tipo, conserva=actual["tipo"])
        if duros.tipo_actividad is not None:
            validar_tipo_actividad(tx, duros.tipo_actividad, conserva=actual["tipo_actividad"])
        validar_carreras(tx, duros.carreras, conserva=actual["carreras"])
        precision = duros.precision_fecha
        if del_lote:
            # Lo que la Pieza ya tiene no se pisa (DEC-19): ni lo heredado ni lo de una persona.
            duros = replace(
                duros,
                tipo=actual["tipo"],
                tipo_actividad=actual["tipo_actividad"],
                autor=actual["autor"],
                carreras=tuple(actual["carreras"]),
                origen=actual["origen"] or duros.origen,
                propietario=actual["propietario"] or duros.propietario,
                contexto=_contexto_previo(actual) or "",
            )
            if actual["fecha_inicio"] is not None:
                inicio, fin = actual["fecha_inicio"], actual["fecha_fin"]
                precision = PrecisionFecha(actual["precision_fecha"])
        nuevos: dict[str, Any] = {
            "tipo": duros.tipo,
            "tipo_actividad": duros.tipo_actividad,
            "fecha_inicio": inicio,
            "fecha_fin": fin,
            "precision_fecha": None if precision is None else precision.value,
            "origen": duros.origen,
            "propietario": duros.propietario,
            "autor": duros.autor,
            "carreras": sorted(duros.carreras),
        }
        if not del_lote:
            # Lo que el Lote copió tarde (151) y la persona nunca escribió no se borra con un vacío:
            # es lo que manda una pantalla abierta antes de la herencia, que no lo conocía.
            intactos = set(actual["campos_heredados"]) - set(actual["campos_de_la_persona"])
            for grupo in _GRUPOS_HEREDABLES:
                if set(grupo) <= intactos and not any(nuevos[campo] for campo in grupo):
                    nuevos |= {campo: actual[campo] for campo in grupo}
        escritos = [campo for campo in CAMPOS_DUROS if nuevos[campo] != actual[campo]]
        de_la_persona = set(actual["campos_de_la_persona"]) | set([] if del_lote else escritos)
        heredados = [*actual["campos_heredados"], *(escritos if del_lote else [])]
        contexto = duros.contexto.strip()
        cambio_contexto = contexto != (_contexto_previo(actual) or "")
        _escribir_carreras(tx, pieza_id, nuevos["carreras"])
        # Solo una Recibida: no tiene Ficha vigente de la que esta escritura pueda diverger.
        fila = tx.execute(
            "UPDATE acervo.pieza SET tipo = %(tipo)s, tipo_actividad = %(tipo_actividad)s,"
            " fecha_inicio = %(fecha_inicio)s, fecha_fin = %(fecha_fin)s,"
            " precision_fecha = %(precision_fecha)s, origen = %(origen)s,"
            " propietario = %(propietario)s, autor = %(autor)s,"
            " campos_de_la_persona = %(de_la_persona)s, campos_heredados = %(heredados)s,"
            " contexto_de_la_persona = CASE WHEN %(cambio)s THEN %(contexto)s"
            "  ELSE contexto_de_la_persona END, actualizada_el = now()"
            f" WHERE pieza_id = %(pieza_id)s AND estado = %(recibida)s RETURNING {_COLUMNAS_PIEZA}",
            nuevos
            | {
                "de_la_persona": [c for c in CAMPOS_DUROS if c in de_la_persona],
                "heredados": heredados,
                "cambio": cambio_contexto,
                "contexto": contexto,
                "pieza_id": pieza_id,
                "recibida": Estado.RECIBIDA.value,
            },
        ).fetchone()
        assert fila is not None
        return _detalle(fila), escritos, cambio_contexto

    def heredar_tarde(
        self,
        tx: Tx,
        *,
        lote_id: UUID,
        herencia: Herencia,
        piezas: Collection[UUID] | None = None,
    ) -> dict[UUID, list[str]]:
        """Copia lo que el Lote acaba de completar a sus Piezas que no lo tienen (151, historia
        74): a las Recibidas sobre la fila (`completar_en_recibida` con `del_lote`) y a las
        Pendientes con una versión `sistema` sobre la vigente, así quien la tenga abierta recibe
        el 409 de versión (RF-16). Incompletas, Validadas y retiradas no se tocan. Con `piezas`,
        solo esas: la restauración de una retirada recibe lo que el Lote completó mientras tanto.

        Returns:
            Por Pieza tocada, los campos que se le copiaron.
        """
        contexto = herencia.contexto
        rango = contexto.precision_fecha is PrecisionFecha.RANGO
        duros = CamposDuros(
            origen=contexto.origen,
            fecha_inicio=contexto.fecha_inicio,
            precision_fecha=contexto.precision_fecha,
            # Como en `EdicionFicha`: el fin lo trae solo el rango, el resto lo deriva.
            fecha_fin=contexto.fecha_fin if rango else None,
            propietario=herencia.propietario,
        )
        filas = tx.execute(
            "SELECT pieza_id, estado FROM acervo.pieza WHERE lote_id = %s AND borrada_el IS NULL"
            " AND estado = ANY(%s) AND (%s::uuid[] IS NULL OR pieza_id = ANY(%s))"
            " ORDER BY pieza_id FOR UPDATE",
            (
                lote_id,
                [Estado.RECIBIDA.value, Estado.PENDIENTE.value],
                None if piezas is None else list(piezas),
                None if piezas is None else list(piezas),
            ),
        ).fetchall()
        copiados: dict[UUID, list[str]] = {}
        for fila in filas:
            if fila["estado"] == Estado.RECIBIDA.value:
                _, escritos, _ = self.completar_en_recibida(
                    tx, pieza_id=fila["pieza_id"], duros=duros, del_lote=True
                )
            else:
                escritos = _heredar_en_pendiente(tx, fila["pieza_id"], duros)
            if escritos:
                copiados[fila["pieza_id"]] = escritos
        return copiados

    def leer_en_recibida(self, pieza_id: UUID) -> tuple[CamposFicha, str]:
        """Los campos y el Contexto que la pantalla partida edita mientras la Pieza se lee: lo
        heredado del Lote y lo que ya escribió una persona (152).

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            TransicionInvalida: Si ya no está Recibida: lo que se abre es su Ficha.
        """
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                f"{_SELECT_PIEZA} WHERE pieza_id = %s AND borrada_el IS NULL", (pieza_id,)
            ).fetchone()
        if fila is None:
            raise PiezaNoEncontrada(pieza_id)
        if fila["estado"] != Estado.RECIBIDA.value:
            raise TransicionInvalida(
                pieza_id,
                fila["estado"],
                Estado.RECIBIDA.value,
                detalle=f"La lectura ya terminó: la Pieza está {fila['estado']}.",
            )
        return _campos(fila), _contexto_previo(fila) or ""

    def obtener_ficha(self, pieza_id: UUID) -> FichaVigente:
        return self._leer_version(_SELECT_FICHA_VIGENTE, pieza_id)

    def obtener_propuesta(self, pieza_id: UUID) -> FichaVigente:
        return self._leer_version(_SELECT_PROPUESTA, pieza_id)

    def listar_por_estado(
        self,
        *,
        estados: Sequence[Estado] | None = None,
        lote_id: UUID | None = None,
        mias: UUID | None = None,
    ) -> tuple[ResumenPieza, ...]:
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                f"SELECT {_COLUMNAS_PIEZA}, {_REQUIERE_ANALISIS_HUMANO}"
                # Una Pieza retirada no aparece en ningún listado (DEC-29).
                " FROM acervo.pieza WHERE borrada_el IS NULL"
                " AND (%(estados)s::text[] IS NULL OR estado = ANY(%(estados)s))"
                " AND (%(lote)s::uuid IS NULL OR lote_id = %(lote)s)"
                # Un Aporte no tiene `creada_por`: nunca es de nadie (DEC-123).
                " AND (%(mias)s::uuid IS NULL OR creada_por = %(mias)s"
                "  OR (trabajada_por = %(mias)s AND estado = 'Incompleta'))"
                " ORDER BY actualizada_el DESC, creada_el DESC",
                {
                    "estados": None if estados is None else [e.value for e in estados],
                    "lote": lote_id,
                    "mias": mias,
                },
            ).fetchall()
        return tuple(
            replace(_resumen(fila), requiere_analisis_humano=fila["requiere_analisis_humano"])
            for fila in filas
        )

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
        # La Fecha se deriva antes de tocar la base: una Fecha incoherente no llega a
        # bloquear la Pieza.
        inicio, fin = _rango_de(edicion)
        # Una retirada no se edita: el retiro la bloqueó y, al soltarla, ya no está (DEC-29).
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s AND borrada_el IS NULL FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] == Estado.RECIBIDA.value:
            raise TransicionInvalida(pieza_id, actual["estado"], Estado.VALIDADA.value)
        # Con la Pieza bloqueada, la vigente no cambia hasta el COMMIT: de dos guardados
        # con la misma `version_base`, el segundo la ve ya cambiada (RF-16).
        vigente = tx.execute(_SELECT_FICHA_VIGENTE, (pieza_id,)).fetchone()
        assert vigente is not None
        if vigente["version"] != version_base:
            raise VersionDesactualizada(version_base, _ficha(vigente, pieza_id))
        if edicion.tipo is not None:
            validar_tipo_de_pieza(tx, edicion.tipo, conserva=actual["tipo"])
        if edicion.tipo_actividad is not None:
            validar_tipo_actividad(tx, edicion.tipo_actividad, conserva=actual["tipo_actividad"])
        validar_carreras(tx, edicion.carreras, conserva=actual["carreras"])
        # `archivo_original` es obligatorio pero no lo edita una persona: sale de la
        # fila. Los otros ocho, el Autor y las Carreras son lo que la pantalla mandó.
        editados = replace(
            _campos(actual),
            tipo=edicion.tipo,
            tipo_actividad=edicion.tipo_actividad,
            fecha_inicio=inicio,
            fecha_fin=fin,
            precision_fecha=edicion.precision_fecha,
            origen=edicion.origen,
            propietario=edicion.propietario,
            autor=edicion.autor,
            carreras=tuple(sorted(edicion.carreras)),
            resumen=edicion.resumen,
            # Sin `publicable` en la edición sigue el de la vigente, que trae `_campos` (125).
            publicable=actual["publicable"] if edicion.publicable is None else edicion.publicable,
        )
        # Un obligatorio que no fuera campo de la Ficha falla acá con AttributeError en
        # vez de guardar una Pieza `Validada` a la que le falta un dato.
        faltantes = [c for c in CAMPOS_OBLIGATORIOS if getattr(editados, c) is None]
        # La firma exige además cada Afirmación decidida, en el servidor (ADR-0007).
        afirmaciones = _afirmaciones(tx, pieza_id)
        if any(a.estado is EstadoAfirmacion.SIN_DECIDIR for a in afirmaciones):
            faltantes.append(FALTAN_AFIRMACIONES)
        # Y que no quede ninguna descripción en curso: sus Afirmaciones todavía no llegaron (148).
        if tx.execute(
            f"SELECT 1 FROM acervo.pieza WHERE pieza_id = %s AND {_DESCRIPCION_EN_CURSO}",
            (pieza_id,),
        ).fetchone():
            faltantes.append(FALTAN_DESCRIPCIONES)
        # Guardar incompleto siempre se permite: falta un dato, no hay un error (US 18).
        estado = Estado.INCOMPLETA if faltantes else Estado.VALIDADA
        principal = edicion.cuerpo
        if (
            afirmaciones
            and estado is Estado.VALIDADA
            and actual["estado"] != Estado.VALIDADA.value
            and not _con_afirmaciones_firmadas(_ficha(vigente, pieza_id).cuerpo)
        ):
            # Al firmar, las aceptadas pasan al cuerpo. Una vez escritas son texto de la persona:
            # reeditar una Validada, o volver a firmarla tras dejarla Incompleta, no las reescribe.
            principal = escribir_afirmaciones(principal, _aceptadas(afirmaciones))
        ficha = _insertar_version(
            tx,
            pieza_id,
            replace(
                editados,
                estado=estado,
                # Sin campos completos no hay firma, y reabrir una Validada para
                # guardarla Incompleta borra la que había: nadie queda firmando una
                # Ficha que ya no está completa (ADR-0002).
                validada_por=editada_por if estado is Estado.VALIDADA else None,
                modo_validacion="individual" if estado is Estado.VALIDADA else None,
            ),
            CuerpoDeFicha(principal, edicion.contexto).armar(),
            version=version_base + 1,
            origen_version="persona",
            editada_por=editada_por,
            metodo=metodo,
        )
        return ResultadoDeEdicion(
            ficha=ficha,
            estado_anterior=Estado(actual["estado"]),
            estado_nuevo=estado,
            campos_faltantes=faltantes,
        )

    def candidatas_en_bloque(
        self,
        tx: Tx,
        *,
        lote_id: UUID,
        clase: ClaseDeOriginal,
        piezas: Collection[UUID] | None = None,
    ) -> list[Candidata]:
        """Las Piezas del Lote de esa clase, en orden de `pieza_id`, para la regla del bloque.

        Sin `piezas` son las no retiradas (la previa). Con ellas son esas, retiradas incluidas, y
        quedan bloqueadas hasta el `COMMIT` (la confirmación): de dos confirmaciones sobre las
        mismas Piezas, la segunda lee lo que firmó la primera.
        """
        if piezas is not None:
            tx.execute(
                "SELECT 1 FROM acervo.pieza WHERE pieza_id = ANY(%s) ORDER BY pieza_id FOR UPDATE",
                (list(piezas),),
            )
        # Otra sentencia, con la foto de después del bloqueo: la vigente que dejó la otra firma.
        filas = tx.execute(
            f"SELECT {_COLUMNAS_PIEZA}, borrada_el IS NOT NULL AS borrada,"
            " (SELECT f.version FROM acervo.ficha f WHERE f.pieza_id = pieza.pieza_id"
            f" AND f.vigente) AS version_vigente, {_REQUIERE_ANALISIS_HUMANO},"
            f" EXISTS ({_AFIRMACIONES_DE} AND f.pieza_id = pieza.pieza_id"
            " AND a.estado = 'sin_decidir')"
            # Una descripción en curso trae Afirmaciones que nadie vio (148).
            f" OR {_DESCRIPCION_EN_CURSO} AS afirmaciones_sin_decidir"
            " FROM acervo.pieza WHERE lote_id = %(lote)s AND clase_original = %(clase)s"
            " AND (CASE WHEN %(piezas)s::uuid[] IS NULL THEN borrada_el IS NULL"
            " ELSE pieza_id = ANY(%(piezas)s) END)"
            " ORDER BY pieza_id",
            {
                "lote": lote_id,
                "clase": clase.value,
                "piezas": None if piezas is None else list(piezas),
            },
        ).fetchall()
        # Las de las imágenes embebidas, que llegan después de la v1 (148); las de la foto entera
        # ya entraron a su informe. Una sola consulta para todo el Lote.
        embebidas: dict[UUID, list[str]] = {}
        for afirmacion in tx.execute(
            "SELECT f.pieza_id, a.texto FROM acervo.afirmacion a JOIN acervo.ficha f"
            " ON f.ficha_id = a.ficha_id WHERE f.pieza_id = ANY(%s) AND a.imagen > 0"
            " AND a.estado <> 'rechazada'",
            ([fila["pieza_id"] for fila in filas],),
        ):
            embebidas.setdefault(afirmacion["pieza_id"], []).append(afirmacion["texto"])
        return [
            Candidata(
                pieza_id=fila["pieza_id"],
                estado=Estado(fila["estado"]),
                version=fila["version_vigente"],
                borrada=fila["borrada"],
                # La de la vigente, o la de una Afirmación que llegó después de ella (148): decidir
                # no crea versión, y el bloque no firma una Pieza con una orden aceptada sin ver.
                alerta_inyeccion=fila["alerta_inyeccion"]
                or alerta_de_afirmaciones(embebidas.get(fila["pieza_id"], ())),
                requiere_analisis_humano=fila["requiere_analisis_humano"],
                afirmaciones_sin_decidir=fila["afirmaciones_sin_decidir"],
                faltantes=tuple(
                    campo
                    for campo in CAMPOS_OBLIGATORIOS
                    if not str(fila[_COLUMNA_DE_CAMPO[campo]] or "").strip()
                ),
            )
            for fila in filas
        ]

    def firmar_en_bloque(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        tipo: str,
        tipo_actividad: str,
        contexto_adicional: str | None,
        firmada_por: UUID,
        metodo: MetodoAutenticacion,
        aprobacion_id: UUID,
    ) -> FichaVigente:
        """Firma una Pendiente sin abrirla (CU-07): versión nueva `persona` con el `tipo` y el
        `tipo_actividad` comunes, el contexto adicional al final de su Contexto y
        `modo_validacion = bloque`. Lo demás sigue como estaba: lo heredado del Lote y lo que
        propuso el modelo. Que entre en el bloque lo decide `validacion` con la regla de
        `dominio`; acá solo se protege la transición.

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            TransicionInvalida: Si no está `Pendiente` (DEC-146 punto 1).
        """
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s AND borrada_el IS NULL FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] != Estado.PENDIENTE.value:
            raise TransicionInvalida(pieza_id, actual["estado"], Estado.VALIDADA.value)
        fila = tx.execute(_SELECT_FICHA_VIGENTE, (pieza_id,)).fetchone()
        assert fila is not None
        vigente = _ficha(fila, pieza_id)
        partido = CuerpoDeFicha.partir(vigente.cuerpo)
        contexto = "\n\n".join(filter(None, (partido.contexto, contexto_adicional)))
        principal = partido.principal
        if afirmaciones := _afirmaciones(tx, pieza_id):
            # Que estén todas decididas lo exige la regla del bloque (`dominio.bloque`).
            principal = escribir_afirmaciones(principal, _aceptadas(afirmaciones))
        return _insertar_version(
            tx,
            pieza_id,
            replace(
                _campos(actual),
                tipo=tipo,
                tipo_actividad=tipo_actividad,
                estado=Estado.VALIDADA,
                validada_por=firmada_por,
                modo_validacion="bloque",
            ),
            CuerpoDeFicha(principal, contexto).armar(),
            version=vigente.version + 1,
            origen_version="persona",
            editada_por=firmada_por,
            metodo=metodo,
            aprobacion_id=aprobacion_id,
        )

    def leer_para_indexar(
        self, tx: Tx, pieza_id: UUID, *, bloquear: bool = False
    ) -> FichaIndexable | None:
        """La Ficha vigente si la Pieza es Validada y no está retirada; si no, `None`.

        Con `bloquear`, `FOR SHARE`: una edición de la Pieza espera al `COMMIT` de quien llama
        y la Ficha que se indexa no cambia antes de guardar sus fragmentos (DEC-146 punto 4).
        """
        # Se bloquea por `pieza_id` solo y la indexabilidad se decide después: con `estado` en el
        # WHERE, una edición sin confirmar de Incompleta a Validada no haría esperar.
        fila = tx.execute(
            "SELECT estado, borrada_el, tipo, tipo_actividad, fecha_inicio, fecha_fin,"
            f" precision_fecha, clase_original, via_ingreso, {_CARRERAS_VIGENTES}"
            " FROM acervo.pieza WHERE pieza_id = %s" + (" FOR SHARE" if bloquear else ""),
            (pieza_id,),
        ).fetchone()
        if fila is None or fila["estado"] != Estado.VALIDADA.value or fila["borrada_el"]:
            return None
        vigente = tx.execute(_SELECT_FICHA_VIGENTE, (pieza_id,)).fetchone()
        assert vigente is not None
        ficha = _ficha(vigente, pieza_id)
        return FichaIndexable(
            pieza_id=pieza_id,
            ficha_id=vigente["ficha_id"],
            tipo=fila["tipo"],
            tipo_actividad=fila["tipo_actividad"],
            carreras=tuple(fila["carreras"]),
            fecha_inicio=fila["fecha_inicio"],
            fecha_fin=fila["fecha_fin"],
            precision_fecha=PrecisionFecha(fila["precision_fecha"]),
            clase_original=ClaseDeOriginal(fila["clase_original"]),
            via_ingreso=ViaDeIngreso(fila["via_ingreso"]),
            cuerpo=ficha.cuerpo,
            resumen=ficha.campos.resumen or "",
        )

    def ids_indexables(self, tx: Tx, *, entre: Collection[UUID] | None = None) -> list[UUID]:
        """Las Validadas no retiradas, o solo las de `entre`: lo que `reindexar` vuelve a
        indexar."""
        filas = tx.execute(
            "SELECT pieza_id FROM acervo.pieza WHERE estado = %(estado)s AND borrada_el IS NULL"
            " AND (%(entre)s::uuid[] IS NULL OR pieza_id = ANY(%(entre)s)) ORDER BY pieza_id",
            {"estado": Estado.VALIDADA.value, "entre": None if entre is None else list(entre)},
        ).fetchall()
        return [fila["pieza_id"] for fila in filas]

    def validadas_del_periodo(
        self, tx: Tx, desde: date, hasta: date, *, carreras: Sequence[str] = ()
    ) -> list[PiezaDelPeriodo]:
        """Las Validadas no retiradas cuya Fecha solapa `[desde, hasta]`, inclusivo, con su
        Ficha vigente; con `carreras`, las que tienen alguna de ellas. Las lee `informes` (R-21).
        """
        filas = tx.execute(
            "SELECT pieza.pieza_id, f.ficha_id, pieza.tipo_actividad, pieza.fecha_inicio,"
            f" pieza.fecha_fin {_DESDE_BUSCABLE}"
            f" WHERE pieza.estado = %s AND {_DEL_PERIODO} ORDER BY pieza.pieza_id",
            (Estado.VALIDADA.value, *_parametros_del_periodo(desde, hasta, carreras)),
        ).fetchall()
        return [PiezaDelPeriodo(**fila) for fila in filas]

    def faltantes_del_periodo(
        self, tx: Tx, desde: date, hasta: date, *, carreras: Sequence[str] = ()
    ) -> list[DetallePieza]:
        """Las Recibidas, Pendientes e Incompletas no retiradas cuya Fecha, declarada o heredada
        del Lote, solapa el período, con lo que les falta para validarse (R-21). Una Fecha sin
        fin cuenta como su inicio. Por nombre de archivo."""
        filas = tx.execute(
            f"{_SELECT_PIEZA} WHERE estado <> %s AND {_DEL_PERIODO}"
            " ORDER BY nombre_archivo, pieza_id",
            (Estado.VALIDADA.value, *_parametros_del_periodo(desde, hasta, carreras)),
        ).fetchall()
        return [_detalle(fila) for fila in filas]

    def cambios_desde(
        self, tx: Tx, congeladas: Mapping[UUID, UUID]
    ) -> list[CambioPosterior]:
        """De las Piezas congeladas (`pieza_id` → `ficha_id`), las que se retiraron, dejaron de
        estar Validadas o tienen otra Ficha vigente (R-22). Por nombre de archivo."""
        filas = tx.execute(
            "SELECT pieza.pieza_id, pieza.nombre_archivo, pieza.borrada_el IS NOT NULL"
            f" AS retirada, {_VALIDADA_SIN_RETIRAR} AS validada, f.ficha_id FROM acervo.pieza"
            " LEFT JOIN acervo.ficha f ON f.pieza_id = pieza.pieza_id AND f.vigente"
            " WHERE pieza.pieza_id = ANY(%s) ORDER BY pieza.nombre_archivo, pieza.pieza_id",
            (list(congeladas),),
        ).fetchall()
        return [
            CambioPosterior(
                pieza_id=fila["pieza_id"], nombre_archivo=fila["nombre_archivo"],
                retirada=fila["retirada"],
            )
            for fila in filas
            if not fila["validada"] or fila["ficha_id"] != congeladas[fila["pieza_id"]]
        ]

    def medibles(self, tx: Tx, pieza_ids: Collection[UUID]) -> dict[UUID, PiezaMedible]:
        """Cada Pieza pedida que existe, con su Ficha vigente y su huella: lo que `banco` compara
        con el índice para M2, también si dejó de ser Validada. Por `pieza_id`."""
        filas = tx.execute(
            f"SELECT pieza.pieza_id, {_VALIDADA_SIN_RETIRAR} AS validada, pieza.huella_original,"
            " f.ficha_id, f.version, f.origen_version, f.contenido_markdown, f.editada_por,"
            " f.editada_el, f.metodo_autenticacion, f.informe_inyeccion"
            " FROM acervo.pieza LEFT JOIN acervo.ficha f"
            " ON f.pieza_id = pieza.pieza_id AND f.vigente WHERE pieza.pieza_id = ANY(%s)",
            (list(pieza_ids),),
        ).fetchall()
        return {
            fila["pieza_id"]: PiezaMedible(
                pieza_id=fila["pieza_id"],
                validada=fila["validada"],
                huella=fila["huella_original"],
                ficha=None if fila["ficha_id"] is None else _ficha(fila, fila["pieza_id"]),
            )
            for fila in filas
        }

    def leer_versiones(self, tx: Tx, ficha_ids: Collection[UUID]) -> dict[UUID, FichaVigente]:
        """Cada versión de Ficha pedida, tal como se guardó: son inmutables, así que lo que un
        informe congeló se relee igual (R-22). Por `ficha_id`."""
        filas = tx.execute(
            "SELECT ficha_id, pieza_id, version, origen_version, contenido_markdown, editada_por,"
            " editada_el, metodo_autenticacion, informe_inyeccion FROM acervo.ficha"
            " WHERE ficha_id = ANY(%s)",
            (list(ficha_ids),),
        ).fetchall()
        return {fila["ficha_id"]: _ficha(fila, fila["pieza_id"]) for fila in filas}

    def buscables(
        self, pares: Collection[tuple[UUID, UUID]], *, solo_publicables: bool = False
    ) -> dict[UUID, PiezaBuscable]:
        """De los pares `(pieza_id, ficha_id)` que trae el índice, los de una Validada no
        retirada cuya Ficha vigente es esa: un residuo del índice se descarta acá (RF-15, RF-24,
        DEC-72). `solo_publicables` es para `/v1`: las personas ven también las no publicables
        (DEC-143). Por `pieza_id`."""
        if not pares:
            return {}
        piezas, fichas = zip(*pares, strict=True)
        with self._bd.transaccion() as tx:
            filas = tx.execute(
                f"{_SELECT_BUSCABLE} {_DESDE_BUSCABLE}"
                " WHERE pieza.estado = %s AND pieza.borrada_el IS NULL"
                + (" AND pieza.publicable" if solo_publicables else "")
                + " AND (pieza.pieza_id, f.ficha_id) IN"
                " (SELECT * FROM unnest(%s::uuid[], %s::uuid[]))",
                (Estado.VALIDADA.value, list(piezas), list(fichas)),
            ).fetchall()
        return {fila["pieza_id"]: PiezaBuscable(**_buscable(fila)) for fila in filas}

    def validadas_con_fecha(
        self, tx: Tx, *, mes: int, dia: int, limite: int
    ) -> list[FuenteDeFecha]:
        """Las Validadas publicables no retiradas cuya Fecha es de ese día y mes, en cualquier
        año: las candidatas de una efeméride de día (DEC-130). Una Fecha de solo mes no tiene
        día y no entra. Orden fijo, `fecha_inicio` y `pieza_id`, y hasta `limite`."""
        filas = tx.execute(
            f"{_SELECT_FUENTE} WHERE {_CANDIDATA} AND pieza.precision_fecha = 'dia'"
            " AND extract(month FROM pieza.fecha_inicio) = %s"
            " AND extract(day FROM pieza.fecha_inicio) = %s"
            " ORDER BY pieza.fecha_inicio, pieza.pieza_id LIMIT %s",
            (mes, dia, limite),
        ).fetchall()
        return [_fuente(fila) for fila in filas]

    def validadas_del_mes(self, tx: Tx, *, mes: int, limite: int) -> list[FuenteDeFecha]:
        """Las Validadas publicables no retiradas cuya Fecha tiene ese mes conocido (precisión de
        día o de mes), en cualquier año: las candidatas de una efeméride de mes (DEC-130,
        historia 260).
        Primero las de solo mes, que ninguna efeméride de día cubre; después por
        `fecha_inicio` y `pieza_id`, hasta `limite`."""
        filas = tx.execute(
            f"{_SELECT_FUENTE} WHERE {_CANDIDATA}"
            " AND pieza.precision_fecha IN ('dia', 'mes')"
            " AND extract(month FROM pieza.fecha_inicio) = %s"
            " ORDER BY pieza.precision_fecha = 'dia', pieza.fecha_inicio, pieza.pieza_id LIMIT %s",
            (mes, limite),
        ).fetchall()
        return [_fuente(fila) for fila in filas]

    def fuentes(
        self, tx: Tx, pieza_ids: Collection[UUID], *, bloquear: bool = False
    ) -> dict[UUID, FuenteDeFecha]:
        """Las Piezas de `pieza_ids` con su Ficha vigente, en cualquier estado, por `pieza_id`.
        Con `bloquear`, `FOR SHARE` de la Pieza: una edición espera al `COMMIT` de quien llama
        y lo que se guarda no cita una Ficha que ya cambió."""
        filas = tx.execute(
            f"{_SELECT_FUENTE} WHERE pieza.pieza_id = ANY(%s)"
            + (" FOR SHARE OF pieza" if bloquear else ""),
            (list(pieza_ids),),
        ).fetchall()
        return {fila["pieza_id"]: _fuente(fila) for fila in filas}

    def disponibles(self, pieza_ids: Collection[UUID]) -> dict[UUID, PiezaBuscable]:
        """De `pieza_ids`, las Validadas no retiradas, con su Ficha vigente: lo que una
        Conversación reabierta todavía muestra de sus fuentes (E8). Por `pieza_id`."""
        if not pieza_ids:
            return {}
        with self._bd.transaccion() as tx:
            filas = tx.execute(
                f"{_SELECT_BUSCABLE} {_DESDE_BUSCABLE}"
                " WHERE pieza.estado = %s AND pieza.borrada_el IS NULL"
                " AND pieza.pieza_id = ANY(%s)",
                (Estado.VALIDADA.value, list(pieza_ids)),
            ).fetchall()
        return {fila["pieza_id"]: PiezaBuscable(**_buscable(fila)) for fila in filas}

    def publicada(self, pieza_id: UUID) -> PiezaPublicada | None:
        """La Pieza que `/v1/piezas/{id}` publica: Validada, no retirada, con su Ficha vigente
        y quien la firmó. Inexistente, Recibida, Pendiente, Incompleta, retirada o no publicable
        dan el mismo `None`: `/v1` no distingue por qué no está (contrato, `V1NoEncontrada`)."""
        with self._bd.transaccion() as tx:
            fila = tx.execute(
                f"{_SELECT_BUSCABLE}, pieza.origen, pieza.lote_id, pieza.modo_validacion,"
                " f.contenido_markdown, split_part(u.email, '@', 1) AS firmante"
                f" {_DESDE_BUSCABLE}"
                " JOIN acervo.usuario u ON u.usuario_id = pieza.validada_por"
                " WHERE pieza.estado = %s AND pieza.borrada_el IS NULL AND pieza.publicable"
                " AND pieza.pieza_id = %s",
                (Estado.VALIDADA.value, pieza_id),
            ).fetchone()
        if fila is None:
            return None
        return PiezaPublicada(
            **_buscable(fila),
            origen=fila["origen"],
            lote_id=fila["lote_id"],
            validada_por=fila["firmante"],
            modo_validacion=fila["modo_validacion"],
            cuerpo=analizar(fila["contenido_markdown"])[1],
        )

    def obtener_original(self, pieza_id: UUID) -> OriginalGuardado:
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                f"{_SELECT_ORIGINAL} WHERE pieza_id = %s", (pieza_id,)
            ).fetchone()
        if fila is None or fila["archivo_original_id"] is None:
            raise PiezaNoEncontrada(pieza_id)
        return _original(fila)

    def originales_guardados(self) -> list[OriginalGuardado]:
        """Los originales que tienen Pieza, con su huella: lo que `verificar-originales` busca
        en disco (spec §2.3)."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                f"{_SELECT_ORIGINAL} WHERE archivo_original_id IS NOT NULL"
                " ORDER BY archivo_original_id"
            ).fetchall()
        return [_original(fila) for fila in filas]

    def originales_registrados(self) -> set[str]:
        """Los identificadores de original que tienen Pieza: lo que el barrido no toca."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                "SELECT archivo_original_id FROM acervo.pieza"
                " WHERE archivo_original_id IS NOT NULL"
            ).fetchall()
        return {fila["archivo_original_id"] for fila in filas}

    def piezas_con_original(self) -> list[UUID]:
        """Las Piezas que tienen original, borradas incluidas: lo que la réplica a Drive copia
        (DRV-12, ticket 124)."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                "SELECT pieza_id FROM acervo.pieza WHERE archivo_original_id IS NOT NULL"
            ).fetchall()
        return [fila["pieza_id"] for fila in filas]

    def leer_afirmaciones(
        self, pieza_id: UUID
    ) -> tuple[DescripcionPropuesta | None, list[Afirmacion]]:
        """Lo que propuso `describir_imagen` para la v1 (la clase, o `tipo_imagen = None` si
        falló el esquema) y sus Afirmaciones con su decisión. `(None, [])` si la Pieza no tiene:
        no es una foto, todavía está Recibida o su Ficha es anterior a 144."""
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                "SELECT d.tipo_imagen FROM acervo.descripcion_de_imagen d"
                " JOIN acervo.ficha f ON f.ficha_id = d.ficha_id"
                " WHERE f.pieza_id = %s AND d.imagen = 0",
                (pieza_id,),
            ).fetchone()
            afirmaciones = _afirmaciones(conexion, pieza_id)
        if fila is None:
            return None, afirmaciones
        textos = tuple(a.texto for a in afirmaciones if a.imagen == 0)
        return DescripcionPropuesta(fila["tipo_imagen"], textos), afirmaciones

    def leer_imagenes(self, pieza_id: UUID) -> list[ImagenDescribible]:
        """Las imágenes embebidas sin texto de la v1, en orden (148), con su ubicación, dónde está
        su descripción y si sus Afirmaciones no rechazadas encienden la Alerta de inyección
        (ADR-0003, `alerta_de_afirmaciones`). El informe de la Ficha lo rehace el próximo guardado,
        que revisa todas las Afirmaciones no rechazadas (`_insertar_version`)."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                "SELECT d.imagen, d.numero, d.pagina, d.hoja, d.estado, d.tipo_imagen"
                " FROM acervo.descripcion_de_imagen d JOIN acervo.ficha f"
                " ON f.ficha_id = d.ficha_id WHERE f.pieza_id = %s AND d.imagen > 0"
                " ORDER BY d.imagen",
                (pieza_id,),
            ).fetchall()
            afirmaciones = _afirmaciones(conexion, pieza_id)
        return [
            ImagenDescribible(
                imagen=fila["imagen"],
                ubicacion=UbicacionDeImagen(fila["numero"], fila["pagina"], fila["hoja"]),
                estado=EstadoDeImagen(fila["estado"]),
                tipo_imagen=fila["tipo_imagen"],
                alerta_inyeccion=alerta_de_afirmaciones(
                    a.texto
                    for a in afirmaciones
                    if a.imagen == fila["imagen"] and a.estado is not EstadoAfirmacion.RECHAZADA
                ),
            )
            for fila in filas
        ]

    def ubicar_imagen(self, pieza_id: UUID, imagen: int) -> UbicacionDeImagen:
        """Dónde está en el original la imagen embebida `imagen` de la Pieza (148).

        Raises:
            ImagenNoEncontrada: Si la Pieza no la tiene.
        """
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                "SELECT d.numero, d.pagina, d.hoja FROM acervo.descripcion_de_imagen d"
                " JOIN acervo.ficha f ON f.ficha_id = d.ficha_id"
                " WHERE f.pieza_id = %s AND d.imagen = %s AND d.imagen > 0",
                (pieza_id, imagen),
            ).fetchone()
        if fila is None:
            raise ImagenNoEncontrada(pieza_id, imagen)
        return UbicacionDeImagen(fila["numero"], fila["pagina"], fila["hoja"])

    def encolar_imagen(self, tx: Tx, *, pieza_id: UUID, imagen: int) -> None:
        """Marca encolada la descripción de una imagen embebida (148), en la transacción en la
        que quien llama encola su trabajo. La Pieza queda bloqueada, como al firmar: una firma
        que corre a la vez ve la descripción en curso o no la ve.

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            TransicionInvalida: Si no está Pendiente ni Incompleta, si sus Afirmaciones ya se
                firmaron (las nuevas no entrarían al cuerpo) o si la imagen ya está descrita.
            ImagenNoEncontrada: Si la Pieza no tiene esa imagen.
        """
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s AND borrada_el IS NULL FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] not in (Estado.PENDIENTE.value, Estado.INCOMPLETA.value):
            raise TransicionInvalida(
                pieza_id,
                actual["estado"],
                actual["estado"],
                detalle=f"La Pieza está {actual['estado']}: sus imágenes se describen antes de"
                " firmar.",
            )
        vigente = tx.execute(_SELECT_FICHA_VIGENTE, (pieza_id,)).fetchone()
        assert vigente is not None
        if _con_afirmaciones_firmadas(_ficha(vigente, pieza_id).cuerpo):
            raise TransicionInvalida(
                pieza_id,
                actual["estado"],
                actual["estado"],
                detalle="Las Afirmaciones ya se firmaron y están en el cuerpo: describí la imagen"
                " ahí.",
            )
        fila = tx.execute(
            "SELECT d.estado FROM acervo.descripcion_de_imagen d JOIN acervo.ficha f"
            " ON f.ficha_id = d.ficha_id WHERE f.pieza_id = %s AND d.imagen = %s AND d.imagen > 0",
            (pieza_id, imagen),
        ).fetchone()
        if fila is None:
            raise ImagenNoEncontrada(pieza_id, imagen)
        if fila["estado"] == EstadoDeImagen.DESCRITA.value:
            raise TransicionInvalida(
                pieza_id,
                actual["estado"],
                actual["estado"],
                detalle=f"La imagen {imagen} ya está descrita.",
            )
        # Una ya encolada sigue igual: si su trabajo sigue activo, el índice único de la cola
        # rechaza el segundo; si falló, el nuevo lo reemplaza.
        tx.execute(
            "UPDATE acervo.descripcion_de_imagen d SET estado = 'encolada' FROM acervo.ficha f"
            " WHERE f.ficha_id = d.ficha_id AND f.pieza_id = %s AND d.imagen = %s",
            (pieza_id, imagen),
        )

    def guardar_descripcion(
        self,
        tx: Tx,
        *,
        pieza_id: UUID,
        imagen: int,
        descripcion: DescripcionPropuesta,
    ) -> None:
        """Guarda lo que propuso `describir_imagen` para una imagen embebida (148): la clase y sus
        Afirmaciones sin decidir, numeradas después de las que ya tiene la Ficha, o la falla
        (`tipo_imagen = None`, sin Afirmaciones). No crea versión: una persona puede estar
        editando la Ficha, y las aceptadas entran al cuerpo al firmar (ADR-0007).

        Raises:
            ImagenNoEncontrada: Si la Pieza no tiene esa imagen.
        """
        # Como al decidir: una firma que corre a la vez ve las Afirmaciones o no las ve enteras.
        tx.execute(f"{_SELECT_PIEZA} WHERE pieza_id = %s FOR UPDATE", (pieza_id,))
        fila = tx.execute(
            "UPDATE acervo.descripcion_de_imagen d SET estado = 'descrita', tipo_imagen = %s"
            " FROM acervo.ficha f WHERE f.ficha_id = d.ficha_id AND f.pieza_id = %s"
            " AND d.imagen = %s AND d.imagen > 0"
            " RETURNING d.ficha_id, (SELECT coalesce(max(a.orden), 0) FROM acervo.afirmacion a"
            " WHERE a.ficha_id = d.ficha_id) AS ultima",
            (descripcion.tipo_imagen, pieza_id, imagen),
        ).fetchone()
        if fila is None:
            raise ImagenNoEncontrada(pieza_id, imagen)
        tx.cursor().executemany(
            "INSERT INTO acervo.afirmacion (ficha_id, imagen, orden, texto)"
            " VALUES (%s, %s, %s, %s)",
            [
                (fila["ficha_id"], imagen, fila["ultima"] + orden, texto)
                for orden, texto in enumerate(descripcion.afirmaciones, start=1)
            ],
        )

    def decidir_afirmacion(
        self, tx: Tx, *, pieza_id: UUID, orden: int, estado: EstadoAfirmacion, por: UUID
    ) -> tuple[Afirmacion, Afirmacion]:
        """Acepta o rechaza una Afirmación a nombre de `por` (ADR-0007), en la transacción de
        quien llama. Se puede cambiar de idea hasta firmar. Devuelve cómo estaba y cómo quedó.

        Raises:
            PiezaNoEncontrada: Si no existe o está retirada.
            TransicionInvalida: Si no está Pendiente ni Incompleta: una Validada ya tiene las
                aceptadas en el cuerpo, y cambiar una decisión ahí no cambiaría lo indexado.
            AfirmacionNoEncontrada: Si la Pieza no tiene esa Afirmación.
        """
        if estado is EstadoAfirmacion.SIN_DECIDIR:
            raise ValueError("Una decisión acepta o rechaza.")
        # Bloqueada como al guardar: una firma que corre a la vez ve la decisión o no la ve entera.
        actual = tx.execute(
            f"{_SELECT_PIEZA} WHERE pieza_id = %s AND borrada_el IS NULL FOR UPDATE", (pieza_id,)
        ).fetchone()
        if actual is None:
            raise PiezaNoEncontrada(pieza_id)
        if actual["estado"] not in (Estado.PENDIENTE.value, Estado.INCOMPLETA.value):
            raise TransicionInvalida(
                pieza_id,
                actual["estado"],
                actual["estado"],
                detalle=f"La Pieza está {actual['estado']}: sus Afirmaciones se deciden antes de"
                " firmar.",
            )
        vigente = tx.execute(_SELECT_FICHA_VIGENTE, (pieza_id,)).fetchone()
        assert vigente is not None
        if _con_afirmaciones_firmadas(_ficha(vigente, pieza_id).cuerpo):
            # Una firma anterior ya las escribió en el cuerpo: cambiarlas es editar el cuerpo.
            raise TransicionInvalida(
                pieza_id,
                actual["estado"],
                actual["estado"],
                detalle="Las Afirmaciones ya se firmaron y están en el cuerpo: corregilas ahí.",
            )
        antes = next((a for a in _afirmaciones(tx, pieza_id) if a.orden == orden), None)
        if antes is None:
            raise AfirmacionNoEncontrada(pieza_id, orden)
        fila = tx.execute(
            "UPDATE acervo.afirmacion a SET estado = %s, decidida_por = %s, decidida_el = now()"
            " FROM acervo.ficha f WHERE f.ficha_id = a.ficha_id AND f.pieza_id = %s"
            " AND a.orden = %s"
            " RETURNING a.estado, a.decidida_por, a.decidida_el",
            (estado.value, por, pieza_id, orden),
        ).fetchone()
        assert fila is not None
        return antes, replace(
            antes,
            estado=EstadoAfirmacion(fila["estado"]),
            decidida_por=fila["decidida_por"],
            decidida_el=fila["decidida_el"],
        )

    def para_rehacer_resumen(self, pieza_id: UUID) -> ParaRehacerResumen:
        """La clase, las Afirmaciones no rechazadas y el texto visible de la v1 de una foto,
        para rehacer su resumen (146). El texto visible es el de la v1: lo leyó el OCR y ninguna
        edición de la persona lo cambia como dato de entrada.

        Raises:
            PiezaNoEncontrada: Si la Pieza no existe, está retirada o no tiene Afirmaciones.
        """
        with self._bd.transaccion() as conexion:
            # Una decisión que se está guardando bloquea la Pieza (`decidir_afirmacion`): se
            # espera a que termine, así no se rehace con las decisiones de antes.
            conexion.execute(
                "SELECT 1 FROM acervo.pieza WHERE pieza_id = %s FOR SHARE", (pieza_id,)
            )
            fila = conexion.execute(
                "SELECT d.tipo_imagen, f.contenido_markdown FROM acervo.descripcion_de_imagen d"
                " JOIN acervo.ficha f ON f.ficha_id = d.ficha_id"
                " JOIN acervo.pieza p ON p.pieza_id = f.pieza_id"
                " WHERE f.pieza_id = %s AND p.borrada_el IS NULL AND d.tipo_imagen IS NOT NULL"
                " AND d.imagen = 0",
                (pieza_id,),
            ).fetchone()
            afirmaciones = _afirmaciones(conexion, pieza_id)
        if fila is None:
            raise PiezaNoEncontrada(pieza_id)
        _, cuerpo = analizar(fila["contenido_markdown"])
        _, texto_visible = CuerpoDeFicha.partir(cuerpo).partir_texto_visible()
        return ParaRehacerResumen(
            tipo_imagen=fila["tipo_imagen"],
            no_rechazadas=tuple(
                a
                for a in afirmaciones
                if a.estado is not EstadoAfirmacion.RECHAZADA and a.imagen == 0
            ),
            texto_visible=texto_visible,
        )

    def guardar_resumen_rehecho(
        self, tx: Tx, *, pieza_id: UUID, texto: str, ordenes: Collection[int]
    ) -> bool:
        """Guarda el resumen rehecho con las Afirmaciones `ordenes` como la propuesta de la Pieza,
        sin crear versión (146), con su informe de inyección (ADR-0003, capa 2). Si las no
        rechazadas ya no son esas, lo descarta y devuelve `False`: otro rechazo encoló uno nuevo
        y el último gana."""
        # Bloqueada como al decidir: o ve la decisión nueva entera o no la ve.
        tx.execute("SELECT 1 FROM acervo.pieza WHERE pieza_id = %s FOR UPDATE", (pieza_id,))
        vigentes = {
            a.orden
            for a in _afirmaciones(tx, pieza_id)
            if a.estado is not EstadoAfirmacion.RECHAZADA and a.imagen == 0
        }
        if vigentes != set(ordenes):
            return False
        informe = revisar({"resumen": texto})
        tx.execute(
            "INSERT INTO acervo.resumen_rehecho (ficha_id, texto, informe_inyeccion)"
            " SELECT ficha_id, %s, %s FROM acervo.ficha WHERE pieza_id = %s AND version = 1",
            (texto, Jsonb(informe.a_json()), pieza_id),
        )
        return True

    def resumenes_rehechos(self, pieza_id: UUID) -> list[ResumenRehecho]:
        """Los resúmenes rehechos de la Pieza, del primero al último: el último es la propuesta y
        cualquiera, guardado sin cambios, lo propuso un modelo (146)."""
        with self._bd.transaccion() as conexion:
            filas = conexion.execute(
                "SELECT r.texto, r.rehecho_el, r.informe_inyeccion FROM acervo.resumen_rehecho r"
                " JOIN acervo.ficha f ON f.ficha_id = r.ficha_id WHERE f.pieza_id = %s"
                " ORDER BY r.rehecho_el",
                (pieza_id,),
            ).fetchall()
        return [ResumenRehecho(**fila) for fila in filas]

    def _leer_version(self, consulta: str, pieza_id: UUID) -> FichaVigente:
        """Lee una versión de la Ficha de una Pieza no retirada y la reconstruye desde su
        markdown (ADR-0001). El trabajador no pasa por acá: lee con `leer_para_indexar`."""
        with self._bd.transaccion() as conexion:
            fila = conexion.execute(
                f"{consulta} AND pieza_id IN"
                " (SELECT pieza_id FROM acervo.pieza WHERE borrada_el IS NULL)",
                (pieza_id,),
            ).fetchone()
        # Una Pieza que no existe y una que todavía está `Recibida` son el mismo 404: en las
        # dos no hay Ficha que abrir (contrato de `GET /api/piezas/{id}/ficha`).
        if fila is None:
            raise PiezaNoEncontrada(pieza_id)
        return _ficha(fila, pieza_id)


def etiquetas_de_control(secciones: Mapping[str, str]) -> tuple[EtiquetaHallada, ...]:
    """Las etiquetas de control de la lista cerrada en las secciones de un Aporte, antes de
    guardar nada (DEC-133). `composicion` se la pasa a `ingesta`, que no importa
    `chequeo_inyeccion`; el informe de la alerta lo sigue haciendo `_insertar_version`."""
    try:
        revisar(secciones, modo=Modo.RECHAZAR)
    except EtiquetaDeControl as rechazo:
        return tuple(
            EtiquetaHallada(ETIQUETA_DEL_PATRON[h.patron_id], h.seccion, h.linea)
            for h in rechazo.hallazgos
        )
    return ()


def alerta_de_afirmaciones(textos: Iterable[str]) -> bool:
    """Si estas Afirmaciones encienden la Alerta de inyección, con el catálogo entero (ADR-0003).
    Las de una imagen embebida llegan después de la v1 y sin versión nueva (148): su alerta no
    puede esperar al informe del próximo guardado para verse en la pantalla ni en el bloque."""
    return revisar({"afirmaciones": "\n".join(textos)}).alerta


def _insertar_version(
    tx: Tx,
    pieza_id: UUID,
    campos: CamposFicha,
    cuerpo: str,
    *,
    version: int,
    origen_version: OrigenVersion,
    editada_por: UUID | None = None,
    metodo: MetodoAutenticacion | None = None,
    configuracion_efectiva: ConfiguracionEfectiva | None = None,
    aprobacion_id: UUID | None = None,
    descripcion: DescripcionPropuesta | None = None,
) -> FichaVigente:
    """La única que escribe una versión de Ficha (DEC-26, DEC-143; `test_arquitectura`).

    En la transacción de quien llama, con la Pieza ya bloqueada: escribe en `pieza` las
    columnas proyectadas de `campos`, arma el frontmatter con la fila ya escrita y publica esa
    versión como la vigente. Así `pieza` es siempre la proyección de la Ficha vigente
    (ADR-0001 enmendado). `id`, `archivo_original` y `validada_el` salen de la fila.

    Toda versión pasa por `chequeo_inyeccion` acá: su informe se guarda con la versión y la
    alerta se proyecta en `pieza` (DEC-26). Avisa, no cambia el estado. También las
    Afirmaciones que nadie rechazó: el modelo puede copiar a una la orden escrita en la imagen
    (ADR-0007). `descripcion` es la de la v1 de una foto, que se guarda con ella (144).
    """
    partes = CuerpoDeFicha.partir(cuerpo)
    principal, texto_visible = partes.partir_texto_visible()
    afirmaciones = (
        [a.texto for a in _afirmaciones(tx, pieza_id) if a.estado is not EstadoAfirmacion.RECHAZADA]
        if descripcion is None
        else list(descripcion.afirmaciones)
    )
    informe = revisar(
        {
            "cuerpo": principal,
            "texto_visible": texto_visible,
            "contexto": partes.contexto,
            "resumen": campos.resumen or "",
            "origen": campos.origen or "",
            "afirmaciones": "\n".join(afirmaciones),
        }
    )
    # Las Carreras primero: el `RETURNING` de `pieza` ya lee las vigentes.
    _escribir_carreras(tx, pieza_id, campos.carreras)
    precision = campos.precision_fecha
    fila = tx.execute(
        "UPDATE acervo.pieza SET estado = %(estado)s, tipo = %(tipo)s,"
        " tipo_actividad = %(tipo_actividad)s, propietario = %(propietario)s,"
        " autor = %(autor)s,"
        " fecha_inicio = %(fecha_inicio)s, fecha_fin = %(fecha_fin)s,"
        " precision_fecha = %(precision_fecha)s, origen = %(origen)s, resumen = %(resumen)s,"
        " validada_por = %(validada_por)s, modo_validacion = %(modo_validacion)s,"
        " alerta_inyeccion = %(alerta_inyeccion)s, publicable = %(publicable)s,"
        " validada_el = CASE WHEN %(validada_por)s::uuid IS NULL THEN NULL ELSE now() END,"
        # `trabajada_por` no es de la Ficha: la mueve solo el guardado de una persona (DEC-23).
        " trabajada_por = COALESCE(%(editada_por)s, trabajada_por), actualizada_el = now()"
        f" WHERE pieza_id = %(pieza_id)s RETURNING {_COLUMNAS_PIEZA}",
        {
            "estado": campos.estado.value,
            "tipo": campos.tipo,
            "tipo_actividad": campos.tipo_actividad,
            "propietario": campos.propietario,
            "autor": campos.autor,
            "fecha_inicio": campos.fecha_inicio,
            "fecha_fin": campos.fecha_fin,
            "precision_fecha": None if precision is None else precision.value,
            "origen": campos.origen,
            "resumen": campos.resumen,
            "validada_por": campos.validada_por,
            "modo_validacion": campos.modo_validacion,
            "alerta_inyeccion": informe.alerta,
            "publicable": campos.publicable,
            "editada_por": editada_por,
            "pieza_id": pieza_id,
        },
    ).fetchone()
    assert fila is not None
    escritos = _campos(fila)
    # Bajar la vigente y publicar la nueva van juntas: la invariante 10 es un índice único,
    # así que fuera de una transacción esto sería un error, no una convención.
    tx.execute(
        "UPDATE acervo.ficha SET vigente = false WHERE pieza_id = %s AND vigente", (pieza_id,)
    )
    nueva = tx.execute(
        "INSERT INTO acervo.ficha (pieza_id, version, contenido_markdown, origen_version,"
        " editada_por, metodo_autenticacion, configuracion_efectiva, informe_inyeccion,"
        " aprobacion_id, vigente)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true) RETURNING ficha_id, editada_el",
        (
            pieza_id,
            version,
            serializar(escritos, cuerpo),
            origen_version,
            editada_por,
            None if metodo is None else metodo.value,
            None if configuracion_efectiva is None else Jsonb(configuracion_efectiva.a_json()),
            Jsonb(informe.a_json()),
            aprobacion_id,
        ),
    ).fetchone()
    assert nueva is not None
    if descripcion is not None:
        tx.execute(
            "INSERT INTO acervo.descripcion_de_imagen (ficha_id, tipo_imagen) VALUES (%s, %s)",
            (nueva["ficha_id"], descripcion.tipo_imagen),
        )
        tx.cursor().executemany(
            "INSERT INTO acervo.afirmacion (ficha_id, orden, texto, respaldo)"
            " VALUES (%s, %s, %s, %s)",
            [
                (
                    nueva["ficha_id"],
                    orden,
                    texto,
                    None if respaldo is None else Jsonb([r.a_json() for r in respaldo]),
                )
                for orden, (texto, respaldo) in enumerate(
                    zip(
                        descripcion.afirmaciones,
                        descripcion.respaldos or (None,) * len(descripcion.afirmaciones),
                        strict=True,
                    ),
                    start=1,
                )
            ],
        )
    return FichaVigente(
        ficha_id=nueva["ficha_id"],
        pieza_id=pieza_id,
        version=version,
        origen_version=origen_version,
        campos=escritos,
        cuerpo=cuerpo,
        editada_por=editada_por,
        editada_el=nueva["editada_el"],
        metodo_autenticacion=metodo,
        informe_inyeccion=informe.a_json(),
    )


_AFIRMACIONES_DE = (
    "SELECT a.orden, a.texto, a.estado, a.decidida_por, a.decidida_el, a.respaldo, a.imagen,"
    " d.numero, d.pagina, d.hoja"
    " FROM acervo.afirmacion a JOIN acervo.ficha f ON f.ficha_id = a.ficha_id"
    " JOIN acervo.descripcion_de_imagen d ON d.ficha_id = a.ficha_id AND d.imagen = a.imagen"
    " WHERE true"
)
"""Las Afirmaciones con la Pieza de su v1 (`f`) y su imagen (`d`): quien la usa agrega
`AND f.pieza_id = …`."""

_DESCRIPCION_EN_CURSO = (
    "EXISTS (SELECT 1 FROM acervo.v_descripcion_en_curso t WHERE t.pieza_id = pieza.pieza_id)"
)
"""Una imagen de la Pieza con su trabajo de descripción encolado o en curso (148): la firma la
espera. Con el trabajo fallido ya no: la pantalla la muestra fallida y se vuelve a pedir."""


def _afirmaciones(tx: Tx, pieza_id: UUID) -> list[Afirmacion]:
    filas = tx.execute(
        f"{_AFIRMACIONES_DE} AND f.pieza_id = %s ORDER BY a.orden", (pieza_id,)
    ).fetchall()
    return [_afirmacion(fila) for fila in filas]


def _afirmacion(fila: dict[str, Any]) -> Afirmacion:
    return Afirmacion(
        orden=fila["orden"],
        texto=fila["texto"],
        estado=EstadoAfirmacion(fila["estado"]),
        decidida_por=fila["decidida_por"],
        decidida_el=fila["decidida_el"],
        respaldo=(
            None
            if fila["respaldo"] is None
            else tuple(Region.de_json(region) for region in fila["respaldo"])
        ),
        imagen=fila["imagen"],
        ubicacion=(
            None
            if fila["numero"] is None
            else UbicacionDeImagen(fila["numero"], fila["pagina"], fila["hoja"]).texto
        ),
    )


def _con_afirmaciones_firmadas(cuerpo: str) -> bool:
    return tiene_afirmaciones(CuerpoDeFicha.partir(cuerpo).principal)


def _aceptadas(afirmaciones: Sequence[Afirmacion]) -> list[str]:
    """Las aceptadas como van al cuerpo: la de una imagen embebida, con su ubicación delante
    («Página 3 · Imagen 2: …», 148), para que se sepa de qué imagen habla."""
    return [
        a.texto if a.ubicacion is None else f"{a.ubicacion}: {a.texto}"
        for a in afirmaciones
        if a.estado is EstadoAfirmacion.ACEPTADA
    ]


def _escribir_carreras(tx: Tx, pieza_id: UUID, carreras: Collection[str]) -> None:
    """Las Carreras vigentes de la Pieza pasan a ser `carreras`. Sin `DELETE` (DEC-143): la que
    falta se marca quitada y la que vuelve se limpia. La llaman `_insertar_version` y, con la
    Pieza Recibida, `completar_en_recibida` (152)."""
    tx.execute(
        "UPDATE acervo.pieza_carrera SET quitada_el = now()"
        " WHERE pieza_id = %s AND quitada_el IS NULL AND carrera_id <> ALL(%s)",
        (pieza_id, list(carreras)),
    )
    tx.execute(
        "INSERT INTO acervo.pieza_carrera (pieza_id, carrera_id)"
        " SELECT %s, unnest(%s::text[])"
        " ON CONFLICT (pieza_id, carrera_id) DO UPDATE SET quitada_el = NULL"
        " WHERE pieza_carrera.quitada_el IS NOT NULL",
        (pieza_id, list(carreras)),
    )


def _insertar_v1_sistema(tx: Tx, fila: dict[str, Any]) -> FichaVigente:
    """La v1 vacía de una Pieza que no leyó ningún modelo (un video, una Recibida completada a
    mano), `Pendiente` y con el Contexto escrito en Recibida o el del Lote si lo hay."""
    cuerpo = CuerpoDeFicha("", _contexto_previo(fila) or "").armar()
    return _insertar_version(
        tx,
        fila["pieza_id"],
        replace(_campos(fila), estado=Estado.PENDIENTE),
        cuerpo,
        version=1,
        origen_version="sistema",
    )


_GRUPOS_HEREDABLES: tuple[tuple[str, ...], ...] = (
    ("fecha_inicio", "fecha_fin", "precision_fecha"),
    ("origen",),
    ("propietario",),
    ("carreras",),
)
"""Los campos duros que copia el Lote, la Fecha entera."""


def _heredar_en_pendiente(tx: Tx, pieza_id: UUID, duros: CamposDuros) -> list[str]:
    """La versión `sistema` con lo que el Lote completó tarde y la Pendiente no tenía (151): el
    cuerpo y los demás campos siguen los de la vigente, y nada se firma."""
    actual = tx.execute(f"{_SELECT_PIEZA} WHERE pieza_id = %s FOR UPDATE", (pieza_id,)).fetchone()
    assert actual is not None
    campos = _campos(actual)
    completos = campos
    if campos.origen is None:
        completos = replace(completos, origen=duros.origen)
    if campos.propietario is None:
        completos = replace(completos, propietario=duros.propietario)
    if campos.fecha_inicio is None and duros.precision_fecha is not None:
        inicio, fin = _rango_de(duros)
        completos = replace(
            completos, fecha_inicio=inicio, fecha_fin=fin, precision_fecha=duros.precision_fecha
        )
    escritos = [c for c in CAMPOS_DUROS if getattr(completos, c) != getattr(campos, c)]
    if not escritos:
        return []
    vigente = tx.execute(_SELECT_FICHA_VIGENTE, (pieza_id,)).fetchone()
    assert vigente is not None
    tx.execute(
        "UPDATE acervo.pieza SET campos_heredados = campos_heredados || %s::text[]"
        " WHERE pieza_id = %s",
        (escritos, pieza_id),
    )
    _insertar_version(
        tx,
        pieza_id,
        completos,
        _ficha(vigente, pieza_id).cuerpo,
        version=vigente["version"] + 1,
        origen_version="sistema",
    )
    return escritos


def _contexto_previo(fila: dict[str, Any]) -> str | None:
    """El Contexto que la Pieza trae a su v1: el que escribió una persona con la Pieza Recibida,
    que ya partió del heredado y lo reemplaza, o el del Lote (152, DEC-113)."""
    persona: str | None = fila["contexto_de_la_persona"]
    return fila["contexto_heredado"] if persona is None else persona or None


def _original(fila: dict[str, Any]) -> OriginalGuardado:
    return OriginalGuardado(
        identificador=fila["archivo_original_id"],
        nombre_archivo=fila["nombre_archivo"],
        tipo_mime=fila["tipo_mime"],
        tamano_bytes=fila["tamano_bytes"],
        creado_el=fila["creada_el"],
        huella=fila["huella_original"],
    )


def _buscable(fila: dict[str, Any]) -> dict[str, Any]:
    return dict(
        pieza_id=fila["pieza_id"],
        ficha_id=fila["ficha_id"],
        tipo=fila["tipo"],
        tipo_actividad=fila["tipo_actividad"],
        carreras=tuple(fila["carreras"]),
        fecha_inicio=fila["fecha_inicio"],
        fecha_fin=fila["fecha_fin"],
        precision_fecha=PrecisionFecha(fila["precision_fecha"]),
        clase_original=ClaseDeOriginal(fila["clase_original"]),
        via_ingreso=ViaDeIngreso(fila["via_ingreso"]),
        resumen=fila["resumen"],
        nombre_archivo=fila["nombre_archivo"],
        validada_el=fila["validada_el"],
    )


def _fuente(fila: dict[str, Any]) -> FuenteDeFecha:
    precision = fila["precision_fecha"]
    return FuenteDeFecha(
        pieza_id=fila["pieza_id"],
        ficha_id=fila["ficha_id"],
        tipo=fila["tipo"],
        fecha_inicio=fila["fecha_inicio"],
        fecha_fin=fila["fecha_fin"],
        precision_fecha=None if precision is None else PrecisionFecha(precision),
        resumen=fila["resumen"],
        alerta_inyeccion=fila["alerta_inyeccion"],
        publicable=fila["publicable"],
        validada=fila["validada"],
    )


def _ficha(fila: dict[str, Any], pieza_id: UUID) -> FichaVigente:
    """Una versión de la Ficha reconstruida desde su markdown (ADR-0001)."""
    campos, cuerpo = analizar(fila["contenido_markdown"])
    origen_version: OrigenVersion = fila["origen_version"]
    return FichaVigente(
        ficha_id=fila["ficha_id"],
        pieza_id=pieza_id,
        version=fila["version"],
        origen_version=origen_version,
        campos=campos,
        cuerpo=cuerpo,
        editada_por=fila["editada_por"],
        editada_el=fila["editada_el"],
        metodo_autenticacion=_metodo(fila["metodo_autenticacion"]),
        informe_inyeccion=fila["informe_inyeccion"],
    )


def _metodo(valor: str | None) -> MetodoAutenticacion | None:
    return None if valor is None else MetodoAutenticacion(valor)


def _rango_de(edicion: EdicionFicha | CamposDuros) -> tuple[date | None, date | None]:
    """El rango cerrado que se guarda, derivado de la precisión que declaró la persona.

    La pantalla pide una fecha y una precisión; el fin se deriva y no se pide dos veces
    (`derivar_rango` es la única autoridad sobre cómo). La excepción es la precisión
    `rango`, donde los dos bordes son justamente el dato que la persona aporta.

    Una Fecha a medio declarar no es un error: es lo que deja la Ficha `Incompleta`. Lo que
    sí se rechaza es una `fecha_fin` sin una precisión que la justifique, porque ahí el
    borde no se deriva de nada y guardarlo sería inventar el rango.

    Raises:
        ValueError: Si la Fecha es incoherente con su precisión declarada.
    """
    if edicion.precision_fecha is None or edicion.fecha_inicio is None:
        if edicion.fecha_fin is not None:
            raise ValueError(
                "La fecha de fin no se declara sola: se deriva de la fecha de inicio y de "
                "su precisión, salvo con precisión 'rango', que exige las dos."
            )
        return (edicion.fecha_inicio, None)
    return derivar_rango(edicion.fecha_inicio, edicion.precision_fecha, edicion.fecha_fin)


def _resumen(fila: dict[str, Any]) -> ResumenPieza:
    return ResumenPieza(
        pieza_id=fila["pieza_id"],
        estado=Estado(fila["estado"]),
        tipo=fila["tipo"],
        resumen=fila["resumen"],
        nombre_archivo=fila["nombre_archivo"],
        creada_el=fila["creada_el"],
        actualizada_el=fila["actualizada_el"],
        campos_faltantes=[
            campo for campo in CAMPOS_OBLIGATORIOS if fila[_COLUMNA_DE_CAMPO[campo]] is None
        ],
        lote_id=fila["lote_id"],
        creada_por=fila["creada_por"],
        alerta_inyeccion=fila["alerta_inyeccion"],
    )


def _detalle(fila: dict[str, Any]) -> DetallePieza:
    return DetallePieza(
        fila=_resumen(fila),
        tipo_mime=fila["tipo_mime"],
        tamano_bytes=fila["tamano_bytes"],
        tipo_actividad=fila["tipo_actividad"],
        propietario=fila["propietario"],
        autor=fila["autor"],
        carreras=tuple(fila["carreras"]),
        lote_id=fila["lote_id"],
        campos_heredados=tuple(fila["campos_heredados"]),
        campos_de_la_persona=tuple(fila["campos_de_la_persona"]),
        clase_original=fila["clase_original"],
        huella=fila["huella_original"],
        via_ingreso=ViaDeIngreso(fila["via_ingreso"]),
        metadata_original=fila["metadata_original"],
    )


def _campos(fila: dict[str, Any]) -> CamposFicha:
    """El frontmatter de una Pieza, a partir de su fila."""
    precision = fila["precision_fecha"]
    return CamposFicha(
        id=fila["pieza_id"],
        tipo=fila["tipo"],
        tipo_actividad=fila["tipo_actividad"],
        fecha_inicio=fila["fecha_inicio"],
        fecha_fin=fila["fecha_fin"],
        precision_fecha=None if precision is None else PrecisionFecha(precision),
        origen=fila["origen"],
        propietario=fila["propietario"],
        autor=fila["autor"],
        carreras=tuple(fila["carreras"]),
        lote=fila["lote_id"],
        resumen=fila["resumen"],
        estado=Estado(fila["estado"]),
        validada_por=fila["validada_por"],
        validada_el=fila["validada_el"],
        modo_validacion=fila["modo_validacion"],
        archivo_original=fila["archivo_original_id"],
        alerta_inyeccion=fila["alerta_inyeccion"],
        publicable=fila["publicable"],
    )


def contar_piezas(tx: Tx) -> int:
    """Todas las Piezas, en cualquier estado y también las retiradas: el servidor de la
    universidad arranca sin ninguna (DEC-119, runbook R16)."""
    fila = tx.execute("SELECT count(*) AS n FROM acervo.pieza").fetchone()
    return int(fila["n"]) if fila else 0


def contar_firmas_de_ensayo(tx: Tx) -> int:
    """Las versiones de Ficha firmadas con una sesión `dev`: las firmas de ensayo, que no viajan
    al servidor de la universidad (DEC-139)."""
    fila = tx.execute(
        "SELECT count(*) AS n FROM acervo.ficha WHERE metodo_autenticacion = 'dev'"
    ).fetchone()
    return int(fila["n"]) if fila else 0
