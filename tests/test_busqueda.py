"""Búsqueda con filtros y Citas (050; spec, módulo 21 `busqueda`).

HTTP para subir, validar y buscar; Postgres real con el índice de verdad, y el trabajador
`--tipos indexacion` manejado a mano. El único doble es la capa, con `embeber` determinista por
trigramas: dos textos que comparten palabras quedan cerca. Hasta 047 validar no encola: cada
Pieza Validada se indexa con `indexacion.encolar` y una vuelta del trabajador.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import psycopg
import pytest
from docx import Document
from ollama_falso import CONFIGURACION_DE_PRUEBA, OllamaFalso

from aplicacion import entrar
from espera import esperar_estado
from historia_viva.api_agentes import Cupos, TopesDeCupo
from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.bd import Bd
from historia_viva.busqueda import Consulta
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CUERPO_FICHA,
    ETIQUETA_DOCUMENTO,
    BloqueBinario,
    BloqueDatos,
    Capacidad,
    UsoEmbedding,
)
from historia_viva.capa_modelos.local import capa_para
from historia_viva.dominio.originales import DOCX
from historia_viva import composicion
from historia_viva.composicion import armar_indexacion, armar_trabajador, main
from historia_viva.dominio.errores import ConsultaInvalida, LimiteExcedido, MotorNoDisponible
from historia_viva.dominio.tipos import FiltroDeBusqueda
from historia_viva.interruptor import frenar_todo
from historia_viva.procesamiento.cola import TipoDeTrabajo
from validacion import DIRECTORA_DE_CARRERA, EDICION_COMPLETA, MARIA, PDF

if TYPE_CHECKING:
    from historia_viva.indexacion import Indexacion
    from historia_viva.procesamiento.trabajador import Trabajador

pytestmark = [
    pytest.mark.rf("RF-19"),
    # El índice carga llama-index: un solo proceso de xdist lo paga (ticket 130).
    pytest.mark.xdist_group("indice"),
    pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools"),
]


# --------------------------------------------------------------------- el filtro, sin base ---


@pytest.mark.rf("RF-20")
@pytest.mark.parametrize(
    ("filtro", "campo"),
    [
        (FiltroDeBusqueda(desde=date(1980, 1, 1), hasta=date(1979, 12, 31)), "desde"),
        (FiltroDeBusqueda(tipos=("acta", "x\x00")), "tipo"),
        (FiltroDeBusqueda(tipos_actividad=("\x00",)), "tipo_actividad"),
        (FiltroDeBusqueda(carreras=("ing-civil\x00",)), "carrera"),
    ],
    ids=["desde-despues-de-hasta", "nul-en-tipo", "nul-en-tipo-de-actividad", "nul-en-carrera"],
)
def test_validar_rechaza_un_rango_invertido_y_un_nul(filtro, campo):
    with pytest.raises(ConsultaInvalida) as rechazo:
        filtro.validar()

    assert rechazo.value.campo == campo


def test_validar_acepta_un_dia_y_un_filtro_vacio():
    FiltroDeBusqueda(desde=date(1975, 5, 3), hasta=date(1975, 5, 3)).validar()
    FiltroDeBusqueda().validar()


# ------------------------------------------------------------- Piezas Validadas e indexadas ---


@pytest.fixture
def bd(url_app: str) -> Iterator[Bd]:
    bd = Bd(url_app, tamano_pool=2)
    yield bd
    bd.cerrar()


@pytest.fixture
def indexacion(bd, capa_modelos) -> Indexacion:
    return armar_indexacion(bd=bd, capa_para=capa_modelos.para, validar=capa_modelos.validar)


@pytest.fixture
def trabajador(bd, capa_modelos, directorio_originales) -> Trabajador:
    return armar_trabajador(
        bd=bd,
        capa_para=capa_modelos.para,
        directorio_originales=directorio_originales,
        tipos=(TipoDeTrabajo.INDEXACION,),
    )


@pytest.fixture
def validada(cliente, bd, indexacion, trabajador, capa_modelos):
    """Sube un PDF distinto, lo valida con `cambios` sobre `EDICION_COMPLETA` y lo indexa.
    Devuelve el `pieza_id`; al final `embebidos` queda vacío, como antes de buscar."""

    def validar(cuerpo: str, *, indexar: bool = True, **cambios: Any) -> str:
        entrar(cliente, MARIA)
        archivo = PDF + f"%{uuid4()}\n".encode()
        pieza_id: str = cliente.post(
            "/api/piezas", files={"archivo": ("pieza.pdf", archivo, "application/pdf")}
        ).json()["pieza_id"]
        esperar_estado(cliente, pieza_id, "Pendiente")
        respuesta = cliente.put(
            f"/api/piezas/{pieza_id}/ficha",
            json={**EDICION_COMPLETA, "cuerpo": cuerpo, **cambios},
        )
        assert respuesta.status_code == 200, respuesta.text
        if indexar:
            with bd.transaccion() as tx:
                indexacion.encolar(tx, UUID(pieza_id))
            assert trabajador.procesar_siguiente() is True
        capa_modelos.embebidos.clear()
        return pieza_id

    return validar


def buscar(cliente, **parametros: Any) -> Any:
    """`GET /api/busqueda` como la Directora de Carrera, que solo tiene CONSULTAR."""
    entrar(cliente, DIRECTORA_DE_CARRERA)
    return cliente.get("/api/busqueda", params=parametros)


def ids(respuesta) -> list[str]:
    assert respuesta.status_code == 200, respuesta.text
    return [r["pieza_id"] for r in respuesta.json()["resultados"]]


LABORATORIO = (
    "--- Página 1 ---\n\nActa de la sesión del Consejo.\n\n"
    "--- Página 2 ---\n\nSe resolvió crear el laboratorio de ensayos de materiales."
)
EGRESADOS = "--- Página 1 ---\n\nCrónica de la fiesta de egresados con orquesta y baile."


@pytest.mark.historia(134, 135)
@pytest.mark.rf("RF-21")
def test_una_consulta_sobre_el_tema_de_una_pieza_la_devuelve_primera_con_su_cita(
    cliente, validada
):
    laboratorio = validada(LABORATORIO)
    egresados = validada(EGRESADOS)

    respuesta = buscar(cliente, q="laboratorio de ensayos de materiales")

    assert ids(respuesta)[0] == laboratorio
    assert set(ids(respuesta)) == {laboratorio, egresados}
    primera = respuesta.json()["resultados"][0]
    assert primera["citas"][0] == {
        "fragmento_id": primera["citas"][0]["fragmento_id"],
        "texto": "--- Página 2 ---\n\nSe resolvió crear el laboratorio de ensayos de materiales.",
        "ubicacion_legible": "p. 2",
        "enlace": f"/api/piezas/{laboratorio}/original#page=2",
    }
    assert len(primera["citas"]) <= 3
    assert (primera["tipo"], primera["resumen"], primera["fecha_inicio"]) == (
        "acta",
        EDICION_COMPLETA["resumen"],
        "1975-05-03",
    )
    assert respuesta.json()["avisos"] == []
    assert respuesta.headers["Server-Timing"].startswith("busqueda;dur=")


@pytest.mark.rf("RF-21")
@pytest.mark.historia(140)
def test_una_pieza_corregida_se_busca_con_su_texto_nuevo_y_ya_no_con_el_viejo(
    cliente, validada, bd, indexacion, trabajador
):
    pieza = validada(LABORATORIO)
    nuevo = "--- Página 1 ---\n\nSe inauguró el observatorio astronómico de la facultad."
    entrar(cliente, MARIA)
    url = f"/api/piezas/{pieza}/ficha"
    corregida = cliente.put(
        url,
        json={**EDICION_COMPLETA, "cuerpo": nuevo, "version_base": cliente.get(url).json()["version"]},
    )
    assert corregida.status_code == 200, corregida.text
    with bd.transaccion() as tx:
        indexacion.encolar(tx, UUID(pieza))
    assert trabajador.procesar_siguiente() is True

    con_lo_nuevo = buscar(cliente, q="observatorio astronómico")
    con_lo_viejo = buscar(cliente, q="laboratorio de ensayos de materiales")

    [encontrada] = [r for r in con_lo_nuevo.json()["resultados"] if r["pieza_id"] == pieza]
    assert [c["texto"] for c in encontrada["citas"]] == [nuevo]
    # El texto viejo no queda en ninguna Cita: la persona no ve datos de la versión anterior.
    textos = [c["texto"] for r in con_lo_viejo.json()["resultados"] for c in r["citas"]]
    assert all("laboratorio" not in texto for texto in textos)


@pytest.mark.rf("RF-07")
def test_una_palabra_de_una_tabla_de_un_word_encuentra_la_pieza(cliente, validada):
    """142: el cuerpo es lo que `transcribir` real lee del Word (el doble no lee Words) y la
    Ficha se valida con él tal cual."""
    documento = Document()
    documento.add_heading("Acta de la sesión del Consejo", 1)
    tabla = documento.add_table(rows=2, cols=2)
    tabla.cell(0, 0).text, tabla.cell(0, 1).text = "Asistente", "Cargo"
    tabla.cell(1, 0).text, tabla.cell(1, 1).text = "Bartolomé Ezcurra", "Vicedecano"
    buffer = io.BytesIO()
    documento.save(buffer)
    cuerpo = (
        capa_para(CONFIGURACION_DE_PRUEBA, OllamaFalso().motores())
        .transcribir(
            archivo=BloqueBinario(
                etiqueta=ETIQUETA_DOCUMENTO, contenido=buffer.getvalue(), tipo_mime=DOCX
            )
        )
        .texto
    )
    acta = validada(cuerpo)
    validada(EGRESADOS)

    respuesta = buscar(cliente, q="Ezcurra")

    assert ids(respuesta)[0] == acta
    cita = respuesta.json()["resultados"][0]["citas"][0]
    assert "| Bartolomé Ezcurra | Vicedecano |" in cita["texto"]
    assert cita["ubicacion_legible"] == "p. 1"


@pytest.mark.rf("RF-07")
def test_el_texto_de_una_captura_pegada_en_un_word_se_busca_y_su_cita_lleva_a_la_pieza(
    cliente, validada, monkeypatch
):
    """143: el cuerpo es el de `transcribir` real con el OCR reemplazado (`image_to_data`): la
    captura entra como bloque «Imagen 1» y la Cita lleva a la Pieza, porque el Word no tiene
    páginas."""
    import pytesseract
    from PIL import Image

    monkeypatch.setattr(
        pytesseract,
        "image_to_data",
        lambda imagen, **_: {
            "text": ["Resolución", "Pellegrini"],
            "left": [10, 200],
            "top": [10, 10],
            "width": [150, 150],
            "height": [20, 20],
            "conf": [90.0, 90.0],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 1],
        },
    )
    captura = io.BytesIO()
    Image.new("RGB", (400, 120), "white").save(captura, "PNG")
    documento = Document()
    documento.add_paragraph("Acta de la sesión del Consejo.")
    documento.add_picture(captura)
    buffer = io.BytesIO()
    documento.save(buffer)
    cuerpo = (
        capa_para(CONFIGURACION_DE_PRUEBA, OllamaFalso().motores())
        .transcribir(
            archivo=BloqueBinario(
                etiqueta=ETIQUETA_DOCUMENTO, contenido=buffer.getvalue(), tipo_mime=DOCX
            )
        )
        .texto
    )
    acta = validada(cuerpo)
    validada(EGRESADOS)

    respuesta = buscar(cliente, q="Pellegrini")

    assert ids(respuesta)[0] == acta
    [cita] = [
        c for c in respuesta.json()["resultados"][0]["citas"] if "Pellegrini" in c["texto"]
    ]
    assert cita["texto"].startswith("--- Lectura automática · Imagen 1 ---")
    assert cita["ubicacion_legible"] == "Imagen 1"
    assert cita["enlace"] == f"/api/piezas/{acta}/original"


@pytest.mark.rf("RF-20")
@pytest.mark.historia(132, 137)
def test_el_filtro_de_fechas_excluye_lo_que_no_solapa_y_sin_resultados_no_se_amplia(
    cliente, validada
):
    validada(LABORATORIO)  # 3 de mayo de 1975
    noventa = validada(
        EGRESADOS, fecha_inicio="1990-01-01", precision_fecha="anio"
    )

    solapa = buscar(cliente, q="laboratorio de ensayos", desde="1989-06-01", hasta="1990-03-01")
    vacia = buscar(cliente, q="laboratorio de ensayos", desde="2000-01-01")

    assert ids(solapa) == [noventa]
    assert vacia.status_code == 200
    assert (vacia.json()["total"], vacia.json()["resultados"]) == (0, [])
    assert vacia.json()["filtros"] == {
        "desde": "2000-01-01", "hasta": None, "tipo": [], "tipo_actividad": [], "carrera": [],
    }


@pytest.fixture
def tres_validadas(validada) -> dict[str, str]:
    """La A tiene dos Carreras, la B una y la C ninguna; cada filtro las distingue."""
    return {
        "A": validada(LABORATORIO, tipo="acta", tipo_actividad="Transferencia",
                      carreras=["ing-informatica", "ing-mecanica"]),
        "B": validada(EGRESADOS, tipo="fotografia", tipo_actividad="Consultoría",
                      carreras=["ing-electronica"]),
        "C": validada("--- Página 1 ---\n\nResolución del rectorado.", tipo="resolucion",
                      tipo_actividad="Consultoría"),
    }


@pytest.mark.rf("RF-20", "RF-50")
@pytest.mark.parametrize(
    ("filtro", "esperadas"),
    [
        ({"tipo": ["acta", "fotografia"]}, "AB"),
        ({"tipo_actividad": ["Consultoría"]}, "BC"),
        ({"tipo_actividad": ["Consultoría", "Transferencia"]}, "ABC"),
        ({"carrera": ["ing-mecanica"]}, "A"),
        ({"carrera": ["ing-electronica", "ing-mecanica"]}, "AB"),
        ({"carrera": ["ing-civil"]}, ""),
        ({"tipo_actividad": ["Consultoría"], "carrera": ["ing-electronica"]}, "B"),
        ({"tipo": ["acta", "resolucion"], "tipo_actividad": ["Consultoría"]}, "C"),
    ],
    ids=[
        "dos-tipos", "tipo-de-actividad", "dos-tipos-de-actividad", "una-de-sus-dos-carreras",
        "cualquiera-de-dos-carreras", "carrera-sin-piezas", "actividad-y-carrera",
        "tipos-y-actividad",
    ],
)
def test_dos_valores_de_tipo_actividad_o_carrera_filtran_y_se_combinan(
    cliente, tres_validadas, filtro, esperadas
):
    con_texto = buscar(cliente, q="acta del laboratorio", **filtro)
    sin_texto = buscar(cliente, desde="1900-01-01", **filtro)

    esperados = sorted(tres_validadas[n] for n in esperadas)
    assert sorted(ids(con_texto)) == esperados
    assert sorted(ids(sin_texto)) == esperados


@pytest.fixture
def residuo(bd, capa_modelos):
    """Deja en el índice, para `pieza_id`, una copia de los fragmentos de `modelo` por cada
    `ficha_id`: lo que quedaría si una desindexación se perdiera."""
    from historia_viva.indice import FragmentoIndexado, Indice

    indice = Indice(bd=bd, dsn=bd.dsn)

    def escribir(modelo: str, pieza_id: str, *fichas: UUID) -> None:
        fragmentos = [
            FragmentoIndexado(
                **{
                    **asdict(f),
                    "fragmento_id": uuid4(),
                    "pieza_id": UUID(pieza_id),
                    "ficha_id": ficha_id,
                },
                vector=capa_modelos.embeber(
                    textos=(BloqueDatos(etiqueta=ETIQUETA_CUERPO_FICHA, contenido=f.texto),),
                    uso=UsoEmbedding.FRAGMENTO,
                ).vectores[0],
            )
            for f in indice.de_pieza(UUID(modelo))
            for ficha_id in fichas
        ]
        assert fragmentos
        with bd.transaccion() as tx:
            indice.reemplazar(tx, UUID(pieza_id), fragmentos)
        capa_modelos.embebidos.clear()

    return escribir


def ficha_vigente(cliente, pieza_id: str) -> UUID:
    entrar(cliente, MARIA)
    return UUID(cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["ficha_id"])


@pytest.mark.rf("RF-15", "RF-24")
@pytest.mark.historia(133, 144)
def test_pendientes_incompletas_retiradas_y_residuos_nunca_aparecen(
    cliente, validada, residuo, pieza_pendiente, url_bd
):
    buscable = validada(LABORATORIO)
    retirada = validada(LABORATORIO)
    corregida = validada(LABORATORIO)
    incompleta = validada(LABORATORIO, indexar=False, resumen=None)
    assert esperar_estado(cliente, incompleta, "Incompleta")
    residuo(buscable, pieza_pendiente, ficha_vigente(cliente, pieza_pendiente))
    residuo(buscable, incompleta, ficha_vigente(cliente, incompleta))
    residuo(buscable, corregida, uuid4())  # fragmentos de una Ficha que ya no es la vigente
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
            " borrada_motivo = 'Retirada en la prueba' WHERE pieza_id = %s",
            (MARIA, retirada),
        )

    con_texto = buscar(cliente, q="laboratorio de ensayos de materiales")
    sin_texto = buscar(cliente, desde="1900-01-01")

    assert ids(con_texto) == [buscable]
    assert ids(sin_texto) == [buscable]


ACTAS = [
    ("--- Página 1 ---\n\nActa del laboratorio de ensayos, primera sesión.\n\n"
    "--- Página 2 ---\n\nActa del laboratorio de ensayos, segunda sesión.\n\n"
    "--- Página 3 ---\n\nActa del laboratorio de ensayos, tercera sesión.\n\n"
    "--- Página 4 ---\n\nActa del laboratorio de ensayos, cuarta sesión."),
    ("--- Página 1 ---\n\nActa del laboratorio de hidráulica, primera sesión.\n\n"
    "--- Página 2 ---\n\nActa del laboratorio de hidráulica, segunda sesión."),
    "--- Página 1 ---\n\nActa del laboratorio de química, única sesión.",
]


@pytest.mark.rf("RF-21", "RF-22")
@pytest.mark.historia(136)
def test_recuperar_da_al_modelo_las_citas_del_resultado_en_su_orden_hasta_cinco(
    cliente, validada
):
    for cuerpo in ACTAS:
        validada(cuerpo)
    busqueda = cliente.app.state.busqueda

    recuperacion = busqueda.recuperar(Consulta(texto="acta del laboratorio de ensayos"))

    citas = [c.fragmento_id for r in recuperacion.resultado.resultados for c in r.citas]
    # La de ensayos primero, con 3 de sus 4 páginas; 6 citas visibles y 5 para el modelo.
    assert sorted(len(r.citas) for r in recuperacion.resultado.resultados) == [1, 2, 3]
    assert len(recuperacion.resultado.resultados[0].citas) == 3
    assert [f.fragmento_id for f in recuperacion.fragmentos] == citas[:5]
    assert busqueda.recuperar(Consulta(texto="acta"), max_fragmentos=2).fragmentos == tuple(
        busqueda.recuperar(Consulta(texto="acta")).fragmentos[:2]
    )


@pytest.mark.rf("RF-24")
@pytest.mark.historia(176)
def test_buscar_fragmentos_es_plana_y_solo_trae_piezas_buscables(
    cliente, validada, residuo, pieza_pendiente
):
    ensayos = validada(ACTAS[0])
    residuo(ensayos, pieza_pendiente, ficha_vigente(cliente, pieza_pendiente))

    hallados = cliente.app.state.busqueda.buscar_fragmentos(
        Consulta(texto="acta del laboratorio de ensayos"), limite=3
    )

    assert [(h.pieza_id, p.pieza_id) for h, p in hallados] == [(UUID(ensayos),) * 2] * 3


@pytest.mark.rf("RF-20")
@pytest.mark.historia(132)
def test_sin_texto_no_se_embebe_y_se_ordena_por_fecha(cliente, validada, capa_modelos):
    noventa = validada(EGRESADOS, fecha_inicio="1990-01-01", precision_fecha="anio")
    setenta = validada(LABORATORIO)
    ochenta = validada(ACTAS[2], fecha_inicio="1980-03-01", precision_fecha="mes")

    respuesta = buscar(cliente, desde="1900-01-01", hasta="1999-12-31")

    assert ids(respuesta) == [setenta, ochenta, noventa]
    assert capa_modelos.embebidos == []
    assert respuesta.headers["Server-Timing"].startswith("busqueda;dur=")


def _problema(respuesta) -> tuple[int, str, str | None]:
    cuerpo = respuesta.json()
    return respuesta.status_code, cuerpo["type"], cuerpo.get("campo")


@pytest.mark.rf("RF-20")
@pytest.mark.parametrize(
    ("parametros", "problema"),
    [
        ({}, (422, "/errores/pedido-invalido", None)),
        ({"tipo": "acta"}, (422, "/errores/pedido-invalido", None)),
        ({"desde": "1990-01-01", "hasta": "1980-01-01"}, (422, "/errores/pedido-invalido", "desde")),
        ({"q": "acta\x00 del laboratorio"}, (422, "/errores/pedido-invalido", "q")),
        ({"q": "acta", "tipo": "x\x00"}, (422, "/errores/pedido-invalido", "tipo")),
        ({"q": "acta", "carrera": "\x00"}, (422, "/errores/pedido-invalido", "carrera")),
        ({"q": "ab"}, (422, "/errores/pedido-invalido", "query.q")),
        ({"q": "acta", "limite": "51"}, (422, "/errores/pedido-invalido", "query.limite")),
        ({"q": "acta", "tipo": ["acta", "afiche"]}, (422, "/errores/tipo-desconocido", "tipo")),
        (
            {"q": "acta", "tipo_actividad": "Kermés"},
            (422, "/errores/tipo-actividad-desconocido", "tipo_actividad"),
        ),
        (
            {"q": "acta", "carrera": ["ing-civil", "medicina"]},
            (422, "/errores/carrera-desconocida", "carreras"),
        ),
    ],
    ids=[
        "sin-criterio", "solo-tipo", "desde-despues-de-hasta", "nul-en-q", "nul-en-tipo",
        "nul-en-carrera", "q-corta", "limite-alto", "tipo-desconocido",
        "tipo-de-actividad-desconocido", "carrera-desconocida",
    ],
)
def test_un_pedido_invalido_responde_422_con_su_codigo(cliente, capa_modelos, parametros, problema):
    respuesta = buscar(cliente, **parametros)

    assert _problema(respuesta) == problema
    assert capa_modelos.embebidos == []


@pytest.mark.rf("RF-19")
@pytest.mark.historia(134)
def test_con_embeber_caido_responde_503_y_no_una_lista_vacia(cliente, validada, capa_modelos):
    validada(LABORATORIO)
    capa_modelos.romper(MotorNoDisponible("el Ollama de embeddings no responde"),
                        en=Capacidad.EMBEBER)

    respuesta = buscar(cliente, q="laboratorio de ensayos")

    assert _problema(respuesta) == (503, "/errores/capa-modelos-no-disponible", None)
    assert respuesta.headers["Retry-After"] == "30"


@pytest.mark.historia(138)
@pytest.mark.rf("RF-21")
def test_con_el_indice_incompleto_responde_lo_indexado_y_avisa(cliente, validada, residuo):
    vigente = validada(LABORATORIO)
    vieja = validada(LABORATORIO, indexar=False)
    residuo(vigente, vieja, ficha_vigente(cliente, vieja))
    _cambiar_modelo_del_indice(cliente, vieja, "bge-m3@ffffffffffff")

    con_texto = buscar(cliente, q="laboratorio de ensayos")
    sin_texto = buscar(cliente, desde="1900-01-01")

    for respuesta in (con_texto, sin_texto):
        assert ids(respuesta) == [vigente]
        assert respuesta.json()["avisos"] == ["indice_incompleto"]
        assert respuesta.headers["Server-Timing"].startswith("busqueda;dur=")


def _cambiar_modelo_del_indice(cliente, pieza_id: str, modelo: str) -> None:
    """Los fragmentos de la Pieza pasan a ser de otro modelo, como antes de un `reindexar`."""
    from historia_viva.indice import FragmentoIndexado, Indice

    bd = cliente.app.state.bd
    indice = Indice(bd=bd, dsn=bd.dsn)
    fragmentos = indice.de_pieza(UUID(pieza_id))
    assert fragmentos
    with bd.transaccion() as tx:
        indice.reemplazar(
            tx,
            UUID(pieza_id),
            [
                FragmentoIndexado(**{**asdict(f), "modelo_embedding": modelo}, vector=(1.0,))
                for f in fragmentos
            ],
        )


@pytest.mark.rf("RF-30")
@pytest.mark.historia(141)
def test_con_el_procesamiento_frenado_la_busqueda_sigue_respondiendo(cliente, validada):
    laboratorio = validada(LABORATORIO)
    with cliente.app.state.bd.transaccion() as tx:
        frenar_todo(tx, por=Actor(TipoDeActor.CLI, "prueba"), motivo="Freno de la prueba de búsqueda")

    assert ids(buscar(cliente, q="laboratorio de ensayos")) == [laboratorio]
    assert ids(buscar(cliente, desde="1900-01-01")) == [laboratorio]


# ------------------------------------------------------------------- ronda 1 de revisión ---


LARGO = "\n\n".join(f"--- Página {n} ---\n\nFolio {n} del libro de actas." for n in range(1, 46))
"""45 páginas: 45 fragmentos, más que los candidatos que se piden al índice."""


@pytest.mark.rf("RF-20")
@pytest.mark.historia(132)
def test_sin_texto_una_pieza_larga_no_tapa_a_las_demas(cliente, validada):
    libro = validada(LARGO, fecha_inicio="1960-01-01", precision_fecha="anio")
    noventa = validada(EGRESADOS, fecha_inicio="1990-01-01", precision_fecha="anio")

    respuesta = buscar(cliente, desde="1900-01-01")

    assert ids(respuesta) == [libro, noventa]
    assert [len(r["citas"]) for r in respuesta.json()["resultados"]] == [3, 1]


@pytest.mark.rf("RF-20")
@pytest.mark.historia(132)
def test_sin_texto_no_hace_falta_el_motor(cliente, validada, capa_modelos):
    laboratorio = validada(LABORATORIO)
    capa_modelos.romper(MotorNoDisponible("el Ollama de embeddings no responde"),
                        en=Capacidad.EMBEBER)

    assert ids(buscar(cliente, desde="1900-01-01")) == [laboratorio]


@pytest.mark.rnf("RNF-10")
def test_si_embeber_agota_su_tope_responde_504(cliente, validada, capa_modelos):
    validada(LABORATORIO)
    capa_modelos.romper(LimiteExcedido("embeber", "tiempo", 30, 31), en=Capacidad.EMBEBER)

    respuesta = buscar(cliente, q="laboratorio de ensayos")

    assert _problema(respuesta) == (504, "/errores/tiempo-agotado", None)
    assert (respuesta.json()["capacidad"], respuesta.json()["tope_ms"]) == ("embeber", 30000)


@pytest.mark.historia(138)
def test_con_texto_y_sin_fragmentos_del_modelo_vigente_el_indice_no_esta_disponible(
    cliente, validada, residuo
):
    respuestas = [buscar(cliente, q="laboratorio de ensayos")]  # índice vacío
    vieja = validada(LABORATORIO)
    # Otro modelo, no otro digest de `bge-m3`: sin texto el digest vigente sale del índice.
    _cambiar_modelo_del_indice(cliente, vieja, "e5-large@ffffffffffff")
    respuestas.append(buscar(cliente, q="laboratorio de ensayos"))

    for respuesta in respuestas:
        assert _problema(respuesta) == (503, "/errores/indice-no-disponible", None)
        assert respuesta.headers["Retry-After"] == "30"
    sin_texto = buscar(cliente, desde="1900-01-01")
    assert (sin_texto.json()["total"], sin_texto.json()["avisos"]) == (0, ["indice_incompleto"])


@pytest.mark.rf("RF-15")
def test_los_fragmentos_de_otra_ficha_de_una_pieza_buscable_se_descartan(
    cliente, validada, residuo
):
    laboratorio = validada(LABORATORIO)
    vigente = ficha_vigente(cliente, laboratorio)
    residuo(laboratorio, laboratorio, vigente, uuid4())
    from historia_viva.indice import Indice

    bd = cliente.app.state.bd
    de_la_vigente = {
        str(f.fragmento_id)
        for f in Indice(bd=bd, dsn=bd.dsn).de_pieza(UUID(laboratorio))
        if f.ficha_id == vigente
    }

    for respuesta in (buscar(cliente, q="laboratorio de ensayos"),
                      buscar(cliente, desde="1900-01-01")):
        [resultado] = respuesta.json()["resultados"]
        assert {c["fragmento_id"] for c in resultado["citas"]} <= de_la_vigente


def test_un_texto_en_blanco_no_es_un_criterio(cliente):
    assert _problema(buscar(cliente, q="     ")) == (422, "/errores/pedido-invalido", None)


@pytest.fixture
def catalogos_restaurados(_conexion_del_dueno):
    """Las bajas se confirman (la app las lee desde su pool) y se deshacen al terminar."""
    conexion = _conexion_del_dueno
    yield conexion
    with conexion.transaction():
        conexion.execute("UPDATE acervo.tipo_pieza SET activo = true WHERE nombre = 'convenio'")
        conexion.execute(
            "UPDATE acervo.tipo_actividad SET activo = true WHERE nombre = 'Consultoría'"
        )
        conexion.execute("UPDATE acervo.carrera SET activo = true WHERE carrera_id = 'ing-civil'")


@pytest.mark.rf("RF-50", "RF-51")
def test_un_valor_dado_de_baja_sigue_filtrando_las_fichas_que_lo_tienen(
    cliente, validada, catalogos_restaurados
):
    convenio = validada(LABORATORIO, tipo="convenio", tipo_actividad="Consultoría",
                        carreras=["ing-civil"])
    with catalogos_restaurados.transaction():
        catalogos_restaurados.execute(
            "UPDATE acervo.tipo_pieza SET activo = false WHERE nombre = 'convenio'"
        )
        catalogos_restaurados.execute(
            "UPDATE acervo.tipo_actividad SET activo = false WHERE nombre = 'Consultoría'"
        )
        catalogos_restaurados.execute(
            "UPDATE acervo.carrera SET activo = false WHERE carrera_id = 'ing-civil'"
        )

    respuesta = buscar(cliente, desde="1900-01-01", tipo="convenio",
                       tipo_actividad="Consultoría", carrera="ing-civil")

    assert ids(respuesta) == [convenio]


@pytest.mark.rf("RF-20")
def test_un_valor_repetido_cuenta_una_vez(cliente, validada):
    laboratorio = validada(LABORATORIO, carreras=["ing-civil"])

    respuesta = buscar(cliente, desde="1900-01-01", tipo=["acta", "acta"],
                       carrera=["ing-civil", "ing-civil"])

    assert ids(respuesta) == [laboratorio]
    assert (respuesta.json()["filtros"]["tipo"], respuesta.json()["filtros"]["carrera"]) == (
        ["acta"], ["ing-civil"],
    )


@pytest.mark.rnf("RNF-10")
def test_si_embeber_supera_otro_tope_la_lectura_no_esta_disponible(cliente, validada, capa_modelos):
    validada(LABORATORIO)
    capa_modelos.romper(LimiteExcedido("embeber", "tokens_entrada", 8192, 9000),
                        en=Capacidad.EMBEBER)

    respuesta = buscar(cliente, q="laboratorio de ensayos")

    assert _problema(respuesta) == (503, "/errores/capa-modelos-no-disponible", None)


# ------------------------------------------- runbook R13: reconstruir un índice vaciado ---


@pytest.mark.rf("RF-33")
@pytest.mark.rnf("RNF-08")
def test_runbook_r13_vaciado_el_indice_reindexar_devuelve_las_3_consultas_de_referencia(
    validada, trabajador, capa_modelos, url_app, monkeypatch, tmp_path: Path, capsys
):
    """El ensayo del runbook R13 con los comandos de la imagen: el único doble es la capa."""
    validada(LABORATORIO)
    validada(EGRESADOS, fecha_inicio="1990-01-01", precision_fecha="anio")
    monkeypatch.setattr(composicion, "capa_para", lambda filas, **_: capa_modelos.para(filas))
    monkeypatch.setenv("HV_BD_URL", url_app)
    # Paso 1: las consultas antes de tocar el índice, a un archivo.
    assert main(["consultas-de-referencia"]) == 0
    antes = tmp_path / "consultas-antes.json"
    antes.write_text(capsys.readouterr().out, encoding="utf-8")
    esperadas = json.loads(antes.read_text(encoding="utf-8"))
    assert set(esperadas) == {"la-facultad", "actos-y-egresados", "desde-1900"}
    assert all(len(piezas) == 2 for piezas in esperadas.values())
    # La falla provocada: el índice vaciado (es derivado, DEC-143; `hv_app` puede borrarlo).
    with psycopg.connect(url_app, autocommit=True) as conexion:
        conexion.execute("DELETE FROM acervo.data_fragmento")
    assert main(["consultas-de-referencia", "--comparar", str(antes)]) == 1
    assert "El índice no tiene fragmentos" in capsys.readouterr().err

    assert main(["reindexar"]) == 0
    while trabajador.procesar_siguiente():  # el trabajador `--tipos indexacion`
        pass

    capsys.readouterr()
    assert main(["consultas-de-referencia", "--comparar", str(antes)]) == 0
    assert "Las 3 consultas de referencia dan lo esperado." in capsys.readouterr().out


@pytest.mark.historia(162)
def test_runbook_r7_frenar_todo_deja_la_indexacion_encolada_y_la_busqueda_responde(
    cliente, validada, indexacion, trabajador, bd, url_app, monkeypatch, capsys
):
    """El ensayo del runbook R7 con el comando: lo encolado espera y se completa al reanudar."""
    laboratorio = validada(LABORATORIO)
    egresados = validada(EGRESADOS, indexar=False)
    with bd.transaccion() as tx:
        indexacion.encolar(tx, UUID(egresados))
    monkeypatch.setenv("HV_BD_URL", url_app)

    assert main(["interruptor", "frenar-todo", "--motivo", "Runbook R7: ensayo"]) == 0
    assert trabajador.procesar_siguiente() is False  # el trabajador `--tipos indexacion` no toma
    assert main(["reindexar"]) == 1
    assert ids(buscar(cliente, q="laboratorio de ensayos")) == [laboratorio]

    assert main(["interruptor", "reanudar", "procesamiento_habilitado"]) == 0
    assert trabajador.procesar_siguiente() is True
    assert egresados in ids(buscar(cliente, q="fiesta de egresados"))
    assert "api_agentes_habilitada: detenido" in capsys.readouterr().out


@pytest.mark.rf("RF-33")
def test_consultas_de_referencia_dice_cual_no_da_lo_esperado(
    validada, capa_modelos, url_app, monkeypatch, tmp_path: Path, capsys
):
    pieza_id = validada(LABORATORIO)
    monkeypatch.setattr(composicion, "capa_para", lambda filas, **_: capa_modelos.para(filas))
    monkeypatch.setenv("HV_BD_URL", url_app)
    esperadas = tmp_path / "20261019T120000Z-manual.referencia.json"  # la de un volcado
    consultas = {"la-facultad": [], "actos-y-egresados": [pieza_id], "desde-1900": [pieza_id]}
    esperadas.write_text(
        json.dumps({"conteos": {}, "consultas": consultas, "estable": True}), encoding="utf-8"
    )

    assert main(["consultas-de-referencia", "--comparar", str(esperadas)]) == 1
    assert capsys.readouterr().out == f"la-facultad: esperadas [], obtenidas ['{pieza_id}']\n"


@pytest.mark.rnf("RNF-22")
def test_seg_la_busqueda_con_texto_tiene_cupo_por_persona(cliente, validada):
    """SEG-09 (165): cada consulta con texto embebe; pasado el cupo, 429 con `Retry-After`.
    Sin texto no toca el motor y no cuenta; un pedido inválido tampoco."""
    validada(LABORATORIO)
    cliente.app.state.cupos_de_busqueda = Cupos(TopesDeCupo(consultas_min=2))

    invalidas = [buscar(cliente, q="laboratorio", tipo="no-existe"),
                 buscar(cliente, q="laboratorio", desde="2000-01-01", hasta="1990-01-01")]
    admitidas = [buscar(cliente, q="laboratorio de ensayos") for _ in range(2)]
    rechazada = buscar(cliente, q="laboratorio de ensayos")
    sin_texto = buscar(cliente, desde="1900-01-01")

    assert all(400 <= r.status_code < 429 for r in invalidas)  # no gastan cupo
    assert [r.status_code for r in admitidas] == [200, 200]
    assert _problema(rechazada) == (429, "/errores/limite-de-consultas", None)
    assert 1 <= int(rechazada.headers["Retry-After"]) <= 60
    assert sin_texto.status_code == 200


@pytest.mark.rnf("RNF-22")
def test_el_cupo_de_busqueda_se_cambia_por_entorno():
    assert composicion.Entorno.desde_variables({}).busquedas_min == 30
    assert composicion.Entorno.desde_variables({"HV_CUPO_BUSQUEDAS_MIN": "7"}).busquedas_min == 7
    with pytest.raises(composicion.EntornoInvalido, match="HV_CUPO_BUSQUEDAS_MIN"):
        composicion.Entorno.desde_variables({"HV_CUPO_BUSQUEDAS_MIN": "0"})
