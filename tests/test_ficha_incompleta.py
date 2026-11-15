"""Guardar una Ficha a la que le falta un dato duro, y retomarla después.

Es el caso que decide si el flujo aguanta: quien valida no conoce el dato, guarda igual, no
pierde el trabajo y vuelve cuando lo consigue. Si en vez de eso hay que inventar el dato o
abandonar la Pieza, el diseño entero del MVP está apoyado en algo que no ocurre.

La pantalla no se prueba acá. Lo que se prueba es que la Ficha quede `Incompleta` con el
trabajo conservado y sin firma, que la respuesta diga qué falta, y que reabrirla devuelva lo
guardado y no la propuesta del modelo (user stories 18 y 20).
"""

from __future__ import annotations

import pytest
from capa_modelos_falsa import TRANSCRIPCION_FIJA
from validacion import (
    CON_CONTEXTO,
    CONTEXTO,
    EDICION_COMPLETA,
    MARIA,
    TRANSCRIPCION_CORREGIDA,
    edicion_sin,
)

pytestmark = [
    pytest.mark.rf("RF-14"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


def test_guardar_sin_origen_deja_la_ficha_incompleta_sin_firma_y_dice_que_falta(
    cliente, pieza_pendiente
):
    # De dónde salió la pieza es lo que quien valida no siempre sabe: el resto lo completó.
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | CON_CONTEXTO,
    )

    assert respuesta.status_code == 200
    resultado = respuesta.json()
    assert resultado["estado"] == "Incompleta"
    assert resultado["campos_faltantes"] == ["origen"]
    # Sin los obligatorios completos nadie firma: `validada_por` es lo que hace responsable
    # a una persona de un dato, y este dato todavía no está (ADR-0002).
    assert resultado["validada_por"] is None
    assert resultado["validada_el"] is None

    # El trabajo quedó guardado: el cuerpo corregido, el Contexto y los campos que sí sabía.
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert (ficha["cuerpo"], ficha["contexto"]) == (TRANSCRIPCION_FIJA, CONTEXTO)
    assert ficha["frontmatter"]["tipo"] == "acta"
    assert ficha["frontmatter"]["resumen"] == EDICION_COMPLETA["resumen"]
    assert ficha["frontmatter"]["estado"] == "Incompleta"
    assert ficha["faltantes"] == ["origen"]


def test_guardar_sin_ninguno_de_los_datos_duros_no_pierde_el_trabajo_y_dice_que_falta(
    cliente, pieza_pendiente
):
    # "Guardar como está" es el otro botón de la misma pantalla y manda todo en nulo salvo
    # el cuerpo. Que falte un dato no es un error: la Ficha queda Incompleta (US 18).
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json={
            "version_base": 1,
            "resumen": EDICION_COMPLETA["resumen"],
            "cuerpo": TRANSCRIPCION_FIJA,
            "contexto": CONTEXTO,
        },
    )

    assert respuesta.status_code == 200
    resultado = respuesta.json()
    assert resultado["estado"] == "Incompleta"
    assert resultado["campos_faltantes"] == [
        "tipo",
        "tipo_actividad",
        "fecha_inicio",
        "fecha_fin",
        "precision_fecha",
        "origen",
        "propietario",
    ]
    assert resultado["validada_por"] is None
    # Nada de lo escrito se perdió, y sigue habiendo una sola Ficha vigente.
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert (ficha["cuerpo"], ficha["contexto"]) == (TRANSCRIPCION_FIJA, CONTEXTO)
    assert ficha["version"] == 2


def test_una_ficha_incompleta_reabierta_devuelve_lo_guardado_y_no_la_propuesta(
    cliente, pieza_pendiente
):
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | {"cuerpo": TRANSCRIPCION_CORREGIDA, "contexto": CONTEXTO},
    )

    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    # Lo que abre la pantalla al apretar "Continuar" es la versión que dejó la persona, no
    # la del modelo: si volviera la propuesta, el trabajo estaría perdido igual (US 20).
    assert (ficha["version"], ficha["origen_version"]) == (2, "persona")
    assert (ficha["cuerpo"], ficha["contexto"]) == (TRANSCRIPCION_CORREGIDA, CONTEXTO)
    assert ficha["frontmatter"]["tipo"] == "acta"
    assert ficha["propuesta"]["cuerpo"] == TRANSCRIPCION_FIJA
    assert ficha["propuesta"]["frontmatter"]["tipo"] is None


