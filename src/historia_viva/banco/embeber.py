"""El banco de `embeber` (RF-28, DEC-132, historias 166 y 169; spec, módulo 27 `banco`).

recall@10 de las consultas de ajuste contra los fragmentos de las Piezas de referencia, con los
vectores en memoria: no lee ni escribe el índice (DEC-97). Los fragmentos son los que indexaría
`indexacion` con la Ficha vigente de cada Pieza de referencia Validada (`fragmentos_de`, con el
mismo texto embebido). Cada candidato embebe los fragmentos una vez y cada consulta por separado,
como la búsqueda; la lista ordena las Piezas por su fragmento más cercano (coseno), como agrupa
`busqueda.buscar`, y deja afuera las que el filtro de la consulta excluye (`excluye`, la regla de
`indice`). Se mide con el conjunto de ajuste: el de prueba es de la corrida oficial de M1, y elegir
el modelo con él lo contaminaría (DEC-54).

El candidato pasa por `Banco.preparar`, el mismo `validar` y la misma `capa_para` que producción:
una `dimension` mayor que 1024 no corre. Cambiar la fila de `embeber` exige `reindexar` (historia
166): la tabla lo avisa si el ganador no es la fila vigente.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from historia_viva.banco import (
    celda_md, fila_md, mrr, percentil, porcentaje, recall_en_k, rutas_libres,
)
from historia_viva.banco.comparacion import (
    Banco,
    CandidatoDeReferencia,
    _numero,
    _segundos,
)
from historia_viva.banco.referencia import ConjuntoDeReferencia, ReferenciaInvalida, excluye
from historia_viva.bd import Tx
from historia_viva.capa_modelos.configuracion import (
    ConfiguracionDeCapacidad,
    ConfiguracionEfectiva,
)
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONSULTA,
    ETIQUETA_CUERPO_FICHA,
    BloqueDatos,
    CapaModelos,
    Capacidad,
    UsoEmbedding,
)
from historia_viva.dominio.errores import ConfiguracionInvalida, LimiteExcedido, MotorNoDisponible
from historia_viva.exportacion import escribir_csv
from historia_viva.indexacion import fragmentos_de

if TYPE_CHECKING:
    from historia_viva.fichas.postgres import ModuloFichasPostgres

__all__ = [
    "CONJUNTO",
    "K",
    "BancoDeEmbeber",
    "ConsultaDeEmbeber",
    "CorpusDeEmbeber",
    "EvaluacionDeEmbeber",
    "ResumenDeEmbeber",
    "elegir",
    "escribir_comparacion_embeber",
    "leer_corpus",
    "lista_de_piezas",
]

K = 10
CONJUNTO = "ajuste"


@dataclass(frozen=True)
class ConsultaDeEmbeber:
    consulta_id: str
    texto: str
    esperadas: tuple[str, ...]
    """`referencia_id` de `piezas.csv`."""
    admitidas: frozenset[str]
    """Las Piezas del corpus que deja pasar su filtro."""


@dataclass(frozen=True)
class CorpusDeEmbeber:
    fragmentos: tuple[tuple[str, str], ...]
    """(`referencia_id`, texto embebido), en el orden de `piezas.csv`."""
    consultas: tuple[ConsultaDeEmbeber, ...]


def leer_corpus(
    tx: Tx, referencia: ConjuntoDeReferencia, *, fichas: ModuloFichasPostgres
) -> CorpusDeEmbeber:
    """Los fragmentos de las Piezas de referencia Validadas no retiradas y las consultas de
    ajuste con Pieza esperada, con la Ficha vigente. Lee en la transacción de quien llama; no
    escribe.

    Raises:
        ReferenciaInvalida: No hay consultas de ajuste, o una Pieza esperada no tiene
            fragmentos (no es Validada no retirada): el recall saldría más bajo sin aviso.
    """
    por_pieza = {p.pieza_id: p.referencia_id for p in referencia.piezas if p.pieza_id}
    indexables = {
        por_pieza[pieza_id]: ficha
        for pieza_id in por_pieza
        if (ficha := fichas.leer_para_indexar(tx, pieza_id)) is not None
    }
    campos = {
        por_pieza[pieza_id]: medible.ficha.campos
        for pieza_id, medible in fichas.medibles(tx, list(por_pieza)).items()
        if medible.ficha
    }
    fragmentos = tuple(
        (referencia_id, f.texto_embebido)
        for referencia_id, ficha in indexables.items()
        for f in fragmentos_de(ficha)
    )
    corpus = {referencia_id for referencia_id, _ in fragmentos}
    consultas = [
        c for c in referencia.consultas if c.conjunto == CONJUNTO and c.piezas_esperadas
    ]
    errores = [
        f"consultas.csv, {c.consulta_id}: la Pieza esperada {r} no es Validada no retirada o no"
        " tiene fragmentos: el banco de embeber no la puede encontrar."
        for c in consultas
        for r in c.piezas_esperadas
        if r not in corpus
    ]
    if not consultas:
        errores.append(
            f"consultas.csv no tiene consultas de {CONJUNTO} con Pieza esperada: el banco de"
            " embeber mide con ellas (las de prueba son de la corrida oficial de M1)."
        )
    if errores:
        raise ReferenciaInvalida(errores)
    return CorpusDeEmbeber(fragmentos, tuple(
        ConsultaDeEmbeber(
            c.consulta_id, c.texto, c.piezas_esperadas,
            frozenset(r for r in corpus if excluye(c.filtro, campos[r]) is None),
        )
        for c in consultas
    ))


def _unitario(vector: Sequence[float]) -> tuple[float, ...]:
    norma = math.sqrt(sum(x * x for x in vector))
    return tuple(x / norma for x in vector) if norma else tuple(vector)


def lista_de_piezas(
    fragmentos: Sequence[tuple[str, Sequence[float]]],
    consulta: Sequence[float],
    admitidas: frozenset[str],
    k: int = K,
) -> dict[str, int]:
    """Las `k` primeras Piezas admitidas por el coseno de su fragmento más cercano a la consulta,
    con su posición (de 1 a `k`). A igual coseno, por `referencia_id`. Pura."""
    q = _unitario(consulta)
    mejor: dict[str, float] = {}
    for referencia_id, vector in fragmentos:
        if referencia_id in admitidas:
            coseno = sum(a * b for a, b in zip(q, _unitario(vector), strict=True))
            mejor[referencia_id] = max(coseno, mejor.get(referencia_id, -math.inf))
    orden = sorted(mejor, key=lambda r: (-mejor[r], r))[:k]
    return {r: posicion for posicion, r in enumerate(orden, start=1)}


@dataclass(frozen=True)
class ConsultaEmbebida:
    consulta_id: str
    posiciones: tuple[tuple[str, int | None], ...]
    """(`referencia_id` esperada, posición en la lista o `None`)."""
    tiempo_ms: int
    """Lo que tardó en embeber la consulta."""


@dataclass(frozen=True)
class ResumenDeEmbeber:
    """Una fila de la tabla (y lo que se relee de las corridas anteriores)."""

    modelo: str
    origen_chino: bool
    digest: str
    corrida: str
    fragmentos: int
    consultas: int
    pares: int
    recall: float | None
    """recall@10, micro-promedio de los pares; `None` si no midió: no compite."""
    mrr: float | None
    tiempo_fragmentos_ms: int | None
    tiempo_mediano_consulta_ms: float | None
    tiempo_p95_consulta_ms: float | None
    error: str | None


@dataclass(frozen=True)
class EvaluacionDeEmbeber:
    candidato: CandidatoDeReferencia
    configuracion_efectiva: Mapping[str, Any]
    corrida: str
    fragmentos: int
    tiempo_fragmentos_ms: int | None
    por_consulta: tuple[ConsultaEmbebida, ...]
    modelo_embedding: str | None
    """El `modelo@digest12` que devolvió la capa."""
    error: str | None = None
    """El motor, el tope o la dimensión real cortaron la corrida: no compite."""

    @property
    def resumen(self) -> ResumenDeEmbeber:
        posiciones = [p for c in self.por_consulta for _, p in c.posiciones]
        tiempos = [c.tiempo_ms for c in self.por_consulta]
        medido = self.error is None
        return ResumenDeEmbeber(
            modelo=self.candidato.modelo,
            origen_chino=self.candidato.origen_chino,
            digest=self.configuracion_efectiva["digest"],
            corrida=self.corrida,
            fragmentos=self.fragmentos,
            consultas=len(self.por_consulta),
            pares=len(posiciones),
            recall=recall_en_k(posiciones, K) if medido else None,
            mrr=mrr([
                min((p for _, p in c.posiciones if p), default=None) for c in self.por_consulta
            ]) if medido else None,
            tiempo_fragmentos_ms=self.tiempo_fragmentos_ms,
            tiempo_mediano_consulta_ms=percentil(tiempos, 50),
            tiempo_p95_consulta_ms=percentil(tiempos, 95),
            error=self.error,
        )


class BancoDeEmbeber:
    def __init__(self, banco: Banco, *, reloj: Callable[[], float] = time.monotonic) -> None:
        """`banco` valida y arma la capa de cada candidato (`Banco.preparar`)."""
        self._banco = banco
        self._reloj = reloj

    def comparar(
        self,
        vigente: Mapping[Capacidad, ConfiguracionDeCapacidad],
        corpus: CorpusDeEmbeber,
        candidatos: Sequence[CandidatoDeReferencia],
        *,
        momento: datetime,
        progreso: Callable[[str, str], None] = lambda modelo, texto: None,
    ) -> list[EvaluacionDeEmbeber]:
        """Valida todos los candidatos antes de embeber con el primero.

        Raises:
            CandidatoInvalido: Alguna fila no pasa `validar` (por ejemplo, `dimension` > 1024).
        """
        preparados = [(c, *self._banco.preparar(vigente, c)) for c in candidatos]
        return [
            self._medir(candidato, capa, efectiva, corpus, momento.isoformat(), progreso)
            for candidato, capa, efectiva in preparados
        ]

    def _ms(self, inicio: float) -> int:
        return int((self._reloj() - inicio) * 1000)

    def _medir(
        self,
        candidato: CandidatoDeReferencia,
        capa: CapaModelos,
        efectiva: Mapping[str, Any],
        corpus: CorpusDeEmbeber,
        corrida: str,
        progreso: Callable[[str, str], None],
    ) -> EvaluacionDeEmbeber:
        medidas: list[ConsultaEmbebida] = []
        tiempo_fragmentos = None
        modelo_embedding = None
        try:
            inicio = self._reloj()
            resultado = capa.embeber(
                textos=tuple(
                    BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=texto)
                    for _, texto in corpus.fragmentos
                ),
                uso=UsoEmbedding.FRAGMENTO,
            )
            tiempo_fragmentos = self._ms(inicio)
            modelo_embedding = resultado.modelo
            progreso(candidato.modelo, f"{len(corpus.fragmentos)} fragmentos:"
                                       f" {tiempo_fragmentos / 1000:.1f} s")
            fragmentos = [
                (referencia_id, vector)
                for (referencia_id, _), vector in zip(
                    corpus.fragmentos, resultado.vectores, strict=True
                )
            ]
            for consulta in corpus.consultas:
                inicio = self._reloj()
                [vector] = capa.embeber(
                    textos=(BloqueDatos(etiqueta=ETIQUETA_CONSULTA, contenido=consulta.texto),),
                    uso=UsoEmbedding.CONSULTA,
                ).vectores
                tiempo = self._ms(inicio)
                lista = lista_de_piezas(fragmentos, vector, consulta.admitidas)
                medidas.append(ConsultaEmbebida(
                    consulta.consulta_id,
                    tuple((r, lista.get(r)) for r in consulta.esperadas),
                    tiempo,
                ))
                progreso(candidato.modelo, f"{consulta.consulta_id}: {tiempo} ms")
            error = None
        except (LimiteExcedido, MotorNoDisponible, ConfiguracionInvalida) as falla:
            error = str(falla)
            progreso(candidato.modelo, error)
        return EvaluacionDeEmbeber(
            candidato, efectiva, corrida, len(corpus.fragmentos), tiempo_fragmentos,
            tuple(medidas), modelo_embedding, error,
        )


def elegir(filas: Sequence[ResumenDeEmbeber]) -> tuple[int | None, int | None]:
    """Los índices del ganador y del mejor no chino (`None` si no hay). Gana el de más recall;
    a igual recall, más MRR, menos tiempo mediano por consulta y, por último, el nombre."""
    def clave(i: int) -> tuple[float, float, float, str, str]:
        f = filas[i]
        tiempo = f.tiempo_mediano_consulta_ms
        return (-(f.recall or 0.0), -(f.mrr or 0.0), math.inf if tiempo is None else tiempo,
                f.modelo, f.digest)

    compiten = [i for i, f in enumerate(filas) if f.recall is not None]
    no_chinos = [i for i in compiten if not filas[i].origen_chino]
    return min(compiten, key=clave, default=None), min(no_chinos, key=clave, default=None)


def _digest_vigente(vigente: Mapping[Capacidad, ConfiguracionDeCapacidad]) -> str:
    """El de la fila vigente como la arma `Banco.preparar`: un candidato idéntico tiene el mismo."""
    fila = vigente[Capacidad.EMBEBER]
    digest: str = ConfiguracionEfectiva({Capacidad.EMBEBER: fila}).a_json()["digest"]
    return digest


def _anteriores(directorio: Path, hash_referencia: str) -> tuple[list[ResumenDeEmbeber], int]:
    resumenes: list[ResumenDeEmbeber] = []
    afuera = 0
    for corrida in sorted(directorio.glob("comparar-embeber-*.json")):
        try:
            datos = json.loads(corrida.read_text(encoding="utf-8"))
            if datos["hash_referencia"] != hash_referencia:
                afuera += 1
                continue
            resumenes += [ResumenDeEmbeber(**e["resumen"]) for e in datos["evaluaciones"]]
        except (ValueError, KeyError, TypeError):
            afuera += 1
    return resumenes, afuera


def _tabla(resumenes: Sequence[ResumenDeEmbeber]) -> list[ResumenDeEmbeber]:
    """La última corrida de cada candidato (modelo y configuración), por modelo."""
    ultimas = {(r.modelo, r.digest): r for r in sorted(resumenes, key=lambda r: r.corrida)}
    return sorted(ultimas.values(), key=lambda r: (r.modelo, r.digest))


def _markdown(
    filas: Sequence[ResumenDeEmbeber], *, vigente: str, modelo_vigente: str,
    hash_referencia: str, afuera: int,
) -> str:
    ganador, no_chino = elegir(filas)
    lineas = [
        (f"Comparación de modelos para embeber (RF-28): recall@{K} de las consultas de"
        f" {CONJUNTO} contra los fragmentos de las Piezas de referencia, en memoria."),
        "",
        ("| Modelo | Origen chino | recall@10 | MRR | Pares | Fragmentos | Embeber fragmentos (s)"
        " | Consulta mediana (s) | p95 (s) | Error | Vigente | Ganador | Mejor no chino |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        *(
            fila_md(
                f.modelo, "sí" if f.origen_chino else "no", porcentaje(f.recall),
                _numero(None if f.mrr is None else round(f.mrr, 3)), f.pares, f.fragmentos,
                _segundos(f.tiempo_fragmentos_ms), _segundos(f.tiempo_mediano_consulta_ms),
                _segundos(f.tiempo_p95_consulta_ms), f.error or "",
                "sí" if f.digest == vigente else "", "**sí**" if i == ganador else "",
                "**sí**" if i == no_chino else "",
            )
            for i, f in enumerate(filas)
        ),
        "",
    ]
    if ganador is None:
        lineas.append("Ningún candidato llegó a medir: no hay ganador.")
    else:
        lineas.append(f"Ganador: {celda_md(filas[ganador].modelo)}.")
        if no_chino is None:
            lineas.append(
                "**No hay candidato no chino que haya medido:** sumá uno a candidatos.csv por si"
                " la FIE restringe los modelos de origen chino (C44)."
            )
        elif no_chino != ganador:
            lineas.append(
                f"El ganador es de origen chino: si la FIE restringe esos modelos (C44), queda"
                f" propuesto el mejor no chino, {celda_md(filas[no_chino].modelo)}."
            )
        if filas[ganador].digest != vigente:
            lineas.append(
                f"**El ganador no es la fila vigente de embeber ({celda_md(modelo_vigente)}):"
                " cambiarla exige `reindexar`**, porque la búsqueda no mezcla vectores de"
                " modelos distintos."
            )
        else:
            lineas.append("El ganador es la fila vigente: no hace falta reindexar.")
    lineas.append(f"Solo se comparan corridas con hash_referencia {celda_md(hash_referencia)}.")
    if afuera:
        lineas.append(f"{afuera} corrida(s) anteriores con otra huella, o ilegibles, quedaron"
                      " afuera.")
    return "\n".join(lineas) + "\n"


_COLUMNAS = (
    "modelo", "origen_chino", "recall", "mrr", "pares", "consultas", "fragmentos",
    "tiempo_fragmentos_ms", "tiempo_mediano_consulta_ms", "tiempo_p95_consulta_ms", "error",
    "vigente", "ganador", "mejor_no_chino", "digest", "corrida", "hash_referencia",
)


def escribir_comparacion_embeber(
    directorio: Path,
    evaluaciones: Sequence[EvaluacionDeEmbeber],
    *,
    vigente: Mapping[Capacidad, ConfiguracionDeCapacidad],
    hash_referencia: str,
    momento: datetime,
) -> tuple[list[Path], str]:
    """`comparar-embeber-<momento>.{json,csv,md}`; la tabla suma las corridas anteriores de
    `directorio` con la misma huella. Devuelve las rutas y la tabla."""
    directorio.mkdir(parents=True, exist_ok=True)
    anteriores, afuera = _anteriores(directorio, hash_referencia)
    filas = _tabla([*anteriores, *(e.resumen for e in evaluaciones)])
    ganador, no_chino = elegir(filas)
    digest_vigente = _digest_vigente(vigente)
    tabla = [
        {**asdict(f), "vigente": f.digest == digest_vigente, "ganador": i == ganador,
         "mejor_no_chino": i == no_chino}
        for i, f in enumerate(filas)
    ]
    datos = {
        "medida": "comparacion",
        "capacidad": Capacidad.EMBEBER.value,
        "momento": momento.isoformat(),
        "hash_referencia": hash_referencia,
        "k": K,
        "conjunto": CONJUNTO,
        "vigente": {"modelo": vigente[Capacidad.EMBEBER].modelo, "digest": digest_vigente},
        "evaluaciones": [
            {
                "modelo": e.candidato.modelo,
                "origen_chino": e.candidato.origen_chino,
                "configuracion_efectiva": e.configuracion_efectiva,
                "modelo_embedding": e.modelo_embedding,
                "resumen": asdict(e.resumen),
                "por_consulta": [asdict(c) for c in e.por_consulta],
            }
            for e in evaluaciones
        ],
        "tabla": tabla,
        "corridas_afuera": afuera,
    }
    nombre = f"comparar-embeber-{momento:%Y%m%dT%H%M%S}"
    rutas = rutas_libres(directorio, nombre)
    markdown = _markdown(
        filas, vigente=digest_vigente, modelo_vigente=vigente[Capacidad.EMBEBER].modelo or "",
        hash_referencia=hash_referencia, afuera=afuera,
    )
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(
        _COLUMNAS, ([*(t[c] for c in _COLUMNAS[:-1]), hash_referencia] for t in tabla)
    ))
    rutas[2].write_text(markdown, encoding="utf-8")
    return rutas, markdown
