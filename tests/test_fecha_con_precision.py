"""Rango de fechas: la Fecha es un rango, siempre, y ningún camino guarda uno inválido.

`CONTEXT.md` define la Fecha como "un rango con inicio y fin. Una fecha exacta es un rango
de un solo día; 'entre 2015 y 2019' es un rango legítimo y suficiente para indexar". Este
archivo es el test "Rango de fechas" de `.scratch/prototipo/spec.md`, y completa el de
"Integridad del frontmatter" por el lado de la Fecha: `test_ficha_markdown.py` prueba que el
texto de la Ficha sobreviva a lo que se le escriba, y esto prueba que ningún `PUT` llegue a
escribirle un rango que la deje incoherente.

Se ejercita el borde HTTP contra Postgres real y no `derivar_rango` aislada: la restricción
`pieza_precision_coherente` es la red de seguridad de esa función, y un rango mal normalizado
solo se delata cuando la base lo rechaza. Cada guardado se vuelve a leer con un `GET`, que
reconstruye la Ficha analizando su markdown: un frontmatter ilegible no pasa ese viaje.
"""

from __future__ import annotations

import pytest
from validacion import EDICION_COMPLETA, edicion_sin

pytestmark = [
    pytest.mark.rf("RF-13"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]

FECHAS_VALIDAS = {
    # Una fecha exacta es un rango de un solo día.
    "exacta": ({"fecha_inicio": "1975-05-03", "precision_fecha": "dia"}, "1975-05-03", "1975-05-03"),
    # En mes, año y década el borde se deriva y la fecha declarada se normaliza: alcanza con
    # que caiga dentro del período. Por eso los casos mandan el **17 de mayo** y no el
    # primero de nada: si el servidor guardara la fecha recibida en vez del borde derivado,
    # `pieza_precision_coherente` lo rechazaría y el `PUT` daría 500. Que la normalización
    # ocurra no se comprueba con un assert: se comprueba con que la base no explote.
    "mes": ({"fecha_inicio": "1975-05-17", "precision_fecha": "mes"}, "1975-05-01", "1975-05-31"),
    "año": ({"fecha_inicio": "1975-05-17", "precision_fecha": "anio"}, "1975-01-01", "1975-12-31"),
    "década": (
        {"fecha_inicio": "1975-05-17", "precision_fecha": "decada"},
        "1970-01-01",
        "1979-12-31",
    ),
    # La quinta precisión, la de la decisión D-P1: los dos bordes son el dato, no se derivan.
    "entre 2015 y 2019": (
        {"fecha_inicio": "2015-01-01", "precision_fecha": "rango", "fecha_fin": "2019-12-31"},
        "2015-01-01",
        "2019-12-31",
    ),
    # El borde del borde: `rango` solo exige que el fin no preceda al inicio, así que un
    # rango de un solo día es válido aunque `dia` diga lo mismo con otra precisión.
    "rango de un solo día": (
        {"fecha_inicio": "2015-01-01", "precision_fecha": "rango", "fecha_fin": "2015-01-01"},
        "2015-01-01",
        "2015-01-01",
    ),
    # La pantalla infiere la precisión de qué campos se completaron: "Desde marzo de 1985,
    # Hasta junio de 1987" va del comienzo de un mes al final del otro. Los bordes no son
    # años completos y el rango tiene que guardarse igual, tal cual llegó.
    "entre marzo de 1985 y junio de 1987": (
        {"fecha_inicio": "1985-03-01", "precision_fecha": "rango", "fecha_fin": "1987-06-30"},
        "1985-03-01",
        "1987-06-30",
    ),
    "entre dos días sueltos": (
        {"fecha_inicio": "1985-03-15", "precision_fecha": "rango", "fecha_fin": "1985-04-02"},
        "1985-03-15",
        "1985-04-02",
    ),
}
"""Las Fechas que se guardan, con los bordes que tienen que quedar escritos.

Los bordes van a mano y no derivados de `derivar_rango`: leerlos de la misma función que
escribe la Ficha volvería el test una tautología, y cambiar cómo se deriva una década tiene
que romper acá.
"""


# ── Rango de fechas ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caso", list(FECHAS_VALIDAS))
def test_cada_precision_guarda_los_bordes_que_le_corresponden(cliente, pieza_pendiente, caso):
    """Las cinco precisiones, cada una con el rango que le toca.

    Es el criterio del issue 06 en una tabla: una fecha exacta es un rango de un día, mes,
    año y década se guardan con sus bordes derivados, y "entre 2015 y 2019" se guarda como
    rango y la Ficha queda `Validada` y firmada (user story 13, decisión D-P1).
    """
    fecha, inicio, fin = FECHAS_VALIDAS[caso]

    guardado = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA | fecha)

    assert guardado.status_code == 200
    resultado = guardado.json()
    assert resultado["estado"] == "Validada"
    assert resultado["campos_faltantes"] == []
    assert resultado["validada_por"]

    frontmatter = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()["frontmatter"]
    assert (frontmatter["fecha_inicio"], frontmatter["fecha_fin"]) == (inicio, fin)
    assert frontmatter["precision_fecha"] == fecha["precision_fecha"]


