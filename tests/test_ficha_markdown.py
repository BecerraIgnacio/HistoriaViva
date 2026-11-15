"""Integridad del frontmatter: lo que se guarda es lo que se vuelve a leer.

Es la única prueba de la suite que no pasa por HTTP, y no es un doble: la Ficha es texto y
esto ejercita el texto. Si el frontmatter no sobrevive a un resumen con comillas o con un
salto de línea, la Ficha guardada queda ilegible y no hay endpoint que lo delate.
"""

from __future__ import annotations

from datetime import date, datetime, UTC
from uuid import UUID

import pytest

from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.tipos import CamposFicha
from historia_viva.fichas.markdown import analizar, serializar

pytestmark = pytest.mark.rf("RF-13")

PIEZA = UUID("9c0f4b21-8e35-4d17-b0a6-15c8e2f7a3d9")
PERSONA = UUID("11111111-1111-4111-8111-111111111111")


def test_una_propuesta_recien_creada_vuelve_igual():
    campos = CamposFicha(
        id=PIEZA,
        resumen="Acta que documenta la fundación de la Facultad.",
        estado=Estado.PENDIENTE,
        archivo_original="local://originales/9c0f4b21",
    )

    leidos, cuerpo = analizar(serializar(campos, "## Página 1\n\nACTA N.º 12.\n"))

    assert leidos == campos
    assert cuerpo == "## Página 1\n\nACTA N.º 12.\n"


def test_una_ficha_validada_vuelve_igual_con_todos_los_campos_escritos():
    campos = CamposFicha(
        id=PIEZA,
        tipo="acta",
        fecha_inicio=date(2015, 1, 1),
        fecha_fin=date(2019, 12, 31),
        precision_fecha=PrecisionFecha.RANGO,
        origen="Archivo de la SEU",
        resumen="Formación del cuerpo de cadetes.",
        estado=Estado.VALIDADA,
        validada_por=PERSONA,
        validada_el=datetime(2026, 10, 5, 14, 2, 18, tzinfo=UTC),
        modo_validacion="individual",
        archivo_original="local://originales/9c0f4b21",
    )

    leidos, cuerpo = analizar(serializar(campos, "Cuerpo."))

    assert leidos == campos
    assert cuerpo == "Cuerpo."


@pytest.mark.parametrize(
    "resumen",
    [
        'Acta "de fundación" del Consejo.',
        "Resumen con\nsalto de línea.",
        "Barra invertida \\ y dos puntos: acá.",
        "---",
    ],
)
def test_un_resumen_con_caracteres_de_yaml_no_rompe_la_ficha(resumen):
    campos = CamposFicha(id=PIEZA, resumen=resumen, estado=Estado.PENDIENTE)

    leidos, cuerpo = analizar(serializar(campos, "---\nUn cuerpo que empieza con guiones."))

    assert leidos.resumen == resumen
    assert cuerpo == "---\nUn cuerpo que empieza con guiones."


def test_un_markdown_sin_frontmatter_no_se_lee_como_ficha():
    with pytest.raises(ValueError):
        analizar("Solo cuerpo, sin frontmatter.")


def test_una_ficha_anterior_al_tipo_de_actividad_y_al_propietario_se_lee_sin_ellos():
    # Guardada antes de 023: el frontmatter no trae esas dos líneas y no por eso es ilegible.
    campos = CamposFicha(id=PIEZA, resumen="Acta de 1975.", estado=Estado.PENDIENTE)
    anterior = "\n".join(
        linea
        for linea in serializar(campos, "Cuerpo.").split("\n")
        if not linea.startswith(("tipo_actividad:", "propietario:"))
    )

    assert analizar(anterior) == (campos, "Cuerpo.")


@pytest.mark.parametrize("falta", ["tipo_actividad:", "propietario:"])
def test_una_ficha_con_uno_solo_de_los_dos_campos_nuevos_no_se_lee(falta):
    # Ni anterior a 023 (no trae ninguno) ni posterior (trae los dos).
    campos = CamposFicha(id=PIEZA, resumen="Acta de 1975.", estado=Estado.PENDIENTE)
    a_medias = "\n".join(
        linea for linea in serializar(campos, "Cuerpo.").split("\n") if not linea.startswith(falta)
    )

    with pytest.raises(ValueError):
        analizar(a_medias)


@pytest.mark.rf("RF-50", "RF-52")
@pytest.mark.parametrize("carreras", [(), ("ing-civil",), ("ing-civil", "ing-mecanica")])
def test_el_autor_y_las_carreras_vuelven_iguales(carreras):
    campos = CamposFicha(
        id=PIEZA, autor="Desconocido", carreras=carreras, estado=Estado.PENDIENTE
    )

    assert analizar(serializar(campos, "Cuerpo.")) == (campos, "Cuerpo.")


@pytest.mark.rf("RF-50")
def test_las_carreras_se_escriben_como_una_lista_yaml_de_una_linea():
    campos = CamposFicha(id=PIEZA, carreras=("ing-civil", "ing-mecanica"))

    assert 'carreras: ["ing-civil", "ing-mecanica"]' in serializar(campos, "").split("\n")


@pytest.mark.rf("RF-50", "RF-52")
def test_una_ficha_anterior_al_autor_y_las_carreras_se_lee_sin_ellos():
    campos = CamposFicha(id=PIEZA, resumen="Acta de 1975.", estado=Estado.PENDIENTE)
    anterior = "\n".join(
        linea
        for linea in serializar(campos, "Cuerpo.").split("\n")
        if not linea.startswith(("autor:", "carreras:"))
    )

    assert analizar(anterior) == (campos, "Cuerpo.")


@pytest.mark.rf("RF-50", "RF-52")
@pytest.mark.parametrize(
    "linea",
    ['carreras: "ing-civil"', 'carreras: ["ing-civil" "ing-mecanica"]', "carreras: [ing-civil]"],
)
def test_una_lista_de_carreras_mal_escrita_no_se_lee(linea):
    campos = CamposFicha(id=PIEZA)
    texto = serializar(campos, "").replace("carreras: []", linea)

    with pytest.raises(ValueError):
        analizar(texto)


@pytest.mark.historia(243)
@pytest.mark.rf("RF-42")
def test_publicable_es_un_campo_del_frontmatter_y_una_ficha_anterior_es_publicable():
    no_publicable = CamposFicha(id=PIEZA, estado=Estado.PENDIENTE, publicable=False)
    anterior = "\n".join(
        linea
        for linea in serializar(no_publicable, "Cuerpo.").split("\n")
        if not linea.startswith("publicable:")
    )

    assert "publicable: false" in serializar(no_publicable, "").split("\n")
    assert analizar(serializar(no_publicable, "Cuerpo.")) == (no_publicable, "Cuerpo.")
    # Una Ficha guardada antes de 123 no lo trae: queda publicable, como la columna.
    assert analizar(anterior)[0].publicable is True
