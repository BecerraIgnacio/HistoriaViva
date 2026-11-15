"""El aviso «abierta por X»: quien abre una Pieza que otra persona abrió hace menos de 30 min lo
ve, y no le impide nada (historia 110, DEC-106, DEC-146 punto 14).

No reserva la Pieza: la exclusión real es el 409 de `version_base` (DEC-24). El reloj es
inyectado: la apertura y el vencimiento se miden con el mismo.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aplicacion import en_marcha, entrar
from capa_modelos_falsa import CapaModelosFalsa
from fastapi.testclient import TestClient
from validacion import EDICION_COMPLETA, JORGE, MARIA, abrir, edicion_sin

pytestmark = [
    pytest.mark.rf("RF-16"),
    pytest.mark.historia(110),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


class Reloj:
    def __init__(self) -> None:
        self.ahora = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.ahora


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def cliente(
    url_app: str, capa_modelos: CapaModelosFalsa, directorio_originales: Path, reloj: Reloj
) -> Iterator[TestClient]:
    """El `cliente` de la suite con el reloj del test (lo usa también `pieza_pendiente`)."""
    with en_marcha(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        reloj=reloj,
    ) as cliente:
        try:
            yield cliente
        finally:
            capa_modelos.liberar()


def en_edicion_por(cliente: TestClient, pieza_id: str) -> object:
    entrar(cliente, MARIA)
    respuesta = cliente.get(f"/api/piezas/{pieza_id}")
    assert respuesta.status_code == 200
    return respuesta.json()["en_edicion_por"]


def iso(marca: datetime) -> str:
    return marca.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(("minutos", "avisa"), [(29, True), (30, False)])
def test_el_aviso_vence_a_los_30_minutos(cliente, pieza_pendiente, reloj, minutos, avisa):
    entrar(cliente, JORGE)
    abrir(cliente, pieza_pendiente)
    desde = reloj()

    reloj.ahora += timedelta(minutes=minutos)

    esperado = {"nombre": "Jorge Alberto Peralta", "desde": iso(desde)} if avisa else None
    assert en_edicion_por(cliente, pieza_pendiente) == esperado


def test_la_apertura_propia_no_avisa(cliente, pieza_pendiente):
    abrir(entrar(cliente, MARIA), pieza_pendiente)

    assert en_edicion_por(cliente, pieza_pendiente) is None


def test_una_apertura_ya_guardada_no_avisa(cliente, pieza_pendiente):
    apertura = abrir(entrar(cliente, JORGE), pieza_pendiente)
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | {"medicion_id": apertura["medicion_id"]},
    )

    assert en_edicion_por(cliente, pieza_pendiente) is None


def test_avisa_la_ultima_apertura_y_no_impide_guardar_ni_firmar(
    cliente, pieza_pendiente, reloj
):
    entrar(cliente, JORGE)
    abrir(cliente, pieza_pendiente)
    reloj.ahora += timedelta(minutes=10)
    abrir(cliente, pieza_pendiente)
    ultima = reloj()
    reloj.ahora += timedelta(minutes=5)

    assert en_edicion_por(cliente, pieza_pendiente) == {
        "nombre": "Jorge Alberto Peralta",
        "desde": iso(ultima),
    }
    apertura = abrir(cliente, pieza_pendiente)
    guardada = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | {"medicion_id": apertura["medicion_id"]},
    )
    assert guardada.status_code == 200
    firmada = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"version_base": 2},
    )
    assert firmada.status_code == 200
    assert firmada.json()["estado"] == "Validada"
