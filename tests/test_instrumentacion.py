"""Medir cada validación: el reloj lo arranca el servidor y el guardado lo cierra.

Es lo que convierte la Validación SEU nº 1 de una conversación en una medición. Sin estas
filas, "¿le resultó rápido y cómodo?" se responde con la memoria de quien miraba, y la
Fase 1 entera existe para no tener que responderla así (user stories 22 a 25).

Lo que se prueba es el registro, no la pantalla: que abrir deje una fila abierta, que
guardar la cierre con el tiempo, el estado, los campos corregidos y los que faltaron, y que
nada de eso toque la Ficha.
"""

from __future__ import annotations

import re

import capa_modelos_falsa
import pytest
from aplicacion import entrar
from capa_modelos_falsa import IMAGEN, TRANSCRIPCION_FIJA
from espera import esperar_estado
from historia_viva.dominio.tipos import MetodoAutenticacion
from validacion import (
    CONTEXTO,
    EDICION_COMPLETA,
    MARIA,
    PDF,
    TRANSCRIPCION_CORREGIDA,
    abrir,
    edicion_sin,
    mediciones,
)

pytestmark = [
    pytest.mark.rf("RF-41"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


def test_abrir_la_ficha_arranca_el_reloj_en_el_servidor(cliente, pieza_pendiente):
    apertura = abrir(cliente, pieza_pendiente)

    # El `medicion_id` es lo que la pantalla devuelve al guardar, y `abierta_el` lo tomó el
    # servidor: un reloj que lleve el navegador no es un dato de la sesión (US 22).
    assert apertura["medicion_id"]
    # En UTC y al segundo, como el ejemplo del contrato: estas filas se exportan y se comparan
    # a ojo en la documentación del gate (US 25).
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", apertura["abierta_el"])

    (medicion,) = mediciones(cliente)
    assert medicion["medicion_id"] == apertura["medicion_id"]
    assert medicion["pieza_id"] == pieza_pendiente
    assert medicion["persona_id"] == MARIA
    assert medicion["nombre_archivo"] == "acta-fundacion.pdf"
    assert medicion["abierta_el"] == apertura["abierta_el"]
    # Abierta y sin guardar: también es un dato de la sesión, es una Pieza que alguien miró.
    assert medicion["guardada_el"] is None
    assert medicion["duracion_s"] is None
    assert medicion["estado_final"] is None
    assert medicion["caracteres_cuerpo_propuesta"] == len(TRANSCRIPCION_FIJA)
    assert medicion["caracteres_cuerpo_guardado"] is None


def test_validar_la_ficha_cierra_su_medicion_con_el_tiempo_y_lo_que_se_corrigio(
    cliente, pieza_pendiente
):
    apertura = abrir(cliente, pieza_pendiente)
    contexto = "Lo confirmó Secretaría Académica."
    # Así lo guarda el Módulo de Fichas: el Contexto bajo su encabezado, al final.
    cuerpo = f"{TRANSCRIPCION_CORREGIDA}\n\n## Contexto\n\n{contexto}"

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA
        | {
            "medicion_id": apertura["medicion_id"],
            "cuerpo": TRANSCRIPCION_CORREGIDA,
            "contexto": contexto,
        },
    )

    (medicion,) = mediciones(cliente)
    assert medicion["medicion_id"] == apertura["medicion_id"]
    assert medicion["guardada_el"] >= medicion["abierta_el"]
    assert medicion["duracion_s"] >= 0
    assert medicion["estado_final"] == "Validada"
    assert medicion["campos_faltantes"] == []
    # La propuesta del modelo trae el cuerpo y nada más: los campos duros los completó la
    # persona, y son exactamente los que aparecen como corregidos (US 23).
    assert medicion["campos_modificados"] == [
        "tipo",
        "tipo_actividad",
        "fecha_inicio",
        "fecha_fin",
        "precision_fecha",
        "origen",
        "propietario",
        "resumen",
    ]
    assert medicion["cuerpo_editado"] is True
    assert medicion["caracteres_cuerpo_propuesta"] == len(TRANSCRIPCION_FIJA)
    assert medicion["caracteres_cuerpo_guardado"] == len(cuerpo)


def test_guardar_sin_tocar_el_cuerpo_lo_registra_sin_editar(cliente, pieza_pendiente):
    apertura = abrir(cliente, pieza_pendiente)

    # `EDICION_COMPLETA` manda la transcripción tal como la escribió el modelo: si esto
    # apareciera como cuerpo editado, "cuánto se corrigió" no mediría nada.
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"medicion_id": apertura["medicion_id"]},
    )

    (medicion,) = mediciones(cliente)
    assert medicion["cuerpo_editado"] is False
    assert medicion["caracteres_cuerpo_guardado"] == len(TRANSCRIPCION_FIJA)
    assert medicion["caracteres_cuerpo_propuesta"] == len(TRANSCRIPCION_FIJA)


