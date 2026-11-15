"""M4: la checklist de la SEU para efemérides y Relatos (RNF-26; DEC-128, DEC-147, R-13).

`banco m4 --plantilla` escribe el CSV que completa la SEU (`efemeride_id, evaluador, criterio,
puntaje, no_verificable`; un Relato lleva su `rel_…` en la primera columna) y `banco m4` lo lee y
mide. Las evaluaciones viven en un archivo de `HV_REFERENCIA`, nunca en la base (DEC-97): el banco
solo lee los textos con `efemerides.textos_redactados`. M4 de las efemérides y M4 de los Relatos
se informan por separado y, dentro de cada uno, por modelo de redacción (R-13).
"""

from __future__ import annotations

import hashlib
import io
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any

from historia_viva.banco import celda_md, fila_md, porcentaje
from historia_viva.efemerides import TextoRedactado
from historia_viva.exportacion import escribir_csv
from historia_viva.planillas import leer_registros

__all__ = [
    "COLUMNAS_M4",
    "CRITERIOS_M4",
    "EVALUADORES_MINIMOS",
    "OBJETIVO_M4",
    "TEXTOS_MINIMOS",
    "Evaluacion",
    "EvaluacionesInvalidas",
    "GrupoM4",
    "MedicionM4",
    "ResultadoM4",
    "escribir_m4",
    "escribir_plantilla_m4",
    "identificador",
    "leer_evaluaciones",
    "medir_m4",
    "tabla_m4",
]

CRITERIOS_M4: Mapping[str, str] = {
    "exactitud": "Exactitud de los datos con cita",
    "verificable": "Ninguna información no verificable",
    "plantilla": "Uso de la plantilla",
    "redaccion": "Redacción",
    "pertinencia": "Pertinencia",
}
"""La checklist provisoria hasta C41 (R-13): la definitiva la fija la SEU."""
COLUMNAS_M4 = ("efemeride_id", "evaluador", "criterio", "puntaje", "no_verificable")
OBJETIVO_M4 = 3.0
"""RNF-26: promedio ≥ 3 de 4 y ninguna con información no verificable (DEC-128)."""
TEXTOS_MINIMOS = 10
EVALUADORES_MINIMOS = 2
_SI = frozenset({"si", "sí", "s", "x", "true", "1"})
_NO = frozenset({"", "no", "n", "false", "0"})
_TIPOS = {"efemeride": ("efe", "efeméride", "efemérides"), "relato": ("rel", "Relato", "Relatos")}


def identificador(texto: TextoRedactado) -> str:
    """`efe_<uuid>` o `rel_<uuid>`, como los entrega `/v1`."""
    return f"{_TIPOS[texto.tipo][0]}_{texto.texto_id}"


@dataclass(frozen=True)
class Evaluacion:
    texto_id: str
    """`efe_<uuid>` o `rel_<uuid>`."""
    evaluador: str
    criterio: str
    puntaje: int
    no_verificable: bool


class EvaluacionesInvalidas(Exception):
    """Todas las filas que no sirven juntas, para corregir el archivo de una vez."""

    def __init__(self, errores: Sequence[str]) -> None:
        super().__init__("\n".join(f"  {e}" for e in errores))
        self.errores = tuple(errores)


