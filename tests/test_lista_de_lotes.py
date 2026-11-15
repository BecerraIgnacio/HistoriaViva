"""La lista de Lotes (085, P10) y retomar un Lote existente: listar con cursor y filtros, editar el
Contexto solo mientras el Lote no tiene Piezas y el lugar que le queda.

Por HTTP contra Postgres real. Cada test crea sus propias personas: la base de la suite junta los
Lotes de todos los tests del proceso, y «Mis Lotes» de una persona nueva es solo lo del test.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest

from aplicacion import entrar
from historia_viva.dominio.errores import LoteLleno, LoteNoEncontrado
from test_lotes import LOTE, _llenar
from validacion import PDF

pytestmark = [
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
    # Las personas `rgc` que crea `_persona` terminarían el interinato de `test_identidad` (R-01).
    pytest.mark.usefixtures("padron_restaurado"),
]

SIN_PIEZAS = {"Recibida": 0, "Pendiente": 0, "Incompleta": 0, "Validada": 0}


def _persona(url_bd, nombre="Ana Pérez", rol="rgc") -> str:
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        fila = conexion.execute(
            "INSERT INTO acervo.usuario (email, nombre, rol) VALUES (%s, %s, %s)"
            " RETURNING usuario_id",
            (f"{uuid4()}@fie.example", nombre, rol),
        ).fetchone()
    assert fila is not None
    return str(fila[0])


def _crear(cliente, **cambios) -> dict:
    respuesta = cliente.post("/api/lotes", json=LOTE | cambios)
    assert respuesta.status_code == 201, respuesta.text
    lote: dict = respuesta.json()["lote"]
    return lote


def _listar(cliente, **query):
    respuesta = cliente.get("/api/lotes", params=query)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


@pytest.mark.historia(73)
def test_la_lista_trae_cada_lote_con_su_estado_sus_conteos_y_quien_lo_creo(cliente, url_bd):
    entrar(cliente, _persona(url_bd, "Ana Pérez"))
    lote = _crear(cliente, propietario="SEU", carreras=["ing-civil"])
    _llenar(url_bd, lote["lote_id"], 2)

    pagina = _listar(cliente, creado_por="yo")

    assert pagina == {
        "lotes": [
            lote
            | {
                "creado_por_nombre": "Ana Pérez",
                "estado": "Procesando",
                "total": 2,
                "por_estado": SIN_PIEZAS | {"Recibida": 2},
                "puede_cargar": True,
                # Ninguna falló y, sin procesamientos terminados, no hay espera que estimar (086).
                "fallidas": 0,
                "estimacion": None,
            }
        ],
        "siguiente": None,
    }


def _ids(pagina) -> list[str]:
    return [lote["lote_id"] for lote in pagina["lotes"]]


@pytest.mark.historia(73)
def test_mis_lotes_trae_solo_los_mios_y_todos_tambien_los_ajenos(cliente, url_bd):
    entrar(cliente, _persona(url_bd))
    mio = _crear(cliente)["lote_id"]
    yo = _listar(cliente, creado_por="yo")
    entrar(cliente, otra := _persona(url_bd, "Jorge Gómez", "coordinador"))
    ajeno = _crear(cliente)["lote_id"]

    assert _ids(yo) == [mio]
    assert _ids(_listar(cliente, creado_por="yo")) == [ajeno]
    assert _ids(_listar(cliente, creado_por=otra)) == [ajeno]
    assert _ids(_listar(cliente, limite=2)) == [ajeno, mio]


@pytest.mark.historia(73)
def test_el_filtro_por_estado_deja_los_lotes_en_ese_estado_y_sin_resultados_da_lista_vacia(
    cliente, url_bd
):
    entrar(cliente, _persona(url_bd))
    vacio = _crear(cliente)["lote_id"]
    procesando = _crear(cliente)["lote_id"]
    _llenar(url_bd, procesando, 1)

    assert _ids(_listar(cliente, creado_por="yo", estado="Vacío")) == [vacio]
    assert _ids(_listar(cliente, creado_por="yo", estado="Procesando")) == [procesando]
    # «Ningún Lote coincide» (P10): la pantalla lo dice sobre la lista vacía.
    assert _listar(cliente, creado_por="yo", estado="Validado") == {"lotes": [], "siguiente": None}


@pytest.mark.historia(73)
def test_sin_lotes_la_lista_viene_vacia(cliente, url_bd):
    # «Todavía no hay Lotes. [+ Nuevo Lote]» (P10) sale de esta lista vacía.
    entrar(cliente, _persona(url_bd))

    assert _listar(cliente, creado_por="yo") == {"lotes": [], "siguiente": None}


@pytest.mark.historia(73)
def test_la_lista_se_pagina_por_cursor_del_mas_nuevo_al_mas_viejo(cliente, url_bd):
    entrar(cliente, _persona(url_bd))
    primero, segundo, tercero = (_crear(cliente)["lote_id"] for _ in range(3))

    pagina = _listar(cliente, creado_por="yo", limite=2)
    ultima = _listar(cliente, creado_por="yo", limite=2, cursor=pagina["siguiente"])

    assert _ids(pagina) == [tercero, segundo]
    assert (_ids(ultima), ultima["siguiente"]) == ([primero], None)
    # El cursor también vale con el filtro de estado.
    filtrada = _listar(cliente, creado_por="yo", estado="Vacío", limite=1)
    resto = _listar(cliente, creado_por="yo", estado="Vacío", cursor=filtrada["siguiente"])
    assert _ids(resto) == [segundo, primero]


@pytest.mark.historia(73)
@pytest.mark.parametrize(
    ("query", "campo"),
    [({"cursor": "no-es-un-cursor"}, "cursor"), ({"creado_por": "ana"}, "creado_por")],
    ids=["cursor", "creado-por"],
)
def test_un_cursor_o_un_creador_que_no_se_entiende_es_pedido_invalido(
    cliente, url_bd, query, campo
):
    entrar(cliente, _persona(url_bd))

    respuesta = cliente.get("/api/lotes", params=query)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (
        "/errores/pedido-invalido",
        campo,
    )


@pytest.mark.historia(73)
def test_listar_lotes_es_de_quien_carga(cliente, url_bd):
    entrar(cliente, _persona(url_bd, rol="director_carrera"))

    assert cliente.get("/api/lotes").status_code == 403


def _carreras_del_lote(url_bd, lote_id):
    with psycopg.connect(url_bd) as conexion:
        return dict(
            conexion.execute(
                "SELECT carrera_id, quitada_el IS NOT NULL FROM acervo.lote_carrera"
                " WHERE lote_id = %s",
                (lote_id,),
            ).fetchall()
        )


@pytest.mark.rf("RF-01")
@pytest.mark.historia(74)
def test_el_contexto_de_un_lote_vacio_se_edita_y_quitar_una_carrera_la_marca_quitada(
    cliente, url_bd
):
    entrar(cliente, _persona(url_bd))
    lote = _crear(cliente, evento="Colación", propietario="SEU", carreras=["ing-civil"])

    respuesta = cliente.patch(
        f"/api/lotes/{lote['lote_id']}",
        json={
            "nombre": "Colaciones 1975",
            "fecha_inicio": "1975-05-03",
            "precision_fecha": "anio",
            "fecha_fin": "1975-12-31",
            "evento": None,
            "propietario": "Biblioteca FIE",
            "carreras": ["ing-mecanica"],
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    editado = lote | {
        "nombre": "Colaciones 1975",
        "fecha_inicio": "1975-01-01",
        "precision_fecha": "anio",
        "fecha_fin": "1975-12-31",
        "evento": None,
        "propietario": "Biblioteca FIE",
        "carreras": ["ing-mecanica"],
    }
    assert respuesta.json() == {"lote": editado}
    assert cliente.get(f"/api/lotes/{lote['lote_id']}").json()["lote"] == editado | {
        "contexto_editable": True, "por_completar": []
    }
    assert _carreras_del_lote(url_bd, lote["lote_id"]) == {"ing-civil": True, "ing-mecanica": False}
    # Volver a poner una Carrera quitada la deja vigente otra vez.
    cliente.patch(f"/api/lotes/{lote['lote_id']}", json={"carreras": ["ing-civil"]})
    assert _carreras_del_lote(url_bd, lote["lote_id"]) == {"ing-civil": False, "ing-mecanica": True}


@pytest.mark.rf("RF-01")
@pytest.mark.historia(74)
def test_con_una_pieza_cargada_el_contexto_queda_congelado(cliente, url_bd):
    entrar(cliente, _persona(url_bd))
    lote = _crear(cliente)
    _llenar(url_bd, lote["lote_id"], 1)

    respuesta = cliente.patch(f"/api/lotes/{lote['lote_id']}", json={"origen": "Otro archivo"})

    assert respuesta.status_code == 409
    assert (respuesta.json()["type"], respuesta.json()["piezas"]) == (
        "/errores/contexto-congelado",
        1,
    )
    assert cliente.get(f"/api/lotes/{lote['lote_id']}").json()["lote"]["origen"] == LOTE["origen"]


@pytest.mark.rf("RF-01")
@pytest.mark.historia(74)
@pytest.mark.parametrize(
    ("cambios", "estado", "tipo", "campo"),
    [
        ({}, 422, "pedido-invalido", None),
        ({"origen": None}, 422, "pedido-invalido", "origen"),
        ({"origen": "  "}, 422, "pedido-invalido", "origen"),
        ({"fecha_fin": "1985-12-31"}, 422, "fecha-invalida", "fecha_fin"),
        ({"carreras": ["ing-naval"]}, 422, "carrera-desconocida", "carreras"),
        ({"tipo": "foto"}, 422, "pedido-invalido", "tipo"),
    ],
    ids=["vacio", "origen-nulo", "origen-en-blanco", "fecha", "carrera", "campo-extra"],
)
def test_un_cambio_de_contexto_invalido_se_rechaza_con_el_campo(
    cliente, url_bd, cambios, estado, tipo, campo
):
    entrar(cliente, _persona(url_bd))
    lote = _crear(cliente)

    respuesta = cliente.patch(f"/api/lotes/{lote['lote_id']}", json=cambios)

    assert respuesta.status_code == estado
    assert (respuesta.json()["type"], respuesta.json().get("campo")) == (f"/errores/{tipo}", campo)


@pytest.mark.rf("RF-01")
def test_editar_un_lote_que_no_existe_responde_404(cliente, url_bd):
    entrar(cliente, _persona(url_bd))

    respuesta = cliente.patch(f"/api/lotes/{uuid4()}", json={"origen": "Otro"})

    assert (respuesta.status_code, respuesta.json()["type"]) == (404, "/errores/lote-no-encontrado")


@pytest.mark.historia(75)
def test_lotes_dice_si_al_lote_le_queda_lugar_para_la_restauracion(cliente, url_bd):
    entrar(cliente, _persona(url_bd))
    lote_id = _crear(cliente)["lote_id"]
    _llenar(url_bd, lote_id, 99)
    estado = cliente.app.state

    with estado.bd.transaccion() as tx:
        estado.lotes.hay_lugar(tx, UUID(lote_id))
    _llenar(url_bd, lote_id, 1)
    with pytest.raises(LoteLleno), estado.bd.transaccion() as tx:
        estado.lotes.hay_lugar(tx, UUID(lote_id))
    with pytest.raises(LoteNoEncontrado), estado.bd.transaccion() as tx:
        estado.lotes.hay_lugar(tx, uuid4())


def _subir(cliente, lote_id, nombre):
    return cliente.post(
        "/api/piezas",
        data={"lote_id": lote_id},
        files={"archivo": (nombre, PDF + nombre.encode(), "application/pdf")},
    )


@pytest.mark.historia(75)
@pytest.mark.historia(79)
def test_retomar_un_lote_a_medio_cargar_suma_otra_tanda_y_lo_ya_subido_ya_estaba(
    cliente, url_bd, capa_modelos
):
    entrar(cliente, _persona(url_bd))
    lote_id = _crear(cliente)["lote_id"]
    capa_modelos.demorar()
    primera = _subir(cliente, lote_id, "acta-1.pdf").json()

    # La primera tanda sigue procesándose: la segunda entra igual.
    segunda = _subir(cliente, lote_id, "acta-2.pdf")
    otra_vez = _subir(cliente, lote_id, "acta-1.pdf")

    assert segunda.status_code == 201
    assert (otra_vez.status_code, otra_vez.json()["pieza_id"], otra_vez.json()["duplicada"]) == (
        200,
        primera["pieza_id"],
        True,
    )
    progreso = cliente.get(f"/api/lotes/{lote_id}").json()["progreso"]
    assert (progreso["estado"], progreso["total"]) == ("Procesando", 2)


@pytest.mark.historia(73)
def test_la_pantalla_de_lotes_se_sirve_con_su_script(cliente):
    pantalla = cliente.get("/lotes.html")

    assert pantalla.status_code == 200
    assert '<script src="/estaticos/lotes.js"></script>' in pantalla.text
    assert cliente.get("/estaticos/lotes.js").status_code == 200


@pytest.mark.rf("RF-01")
@pytest.mark.historia(74)
def test_un_lote_cuya_unica_pieza_se_retiro_vuelve_a_tener_el_contexto_editable(cliente, url_bd):
    entrar(cliente, _persona(url_bd))
    lote = _crear(cliente)
    _llenar(url_bd, lote["lote_id"], 1)
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = creada_por,"
            " borrada_motivo = 'Subida por error' WHERE lote_id = %s",
            (lote["lote_id"],),
        )

    respuesta = cliente.patch(f"/api/lotes/{lote['lote_id']}", json={"origen": "Otro archivo"})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["lote"]["origen"] == "Otro archivo"
