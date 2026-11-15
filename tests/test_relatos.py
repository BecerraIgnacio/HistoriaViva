"""«Proponer como relato» desde la Conversación y la lectura de los Relatos (064; spec, módulos 31
y 32, §1.21b; DEC-147, R-05, R-24, R-31, R-42).

HTTP contra Postgres real y el índice de verdad, como `test_conversacion`: el turno deja una
respuesta guardada y el Relato nace de ella sin llamar a ningún modelo.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from aplicacion import entrar
from historia_viva.auditoria import Accion, ObjetoRef, RegistroDeAuditoria
from historia_viva.dominio.permisos import Rol
from historia_viva.dominio.tipos import FiltroDeBusqueda, MetodoAutenticacion
from historia_viva.efemerides import FuenteDeRelato, RelatoEntregable
from historia_viva.identidad import Principal
from test_avisos_de_efemerides import INCOMPLETA
from test_avisos_de_efemerides import _editar as editar_ficha
from test_busqueda import (  # noqa: F401 — fixtures de Piezas Validadas e indexadas
    EGRESADOS,
    LABORATORIO,
    bd,
    indexacion,
    trabajador,
    validada,
)
from test_conversacion import nueva, turno
from test_efemerides import sembrar  # noqa: F401 (Piezas Validadas sin indexar)
from test_fichas import COMPLETA
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA

pytestmark = [
    pytest.mark.rf("RF-55"),
    pytest.mark.xdist_group("indice"),
    pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools"),
]

PREGUNTA = "laboratorio de ensayos"


def responder(cliente, persona: str = JORGE, pregunta: str = PREGUNTA) -> tuple[str, str]:
    """Una Conversación de `persona` con un turno respondido: `(conversacion_id, mensaje_id)`."""
    conversacion = nueva(cliente, persona)
    respuesta, eventos = turno(cliente, conversacion, pregunta)
    assert respuesta.status_code == 200, respuesta.text
    return conversacion, str(dict(eventos)["fuentes"]["mensaje_id"])


def proponer(cliente, conversacion: str, mensaje: str) -> Any:
    return cliente.post(f"/api/conversaciones/{conversacion}/mensajes/{mensaje}/relato")


# --- Migración (§1.21b) -----------------------------------------------------------------------


def _relato(
    tx, *, estado: str = "propuesta", aprobado_el: datetime | None = None, retirada: bool = False
) -> str:
    """Un Relato sin fuentes sobre una respuesta sembrada a mano, en `tx` (o confirmado, con la
    conexión del dueño)."""
    conversacion = tx.execute(
        "INSERT INTO acervo.conversacion (persona_id) VALUES (%s) RETURNING conversacion_id",
        (JORGE,),
    ).fetchone()[0]
    mensaje = tx.execute(
        "INSERT INTO acervo.mensaje_conversacion (conversacion_id, orden, autor, contenido,"
        " estado) VALUES (%s, 2, 'historia_viva', 'Se creó [1].', 'completa')"
        " RETURNING mensaje_id",
        (conversacion,),
    ).fetchone()[0]
    ahora = datetime.now(UTC)
    aprobado, descartado = estado == "aprobada", estado == "descartada"
    fila = tx.execute(
        "INSERT INTO acervo.relato (mensaje_id, titulo, cuerpo, estado, configuracion_efectiva,"
        " propuesto_por, aprobado_por, aprobado_el, metodo_autenticacion, descartado_por,"
        " descartado_el, descarte_motivo, fuente_retirada_el) VALUES (%s, 'El laboratorio',"
        " 'Se creó [1].', %s, '{}', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING relato_id::text",
        (
            mensaje, estado, JORGE,
            MARIA if aprobado else None, (aprobado_el or ahora) if aprobado else None,
            "dev" if aprobado else None,
            MARIA if descartado else None, ahora if descartado else None,
            "No corresponde" if descartado else None, ahora if retirada else None,
        ),  # fmt: skip
    ).fetchone()
    return str(fila[0])


@pytest.mark.historia(298)
@pytest.mark.parametrize("estado", ["aprobada", "descartada"])
def test_un_relato_cerrado_solo_admite_la_marca_de_fuente_retirada_una_vez(tx, estado):
    relato = _relato(tx, estado=estado)

    with pytest.raises(psycopg.errors.RestrictViolation, match="cerrado"), tx.transaction():
        tx.execute("UPDATE acervo.relato SET cuerpo = 'Otro' WHERE relato_id = %s", (relato,))
    tx.execute("UPDATE acervo.relato SET fuente_retirada_el = now() WHERE relato_id = %s", (relato,))
    with pytest.raises(psycopg.errors.RestrictViolation, match="cerrado"), tx.transaction():
        tx.execute(
            "UPDATE acervo.relato SET fuente_retirada_el = now() + interval '1 day'"
            " WHERE relato_id = %s",
            (relato,),
        )


@pytest.mark.historia(298)
def test_una_respuesta_no_tiene_dos_relatos_vigentes_pero_si_uno_tras_un_descarte(tx):
    descartado = _relato(tx, estado="descartada")
    mensaje = tx.execute(
        "SELECT mensaje_id FROM acervo.relato WHERE relato_id = %s", (descartado,)
    ).fetchone()[0]
    nuevo = (
        "INSERT INTO acervo.relato (mensaje_id, titulo, cuerpo, configuracion_efectiva,"
        " propuesto_por) VALUES (%s, 'T', 'C', '{}', %s)"
    )

    tx.execute(nuevo, (mensaje, JORGE))
    with pytest.raises(psycopg.errors.UniqueViolation), tx.transaction():
        tx.execute(nuevo, (mensaje, JORGE))


# --- Proponer (POST …/relato) -----------------------------------------------------------------


@pytest.mark.historia(297, 298)
def test_proponer_crea_el_relato_propuesto_con_sus_citas_sin_llamar_a_ningun_modelo(
    cliente, validada, capa_modelos, tx
):
    laboratorio = validada(LABORATORIO)
    # `[9]` no es de ningún fragmento: queda en la respuesta guardada, no en el Relato.
    capa_modelos.respuesta = "Se resolvió crear el laboratorio [1]. Tuvo diez ensayos [9]."
    conversacion, mensaje = responder(cliente)
    llamadas = len(capa_modelos.invocaciones)

    respuesta = proponer(cliente, conversacion, mensaje)

    assert respuesta.status_code == 201, respuesta.text
    relato = respuesta.json()
    assert respuesta.headers["Location"] == f"/api/relatos/{relato['relato_id']}"
    assert len(capa_modelos.invocaciones) == llamadas  # el doble de capa no se llamó
    assert {k: relato[k] for k in ("estado", "titulo", "texto", "version", "mensaje_id")} == {
        "estado": "Propuesta",
        "titulo": PREGUNTA,  # la pregunta del turno (auditoría de tickets, decisión 18)
        "texto": "Se resolvió crear el laboratorio [1]. Tuvo diez ensayos.",
        "version": 1,
        "mensaje_id": mensaje,
    }
    assert (relato["aviso_inyeccion"], relato["fuente_retirada"]) == (False, False)
    assert relato["propuesto_por"] == "jperalta@fie.undef.edu.ar"
    assert [(f["pieza_id"], f["alerta_inyeccion"]) for f in relato["fuentes"]] == [
        (laboratorio, False)
    ]
    # Cada fuente conserva el número de su `[n]` y la versión de la Ficha citada.
    fuentes = tx.execute(
        "SELECT f.posicion, f.pieza_id::text, f.ficha_id = c.ficha_id FROM acervo.relato_fuente f"
        " JOIN acervo.mensaje_cita c ON c.mensaje_id = %s AND c.posicion = f.posicion"
        " WHERE f.relato_id = %s",
        (mensaje, relato["relato_id"]),
    ).fetchall()
    assert fuentes == [(1, laboratorio, True)]
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(accion=Accion.RELATO_PROPUESTA)
    assert (evento.actor.usuario_id, evento.objeto) == (
        UUID(JORGE), ObjetoRef("relato", relato["relato_id"])
    )
    assert evento.despues == {"mensaje_id": mensaje, "fuentes": [laboratorio]}


@pytest.mark.historia(298)
def test_una_fuente_con_alerta_de_inyeccion_deja_el_aviso(cliente, validada, dueno):
    laboratorio = validada(LABORATORIO)
    conversacion, mensaje = responder(cliente)
    dueno.execute(
        "UPDATE acervo.pieza SET alerta_inyeccion = true WHERE pieza_id = %s", (laboratorio,)
    )

    relato = proponer(cliente, conversacion, mensaje).json()

    assert relato["aviso_inyeccion"] is True
    assert [f["alerta_inyeccion"] for f in relato["fuentes"]] == [True]


@pytest.fixture
def dueno(_conexion_del_dueno) -> psycopg.Connection[Any]:
    return _conexion_del_dueno


def _jorge() -> Principal:
    return Principal(
        usuario_id=UUID(JORGE), nombre="Jorge", email="jperalta@fie.undef.edu.ar",
        rol=Rol.COORDINADOR, metodo=MetodoAutenticacion.DEV, vence_el=datetime.now(UTC),
    )  # fmt: skip


@pytest.mark.historia(297)
@pytest.mark.parametrize("caso", ["sin_evidencia", "interrumpida", "pregunta"])
def test_una_respuesta_no_completa_o_sin_citas_verificadas_no_se_propone(
    cliente, validada, capa_modelos, caso
):
    validada(LABORATORIO)
    if caso == "interrumpida":
        conversacion = nueva(cliente, JORGE)
        eventos = cliente.app.state.conversaciones.enviar_mensaje(
            UUID(conversacion), principal=_jorge(), texto=PREGUNTA, filtro=FiltroDeBusqueda()
        )
        mensaje = str(next(eventos).datos["mensaje_id"])
        eventos.close()
        esperado = "interrumpida"
    else:
        if caso == "sin_evidencia":
            capa_modelos.respuesta = "Se resolvió crear el laboratorio."
        conversacion, mensaje = responder(cliente)
        esperado = "sin_evidencia"
        if caso == "pregunta":
            mensaje = cliente.get(f"/api/conversaciones/{conversacion}").json()["mensajes"][0][
                "mensaje_id"
            ]
            esperado = None

    respuesta = proponer(cliente, conversacion, mensaje)

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["type"] == "/errores/respuesta-no-proponible"
    assert respuesta.json()["estado_actual"] == esperado


@pytest.mark.historia(297)
def test_la_misma_respuesta_dos_veces_es_relato_ya_propuesto(cliente, validada):
    validada(LABORATORIO)
    conversacion, mensaje = responder(cliente)
    primero = proponer(cliente, conversacion, mensaje).json()

    respuesta = proponer(cliente, conversacion, mensaje)

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["type"] == "/errores/relato-ya-propuesto"
    assert respuesta.json()["relato_id"] == primero["relato_id"]


@pytest.mark.historia(297)
def test_una_conversacion_ajena_o_un_mensaje_de_otra_responde_404(cliente, validada):
    validada(LABORATORIO)
    conversacion, mensaje = responder(cliente, JORGE)
    propia = nueva(cliente, MARIA)

    ajena = proponer(cliente, conversacion, mensaje)  # María, sobre la de Jorge
    cruzada = proponer(cliente, propia, mensaje)

    assert ajena.status_code == 404
    assert ajena.json()["type"] == "/errores/conversacion-no-encontrada"
    assert cruzada.status_code == 404
    assert (cruzada.json()["type"], cruzada.json()["recurso"]) == (
        "/errores/recurso-no-encontrado", "mensaje"
    )


@pytest.mark.historia(297)
def test_sin_revisar_efemerides_proponer_responde_403(cliente, validada):
    validada(LABORATORIO)
    conversacion, mensaje = responder(cliente, DIRECTORA_DE_CARRERA)

    respuesta = proponer(cliente, conversacion, mensaje)

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"


# --- Leer (GET /api/relatos) ------------------------------------------------------------------


@pytest.mark.historia(300)
def test_revisar_efemerides_ve_todo_y_consultar_solo_los_aprobados(cliente, validada, dueno):
    validada(LABORATORIO)
    conversacion, mensaje = responder(cliente)
    relato = proponer(cliente, conversacion, mensaje).json()["relato_id"]

    entrar(cliente, MARIA)  # la RGC revisa lo que propuso el Coordinador
    lista_rgc = cliente.get("/api/relatos").json()
    detalle_rgc = cliente.get(f"/api/relatos/{relato}")
    entrar(cliente, DIRECTORA_DE_CARRERA)
    lista_directora = cliente.get("/api/relatos").json()
    propuestas = cliente.get("/api/relatos", params={"estado": "Propuesta"})
    detalle_directora = cliente.get(f"/api/relatos/{relato}")
    dueno.execute(
        "UPDATE acervo.relato SET estado = 'aprobada', aprobado_por = %s, aprobado_el = now(),"
        " metodo_autenticacion = 'dev' WHERE relato_id = %s",
        (MARIA, relato),
    )
    aprobado = cliente.get(f"/api/relatos/{relato}")

    assert [r["relato_id"] for r in lista_rgc["relatos"]] == [relato]
    assert lista_rgc["siguiente"] is None
    assert detalle_rgc.status_code == 200 and detalle_rgc.json()["estado"] == "Propuesta"
    assert lista_directora["relatos"] == []
    assert propuestas.status_code == 403
    assert detalle_directora.status_code == 404
    assert (detalle_directora.json()["type"], detalle_directora.json()["recurso"]) == (
        "/errores/recurso-no-encontrado", "relato"
    )
    assert aprobado.status_code == 200
    assert (aprobado.json()["estado"], aprobado.json()["aprobado_por"]) == (
        "Aprobada", "mrossi@fie.undef.edu.ar"
    )
    assert [r["relato_id"] for r in cliente.get("/api/relatos").json()["relatos"]] == [relato]


@pytest.mark.historia(243)
@pytest.mark.rf("RF-42")
def test_una_pieza_no_publicable_sigue_en_la_conversacion_pero_no_es_fuente_de_un_relato(
    cliente, validada, url_bd
):
    pieza = validada(LABORATORIO)
    # La persona la marca con 125; acá, la columna que proyecta la Ficha (como el retiro).
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET publicable = false WHERE pieza_id = %s", (pieza,)
        )
    conversacion = nueva(cliente, JORGE)
    respuesta, eventos = turno(cliente, conversacion, PREGUNTA)
    fuentes = dict(eventos)["fuentes"]

    propuesta = proponer(cliente, conversacion, str(fuentes["mensaje_id"]))

    assert respuesta.status_code == 200, respuesta.text
    assert pieza in str(fuentes)
    assert propuesta.status_code == 409, propuesta.text
    assert propuesta.json()["type"] == "/errores/respuesta-no-proponible"


# --- Revisar (065): editar, aprobar y descartar como una efeméride ----------------------------


@pytest.fixture
def propuesto(cliente, sembrar, dueno) -> tuple[str, list[str]]:
    """Un Relato Propuesto por Jorge con las Citas `[1]` y `[3]` (la `[2]` no se verificó), de dos
    Piezas Validadas, sin pasar por el índice: `(relato_id, [pieza_1, pieza_3])`."""
    piezas = [sembrar("1975-05-03"), sembrar("1980-08-10", resumen="Creación del laboratorio.")]
    fichas = [
        dueno.execute("SELECT ficha_id FROM acervo.ficha WHERE pieza_id = %s", (p,)).fetchone()[0]
        for p in piezas
    ]
    conversacion = dueno.execute(
        "INSERT INTO acervo.conversacion (persona_id) VALUES (%s) RETURNING conversacion_id",
        (JORGE,),
    ).fetchone()[0]
    mensaje = dueno.execute(
        "INSERT INTO acervo.mensaje_conversacion (conversacion_id, orden, autor, contenido,"
        " estado) VALUES (%s, 2, 'historia_viva', 'Se creó [1]. Se amplió [3].', 'completa')"
        " RETURNING mensaje_id",
        (conversacion,),
    ).fetchone()[0]
    with cliente.app.state.bd.transaccion() as tx:
        vista = cliente.app.state.efemerides.proponer_relato(
            tx, mensaje_id=mensaje, titulo="El laboratorio", texto="Se creó [1]. Se amplió [3].",
            fuentes=[
                FuenteDeRelato(1, UUID(piezas[0]), fichas[0]),
                FuenteDeRelato(3, UUID(piezas[1]), fichas[1]),
            ],
            configuracion_efectiva={}, principal=_jorge(),
        )  # fmt: skip
    return str(vista.relato_id), piezas


def _editar(cliente, relato: str, cuerpo: dict):
    return cliente.put(f"/api/relatos/{relato}", json=cuerpo)


def _aprobar(cliente, relato: str, version_base: int = 1):
    return cliente.post(f"/api/relatos/{relato}/aprobacion", json={"version_base": version_base})


def _descartar(cliente, relato: str, motivo: object = "El hito ya tiene otro relato."):
    return cliente.post(f"/api/relatos/{relato}/descarte", json={"motivo": motivo})


def _eventos(cliente, relato: str, accion: Accion) -> list:
    return RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        objeto=ObjetoRef("relato", relato), accion=accion
    )


@pytest.mark.historia(299, 300)
def test_editar_y_aprobar_firma_el_relato_con_sus_citas_y_lo_audita(cliente, propuesto, dueno):
    relato, piezas = propuesto
    entrar(cliente, MARIA)  # la RGC revisa lo que propuso el Coordinador

    editado = _editar(cliente, relato, {"version_base": 1, "texto": "Se fundó [1]. Creció [3]."})
    vieja = _aprobar(cliente, relato, 1)
    aprobado = _aprobar(cliente, relato, 2)

    assert editado.status_code == 200, editado.text
    assert (editado.json()["version"], editado.json()["titulo"]) == (2, "El laboratorio")
    assert editado.json()["editado_por"] == "mrossi@fie.undef.edu.ar"
    [edicion] = _eventos(cliente, relato, Accion.RELATO_EDICION)
    assert edicion.antes == {
        "version": 1, "titulo": "El laboratorio", "cuerpo": "Se creó [1]. Se amplió [3]."
    }
    assert edicion.despues == {
        "version": 2, "titulo": "El laboratorio", "cuerpo": "Se fundó [1]. Creció [3]."
    }
    assert vieja.status_code == 409
    assert vieja.json()["type"] == "/errores/version-desactualizada"
    assert (vieja.json()["version_base"], vieja.json()["version_actual"]) == (1, 2)
    assert aprobado.status_code == 200, aprobado.text
    cuerpo = aprobado.json()
    assert cuerpo["estado"] == "Aprobada"
    assert (cuerpo["aprobado_por"], cuerpo["aprobado_por_nombre"]) == (
        "mrossi@fie.undef.edu.ar", "Maria Fernanda Rossi"
    )
    assert cuerpo["metodo_autenticacion"] == "dev"
    assert cuerpo["cantidad_fuentes"] == 2
    # Las fuentes conservan su `[n]`, aunque falte el 2.
    assert [(f["posicion"], f["pieza_id"]) for f in cuerpo["fuentes"]] == [
        (1, piezas[0]), (3, piezas[1])
    ]
    assert dueno.execute(
        "SELECT estado, aprobado_por::text, metodo_autenticacion FROM acervo.relato"
        " WHERE relato_id = %s",
        (relato,),
    ).fetchone() == ("aprobada", MARIA, "dev")
    [aprobacion] = _eventos(cliente, relato, Accion.RELATO_APROBACION)
    assert aprobacion.actor.usuario_id == UUID(MARIA)
    assert aprobacion.despues == {"version": 2, "metodo_autenticacion": "dev"}
    # Quien solo consulta ya lo ve, y la lista trae la cantidad de fuentes sin traerlas.
    entrar(cliente, DIRECTORA_DE_CARRERA)
    assert cliente.get(f"/api/relatos/{relato}").json() == cuerpo
    [fila] = cliente.get("/api/relatos").json()["relatos"]
    assert "fuentes" not in fila and fila["cantidad_fuentes"] == 2


@pytest.mark.historia(299)
def test_descartar_guarda_quien_y_el_motivo_y_queda_inmutable(cliente, propuesto):
    relato, _ = propuesto
    entrar(cliente, MARIA)

    corto = _descartar(cliente, relato, "  abcd  ")
    respuesta = _descartar(cliente, relato, "  El hito ya tiene otro relato.  ")
    otra_vez = _descartar(cliente, relato)
    editar = _editar(cliente, relato, {"version_base": 1, "texto": "Otro."})
    aprobar = _aprobar(cliente, relato)

    assert (corto.status_code, corto.json()["campo"]) == (422, "motivo")
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["motivo_descarte"]) == (
        "Descartada", "El hito ya tiene otro relato."
    )
    assert (cuerpo["descartado_por"], cuerpo["descartado_por_nombre"]) == (
        "mrossi@fie.undef.edu.ar", "Maria Fernanda Rossi"
    )
    [descarte] = _eventos(cliente, relato, Accion.RELATO_DESCARTE)
    assert descarte.despues == {"version": 1, "motivo": "El hito ya tiene otro relato."}
    for cerrado in (otra_vez, editar, aprobar):
        assert cerrado.status_code == 409, cerrado.text
        assert cerrado.json()["type"] == "/errores/relato-no-propuesto"
        assert cerrado.json()["estado_actual"] == "Descartada"
        assert "Descartado por Maria Fernanda Rossi" in cerrado.json()["detail"]
    assert cliente.get(f"/api/relatos/{relato}").json() == cuerpo


@pytest.mark.historia(299)
@pytest.mark.parametrize(
    "cuerpo",
    [{"texto": "Sin versión."}, {"version_base": 1}, {"version_base": 1, "texto": "  "}],
    ids=["sin-version", "sin-cambios", "texto-vacio"],
)
def test_editar_sin_version_o_con_el_texto_vacio_es_422(cliente, propuesto, cuerpo):
    relato, _ = propuesto
    entrar(cliente, MARIA)

    respuesta = _editar(cliente, relato, cuerpo)

    assert respuesta.status_code == 422, respuesta.text
    assert cliente.get(f"/api/relatos/{relato}").json()["version"] == 1


@pytest.mark.historia(299)
@pytest.mark.parametrize("persona", [DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO])
def test_con_solo_consultar_revisar_un_relato_es_403(cliente, propuesto, persona):
    relato, _ = propuesto
    entrar(cliente, persona)

    respuestas = [
        _editar(cliente, relato, {"version_base": 1, "texto": "Otro."}),
        _aprobar(cliente, relato),
        _descartar(cliente, relato),
    ]

    assert [r.status_code for r in respuestas] == [403, 403, 403]
    assert {r.json()["type"] for r in respuestas} == {"/errores/permiso-insuficiente"}
    entrar(cliente, MARIA)
    assert cliente.get(f"/api/relatos/{relato}").json()["estado"] == "Propuesta"


@pytest.mark.historia(299)
def test_revisar_un_relato_inexistente_es_404(cliente):
    entrar(cliente, MARIA)
    nadie = "00000000-0000-4000-8000-000000000000"

    respuestas = [
        _editar(cliente, nadie, {"version_base": 1, "texto": "x"}),
        _aprobar(cliente, nadie),
        _descartar(cliente, nadie),
    ]

    assert {(r.status_code, r.json()["recurso"]) for r in respuestas} == {(404, "relato")}


# --- «Fuente retirada» y relatos_aprobados (065; R-12, auditoría de tickets, decisión 11) ----


def _marca(dueno, relato: str):
    return dueno.execute(
        "SELECT fuente_retirada_el FROM acervo.relato WHERE relato_id = %s", (relato,)
    ).fetchone()[0]


@pytest.mark.historia(299, 301)
def test_una_fuente_que_deja_de_estar_validada_marca_el_relato_y_sale_de_los_aprobados(
    cliente, propuesto, dueno
):
    relato, piezas = propuesto
    entrar(cliente, MARIA)
    assert _aprobar(cliente, relato).status_code == 200
    hoy = datetime.now(UTC).date()
    dias = (hoy - timedelta(days=1), hoy + timedelta(days=1))
    with cliente.app.state.bd.transaccion() as tx:
        [antes] = cliente.app.state.efemerides.relatos_aprobados(tx, *dias)
    assert [(f.posicion, str(f.pieza_id)) for f in antes.fuentes] == [
        (1, piezas[0]), (3, piezas[1])
    ]

    editar_ficha(cliente, piezas[1], INCOMPLETA, 1)  # Validada → Incompleta, en su transacción
    marcada = _marca(dueno, relato)
    editar_ficha(cliente, piezas[1], COMPLETA, 2)  # vuelve a Validada: la marca queda
    editar_ficha(cliente, piezas[1], INCOMPLETA, 3)  # y no se remarca

    assert marcada is not None and _marca(dueno, relato) == marcada
    detalle = cliente.get(f"/api/relatos/{relato}").json()
    assert (detalle["estado"], detalle["fuente_retirada"]) == ("Aprobada", True)
    assert [f["retirada"] for f in detalle["fuentes"]] == [False, True]
    with cliente.app.state.bd.transaccion() as tx:
        assert cliente.app.state.efemerides.relatos_aprobados(tx, *dias) == []


@pytest.mark.historia(299)
def test_un_propuesto_marcado_se_edita_y_se_descarta_y_la_marca_no_se_quita(
    cliente, propuesto, dueno
):
    relato, piezas = propuesto
    editar_ficha(cliente, piezas[0], INCOMPLETA, 1)
    entrar(cliente, MARIA)

    editado = _editar(cliente, relato, {"version_base": 1, "titulo": "Otro título"})
    with pytest.raises(psycopg.errors.RestrictViolation, match="no se quita"), dueno.transaction():
        dueno.execute(
            "UPDATE acervo.relato SET fuente_retirada_el = NULL WHERE relato_id = %s",
            (relato,),
        )
    descartado = _descartar(cliente, relato)

    assert (editado.json()["version"], editado.json()["fuente_retirada"]) == (2, True)
    assert (descartado.json()["estado"], descartado.json()["fuente_retirada"]) == (
        "Descartada", True
    )


@pytest.mark.historia(301)
def test_relatos_aprobados_va_por_dia_de_aprobacion_en_buenos_aires_sin_los_demas(
    cliente, dueno
):
    def aprobado(cuando: str, **extra: bool) -> str:
        return _relato(dueno, estado="aprobada", aprobado_el=datetime.fromisoformat(cuando), **extra)

    tarde = aprobado("2026-05-03T23:30:00-03:00")  # en UTC ya es el 4
    temprano = aprobado("2026-05-03T00:10:00-03:00")
    aprobado("2026-05-02T23:59:00-03:00")
    retirado = aprobado("2026-05-03T12:00:00-03:00", retirada=True)
    _relato(dueno, estado="propuesta")
    _relato(dueno, estado="descartada")

    def ids(desde: date, hasta: date) -> list[str]:
        efemerides = cliente.app.state.efemerides
        with cliente.app.state.bd.transaccion() as tx:
            return [str(r.relato_id) for r in efemerides.relatos_aprobados(tx, desde, hasta)]

    assert ids(date(2026, 5, 3), date(2026, 5, 3)) == [temprano, tarde]
    assert ids(date(2026, 5, 4), date(2026, 5, 4)) == []
    with cliente.app.state.bd.transaccion() as tx:
        [primero, _] = cliente.app.state.efemerides.relatos_aprobados(
            tx, date(2026, 5, 3), date(2026, 5, 3)
        )
    assert (primero.aprobado_por, primero.titulo, primero.fuentes) == (
        "mrossi@fie.undef.edu.ar", "El laboratorio", ()
    )
    assert not {f.name for f in fields(RelatoEntregable)} & {"alerta_inyeccion", "aviso_inyeccion"}
    # `GET /api/relatos` filtra por el mismo día de aprobación (ahí sí se ve el marcado).
    entrar(cliente, MARIA)
    lista = cliente.get("/api/relatos", params={"desde": "2026-05-03", "hasta": "2026-05-03"})
    assert {r["relato_id"] for r in lista.json()["relatos"]} == {temprano, tarde, retirado}
    al_reves = cliente.get("/api/relatos", params={"desde": "2026-05-04", "hasta": "2026-05-03"})
    assert al_reves.status_code == 422


# --- De punta a punta desde P6 (R11) ----------------------------------------------------------


@pytest.mark.historia(297, 299, 300)
def test_una_respuesta_de_la_conversacion_se_propone_se_revisa_y_se_aprueba_con_sus_citas(
    cliente, validada
):
    laboratorio = validada(LABORATORIO)
    conversacion, mensaje = responder(cliente)  # Jorge pregunta en P6
    relato = proponer(cliente, conversacion, mensaje).json()["relato_id"]
    entrar(cliente, MARIA)

    [en_la_pestana] = cliente.get("/api/relatos", params={"estado": "Propuesta"}).json()["relatos"]
    editado = _editar(cliente, relato, {"version_base": 1, "titulo": "El laboratorio"})
    aprobado = _aprobar(cliente, relato, 2)

    assert en_la_pestana["relato_id"] == relato
    assert editado.status_code == 200, editado.text
    assert aprobado.status_code == 200, aprobado.text
    assert [f["pieza_id"] for f in aprobado.json()["fuentes"]] == [laboratorio]
    hoy = datetime.now(UTC).date()
    with cliente.app.state.bd.transaccion() as tx:
        [entregable] = cliente.app.state.efemerides.relatos_aprobados(
            tx, hoy - timedelta(days=1), hoy + timedelta(days=1)
        )
    assert (str(entregable.relato_id), entregable.titulo) == (relato, "El laboratorio")
    assert [str(f.pieza_id) for f in entregable.fuentes] == [laboratorio]