def leer_evaluaciones(
    contenido: bytes, nombre: str, conocidos: Iterable[str]
) -> tuple[list[Evaluacion], int]:
    """Las evaluaciones completas y cuántas filas quedan sin completar (sin puntaje ni marca).

    Raises:
        EvaluacionesInvalidas: Faltan columnas, un id no es una efeméride ni un Relato, un
            criterio no es de la checklist, un puntaje no va de 1 a 4, falta el evaluador, la
            marca no es «sí» o «no», o una evaluación se repite.
    """
    try:
        cabecera, *filas = leer_registros(io.BytesIO(contenido), nombre) or [[]]
    except ValueError as error:
        raise EvaluacionesInvalidas([str(error)]) from error
    cabecera = [c.strip().lower() for c in cabecera]
    if faltan := [c for c in COLUMNAS_M4 if c not in cabecera]:
        raise EvaluacionesInvalidas([f"Faltan las columnas: {', '.join(faltan)}."])
    columna = {c: cabecera.index(c) for c in COLUMNAS_M4}
    conocidos = set(conocidos)
    errores: list[str] = []
    evaluaciones: list[Evaluacion] = []
    vistas: set[tuple[str, str, str]] = set()
    pendientes = 0
    for numero, fila in enumerate(filas, start=2):
        celdas = {c: (fila[i] if i < len(fila) else "").strip() for c, i in columna.items()}
        # «Ana» y « ana» son la misma persona: no cuentan dos veces para el mínimo.
        celdas["evaluador"] = celdas["evaluador"].casefold()
        marca = celdas["no_verificable"].lower()
        propios: list[str] = []
        if celdas["efemeride_id"] not in conocidos:
            propios.append(f"«{celdas['efemeride_id']}» no es una efeméride ni un Relato")
        if celdas["criterio"] not in CRITERIOS_M4:
            propios.append(
                f"criterio desconocido «{celdas['criterio']}» (son {', '.join(CRITERIOS_M4)})"
            )
        if not celdas["puntaje"] and marca in ("", "no") and not propios:
            pendientes += 1
            continue
        if celdas["puntaje"] not in ("1", "2", "3", "4"):
            propios.append(f"el puntaje «{celdas['puntaje']}» tiene que ir de 1 a 4")
        if not celdas["evaluador"]:
            propios.append("falta el evaluador")
        if marca not in _SI | _NO:
            propios.append(f"no_verificable «{celdas['no_verificable']}» tiene que ser sí o no")
        clave = (celdas["efemeride_id"], celdas["evaluador"], celdas["criterio"])
        if clave in vistas:
            propios.append("repite el texto, el evaluador y el criterio de otra fila")
        vistas.add(clave)
        if propios:
            errores.append(f"Fila {numero}: " + "; ".join(propios) + ".")
            continue
        evaluaciones.append(Evaluacion(
            texto_id=clave[0], evaluador=clave[1], criterio=clave[2],
            puntaje=int(celdas["puntaje"]), no_verificable=marca in _SI,
        ))
    if errores:
        raise EvaluacionesInvalidas(errores)
    return evaluaciones, pendientes


