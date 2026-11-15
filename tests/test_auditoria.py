"""El registro de auditoría (spec, módulo `auditoria`; RF-45, RNF-07, DEC-40, ADR-0002).

Postgres real y como `hv_app`: lo que se registró no se borra ni se reescribe, y cada evento
se escribe en la misma transacción que el cambio.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from historia_viva.auditoria import (
    Accion,
    Actor,
    Evento,
    ObjetoRef,
    RegistroDeAuditoria,
    TipoDeActor,
)
from historia_viva.bd import BaseNoDisponible, Bd
from historia_viva.procesamiento.cola import Cola
from validacion import ADMINISTRADOR_TECNICO, MARIA, PDF

pytestmark = [
    pytest.mark.rf("RF-45"),
    pytest.mark.confirma("`listar` lee desde su propio pool lo que confirmó `registrar`"),
]

MARIA_ACTOR = Actor(TipoDeActor.PERSONA, "mrossi@fie.undef.edu.ar", usuario_id=UUID(MARIA))


@pytest.fixture(scope="module")
def bd(url_app: str) -> Iterator[Bd]:
    """Un pool por módulo: en Windows abrir una conexión por test es el cuello de botella
    (ticket 130)."""
    bd = Bd(url_app, tamano_pool=1)
    yield bd
    bd.cerrar()


@pytest.fixture
def registro(bd: Bd) -> RegistroDeAuditoria:
    return RegistroDeAuditoria(bd=bd)


@pytest.fixture
def cliente(url_app, capa_modelos, directorio_originales) -> Iterator[TestClient]:
    """Sin trabajador: lo único que confirma es la subida que se está mirando."""
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
    )
    cliente = TestClient(app, headers=CABECERAS)
    entrar(cliente, MARIA)
    yield cliente
    app.state.bd.cerrar()


def _subir(cliente: TestClient) -> str:
    respuesta = cliente.post("/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")})
    pieza_id: str = respuesta.json()["pieza_id"]
    return pieza_id


@pytest.mark.rnf("RNF-07")
@pytest.mark.parametrize(
    "sentencia", ["UPDATE acervo.auditoria SET accion = ''", "DELETE FROM acervo.auditoria"]
)
def test_hv_app_solo_inserta_y_consulta(bd, registro, sentencia):
    pieza = ObjetoRef("pieza", "una-pieza")
    with bd.transaccion() as tx:
        registro.registrar(tx, actor=MARIA_ACTOR, accion=Accion.PIEZA_INGRESO, objeto=pieza)

    with pytest.raises(psycopg.errors.InsufficientPrivilege), bd.transaccion() as tx:
        tx.execute(sentencia)

    assert [e.accion for e in registro.listar(objeto=pieza)] == [Accion.PIEZA_INGRESO]


@pytest.mark.historia(290)
def test_cada_ingesta_deja_registro_con_quien_la_subio(cliente, registro):
    pieza_id = _subir(cliente)

    [evento] = registro.listar(objeto=ObjetoRef("pieza", pieza_id))
    assert (evento.accion, evento.actor) == (Accion.PIEZA_INGRESO, MARIA_ACTOR)
    assert evento.despues == {"via_ingreso": "archivo", "sha256": hashlib.sha256(PDF).hexdigest()}


@pytest.mark.historia(171)
def test_una_ingesta_que_se_revierte_no_deja_evento(cliente, registro, monkeypatch):
    def encolar_que_falla(*args, **kwargs):
        raise RuntimeError("falla inyectada al encolar")

    monkeypatch.setattr(Cola, "encolar", encolar_que_falla)

    with pytest.raises(RuntimeError, match="falla inyectada"):
        _subir(cliente)

    assert registro.listar() == []


def _zip(miembros: dict[str, str]) -> bytes:
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as comprimido:
        for nombre, contenido in miembros.items():
            comprimido.writestr(nombre, contenido)
    return memoria.getvalue()


def _rechazo(registro: RegistroDeAuditoria) -> Evento:
    [evento] = registro.listar(accion=Accion.ARCHIVO_RECHAZO)
    assert evento.actor == MARIA_ACTOR
    return evento


ZIP_SUELTO = _zip({"fotos/leeme.txt": "no soy un Word"})
LIBRO = _zip({"xl/workbook.xml": "<workbook/>"})
WORD_CON_MACROS = _zip({"word/document.xml": "<w:document/>", "word/vbaProject.bin": "macro"})
WORD_REPETIDO = _zip({"word/document.xml": "<w:document/>", "WORD/document.xml": "<w:document/>"})
WORD_SIN_TIPOS = _zip({"word/document.xml": "<w:document/>"})
FRONTERA = "frontera-de-prueba"
CSV_ILEGIBLE_AL_FINAL = "Año;Carrera\r\n2024;Ingeniería\r\n".encode() * 80_000 + b"\xff\r\n"


@pytest.mark.historia(290)
@pytest.mark.parametrize(
    ("nombre", "contenido", "estado", "motivo", "despues"),
    [
        pytest.param(
            "pagina.html",
            b"<html>hola</html>",
            415,
            "tipo-no-admitido",
            {"clase": None, "tamano_bytes": None, "sha256": None},
            id="formato-desconocido",
        ),
        pytest.param(
            "fotos.pdf",
            ZIP_SUELTO,
            415,
            "tipo-no-admitido",
            {
                "clase": None,
                "tamano_bytes": len(ZIP_SUELTO),
                "sha256": hashlib.sha256(ZIP_SUELTO).hexdigest(),
                "causa": "contenedor-no-admitido",
            },
            id="zip-que-no-es-word",
        ),
        # Un byte que no es UTF-8 pasado el primer bloque: se rechaza ya escrito (089).
        pytest.param(
            "notas.csv",
            CSV_ILEGIBLE_AL_FINAL,
            422,
            "archivo-ilegible",
            {
                "clase": None,
                "tamano_bytes": len(CSV_ILEGIBLE_AL_FINAL),
                "sha256": hashlib.sha256(CSV_ILEGIBLE_AL_FINAL).hexdigest(),
                "causa": "codificacion-desconocida",
            },
            id="csv-ilegible-pasada-la-muestra",
        ),
        pytest.param(
            "acta.docm",
            WORD_CON_MACROS,
            415,
            "tipo-no-admitido",
            {
                "clase": "documento",
                "tamano_bytes": len(WORD_CON_MACROS),
                "sha256": hashlib.sha256(WORD_CON_MACROS).hexdigest(),
                "causa": "con-macros",
            },
            id="word-con-macros",
        ),
        pytest.param(
            "acta.docx",
            ZIP_SUELTO[:-10],
            422,
            "archivo-ilegible",
            {
                "clase": None,
                "tamano_bytes": len(ZIP_SUELTO) - 10,
                "sha256": hashlib.sha256(ZIP_SUELTO[:-10]).hexdigest(),
                "causa": "zip-corrupto",
            },
            id="zip-danado",
        ),
        pytest.param(
            "acta.docx",
            WORD_REPETIDO,
            422,
            "archivo-ilegible",
            {
                "clase": "documento",
                "tamano_bytes": len(WORD_REPETIDO),
                "sha256": hashlib.sha256(WORD_REPETIDO).hexdigest(),
                "causa": "zip-corrupto",
            },
            id="word-con-nombres-repetidos",
        ),
        pytest.param(
            "acta.docx",
            WORD_SIN_TIPOS,
            415,
            "tipo-no-admitido",
            {
                "clase": None,
                "tamano_bytes": len(WORD_SIN_TIPOS),
                "sha256": hashlib.sha256(WORD_SIN_TIPOS).hexdigest(),
                "causa": "contenedor-no-admitido",
            },
            id="word-sin-su-tipo-declarado",
        ),
    ],
)
def test_un_archivo_que_no_se_admite_deja_archivo_rechazo_con_motivo_clase_tamano_y_sha256(
    cliente, registro, nombre, contenido, estado, motivo, despues
):
    respuesta = cliente.post(
        "/api/piezas", files={"archivo": (nombre, contenido, "application/octet-stream")}
    )

    assert respuesta.status_code == estado
    evento = _rechazo(registro)
    assert (evento.motivo, evento.despues) == (motivo, despues)
    assert evento.objeto == ObjetoRef("archivo", despues["sha256"])


@pytest.mark.historia(290)
def test_un_archivo_sobre_el_tope_deja_archivo_rechazo_con_su_clase(cliente, registro):
    cuerpo = (
        f'--{FRONTERA}\r\nContent-Disposition: form-data; name="archivo"; '
        f'filename="acta.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
    ).encode() + PDF + f"\r\n--{FRONTERA}--\r\n".encode()

    respuesta = cliente.post(
        "/api/piezas",
        content=cuerpo,
        headers={
            "content-type": f"multipart/form-data; boundary={FRONTERA}",
            "content-length": str(41 * 1024 * 1024),
        },
    )

    assert respuesta.status_code == 413
    evento = _rechazo(registro)
    assert evento.motivo == "archivo-demasiado-grande"
    assert evento.despues == {"clase": "documento", "tamano_bytes": None, "sha256": None}


@pytest.mark.historia(290)
def test_un_duplicado_deja_archivo_rechazo_con_la_pieza_que_ya_estaba(cliente, registro):
    pieza_id = _subir(cliente)

    respuesta = cliente.post(
        "/api/piezas", files={"archivo": ("copia.pdf", PDF, "application/pdf")}
    )

    assert respuesta.status_code == 409
    sha256 = hashlib.sha256(PDF).hexdigest()
    evento = _rechazo(registro)
    assert evento.motivo == "pieza-duplicada"
    assert evento.objeto == ObjetoRef("archivo", sha256)
    assert evento.despues == {
        "clase": "documento",
        "tamano_bytes": len(PDF),
        "sha256": sha256,
        "pieza_id": pieza_id,
    }
    assert len(registro.listar(accion=Accion.PIEZA_INGRESO)) == 1


@pytest.mark.historia(290)
@pytest.mark.parametrize(
    "falla",
    [
        pytest.param(
            BaseNoDisponible(psycopg.OperationalError("base caída inyectada")), id="sin-base"
        ),
        pytest.param(RuntimeError("falla inyectada al registrar"), id="otra-falla"),
    ],
)
def test_si_registrar_el_rechazo_falla_la_respuesta_es_la_del_rechazo(
    cliente, registro, monkeypatch, falla
):
    def registrar_que_falla(*args, **kwargs):
        raise falla

    monkeypatch.setattr(RegistroDeAuditoria, "registrar", registrar_que_falla)

    respuesta = cliente.post(
        "/api/piezas", files={"archivo": ("pagina.html", b"<html>hola</html>", "text/html")}
    )

    assert respuesta.status_code == 415
    assert respuesta.json()["type"] == "/errores/tipo-no-admitido"


@pytest.mark.parametrize("lado", ["antes", "despues"])
@pytest.mark.parametrize(
    "clave", ["token", "token_hash", "secreto", "password", "code", "id_token"]
)
def test_registrar_con_una_clave_de_secreto_lanza_error_y_no_deja_evento(
    bd, registro, clave, lado
):
    with pytest.raises(ValueError, match=clave) as error, bd.transaccion() as tx:
        registro.registrar(
            tx,
            actor=MARIA_ACTOR,
            accion=Accion.PIEZA_INGRESO,
            objeto=ObjetoRef("pieza", "una-pieza"),
            **{lado: {"agente": "Juan", clave: "hv_12345678_secreto"}},
        )

    assert "hv_12345678_secreto" not in str(error.value)
    assert registro.listar() == []


def test_una_clave_de_secreto_anidada_tambien_lanza_error(bd, registro):
    with pytest.raises(ValueError, match="id_token"), bd.transaccion() as tx:
        registro.registrar(
            tx,
            actor=MARIA_ACTOR,
            accion=Accion.PIEZA_INGRESO,
            objeto=ObjetoRef("pieza", "una-pieza"),
            despues={"sesion": [{"proveedor": "google", "id_token": "eyJ..."}]},
        )


@pytest.mark.parametrize("tipo", ["persona", "cli", "agente", "anonimo", "sistema"])
def test_el_actor_distingue_persona_cli_agente_anonimo_y_sistema(bd, registro, tipo):
    actor = Actor(TipoDeActor(tipo), f"quien-{tipo}")
    with bd.transaccion() as tx:
        registro.registrar(
            tx, actor=actor, accion=Accion.PIEZA_INGRESO, objeto=ObjetoRef("pieza", "una")
        )

    assert [e.actor for e in registro.listar()] == [actor]


@pytest.mark.historia(171)
def test_un_acceso_denegado_guarda_el_dominio_y_un_hash_corto_nunca_el_email(bd, registro):
    with bd.transaccion() as tx:
        registro.registrar(
            tx,
            actor=Actor.rechazado("intruso.total@gmail.com"),
            accion=Accion.SESION_RECHAZO,
            objeto=ObjetoRef("sesion"),
        )

    [evento] = registro.listar()
    assert evento.actor.tipo == TipoDeActor.ANONIMO
    assert re.fullmatch(r"gmail\.com#[0-9a-f]{12}", evento.actor.etiqueta)
    assert Actor.rechazado("otro@gmail.com").etiqueta != evento.actor.etiqueta
    assert "sin-arroba" not in Actor.rechazado("sin-arroba").etiqueta


def test_un_anonimo_con_el_email_completo_no_se_registra(bd, registro):
    with pytest.raises(ValueError, match="email"), bd.transaccion() as tx:
        registro.registrar(
            tx,
            actor=Actor(TipoDeActor.ANONIMO, "intruso.total@gmail.com"),
            accion=Accion.SESION_RECHAZO,
            objeto=ObjetoRef("sesion"),
        )


def test_la_lista_filtra_por_objeto_y_por_accion_y_pagina_hacia_atras(bd, registro):
    # En una sola transacción: los cuatro eventos comparten `cuando` y la página no salta ninguno.
    eventos = [
        (Accion.PIEZA_INGRESO, ObjetoRef("pieza", "primera")),
        (Accion.SESION_RECHAZO, ObjetoRef("sesion")),
        (Accion.PIEZA_INGRESO, ObjetoRef("pieza", "segunda")),
        (Accion.PIEZA_INGRESO, ObjetoRef("pieza", "tercera")),
    ]
    with bd.transaccion() as tx:
        for accion, objeto in eventos:
            registro.registrar(tx, actor=MARIA_ACTOR, accion=accion, objeto=objeto)

    def ids(lista):
        return [e.objeto.id for e in lista]

    assert ids(registro.listar(objeto=ObjetoRef("pieza", "segunda"))) == ["segunda"]
    assert ids(registro.listar(objeto=ObjetoRef("pieza"))) == ["tercera", "segunda", "primera"]
    assert [e.accion for e in registro.listar(accion=Accion.SESION_RECHAZO)] == [
        Accion.SESION_RECHAZO
    ]
    pagina = registro.listar(accion=Accion.PIEZA_INGRESO, limite=2)
    assert ids(pagina) == ["tercera", "segunda"]
    siguiente = registro.listar(accion=Accion.PIEZA_INGRESO, limite=2, antes_de=pagina[-1].id)
    assert ids(siguiente) == ["primera"]


@pytest.mark.historia(146)
def test_admin_auditoria_lista_filtra_y_pagina_con_cursor_opaco(cliente, bd, registro):
    with bd.transaccion() as tx:
        for numero in range(3):
            registro.registrar(
                tx, actor=MARIA_ACTOR, accion=Accion.CATALOGO_CARGA,
                objeto=ObjetoRef("carrera", f"c{numero}"), despues={"numero": numero},
            )
        registro.registrar(
            tx, actor=MARIA_ACTOR, accion=Accion.SESION_CIERRE, objeto=ObjetoRef("sesion")
        )
    tecnico = entrar(cliente, ADMINISTRADOR_TECNICO)

    def pedir(**parametros):
        respuesta = tecnico.get("/api/admin/auditoria", params=parametros)
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        return [e["objeto_id"] for e in cuerpo["eventos"]], cuerpo["siguiente"]

    primera, siguiente = pedir(accion="catalogo.carga", limite=2)
    assert (primera, siguiente is not None) == (["c2", "c1"], True)
    assert pedir(accion="catalogo.carga", limite=2, cursor=siguiente) == (["c0"], None)
    assert pedir(objeto_tipo="carrera", objeto_id="c1") == (["c1"], None)
    assert pedir(objeto_tipo="sesion")[0] == [None]
    [evento] = tecnico.get(
        "/api/admin/auditoria", params={"objeto_id": "c0"}
    ).json()["eventos"]
    assert {k: evento[k] for k in ("persona", "accion", "objeto_tipo", "detalle")} == {
        "persona": "mrossi@fie.undef.edu.ar",
        "accion": "catalogo.carga",
        "objeto_tipo": "carrera",
        "detalle": {"antes": None, "despues": {"numero": 0}, "motivo": None},
    }
    assert set(evento) == {
        "evento_id", "fecha", "persona", "accion", "objeto_tipo", "objeto_id", "detalle"
    }


@pytest.mark.historia(146)
@pytest.mark.parametrize("parametros", [{"cursor": "no-es-un-cursor"}, {"accion": "inventada"},
                                        {"limite": 201}])
def test_admin_auditoria_rechaza_un_pedido_invalido(cliente, parametros):
    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get(
        "/api/admin/auditoria", params=parametros
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["type"] == "/errores/pedido-invalido"


@pytest.mark.historia(146)
def test_admin_auditoria_sin_operar_sistema_da_403(cliente):
    respuesta = cliente.get("/api/admin/auditoria")

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"


@pytest.mark.historia(146)
@pytest.mark.parametrize("numero", ["0", "-1", str(2**63), "1e3"])
def test_admin_auditoria_un_cursor_fuera_de_rango_da_422(cliente, numero):
    cursor = base64.urlsafe_b64encode(numero.encode()).decode()

    respuesta = entrar(cliente, ADMINISTRADOR_TECNICO).get(
        "/api/admin/auditoria", params={"cursor": cursor}
    )

    assert (respuesta.status_code, respuesta.json()["campo"]) == (422, "cursor")
