"""Verifica que el paquete y sus fronteras se importan."""

import importlib

import pytest

pytestmark = pytest.mark.historia(210)


@pytest.mark.parametrize(
    "modulo",
    [
        "historia_viva",
        "historia_viva.dominio.tipos",
        "historia_viva.dominio.estados",
        "historia_viva.dominio.fechas",
        "historia_viva.dominio.errores",
        "historia_viva.capa_modelos.contrato",
        "historia_viva.capa_modelos.limites",
        "historia_viva.fichas.contrato",
    ],
)
def test_modulo_se_importa(modulo):
    importlib.import_module(modulo)
