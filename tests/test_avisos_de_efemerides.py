"""Avisos de revisión de efemérides: alerta de inyección y «fuente retirada» (063; historia 266,
DEC-130, R-11, R-12, ADR-0003; auditoría de tickets, decisión 11).

La marca «fuente retirada» se escribe en la transacción que la causa: `validacion` llama a
`efemerides.marcar_fuente_retirada` cuando una Validada deja de serlo. Ningún `GET` la escribe.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
from uuid import UUID

import psycopg
import pytest

from aplicacion import entrar
from historia_viva.capa_modelos.contrato import ETIQUETA_FICHA
from historia_viva.efemerides import EfemerideEntregable, Efemerides
from historia_viva.validacion import Validacion
from test_efemerides import (  # noqa: F401 (fixtures)
    AHORA,
    CANARIO,
    DIA,
    RGC,
    _aprobar,
    bd,
    efemerides,
    propuesta,
    sembrar,
)
from test_fichas import COMPLETA
from validacion import JORGE, MARIA

pytestmark = [
    pytest.mark.rf("RF-48"),
    pytest.mark.confirma("la generación y la app confirman desde el pool"),
]


def _marca(conexion, efemeride: str) -> tuple:
    return conexion.execute(
        "SELECT fuente_retirada_el, cuerpo, version FROM acervo.efemeride"
        " WHERE efemeride_id = %s",
        (efemeride,),
    ).fetchone()


INCOMPLETA = replace(COMPLETA, origen=None)


def _editar(cliente, pieza: str, edicion, version_base: int) -> None:
    """Lo que hace `PUT /ficha`, sin leer la propuesta: la Ficha sembrada no es markdown."""
    cliente.app.state.validacion.guardar_ficha(
        pieza_id=UUID(pieza), version_base=version_base, edicion=edicion, principal=RGC
    )


@pytest.mark.historia(266)
@pytest.mark.rnf("RNF-04")
def test_una_fuente_con_alerta_deja_el_aviso_y_lo_hostil_viaja_solo_como_dato(
    cliente, efemerides, sembrar, capa_modelos
):
    sembrar("1975-05-03", alerta=True, resumen=f"Acta de 1975. {CANARIO}")

    efemeride = efemerides.generar(DIA)

    [invocacion] = capa_modelos.invocaciones
    [ficha] = invocacion.bloques
    assert ficha.etiqueta == ETIQUETA_FICHA and CANARIO in ficha.contenido
    entrar(cliente, JORGE)
    detalle = cliente.get(f"/api/efemerides/{efemeride}").json()
    assert detalle["aviso_inyeccion"] is True
    assert [f["alerta_inyeccion"] for f in detalle["fuentes"]] == [True]
    # Aprobar no se bloquea, y lo que entrega `/v1` no trae la alerta (C33).
    assert _aprobar(cliente, str(efemeride)).status_code == 200
    with cliente.app.state.bd.transaccion() as tx:
        [entregable] = efemerides.aprobadas(tx, DIA.fecha, DIA.fecha)
    assert entregable.efemeride_id == efemeride
    assert not {f.name for f in fields(EfemerideEntregable)} & {
        "alerta_inyeccion", "aviso_inyeccion"
    }


@pytest.mark.historia(264)
def test_dejar_incompleta_una_ficha_fuente_marca_la_fuente_retirada_una_sola_vez(
    cliente, propuesta, _conexion_del_dueno
):
    efemeride, pieza = propuesta
    antes = _marca(_conexion_del_dueno, efemeride)

    _editar(cliente, pieza, COMPLETA, 1)  # Validada → Validada: no marca
    assert _marca(_conexion_del_dueno, efemeride) == antes
    _editar(cliente, pieza, INCOMPLETA, 2)  # Validada → Incompleta
    marcada, cuerpo, version = _marca(_conexion_del_dueno, efemeride)
    assert marcada is not None and (cuerpo, version) == antes[1:]  # el texto no cambia
    _editar(cliente, pieza, COMPLETA, 3)  # vuelve a Validada: la marca queda
    _editar(cliente, pieza, INCOMPLETA, 4)  # y otra vez Incompleta: no se remarca
    assert _marca(_conexion_del_dueno, efemeride)[0] == marcada


@pytest.mark.historia(264)
def test_la_marca_va_en_la_transaccion_de_la_edicion(cliente, propuesta, _conexion_del_dueno):
    efemeride, pieza = propuesta
    estado = cliente.app.state

    def marcar_y_fallar(tx, pieza_id: UUID) -> None:
        estado.efemerides.marcar_fuente_retirada(tx, pieza_id)
        raise RuntimeError("falla después de marcar")

    validacion = Validacion(
        bd=estado.bd, fichas=estado.fichas, indexador=estado.indexacion,
        lotes=estado.lotes, auditoria=estado.auditoria, marcar_fuente_retirada=marcar_y_fallar,
    )  # fmt: skip
    with pytest.raises(RuntimeError):
        validacion.guardar_ficha(
            pieza_id=UUID(pieza), version_base=1, edicion=INCOMPLETA,
            principal=RGC,
        )  # fmt: skip

    assert _marca(_conexion_del_dueno, efemeride)[0] is None
    assert _conexion_del_dueno.execute(
        "SELECT estado FROM acervo.pieza WHERE pieza_id = %s", (pieza,)
    ).fetchone() == ("Validada",)


@pytest.mark.historia(264)
def test_un_get_no_escribe_la_marca_pero_muestra_la_fuente_retirada(
    cliente, propuesta, _conexion_del_dueno
):
    efemeride, pieza = propuesta
    # Una causa que no pasó por `validacion`: el `GET` no la repara escribiendo.
    _conexion_del_dueno.execute(
        "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
        " borrada_motivo = 'Retirada en la prueba' WHERE pieza_id = %s",
        (MARIA, pieza),
    )
    entrar(cliente, JORGE)

    detalle = cliente.get(f"/api/efemerides/{efemeride}").json()
    cliente.get("/api/efemerides", params={"estado": "Propuesta"})

    assert detalle["fuente_retirada"] is False
    assert [f["retirada"] for f in detalle["fuentes"]] == [True]
    assert _marca(_conexion_del_dueno, efemeride)[0] is None


@pytest.mark.historia(264)
def test_una_propuesta_con_fuente_retirada_se_aprueba_y_la_marca_no_se_quita(
    cliente, propuesta, _conexion_del_dueno
):
    efemeride, pieza = propuesta
    _editar(cliente, pieza, INCOMPLETA, 1)
    entrar(cliente, JORGE)

    detalle = cliente.get(f"/api/efemerides/{efemeride}").json()
    assert detalle["fuente_retirada"] is True
    assert [f["retirada"] for f in detalle["fuentes"]] == [True]
    with (
        pytest.raises(psycopg.errors.RestrictViolation, match="no se quita"),
        _conexion_del_dueno.transaction(),
    ):
        _conexion_del_dueno.execute(
            "UPDATE acervo.efemeride SET fuente_retirada_el = NULL WHERE efemeride_id = %s",
            (efemeride,),
        )
    aprobada = _aprobar(cliente, efemeride)
    assert aprobada.status_code == 200, aprobada.text
    assert (aprobada.json()["estado"], aprobada.json()["fuente_retirada"]) == ("Aprobada", True)
    with cliente.app.state.bd.transaccion() as tx:
        assert cliente.app.state.efemerides.aprobadas(tx, DIA.fecha, DIA.fecha) == []
    with (
        pytest.raises(psycopg.errors.RestrictViolation, match="no se quita"),
        _conexion_del_dueno.transaction(),
    ):
        _conexion_del_dueno.execute(
            "UPDATE acervo.efemeride SET fuente_retirada_el = NULL WHERE efemeride_id = %s",
            (efemeride,),
        )


@pytest.mark.historia(263, 264, 265)
def test_una_propuesta_marcada_se_edita_y_se_descarta_y_conserva_la_marca(
    cliente, propuesta, _conexion_del_dueno
):
    efemeride, pieza = propuesta
    _editar(cliente, pieza, INCOMPLETA, 1)
    entrar(cliente, JORGE)

    editada = cliente.put(
        f"/api/efemerides/{efemeride}", json={"version_base": 1, "texto": "Corregido [1]."}
    )
    assert editada.status_code == 200, editada.text
    assert (editada.json()["version"], editada.json()["fuente_retirada"]) == (2, True)
    descartada = cliente.post(
        f"/api/efemerides/{efemeride}/descarte", json={"motivo": "La fuente se retiró."}
    )
    assert descartada.status_code == 200, descartada.text
    assert (descartada.json()["estado"], descartada.json()["fuente_retirada"]) == (
        "Descartada", True
    )


def _sembrar_efemeride(conexion, *, alcance: str, desde: str, hasta: str, estado: str,
                       retirada: bool = False) -> UUID:  # fmt: skip
    aprobada, descartada = estado == "aprobada", estado == "descartada"
    fila = conexion.execute(
        "INSERT INTO acervo.efemeride (alcance, fecha_objetivo, difusion_desde, difusion_hasta,"
        " titulo, cuerpo, estado, configuracion_efectiva, origen_generacion, aprobada_por,"
        " aprobada_el, metodo_autenticacion, descartada_por, descartada_el, descarte_motivo,"
        " fuente_retirada_el) VALUES (%s, %s, %s, %s, 'Título', 'Texto [1].', %s, '{}',"
        " 'programada', %s, %s, %s, %s, %s, %s, %s) RETURNING efemeride_id",
        (
            alcance, desde, desde, hasta, estado,
            MARIA if aprobada else None, AHORA if aprobada else None, "dev" if aprobada else None,
            MARIA if descartada else None, AHORA if descartada else None,
            "No corresponde" if descartada else None, AHORA if retirada else None,
        ),  # fmt: skip
    ).fetchone()
    return fila[0]


@pytest.mark.historia(267)
def test_aprobadas_devuelve_las_que_se_solapan_sin_propuestas_descartadas_ni_retiradas(
    efemerides: Efemerides, bd, _conexion_del_dueno
):
    def sembrar_una(alcance, desde, hasta, estado="aprobada", **extra):
        return _sembrar_efemeride(
            _conexion_del_dueno, alcance=alcance, desde=desde, hasta=hasta, estado=estado, **extra
        )

    dia = sembrar_una("dia", "2026-05-03", "2026-05-03")
    mes = sembrar_una("mes", "2026-05-01", "2026-05-31")
    sembrar_una("dia", "2026-05-04", "2026-05-04", "propuesta")
    sembrar_una("dia", "2026-05-05", "2026-05-05", "descartada")
    sembrar_una("dia", "2026-05-06", "2026-05-06", retirada=True)
    sembrar_una("dia", "2026-06-01", "2026-06-01")

    def ids(desde: date, hasta: date) -> list[UUID]:
        with bd.transaccion() as tx:
            return [e.efemeride_id for e in efemerides.aprobadas(tx, desde, hasta)]

    assert ids(date(2026, 5, 3), date(2026, 5, 3)) == [mes, dia]
    assert ids(date(2026, 5, 20), date(2026, 5, 20)) == [mes]  # la de mes, con cualquier día
    assert ids(date(2026, 5, 4), date(2026, 5, 6)) == [mes]
    assert ids(date(2026, 4, 1), date(2026, 4, 30)) == []
    with bd.transaccion() as tx:
        [entregable, _] = efemerides.aprobadas(tx, date(2026, 5, 31), date(2026, 6, 1))
    assert (entregable.efemeride_id, entregable.aprobada_por, entregable.piezas_fuente) == (
        mes, "mrossi@fie.undef.edu.ar", ()
    )