def test_una_ficha_incompleta_completada_queda_validada_a_nombre_de_quien_la_retomo(
    cliente, pieza_pendiente
):
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | CON_CONTEXTO,
    )

    # Consiguió el dato que le faltaba y vuelve a la misma pantalla a cerrarla.
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | CON_CONTEXTO | {"version_base": 2},
    )

    assert respuesta.status_code == 200
    resultado = respuesta.json()
    assert resultado["estado"] == "Validada"
    assert resultado["campos_faltantes"] == []
    assert resultado["validada_por"] == MARIA
    assert resultado["validada_el"]
    # Cada paso deja su versión: la propuesta, lo incompleto y lo firmado conviven.
    assert resultado["version"] == 3

    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert ficha["faltantes"] == []
    assert (ficha["cuerpo"], ficha["contexto"]) == (TRANSCRIPCION_FIJA, CONTEXTO)
    assert ficha["propuesta"]["cuerpo"] == TRANSCRIPCION_FIJA


def test_una_ficha_incompleta_se_ofrece_para_continuar_en_la_lista(cliente, pieza_pendiente):
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | CON_CONTEXTO,
    )

    (fila,) = cliente.get("/api/piezas?estado=Incompleta").json()["piezas"]

    # La lista es lo que le dice a la persona qué le queda por hacer: una Incompleta se
    # distingue de una Pendiente por el estado y por qué campo le falta (US 21).
    assert fila["pieza_id"] == pieza_pendiente
    assert fila["estado"] == "Incompleta"
    assert fila["campos_faltantes"] == ["origen"]


def test_reabrir_una_validada_y_dejarla_sin_un_dato_duro_le_saca_la_firma(
    cliente, pieza_pendiente
):
    cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    # Alguien vuelve sobre una Ficha ya firmada y borra un dato que resultó estar mal.
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen") | CON_CONTEXTO | {"version_base": 2},
    )

    resultado = respuesta.json()
    assert resultado["estado"] == "Incompleta"
    # Nadie queda firmando una Ficha que ya no está completa: la firma se va con el dato.
    assert resultado["validada_por"] is None
    frontmatter = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()["frontmatter"]
    assert frontmatter["validada_por"] is None
    assert frontmatter["validada_el"] is None
    assert frontmatter["modo_validacion"] is None


@pytest.mark.parametrize(
    "ausentes, esperados",
    [
        ((), []),
        (("tipo",), ["tipo"]),
        (("tipo_actividad",), ["tipo_actividad"]),
        (("origen",), ["origen"]),
        (("propietario",), ["propietario"]),
        (("resumen",), ["resumen"]),
        (("fecha_inicio", "precision_fecha"), ["fecha_inicio", "fecha_fin", "precision_fecha"]),
        (("tipo", "origen"), ["tipo", "origen"]),
        (("propietario", "tipo_actividad"), ["tipo_actividad", "propietario"]),
    ],
)
@pytest.mark.rf("RF-51", "RF-52")
@pytest.mark.historia(117, 288)
def test_campos_faltantes_esta_vacio_si_y_solo_si_la_ficha_quedo_validada(
    cliente, pieza_pendiente, ausentes, esperados
):
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin(*ausentes) | CON_CONTEXTO,
    )

    assert respuesta.status_code == 200
    resultado = respuesta.json()
    # Los esperados van escritos a mano y no derivados de `CAMPOS_OBLIGATORIOS`: derivarlos
    # de la misma constante que lee el código volvería el test una tautología. Agregar o
    # reordenar un obligatorio tiene que romper acá, y el caso sin campos ausentes —que
    # espera la lista vacía— es el que garantiza que rompa.
    assert resultado["campos_faltantes"] == esperados
    # La equivalencia es la regla, no una coincidencia de estos casos: una Ficha sin nada
    # que falte está Validada, y una a la que le falta algo está Incompleta.
    assert (resultado["campos_faltantes"] == []) == (resultado["estado"] == "Validada")
    # Guardar nunca devuelve a la Pieza a un estado anterior al trabajo de la persona.
    assert resultado["estado"] in {"Validada", "Incompleta"}
