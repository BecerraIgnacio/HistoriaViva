"""Cada test declara el requisito que cubre (historia 221, DEC-111): insumo de la matriz generada."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.historia(221)


def test_un_test_sin_requisito_hace_fallar_la_recoleccion(pytester: pytest.Pytester):
    pytester.makepyfile(
        """
        import pytest

        def test_sin_marcador():
            pass

        @pytest.mark.rf()
        def test_marcador_vacio():
            pass

        @pytest.mark.rf("RF-02")
        def test_con_requisito():
            pass
        """
    )

    resultado = pytester.runpytest("-p", "marcador_de_requisito")

    assert resultado.ret == pytest.ExitCode.USAGE_ERROR
    salida = resultado.stdout.str() + resultado.stderr.str()
    assert "test_sin_marcador" in salida
    assert "test_marcador_vacio" in salida
    assert "test_con_requisito" not in salida


def test_rf_rnf_o_historia_alcanzan(pytester: pytest.Pytester):
    pytester.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.historia(209)

        def test_del_modulo():
            pass

        @pytest.mark.rf("RF-02", "RF-06")
        def test_rf():
            pass

        @pytest.mark.rnf("RNF-02")
        def test_rnf():
            pass
        """
    )

    resultado = pytester.runpytest("-p", "marcador_de_requisito", "--strict-markers")

    resultado.assert_outcomes(passed=3)
