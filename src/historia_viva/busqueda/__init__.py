"""Búsqueda: recuperar fragmentos filtrados y citados del Acervo (spec, módulo 21).

Servicio de solo lectura: embebe la consulta, aplica el filtro duro antes de comparar vectores
(RF-20), descarta lo que `fichas` ya no da por buscable (DEC-72), agrupa por Pieza y cita. Es la
recuperación de la Conversación (`recuperar`), de M1 (`buscar`) y de `/v1` (`buscar_fragmentos`).
No redacta ni llama a `responder`, y nunca pregunta por el interruptor: la consulta del Acervo
validado no se pausa (DEC-136). El modelo vigente y el aviso de índice incompleto le llegan de
`composicion` (DEC-146 puntos 6 y 12).

No importa LlamaIndex: `indice` llega por constructor y se arma al usarlo (ticket 130).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from typing import TYPE_CHECKING, Any
from uuid import UUID

from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONSULTA,
    BloqueDatos,
    CapaModelos,
    UsoEmbedding,
)
from historia_viva.dominio.cuerpo import segundos_de_marca
from historia_viva.dominio.errores import ConsultaInvalida, LimiteExcedido, MotorNoDisponible
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import (
    FiltroDeBusqueda,
    PiezaBuscable,
    PiezaPublicada,
    ViaDeIngreso,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres

if TYPE_CHECKING:
    from historia_viva.indice import Fragmento, FragmentoHallado, Indice

__all__ = [
    "BuscadorNoDisponible",
    "Busqueda",
    "Cita",
    "Consulta",
    "ConsultaVacia",
    "IndiceNoDisponible",
    "Recuperacion",
    "ResultadoDeBusqueda",
    "ResultadoDePieza",
    "UMBRAL_DE_SIMILITUD",
    "consultas_de_referencia",
    "ubicar",
]

CITAS_POR_PIEZA = 3
CANDIDATOS = 40
"""Fragmentos que se piden al índice antes de agrupar (spec, pseudocódigo de `buscar`)."""

LIMITE_MAXIMO = 50

UMBRAL_DE_SIMILITUD = 0.42
"""Similitud coseno mínima del mejor fragmento de una Ficha para que entre como Fuente (133).
Calibrado con el modelo de `embeber` de la PC el 21-oct-2026: lo que no responde la pregunta
llegó hasta 0,41 y lo que sí, desde 0,51. Depende del modelo: `HV_UMBRAL_SIMILITUD` lo cambia
sin tocar código."""


class ConsultaVacia(ConsultaInvalida):
    """Ni texto ni Fechas: sin criterio no se recorre el Acervo entero."""

    def __init__(self) -> None:
        super().__init__("Hace falta un texto, una fecha «desde» o una «hasta».", campo=None)


class BuscadorNoDisponible(Exception):
    """No se pudo embeber la consulta: la API responde 503, nunca una lista vacía."""


class IndiceNoDisponible(Exception):
    """Con texto, el índice no tiene ningún fragmento del modelo vigente (vacío o sin
    reindexar): la API responde 503, no una lista vacía. Sin texto la búsqueda sigue."""


@dataclass(frozen=True)
class Consulta:
    texto: str | None = None
    filtro: FiltroDeBusqueda = field(default_factory=FiltroDeBusqueda)
    limite: int = 10

    def __post_init__(self) -> None:
        # Un texto en blanco no es un criterio; se valida una sola vez, al crearla.
        object.__setattr__(self, "texto", (self.texto or "").strip() or None)
        self.validar()

    def validar(self) -> None:
        """Raises:
        ConsultaVacia: Sin texto ni Fechas.
        ConsultaInvalida: El filtro no valida, o el texto trae un NUL.
        """
        if not self.texto and not self.filtro.desde and not self.filtro.hasta:
            raise ConsultaVacia()
        if self.texto and "\x00" in self.texto:
            raise ConsultaInvalida("El texto trae un carácter nulo.", campo="q")
        self.filtro.validar()


@dataclass(frozen=True)
class Cita:
    fragmento_id: UUID
    texto: str
    ubicacion_legible: str
    """«p. 4»; sin página, la sección."""
    enlace: str
    """El original con ancla: `#page=N` en un PDF."""


@dataclass(frozen=True)
class ResultadoDePieza:
    pieza_id: UUID
    tipo: str
    tipo_actividad: str
    carreras: tuple[str, ...]
    fecha_inicio: date
    fecha_fin: date
    precision_fecha: PrecisionFecha
    clase_original: ClaseDeOriginal
    via_ingreso: ViaDeIngreso
    resumen: str
    nombre_archivo: str | None
    puntaje: float
    """El de su mejor cita; 0 sin texto."""
    citas: tuple[Cita, ...]


@dataclass(frozen=True)
class ResultadoDeBusqueda:
    filtro_aplicado: FiltroDeBusqueda
    total: int
    resultados: tuple[ResultadoDePieza, ...]
    indice_incompleto: bool
    duracion_ms: int


@dataclass(frozen=True)
class Recuperacion:
    resultado: ResultadoDeBusqueda
    fragmentos: tuple[FragmentoHallado, ...]
    """Las citas de `resultado`, en su orden: lo único que el modelo puede citar."""
    mostrados: tuple[FragmentoHallado, ...] = ()
    """Todas las citas de `resultado`, en su orden: lo que vio la persona (`mensaje_cita`)."""


class Busqueda:
    def __init__(
        self,
        *,
        indice: Callable[[], Indice],
        capa: Callable[[], CapaModelos],
        fichas: ModuloFichasPostgres,
        modelo_vigente: Callable[[], str | None],
        indice_incompleto: Callable[[str], bool],
        umbral: float,
    ) -> None:
        """`modelo_vigente` es el `modelo_embedding` de la fila vigente de `embeber`, para
        buscar sin texto sin tocar el motor (`None` si el índice no tiene ninguno);
        `indice_incompleto(modelo)` dice si hay material que ese modelo no cubre. Los dos los
        arma `composicion`. Con texto, una Ficha cuyo mejor fragmento queda debajo de `umbral`
        no entra: ni en la Conversación, ni en M1, ni en `/v1` (133)."""
        self._armar_indice = indice
        self._capa = capa
        self._fichas = fichas
        self._modelo_vigente = modelo_vigente
        self._indice_incompleto = indice_incompleto
        self._umbral = umbral

    @cached_property
    def _indice(self) -> Indice:
        return self._armar_indice()

    def buscar(self, consulta: Consulta) -> ResultadoDeBusqueda:
        """Agrupada por Pieza, con hasta 3 Citas cada una: la lista que ve la persona y la que
        mide M1 (DEC-89). Un filtro sin resultados da `total = 0`, sin ampliarlo.

        Raises:
            BuscadorNoDisponible: No se pudo embeber la consulta.
            LimiteExcedido: Embeber la consulta agotó su tope de tiempo.
            IndiceNoDisponible: Con texto, no hay fragmentos del modelo vigente.
        """
        return self._buscar(consulta)[0]

    def recuperar(self, consulta: Consulta, *, max_fragmentos: int = 5) -> Recuperacion:
        """El resultado completo y, para el modelo, sus primeras `max_fragmentos` citas en el
        orden de la lista: todo lo que el modelo puede citar está a la vista."""
        resultado, citados = self._buscar(consulta)
        return Recuperacion(resultado, tuple(citados[:max_fragmentos]), tuple(citados))

    def buscar_fragmentos(
        self, consulta: Consulta, limite: int
    ) -> list[tuple[FragmentoHallado, PiezaBuscable]]:
        """Plana, para `/v1` (hasta 50): solo fragmentos de Piezas buscables y publicables, cada
        uno con su Pieza, que trae lo que el fragmento no tiene (precisión, resumen, firma)."""
        limite = min(limite, LIMITE_MAXIMO)
        hallados, _ = self._hallar(consulta, candidatos=max(CANDIDATOS, limite))
        buscables, vigentes = self._buscables(hallados, solo_publicables=True)
        return [(h, buscables[h.pieza_id]) for h in vigentes[:limite]]

    def ampliar(self, pieza_id: UUID) -> tuple[PiezaPublicada, list[Fragmento]] | None:
        """La Pieza publicable y todos los fragmentos de su Ficha vigente, en el orden del
        cuerpo, para `GET /v1/piezas/{id}` (RF-35). `None` si `/v1` no la publica."""
        pieza = self._fichas.publicada(pieza_id)
        if pieza is None:
            return None
        # Un residuo de una Ficha anterior se descarta, como en la búsqueda (DEC-72).
        fragmentos = [f for f in self._indice.de_pieza(pieza_id) if f.ficha_id == pieza.ficha_id]
        return pieza, fragmentos

    def _buscar(self, consulta: Consulta) -> tuple[ResultadoDeBusqueda, list[FragmentoHallado]]:
        comienzo = time.monotonic()
        limite = min(consulta.limite, LIMITE_MAXIMO)
        hallados, modelo = self._hallar(
            consulta, candidatos=max(CANDIDATOS, limite * CITAS_POR_PIEZA), por_pieza=True
        )
        buscables, vigentes = self._buscables(hallados)
        # En el orden del índice: por puntaje con texto, por Fecha sin él.
        por_pieza: dict[UUID, list[FragmentoHallado]] = {}
        for hallado in vigentes:
            por_pieza.setdefault(hallado.pieza_id, []).append(hallado)
        elegidas = [
            (buscables[pieza_id], fragmentos[:CITAS_POR_PIEZA])
            for pieza_id, fragmentos in list(por_pieza.items())[:limite]
        ]
        resultados = tuple(
            _resultado(pieza, fragmentos) for pieza, fragmentos in elegidas
        )
        return (
            ResultadoDeBusqueda(
                filtro_aplicado=consulta.filtro,
                total=len(resultados),
                resultados=resultados,
                indice_incompleto=self._indice_incompleto(modelo),
                duracion_ms=int((time.monotonic() - comienzo) * 1000),
            ),
            [f for _, fragmentos in elegidas for f in fragmentos],
        )

    def _buscables(
        self, hallados: Sequence[FragmentoHallado], *, solo_publicables: bool = False
    ) -> tuple[dict[UUID, PiezaBuscable], list[FragmentoHallado]]:
        """Las Piezas buscables y, en su orden, los hallados de su Ficha vigente: un residuo
        del índice se descarta (DEC-72)."""
        buscables = self._fichas.buscables(
            {(h.pieza_id, h.ficha_id) for h in hallados}, solo_publicables=solo_publicables
        )
        vigentes = [
            h for h in hallados
            if h.pieza_id in buscables and h.ficha_id == buscables[h.pieza_id].ficha_id
        ]
        return buscables, vigentes

    def _hallar(
        self, consulta: Consulta, *, candidatos: int, por_pieza: bool = False
    ) -> tuple[list[FragmentoHallado], str]:
        """Los fragmentos del índice y el modelo con el que se filtraron. Sin texto no se
        embebe ni se toca el motor: las consultas por período no gastan modelo."""
        if not consulta.texto:
            modelo = self._modelo_vigente()
            if modelo is None:
                return [], ""
            listados = self._indice.listar(
                filtro=consulta.filtro,
                modelo_embedding=modelo,
                limite=candidatos,
                por_pieza=CITAS_POR_PIEZA if por_pieza else None,
            )
            return listados, modelo
        try:
            vectores = self._capa().embeber(
                textos=(BloqueDatos(etiqueta=ETIQUETA_CONSULTA, contenido=consulta.texto),),
                uso=UsoEmbedding.CONSULTA,
            )
        except MotorNoDisponible as error:
            raise BuscadorNoDisponible(str(error)) from error
        except LimiteExcedido as error:
            if error.limite == "tiempo":
                raise  # 504 `tiempo-agotado`
            raise BuscadorNoDisponible(str(error)) from error
        # El modelo que embebió la consulta: nunca se comparan vectores de modelos distintos.
        hallados = self._indice.buscar(
            vectores.vectores[0],
            modelo_embedding=vectores.modelo,
            filtro=consulta.filtro,
            k=candidatos,
        )
        if not hallados:
            estado = self._indice.estado(vectores.modelo)
            if estado.fragmentos == estado.de_otro_modelo:
                raise IndiceNoDisponible(f"El índice no tiene fragmentos de {vectores.modelo}.")
        # Por Ficha: si su mejor fragmento llega al umbral, entra con sus demás Citas (133).
        if not self._umbral:  # 0: sin corte (el coseno puede ser negativo)
            return hallados, vectores.modelo
        relevantes = {(h.pieza_id, h.ficha_id) for h in hallados if h.puntaje >= self._umbral}
        return [h for h in hallados if (h.pieza_id, h.ficha_id) in relevantes], vectores.modelo


CONSULTAS_DE_REFERENCIA = {
    "la-facultad": Consulta("historia de la Facultad de Ingeniería"),
    "actos-y-egresados": Consulta("acto de colación de grados y egresados"),
    "desde-1900": Consulta(filtro=FiltroDeBusqueda(desde=date(1900, 1, 1))),
}
"""Las 3 consultas de referencia (spec §2.3; runbooks R9 y R13): dos con texto, que embeben y
comparan vectores, y una por período, que lee el índice sin el motor."""


def consultas_de_referencia(busqueda: Busqueda) -> dict[str, list[str]]:
    """Las Piezas de cada consulta de referencia, en el orden de la lista: lo que la
    restauración probada compara con el origen y `reindexar` con lo de antes de vaciar.

    Raises:
        BuscadorNoDisponible, LimiteExcedido, IndiceNoDisponible: Como `Busqueda.buscar`.
    """
    return {
        nombre: [str(r.pieza_id) for r in busqueda.buscar(consulta).resultados]
        for nombre, consulta in CONSULTAS_DE_REFERENCIA.items()
    }


def _resultado(pieza: PiezaBuscable, fragmentos: Sequence[FragmentoHallado]) -> ResultadoDePieza:
    return ResultadoDePieza(
        pieza_id=pieza.pieza_id,
        tipo=pieza.tipo,
        tipo_actividad=pieza.tipo_actividad,
        carreras=pieza.carreras,
        fecha_inicio=pieza.fecha_inicio,
        fecha_fin=pieza.fecha_fin,
        precision_fecha=pieza.precision_fecha,
        clase_original=pieza.clase_original,
        via_ingreso=pieza.via_ingreso,
        resumen=pieza.resumen,
        nombre_archivo=pieza.nombre_archivo,
        puntaje=max(f.puntaje for f in fragmentos),
        citas=tuple(_cita(f) for f in fragmentos),
    )


_SECCIONES = {
    "principal": "Texto",
    "texto_visible": "Texto visible",
    "contexto": "Contexto",
    "resumen": "Resumen",
}


def _cita(fragmento: FragmentoHallado) -> Cita:
    legible, enlace = ubicar(fragmento.pieza_id, fragmento.ubicacion)
    return Cita(fragmento.fragmento_id, fragmento.texto, legible, enlace)


def ubicar(pieza_id: UUID, ubicacion: Mapping[str, Any]) -> tuple[str, str]:
    """La ubicación legible de una Cita y su enlace al original: también para la Cita guardada
    de una Conversación que se reabre."""
    enlace = f"/api/piezas/{pieza_id}/original"
    if "pagina" in ubicacion:
        legible = f"p. {ubicacion['pagina']}"
        enlace += f"#page={ubicacion['pagina']}"
    elif "marca" in ubicacion:
        segundos = segundos_de_marca(ubicacion["marca"])
        legible = f"min {_marca_legible(segundos)}"
        enlace += f"#t={segundos}"
    elif "hoja" in ubicacion:
        legible = f"Hoja {ubicacion['hoja']}"
        if "filas" in ubicacion:
            desde, hasta = ubicacion["filas"]
            legible += f" · filas {desde}–{hasta}"
    elif "imagen" in ubicacion:
        legible = ""  # una imagen de un Word: el Word no tiene páginas y el enlace va a la Pieza
    else:
        seccion = str(ubicacion.get("seccion", ""))
        legible = _SECCIONES.get(seccion, seccion)
    if "imagen" in ubicacion:
        legible = " · ".join(filter(None, (legible, f"Imagen {ubicacion['imagen']}")))
    return legible, enlace


def _marca_legible(segundo: int) -> str:
    """«12:30» o, desde la hora, «1:02:05»: como lo muestra un reproductor."""
    horas, resto = divmod(segundo, 3600)
    minutos, segundos = divmod(resto, 60)
    return f"{horas}:{minutos:02}:{segundos:02}" if horas else f"{minutos}:{segundos:02}"
