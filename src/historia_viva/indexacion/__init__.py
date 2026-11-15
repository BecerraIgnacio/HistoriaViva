"""Indexación: de una Ficha Validada a fragmentos con embedding en el índice (spec, módulo 19).

La parte pura, `fragmentar`, corta el cuerpo en fragmentos que nunca cruzan una página ni una
sección (DEC-04, DEC-05); `Indexacion` es el manejador del tipo `indexacion` de la cola:
`ejecutar` embebe sin transacción abierta y `guardar` reemplaza los fragmentos de la Pieza en la
transacción del trabajador (DEC-84, DEC-99, DEC-146 punto 4).

No importa LlamaIndex: `indice` llega por constructor y su módulo se importa solo al construir
los fragmentos que guarda, así recolectar la suite no lo carga (ticket 130).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID, uuid5

from historia_viva.auditoria import Accion, Actor, ObjetoRef, RegistroDeAuditoria
from historia_viva.bd import Bd, Tx
from historia_viva.capa_modelos.configuracion import CapaPara
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CUERPO_FICHA,
    BloqueDatos,
    Capacidad,
    UsoEmbedding,
)
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.cuerpo import MarcaDeHoja, Segmento, segmentos
from historia_viva.dominio.errores import ConfiguracionInvalida, FichaCambio, PiezaNoEncontrada
from historia_viva.dominio.fechas import fecha_legible
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import FichaIndexable, ViaDeIngreso
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.interruptor import Freno, ProcesamientoDetenido, habilitado
from historia_viva.procesamiento.cola import Cola, EstadoDeTrabajo, TipoDeTrabajo, TrabajoTomado
from historia_viva.procesamiento.trabajador import ResultadoDeTrabajo

if TYPE_CHECKING:
    from historia_viva.indice import Fragmento, Indice

__all__ = [
    "PARAMETROS",
    "VERSION_FRAGMENTADOR",
    "EstadoDeIndexacion",
    "FragmentoNuevo",
    "Indexacion",
    "InformeDeIndice",
    "LineaDeContexto",
    "MotivoNoIndexada",
    "ParametrosDeFragmentacion",
    "ResultadoDeIndexacion",
    "fragmentar",
    "fragmentos_de",
]

_log = logging.getLogger(__name__)

VERSION_FRAGMENTADOR = "4"
"""Entra en el `fragmento_id`: cambiar la estrategia cambia los ids y exige `reindexar` (DEC-04)."""


@dataclass(frozen=True)
class ParametrosDeFragmentacion:
    objetivo_car: int = 1200
    maximo_car: int = 1500
    solape_car: int = 150


PARAMETROS = ParametrosDeFragmentacion()


@dataclass(frozen=True)
class LineaDeContexto:
    """Lo que se antepone al texto que se embebe, no al que se cita: «acta · marzo de 1998»."""

    tipo: str
    fecha_legible: str

    def como_texto(self) -> str:
        return f"{self.tipo} · {self.fecha_legible}"


@dataclass(frozen=True)
class FragmentoNuevo:
    orden: int
    fragmento_id: UUID
    texto: str
    """El tramo citable, tal cual está en el cuerpo: `cuerpo[inicio:fin]`."""
    texto_embebido: str
    ubicacion: Mapping[str, Any]
    inicio: int
    fin: int


def fragmentar(
    ficha_id: UUID,
    segmentos: Sequence[Segmento],
    linea: LineaDeContexto,
    parametros: ParametrosDeFragmentacion = PARAMETROS,
    *,
    resumen: str = "",
) -> list[FragmentoNuevo]:
    """Los fragmentos de una versión de la Ficha, en orden. Pura.

    Un segmento que entra en `maximo_car` es un fragmento; uno más largo se parte cerca de
    `objetivo_car`, en un salto de párrafo, de línea o un espacio, con `solape_car` de solape
    dentro del segmento. La hoja de una planilla se parte en bloques de filas sin solape (ver
    `_bloques`). Sin principal con texto, `resumen` (el firmado de un video o un Aporte,
    DEC-05) es el primer fragmento, en `{"seccion": "resumen"}` y sin tramo del cuerpo
    (`inicio == fin == 0`); el Contexto, si lo hay, va detrás.
    El `fragmento_id` es UUIDv5 de `ficha_id`: cambia con cada versión (DEC-146 punto 9) y se
    repite al reindexar la misma (RNF-08).
    """
    tramos = [
        (segmento, tramo)
        for segmento in segmentos
        for tramo in (
            _bloques(segmento, parametros)
            # El bloque de una imagen de la hoja (143) es texto, no filas.
            if "hoja" in segmento.ubicacion and "imagen" not in segmento.ubicacion
            else [
                Tramo(inicio, fin, "", segmento.ubicacion)
                for inicio, fin in _tramos(segmento.texto, parametros)
            ]
        )
    ]
    # (texto citable, texto embebido sin la línea de contexto, ubicacion, inicio, fin)
    piezas: list[tuple[str, str, Mapping[str, Any], int, int]] = [
        (
            segmento.texto[tramo.inicio : tramo.fin],
            tramo.prefijo + segmento.texto[tramo.inicio : tramo.fin],
            tramo.ubicacion,
            segmento.inicio + tramo.inicio,
            segmento.inicio + tramo.fin,
        )
        for segmento, tramo in tramos
    ]
    sin_principal = not any(
        s.ubicacion["seccion"] == "principal" and s.texto.strip() for s in segmentos
    )
    if sin_principal and resumen.strip():
        piezas.insert(0, (resumen, resumen, {"seccion": "resumen"}, 0, 0))
    return [
        FragmentoNuevo(
            orden=orden,
            fragmento_id=uuid5(ficha_id, f"{VERSION_FRAGMENTADOR}:{orden}"),
            texto=texto,
            texto_embebido=f"{linea.como_texto()}\n\n{embebido}",
            ubicacion=ubicacion,
            inicio=inicio,
            fin=fin,
        )
        for orden, (texto, embebido, ubicacion, inicio, fin) in enumerate(piezas)
    ]


def fragmentos_de(ficha: FichaIndexable) -> list[FragmentoNuevo]:
    """Los fragmentos que indexa `ejecutar` de la Ficha vigente; también los embebe el banco de
    `embeber` (077) en memoria. Pura."""
    linea = LineaDeContexto(
        ficha.tipo, fecha_legible(ficha.fecha_inicio, ficha.fecha_fin, ficha.precision_fecha)
    )
    # Video y Aporte: solo lo que escribió una persona, o el resumen que firmó (DEC-05).
    del_resumen = ficha.clase_original is ClaseDeOriginal.VIDEO or (
        ficha.via_ingreso is ViaDeIngreso.APORTE
    )
    return fragmentar(
        ficha.ficha_id,
        segmentos(ficha.cuerpo, ficha.clase_original),
        linea,
        resumen=ficha.resumen if del_resumen else "",
    )


class Tramo(NamedTuple):
    """Un tramo de un segmento que va a ser fragmento."""

    inicio: int
    fin: int
    prefijo: str
    """Lo que se le antepone solo al texto que se embebe: la marca y el encabezado de su hoja."""
    ubicacion: Mapping[str, Any]


def _bloques(hoja: Segmento, parametros: ParametrosDeFragmentacion) -> list[Tramo]:
    """La hoja de una planilla en bloques de filas sin solape. El primero empieza con la marca y el encabezado; los demás los llevan en el
    `prefijo`, que va solo en el texto que se embebe (el citable es el tramo del cuerpo). Cada
    bloque junta filas mientras el prefijo y las filas entren en `objetivo_car`; una fila más
    larga va sola. La fila de la tabla es la de la planilla: el encabezado es `desde`."""
    lineas = hoja.texto.splitlines(keepends=True)
    marca = MarcaDeHoja.leer(lineas[0].rstrip("\n"))
    assert marca is not None  # `segmentos` corta una hoja solo en una marca válida
    tabla = len(lineas) >= 3 and all(linea.startswith("|") for linea in lineas[1:3])
    cabecera = lineas[:3] if tabla else lineas[:1]
    prefijo = "".join(cabecera)
    grupos: list[list[int]] = [list(range(len(cabecera)))]
    largo, filas = len(prefijo), 0
    for numero in range(len(cabecera), len(lineas)):
        linea = lineas[numero]
        # Una línea en blanco (la que separa de la hoja siguiente) no abre un bloque.
        if linea.strip() and filas and largo + len(linea) > parametros.objetivo_car:
            grupos.append([])
            largo, filas = len(prefijo), 0
        grupos[-1].append(numero)
        largo, filas = largo + len(linea), filas + 1
    inicios = [sum(map(len, lineas[: grupo[0]])) for grupo in grupos]
    fines = [*inicios[1:], len(hoja.texto)]
    return [
        Tramo(
            inicio,
            fin,
            "" if orden == 0 else prefijo,
            {**hoja.ubicacion, **_filas(marca.desde, grupo, lineas, tabla)},
        )
        for orden, (grupo, inicio, fin) in enumerate(zip(grupos, inicios, fines, strict=True))
    ]


def _filas(desde: int, grupo: list[int], lineas: list[str], tabla: bool) -> dict[str, list[int]]:
    """Las filas de la planilla que cita el bloque: la línea 1 de la hoja es el encabezado
    (`desde`) y la 3 en adelante, una fila cada una (la 2 son los guiones)."""
    numeros = [
        desde if numero == 1 else desde + numero - 2
        for numero in grupo
        if tabla and numero != 2 and numero > 0 and lineas[numero].startswith("|")
    ]
    return {"filas": [min(numeros), max(numeros)]} if numeros else {}


def _tramos(texto: str, p: ParametrosDeFragmentacion) -> list[tuple[int, int]]:
    tramos: list[tuple[int, int]] = []
    inicio = 0
    while len(texto) - inicio > p.maximo_car:
        fin = _corte(texto, inicio + p.objetivo_car // 2, inicio + p.objetivo_car)
        tramos.append((inicio, fin))
        inicio = fin - p.solape_car
    tramos.append((inicio, len(texto)))
    return tramos


def _corte(texto: str, desde: int, hasta: int) -> int:
    """El último salto de párrafo, de línea o espacio en `(desde, hasta]`, o `hasta`."""
    for separador in ("\n\n", "\n", " "):
        posicion = texto.rfind(separador, desde, hasta)
        if posicion > desde:
            return posicion + len(separador)
    return hasta


@dataclass(frozen=True, kw_only=True)
class ResultadoDeIndexacion(ResultadoDeTrabajo):
    """Lo que `ejecutar` embebió, todavía sin guardar. Sin Ficha, la Pieza dejó de ser
    indexable y `guardar` no escribe (DEC-91)."""

    ficha: FichaIndexable | None
    fragmentos: tuple[FragmentoNuevo, ...] = ()
    vectores: tuple[tuple[float, ...], ...] = ()
    modelo_embedding: str = ""


@dataclass(frozen=True)
class InformeDeIndice:
    """Lo que el índice tiene de una Pieza: la fuente de M2."""

    pieza_id: UUID
    ficha_id: UUID | None
    """La vigente, para comparar con la de los fragmentos; `None` en una Recibida."""
    fragmentos: tuple[Fragmento, ...]
    modelos: frozenset[str]
    trabajo: EstadoDeTrabajo | None
    """El estado de su última indexación; `None` si nunca se encoló."""


@dataclass(frozen=True)
class EstadoDeIndexacion:
    """Cuánto del Acervo validado refleja el índice: la salud y la franja (DEC-146 punto 6)."""

    indexables: int
    """Validadas no retiradas."""
    con_fragmentos: int
    sin_fragmentos: int
    pendientes: int
    """Indexables con su última indexación `encolado` o `en_curso`."""
    fallidas: int
    """Indexables con su última indexación `fallido`."""
    de_otro_modelo: int
    """Fragmentos que la búsqueda no ve: los embebió otro modelo (DEC-02)."""
    afectadas: int
    """Indexables sin fragmentos, con la indexación fallida o con fragmentos de otro modelo: el
    conteo de la franja."""

    @property
    def incompleto(self) -> bool:
        return self.de_otro_modelo > 0 or self.sin_fragmentos > 0 or self.fallidas > 0


class MotivoNoIndexada(StrEnum):
    """Por qué una Validada no tiene fragmentos: lo que Mis Piezas y el Lote muestran como «No
    indexada» (DEC-146 punto 6)."""

    EN_COLA = "en_cola"
    FALLIDA = "fallida"
    SIN_TRABAJO = "sin_trabajo"


_MOTIVO_POR_ESTADO: dict[EstadoDeTrabajo | None, MotivoNoIndexada] = {
    EstadoDeTrabajo.ENCOLADO: MotivoNoIndexada.EN_COLA,
    EstadoDeTrabajo.EN_CURSO: MotivoNoIndexada.EN_COLA,
    EstadoDeTrabajo.FALLIDO: MotivoNoIndexada.FALLIDA,
}


class Indexacion:
    """El manejador del tipo `indexacion` y el servicio que encola y reconstruye."""

    def __init__(
        self,
        *,
        bd: Bd,
        indice: Callable[[], Indice],
        capa_para: CapaPara,
        fichas: ModuloFichasPostgres,
        cola: Cola,
        configuracion: ConfiguracionDeModelos,
        auditoria: RegistroDeAuditoria,
    ) -> None:
        self._bd = bd
        self._auditoria = auditoria
        self._armar_indice = indice
        self._capa_para = capa_para
        self._fichas = fichas
        self._cola = cola
        self._configuracion = configuracion

    @cached_property
    def _indice(self) -> Indice:
        """Se arma al usarlo: encolar y `reindexar` no cargan llama-index (ticket 130)."""
        return self._armar_indice()

    def encolar(self, tx: Tx, pieza_id: UUID) -> UUID | None:
        """Encola la indexación de la Ficha vigente en la transacción de quien llama, o reusa
        el trabajo activo. Una Pieza que no es Validada no se encola: nunca tiene fragmentos."""
        ficha = self._fichas.leer_para_indexar(tx, pieza_id)
        if ficha is None:
            return None
        return self._cola.encolar_indexacion(
            tx,
            pieza_id,
            ficha_id=ficha.ficha_id,
            configuracion_efectiva=self._configuracion.instantanea(tx, (Capacidad.EMBEBER,)),
        )

    def desindexar(self, tx: Tx, pieza_id: UUID) -> int:
        """Quita los fragmentos de la Pieza en la transacción de quien llama (DEC-73), sin
        cargar llama-index con la Pieza bloqueada."""
        from historia_viva.indice import quitar_fragmentos

        return quitar_fragmentos(tx, pieza_id)

    def reconstruir(self, tx: Tx, *, actor: Actor) -> int:
        """`reindexar`: encola una indexación por cada Validada no retirada (RNF-08) y lo deja
        en la auditoría (DEC-146 punto 13).

        Raises:
            ProcesamientoDetenido: Con `procesamiento_habilitado` en falso no encola nada
                (RF-30).
        """
        if not habilitado(tx, Freno.PROCESAMIENTO):
            raise ProcesamientoDetenido()
        piezas = self._fichas.ids_indexables(tx)
        for pieza_id in piezas:
            self.encolar(tx, pieza_id)
        self._auditoria.registrar(
            tx,
            actor=actor,
            accion=Accion.INDICE_RECONSTRUCCION,
            objeto=ObjetoRef("indice"),
            despues={"encoladas": len(piezas)},
        )
        return len(piezas)

    def ejecutar(self, trabajo: TrabajoTomado) -> ResultadoDeIndexacion:
        """Fragmenta y embebe la Ficha vigente al correr, no la del encolado, sin transacción
        abierta mientras embebe (DEC-99)."""
        with self._bd.transaccion() as tx:
            ficha = self._fichas.leer_para_indexar(tx, trabajo.pieza_id)
        if ficha is None:
            return ResultadoDeIndexacion(ficha=None)
        fragmentos = fragmentos_de(ficha)
        if not fragmentos:
            return ResultadoDeIndexacion(ficha=ficha)
        capa = self._capa_para(trabajo.configuracion_efectiva.por_capacidad)
        vectores = capa.embeber(
            textos=tuple(
                BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=f.texto_embebido)
                for f in fragmentos
            ),
            uso=UsoEmbedding.FRAGMENTO,
        )
        return ResultadoDeIndexacion(
            ficha=ficha,
            fragmentos=tuple(fragmentos),
            vectores=vectores.vectores,
            modelo_embedding=vectores.modelo,
        )

    def guardar(self, tx: Tx, trabajo: TrabajoTomado, resultado: ResultadoDeIndexacion) -> None:
        """Reemplaza los fragmentos de la Pieza con la Ficha bloqueada `FOR SHARE`.

        Raises:
            FichaCambio: Si la vigente ya no es la que se embebió.
        """
        from historia_viva.indice import FragmentoIndexado, precargar

        precargar()  # llama-index entra antes de bloquear la Ficha, no con la edición esperando
        vigente = self._fichas.leer_para_indexar(tx, trabajo.pieza_id, bloquear=True)
        if vigente is None:
            return  # dejó de ser indexable: no escribe (DEC-91)
        ficha = resultado.ficha
        if ficha is None or ficha.ficha_id != vigente.ficha_id:
            raise FichaCambio(trabajo.pieza_id)
        self._indice.reemplazar(
            tx,
            trabajo.pieza_id,
            [
                FragmentoIndexado(
                    fragmento_id=f.fragmento_id,
                    pieza_id=ficha.pieza_id,
                    ficha_id=ficha.ficha_id,
                    tipo=ficha.tipo,
                    tipo_actividad=ficha.tipo_actividad,
                    carreras=ficha.carreras,
                    fecha_inicio=ficha.fecha_inicio,
                    fecha_fin=ficha.fecha_fin,
                    clase_original=ficha.clase_original,
                    ubicacion=f.ubicacion,
                    inicio=f.inicio,
                    fin=f.fin,
                    texto=f.texto,
                    modelo_embedding=resultado.modelo_embedding,
                    via_ingreso=ficha.via_ingreso,
                    vector=vector,
                )
                for f, vector in zip(resultado.fragmentos, resultado.vectores, strict=True)
            ],
        )

    def despues_de_completar(
        self, tx: Tx, trabajo: TrabajoTomado, resultado: ResultadoDeIndexacion
    ) -> None:
        """Si el trabajo embebió con un modelo que ya no es el de `embeber` (cambió la fila
        mientras corría), encola otro: ya está `completado`, así que no lo reusa (DEC-146
        punto 5). Si congelar lanza `ConfiguracionInvalida`, no encola y avisa: revertir el
        `completar` perdería lo embebido. En el trabajador del compose no lanza (congela sin
        validar): se encola con la fila inválida y el trabajo nuevo queda `fallido` al
        ejecutar; se recupera con `reprocesar --fallidos --tipo indexacion` o `reindexar`."""
        if not resultado.fragmentos or resultado.modelo_embedding.startswith(
            self._prefijo_vigente(tx)
        ):
            return
        try:
            self.encolar(tx, trabajo.pieza_id)
        except ConfiguracionInvalida:
            _log.warning(
                "No se pudo reencolar la indexación de %s con el modelo nuevo: la fila de"
                " `embeber` no valida. Arreglala y corré `reindexar`.",
                trabajo.pieza_id,
                exc_info=True,
            )

    def _prefijo_vigente(self, tx: Tx) -> str:
        """`<modelo>@` de la fila de `embeber`. shortcut: un `pull` del mismo tag cambia solo el
        digest y no se nota acá; detectarlo exige preguntarle el digest al motor."""
        fila = self._configuracion.vigente(tx).get(Capacidad.EMBEBER)
        return "" if fila is None else f"{fila.modelo}@"  # sin fila no hay con qué comparar

    def progreso(self, modelo_embedding: str | None = None) -> EstadoDeIndexacion:
        """El estado del índice contra `modelo_embedding` (el de la consulta, en la búsqueda) o,
        sin él, contra el digest del modelo de `embeber` con más fragmentos, como la búsqueda
        sin texto (050).

        shortcut: recorre todas las Validadas y el índice entero en cada llamada; alcanza para
        el Acervo del MVP, vigilarlo contra RNF-10 si crece.
        """
        if modelo_embedding is None:
            with self._bd.transaccion() as tx:
                prefijo = self._prefijo_vigente(tx)
            modelo_embedding = self._indice.modelo_mayoritario(prefijo) or ""
        with self._bd.transaccion() as tx:
            indexables = set(self._fichas.ids_indexables(tx))
            trabajos = self._cola.estado_de_trabajos(
                tx, list(indexables), TipoDeTrabajo.INDEXACION
            )
            cobertura = self._indice.cobertura(tx, modelo_embedding)
        con_fragmentos = indexables & cobertura.keys()
        fallidas = {p for p, estado in trabajos.items() if estado is EstadoDeTrabajo.FALLIDO}
        de_otro_modelo = {p for p in con_fragmentos if cobertura[p]}
        return EstadoDeIndexacion(
            indexables=len(indexables),
            con_fragmentos=len(con_fragmentos),
            sin_fragmentos=len(indexables - con_fragmentos),
            pendientes=sum(
                estado in (EstadoDeTrabajo.ENCOLADO, EstadoDeTrabajo.EN_CURSO)
                for estado in trabajos.values()
            ),
            fallidas=len(fallidas),
            de_otro_modelo=sum(cobertura[p] for p in con_fragmentos),
            afectadas=len((indexables - con_fragmentos) | fallidas | de_otro_modelo),
        )

    def no_indexadas(self, tx: Tx, pieza_ids: Sequence[UUID]) -> dict[UUID, MotivoNoIndexada]:
        """De `pieza_ids`, las Validadas no retiradas sin fragmentos y por qué: su última
        indexación está en cola, falló, o no hay ninguna activa (terminó y el índice se perdió,
        o nunca se encoló)."""
        # Solo los ids pedidos y sin armar el `Indice`: lo pregunta la lista de Piezas en cada
        # sondeo, y armarlo carga llama-index en `web` (ticket 130, 084).
        from historia_viva.indice import piezas_con_fragmentos

        indexables = set(self._fichas.ids_indexables(tx, entre=pieza_ids))
        sin_fragmentos = indexables - piezas_con_fragmentos(tx, indexables)
        trabajos = self._cola.estado_de_trabajos(
            tx, list(sin_fragmentos), TipoDeTrabajo.INDEXACION
        )
        return {
            pieza_id: _MOTIVO_POR_ESTADO.get(trabajos.get(pieza_id), MotivoNoIndexada.SIN_TRABAJO)
            for pieza_id in sin_fragmentos
        }

    def pendientes(self, tx: Tx) -> int:
        """Las Validadas no retiradas con su última indexación en cola o en curso, en la
        transacción de quien llama: `banco m2` no mide un índice a medio armar."""
        indexables = self._fichas.ids_indexables(tx)
        trabajos = self._cola.estado_de_trabajos(tx, indexables, TipoDeTrabajo.INDEXACION)
        return sum(
            estado in (EstadoDeTrabajo.ENCOLADO, EstadoDeTrabajo.EN_CURSO)
            for estado in trabajos.values()
        )

    def inspeccionar(self, pieza_id: UUID) -> InformeDeIndice:
        fragmentos = tuple(self._indice.de_pieza(pieza_id))
        with self._bd.transaccion() as tx:
            trabajos = self._cola.estado_de_trabajos(tx, [pieza_id], TipoDeTrabajo.INDEXACION)
        try:
            ficha_id: UUID | None = self._fichas.obtener_ficha(pieza_id).ficha_id
        except PiezaNoEncontrada:
            ficha_id = None
        return InformeDeIndice(
            pieza_id=pieza_id,
            ficha_id=ficha_id,
            fragmentos=fragmentos,
            modelos=frozenset(f.modelo_embedding for f in fragmentos),
            trabajo=trabajos.get(pieza_id),
        )