@dataclass(frozen=True)
class ResultadoM4:
    """M4 sobre un grupo de evaluaciones: todo se calcula desde ellas."""

    evaluaciones: tuple[Evaluacion, ...]

    @property
    def por_texto(self) -> dict[str, float]:
        """El promedio de cada texto sobre todos sus evaluadores y criterios."""
        return _promedios((e.texto_id, e.puntaje) for e in self.evaluaciones)

    @property
    def por_evaluador(self) -> dict[str, float]:
        return _promedios((e.evaluador, e.puntaje) for e in self.evaluaciones)

    @property
    def por_criterio(self) -> dict[str, float]:
        return _promedios((e.criterio, e.puntaje) for e in self.evaluaciones)

    @property
    def n_textos(self) -> int:
        return len(self.por_texto)

    @property
    def n_evaluadores(self) -> int:
        return len(self.por_evaluador)

    @property
    def n_con_dos_evaluadores(self) -> int:
        """Los textos que evaluaron al menos dos personas distintas: los que cuentan para el
        mínimo de DEC-128 («≥ 10 efemérides con ≥ 2 evaluadores»)."""
        evaluadores: dict[str, set[str]] = defaultdict(set)
        for e in self.evaluaciones:
            evaluadores[e.texto_id].add(e.evaluador)
        return sum(len(personas) >= EVALUADORES_MINIMOS for personas in evaluadores.values())

    @property
    def promedio(self) -> float | None:
        """El promedio de los promedios por texto: cada texto pesa lo mismo."""
        return fmean(self.por_texto.values()) if self.evaluaciones else None

    @property
    def minimo(self) -> tuple[str, float] | None:
        """El texto con el promedio más bajo."""
        return min(self.por_texto.items(), key=lambda t: (t[1], t[0]), default=None)

    @property
    def acuerdo(self) -> tuple[float, float] | None:
        """Entre evaluadores, por pares sobre el mismo texto y criterio: la fracción con el mismo
        puntaje y la que difiere en un punto o menos. `None` sin dos evaluadores en algo."""
        puntajes: dict[tuple[str, str], list[int]] = defaultdict(list)
        for e in self.evaluaciones:
            puntajes[e.texto_id, e.criterio].append(e.puntaje)
        pares = [par for lista in puntajes.values() for par in combinations(lista, 2)]
        if not pares:
            return None
        return (
            sum(a == b for a, b in pares) / len(pares),
            sum(abs(a - b) <= 1 for a, b in pares) / len(pares),
        )

    @property
    def no_verificables(self) -> tuple[str, ...]:
        """Los textos que algún evaluador marcó con información no verificable."""
        return tuple(sorted({e.texto_id for e in self.evaluaciones if e.no_verificable}))

    @property
    def faltan(self) -> tuple[int, int]:
        """Cuántos textos con dos evaluadores y cuántos evaluadores faltan para dar veredicto
        (DEC-128): nueve textos de Ana y otro de Beto no tienen ningún par en común."""
        return (
            max(0, TEXTOS_MINIMOS - self.n_con_dos_evaluadores),
            max(0, EVALUADORES_MINIMOS - self.n_evaluadores),
        )

    @property
    def cumple(self) -> bool | None:
        """`None` sin el mínimo de textos o de evaluadores. Una sola marcada no verificable
        alcanza para no cumplir, aunque el promedio sea ≥ 3."""
        if any(self.faltan) or self.promedio is None:
            return None
        return self.promedio >= OBJETIVO_M4 and not self.no_verificables


def _promedios(pares: Iterable[tuple[str, int]]) -> dict[str, float]:
    grupos: dict[str, list[int]] = defaultdict(list)
    for clave, puntaje in pares:
        grupos[clave].append(puntaje)
    return {clave: fmean(valores) for clave, valores in sorted(grupos.items())}


@dataclass(frozen=True)
class GrupoM4:
    tipo: str
    """`efemeride` o `relato`: se informan por separado (DEC-147)."""
    modelo: str | None
    """`None` es el grupo de todos los modelos; si no, el modelo de redacción (R-13)."""
    todos: bool
    resultado: ResultadoM4


@dataclass(frozen=True)
class MedicionM4:
    grupos: tuple[GrupoM4, ...]
    pendientes: int
    """Filas del archivo sin puntaje ni marca: todavía sin completar."""
    hash_evaluaciones: str
    textos: Mapping[str, TextoRedactado]


def medir_m4(
    contenido: bytes, nombre: str, textos: Sequence[TextoRedactado]
) -> MedicionM4:
    """M4 de las efemérides y de los Relatos, cada uno en total y por modelo de redacción.

    Raises:
        EvaluacionesInvalidas: Como `leer_evaluaciones`.
    """
    por_id = {identificador(t): t for t in textos}
    evaluaciones, pendientes = leer_evaluaciones(contenido, nombre, por_id)
    grupos: list[GrupoM4] = []
    for tipo in _TIPOS:
        propias = [e for e in evaluaciones if por_id[e.texto_id].tipo == tipo]
        grupos.append(GrupoM4(tipo, None, True, ResultadoM4(tuple(propias))))
        modelos = sorted({por_id[e.texto_id].modelo or "" for e in propias})
        grupos += [
            GrupoM4(tipo, modelo or None, False, ResultadoM4(tuple(
                e for e in propias if (por_id[e.texto_id].modelo or "") == modelo
            )))
            for modelo in modelos
        ]
    return MedicionM4(
        grupos=tuple(grupos), pendientes=pendientes,
        hash_evaluaciones=hashlib.sha256(contenido).hexdigest(), textos=por_id,
    )


