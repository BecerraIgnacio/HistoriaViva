"""Marcador de requisito obligatorio (DEC-111, historia 221): insumo de la matriz generada.

Cada test declara lo que cubre con `@pytest.mark.rf("RF-NN")`, `@pytest.mark.rnf("RNF-NN")` o
`@pytest.mark.historia(NNN)`, uno o varios, también como `pytestmark` del módulo. Un test sin
marcador hace fallar la recolección.
"""

from __future__ import annotations

import pytest

MARCADORES = {"rf": '"RF-NN"', "rnf": '"RNF-NN"', "historia": "número de historia"}


def pytest_configure(config: pytest.Config) -> None:
    for nombre, valor in MARCADORES.items():
        config.addinivalue_line("markers", f"{nombre}(*ids): requisito que cubre el test ({valor})")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    sin_requisito = [
        item.nodeid
        for item in items
        if not any(marca.args for nombre in MARCADORES for marca in item.iter_markers(nombre))
    ]
    if sin_requisito:
        raise pytest.UsageError(
            "Tests sin marcador de requisito (rf, rnf o historia; DEC-111):\n  "
            + "\n  ".join(sin_requisito)
        )
