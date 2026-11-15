"""La carpeta del MVP nace de una copia limpia del Prototipo (historia 209, DEC-49).

`git archive` del árbol de `prototipo/` no trajo historia de git ni datos (resultados del banco,
originales, `.env`). El Prototipo se retiró del repositorio; queda en la historia de git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.historia(209)

MVP = Path(__file__).resolve().parent.parent
# Resultados del banco del Prototipo (no se porta, spec §8) y originales subidos (el MVP los
# guarda en el bind mount de `HV_ORIGINALES`, fuera del repositorio).
# `originales/` no: la app la crea al arrancar desde `mvp/` (HV_ORIGINALES por defecto) y la ignora `.gitignore`.
DATOS_EN_LA_RAIZ = {"banco"}


def test_la_copia_no_trae_datos():
    assert [nombre for nombre in DATOS_EN_LA_RAIZ if (MVP / nombre).exists()] == []
