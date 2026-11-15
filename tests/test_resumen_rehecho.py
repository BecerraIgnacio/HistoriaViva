"""El resumen de una foto sale de las Afirmaciones no rechazadas (146, ADR-0007 punto 3).

En el procesamiento nadie decidió nada y el resumen de la v1 las ve todas. Al rechazar una, la
cola rehace el resumen en segundo plano sin ella: un trabajo `resumen` por Pieza, que no crea
versión de la Ficha y queda como propuesta. La capa falsa resume repitiendo lo que recibió
(`resumen_de_lo_que_vio`): lo que no le llegó no puede aparecer en el resumen.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import psycopg
import pytest
from aplicacion import entrar
from capa_modelos_falsa import TEXTO_VISIBLE_FIJO, imagen_con_afirmaciones, resumen_de_lo_que_vio
from espera import TOPE_S, esperar_detras_del_candado
from test_afirmaciones import (
    CUERPO_LEIDO,
    DOS_PERSONAS,
    EN_LA_APP,
    INVENTADA,
    LOGO,
    _decidir,
    _firmar,
    _foto_pendiente,
)
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, EDICION_COMPLETA, MARIA

from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.capa_modelos.contrato import ETIQUETA_CUERPO_FICHA, Capacidad, Plantilla
from historia_viva.dominio.errores import LimiteExcedido
from historia_viva.interruptor import Freno, cambiar


TECNICO = Actor(
    TipoDeActor.PERSONA, "tibarra@fie.undef.edu.ar", usuario_id=UUID(ADMINISTRADOR_TECNICO)
)


def _resumen(cliente, pieza_id: str) -> dict[str, Any] | None:
    resumen: dict[str, Any] | None = cliente.get(f"/api/piezas/{pieza_id}/afirmaciones").json()[
        "resumen"
    ]
    return resumen


def _esperar_resumen(cliente, pieza_id: str) -> dict[str, Any]:
    """Sondea como la pantalla hasta que el resumen deja de rehacerse."""
    limite = time.monotonic() + TOPE_S
    while (resumen := _resumen(cliente, pieza_id)) is None or resumen["estado"] == "rehaciendo":
        assert time.monotonic() < limite, f"El resumen sigue rehaciéndose: {resumen}"
        time.sleep(0.02)
    return resumen


def _trabajos_de_resumen(url_bd: str, pieza_id: str) -> list[str]:
    with psycopg.connect(url_bd) as conexion:
        filas = conexion.execute(
            "SELECT estado FROM acervo.trabajo_procesamiento WHERE pieza_id = %s"
            " AND tipo = 'resumen' ORDER BY encolado_el",
            (pieza_id,),
        ).fetchall()
    return [estado for (estado,) in filas]


def _frenar(cliente, frenado: bool) -> None:
    with cliente.app.state.bd.transaccion() as tx:
        cambiar(tx, por=TECNICO, cambios={Freno.PROCESAMIENTO: not frenado}, motivo="Prueba 146")


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_rechazar_rehace_el_resumen_sin_la_afirmacion_rechazada(cliente, capa_modelos):
    capa_modelos.resumen = resumen_de_lo_que_vio
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, LOGO, INVENTADA))
    v1 = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    # En el procesamiento nadie decidió nada: el resumen de la v1 las vio todas (144).
    assert INVENTADA in v1["frontmatter"]["resumen"]
    assert _resumen(cliente, pieza_id) is None

    assert _decidir(cliente, pieza_id, 3, "rechazada").status_code == 200
    resumen = _esperar_resumen(cliente, pieza_id)

    assert resumen["estado"] == "listo"
    assert INVENTADA not in resumen["texto"]
    assert DOS_PERSONAS in resumen["texto"] and LOGO in resumen["texto"]
    assert TEXTO_VISIBLE_FIJO in resumen["texto"]
    assert resumen["informe_inyeccion"]["alerta"] is False
    # Las Afirmaciones y lo leído van como bloque de datos; la instrucción es un identificador.
    resumir = capa_modelos.invocaciones[-1]
    assert resumir.capacidad is Capacidad.RESUMIR and isinstance(resumir.plantilla, Plantilla)
    assert [b.etiqueta for b in resumir.bloques] == [ETIQUETA_CUERPO_FICHA]
    assert DOS_PERSONAS in resumir.bloques[0].contenido
    assert INVENTADA not in resumir.bloques[0].contenido
    # No crea versión: la propuesta la pone la pantalla en el campo.
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["frontmatter"]["resumen"]) == (1, v1["frontmatter"]["resumen"])


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_varios_rechazos_seguidos_dejan_un_solo_trabajo_y_gana_el_ultimo(
    cliente, capa_modelos, url_bd
):
    capa_modelos.resumen = resumen_de_lo_que_vio
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, LOGO, INVENTADA))
    resumenes_antes = sum(i.capacidad is Capacidad.RESUMIR for i in capa_modelos.invocaciones)
    _frenar(cliente, True)  # el trabajador no toma nada: los rechazos se acumulan

    for orden in (2, 3):
        assert _decidir(cliente, pieza_id, orden, "rechazada").status_code == 200

    assert _trabajos_de_resumen(url_bd, pieza_id) == ["encolado"]
    assert _resumen(cliente, pieza_id) == {
        "estado": "rehaciendo", "texto": None, "rehecho_el": None, "atendido": False,
        "informe_inyeccion": None, "frenado": True,
    }
    _frenar(cliente, False)
    resumen = _esperar_resumen(cliente, pieza_id)
    assert DOS_PERSONAS in resumen["texto"]
    assert LOGO not in resumen["texto"] and INVENTADA not in resumen["texto"]
    resumenes = sum(i.capacidad is Capacidad.RESUMIR for i in capa_modelos.invocaciones)
    assert resumenes - resumenes_antes == 1


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_aceptar_o_volver_atras_un_rechazo_no_y_si_rehacen_el_resumen(
    cliente, capa_modelos, url_bd
):
    capa_modelos.resumen = resumen_de_lo_que_vio
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, INVENTADA))

    _decidir(cliente, pieza_id, 1, "aceptada")
    assert _trabajos_de_resumen(url_bd, pieza_id) == []

    _decidir(cliente, pieza_id, 2, "rechazada")
    assert INVENTADA not in _esperar_resumen(cliente, pieza_id)["texto"]
    # Cambiar de idea también cambia qué entra al resumen.
    _decidir(cliente, pieza_id, 2, "aceptada")
    assert INVENTADA in _esperar_resumen(cliente, pieza_id)["texto"]
    assert _trabajos_de_resumen(url_bd, pieza_id) == ["completado", "completado"]


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_el_resumen_rehecho_guardado_sin_cambios_figura_como_propuesto_por_un_modelo(
    cliente, capa_modelos
):
    capa_modelos.resumen = resumen_de_lo_que_vio
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, INVENTADA))
    _decidir(cliente, pieza_id, 1, "aceptada")
    _decidir(cliente, pieza_id, 2, "rechazada")
    rehecho = _esperar_resumen(cliente, pieza_id)["texto"]

    def firmar(resumen: str, version_base: int) -> dict[str, Any]:
        respuesta = cliente.put(
            f"/api/piezas/{pieza_id}/ficha",
            json=EDICION_COMPLETA | {
                "version_base": version_base, "tipo": "fotografia", "cuerpo": CUERPO_LEIDO,
                "resumen": resumen,
            },
        )
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["estado"] == "Validada"
        ficha: dict[str, Any] = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
        return ficha

    # Distinto del de la v1, pero no lo escribió la persona: lo rehízo un modelo.
    assert firmar(rehecho, 1)["procedencia"]["resumen"] == "modelo"
    assert firmar(f"{rehecho} Corregido.", 2)["procedencia"]["resumen"] == "persona"


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_el_resumen_rehecho_no_pisa_el_que_guardo_la_persona(cliente, capa_modelos):
    capa_modelos.resumen = resumen_de_lo_que_vio
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, INVENTADA))
    propio = "Cartel de una jornada, escrito por quien valida."
    guardada = cliente.put(
        f"/api/piezas/{pieza_id}/ficha",
        json=EDICION_COMPLETA | {"tipo": "fotografia", "cuerpo": CUERPO_LEIDO, "resumen": propio},
    )
    assert guardada.json()["estado"] == "Incompleta"  # quedan Afirmaciones sin decidir

    _decidir(cliente, pieza_id, 2, "rechazada")
    resumen = _esperar_resumen(cliente, pieza_id)

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["frontmatter"]["resumen"], ficha["procedencia"]["resumen"]) == (propio, "persona")
    # El nuevo queda ofrecido: la pantalla no lo pone en el campo (lo elige la persona).
    assert resumen["estado"] == "listo" and INVENTADA not in resumen["texto"]
    assert resumen["atendido"] is False
    # Guardar después de que llegó: la pantalla ya lo ofreció y no vuelve a avisar.
    cliente.put(
        f"/api/piezas/{pieza_id}/ficha",
        json=EDICION_COMPLETA | {
            "version_base": 2, "tipo": "fotografia", "cuerpo": CUERPO_LEIDO, "resumen": propio,
        },
    )
    assert _resumen(cliente, pieza_id)["atendido"] is True  # type: ignore[index]


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_si_el_rehecho_falla_la_pantalla_lo_sabe_y_el_resumen_queda_como_estaba(
    cliente, capa_modelos
):
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, INVENTADA))
    capa_modelos.romper(LimiteExcedido("resumir", "tiempo", 60, 61), en=Capacidad.RESUMIR)

    _decidir(cliente, pieza_id, 2, "rechazada")

    assert _esperar_resumen(cliente, pieza_id) == {
        "estado": "fallido", "texto": None, "rehecho_el": None, "atendido": False,
        "informe_inyeccion": None, "frenado": False,
    }


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_quien_no_valida_no_ve_el_resumen_rehecho(cliente, capa_modelos):
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, INVENTADA))
    _decidir(cliente, pieza_id, 1, "aceptada")
    _decidir(cliente, pieza_id, 2, "rechazada")
    _esperar_resumen(cliente, pieza_id)
    _firmar(cliente, pieza_id)  # la Directora de Carrera ve las Validadas

    vista = entrar(cliente, DIRECTORA_DE_CARRERA).get(f"/api/piezas/{pieza_id}/afirmaciones")

    assert vista.status_code == 200
    assert vista.json()["resumen"] is None


@EN_LA_APP
@pytest.mark.rf("RF-09")
def test_un_rechazo_que_se_guarda_mientras_se_toma_el_trabajo_no_se_pierde(
    cliente, capa_modelos, url_bd
):
    capa_modelos.resumen = resumen_de_lo_que_vio
    pieza_id = _foto_pendiente(cliente, imagen_con_afirmaciones(DOS_PERSONAS, LOGO, INVENTADA))
    _frenar(cliente, True)
    _decidir(cliente, pieza_id, 2, "rechazada")  # deja un trabajo encolado
    with psycopg.connect(url_bd) as candado:
        # Otra decisión a medio guardar: su encolado no hizo nada porque ya había uno.
        candado.execute("SELECT 1 FROM acervo.pieza WHERE pieza_id = %s FOR UPDATE", (pieza_id,))
        candado.execute(
            "UPDATE acervo.afirmacion a SET estado = 'rechazada', decidida_por = %s,"
            " decidida_el = now() FROM acervo.ficha f WHERE f.ficha_id = a.ficha_id"
            " AND f.pieza_id = %s AND a.orden = 3",
            (MARIA, pieza_id),
        )
        _frenar(cliente, False)  # el trabajador toma el encolado y espera detrás de la decisión
        esperar_detras_del_candado(candado, 1)
        candado.commit()

    resumen = _esperar_resumen(cliente, pieza_id)
    assert DOS_PERSONAS in resumen["texto"]
    assert LOGO not in resumen["texto"] and INVENTADA not in resumen["texto"]