def test_la_fecha_normalizada_es_la_que_se_relee_al_retomar_la_ficha(cliente, pieza_pendiente):
    """Al reabrir vuelve el rango derivado, no la fecha que se escribió (user story 20).

    Es lo que la pantalla usa para reconstruir el selector: con `decada` reconstruye "los
    años 70" desde el 1970-01-01, y si volviera el 17 de mayo original el control quedaría
    mostrando otra cosa que la que se guardó.
    """
    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha",
        json=edicion_sin("resumen") | {"fecha_inicio": "1975-05-17", "precision_fecha": "decada"},
    )

    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    # Falta el resumen, así que la Ficha quedó `Incompleta`: la Fecha se conserva igual.
    assert ficha["frontmatter"]["estado"] == "Incompleta"
    assert ficha["faltantes"] == ["resumen"]
    assert ficha["frontmatter"]["fecha_inicio"] == "1970-01-01"
    assert ficha["frontmatter"]["fecha_fin"] == "1979-12-31"


def test_un_selector_de_fecha_a_medio_completar_deja_la_ficha_incompleta_y_no_es_un_error(
    cliente, pieza_pendiente
):
    """Sin precisión no hay rango que derivar, pero tampoco hay nada que rechazar.

    Es la diferencia entre "falta un dato" y "el dato está mal": lo primero deja la Ficha
    `Incompleta` con el trabajo guardado (user story 18); lo segundo es un 422. En la
    pantalla es el mismo caso: la Fecha cuenta como faltante hasta que el selector elegido
    esté completo, y recién ahí el pedido lleva los tres campos.
    """
    guardado = cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha", json=edicion_sin("precision_fecha")
    )

    assert guardado.status_code == 200
    resultado = guardado.json()
    assert resultado["estado"] == "Incompleta"
    assert resultado["campos_faltantes"] == ["fecha_fin", "precision_fecha"]


# ── Integridad del frontmatter ─────────────────────────────────────────────────