def test_guardar_sin_tocar_un_cuerpo_con_blancos_finales_lo_registra_sin_editar(
    cliente, monkeypatch
):
    # La versión 1 de un DOCX conserva los blancos finales de su último párrafo; la pantalla
    # los recibe recortados y los devuelve igual.
    monkeypatch.setattr(capa_modelos_falsa, "TRANSCRIPCION_FIJA", f"{TRANSCRIPCION_FIJA}  \t")
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ("acta-fundacion.pdf", PDF, "application/pdf")}
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    apertura = abrir(cliente, pieza_id)
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()

    cliente.put(
        f"/api/piezas/{pieza_id}/ficha",
        json=EDICION_COMPLETA
        | {"medicion_id": apertura["medicion_id"], "cuerpo": ficha["cuerpo"]},
    )

    (medicion,) = mediciones(cliente)
    assert medicion["cuerpo_editado"] is False


def test_guardar_incompleta_registra_que_obligatorios_faltaron(cliente, pieza_pendiente):
    apertura = abrir(cliente, pieza_pendiente)

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen")
        | {"medicion_id": apertura["medicion_id"], "contexto": CONTEXTO},
    )

    (medicion,) = mediciones(cliente)
    assert medicion["estado_final"] == "Incompleta"
    # Qué campo faltó es lo que distingue "la pantalla es incómoda" de "el dato no existe",
    # y es la diferencia entre rediseñar el flujo y no rediseñarlo (US 24).
    assert medicion["campos_faltantes"] == ["origen"]
    assert "origen" not in medicion["campos_modificados"]
    # Sin firma no hay modo; el método sí, el de la sesión que guardó (DEC-92).
    assert medicion["metodo_autenticacion"] == "dev"
    assert medicion["modo_validacion"] is None


@pytest.mark.historia(196)
def test_la_firma_individual_registra_clase_metodo_y_modo(cliente, pieza_pendiente):
    apertura = abrir(cliente, pieza_pendiente)
    (abierta,) = mediciones(cliente)
    # La clase se copia al abrir, como el nombre: la medición no consulta `pieza` (DEC-92).
    assert abierta["clase_original"] == "documento"
    assert abierta["metodo_autenticacion"] is None
    assert abierta["modo_validacion"] is None

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"medicion_id": apertura["medicion_id"]},
    )

    (medicion,) = mediciones(cliente)
    assert medicion["clase_original"] == "documento"
    # Una sesión `dev` firma de ensayo: la columna separa esas firmas de las reales (DEC-139).
    assert medicion["metodo_autenticacion"] == "dev"
    assert medicion["modo_validacion"] == "individual"


def test_reabrir_una_incompleta_crea_otra_medicion_sin_tocar_la_anterior(
    cliente, pieza_pendiente
):
    primera = abrir(cliente, pieza_pendiente)
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("origen")
        | {"medicion_id": primera["medicion_id"], "contexto": CONTEXTO},
    )
    cerrada = next(m for m in mediciones(cliente) if m["medicion_id"] == primera["medicion_id"])

    # Consiguió el dato que le faltaba y vuelve a la misma pantalla: eso es trabajo real y
    # se mide aparte, no se le suma al intento anterior (US 20).
    segunda = abrir(cliente, pieza_pendiente)
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA
        | {"medicion_id": segunda["medicion_id"], "contexto": CONTEXTO, "version_base": 2},
    )

    filas = mediciones(cliente)
    assert len(filas) == 2
    assert {fila["medicion_id"] for fila in filas} == {
        primera["medicion_id"],
        segunda["medicion_id"],
    }
    por_id = {fila["medicion_id"]: fila for fila in filas}
    assert por_id[primera["medicion_id"]] == cerrada
    assert por_id[segunda["medicion_id"]]["estado_final"] == "Validada"
    assert por_id[segunda["medicion_id"]]["campos_faltantes"] == []


def test_la_medicion_no_modifica_el_frontmatter_ni_la_propuesta(cliente, pieza_pendiente):
    antes = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    abrir(cliente, pieza_pendiente)

    # Medir es observar, no escribir: la Ficha queda exactamente como estaba, en su versión
    # 1, sin una marca de que alguien la abrió.
    assert cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json() == antes


def test_la_medicion_se_guarda_aparte_de_la_ficha(cliente, pieza_pendiente):
    apertura = abrir(cliente, pieza_pendiente)

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"medicion_id": apertura["medicion_id"]},
    )

    # Ni el `medicion_id` ni ninguna medida de la sesión ensucian el frontmatter: el acervo
    # no queda contaminado por el instrumento que lo miraba.
    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()
    assert "medicion_id" not in ficha["frontmatter"]
    assert "duracion_s" not in ficha["frontmatter"]
    assert ficha["propuesta"]["cuerpo"] == TRANSCRIPCION_FIJA


