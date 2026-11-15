"""Umbral de relevancia (133): una Ficha cuyo mejor fragmento queda debajo del umbral no entra
como Fuente, ni en la Conversación ni en `/v1`.

Reproduce el caso de la PC del 21-oct-2026 con embeddings fijos: las similitudes coseno que dio
`bge-m3` para la pregunta del hackathon, 0,6397 contra la Memoria y 0,2759 contra la Fotografía
de la fachada. La app de la suite busca sin corte (la capa falsa no es `bge-m3`): cada test le
pone el umbral calibrado.
"""

from __future__ import annotations

from collections.abc import Callable
from math import sqrt
from uuid import UUID

import pytest

import capa_modelos_falsa
from historia_viva.busqueda import UMBRAL_DE_SIMILITUD, Consulta
from historia_viva.composicion import Entorno, EntornoInvalido
from test_busqueda import bd, indexacion, trabajador, validada  # noqa: F401 — fixtures
from test_conversacion import nombres, nueva, turno

pytestmark = [
    pytest.mark.xdist_group("indice"),
    pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools"),
]

PREGUNTA = "en que puesto salio juan ignacio goñe en el hackathon de ciberdefensa"
MEMORIA = (
    "--- Página 1 ---\n\nHACKATHON CYBER.AR 2026. El equipo de Juan Ignacio Goñe salió segundo"
    " en el hackathon de ciberdefensa."
)
FOTOGRAFIA = "--- Página 1 ---\n\nLa fotografía muestra la fachada de un edificio blanco de la FIE."


def _vector_fijo(texto: str, dimension: int) -> tuple[float, ...]:
    """La pregunta es el eje 0; lo que habla del hackathon queda a 0,6397 de ella y el resto,
    a 0,2759, en ejes propios: el coseno da exactamente lo medido."""
    vector = [0.0] * dimension
    if texto == PREGUNTA:
        vector[0] = 1.0
    else:
        eje, similitud = (1, 0.6397) if "hackathon" in texto.lower() else (2, 0.2759)
        vector[0], vector[eje] = similitud, sqrt(1 - similitud**2)
    return tuple(vector)


@pytest.fixture
def con_umbral(cliente, monkeypatch) -> Callable[[float], None]:
    monkeypatch.setattr(capa_modelos_falsa, "_vector", _vector_fijo)
    return lambda umbral: monkeypatch.setattr(cliente.app.state.busqueda, "_umbral", umbral)


@pytest.mark.rf("RF-21", "RF-24", "RF-47")
def test_la_fachada_no_es_fuente_de_la_pregunta_del_hackathon_y_la_memoria_si(
    cliente, validada, con_umbral
):
    memoria, fotografia = UUID(validada(MEMORIA)), UUID(validada(FOTOGRAFIA))
    busqueda = cliente.app.state.busqueda

    def fuentes() -> tuple[list[UUID], set[UUID]]:
        recuperacion = busqueda.recuperar(Consulta(texto=PREGUNTA))
        planos = busqueda.buscar_fragmentos(Consulta(texto=PREGUNTA), limite=50)
        return (
            [r.pieza_id for r in recuperacion.resultado.resultados],
            {h.pieza_id for h, _ in planos},
        )

    # Sin corte, el caso de la PC: la fachada aparece detrás de la Memoria.
    con_umbral(0.0)
    assert fuentes() == ([memoria, fotografia], {memoria, fotografia})

    con_umbral(UMBRAL_DE_SIMILITUD)
    assert fuentes() == ([memoria], {memoria})


@pytest.mark.rf("RF-47")
@pytest.mark.historia(251)
def test_con_todas_las_fichas_debajo_del_umbral_la_conversacion_responde_sin_evidencia(
    cliente, validada, con_umbral, capa_modelos
):
    validada(FOTOGRAFIA)
    con_umbral(UMBRAL_DE_SIMILITUD)

    _, eventos = turno(cliente, nueva(cliente), PREGUNTA)

    assert nombres(eventos) == ["fuentes", "fin"]
    assert (dict(eventos)["fuentes"]["total"], dict(eventos)["fin"]["sin_evidencia"]) == (0, True)
    assert not [i for i in capa_modelos.invocaciones if i.capacidad == "responder"]


@pytest.mark.rf("RF-21")
def test_el_umbral_se_configura_con_hv_umbral_similitud():
    assert Entorno.desde_variables({}).umbral_de_similitud == UMBRAL_DE_SIMILITUD
    assert Entorno.desde_variables({"HV_UMBRAL_SIMILITUD": "0.5"}).umbral_de_similitud == 0.5
    assert Entorno.desde_variables({"HV_UMBRAL_SIMILITUD": "0"}).umbral_de_similitud == 0.0
    for invalido in ("1", "-0.1", "medio", "nan"):
        with pytest.raises(EntornoInvalido, match="HV_UMBRAL_SIMILITUD"):
            Entorno.desde_variables({"HV_UMBRAL_SIMILITUD": invalido})