FECHAS_QUE_NO_SE_GUARDAN = {
    # El fin precede al inicio. Es E2 de CU-05: la pantalla ya lo marca mientras se escribe,
    # y el servidor lo rechaza igual porque la pantalla no es la autoridad.
    "rango invertido": (
        EDICION_COMPLETA
        | {"fecha_inicio": "2019-01-01", "precision_fecha": "rango", "fecha_fin": "2015-12-31"},
        "no puede preceder a su inicio",
    ),
    # `rango` es la única precisión cuyo fin no se deriva: sin él no hay rango.
    "rango sin fin": (
        EDICION_COMPLETA | {"fecha_inicio": "2015-01-01", "precision_fecha": "rango"},
        "exige una fecha de fin",
    ),
    # Con las otras cuatro el fin se deriva, y uno que lo contradiga no se puede honrar sin
    # guardar un rango que viola `pieza_precision_coherente`.
    "fin que contradice la precisión": (
        EDICION_COMPLETA
        | {"fecha_inicio": "1975-05-03", "precision_fecha": "mes", "fecha_fin": "1975-06-30"},
        "contradice la precision",
    ),
    # Sin precisión declarada el fin no se deriva de nada: guardarlo sería inventar el rango.
    "fin sin precisión": (
        edicion_sin("precision_fecha") | {"fecha_fin": "1975-12-31"},
        "no se declara sola",
    ),
}
"""Los cuatro pedidos que no se guardan, con un fragmento del `detail` que los distingue.

El fragmento no es decoración: `api/ficha.py` traduce **cualquier** `ValueError` de
`guardar_edicion` al mismo `type` y al mismo `campo`, así que sin él los cuatro casos serían
indistinguibles y un error ajeno a la Fecha pasaría por cualquiera de ellos.
"""


@pytest.mark.parametrize("caso", list(FECHAS_QUE_NO_SE_GUARDAN))
def test_ningun_camino_de_la_api_guarda_un_rango_de_fechas_invalido(
    cliente, pieza_pendiente, caso
):
    edicion, fragmento = FECHAS_QUE_NO_SE_GUARDAN[caso]

    respuesta = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=edicion)

    assert respuesta.status_code == 422
    problema = respuesta.json()
    assert problema["type"] == "/errores/fecha-invalida"
    # La pantalla enfoca el campo que nombra el problema: con la Fecha siempre es el fin,
    # porque es el único de los tres que una persona declara o deja derivar.
    assert problema["campo"] == "fecha_fin"
    # `detail` está en castellano porque se le muestra a la persona, y es lo único que dice
    # cuál de los cuatro problemas es.
    assert fragmento in problema["detail"]


@pytest.mark.parametrize("caso", list(FECHAS_QUE_NO_SE_GUARDAN))
def test_un_rango_rechazado_no_deja_nada_a_medio_guardar(cliente, pieza_pendiente, caso):
    """El 422 se levanta antes de abrir la transacción: la Pieza queda donde estaba.

    Sin esto, un rango inválido podría dejar la Ficha versionada y la Pieza movida de
    estado, que es la forma en que un error de validación se convierte en datos rotos.
    """
    antes = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha", json=FECHAS_QUE_NO_SE_GUARDAN[caso][0]
    )

    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Pendiente"
    assert cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json() == antes


@pytest.mark.parametrize("caso", list(FECHAS_VALIDAS))
def test_toda_fecha_aceptada_deja_un_frontmatter_que_se_vuelve_a_leer(
    cliente, pieza_pendiente, caso
):
    """Guardar y releer: el `GET` reconstruye la Ficha analizando su markdown.

    Si el frontmatter quedara malformado, este viaje de vuelta falla, y falla acá y no en la
    sesión con la SEU. Es el complemento del test de `test_ficha_markdown.py`, que prueba el
    texto en unidad: esto prueba que las cinco precisiones llegan a ese texto y vuelven.
    """
    fecha, inicio, fin = FECHAS_VALIDAS[caso]
    assert cliente.put(
        f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA | fecha
    ).status_code == 200

    ficha = cliente.get(f"/api/piezas/{pieza_pendiente}/ficha").json()

    frontmatter = ficha["frontmatter"]
    assert frontmatter["estado"] == "Validada"
    assert (frontmatter["fecha_inicio"], frontmatter["fecha_fin"]) == (inicio, fin)
    assert frontmatter["resumen"] == EDICION_COMPLETA["resumen"]
    assert ficha["cuerpo"] == EDICION_COMPLETA["cuerpo"]
