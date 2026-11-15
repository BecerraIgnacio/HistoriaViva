"""El banco de pruebas: M1 a M4 (spec, módulo 27 `banco`; DEC-128, DEC-97).

067 trae `medir_m2`, la integridad del Informe de acreditación (M2 sobre sus Piezas, R-21,
DIR-38); 072 suma el comando `banco` (`aceptacion`) y el conjunto de referencia (`referencia`);
073, M1 y sus funciones puras (recall, Wilson, MRR, azar y percentiles).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from historia_viva.bd import Tx
from historia_viva.dominio.tipos import FichaVigente, PiezaMedible
from historia_viva.exportacion import celda_segura

if TYPE_CHECKING:
    from historia_viva.configuracion import ConfiguracionDeModelos
    from historia_viva.fichas.postgres import ModuloFichasPostgres
    from historia_viva.indice import Fragmento

__all__ = [
    "OBJETIVO_M1",
    "PARES_MINIMOS",
    "ConsultaMedida",
    "ParMedido",
    "PiezaAMedir",
    "ResultadoM1",
    "ResultadoM2",
    "azar",
    "celda_md",
    "fila_md",
    "integridad",
    "intervalo_de_wilson",
    "leer",
    "medir_m2",
    "mrr",
    "percentil",
    "porcentaje",
    "recall_en_k",
    "rutas_libres",
]

OBJETIVO_M1 = 0.80
"""RNF-11: M1 ≥ 80 % sobre el valor medido; el IC se informa y no decide (DEC-128)."""
PARES_MINIMOS = 40
"""Con menos pares del conjunto de prueba se mide igual, pero el IC es ancho (DEC-54)."""
AZAR_MAXIMO = 0.20
"""Si el azar lo supera, el corpus es tan chico que acertar no dice mucho (spec, M1)."""


def recall_en_k(posiciones: Sequence[int | None], k: int) -> float | None:
    """Micro-promedio: la fracción de pares cuya Pieza esperada está entre las `k` primeras.
    `posiciones` va de 1 en adelante; `None`, no apareció. `None` sin pares."""
    if not posiciones:
        return None
    return sum(p is not None and p <= k for p in posiciones) / len(posiciones)


def intervalo_de_wilson(aciertos: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """El intervalo de Wilson al 95 % de `aciertos` sobre `n`; `None` sin pares."""
    if n == 0:
        return None
    p, z2 = aciertos / n, z * z
    centro = (p + z2 / (2 * n)) / (1 + z2 / n)
    radio = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, centro - radio), min(1.0, centro + radio)


def mrr(mejores: Sequence[int | None]) -> float | None:
    """El promedio, por consulta, de 1 / la posición de su mejor Pieza esperada (0 si no
    apareció ninguna). `None` sin consultas."""
    if not mejores:
        return None
    return sum(1 / p for p in mejores if p) / len(mejores)


def azar(candidatos: Sequence[int], k: int) -> float | None:
    """El recall@k que daría una lista al azar: por par, `min(k, c) / c` con `c` las Piezas que
    deja pasar el filtro de su consulta; micro-promedio como el recall. `None` sin pares."""
    if not candidatos:
        return None
    return sum(min(k, c) / c if c else 0.0 for c in candidatos) / len(candidatos)


def percentil(valores: Sequence[float], p: float) -> float | None:
    """El percentil `p` (de 0 a 100) con interpolación lineal entre rangos, como el método por
    defecto de numpy. `None` sin valores."""
    if not valores:
        return None
    ordenados = sorted(valores)
    rango = (len(ordenados) - 1) * p / 100
    abajo = math.floor(rango)
    arriba = min(abajo + 1, len(ordenados) - 1)
    return ordenados[abajo] + (ordenados[arriba] - ordenados[abajo]) * (rango - abajo)


def rutas_libres(
    directorio: Path, nombre: str, extensiones: Sequence[str] = ("json", "csv", "md")
) -> list[Path]:
    """Las rutas `nombre.<extensión>` de una corrida en `directorio`. Si otra corrida del mismo
    segundo ya las ocupa, `nombre_2`, `nombre_3`…: no la pisa y ordena después de ella (168)."""
    # shortcut: `_10` ordena antes que `_2`; diez corridas en el mismo segundo no pasan.
    libre, n = nombre, 1
    while any((directorio / f"{libre}.{e}").exists() for e in extensiones):
        n += 1
        libre = f"{nombre}_{n}"
    return [directorio / f"{libre}.{e}" for e in extensiones]


def porcentaje(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.1%}".replace(".", ",").replace("%", " %")


def celda_md(valor: object) -> str:
    """Como en el CSV, por `celda_segura`; la barra no corta la columna de la tabla."""
    return str(celda_segura(valor)).replace("|", "\\|")


def fila_md(*celdas: object) -> str:
    return "| " + " | ".join(celda_md(c) for c in celdas) + " |"


@dataclass(frozen=True)
class ParMedido:
    """Un par (consulta, Pieza esperada) de M1."""

    referencia_id: str
    pieza_id: UUID
    posicion: int | None
    """En la lista de Piezas de `busqueda.buscar`, de 1 a k; `None` si no está."""
    en_fragmentos: bool
    """Si alguno de los k fragmentos de `buscar_fragmentos` (`/v1`) es de ella: no es M1."""


@dataclass(frozen=True)
class ConsultaMedida:
    consulta_id: str
    candidatos: int
    """Las Piezas buscables que deja pasar su filtro: la base del azar."""
    pares: tuple[ParMedido, ...]

    @property
    def mejor(self) -> int | None:
        return min((p.posicion for p in self.pares if p.posicion), default=None)


@dataclass(frozen=True)
class ResultadoM1:
    conjunto: str
    k: int
    tamano_corpus: int
    """Las Validadas no retiradas al medir."""
    por_consulta: tuple[ConsultaMedida, ...]
    indice_incompleto: bool = False
    """Alguna búsqueda avisó `indice_incompleto`: hay Validadas que el modelo no cubre."""

    @property
    def _pares(self) -> list[ParMedido]:
        return [p for c in self.por_consulta for p in c.pares]

    @property
    def n_consultas(self) -> int:
        return len(self.por_consulta)

    @property
    def n_pares(self) -> int:
        return len(self._pares)

    @property
    def recuperados(self) -> int:
        return sum(p.posicion is not None for p in self._pares)

    @property
    def recall(self) -> float | None:
        """M1: recall@k sobre las k primeras Piezas, micro-promedio."""
        return recall_en_k([p.posicion for p in self._pares], self.k)

    @property
    def ic95(self) -> tuple[float, float] | None:
        return intervalo_de_wilson(self.recuperados, self.n_pares)

    @property
    def recall_3(self) -> float | None:
        return recall_en_k([p.posicion for p in self._pares], 3)

    @property
    def recall_5(self) -> float | None:
        return recall_en_k([p.posicion for p in self._pares], 5)

    @property
    def mrr(self) -> float | None:
        return mrr([c.mejor for c in self.por_consulta])

    @property
    def azar(self) -> float | None:
        return azar([c.candidatos for c in self.por_consulta for _ in c.pares], self.k)

    @property
    def recall_fragmentos(self) -> float | None:
        """Informativo, el camino de `/v1`: no es M1 (DEC-89)."""
        pares = self._pares
        return sum(p.en_fragmentos for p in pares) / len(pares) if pares else None

    @property
    def cumple(self) -> bool:
        """Sobre el valor medido, sin piso por el IC (DEC-128)."""
        return self.recall is not None and self.recall >= OBJETIVO_M1

    @property
    def avisos(self) -> tuple[str, ...]:
        avisos = []
        if self.n_pares < PARES_MINIMOS:
            avisos.append(
                f"Con {self.n_pares} pares (menos de {PARES_MINIMOS}) el IC es ancho: el valor"
                " se informa igual (DEC-128)."
            )
        if self.ic95 and self.ic95[0] <= OBJETIVO_M1 <= self.ic95[1]:
            avisos.append(
                f"El IC contiene el {OBJETIVO_M1:.0%}: no decide, el criterio se lee sobre el"
                " valor medido (DEC-128)."
            )
        if (valor := self.azar) is not None and valor > AZAR_MAXIMO:
            avisos.append(
                f"El azar supera el {AZAR_MAXIMO:.0%}: con este corpus una lista al azar ya"
                " acierta mucho."
            )
        if self.indice_incompleto:
            avisos.append(
                "La búsqueda avisó índice incompleto: hay Validadas sin fragmentos del modelo"
                " vigente, y M1 sale más bajo."
            )
        return tuple(a.replace("%", " %") for a in avisos)


@dataclass(frozen=True, kw_only=True)
class PiezaAMedir:
    """Una Pieza con lo que M2 compara: su Ficha vigente y lo que el índice tiene de ella."""

    pieza_id: UUID
    validada: bool
    """Validada y no retirada hoy."""
    ficha: FichaVigente | None
    huella: str | None
    fragmentos: tuple[Fragmento, ...]
    fragmentos_ilegibles: int = 0
    """Filas del índice cuyos metadatos no tienen la forma de `indice`: fallan P3."""


@dataclass(frozen=True)
class ResultadoM2:
    piezas_total: int
    fallas: Mapping[UUID, tuple[str, ...]]
    """Cada Pieza que no cumple P1, P2 o P3, con qué falló."""
    p1_resto: Mapping[UUID, tuple[str, ...]]
    """Los demás obligatorios que le faltan a cada Validada: se informan aparte y no cuentan en
    el valor (DEC-128)."""
    violaciones_rf15: tuple[UUID, ...] = ()
    """Piezas con fragmentos que no son Validadas no retiradas: una sola invalida la medición
    (spec, M2). La llena `Aceptacion`; `medir_m2` no ve el índice entero."""
    piezas: tuple[UUID, ...] = ()
    """Las medidas, en orden: el resultado por Pieza de la corrida (DEC-97)."""
    modelo: str | None = None
    """El `modelo_embedding` contra el que se midió P3."""

    @property
    def sin_indexar(self) -> tuple[UUID, ...]:
        """Las Validadas sin fragmentos: fallan P2 y se informan aparte."""
        return tuple(p for p, motivos in self.fallas.items() if "p2.sin_fragmentos" in motivos)

    @property
    def piezas_completas(self) -> int:
        return self.piezas_total - len(self.fallas)

    @property
    def valor(self) -> float | None:
        """`None` sin Piezas: no hay qué medir."""
        return self.piezas_completas / self.piezas_total if self.piezas_total else None

    @property
    def partes(self) -> dict[str, int]:
        """Cuántas Piezas cumplen P1, P2 y P3 por separado; una no Validada no cumple ninguna."""
        return {
            parte: self.piezas_total - sum(
                any(m == "no_validada" or m.startswith(f"{parte}.") for m in motivos)
                for motivos in self.fallas.values()
            )
            for parte in ("p1", "p2", "p3")
        }


def medir_m2(piezas: Sequence[PiezaAMedir], *, modelo: str | None = None) -> ResultadoM2:
    """M2 sobre `piezas`: la fracción que es Validada y cumple P1 (Fecha, tipo de actividad,
    origen y Propietario presentes, y los fragmentos con los de la Ficha), P2 (sin duplicados)
    y P3 (contenido íntegro). Una Validada sin indexar cuenta como falla de P2 y no impide
    medir. Un solo modelo para todas, `modelo` (el vigente): los fragmentos de otro no los ve
    la búsqueda (DEC-02); sin él, el más frecuente. Pura."""
    if modelo is None:
        modelos = Counter(f.modelo_embedding for p in piezas for f in p.fragmentos)
        modelo = modelos.most_common(1)[0][0] if modelos else None
    huellas = Counter(p.huella for p in piezas if p.huella and p.validada)
    fallas = {
        p.pieza_id: motivos
        for p in piezas
        if (motivos := _fallas(p, modelo=modelo, huella_repetida=huellas[p.huella or ""] > 1))
    }
    p1_resto = {
        p.pieza_id: faltan
        for p in piezas
        if p.validada and p.ficha
        and (faltan := tuple(c for c in _RESTO if not _presente(getattr(p.ficha.campos, c))))
    }
    return ResultadoM2(
        piezas_total=len(piezas), fallas=fallas, p1_resto=p1_resto,
        piezas=tuple(p.pieza_id for p in piezas),
        modelo=modelo,
    )


_RESTO = ("tipo", "precision_fecha", "resumen", "archivo_original")
"""Los obligatorios que M2 informa aparte (`p1_resto`)."""


def _fallas(pieza: PiezaAMedir, *, modelo: str | None, huella_repetida: bool) -> tuple[str, ...]:
    if not pieza.validada or pieza.ficha is None:
        return ("no_validada",)
    ficha, fragmentos = pieza.ficha, pieza.fragmentos
    c = ficha.campos
    fallas = [
        f"p1.{nombre}"
        for nombre, presente in (
            ("fecha", c.fecha_inicio is not None and c.fecha_fin is not None),
            ("tipo_actividad", _presente(c.tipo_actividad)),
            ("origen", _presente(c.origen)),
            ("propietario", _presente(c.propietario)),
        )
        if not presente
    ]
    # Lo que falta en la Ficha ya falló: se compara solo lo que tiene.
    de_la_ficha = {
        "tipo": c.tipo, "tipo_actividad": c.tipo_actividad,
        "fecha_inicio": c.fecha_inicio, "fecha_fin": c.fecha_fin,
    }
    if any(
        valor and getattr(f, campo) != valor
        for f in fragmentos
        for campo, valor in de_la_ficha.items()
    ):
        fallas.append("p1.fragmento")
    if not fragmentos:
        fallas.append("p2.sin_fragmentos")
    tramos = [(f.inicio, f.fin) for f in fragmentos]
    textos = [(repr(sorted(f.ubicacion.items())), f.texto) for f in fragmentos]
    if len(set(tramos)) < len(tramos) or len(set(textos)) < len(textos):
        fallas.append("p2.repetido")
    if any(f.ficha_id != ficha.ficha_id for f in fragmentos):
        fallas.append("p2.version_no_vigente")
    if huella_repetida:
        fallas.append("p2.huella_repetida")
    if fragmentos and not _cubre(ficha.cuerpo, tramos):
        fallas.append("p3.cobertura")
    if any(f.texto != _citable(f, ficha) for f in fragmentos):
        fallas.append("p3.texto")
    if pieza.fragmentos_ilegibles:
        fallas.append("p3.metadatos")
    # Sin NaN: pgvector no admite NaN en un `vector`, la base ya lo rechaza.
    if any(f.modelo_embedding != modelo for f in fragmentos):
        fallas.append("p3.modelo")
    return tuple(fallas)


def _presente(valor: object) -> bool:
    return bool(valor and str(valor).strip())


def _es_resumen(fragmento: Fragmento) -> bool:
    """El de un video o un Aporte sin texto: no es un tramo del cuerpo (`inicio == fin == 0`)."""
    return fragmento.ubicacion.get("seccion") == "resumen"


def _citable(fragmento: Fragmento, ficha: FichaVigente) -> str | None:
    if _es_resumen(fragmento):
        return ficha.campos.resumen
    return ficha.cuerpo[fragmento.inicio : fragmento.fin]


def _cubre(cuerpo: str, tramos: Sequence[tuple[int, int]]) -> bool:
    """La unión de los `[inicio, fin)` es el cuerpo entero, sin huecos."""
    hasta = 0
    for inicio, fin in sorted(t for t in tramos if t != (0, 0)):
        if inicio > hasta:
            return False
        hasta = max(hasta, fin)
    return hasta == len(cuerpo)


def leer(tx: Tx, pieza_ids: Sequence[UUID], *, fichas: ModuloFichasPostgres) -> list[PiezaAMedir]:
    """Lo que `medir_m2` necesita de cada Pieza, en la transacción de quien llama: la Ficha
    vigente de `fichas` y los fragmentos de `indice`, leídos fila por fila: una con metadatos
    ilegibles deja a su Pieza fallando, no corta la medición."""
    # Como en `indexacion`: importar `banco` (lo hace `composicion`) no trae `indice` (ticket 130).
    from historia_viva.indice import filas_de_fragmentos

    medibles = fichas.medibles(tx, pieza_ids)
    fragmentos: dict[UUID, list[Fragmento]] = {}
    ilegibles: Counter[UUID] = Counter()
    for fila in filas_de_fragmentos(tx, pieza_ids):
        try:
            fragmentos.setdefault(fila.pieza_id, []).append(fila.leer())
        except (ValueError, KeyError, TypeError, AttributeError):  # metadatos no confiables
            ilegibles[fila.pieza_id] += 1
    sin_pieza = PiezaMedible(pieza_id=UUID(int=0), validada=False, huella=None, ficha=None)
    return [
        PiezaAMedir(
            pieza_id=pieza_id,
            validada=medible.validada,
            ficha=medible.ficha,
            huella=medible.huella,
            fragmentos=tuple(
                sorted(fragmentos.get(pieza_id, ()), key=lambda f: (f.inicio, f.fragmento_id))
            ),
            fragmentos_ilegibles=ilegibles[pieza_id],
        )
        for pieza_id in pieza_ids
        for medible in (medibles.get(pieza_id, sin_pieza),)
    ]


def modelo_vigente(tx: Tx, configuracion: ConfiguracionDeModelos) -> str | None:
    """El `modelo_embedding` de `embeber` con más fragmentos, como la búsqueda sin texto. Si
    ningún fragmento es de ese modelo, `<modelo>@`: ninguno coincide y todos fallan P3."""
    from historia_viva.capa_modelos.contrato import Capacidad
    from historia_viva.indice import modelo_mayoritario

    fila = configuracion.vigente(tx).get(Capacidad.EMBEBER)
    if fila is None:
        return None  # sin fila no hay con qué comparar: el más frecuente
    prefijo = f"{fila.modelo}@"
    return modelo_mayoritario(tx, prefijo) or prefijo


def integridad(
    tx: Tx,
    pieza_ids: Sequence[UUID],
    *,
    fichas: ModuloFichasPostgres,
    configuracion: ConfiguracionDeModelos,
) -> float | None:
    """M2 sobre `pieza_ids` contra el modelo vigente: la integridad del Informe de acreditación
    (R-21, DIR-38). La inyecta `composicion` en `informes`, que así no depende de `banco`."""
    piezas = leer(tx, pieza_ids, fichas=fichas)
    return medir_m2(piezas, modelo=modelo_vigente(tx, configuracion)).valor
