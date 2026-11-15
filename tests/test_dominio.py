"""`dominio`: reglas puras, sin base ni HTTP. Qué puede hacer cada uno de los cuatro roles del
Director y la marca de hoja de una planilla."""

from __future__ import annotations

from datetime import timedelta

import pytest

from historia_viva.dominio.citas import CitaDelTexto, verificar_citas
from historia_viva.dominio.cuerpo import MarcaDeHoja
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.lote import EstadoDeLote, estado_de_lote, estimar_espera
from historia_viva.dominio.permisos import Permiso, Rol


@pytest.mark.rf("RF-53")
@pytest.mark.parametrize(
    ("rol", "permisos"),
    [
        (
            "rgc",
            ("CARGAR VALIDAR CONSULTAR GESTIONAR_PERSONAS RETIRAR_PIEZAS REVISAR_EFEMERIDES"
            " APROBAR_INFORME"),
        ),
        (
            "coordinador",
            "CARGAR VALIDAR CONSULTAR REVISAR_EFEMERIDES GENERAR_INFORME APROBAR_INFORME",
        ),
        ("director_carrera", "CONSULTAR"),
        ("admin_tecnico", "CONSULTAR OPERAR_SISTEMA"),
    ],
    ids=["rgc", "coordinador", "director_carrera", "admin_tecnico"],
)
def test_tabla_de_roles_y_permisos(rol, permisos):
    # La matriz de DEC-124, tal cual la escribe el spec.
    assert {p for p in Permiso if Rol(rol).puede(p)} == {Permiso[n] for n in permisos.split()}


@pytest.mark.rf("RF-53")
def test_los_nueve_permisos_de_dec_124_son_los_unicos():
    # R-04: los permisos viejos (agentes, modelos, borrar) ya no existen.
    assert {p.name for p in Permiso} == {
        "CARGAR", "VALIDAR", "CONSULTAR", "GESTIONAR_PERSONAS", "RETIRAR_PIEZAS",
        "REVISAR_EFEMERIDES", "GENERAR_INFORME", "APROBAR_INFORME", "OPERAR_SISTEMA",
    }


@pytest.mark.rf("RF-53")
@pytest.mark.historia(281)
def test_el_interinato_le_suma_al_tecnico_gestionar_personas_y_retirar_piezas():
    interinos = {p for p in Permiso if Rol.ADMIN_TECNICO.puede(p, interino=True)}

    assert interinos == {
        Permiso.CONSULTAR, Permiso.OPERAR_SISTEMA,
        Permiso.GESTIONAR_PERSONAS, Permiso.RETIRAR_PIEZAS,
    }


@pytest.mark.rf("RF-53")
@pytest.mark.historia(280)
def test_solo_el_rgc_y_el_coordinador_validan_ni_siquiera_con_interinato():
    validan = {
        rol for rol in Rol for interino in (False, True)
        if rol.puede(Permiso.VALIDAR, interino=interino)
    }

    assert validan == {Rol.RGC, Rol.COORDINADOR}


@pytest.mark.rf("RF-38")
@pytest.mark.parametrize(
    ("marca", "texto"),
    [
        (
            MarcaDeHoja("Ingresantes 2010", 1, 500, 2000),
            "--- Hoja «Ingresantes 2010» (filas 1–500 de 2000) ---",
        ),
        # El nombre lo eligió quien armó la planilla: puede traer comillas y paréntesis.
        (
            MarcaDeHoja("Notas «finales» (2)", 3, 4, 4),
            "--- Hoja «Notas «finales» (2)» (filas 3–4 de 4) ---",
        ),
        # `planillas` anota en la marca el recorte por tope y, en un CSV, lo que detectó.
        (
            MarcaDeHoja("CSV", 1, 1000, 1340, "recortada al tope de 1000 filas"),
            "--- Hoja «CSV» (filas 1–1000 de 1340) · recortada al tope de 1000 filas ---",
        ),
        (
            MarcaDeHoja(
                "Notas «finales» (2)", 1, 20, 20, "Codificación detectada: cp1252 · Separador: ;"
            ),
            ("--- Hoja «Notas «finales» (2)» (filas 1–20 de 20)"
            " · Codificación detectada: cp1252 · Separador: ; ---"),
        ),
    ],
)
def test_la_marca_de_hoja_va_y_vuelve(marca, texto):
    assert marca.a_texto() == texto
    assert MarcaDeHoja.leer(texto) == marca


