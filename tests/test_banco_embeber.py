"""El banco de `embeber` (RF-28, DEC-132, historias 166 y 169; ticket 077).

recall@10 en memoria: con un `embeber` de vectores puestos a mano el recall y el MRR son los
hechos a mano; el candidato con `dimension` > 1024 no corre; el comando `banco comparar
--capacidad embeber`, como `hv_app`, no cambia el índice. El último corre contra un Ollama real
(`-m real`) y se saltea sin él.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from capa_modelos_falsa import CapaModelosFalsa
from test_banco import _validada, acervo, fichas  # noqa: F401  (fixtures)
from test_informes import _crear
from test_banco_comparacion import vigente  # noqa: F401  (fixture)

from historia_viva import composicion
from historia_viva.banco.comparacion import (
    Banco,
    CandidatoDeReferencia,
    CandidatoInvalido,
    leer_candidatos,
)
from historia_viva.banco.embeber import (
    BancoDeEmbeber,
    ConsultaDeEmbeber,
    CorpusDeEmbeber,
    ResumenDeEmbeber,
    elegir,
    escribir_comparacion_embeber,
    leer_corpus,
    lista_de_piezas,
)
from historia_viva.banco.referencia import ReferenciaInvalida, cargar_referencia
from historia_viva.bd import Bd
from historia_viva.capa_modelos.configuracion import validar
from historia_viva.capa_modelos.contrato import (
    BloqueDatos,
    CapaModelos,
    Capacidad,
    ResultadoVectores,
    UsoEmbedding,
)
from historia_viva.capa_modelos.local import capa_para
from historia_viva.composicion import Entorno, main, motores
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.errores import MotorNoDisponible
from historia_viva.dominio.estados import Estado
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.indexacion import fragmentos_de

pytestmark = pytest.mark.rf("RF-28")

MOMENTO = datetime(2026, 10, 19, 10, 0).astimezone()
PIEZAS = tuple(f"p{i:02}" for i in range(1, 13))


def _uno(i: int) -> tuple[float, ...]:
    return tuple(float(j == i) for j in range(12))


# Cada fragmento `pNN` es un eje; cada consulta, un peso por eje: el coseno ordena por peso.
VECTORES = {
    **{p: _uno(i) for i, p in enumerate(PIEZAS)},
    "c1": tuple(float(12 - i) for i in range(12)),  # p01 primera, p12 última (fuera del top 10)
    "c2": tuple(float(i + 1) for i in range(12)),  # p12 primera, pero su filtro la excluye
    "c3": (10.0, 9.0, 8.0, *(1.0,) * 9),  # p03 tercera
}


class _Conocida(CapaModelosFalsa):
    """`embeber` con los vectores de `VECTORES`; con `caida`, el motor no contesta."""

    caida = False

    def embeber(self, *, textos: tuple[BloqueDatos, ...], uso: UsoEmbedding) -> ResultadoVectores:
        if self.caida:
            raise MotorNoDisponible("ollama_embeddings")
        self.embebidos.extend(t.contenido for t in textos)
        return ResultadoVectores(tuple(VECTORES[t.contenido] for t in textos), "conocido@0", 0)


class _Fabrica:
    def __init__(self, caidos: tuple[str, ...] = ()) -> None:
        self.capas: list[_Conocida] = []
        self._caidos = caidos

    def __call__(self, filas: Any, **_: Any) -> CapaModelos:
        capa = _Conocida().para(filas)
        assert isinstance(capa, _Conocida)
        capa.caida = filas[Capacidad.EMBEBER].modelo in self._caidos
        self.capas.append(capa)
        return capa


def _banco(fabrica: _Fabrica) -> BancoDeEmbeber:
    reloj = count(0.0, 0.25)
    banco = Banco(validar=partial(validar, motores=("ollama", "ollama_embeddings")),
                  fabrica_capa=fabrica)
    return BancoDeEmbeber(banco, reloj=lambda: next(reloj))


def _candidato(modelo: str, chino: bool = False, **parametros: Any) -> CandidatoDeReferencia:
    return CandidatoDeReferencia(Capacidad.EMBEBER, modelo, chino, parametros)


CORPUS = CorpusDeEmbeber(
    fragmentos=tuple((p, p) for p in PIEZAS),
    consultas=(
        ConsultaDeEmbeber("c1", "c1", ("p01", "p12"), frozenset(PIEZAS)),
        ConsultaDeEmbeber("c2", "c2", ("p11",), frozenset(PIEZAS) - {"p12"}),
        ConsultaDeEmbeber("c3", "c3", ("p03",), frozenset(PIEZAS)),
    ),
)


def test_con_un_embeber_conocido_el_recall_y_el_mrr_son_los_hechos_a_mano(vigente):
    fabrica = _Fabrica(caidos=("nomic-embed-text",))

    bge, caido = _banco(fabrica).comparar(
        vigente, CORPUS, [_candidato("bge-m3", chino=True),
                          _candidato("nomic-embed-text", dimension=768)], momento=MOMENTO,
    )

    # A mano: (c1, p01) en 1; (c1, p12) en 12, fuera; (c2, p11) en 1, porque el filtro saca a
    # p12; (c3, p03) en 3. recall@10 = 3/4; MRR = (1/1 + 1/1 + 1/3) / 3.
    assert [c.posiciones for c in bge.por_consulta] == [
        (("p01", 1), ("p12", None)), (("p11", 1),), (("p03", 3),)
    ]
    resumen = bge.resumen
    assert (resumen.pares, resumen.recall) == (4, 0.75)
    assert resumen.mrr == pytest.approx(7 / 9)
    # Los fragmentos se embeben una vez y cada consulta aparte; el reloj avanza 250 ms por lectura.
    assert fabrica.capas[0].embebidos == [*PIEZAS, "c1", "c2", "c3"]
    assert (resumen.fragmentos, resumen.tiempo_fragmentos_ms) == (12, 250)
    assert {c.tiempo_ms for c in bge.por_consulta} == {250}
    assert bge.modelo_embedding == "conocido@0"
    # El que no pudo embeber queda con el motivo y no compite.
    assert caido.error is not None and "ollama_embeddings" in caido.error
    assert caido.resumen.recall is None
    assert elegir([resumen, caido.resumen]) == (0, None)


def test_la_lista_ordena_las_piezas_por_su_fragmento_mas_cercano_con_el_filtro():
    fragmentos = [("a", (1.0, 0.0)), ("b", (0.0, 1.0)), ("b", (0.9, 0.1)), ("c", (0.9, 0.1)),
                  ("d", (1.0, 0.0))]

    # `b` vale por su mejor fragmento; `b` y `c` empatan y los ordena el id; `d` no pasa el filtro.
    assert lista_de_piezas(fragmentos, (2.0, 0.0), frozenset("abc")) == {"a": 1, "b": 2, "c": 3}
    assert lista_de_piezas(fragmentos, (2.0, 0.0), frozenset("abcd"), k=2) == {"a": 1, "d": 2}


def _fila(modelo: str, *, chino: bool, recall: float | None, mrr: float = 0.5,
          mediana: float = 100.0) -> ResumenDeEmbeber:
    return ResumenDeEmbeber(modelo, chino, modelo, "2026-10-19", 12, 3, 4, recall, mrr, 250,
                            mediana, mediana, None if recall is not None else "caído")


def test_si_gana_un_chino_se_marca_ademas_el_mejor_no_chino_y_los_empates_por_mrr_y_tiempo():
    filas = [
        _fila("bge-m3", chino=True, recall=0.9),
        _fila("nomic-embed-text", chino=False, recall=0.8, mediana=200.0),
        _fila("mxbai-embed-large", chino=False, recall=0.8, mediana=100.0),
        _fila("granite-embedding:278m", chino=False, recall=0.8, mrr=0.4),
        _fila("snowflake-arctic-embed2", chino=False, recall=None),
    ]

    assert elegir(filas) == (0, 2)
    assert elegir(filas[1:]) == (1, 1)  # gana un no chino: es también el mejor no chino
    assert elegir(filas[:1]) == (0, None)


@pytest.mark.historia(166)
def test_un_candidato_con_dimension_mayor_que_1024_no_corre_y_tampoco_los_demas(vigente):
    fabrica = _Fabrica()

    with pytest.raises(CandidatoInvalido, match=r"qwen3-embedding:4b(.|\n)*dimension.*2560 pasa"
                                                r" de 1024"):
        _banco(fabrica).comparar(
            vigente, CORPUS,
            [_candidato("bge-m3", chino=True), _candidato("qwen3-embedding:4b", dimension=2560)],
            momento=MOMENTO,
        )

    assert all(not capa.embebidos for capa in fabrica.capas)


def _referencia(directorio: Path, piezas: dict[str, Any], consultas: str) -> Path:
    directorio.mkdir(exist_ok=True)
    (directorio / "acta.pdf").write_bytes(b"%PDF-1.4")
    (directorio / "piezas.csv").write_text(
        "referencia_id,archivo,pieza_id,tipo\n"
        + "".join(f"{r},acta.pdf,{pieza_id},acta\n" for r, pieza_id in piezas.items()),
        encoding="utf-8",
    )
    (directorio / "consultas.csv").write_text(
        "consulta_id,texto,piezas_esperadas,tipo_actividad,conjunto,sin_evidencia\n" + consultas,
        encoding="utf-8",
    )
    return directorio


def test_el_corpus_son_los_fragmentos_de_indexacion_y_el_filtro_de_cada_consulta(
    acervo, fichas, tmp_path
):
    validada = _validada(acervo, fichas)
    otra = _crear(acervo, fichas)(tipo_actividad="Consultoría")  # sin indexar
    pendiente = _crear(acervo, fichas)(Estado.PENDIENTE)
    referencia = cargar_referencia(acervo, _referencia(
        tmp_path, {"r1": validada, "r2": otra, "r3": pendiente}, (
            "a1,El convenio,r1,,ajuste,false\n"
            "a2,El convenio con filtro,r1,Transferencia,ajuste,false\n"
            "p1,La de prueba no cuenta,r2,,prueba,false\n"
        )), fichas=fichas)

    corpus = leer_corpus(acervo, referencia, fichas=fichas)

    textos = [f.texto_embebido for f in fragmentos_de(fichas.leer_para_indexar(acervo, validada))]
    assert corpus.fragmentos[:len(textos)] == tuple(("r1", t) for t in textos)
    # La no indexada también entra (es en memoria); la que no es Validada, no.
    assert {r for r, _ in corpus.fragmentos} == {"r1", "r2"}
    assert [(c.consulta_id, c.admitidas) for c in corpus.consultas] == [
        ("a1", {"r1", "r2"}), ("a2", {"r1"})
    ]


def test_una_pieza_esperada_que_no_es_validada_o_sin_consultas_de_ajuste_no_se_mide(
    acervo, fichas, tmp_path
):
    pendiente = _crear(acervo, fichas)(Estado.PENDIENTE)
    referencia = cargar_referencia(acervo, _referencia(
        tmp_path, {"r1": pendiente}, "a1,El convenio,r1,,ajuste,false\n"
    ), fichas=fichas)

    with pytest.raises(ReferenciaInvalida, match="a1: la Pieza esperada r1 no es Validada"):
        leer_corpus(acervo, referencia, fichas=fichas)
    with pytest.raises(ReferenciaInvalida, match="no tiene consultas de ajuste"):
        leer_corpus(acervo, dataclasses.replace(referencia, consultas=()), fichas=fichas)


# El comando, como `hv_app`.


def _fragmentos_del_indice(bd: Bd) -> int:
    with bd.transaccion() as tx:
        fila = tx.execute("SELECT count(*) AS n FROM acervo.data_fragmento").fetchone()
        assert fila is not None
        return int(fila["n"])


@pytest.mark.historia(166, 169)
@pytest.mark.confirma("el comando lee las Piezas desde su propio pool")
def test_banco_comparar_embeber_por_comando_no_toca_el_indice_y_marca_ganador_y_no_chino(
    url_app, tmp_path, monkeypatch, capsys
):
    bd = Bd(url_app, tamano_pool=1)
    fichas_ = ModuloFichasPostgres(bd=bd)
    try:
        with bd.transaccion() as tx:
            piezas = {"r1": _validada(tx, fichas_), "r2": _validada(tx, fichas_)}
        referencia = _referencia(
            tmp_path / "referencia", piezas, "a1,El convenio,r1,,ajuste,false\n"
        )
        monkeypatch.setattr(composicion, "capa_para",
                            lambda filas, **_: CapaModelosFalsa().para(filas))
        monkeypatch.setenv("HV_BD_URL", url_app)
        monkeypatch.setenv("HV_REFERENCIA", str(referencia))
        antes = _fragmentos_del_indice(bd)

        assert main(["banco", "comparar", "--capacidad", "embeber"]) == 0
        assert main(["banco", "comparar", "--capacidad", "embeber", "--modelo", "nomic-embed-text",
                     "--salida", str(tmp_path / "otra")]) == 0

        assert _fragmentos_del_indice(bd) == antes
    finally:
        bd.cerrar()
    salida = capsys.readouterr()
    # El doble embebe igual con todos: empatan y gana el nombre, `bge-m3`, la fila vigente.
    assert "Ganador: bge-m3." in salida.out
    assert "queda propuesto el mejor no chino, granite-embedding:278m" in salida.out
    assert "El ganador es la fila vigente: no hace falta reindexar." in salida.out
    assert "**El ganador no es la fila vigente de embeber (bge-m3): cambiarla exige `reindexar`**" \
        in salida.out
    assert "nomic-embed-text · a1:" in salida.err
    [corrida] = (referencia / "corridas").glob("comparar-embeber-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    assert {e["modelo"] for e in datos["evaluaciones"]} == {
        "bge-m3", "snowflake-arctic-embed2", "granite-embedding:278m", "nomic-embed-text",
        "mxbai-embed-large",
    }
    marcas = {
        f["modelo"]: (f["ganador"], f["mejor_no_chino"], f["vigente"]) for f in datos["tabla"]
    }
    assert marcas["bge-m3"] == (True, False, True)
    assert marcas["granite-embedding:278m"] == (False, True, False)
    assert all(f["recall"] == 1.0 for f in datos["tabla"])
    assert {r.suffix for r in (referencia / "corridas").iterdir()} == {".json", ".csv", ".md"}


# Con Ollama real (`-m real`).

_LOCAL = "http://localhost:11434"
_REALES = ("bge-m3", "granite-embedding:278m")
_TEXTOS = {
    "r1": "acta · mayo de 2019\n\nEl Consejo Académico aprobó el convenio de pasantías con la"
          " empresa siderúrgica de la zona.",
    "r2": "fotografia · marzo de 1975\n\nInauguración del laboratorio de ensayos de materiales.",
    "r3": "acta · junio de 2001\n\nSe otorgaron becas de ayuda económica a estudiantes de primer"
          " año.",
}


@pytest.mark.real
@pytest.mark.historia(169)
def test_bge_m3_y_un_no_chino_corren_como_candidatos_de_embeber_con_ollama_real(
    url_app, tmp_path
):
    variables = {"HV_MOTOR_URL": _LOCAL, "HV_MOTOR_EMBEDDINGS_URL": _LOCAL, **os.environ}
    motores_ = motores(Entorno.desde_variables(variables))
    faltan = motores_["ollama_embeddings"].faltantes(_REALES)
    if faltan is None or faltan:
        pytest.skip(f"Necesita un Ollama real con {', '.join(_REALES)} descargados.")
    bd = Bd(url_app, tamano_pool=1)
    try:
        vigente_ = ConfiguracionDeModelos(bd=bd, validar=lambda _: None).vigente()
    finally:
        bd.cerrar()
    banco = BancoDeEmbeber(Banco(validar=partial(validar, motores=motores_),
                                 fabrica_capa=partial(capa_para, motores=motores_)))
    todas = frozenset(_TEXTOS)
    corpus = CorpusDeEmbeber(tuple(_TEXTOS.items()), (
        ConsultaDeEmbeber("a1", "¿Cuándo se firmó el convenio de pasantías?", ("r1",), todas),
        ConsultaDeEmbeber("a2", "laboratorio de materiales", ("r2",), todas),
        ConsultaDeEmbeber("a3", "becas para estudiantes", ("r3",), todas),
    ))
    candidatos = [c for c in leer_candidatos(tmp_path)
                  if c.capacidad is Capacidad.EMBEBER and c.modelo in _REALES]

    evaluaciones = banco.comparar(vigente_, corpus, candidatos, momento=MOMENTO)

    for e in evaluaciones:
        assert e.error is None and e.resumen.recall is not None
        assert e.tiempo_fragmentos_ms and e.tiempo_fragmentos_ms > 0
    rutas, tabla = escribir_comparacion_embeber(
        tmp_path, evaluaciones, vigente=vigente_, hash_referencia="h", momento=MOMENTO
    )
    datos = json.loads(rutas[0].read_text(encoding="utf-8"))
    assert sum(f["ganador"] for f in datos["tabla"]) == 1
    assert [f["modelo"] for f in datos["tabla"] if f["mejor_no_chino"]] == [
        "granite-embedding:278m"
    ]
    print(tabla)
