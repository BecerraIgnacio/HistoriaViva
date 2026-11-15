"""Subir sin crear el Lote antes (151): arrastrar varios archivos arma un Lote con nombre provisorio y
sin Contexto; Origen, Fecha y Propietario se completan después, con las Piezas ya procesándose.

Con Piezas, el Lote completa lo vacío y nunca cambia lo que ya tenía (historia 74 ajustada). Lo
completado se copia a las Recibidas (sobre la fila, con la operación de 152) y a las Pendientes (con
una versión `sistema`), y no a las Incompletas ni a las Validadas. Por HTTP contra Postgres real.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

from aplicacion import entrar
from capa_modelos_falsa import RESUMEN_FIJO
from espera import esperar_error_procesamiento, esperar_estado
from historia_viva.auditoria import Accion, ObjetoRef
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.dominio.errores import LimiteExcedido
from historia_viva.dominio.tipos import CamposDuros
from historia_viva.fichas.postgres import _heredar_en_pendiente
from test_fichas import divergencias
from test_lotes import LOTE, _llenar, _subir
from test_retiradas import restaurar
from test_retiro import retirar
from validacion import EDICION_COMPLETA, MARIA, PDF, abrir

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")

COMPLETADO = {
    "origen": "Donación Pérez",
    "fecha_inicio": "1972-03-01",
    "precision_fecha": "mes",
}
FECHA = {"fecha_inicio": "1972-03-01", "fecha_fin": "1972-03-31", "precision_fecha": "mes"}


def _implicito(cliente, nombre="Carga del 1/11/26 10:00 (4 archivos)") -> dict:
    respuesta = cliente.post("/api/lotes/implicito", json={"nombre": nombre})
    assert respuesta.status_code == 201, respuesta.text
    lote: dict = respuesta.json()["lote"]
    return lote


def _pdf(n: int) -> bytes:
    return PDF + f"151-{n}".encode()


def _guardar(cliente, pieza_id, **cambios):
    apertura = abrir(cliente, pieza_id)
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    edicion = EDICION_COMPLETA | {
        "resumen": RESUMEN_FIJO,
        "contexto": ficha["contexto"],
        "medicion_id": apertura["medicion_id"],
    }
    respuesta = cliente.put(f"/api/piezas/{pieza_id}/ficha", json=edicion | cambios)
    assert respuesta.status_code == 200, respuesta.text


def _eventos(cliente, accion, lote_id):
    return cliente.app.state.auditoria.listar(accion=accion, objeto=ObjetoRef("lote", lote_id))


@pytest.mark.rf("RF-02")
@pytest.mark.historia(74)
def test_varios_archivos_arman_un_lote_sin_contexto_que_se_audita_y_lo_suben_todo(cliente):
    entrar(cliente, MARIA)

    lote = _implicito(cliente)
    subidas = [_subir(cliente, lote["lote_id"], f"acta-{n}.pdf", _pdf(n)) for n in (1, 2)]

    assert {campo: lote[campo] for campo in ("origen", "fecha_inicio", "propietario")} == {
        "origen": None, "fecha_inicio": None, "propietario": None,
    }
    assert [s.status_code for s in subidas] == [201, 201]
    # Lo que el Lote no tiene la Pieza no lo hereda: lo sigue pidiendo.
    assert [s.json()["campos_heredados"] for s in subidas] == [[], []]
    vista = cliente.get(f"/api/lotes/{lote['lote_id']}").json()
    assert (vista["progreso"]["total"], vista["lote"]["por_completar"]) == (
        2,
        ["fecha_inicio", "fecha_fin", "precision_fecha", "origen", "propietario"],
    )
    [evento] = _eventos(cliente, Accion.LOTE_CREACION_IMPLICITA, lote["lote_id"])
    assert evento.despues == {"nombre": lote["nombre"]}
    for subida in subidas:
        esperar_estado(cliente, subida.json()["pieza_id"], "Pendiente")


@pytest.mark.rf("RF-02")
@pytest.mark.parametrize(
    ("pedido", "campo"),
    [({}, "nombre"), ({"nombre": "  "}, "nombre"), ({"nombre": "x", "origen": "y"}, "origen")],
    ids=["sin-nombre", "nombre-en-blanco", "campo-extra"],
)
def test_el_lote_implicito_pide_solo_un_nombre(cliente, pedido, campo):
    entrar(cliente, MARIA)

    respuesta = cliente.post("/api/lotes/implicito", json=pedido)

    assert (respuesta.status_code, respuesta.json()["campo"]) == (422, campo)


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
@pytest.mark.historia(78)
def test_completar_el_lote_copia_a_recibidas_y_pendientes_y_no_a_incompletas_ni_validadas(
    cliente, capa_modelos, url_bd
):
    entrar(cliente, MARIA)
    lote = _implicito(cliente)
    lote_id = lote["lote_id"]
    pendiente, incompleta, validada = (
        _subir(cliente, lote_id, f"acta-{n}.pdf", _pdf(10 + n)).json()["pieza_id"]
        for n in range(3)
    )
    for pieza_id in (pendiente, incompleta, validada):
        esperar_estado(cliente, pieza_id, "Pendiente")
    _guardar(cliente, incompleta, origen=None, propietario=None)
    _guardar(cliente, validada)
    abrir(cliente, pendiente)
    capa_modelos.demorar()
    recibida = _subir(cliente, lote_id, "acta-r.pdf", _pdf(20)).json()["pieza_id"]

    respuesta = cliente.patch(
        f"/api/lotes/{lote_id}",
        json=COMPLETADO | {"propietario": "Biblioteca FIE", "nombre": "Donación Pérez 1972"},
    )

    assert respuesta.status_code == 200, respuesta.text
    guardado = respuesta.json()["lote"]
    assert {c: guardado[c] for c in (*FECHA, "origen", "nombre")} == {
        **FECHA, "origen": "Donación Pérez", "nombre": "Donación Pérez 1972"
    }
    completos = {**FECHA, "origen": "Donación Pérez", "propietario": "Biblioteca FIE"}
    heredados = ["fecha_inicio", "fecha_fin", "precision_fecha", "origen", "propietario"]
    # La Pendiente: una versión `sistema` con lo que le faltaba, sin firma y sin volverse
    # «Requiere análisis humano»; quien la tenía abierta en la v1 rebota con 409 (RF-16).
    ficha = cliente.get(f"/api/piezas/{pendiente}/ficha").json()
    assert (ficha["version"], ficha["origen_version"], ficha["requiere_analisis_humano"]) == (
        2, "sistema", False,
    )
    assert {c: ficha["frontmatter"][c] for c in completos} == completos
    assert ficha["frontmatter"]["estado"] == "Pendiente"
    assert ficha["campos_heredados"] == heredados
    assert ficha["frontmatter"]["resumen"] == RESUMEN_FIJO
    vieja = cliente.put(
        f"/api/piezas/{pendiente}/ficha",
        json=EDICION_COMPLETA | {"resumen": RESUMEN_FIJO, "contexto": ficha["contexto"]},
    )
    assert (vieja.status_code, vieja.json()["type"]) == (409, "/errores/version-desactualizada")
    # La Incompleta y la Validada no se tocan: siguen en la versión que guardó la persona.
    sin_tocar = cliente.get(f"/api/piezas/{incompleta}/ficha").json()
    assert (sin_tocar["version"], sin_tocar["frontmatter"]["origen"]) == (2, None)
    assert sin_tocar["frontmatter"]["propietario"] is None
    assert cliente.get(f"/api/piezas/{validada}/ficha").json()["version"] == 2
    # La Recibida lo tiene en la fila y su v1 lo toma; su resumen se pidió sin el Contexto,
    # que el trabajo congeló vacío al encolarla (historia 78).
    detalle = cliente.get(f"/api/piezas/{recibida}/en-lectura").json()
    assert (detalle["en_lectura"], detalle["campos_heredados"]) == (True, heredados)
    capa_modelos.liberar()
    esperar_estado(cliente, recibida, "Pendiente")
    v1 = cliente.get(f"/api/piezas/{recibida}/ficha").json()
    assert (v1["version"], v1["origen_version"]) == (1, "modelo")
    assert {c: v1["frontmatter"][c] for c in completos} == completos
    assert {c: v1["procedencia"][c] for c in ("origen", "fecha_inicio", "propietario")} == {
        "origen": "lote", "fecha_inicio": "lote", "propietario": "lote",
    }
    resumir = [i for i in capa_modelos.invocaciones if i.capacidad is Capacidad.RESUMIR][-1]
    assert [b.etiqueta for b in resumir.bloques] == ["cuerpo_ficha"]
    # La auditoría registra la herencia tardía: qué campos y a qué Piezas, sin contenido.
    [evento] = _eventos(cliente, Accion.LOTE_HERENCIA_TARDIA, lote_id)
    assert evento.despues == {
        "campos": heredados,
        "piezas": {pendiente: heredados, recibida: heredados},
    }
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
@pytest.mark.parametrize(
    "cambio",
    [
        {"origen": "Otro archivo"},
        {"fecha_inicio": "1980-01-01", "precision_fecha": "decada", "fecha_fin": "1989-12-31"},
        {"propietario": "Otra"},
        {"evento": "Colación"},
    ],
    ids=["origen", "fecha", "propietario", "evento"],
)
def test_con_piezas_lo_que_el_lote_ya_tenia_no_cambia(cliente, cambio):
    entrar(cliente, MARIA)
    respuesta = cliente.post("/api/lotes", json=LOTE | {"propietario": "SEU"})
    lote_id = respuesta.json()["lote"]["lote_id"]
    pieza_id = _subir(cliente, lote_id, "acta.pdf", _pdf(30)).json()["pieza_id"]

    rechazo = cliente.patch(f"/api/lotes/{lote_id}", json=cambio)

    assert (rechazo.status_code, rechazo.json()["type"]) == (409, "/errores/contexto-congelado")
    leido = cliente.get(f"/api/lotes/{lote_id}").json()["lote"]
    assert (leido["origen"], leido["fecha_inicio"], leido["propietario"]) == (
        LOTE["origen"], LOTE["fecha_inicio"], "SEU",
    )
    assert leido["por_completar"] == []
    esperar_estado(cliente, pieza_id, "Pendiente")


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
def test_con_piezas_se_completa_solo_lo_vacio_y_el_nombre_provisorio_se_cambia(cliente):
    entrar(cliente, MARIA)
    lote_id = _implicito(cliente)["lote_id"]
    pieza_id = _subir(cliente, lote_id, "acta.pdf", _pdf(40)).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")

    origen = cliente.patch(f"/api/lotes/{lote_id}", json={"origen": "Donación Pérez"})
    otra_vez = cliente.patch(f"/api/lotes/{lote_id}", json={"origen": "Otro"})
    nombre = cliente.patch(f"/api/lotes/{lote_id}", json={"nombre": "Donación Pérez"})
    media_fecha = cliente.patch(f"/api/lotes/{lote_id}", json={"fecha_inicio": "1972-03-01"})

    assert origen.status_code == 200, origen.text
    assert otra_vez.status_code == 409
    assert (nombre.status_code, nombre.json()["lote"]["nombre"]) == (200, "Donación Pérez")
    assert (media_fecha.status_code, media_fecha.json()["campo"]) == (422, "precision_fecha")
    vista = cliente.get(f"/api/lotes/{lote_id}").json()["lote"]
    assert vista["por_completar"] == ["fecha_inicio", "fecha_fin", "precision_fecha", "propietario"]
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    # Solo el origen cambió en la Pieza: la versión `sistema` es una sola.
    assert (ficha["version"], ficha["frontmatter"]["origen"], ficha["campos_heredados"]) == (
        2, "Donación Pérez", ["origen"],
    )


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
def test_completar_no_pisa_lo_que_una_persona_escribio_en_la_recibida(cliente, capa_modelos):
    entrar(cliente, MARIA)
    lote_id = _implicito(cliente)["lote_id"]
    capa_modelos.demorar()
    pieza_id = _subir(cliente, lote_id, "acta.pdf", _pdf(50)).json()["pieza_id"]
    escrita = cliente.put(
        f"/api/piezas/{pieza_id}/en-lectura", json={"origen": "Archivo de Rectorado"}
    )
    assert escrita.status_code == 200, escrita.text

    cliente.patch(f"/api/lotes/{lote_id}", json=COMPLETADO)

    detalle = cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()
    assert detalle["campos_heredados"] == ["fecha_inicio", "fecha_fin", "precision_fecha"]
    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["frontmatter"]["origen"], ficha["procedencia"]["origen"]) == (
        "Archivo de Rectorado", "persona",
    )
    assert ficha["procedencia"]["fecha_inicio"] == "lote"



@pytest.mark.rf("RF-02")
@pytest.mark.historia(75)
def test_el_lote_implicito_sigue_el_tope_de_100_y_el_rechazo_de_duplicados(cliente, url_bd):
    entrar(cliente, MARIA)
    lleno = _implicito(cliente)["lote_id"]
    _llenar(url_bd, lleno, 100)
    otro = _implicito(cliente)["lote_id"]

    sobre_el_tope = _subir(cliente, lleno, "acta.pdf", _pdf(60))
    primera = _subir(cliente, otro, "acta.pdf", _pdf(61))
    repetida = _subir(cliente, otro, "acta.pdf", _pdf(61))

    assert (sobre_el_tope.status_code, sobre_el_tope.json()["tope"]) == (422, 100)
    assert (repetida.status_code, repetida.json()["duplicada"]) == (200, True)
    assert repetida.json()["pieza_id"] == primera.json()["pieza_id"]
    esperar_estado(cliente, primera.json()["pieza_id"], "Pendiente")


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
def test_la_pantalla_abierta_antes_de_la_herencia_no_borra_lo_que_copio_el_lote(
    cliente, capa_modelos
):
    entrar(cliente, MARIA)
    lote_id = _implicito(cliente)["lote_id"]
    capa_modelos.demorar()
    pieza_id = _subir(cliente, lote_id, "acta.pdf", _pdf(70)).json()["pieza_id"]
    vieja = cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()["frontmatter"]
    assert vieja["origen"] is None
    cliente.patch(f"/api/lotes/{lote_id}", json=COMPLETADO | {"propietario": "Biblioteca FIE"})

    # La pantalla manda el formulario entero, con los vacíos que leyó antes de la herencia.
    guardada = cliente.put(
        f"/api/piezas/{pieza_id}/en-lectura",
        json={"tipo": "acta", "origen": None, "propietario": None, "fecha_inicio": None},
    )

    assert guardada.status_code == 200, guardada.text
    leida = cliente.get(f"/api/piezas/{pieza_id}/en-lectura").json()
    completos = {**FECHA, "origen": "Donación Pérez", "propietario": "Biblioteca FIE"}
    assert {c: leida["frontmatter"][c] for c in completos} == completos
    assert leida["frontmatter"]["tipo"] == "acta"
    assert leida["campos_de_la_persona"] == ["tipo"]
    [evento] = cliente.app.state.auditoria.listar(
        accion=Accion.PIEZA_COMPLETAR_EN_RECIBIDA, objeto=ObjetoRef("pieza", pieza_id)
    )
    assert evento.despues == {"campos": ["tipo"], "contexto": False}


@pytest.mark.rf("RF-54")
@pytest.mark.historia(74)
def test_la_version_sistema_de_la_herencia_no_cambia_si_requiere_analisis_humano(
    cliente, capa_modelos
):
    entrar(cliente, MARIA)
    lote_id = _implicito(cliente)["lote_id"]
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    pieza_id = _subir(cliente, lote_id, "acta.pdf", _pdf(80)).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)
    capa_modelos.arreglar()
    assert cliente.post(f"/api/piezas/{pieza_id}/completar-a-mano").status_code == 200

    cliente.patch(f"/api/lotes/{lote_id}", json={"origen": "Donación Pérez"})

    # v1 y v2 `sistema`: nadie la leyó todavía, sigue pidiendo análisis humano.
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["requiere_analisis_humano"]) == (2, True)
    assert cliente.get(f"/api/lotes/{lote_id}").json()["progreso"]["para_analisis_humano"] == 1
    _guardar(cliente, pieza_id, version_base=2, propietario=None)
    # Una `sistema` encima de lo que guardó una persona no la devuelve a análisis humano.
    estado = cliente.app.state
    with estado.bd.transaccion() as tx:
        _heredar_en_pendiente(tx, UUID(pieza_id), CamposDuros(propietario="Biblioteca FIE"))
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (4, "sistema")
    assert ficha["requiere_analisis_humano"] is False
    assert cliente.get(f"/api/lotes/{lote_id}").json()["progreso"]["para_analisis_humano"] == 0


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
def test_la_retirada_mientras_se_completa_el_lote_lo_recibe_al_restaurarse(cliente):
    entrar(cliente, MARIA)
    lote_id = _implicito(cliente)["lote_id"]
    pieza_id = _subir(cliente, lote_id, "acta.pdf", _pdf(90)).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    assert retirar(cliente, pieza_id).status_code == 200
    cliente.patch(f"/api/lotes/{lote_id}", json=COMPLETADO)

    assert restaurar(cliente, pieza_id).status_code == 200

    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["version"], ficha["origen_version"]) == (2, "sistema")
    assert {c: ficha["frontmatter"][c] for c in (*FECHA, "origen")} == {
        **FECHA, "origen": "Donación Pérez"
    }
    [evento] = cliente.app.state.auditoria.listar(
        accion=Accion.PIEZA_RESTAURACION, objeto=ObjetoRef("pieza", pieza_id)
    )
    assert evento.despues["heredados"] == ["fecha_inicio", "fecha_fin", "precision_fecha", "origen"]


@pytest.mark.rf("RF-03")
@pytest.mark.historia(74)
def test_las_mismas_carreras_en_otro_orden_no_son_un_cambio(cliente):
    entrar(cliente, MARIA)
    lote = cliente.post(
        "/api/lotes", json=LOTE | {"carreras": ["ing-civil", "ing-mecanica"]}
    ).json()["lote"]
    pieza_id = _subir(cliente, lote["lote_id"], "acta.pdf", _pdf(95)).json()["pieza_id"]

    respuesta = cliente.patch(
        f"/api/lotes/{lote['lote_id']}",
        json={"carreras": ["ing-mecanica", "ing-civil"], "propietario": "SEU"},
    )

    assert respuesta.status_code == 200, respuesta.text
    esperar_estado(cliente, pieza_id, "Pendiente")
