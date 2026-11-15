"""La prioridad de cola y la estimación de espera del Lote (086, DEC-25, RF-44).

La Pieza de un Lote encola con prioridad 1; la suelta y el reproceso individual, con 0. `web`
corre sin trabajador (el `banco` de `test_reprocesar`): los tests toman la cola a mano.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from historia_viva.procesamiento.cola import Cola, TipoDeTrabajo
from test_lotes import LOTE
from test_reprocesar import Banco, banco, comando  # noqa: F401 (fixtures)
from validacion import PDF

pytestmark = [
    pytest.mark.rf("RF-44"),
    pytest.mark.confirma("`web` y el trabajador confirman desde sus pools"),
]


def _lote(banco: Banco) -> str:
    lote_id: str = banco.web.post("/api/lotes", json=LOTE).json()["lote"]["lote_id"]
    return lote_id


def _subir(banco: Banco, nombre: str, lote_id: str | None = None) -> UUID:
    respuesta = banco.web.post(
        "/api/piezas",
        data={} if lote_id is None else {"lote_id": lote_id},
        files={"archivo": (nombre, PDF + nombre.encode(), "application/pdf")},
    )
    assert respuesta.status_code == 201, respuesta.text
    return UUID(respuesta.json()["pieza_id"])


def _reintentar(banco: Banco, pieza_id: str) -> UUID:
    assert banco.web.post(f"/api/piezas/{pieza_id}/reproceso").status_code == 202
    return UUID(pieza_id)


def _orden_de_toma(banco: Banco) -> list[UUID]:
    cola = Cola(bd=banco.trabajador_bd)
    tomadas = []
    while (trabajo := cola.tomar((TipoDeTrabajo.PROCESAMIENTO,))) is not None:
        tomadas.append(trabajo.pieza_id)
    return tomadas


@pytest.mark.historia(89)
def test_la_suelta_y_el_reproceso_individual_pasan_adelante_de_las_piezas_del_lote(banco):
    fallida = banco.subir_fallida("urgente.pdf", PDF + b"urgente")
    lote_id = _lote(banco)
    primera, segunda = _subir(banco, "a.pdf", lote_id), _subir(banco, "b.pdf", lote_id)
    suelta = _subir(banco, "acta.pdf")
    reproceso = _reintentar(banco, fallida)

    assert _orden_de_toma(banco) == [suelta, reproceso, primera, segunda]


@pytest.mark.historia(89)
def test_el_reproceso_de_una_pieza_de_lote_vuelve_con_prioridad_cero(banco):
    lote_id = _lote(banco)
    fallida = banco.subir_fallida("a.pdf", PDF + b"a", lote_id=lote_id)
    otra = _subir(banco, "b.pdf", lote_id)
    reproceso = _reintentar(banco, fallida)

    assert _orden_de_toma(banco) == [reproceso, otra]


@pytest.mark.historia(89)
def test_reprocesar_fallidos_devuelve_la_pieza_de_lote_con_su_prioridad(banco, comando, capsys):
    lote_id = _lote(banco)
    fallida = banco.subir_fallida("a.pdf", PDF + b"a", lote_id=lote_id)

    assert comando(["reprocesar", "--fallidos"]) == 0
    suelta = _subir(banco, "acta.pdf")

    # El reproceso en tanda no es individual: la Pieza de Lote sigue detrás de la suelta.
    assert _orden_de_toma(banco) == [suelta, UUID(fallida)]
    capsys.readouterr()


def _estimaciones(banco: Banco, lote_id: str) -> tuple[object, object]:
    """La del detalle (`progreso.estimacion`) y la de su fila en la lista de Lotes (P10)."""
    detalle = banco.web.get(f"/api/lotes/{lote_id}").json()["progreso"]["estimacion"]
    [fila] = banco.web.get("/api/lotes").json()["lotes"]
    return detalle, fila["estimacion"]


@pytest.mark.historia(88)
def test_la_espera_del_lote_sale_de_la_mediana_y_se_va_cuando_no_quedan_recibidas(banco):
    lote_id = _lote(banco)
    _subir(banco, "a.pdf", lote_id)
    _subir(banco, "b.pdf", lote_id)
    _subir(banco, "acta.pdf")
    assert _estimaciones(banco, lote_id) == (None, None)  # todavía no terminó ningún trabajo

    cola = Cola(bd=banco.trabajador_bd)
    suelta = cola.tomar((TipoDeTrabajo.PROCESAMIENTO,))
    assert suelta is not None
    with banco.trabajador_bd.transaccion() as tx:
        cola.completar(tx, suelta, duracion_ms=60_000, tokens_entrada=0, tokens_salida=0)

    # Un minuto por cada una de las dos Recibidas del Lote, en segundos.
    assert _estimaciones(banco, lote_id) == (120, 120)
    while banco.trabajador.procesar_siguiente():
        pass
    assert _estimaciones(banco, lote_id) == (None, None)


@pytest.mark.historia(88)
def test_un_lote_con_todas_sus_recibidas_fallidas_no_tiene_espera_y_la_fila_trae_las_fallidas(
    banco,
):
    lote_id = _lote(banco)
    banco.subir_fallida("a.pdf", PDF + b"a", lote_id=lote_id)
    banco.capa.arreglar()
    _subir(banco, "acta.pdf")
    assert banco.trabajador.procesar_siguiente() is True  # hay historial

    [fila] = banco.web.get("/api/lotes").json()["lotes"]
    assert (fila["fallidas"], fila["estimacion"]) == (1, None)
    assert _estimaciones(banco, lote_id) == (None, None)
