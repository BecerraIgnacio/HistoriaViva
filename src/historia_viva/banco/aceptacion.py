"""La aceptación del banco: M1 a M4 y la salida de cada corrida (spec, módulo 27 `banco`).

Sin tablas (DEC-97): cada corrida escribe un JSON, un CSV por `exportacion.escribir_csv` y una
tabla markdown, con `hash_referencia` y la configuración efectiva. Toda celda pasa por
`celda_segura`, también las de la tabla. No consulta el interruptor: no encola nada, y medir con
la cola frenada es justamente para qué está el freno (DEC-96). Lo arma
`composicion.componer_banco`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from historia_viva.banco import (
    OBJETIVO_M1,
    ConsultaMedida,
    ParMedido,
    ResultadoM1,
    ResultadoM2,
    celda_md,
    fila_md,
    leer,
    medir_m2,
    modelo_vigente,
    porcentaje,
)
from historia_viva.banco.m3 import CONSULTAS, CargaDeFoto, ResultadoM3, medir
from historia_viva.banco.m4 import MedicionM4, escribir_plantilla_m4, medir_m4
from historia_viva.banco.referencia import ConjuntoDeReferencia, excluye
from historia_viva.bd import Tx
from historia_viva.busqueda import Consulta
from historia_viva.efemerides import textos_redactados
from historia_viva.exportacion import escribir_csv

if TYPE_CHECKING:
    from historia_viva.busqueda import Busqueda
    from historia_viva.conversacion import Conversaciones
    from historia_viva.configuracion import ConfiguracionDeModelos
    from historia_viva.fichas.postgres import ModuloFichasPostgres

__all__ = [
    "OBJETIVO_M2",
    "Aceptacion",
    "EsperadasNoValidadas",
    "IndexacionPendiente",
    "escribir_m1",
    "escribir_m2",
    "es_oficial",
    "tabla_m1",
    "tabla_m2",
]

OBJETIVO_M2 = 0.95
"""RNF-12: M2 ≥ 95 %; la integridad de un informe, > 95 % (DIR-38)."""

_COLUMNAS = ("pieza_id", "cumple", "fallas", "p1_resto")


class IndexacionPendiente(Exception):
    def __init__(self, pendientes: int) -> None:
        super().__init__(
            f"Hay {pendientes} indexación(es) en cola o en curso: M1, y M2 sobre todas las"
            " Validadas, se miden con el índice al día. Esperá a que termine el trabajador"
            " `--tipos indexacion` (M2 de un informe, con --informe, mide igual)."
        )
        self.pendientes = pendientes


class EsperadasNoValidadas(Exception):
    """Una Pieza esperada no es Validada no retirada: la búsqueda nunca la devuelve y M1 saldría
    más bajo sin que nadie lo note. Se corrige la referencia o el Acervo antes de medir."""

    def __init__(self, faltan: Sequence[tuple[str, str, UUID]]) -> None:
        super().__init__(
            "M1 no se mide: estas Piezas esperadas no son Validadas no retiradas, y la búsqueda"
            " no las puede devolver:" + "".join(
                f"\n  {consulta}, {referencia} ({pieza_id})" for consulta, referencia, pieza_id in faltan
            )
        )
        self.faltan = tuple(faltan)


class Aceptacion:
    def __init__(
        self,
        *,
        fichas: ModuloFichasPostgres,
        configuracion: ConfiguracionDeModelos,
        pendientes: Callable[[Tx], int],
        busqueda: Busqueda | None = None,
        conversaciones: Conversaciones | None = None,
        descargar: Callable[[], None] | None = None,
        describir_foto: Callable[[Path], Callable[[], object]] | None = None,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        """`busqueda` es la de `web`, con la capa vigente: M1 mide la lista que ve la persona
        (DEC-89). Sin ella, solo M2. M3 usa `conversaciones` (la de `web`), `descargar` (B: los
        modelos fuera de memoria) y `describir_foto` (C: una descripción de la foto por
        llamada)."""
        self._conversaciones = conversaciones
        self._descargar = descargar
        self._describir_foto = describir_foto
        self._reloj = reloj
        self._fichas = fichas
        self._configuracion = configuracion
        self._pendientes = pendientes
        self._busqueda = busqueda

    def medir_m1(
        self, tx: Tx, referencia: ConjuntoDeReferencia, conjunto: str, *, k: int = 10
    ) -> ResultadoM1:
        """M1 sobre las consultas de `conjunto` con Pieza esperada (las de control no cuentan):
        por cada una, las k primeras Piezas de `busqueda.buscar` con su filtro y, aparte, los k
        primeros fragmentos de `buscar_fragmentos`. Se niega a medir con el índice a medio hacer
        o con una Pieza esperada que la búsqueda no puede devolver.

        Raises:
            IndexacionPendiente: Hay indexaciones en cola o en curso.
            EsperadasNoValidadas: Alguna Pieza esperada no es Validada no retirada.
            BuscadorNoDisponible, IndiceNoDisponible, LimiteExcedido: Como la búsqueda.
        """
        if self._busqueda is None:
            raise RuntimeError("M1 necesita la búsqueda: la arma `componer_banco`.")
        if pendientes := self._pendientes(tx):
            raise IndexacionPendiente(pendientes)
        corpus = self._fichas.ids_indexables(tx)
        validas = set(corpus)
        # `cargar_referencia` ya rechazó la Pieza esperada sin `pieza_id`.
        por_referencia = {p.referencia_id: p.pieza_id for p in referencia.piezas if p.pieza_id}
        consultas = [
            c for c in referencia.consultas if c.conjunto == conjunto and c.piezas_esperadas
        ]
        faltan = [
            (c.consulta_id, r, pieza_id)
            for c in consultas
            for r in c.piezas_esperadas
            if (pieza_id := por_referencia[r]) not in validas
        ]
        if faltan:
            raise EsperadasNoValidadas(faltan)
        campos = [m.ficha.campos for m in self._fichas.medibles(tx, corpus).values() if m.ficha]
        medidas: list[ConsultaMedida] = []
        incompleto = False
        for c in consultas:
            resultado = self._busqueda.buscar(Consulta(c.texto, c.filtro, limite=k))
            incompleto |= resultado.indice_incompleto
            lista = [r.pieza_id for r in resultado.resultados[:k]]
            fragmentos = {
                h.pieza_id for h, _ in self._busqueda.buscar_fragmentos(
                    Consulta(c.texto, c.filtro), limite=k
                )
            }
            pares = tuple(
                ParMedido(
                    referencia_id=r,
                    pieza_id=pieza_id,
                    posicion=lista.index(pieza_id) + 1 if pieza_id in lista else None,
                    en_fragmentos=pieza_id in fragmentos,
                )
                for r in c.piezas_esperadas
                for pieza_id in (por_referencia[r],)
            )
            medidas.append(ConsultaMedida(
                consulta_id=c.consulta_id,
                candidatos=sum(excluye(c.filtro, cf) is None for cf in campos),
                pares=pares,
            ))
        return ResultadoM1(
            conjunto=conjunto, k=k, tamano_corpus=len(corpus), por_consulta=tuple(medidas),
            indice_incompleto=incompleto,
        )

    def medir_m3(
        self,
        referencia: ConjuntoDeReferencia,
        conjunto: str,
        condicion: str,
        *,
        cpu: str,
        foto: Path | None = None,
        consultas: int | None = None,
    ) -> ResultadoM3:
        """M3 sobre las consultas de `conjunto` con Pieza esperada, sin guardar nada. Corre sin
        transacción abierta: un turno llama a modelos (DEC-99).

        Raises:
            ValueError: El conjunto no tiene consultas, o C sin foto.
            CargaNoArranca: En C, la primera descripción de la foto falló.
            BuscadorNoDisponible, IndiceNoDisponible, LimiteExcedido: Como la Conversación.
        """
        conversaciones, descargar = self._conversaciones, self._descargar
        if conversaciones is None or descargar is None or self._describir_foto is None:
            raise RuntimeError("M3 necesita la Conversación y los motores: los arma"
                               " `componer_banco`.")
        lista = [c for c in referencia.consultas if c.conjunto == conjunto and c.piezas_esperadas]
        if not lista:
            raise ValueError(f"El conjunto de {conjunto} no tiene consultas con Pieza esperada.")
        if condicion == "C" and foto is None:
            raise ValueError("La condición C necesita la foto que se describe en bucle (--foto).")
        efectiva = self._configuracion.instantanea().a_json()
        carga = CargaDeFoto(self._describir_foto(foto)) if condicion == "C" and foto else None
        with carga or nullcontext():
            muestras = medir(
                lambda texto, filtro, historial: conversaciones.enviar_mensaje(
                    None, principal=None, texto=texto, filtro=filtro, persistir=False,
                    historial=historial,
                ),
                lista,
                n=CONSULTAS[condicion] if consultas is None else consultas,
                reloj=self._reloj,
                antes_de_cada_turno=descargar if condicion == "B" else lambda: None,
                calentar=condicion != "B",
            )
        return ResultadoM3(
            condicion=condicion,
            muestras=tuple(muestras),
            cpu=cpu,
            num_thread={
                capacidad: fila["parametros"].get("num_thread")
                for capacidad, fila in efectiva["por_capacidad"].items()
            },
            carga=None if carga is None else carga.informe(),
        )

    def medir_m2(self, tx: Tx, piezas: Sequence[UUID] | None = None) -> ResultadoM2:
        """Sin `piezas`, sobre todas las Validadas no retiradas, y se niega con indexaciones
        pendientes; con `piezas` (las de un informe) mide igual que la integridad que muestra
        P9: una Validada sin indexar es una falla de P2. En los dos casos marca las violaciones
        de RF-15 de todo el índice.

        Raises:
            IndexacionPendiente: Sin `piezas`, con indexaciones en cola o en curso.
        """
        from historia_viva.indice import ids_de_piezas  # sin cargar llama-index (ticket 130)

        validas = self._fichas.ids_indexables(tx)
        if piezas is None:
            if pendientes := self._pendientes(tx):
                raise IndexacionPendiente(pendientes)
            piezas = validas
        resultado = medir_m2(
            leer(tx, piezas, fichas=self._fichas), modelo=modelo_vigente(tx, self._configuracion)
        )
        return replace(
            resultado, violaciones_rf15=tuple(sorted(ids_de_piezas(tx) - set(validas)))
        )

    def medir_m4(self, tx: Tx, evaluaciones: Path) -> MedicionM4:
        """M4 con las evaluaciones del archivo sobre los textos de la base, que solo se leen.

        Raises:
            FileNotFoundError: No está el archivo.
            EvaluacionesInvalidas: Alguna fila no sirve.
        """
        return medir_m4(evaluaciones.read_bytes(), evaluaciones.name, textos_redactados(tx))

    def plantilla_m4(
        self, tx: Tx, directorio: Path, *, evaluadores: Sequence[str], momento: datetime
    ) -> list[Path]:
        """La plantilla de la checklist con todas las efemérides y todos los Relatos."""
        return escribir_plantilla_m4(
            directorio, textos_redactados(tx), evaluadores=evaluadores, momento=momento
        )

    def configuracion_efectiva(self, tx: Tx) -> dict[str, Any]:
        return self._configuracion.instantanea(tx).a_json()


def tabla_m2(
    resultado: ResultadoM2,
    *,
    hash_referencia: str,
    informe_id: UUID | None,
) -> str:
    alcance = f"informe {informe_id}" if informe_id else "todas las Validadas no retiradas"
    partes = resultado.partes
    lineas = [
        f"M2 sobre {alcance}.",
        "",
        "| M2 | Completas | Piezas | P1 | P2 | P3 | Sin indexar | Violaciones de RF-15 |",
        "|---|---|---|---|---|---|---|---|",
        fila_md(
            porcentaje(resultado.valor), resultado.piezas_completas, resultado.piezas_total,
            partes["p1"], partes["p2"], partes["p3"], len(resultado.sin_indexar),
            len(resultado.violaciones_rf15),
        ),
        "",
        f"Objetivo: M2 ≥ {OBJETIVO_M2:.0%}; la integridad de un informe, más de"
        f" {OBJETIVO_M2:.0%} (DIR-38).".replace("%", " %"),
        (f"hash_referencia: {celda_md(hash_referencia)}."
        f" modelo_embedding: {celda_md(resultado.modelo or '—')}."),
    ]
    if resultado.violaciones_rf15:
        lineas += [
            "",
            "**Medición inválida:** hay fragmentos de Piezas que no son Validadas no retiradas"
            " (RF-15): " + ", ".join(celda_md(p) for p in resultado.violaciones_rf15) + ".",
        ]
    if resultado.fallas:
        lineas += ["", "| Pieza | Fallas |", "|---|---|"]
        lineas += [fila_md(p, "; ".join(m)) for p, m in resultado.fallas.items()]
    if resultado.p1_resto:
        lineas += ["", "Obligatorios que faltan y no cuentan en M2:", "", "| Pieza | Faltan |",
                   "|---|---|"]
        lineas += [fila_md(p, "; ".join(f)) for p, f in resultado.p1_resto.items()]
    return "\n".join(lineas) + "\n"


def escribir_m2(
    directorio: Path,
    resultado: ResultadoM2,
    *,
    hash_referencia: str,
    configuracion_efectiva: Mapping[str, Any],
    informe_id: UUID | None,
    momento: datetime,
) -> list[Path]:
    """El JSON, el CSV y la tabla de una corrida, con un nombre que no pisa a otra."""
    nombre = f"m2-{momento:%Y%m%dT%H%M%S}" + (f"-informe-{informe_id}" if informe_id else "")
    directorio.mkdir(parents=True, exist_ok=True)
    por_pieza = [
        {
            "pieza_id": str(p),
            "cumple": p not in resultado.fallas,
            "fallas": list(resultado.fallas.get(p, ())),
            "p1_resto": list(resultado.p1_resto.get(p, ())),
        }
        for p in resultado.piezas
    ]
    datos = {
        "medida": "m2",
        "momento": momento.isoformat(),
        "informe_id": None if informe_id is None else str(informe_id),
        "hash_referencia": hash_referencia,
        "configuracion_efectiva": configuracion_efectiva,
        "modelo_embedding": resultado.modelo,
        "valor": resultado.valor,
        "objetivo": OBJETIVO_M2,
        "piezas_completas": resultado.piezas_completas,
        "piezas_total": resultado.piezas_total,
        "partes": resultado.partes,
        "sin_indexar": [str(p) for p in resultado.sin_indexar],
        "violaciones_rf15": [str(p) for p in resultado.violaciones_rf15],
        "valida": not resultado.violaciones_rf15,
        "por_pieza": por_pieza,
    }
    rutas = [directorio / f"{nombre}.{extension}" for extension in ("json", "csv", "md")]
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(
        escribir_csv(_COLUMNAS, ([f[c] for c in _COLUMNAS] for f in por_pieza))
    )
    rutas[2].write_text(
        tabla_m2(resultado, hash_referencia=hash_referencia, informe_id=informe_id),
        encoding="utf-8",
    )
    return rutas


_COLUMNAS_M1 = (
    "consulta_id", "referencia_id", "pieza_id", "posicion", "recuperada", "en_fragmentos",
    "candidatos",
)


def es_oficial(directorio: Path, conjunto: str, hash_referencia: str) -> bool | None:
    """La corrida del conjunto de prueba es una sola (DEC-54): con una oficial anterior de la
    misma huella en `directorio`, esta no lo es. El de ajuste no tiene corrida oficial: `None`."""
    if conjunto != "prueba":
        return None
    for anterior in directorio.glob("m1-prueba-*.json"):
        datos = json.loads(anterior.read_text(encoding="utf-8"))
        if datos.get("hash_referencia") == hash_referencia and datos.get("oficial"):
            return False
    return True


def _decimal(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.3f}".replace(".", ",")


def tabla_m1(resultado: ResultadoM1, *, hash_referencia: str, oficial: bool | None) -> str:
    ic = resultado.ic95
    corrida = {True: "oficial", False: "**no oficial**: ya hay una oficial con esta huella",
               None: "de ajuste"}[oficial]
    lineas = [
        f"M1 sobre el conjunto de {resultado.conjunto} (corrida {corrida}).",
        "",
        (f"| M1 (recall@{resultado.k}) | IC 95 % (Wilson) | Recuperados | Pares | Consultas"
        " | recall@3 | recall@5 | MRR | Azar | Recall de fragmentos | Corpus |"),
        "|---|---|---|---|---|---|---|---|---|---|---|",
        fila_md(
            porcentaje(resultado.recall),
            "—" if ic is None else f"[{porcentaje(ic[0])}; {porcentaje(ic[1])}]",
            resultado.recuperados, resultado.n_pares, resultado.n_consultas,
            porcentaje(resultado.recall_3), porcentaje(resultado.recall_5),
            _decimal(resultado.mrr), porcentaje(resultado.azar),
            porcentaje(resultado.recall_fragmentos), resultado.tamano_corpus,
        ),
        "",
        f"Criterio: M1 ≥ {OBJETIVO_M1:.0%} sobre el valor medido; el IC se informa y no decide"
        f" (DEC-128). {'Cumple' if resultado.cumple else 'No cumple'}.".replace("%", " %"),
        "El recall de fragmentos es el camino de `/v1` (`buscar_fragmentos`): no es M1 (DEC-89).",
        f"hash_referencia: {celda_md(hash_referencia)}.",
    ]
    if resultado.avisos:
        lineas += ["", *(f"**Aviso:** {celda_md(a)}" for a in resultado.avisos)]
    if fallados := [
        (c.consulta_id, p.referencia_id) for c in resultado.por_consulta for p in c.pares
        if p.posicion is None
    ]:
        lineas += ["", "| Consulta | Pieza esperada que no apareció |", "|---|---|"]
        lineas += [fila_md(c, r) for c, r in fallados]
    return "\n".join(lineas) + "\n"


def escribir_m1(
    directorio: Path,
    resultado: ResultadoM1,
    *,
    hash_referencia: str,
    configuracion_efectiva: Mapping[str, Any],
    oficial: bool | None,
    momento: datetime,
) -> list[Path]:
    """El JSON (con el resultado por consulta), el CSV (uno por par) y la tabla de la corrida."""
    nombre = f"m1-{resultado.conjunto}-{momento:%Y%m%dT%H%M%S}"
    directorio.mkdir(parents=True, exist_ok=True)
    ic = resultado.ic95
    datos = {
        "medida": "m1",
        "momento": momento.isoformat(),
        "conjunto": resultado.conjunto,
        "oficial": oficial,
        "hash_referencia": hash_referencia,
        "configuracion_efectiva": configuracion_efectiva,
        "k": resultado.k,
        "n_consultas": resultado.n_consultas,
        "n_pares": resultado.n_pares,
        "recuperados": resultado.recuperados,
        "recall": resultado.recall,
        "ic95": None if ic is None else list(ic),
        "recall_3": resultado.recall_3,
        "recall_5": resultado.recall_5,
        "mrr": resultado.mrr,
        "azar": resultado.azar,
        "recall_fragmentos": resultado.recall_fragmentos,
        "tamano_corpus": resultado.tamano_corpus,
        "objetivo": OBJETIVO_M1,
        "cumple": resultado.cumple,
        "avisos": list(resultado.avisos),
        "por_consulta": [
            {
                "consulta_id": c.consulta_id,
                "candidatos": c.candidatos,
                "pares": [
                    {
                        "referencia_id": p.referencia_id, "pieza_id": str(p.pieza_id),
                        "posicion": p.posicion, "en_fragmentos": p.en_fragmentos,
                    }
                    for p in c.pares
                ],
            }
            for c in resultado.por_consulta
        ],
    }
    filas = (
        [c.consulta_id, p.referencia_id, p.pieza_id, p.posicion, p.posicion is not None,
         p.en_fragmentos, c.candidatos]
        for c in resultado.por_consulta for p in c.pares
    )
    rutas = [directorio / f"{nombre}.{extension}" for extension in ("json", "csv", "md")]
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(_COLUMNAS_M1, filas))
    rutas[2].write_text(
        tabla_m1(resultado, hash_referencia=hash_referencia, oficial=oficial), encoding="utf-8"
    )
    return rutas