def _numero(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.2f}".replace(".", ",")


def _veredicto(resultado: ResultadoM4, tipo: str) -> str:
    textos, evaluadores = resultado.faltan
    if textos or evaluadores:
        faltan = (
            [f"{textos} {_TIPOS[tipo][1 if textos == 1 else 2]} con dos evaluadores"]
            if textos else []
        )
        if evaluadores:
            faltan.append(f"{evaluadores} evaluador{'' if evaluadores == 1 else 'es'}")
        return "Sin veredicto: faltan " + " y ".join(faltan)
    return "Cumple" if resultado.cumple else "No cumple"


def _nombre(grupo: GrupoM4) -> str:
    tipo = _TIPOS[grupo.tipo][2].capitalize()
    return tipo if grupo.todos else f"{tipo} · {grupo.modelo or 'modelo sin registrar'}"


def tabla_m4(medicion: MedicionM4) -> str:
    lineas = [
        "M4: checklist de la SEU de 1 a 4, efemérides y Relatos por separado (DEC-128, DEC-147).",
        "",
        ("| Grupo | Textos | Evaluadores | Promedio | Mínimo | Acuerdo exacto | Acuerdo ±1"
        " | No verificables | Veredicto |"),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for g in medicion.grupos:
        r = g.resultado
        minimo, acuerdo = r.minimo, r.acuerdo
        lineas.append(fila_md(
            _nombre(g), r.n_textos, r.n_evaluadores, _numero(r.promedio),
            "—" if minimo is None else f"{_numero(minimo[1])} ({minimo[0]})",
            porcentaje(acuerdo and acuerdo[0]), porcentaje(acuerdo and acuerdo[1]),
            len(r.no_verificables), _veredicto(r, g.tipo),
        ))
    lineas += [
        "",
        (f"Criterio: promedio ≥ {OBJETIVO_M4:.0f} y ninguno con información no verificable, sobre"
        f" ≥ {TEXTOS_MINIMOS} textos y ≥ {EVALUADORES_MINIMOS} evaluadores (DEC-128)."),
        "Checklist provisoria hasta C41 (R-13): "
        + "; ".join(f"`{c}` {d.lower()}" for c, d in CRITERIOS_M4.items()) + ".",
        f"hash_evaluaciones: {celda_md(medicion.hash_evaluaciones)}.",
    ]
    if medicion.pendientes:
        lineas += ["", f"**Aviso:** {medicion.pendientes} fila(s) sin completar no cuentan."]
    for g in (g for g in medicion.grupos if g.todos and g.resultado.evaluaciones):
        nombre = _nombre(g)
        lineas += ["", f"{nombre} por criterio:", "", "| Criterio | Promedio |", "|---|---|"]
        lineas += [fila_md(c, _numero(v)) for c, v in g.resultado.por_criterio.items()]
        lineas += ["", f"{nombre} por evaluador:", "", "| Evaluador | Promedio |", "|---|---|"]
        lineas += [fila_md(e, _numero(v)) for e, v in g.resultado.por_evaluador.items()]
        if g.resultado.no_verificables:
            lineas += ["", f"{nombre} con información no verificable: "
                       + ", ".join(celda_md(t) for t in g.resultado.no_verificables) + "."]
    return "\n".join(lineas) + "\n"


_COLUMNAS_SALIDA = ("tipo", "texto_id", "modelo", "evaluadores", "promedio", "no_verificable")


def escribir_m4(directorio: Path, medicion: MedicionM4, *, momento: datetime) -> list[Path]:
    """El JSON, el CSV (uno por texto evaluado) y la tabla de la corrida."""
    nombre = f"m4-{momento:%Y%m%dT%H%M%S}"
    directorio.mkdir(parents=True, exist_ok=True)

    def grupo(g: GrupoM4) -> dict[str, Any]:
        r = g.resultado
        return {
            "tipo": g.tipo, "modelo": g.modelo, "todos": g.todos,
            "n_textos": r.n_textos, "n_evaluadores": r.n_evaluadores,
            "n_con_dos_evaluadores": r.n_con_dos_evaluadores,
            "promedio": r.promedio, "minimo": r.minimo and list(r.minimo),
            "por_texto": r.por_texto, "por_evaluador": r.por_evaluador,
            "por_criterio": r.por_criterio, "acuerdo": r.acuerdo and list(r.acuerdo),
            "no_verificables": list(r.no_verificables),
            "faltan_textos": r.faltan[0], "faltan_evaluadores": r.faltan[1],
            "cumple": r.cumple,
        }

    datos = {
        "medida": "m4",
        "momento": momento.isoformat(),
        "hash_evaluaciones": medicion.hash_evaluaciones,
        "objetivo": OBJETIVO_M4,
        "criterios": dict(CRITERIOS_M4),
        "checklist_provisoria": True,
        "pendientes": medicion.pendientes,
        "grupos": [grupo(g) for g in medicion.grupos],
    }
    evaluadores: dict[str, set[str]] = defaultdict(set)
    marcados: set[str] = set()
    for g in medicion.grupos:
        for e in g.resultado.evaluaciones if g.todos else ():
            evaluadores[e.texto_id].add(e.evaluador)
            if e.no_verificable:
                marcados.add(e.texto_id)
    filas = (
        [medicion.textos[t].tipo, t, medicion.textos[t].modelo, sorted(evaluadores[t]),
         round(promedio, 4), t in marcados]
        for g in medicion.grupos if g.todos
        for t, promedio in g.resultado.por_texto.items()
    )
    rutas = [directorio / f"{nombre}.{extension}" for extension in ("json", "csv", "md")]
    rutas[0].write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(_COLUMNAS_SALIDA, filas))
    rutas[2].write_text(tabla_m4(medicion), encoding="utf-8")
    return rutas


