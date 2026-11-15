"""Matriz requisito → test generada (historia 221; DEC-111, DEC-115, DEC-116; R-40; spec, Tests §9).

La matriz no se mantiene a mano (REQ-22): `matriz` recolecta la suite con `pytest --collect-only`,
lee los marcadores `rf`, `rnf` e `historia` de cada test y los cruza con `requisitos.csv`
(requisito, prioridad, categoría del Alcance del Plan e historias que lo cubren, sacadas de la
matriz de trazabilidad del spec). Un test cubre un requisito si lo nombra o si nombra una de sus
historias. El último resultado de cada test sale del `junit-*.xml` más nuevo que lo trae.

Un requisito que entra (también el que se activa con un tercero) sin test es un hueco; uno
Fuera del MVP o Desvío documentado es «diferido». Un hueco o un marcador que apunta a un
requisito inexistente hacen fallar el comando. M1 a M4 no son tests: figuran como medición
(`banco m1` a `banco m4`) con el número de su última corrida.

Herramienta de desarrollo: necesita `pytest` y la carpeta `tests`, que la imagen no trae.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from historia_viva.exportacion import escribir_csv
from historia_viva.planillas import leer_registros

RAIZ = Path(__file__).resolve().parents[3]
"""`mvp/`: de acá salen `tests` y `.resultados`."""
REQUISITOS = Path(__file__).with_name("requisitos.csv")
ULTIMA_HISTORIA = 302
"""Las historias 1 a 64 son del spec de paridad; 65 a 302, del spec del MVP."""
MARCADORES = ("rf", "rnf", "historia")
FUERA_DE_LA_SUITE_POR_DEFECTO = ("real",)
"""Marcas que el `addopts` deja afuera (`-m 'not real'`): el test existe pero no corre solo."""
MEDICIONES = {"RNF-11": "m1", "RNF-12": "m2", "RNF-10": "m3", "RNF-26": "m4"}
DIFERIDOS = ("Fuera del MVP", "Desvío documentado")
COLUMNAS = ("Requisito", "Prioridad", "Alcance", "Tests", "Suites", "Último resultado")
COLUMNAS_INVERSA = ("Test", "Suite", "Marcadores", "Requisitos", "Último resultado")


class RecoleccionFallida(Exception):
    """`pytest --collect-only` no terminó bien (por ejemplo, un test sin marcador)."""


@dataclass(frozen=True)
class Requisito:
    id: str
    prioridad: str
    alcance: str
    historias: frozenset[int]

    @property
    def diferido(self) -> bool:
        return self.alcance.startswith(DIFERIDOS)


@dataclass(frozen=True)
class Recolectado:
    nodeid: str
    marcadores: tuple[str, ...]
    """Lo que dicen sus marcas, tal cual: `RF-02`, `RNF-20`, `historia 221`."""
    por_defecto: bool = True
    """`False` si una marca lo deja fuera de la suite por defecto (`real`)."""

    @property
    def suite(self) -> str:
        return Path(self.nodeid.split("::")[0]).stem


@dataclass(frozen=True)
class Fila:
    requisito: Requisito
    tests: tuple[Recolectado, ...]
    medicion: str | None
    """`banco mN: <último número>` o `None` si el requisito no es una medición."""

    @property
    def hueco(self) -> bool:
        return not self.requisito.diferido and not self.tests and self.medicion is None


@dataclass(frozen=True)
class Matriz:
    filas: tuple[Fila, ...]
    tests: tuple[Recolectado, ...]
    resultados: Mapping[str, str]
    """`nodeid → pasa | falla | error | salteado`; el que falta no corrió."""
    inexistentes: Mapping[str, tuple[str, ...]]
    """`nodeid → marcadores` que no son ningún requisito ni historia."""
    cubre: Mapping[str, tuple[str, ...]]
    """`nodeid → requisitos` que cubre: la matriz inversa."""

    @property
    def huecos(self) -> list[str]:
        return [f.requisito.id for f in self.filas if f.hueco]

    @property
    def falla(self) -> bool:
        return bool(self.huecos or self.inexistentes)


def leer_requisitos(archivo: Path = REQUISITOS) -> list[Requisito]:
    with archivo.open("rb") as entrada:
        encabezado, *registros = leer_registros(entrada, archivo.name)
    filas = [dict(zip(encabezado, r, strict=True)) for r in registros]
    return [
        Requisito(f["requisito"], f["prioridad"], f["alcance"], _historias(f["historias"]))
        for f in filas
    ]


def _historias(texto: str) -> frozenset[int]:
    numeros: set[int] = set()
    for parte in filter(None, (p.strip() for p in texto.split(";"))):
        desde, _, hasta = parte.partition("-")
        numeros.update(range(int(desde), int(hasta or desde) + 1))
    return frozenset(numeros)


def recolectar(tests: Path = RAIZ / "tests") -> list[Recolectado]:
    """La suite entera, también lo que el `addopts` deja afuera (`-m ""` pisa su `-m`)."""
    import pytest

    recolectados: list[Recolectado] = []

    class Colector:
        @staticmethod
        def pytest_collection_finish(session: pytest.Session) -> None:
            recolectados.extend(_recolectado(item) for item in session.items)

    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        codigo = pytest.main(
            [str(tests), "--collect-only", "-q", "-m", "", "-p", "no:cacheprovider"],
            plugins=[Colector()],
        )
    if codigo != pytest.ExitCode.OK:
        raise RecoleccionFallida(salida.getvalue()[-4000:])
    return recolectados


def _recolectado(item: Any) -> Recolectado:
    marcadores = []
    for nombre in MARCADORES:
        for marca in item.iter_markers(nombre):
            marcadores += [f"historia {a}" if nombre == "historia" else str(a) for a in marca.args]
    return Recolectado(
        item.nodeid,
        tuple(dict.fromkeys(marcadores)),
        not any(item.get_closest_marker(m) for m in FUERA_DE_LA_SUITE_POR_DEFECTO),
    )


_GRAVEDAD = {"pasa": 0, "salteado": 1, "falla": 2, "error": 3}


def leer_resultados(directorio: Path = RAIZ / ".resultados") -> dict[tuple[str, str], str]:
    """`(classname, name) → resultado` del `junit-*.xml` más nuevo que trae cada test.

    Dentro de un archivo, un test con dos entradas (falla en la llamada y error al desarmar)
    se queda con la peor."""
    resultados: dict[tuple[str, str], str] = {}
    archivos = sorted(directorio.glob("junit-*.xml"), key=lambda a: a.stat().st_mtime)
    for archivo in archivos:
        del_archivo: dict[tuple[str, str], str] = {}
        for caso in ElementTree.parse(archivo).iter("testcase"):
            clave = (caso.get("classname", ""), caso.get("name", ""))
            hijos = {hijo.tag for hijo in caso}
            resultado = next(
                (r for t, r in (("error", "error"), ("failure", "falla"), ("skipped", "salteado"))
                 if t in hijos),
                "pasa",
            )
            previo = del_archivo.get(clave, "pasa")
            del_archivo[clave] = max(previo, resultado, key=_GRAVEDAD.__getitem__)
        resultados.update(del_archivo)
    return resultados


def clave_junit(nodeid: str) -> tuple[str, str]:
    """Cómo nombra `junitxml` a un test (`mangle_test_address` de pytest)."""
    ruta, corchete, parametros = nodeid.partition("[")
    partes = ruta.split("::")
    partes[0] = re.sub(r"\.py$", "", partes[0].replace("/", "."))
    partes[-1] += corchete + parametros
    return ".".join(partes[:-1]), partes[-1]


def ultimas_mediciones(corridas: Path | None) -> dict[str, str]:
    """`m1` a `m4` → el número de la corrida más nueva de `corridas` (por su `momento`)."""
    ultimas: dict[str, dict[str, Any]] = {}
    for archivo in sorted(corridas.glob("m[1-4]-*.json")) if corridas else ():
        try:
            datos = json.loads(archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        medida = datos.get("medida")
        if medida in MEDICIONES.values() and datos.get("momento", "") >= ultimas.get(
            medida, {}
        ).get("momento", ""):
            ultimas[medida] = datos
    return {medida: _numero(medida, datos) for medida, datos in ultimas.items()}


def _numero(medida: str, d: Mapping[str, Any]) -> str:
    fecha = str(d.get("momento", ""))[:10]
    if medida == "m1":
        ic: Sequence[float] = d.get("ic95") or []
        intervalo = f" (IC95 {_pct(ic[0])} a {_pct(ic[1])})" if len(ic) == 2 else ""
        oficial = ", oficial" if d.get("oficial") else ""
        recall = _pct(d.get("recall"))
        return f"recall@10 {recall}{intervalo}, {d.get('conjunto')}{oficial}, {fecha}"
    if medida == "m2":
        completas = f"{d.get('piezas_completas')}/{d.get('piezas_total')}"
        return f"{_pct(d.get('valor'))} ({completas} Piezas), {fecha}"
    if medida == "m3":
        p95 = (d.get("a_fuentes") or {}).get("primer_turno", {}).get("p95")
        segundos = "sin dato" if p95 is None else f"{p95 / 1000:.1f} s"
        return f"p95 hasta las fuentes {segundos}, condición {d.get('condicion')}, {fecha}"
    grupos = [
        f"{g.get('tipo')} {g.get('modelo')}: "
        + ("sin veredicto" if g.get("promedio") is None else f"{g['promedio']:.2f}")
        for g in d.get("grupos", ())
    ]
    return f"promedio {'; '.join(grupos) or 'sin grupos'}, {fecha}"


def _pct(valor: object) -> str:
    return f"{valor:.1%}" if isinstance(valor, int | float) else "sin dato"


def armar(
    requisitos: Sequence[Requisito],
    tests: Sequence[Recolectado],
    resultados: Mapping[tuple[str, str], str],
    mediciones: Mapping[str, str],
) -> Matriz:
    ids = {r.id for r in requisitos}
    cubre: dict[str, tuple[str, ...]] = {}
    inexistentes: dict[str, tuple[str, ...]] = {}
    por_requisito: dict[str, list[Recolectado]] = defaultdict(list)
    for test in tests:
        malos = tuple(m for m in test.marcadores if not _existe(m, ids))
        if malos:
            inexistentes[test.nodeid] = malos
        historias = {int(m.split()[1]) for m in test.marcadores if m.startswith("historia ")
                     and m not in malos}
        cubiertos = tuple(
            r.id for r in requisitos if r.id in test.marcadores or r.historias & historias
        )
        cubre[test.nodeid] = cubiertos
        for rid in cubiertos:
            por_requisito[rid].append(test)
    filas = tuple(
        Fila(
            r,
            tuple(por_requisito[r.id]),
            None if r.id not in MEDICIONES
            else f"banco {MEDICIONES[r.id]}: {mediciones.get(MEDICIONES[r.id], 'sin corrida')}",
        )
        for r in requisitos
    )
    return Matriz(
        filas,
        tuple(tests),
        {t.nodeid: resultados[clave_junit(t.nodeid)] for t in tests
         if clave_junit(t.nodeid) in resultados},
        inexistentes,
        cubre,
    )


def _existe(marcador: str, ids: set[str]) -> bool:
    if marcador.startswith("historia "):
        numero = marcador.split()[1]
        return numero.isdigit() and 1 <= int(numero) <= ULTIMA_HISTORIA
    return marcador in ids


def _resumen(tests: Iterable[Recolectado], resultados: Mapping[str, str]) -> str:
    cuenta = Counter(
        resultados.get(t.nodeid) or ("sin correr" if t.por_defecto else "fuera de la suite")
        for t in tests
    )
    orden = ("pasa", "falla", "error", "salteado", "sin correr", "fuera de la suite")
    return " · ".join(f"{cuenta[r]} {r}" for r in orden if cuenta[r])


def tabla(matriz: Matriz) -> list[list[str]]:
    filas = []
    for f in matriz.filas:
        n = len(f.tests)
        directos = sum(1 for t in f.tests if f.requisito.id in t.marcadores)
        tests = (
            "diferido" + (f" ({n} tests)" if n else "") if f.requisito.diferido
            else "hueco" if f.hueco else f"{n} ({directos} directos)"
        )
        suites = sorted({t.suite for t in f.tests})
        resultado = _resumen(f.tests, matriz.resultados)
        if f.medicion:
            suites.append(f"banco {MEDICIONES[f.requisito.id]}")
            resultado = " · ".join(filter(None, (resultado, f.medicion)))
        filas.append([
            f.requisito.id, f.requisito.prioridad, f.requisito.alcance, tests,
            ", ".join(suites), resultado or "—",
        ])
    return filas


def tabla_inversa(matriz: Matriz) -> list[list[str]]:
    return [
        [
            t.nodeid, t.suite, ", ".join(t.marcadores), ", ".join(matriz.cubre[t.nodeid]) or "—",
            matriz.resultados.get(t.nodeid)
            or ("sin correr" if t.por_defecto else "fuera de la suite (-m real)"),
        ]
        for t in matriz.tests
    ]


def escribir(matriz: Matriz, directorio: Path) -> list[Path]:
    """`matriz.md` (las dos tablas), `matriz.csv` y `matriz-inversa.csv` (por `exportacion`)."""
    directorio.mkdir(parents=True, exist_ok=True)
    filas, inversa = tabla(matriz), tabla_inversa(matriz)
    cubiertos = sum(1 for f in matriz.filas if not f.hueco and not f.requisito.diferido)
    diferidos = sum(1 for f in matriz.filas if f.requisito.diferido)
    texto = [
        "# Matriz requisito → test",
        "",
        (f"{len(matriz.filas)} requisitos: {cubiertos} cubiertos, {len(matriz.huecos)} huecos,"
        f" {diferidos} diferidos. {len(matriz.tests)} tests. Generada por `matriz`"
        " (historia 221): no se edita a mano. Un test cubre un requisito si lo nombra (directo)"
        " o si nombra una de sus historias."),
        "",
        *_markdown(COLUMNAS, filas),
    ]
    if matriz.inexistentes:
        texto += ["", "## Marcadores que no apuntan a ningún requisito", "",
                  *_markdown(("Test", "Marcadores"),
                             [[n, ", ".join(m)] for n, m in matriz.inexistentes.items()])]
    texto += ["", "## Matriz inversa (test → requisitos)", ""]
    texto += _markdown(COLUMNAS_INVERSA, inversa)
    rutas = [directorio / n for n in ("matriz.md", "matriz.csv", "matriz-inversa.csv")]
    rutas[0].write_text("\n".join(texto) + "\n", encoding="utf-8")
    rutas[1].write_bytes(escribir_csv(COLUMNAS, filas))
    rutas[2].write_bytes(escribir_csv(COLUMNAS_INVERSA, inversa))
    return rutas


def _markdown(columnas: Sequence[str], filas: Iterable[Sequence[str]]) -> list[str]:
    def celda(texto: str) -> str:
        return " ".join(texto.split()).replace("|", "\\|")

    return [
        "| " + " | ".join(columnas) + " |",
        "|" + "---|" * len(columnas),
        *("| " + " | ".join(celda(c) for c in fila) + " |" for fila in filas),
    ]