@pytest.mark.rf("RF-38")
@pytest.mark.parametrize(
    "linea",
    [
        # Toda línea de celdas empieza con `|`: una celda hostil no abre una hoja.
        "| --- Hoja «Y» (filas 1–2 de 3) --- |",
        "--- Hoja «Y» ---",
        "Ver la Hoja «Y» (filas 1–2 de 3).",
    ],
)
def test_lo_que_no_es_una_marca_de_hoja_no_se_lee_como_marca(linea):
    assert MarcaDeHoja.leer(linea) is None


# --- `verificar_citas` (052, R-14, R-15) -----------------------------------------------------


@pytest.mark.rf("RF-47")
@pytest.mark.historia(250)
def test_cada_cita_del_texto_queda_verificada_o_no_segun_los_fragmentos_recuperados():
    texto = "Se creó en 1975 [1] y se amplió en 1980 [3]; lo firmó el decano [7] [1] [0]."

    citas = verificar_citas(texto, 3)

    # En el orden en que aparecen, sin repetir: la [1] repetida es una sola Cita.
    assert citas == (
        CitaDelTexto(1, verificada=True),
        CitaDelTexto(3, verificada=True),
        CitaDelTexto(7, verificada=False),
        CitaDelTexto(0, verificada=False),
    )


@pytest.mark.rf("RF-47")
@pytest.mark.historia(250)
@pytest.mark.parametrize(
    "texto",
    [
        "Sin marcas.",
        "Un año [1975] no es una cita.",
        "Ni [a] ni [] ni [ 1 ] ni [1,2].",
        "Un dígito árabe [١] no es una marca: la pantalla tampoco lo convierte.",
        "",
    ],
)
def test_lo_que_no_es_una_marca_n_no_es_una_cita(texto):
    assert verificar_citas(texto, 5) == ()


@pytest.mark.rf("RF-47")
@pytest.mark.historia(250)
def test_sin_fragmentos_ninguna_cita_se_verifica():
    assert verificar_citas("Dato [1].", 0) == (CitaDelTexto(1, verificada=False),)


# 084 (spec §5.3): el estado del Lote no se guarda; sale de los conteos de sus Piezas no retiradas,
# con la precedencia Vacío, Procesando, En validación, Validado.
@pytest.mark.historia(72)
@pytest.mark.parametrize(
    ("conteos", "esperado"),
    [
        ({}, "Vacío"),
        ({"Recibida": 1}, "Procesando"),
        ({"Recibida": 1, "Pendiente": 3, "Validada": 5}, "Procesando"),
        ({"Pendiente": 1}, "En validación"),
        ({"Incompleta": 1, "Validada": 9}, "En validación"),
        ({"Validada": 2}, "Validado"),
        ({"Recibida": 0, "Pendiente": 0, "Incompleta": 0, "Validada": 0}, "Vacío"),
    ],
    ids=["vacio", "recibida", "recibida-manda", "pendiente", "incompleta", "validado", "ceros"],
)
def test_el_estado_del_lote_se_deriva_de_los_conteos(conteos, esperado):
    assert estado_de_lote({Estado(e): n for e, n in conteos.items()}) == EstadoDeLote(esperado)


@pytest.mark.rf("RF-44")
@pytest.mark.historia(88)
@pytest.mark.parametrize(
    ("duraciones_ms", "pendientes", "segundos"),
    [
        ([10_000, 30_000, 20_000], 4, 80),  # mediana impar: 20 s
        ([10_000, 40_000, 20_000, 30_000], 2, 50),  # mediana par: 25 s
        ([45_000], 3, 135),  # un solo trabajo terminado
        ([], 5, None),  # sin historial no hay estimación
        ([10_000], 0, None),  # sin Recibidas por procesar tampoco
    ],
    ids=["mediana-impar", "mediana-par", "un-solo-trabajo", "sin-historial", "sin-pendientes"],
)
def test_la_espera_del_lote_es_la_mediana_de_los_terminados_por_sus_pendientes(
    duraciones_ms, pendientes, segundos
):
    esperada = None if segundos is None else timedelta(seconds=segundos)
    assert estimar_espera(duraciones_ms, pendientes) == esperada