def test_las_mediciones_vuelven_de_la_mas_reciente_a_la_mas_antigua(
    cliente, pieza_pendiente
):
    primera = abrir(cliente, pieza_pendiente)
    segunda = abrir(cliente, pieza_pendiente)

    filas = mediciones(cliente)

    assert [fila["medicion_id"] for fila in filas] == [
        segunda["medicion_id"],
        primera["medicion_id"],
    ]


def test_abrir_una_pieza_todavia_recibida_responde_409(cliente, capa_modelos):
    capa_modelos.demorar()
    entrar(cliente, MARIA)
    pieza_id = cliente.post(
        "/api/piezas", files={"archivo": ("acta-fundacion.pdf", PDF, "application/pdf")}
    ).json()["pieza_id"]

    respuesta = cliente.post(f"/api/piezas/{pieza_id}/apertura")

    # Todavía no hay Ficha que abrir: el procesamiento no terminó. No es "no existe", es
    # "esperá" (US 22, contrato de `POST /api/piezas/{id}/apertura`).
    assert respuesta.status_code == 409
    assert respuesta.json()["type"] == "/errores/transicion-invalida"
    assert mediciones(cliente) == []


def test_abrir_una_pieza_que_no_existe_responde_404(cliente):
    entrar(cliente, MARIA)

    respuesta = cliente.post("/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/apertura")

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/pieza-no-encontrada"


def test_abrir_sin_sesion_responde_401(cliente):
    respuesta = cliente.post("/api/piezas/9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9/apertura")

    assert respuesta.status_code == 401


def test_listar_mediciones_sin_sesion_responde_401(cliente):
    assert cliente.get("/api/mediciones").status_code == 401


def test_guardar_sin_medicion_abierta_guarda_igual_y_no_registra_nada(
    cliente, pieza_pendiente
):
    # Una Ficha guardada sin pasar por la pantalla partida —o con un identificador que no
    # corresponde a ninguna medición— se guarda igual. La instrumentación observa la sesión;
    # no puede ser lo que la arruine.
    respuesta = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"medicion_id": "3d9a7e15-6b4c-4f28-a0d3-8e1f5c2b7904"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "Validada"
    assert mediciones(cliente) == []


def test_una_medicion_de_otra_pieza_no_se_cierra_con_el_guardado_de_esta(
    cliente, pieza_pendiente
):
    ajena = abrir(cliente, pieza_pendiente)
    otra_pieza = cliente.post(
        # Otro contenido: el mismo archivo sería un duplicado de la primera (DEC-20).
        "/api/piezas", files={"archivo": ("otra-acta.pdf", PDF + b"%otra", "application/pdf")}
    ).json()["pieza_id"]
    esperar_estado(cliente, otra_pieza, "Pendiente")

    cliente.put(
        f"/api/piezas/{otra_pieza}/ficha",
        json=EDICION_COMPLETA | {"medicion_id": ajena["medicion_id"]},
    )

    # El reloj de una Pieza no lo cierra el guardado de otra: si lo hiciera, el tiempo por
    # Pieza sería la suma de dos trabajos distintos.
    (medicion,) = mediciones(cliente)
    assert medicion["medicion_id"] == ajena["medicion_id"]
    assert medicion["guardada_el"] is None
    assert medicion["estado_final"] is None


@pytest.mark.historia(198)
def test_cada_medicion_trae_la_clase_de_original_de_su_pieza(cliente):
    entrar(cliente, MARIA)
    esperadas = {}
    for nombre, contenido, mime, clase in (
        ("acta.pdf", PDF, "application/pdf", "documento"),
        ("foto.jpg", IMAGEN, "image/jpeg", "imagen"),
        ("socios.csv", b"Nombre;Nota\nAna;10\n", "text/csv", "planilla"),
    ):
        pieza_id = cliente.post(
            "/api/piezas", files={"archivo": (nombre, contenido, mime)}
        ).json()["pieza_id"]
        esperar_estado(cliente, pieza_id, "Pendiente")
        abrir(cliente, pieza_id)
        esperadas[pieza_id] = clase

    # La clase sale de la medición misma, para comparar cuánto lleva validar cada una.
    assert {m["pieza_id"]: m["clase_original"] for m in mediciones(cliente)} == esperadas


@pytest.mark.historia(199)
@pytest.mark.parametrize("metodo", list(MetodoAutenticacion))
def test_cada_medicion_guarda_si_la_firma_fue_de_ensayo_o_real(cliente, pieza_pendiente, metodo):
    entrar(cliente, MARIA, metodo)
    apertura = abrir(cliente, pieza_pendiente)

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=EDICION_COMPLETA | {"medicion_id": apertura["medicion_id"]},
    )

    # `dev` es una firma de ensayo; `oidc`, una real (DEC-139): el análisis las separa por esto.
    (medicion,) = mediciones(cliente)
    assert medicion["metodo_autenticacion"] == metodo.value
