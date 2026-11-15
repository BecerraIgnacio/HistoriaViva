"""M1: recall@10 con intervalo de Wilson (073; spec, módulo 27 `banco`; DEC-54, DEC-89, DEC-128).

Las funciones puras, con casos hechos a mano. Después, M1 por `componer_banco` y por el comando
sobre un mini corpus de Postgres real indexado con el `embeber` determinista de la capa falsa
(el único doble): la búsqueda es la de `web`, armada por `composicion`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_busqueda import ACTAS, EGRESADOS, bd, indexacion, trabajador, validada  # noqa: F401

from historia_viva import composicion
from historia_viva.banco import (
    ConsultaMedida,
    ParMedido,
    ResultadoM1,
    azar,
    intervalo_de_wilson,
    mrr,
    percentil,
    recall_en_k,
)
from historia_viva.banco.aceptacion import es_oficial, escribir_m1
from historia_viva.banco.referencia import cargar_referencia
from historia_viva.composicion import Entorno, componer_banco, main

pytestmark = [pytest.mark.rnf("RNF-11"), pytest.mark.historia(201)]


# ------------------------------------------------------------------ funciones puras, a mano ---


def test_recall_wilson_mrr_azar_y_percentiles_con_casos_hechos_a_mano():
    # El ejemplo del spec: 42 de 50 dan 84,0 % con IC [71,5 %; 91,7 %]; 40 de 50, 80,0 %.
    acertados = [1] * 42 + [None] * 8
    assert recall_en_k(acertados, 10) == 0.84
    assert tuple(round(x, 3) for x in intervalo_de_wilson(42, 50) or ()) == (0.715, 0.917)
    assert recall_en_k([1] * 40 + [None] * 10, 10) == 0.80
    # La posición 11 no cuenta en recall@10; la 4, sí en recall@5 y no en recall@3.
    assert recall_en_k([1, 4, 11, None], 10) == 0.5
    assert recall_en_k([1, 4, 11, None], 3) == 0.25
    assert mrr([1, 2, None, 4]) == (1 + 0.5 + 0 + 0.25) / 4
    # Por par, min(k, c) / c: con 5 candidatas siempre acierta; con 20, la mitad.
    assert azar([5, 20, 40], 10) == pytest.approx((1 + 0.5 + 0.25) / 3)
    assert percentil([4, 1, 3, 2], 50) == 2.5
    assert percentil(list(range(1, 21)), 95) == pytest.approx(19.05)
    assert (percentil([7.0], 95), percentil([1, 2], 0), percentil([1, 2], 100)) == (7, 1, 2)
    vacios = (recall_en_k([], 10), intervalo_de_wilson(0, 0), mrr([]), azar([], 10))
    assert vacios + (percentil([], 50),) == (None,) * 5


def _resultado(aciertos: int, pares: int, *, candidatos: int = 1000) -> ResultadoM1:
    """Una consulta por par; acierta en la posición 1 las primeras `aciertos`."""
    return ResultadoM1(
        conjunto="prueba", k=10, tamano_corpus=candidatos,
        por_consulta=tuple(
            ConsultaMedida(
                f"c{n}", candidatos, (ParMedido("r", uuid4(), 1 if n < aciertos else None, False),)
            )
            for n in range(pares)
        ),
    )


def test_el_criterio_se_lee_sobre_el_valor_medido_y_el_ic_solo_avisa():
    con_42 = _resultado(42, 50)
    assert (con_42.recall, con_42.cumple) == (0.84, True)
    assert con_42.avisos == (
        "El IC contiene el 80 %: no decide, el criterio se lee sobre el valor medido (DEC-128).",
    )
    assert _resultado(40, 50).cumple is True
    assert _resultado(39, 50).cumple is False
    assert _resultado(50, 50).avisos == ()  # IC [92,9 %; 100 %]: no contiene el 80 %


def test_con_menos_de_40_pares_y_un_corpus_chico_avisa():
    resultado = _resultado(10, 12, candidatos=20)

    assert resultado.n_pares == 12
    assert resultado.avisos[0].startswith("Con 12 pares (menos de 40) el IC es ancho")
    assert resultado.azar == 0.5
    assert any(a.startswith("El azar supera el 20 %") for a in resultado.avisos)


@pytest.mark.historia(205)
def test_una_segunda_corrida_de_prueba_con_la_misma_huella_no_es_oficial(tmp_path):
    resultado = _resultado(42, 50)
    assert es_oficial(tmp_path, "ajuste", "h") is None
    assert es_oficial(tmp_path, "prueba", "h") is True
    escribir_m1(
        tmp_path, resultado, hash_referencia="h", configuracion_efectiva={}, oficial=True,
        momento=_momento(1),
    )

    assert es_oficial(tmp_path, "prueba", "h") is False
    assert es_oficial(tmp_path, "prueba", "otra huella") is True


@pytest.mark.historia(168)
def test_dos_corridas_en_el_mismo_segundo_no_se_pisan(tmp_path):
    for oficial in (True, False):
        escribir_m1(
            tmp_path, _resultado(42, 50), hash_referencia="h", configuracion_efectiva={},
            oficial=oficial, momento=_momento(1),
        )

    corridas = sorted(tmp_path.glob("m1-prueba-*.json"))
    # La segunda ordena después de la primera: la última corrida es la última del `glob`.
    assert [c.name for c in corridas] == [
        "m1-prueba-20261019T100001.json", "m1-prueba-20261019T100001_2.json",
    ]
    assert [json.loads(c.read_text(encoding="utf-8"))["oficial"] for c in corridas] == [
        True, False,
    ]
    assert len(list(tmp_path.iterdir())) == 6


def _momento(segundo: int) -> datetime:
    return datetime(2026, 10, 19, 10, 0, segundo, tzinfo=UTC)


# ---------------------------------------------- composición + Postgres real + `embeber` doble ---

QUIMICA = ACTAS[2]  # «Acta del laboratorio de química», una sola página


def _referencia(directorio: Path, piezas: dict[str, str], consultas: list[str]) -> Path:
    directorio.mkdir()
    (directorio / "o.pdf").write_bytes(b"%PDF-1.4")
    (directorio / "piezas.csv").write_text(
        "referencia_id,archivo,pieza_id\n"
        + "".join(f"{r},o.pdf,{p}\n" for r, p in piezas.items()),
        encoding="utf-8",
    )
    (directorio / "consultas.csv").write_text(
        "consulta_id,texto,piezas_esperadas,desde,hasta,conjunto,sin_evidencia\n"
        + "".join(f"{c}\n" for c in consultas),
        encoding="utf-8",
    )
    return directorio


@pytest.fixture
def banco_con_la_capa_falsa(monkeypatch, url_app, capa_modelos, tmp_path):
    """El entorno del comando, con la capa falsa en lugar de los Ollama de `motores`."""
    monkeypatch.setattr(composicion, "capa_para", lambda filas, **_: capa_modelos.para(filas))
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(tmp_path / "referencia"))


@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_m1_cuenta_las_k_primeras_piezas_y_el_recall_de_fragmentos_sale_aparte(
    banco_con_la_capa_falsa, validada, tmp_path
):
    # Cuatro fragmentos de «ensayos» copan los 2 primeros de `buscar_fragmentos`; en la lista
    # de Piezas, «química» queda segunda y cuenta para recall@2.
    ensayos, quimica = validada(ACTAS[0]), validada(QUIMICA)
    referencia = _referencia(
        tmp_path / "referencia", {"r1": ensayos, "r2": quimica},
        ["c1,Acta del laboratorio de ensayos,r1;r2,,,ajuste,"],
    )
    banco_ = componer_banco(Entorno.desde_variables(os.environ))
    try:
        with banco_.bd.transaccion() as tx:
            conjunto = cargar_referencia(tx, referencia, fichas=banco_.fichas)
            con_2 = banco_.aceptacion.medir_m1(tx, conjunto, "ajuste", k=2)
            con_1 = banco_.aceptacion.medir_m1(tx, conjunto, "ajuste", k=1)
    finally:
        banco_.bd.cerrar()

    assert [(p.posicion, p.en_fragmentos) for p in con_2.por_consulta[0].pares] == [
        (1, True), (2, False),
    ]
    assert (con_2.recall, con_2.recall_fragmentos, con_2.mrr, con_2.azar) == (1.0, 0.5, 1.0, 1.0)
    # Con k = 1, «química» queda afuera: 1 de 2, y el azar es 1/2 por par.
    assert (con_1.recall, con_1.recuperados, con_1.azar) == (0.5, 1, 0.5)


@pytest.mark.historia(202, 205)
@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_banco_m1_por_comando_sobre_un_mini_corpus_da_el_valor_hecho_a_mano(
    banco_con_la_capa_falsa, validada, tmp_path, capsys
):
    ensayos, quimica = validada(ACTAS[0]), validada(QUIMICA)
    egresados = validada(EGRESADOS, fecha_inicio="1990-01-01", precision_fecha="anio")
    referencia = _referencia(
        tmp_path / "referencia", {"r1": ensayos, "r2": quimica, "r3": egresados},
        [
            "c1,Acta del laboratorio de ensayos,r1;r2,,,ajuste,",
            "c2,Fiesta de egresados con orquesta,r3,1989-01-01,,ajuste,",
            "c3,¿Y el acta de 1950?,,,,ajuste,true",  # de control: no cuenta en M1
            "c4,Laboratorio de química,r2,,,prueba,",  # de prueba: no cuenta en ajuste
        ],
    )

    assert main(["banco", "m1", "--conjunto", "ajuste"]) == 0

    [corrida] = (referencia / "corridas").glob("m1-ajuste-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    # A mano: 3 pares de 2 consultas, todos entre las 10 primeras (el corpus tiene 3 Piezas).
    assert (datos["n_consultas"], datos["n_pares"], datos["recuperados"]) == (2, 3, 3)
    assert (datos["recall"], datos["recall_3"], datos["mrr"], datos["oficial"]) == (
        1.0, 1.0, 1.0, None,
    )
    assert [round(x, 3) for x in datos["ic95"]] == [0.438, 1.0]
    # c1 no filtra (3 candidatas) y c2 deja pasar solo la de 1990: con k = 10, azar 1.
    assert [c["candidatos"] for c in datos["por_consulta"]] == [3, 1]
    assert (datos["azar"], datos["tamano_corpus"], datos["cumple"]) == (1.0, 3, True)
    assert datos["por_consulta"][1]["pares"][0]["pieza_id"] == egresados
    # Menos de 40 pares, el IC contiene el 80 % y el azar supera el 20 %.
    assert len(datos["avisos"]) == 3
    assert "embeber" in datos["configuracion_efectiva"]["por_capacidad"]
    tabla = capsys.readouterr().out
    assert "| 100,0 % | [43,8 %; 100,0 %] | 3 | 3 | 2 |" in tabla
    assert "corrida de ajuste" in tabla

    # El conjunto de prueba: la primera corrida es la oficial; la segunda, con la misma huella, no.
    assert main(["banco", "m1", "--conjunto", "prueba"]) == 0
    assert "corrida oficial" in capsys.readouterr().out
    assert main(["banco", "m1", "--conjunto", "prueba"]) == 0
    assert "**no oficial**" in capsys.readouterr().out
    oficiales = sorted(
        json.loads(r.read_text(encoding="utf-8"))["oficial"]
        for r in (referencia / "corridas").glob("m1-prueba-*.json")
    )
    assert oficiales == [False, True]


@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_banco_m1_se_niega_con_una_pieza_esperada_que_no_es_validada(
    banco_con_la_capa_falsa, validada, pieza_pendiente, tmp_path, capsys
):
    ensayos = validada(ACTAS[0])
    _referencia(
        tmp_path / "referencia", {"r1": ensayos, "r2": pieza_pendiente},
        ["c1,Acta del laboratorio de ensayos,r1;r2,,,prueba,"],
    )

    assert main(["banco", "m1", "--conjunto", "prueba"]) == 1

    error = capsys.readouterr().err
    assert "no son Validadas no retiradas" in error
    assert f"c1, r2 ({UUID(pieza_pendiente)})" in error
    assert not (tmp_path / "referencia" / "corridas").exists()
