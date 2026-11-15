"""Efectos del retiro en efemérides, Relatos e informes (107; R-12, R-22, DEC-138).

Retirar una Pieza marca «fuente retirada» en las efemérides y los Relatos que la citan, en la
misma transacción, con la función de `efemerides` que `composicion` inyecta en `retiro`. Desde
ahí `/v1` no los sirve, y restaurar la Pieza no los devuelve. Los informes no necesitan nada
de `retiro`: el aviso y el 409 salen de `fichas.cambios_desde` (069). HTTP contra Postgres real.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from aplicacion import entrar
from historia_viva.auditoria import Accion, RegistroDeAuditoria
from test_api_agentes import (  # noqa: F401 — fixtures de /v1 y de efemérides y Relatos
    PERIODO,
    difusor,
    efemeride,
    emitir,
    encender,
    relato,
)
from test_informes import _aprobar, _pedido, validadas  # noqa: F401
from test_retiradas import restaurar
from test_retiro import retirar
from validacion import JORGE, MARIA

pytestmark = [
    pytest.mark.rf("RF-29", "RF-48", "RF-55"),
    pytest.mark.historia(157, 266),
    pytest.mark.confirma("la app confirma desde su propio pool"),
]


def _marcas(dueno: Any, efemeride_id: str, relato_id: str) -> tuple[Any, ...]:
    """Si la efeméride y el Relato tienen la marca, y su texto, que el retiro no toca."""
    return (
        dueno.execute(
            "SELECT fuente_retirada_el IS NOT NULL, cuerpo, version FROM acervo.efemeride"
            " WHERE efemeride_id = %s",
            (efemeride_id,),
        ).fetchone(),
        dueno.execute(
            "SELECT fuente_retirada_el IS NOT NULL, cuerpo FROM acervo.relato"
            " WHERE relato_id = %s",
            (relato_id,),
        ).fetchone(),
    )


def _servidos(cliente: Any, difusor: dict[str, str]) -> tuple[list[str], list[str]]:
    efemerides = cliente.get("/v1/efemerides", params=PERIODO, headers=difusor).json()
    relatos = cliente.get("/v1/relatos", params=PERIODO, headers=difusor).json()
    return (
        [e["efemeride_id"] for e in efemerides["efemerides"]],
        [r["relato_id"] for r in relatos["relatos"]],
    )


@pytest.mark.historia(271)
def test_retirar_marca_la_efemeride_y_el_relato_sin_tocar_su_texto_y_restaurar_no_los_devuelve(
    cliente, validadas, efemeride, relato, difusor, _conexion_del_dueno
):
    dueno = _conexion_del_dueno
    sana, fuente = validadas(), str(validadas(nombre="acta-2021-04.pdf"))
    queda = efemeride("2026-04-11", [str(sana)])
    marcada = efemeride("2026-04-12", [str(sana), fuente])
    queda_relato, marcado = relato([(1, str(sana))]), relato([(1, fuente)])
    assert _marcas(dueno, marcada, marcado) == (
        (False, "Se firmó el convenio [1].", 1), (False, "Se creó [1]."),
    )  # fmt: skip

    respuesta = retirar(cliente, fuente)

    assert respuesta.status_code == 200, respuesta.text
    assert _marcas(dueno, marcada, marcado) == (
        (True, "Se firmó el convenio [1].", 1), (True, "Se creó [1]."),
    )  # fmt: skip
    assert _servidos(cliente, difusor) == ([f"efe_{queda}"], [f"rel_{queda_relato}"])

    restaurada = restaurar(cliente, fuente)

    assert restaurada.status_code == 200, restaurada.text
    assert _marcas(dueno, marcada, marcado)[0][0] and _marcas(dueno, marcada, marcado)[1][0]
    assert _servidos(cliente, difusor) == ([f"efe_{queda}"], [f"rel_{queda_relato}"])


def test_una_falla_al_marcar_revierte_el_retiro_entero(
    cliente, validadas, efemeride, relato, _conexion_del_dueno, monkeypatch
):
    fuente = str(validadas())
    marcada, marcado = efemeride("2026-04-12", [fuente]), relato([(1, fuente)])
    retiro = cliente.app.state.retiro
    marcar = retiro._marcar_fuente_retirada

    def marcar_y_fallar(tx: Any, pieza_id: UUID) -> None:
        marcar(tx, pieza_id)  # la marca se escribe y la falla la tiene que revertir
        raise RuntimeError("falla inyectada al marcar")

    monkeypatch.setattr(retiro, "_marcar_fuente_retirada", marcar_y_fallar)
    with pytest.raises(RuntimeError, match="falla inyectada"):
        retirar(cliente, fuente)

    assert [m[0] for m in _marcas(_conexion_del_dueno, marcada, marcado)] == [False, False]
    assert cliente.get(f"/api/piezas/{fuente}").status_code == 200
    assert RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.PIEZA_RETIRO) == []


@pytest.mark.rf("RF-49")
@pytest.mark.historia(271, 275)
def test_retirar_por_http_avisa_en_el_aprobado_y_deja_desactualizado_el_borrador(
    cliente, validadas
):
    validadas()
    fuente = validadas(nombre="acta-2021-04.pdf")
    entrar(cliente, JORGE)
    borrador = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    aprobado = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    for persona in (JORGE, MARIA):
        _aprobar(cliente, persona, aprobado)

    assert retirar(cliente, str(fuente)).status_code == 200

    entrar(cliente, JORGE)
    detalle = cliente.get(f"/api/informes/{aprobado}").json()
    desactualizado = _aprobar(cliente, JORGE, borrador)
    assert (detalle["estado"], detalle["cambio_despues_de_aprobar"]) == ("Aprobado", True)
    assert detalle["cambios"] == [
        {"pieza_id": str(fuente), "nombre_archivo": "acta-2021-04.pdf", "retirada": True}
    ]
    assert (desactualizado.status_code, desactualizado.json()["type"]) == (
        409, "/errores/informe-desactualizado",
    )  # fmt: skip
    assert "«acta-2021-04.pdf»" in desactualizado.json()["detail"]
