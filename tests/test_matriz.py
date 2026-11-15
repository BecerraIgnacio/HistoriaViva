"""La matriz requisito → test sale de los marcadores (historia 221; DEC-111, DEC-115; R-40)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from historia_viva import matriz
from historia_viva.composicion import main
from historia_viva.matriz import Requisito, Recolectado

pytestmark = pytest.mark.historia(221)


def _requisito(id_: str, alcance: str = "Entra", historias: tuple[int, ...] = ()) -> Requisito:
    return Requisito(id_, "M", alcance, frozenset(historias))


def _test(nodeid: str, *marcadores: str, por_defecto: bool = True) -> Recolectado:
    return Recolectado(nodeid, marcadores, por_defecto)


def test_la_lista_trae_las_hu_del_director_rf_47_a_55_y_los_dir_diferidos():
    requisitos = {r.id: r for r in matriz.leer_requisitos()}

    for id_ in ("HU-030", "HU-031", "HU-032", *(f"RF-{n}" for n in range(47, 56))):
        assert not requisitos[id_].diferido, id_
    dirs = [r for r in requisitos.values() if r.id.startswith("DIR-")]
    assert {"DIR-03", "DIR-11", "DIR-44", "DIR-50", "DIR-59", "DIR-97"} <= {r.id for r in dirs}
    assert all(r.diferido for r in dirs)
    assert requisitos["DIR-03"].alcance == "Fuera del MVP"
    assert requisitos["RF-06"].alcance == "Entra; se activa con C14/C34"
    assert requisitos["HU-030"].historias >= {65, 82, 117, 285, 290}
    assert max(h for r in requisitos.values() for h in r.historias) <= matriz.ULTIMA_HISTORIA


def test_lo_que_entra_sin_test_es_hueco_y_lo_de_fuera_es_diferido():
    requisitos = [
        _requisito("RF-01", historias=(65,)),
        _requisito("RF-02"),
        _requisito("RF-06", "Entra; se activa con C14/C34"),
        _requisito("DIR-03", "Fuera del MVP"),
        _requisito("DIR-16", "Desvío documentado"),
    ]
    tests = [_test("tests/test_a.py::test_por_historia", "historia 65"),
             _test("tests/test_a.py::test_directo", "RF-02")]

    resultado = matriz.armar(requisitos, tests, {}, {})

    assert resultado.huecos == ["RF-06"]
    assert resultado.falla
    assert [fila[3] for fila in matriz.tabla(resultado)] == [
        "1 (0 directos)", "1 (1 directos)", "hueco", "diferido", "diferido",
    ]
    assert resultado.cubre["tests/test_a.py::test_por_historia"] == ("RF-01",)


def test_sin_huecos_ni_marcadores_sueltos_no_falla():
    tests = [_test("tests/test_a.py::test_x", "RF-02")]

    resultado = matriz.armar([_requisito("RF-02")], tests, {}, {})

    assert not resultado.falla


@pytest.mark.parametrize("marcador", ["RF-99", "RNF-NN", "historia 0", "historia 303"])
def test_un_marcador_que_apunta_a_un_requisito_inexistente_hace_fallar(marcador: str):
    tests = [_test("tests/test_a.py::test_x", "RF-02", marcador)]

    resultado = matriz.armar([_requisito("RF-02")], tests, {}, {})

    assert resultado.inexistentes == {"tests/test_a.py::test_x": (marcador,)}
    assert resultado.falla


def _corrida(directorio: Path, nombre: str, **datos: object) -> None:
    (directorio / f"{nombre}.json").write_text(json.dumps(datos), encoding="utf-8")


def test_m1_a_m4_salen_como_medicion_con_su_ultimo_numero(tmp_path: Path):
    _corrida(tmp_path, "m1-prueba-20261101T100000", medida="m1", momento="2026-11-01T10:00:00",
             recall=0.5, ic95=[0.4, 0.6], conjunto="prueba", oficial=True)
    _corrida(tmp_path, "m1-ajuste-20261102T100000", medida="m1", momento="2026-11-02T10:00:00",
             recall=0.8325, ic95=[0.71, 0.9], conjunto="ajuste", oficial=None)
    _corrida(tmp_path, "m3-A-20261103T100000", medida="m3", momento="2026-11-03T10:00:00",
             condicion="A", a_fuentes={"primer_turno": {"p95": 4200.0}})
    _corrida(tmp_path, "m4-20261104T100000", medida="m4", momento="2026-11-04T10:00:00",
             grupos=[{"tipo": "efemeride", "modelo": "m", "promedio": 3.25},
                     {"tipo": "relato", "modelo": "m", "promedio": None}])
    (tmp_path / "m1-roto.json").write_text("{", encoding="utf-8")

    mediciones = matriz.ultimas_mediciones(tmp_path)
    requisitos = [_requisito(r) for r in ("RNF-10", "RNF-11", "RNF-12", "RNF-26")]
    resultado = matriz.armar(requisitos, [], {}, mediciones)
    filas = {fila[0]: fila for fila in matriz.tabla(resultado)}

    assert resultado.huecos == []
    assert filas["RNF-11"][4] == "banco m1"
    assert filas["RNF-11"][5] == (
        "banco m1: recall@10 83.2% (IC95 71.0% a 90.0%), ajuste, 2026-11-02"
    )
    assert filas["RNF-10"][5] == "banco m3: p95 hasta las fuentes 4.2 s, condición A, 2026-11-03"
    assert filas["RNF-26"][5] == (
        "banco m4: promedio efemeride m: 3.25; relato m: sin veredicto, 2026-11-04"
    )
    assert filas["RNF-12"][5] == "banco m2: sin corrida"
    assert matriz.ultimas_mediciones(None) == {}


def test_toma_el_ultimo_resultado_de_cada_test_y_cuenta_los_que_no_corren_solos(
    pytester: pytest.Pytester,
):
    pytester.makepyfile(
        test_a="""
        import pytest

        def test_pasa():
            pass

        def test_cambia():
            assert False

        @pytest.mark.skip
        def test_salteado():
            pass

        @pytest.mark.parametrize("x", ["a-b", "c"])
        def test_param(x):
            pass

        class TestClase:
            def test_metodo(self):
                pass
        """
    )
    resultados_dir = pytester.path / ".resultados"
    pytester.runpytest(f"--junitxml={resultados_dir / 'junit-1.xml'}")
    pytester.makepyfile(test_a="def test_cambia():\n    pass\n")
    pytester.runpytest(f"--junitxml={resultados_dir / 'junit-2.xml'}")
    os.utime(resultados_dir / "junit-1.xml", (1, 1))  # el orden es por fecha, no por nombre

    resultados = matriz.leer_resultados(resultados_dir)
    tests = [
        _test(f"test_a.py::{n}", "RF-02")
        for n in ("test_pasa", "test_cambia", "test_salteado", "test_param[a-b]",
                  "TestClase::test_metodo", "test_nunca_corrio")
    ] + [_test("test_a.py::test_con_ollama", "RF-02", por_defecto=False)]
    resultado = matriz.armar([_requisito("RF-02")], tests, resultados, {})

    assert resultado.resultados == {
        "test_a.py::test_pasa": "pasa",
        "test_a.py::test_cambia": "pasa",
        "test_a.py::test_salteado": "salteado",
        "test_a.py::test_param[a-b]": "pasa",
        "test_a.py::TestClase::test_metodo": "pasa",
    }
    assert matriz.tabla(resultado)[0][5] == (
        "4 pasa · 1 salteado · 1 sin correr · 1 fuera de la suite"
    )
    assert matriz.tabla_inversa(resultado)[-1][4] == "fuera de la suite (-m real)"


def test_recolecta_los_marcadores_sin_perder_lo_que_el_addopts_deja_afuera(
    pytester: pytest.Pytester,
):
    pytester.makeini("[pytest]\naddopts = -m 'not real'\nmarkers =\n    real: con Ollama\n")
    pytester.makeconftest('pytest_plugins = ["marcador_de_requisito"]\n')
    pytester.makepyfile(
        test_b="""
        import pytest

        pytestmark = pytest.mark.historia(221)

        @pytest.mark.rf("RF-02", "RF-06")
        def test_rf():
            pass

        @pytest.mark.real
        @pytest.mark.rnf("RNF-10")
        def test_real():
            pass
        """
    )

    recolectados = matriz.recolectar(pytester.path)

    assert recolectados == [
        _test("test_b.py::test_rf", "RF-02", "RF-06", "historia 221"),
        _test("test_b.py::test_real", "RNF-10", "historia 221", por_defecto=False),
    ]


def test_un_test_sin_marcador_hace_fallar_la_recoleccion(pytester: pytest.Pytester):
    pytester.makeconftest('pytest_plugins = ["marcador_de_requisito"]\n')
    pytester.makepyfile(test_c="def test_sin_marcador():\n    pass\n")

    with pytest.raises(matriz.RecoleccionFallida, match="test_sin_marcador"):
        matriz.recolectar(pytester.path)


def test_el_csv_sale_por_exportacion_y_el_markdown_trae_las_dos_tablas(tmp_path: Path):
    tests = [_test("tests/test_a.py::test_x", "=1+1"), _test("tests/test_a.py::test_y", "RF-02")]
    resultado = matriz.armar([_requisito("RF-02")], tests, {}, {})

    rutas = matriz.escribir(resultado, tmp_path)

    inversa = (tmp_path / "matriz-inversa.csv").read_bytes()
    assert inversa.startswith(b"\xef\xbb\xbfTest,Suite,Marcadores,Requisitos,")
    assert "'=1+1" in inversa.decode("utf-8-sig")
    assert (tmp_path / "matriz.csv").read_bytes().startswith(
        "﻿Requisito,Prioridad,Alcance,Tests,Suites,Último resultado".encode()
    )
    texto = rutas[0].read_text(encoding="utf-8")
    assert "| RF-02 | M | Entra | 1 (1 directos) | test_a | 1 sin correr |" in texto
    assert "## Marcadores que no apuntan a ningún requisito" in texto
    assert "## Matriz inversa (test → requisitos)" in texto


def test_el_comando_escribe_la_matriz_y_sale_con_1_si_hay_huecos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(matriz, "recolectar", lambda: [_test("tests/test_a.py::test_x", "RF-02")])
    dos = [_requisito("RF-02"), _requisito("RF-03")]
    monkeypatch.setattr(matriz, "leer_requisitos", lambda: dos)
    monkeypatch.setattr(matriz, "leer_resultados", dict)

    assert main(["matriz", "--salida", str(tmp_path), "--corridas", str(tmp_path)]) == 1
    assert "Huecos (entran y no tienen test): RF-03" in capsys.readouterr().err
    assert (tmp_path / "matriz.csv").is_file()

    monkeypatch.setattr(matriz, "leer_requisitos", lambda: [_requisito("RF-02")])
    assert main(["matriz", "--salida", str(tmp_path)]) == 0


def test_el_comando_sale_con_1_si_la_recoleccion_falla(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def falla() -> list[Recolectado]:
        raise matriz.RecoleccionFallida("Tests sin marcador de requisito")

    monkeypatch.setattr(matriz, "recolectar", falla)

    assert main(["matriz", "--salida", str(tmp_path)]) == 1