def escribir_plantilla_m4(
    directorio: Path,
    textos: Sequence[TextoRedactado],
    *,
    evaluadores: Sequence[str] = (),
    momento: datetime,
) -> list[Path]:
    """El CSV que completa la SEU (una fila por texto, evaluador y criterio, con el puntaje y la
    marca en blanco) y, al lado, la lista de los textos con su título, para encontrarlos en P8."""
    nombre = f"m4-plantilla-{momento:%Y%m%dT%H%M%S}"
    directorio.mkdir(parents=True, exist_ok=True)
    filas = (
        [identificador(t), evaluador, criterio, None, None]
        for t in textos for evaluador in (evaluadores or ("",)) for criterio in CRITERIOS_M4
    )
    lista = [
        (f"Textos para la checklist de M4 ({len(textos)}). Completá `puntaje` (1 a 4) y, si el"
        " texto tiene información que las fuentes no respaldan, `no_verificable` con «sí»."),
        "",
        "| Id | Tipo | Estado | Título | Modelo |",
        "|---|---|---|---|---|",
        *(
            fila_md(identificador(t), t.tipo, t.estado.value, t.titulo, t.modelo or "—")
            for t in textos
        ),
        "",
        "Criterios: " + "; ".join(f"`{c}` {d.lower()}" for c, d in CRITERIOS_M4.items()) + ".",
    ]
    rutas = [directorio / f"{nombre}.csv", directorio / f"{nombre}.md"]
    rutas[0].write_bytes(escribir_csv(COLUMNAS_M4, filas))
    rutas[1].write_text("\n".join(lista) + "\n", encoding="utf-8")
    return rutas
