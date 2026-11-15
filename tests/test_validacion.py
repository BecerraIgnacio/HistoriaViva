"""«Validar y firmar» pasa por `validacion`: abre la transacción, guarda la Ficha y refleja el
cambio de indexabilidad en la misma (DEC-21, DEC-72, DEC-91, ADR-0002).

`reflejar` se prueba con un espía en lugar del indexador; la aplicación usa `indexacion` (047).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from aplicacion import CABECERAS, app_sin_trabajador, en_marcha, entrar
from capa_modelos_falsa import PDF_CON_INYECCION, TRANSCRIPCION_FIJA
from espera import esperar_estado
from fastapi.testclient import TestClient
from historia_viva.dominio.errores import PermisoInsuficiente
from historia_viva.dominio.permisos import Rol
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.identidad import Principal
from historia_viva.validacion import PiezaVista, Validacion
from test_fichas import COMPLETA, divergencias
from test_ingesta import MP4, AnalizadorFalso
from test_lotes import LOTE
from validacion import (
    ADMINISTRADOR_TECNICO,
    DIRECTORA_DE_CARRERA,
    EDICION_COMPLETA,
    JORGE,
    MARIA,
    PDF,
    edicion_sin,
)

pytestmark = [
    pytest.mark.rf("RF-15"),
    pytest.mark.confirma("la app confirma desde su propio pool y su trabajador"),
]


@pytest.mark.historia(120)
def test_guardar_completa_deja_validada_firmada_individual_y_la_propuesta_intacta(
    cliente, pieza_pendiente, url_bd
):
    url = f"/api/piezas/{pieza_pendiente}/ficha"

    guardado = cliente.put(url, json=EDICION_COMPLETA).json()

    assert guardado["estado"] == "Validada"
    assert guardado["validada_por"] == MARIA
    assert guardado["validada_el"]
    assert guardado["modo_validacion"] == "individual"
    ficha = cliente.get(url).json()
    assert (ficha["version"], ficha["origen_version"]) == (2, "persona")
    assert ficha["frontmatter"]["modo_validacion"] == "individual"
    propuesta = ficha["propuesta"]
    assert propuesta["frontmatter"]["estado"] == "Pendiente"
    assert propuesta["frontmatter"]["modo_validacion"] is None
    assert propuesta["cuerpo"] == TRANSCRIPCION_FIJA
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []



@pytest.mark.historia(108, 120)
@pytest.mark.rnf("RNF-07")
def test_editar_una_validada_crea_otra_version_y_la_firma_queda_trazada(
    cliente, pieza_pendiente, url_bd
):
    url = f"/api/piezas/{pieza_pendiente}/ficha"
    primera = cliente.put(url, json=EDICION_COMPLETA).json()

    segunda = entrar(cliente, JORGE).put(
        url, json=EDICION_COMPLETA | {"version_base": 2, "resumen": "Acta corregida."}
    ).json()

    assert (segunda["estado"], segunda["version"]) == ("Validada", 3)
    assert (segunda["validada_por"], segunda["modo_validacion"]) == (JORGE, "individual")
    assert segunda["validada_el"] >= primera["validada_el"]
    with psycopg.connect(url_bd) as conexion:
        versiones = conexion.execute(
            "SELECT version, origen_version, editada_por::text, metodo_autenticacion"
            " FROM acervo.ficha WHERE pieza_id = %s ORDER BY version",
            (pieza_pendiente,),
        ).fetchall()
        assert divergencias(conexion) == []
    assert versiones == [
        (1, "modelo", None, None),
        (2, "persona", MARIA, "dev"),
        (3, "persona", JORGE, "dev"),
    ]


def _indexaciones(url_bd: str, pieza_id: str) -> list[tuple[str, str, list[str]]]:
    with psycopg.connect(url_bd) as conexion:
        return conexion.execute(
            "SELECT estado, ficha_id::text,"
            " ARRAY(SELECT jsonb_object_keys(configuracion_efectiva->'por_capacidad'))"
            " FROM acervo.trabajo_procesamiento WHERE pieza_id = %s AND tipo = 'indexacion'",
            (pieza_id,),
        ).fetchall()


def _ficha_id_vigente(url_bd: str, pieza_id: str) -> str:
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(
            "SELECT ficha_id::text FROM acervo.ficha WHERE pieza_id = %s"
            " ORDER BY version DESC LIMIT 1",
            (pieza_id,),
        ).fetchone()
    assert fila is not None
    return str(fila[0])


@pytest.mark.historia(121)
@pytest.mark.rf("RF-14")
def test_validar_encola_la_indexacion_de_la_ficha_vigente_con_solo_la_fila_de_embeber(
    cliente, pieza_pendiente, url_bd
):
    respuesta = cliente.put(f"/api/piezas/{pieza_pendiente}/ficha", json=EDICION_COMPLETA)

    assert respuesta.json()["estado"] == "Validada"
    assert _indexaciones(url_bd, pieza_pendiente) == [
        ("encolado", _ficha_id_vigente(url_bd, pieza_pendiente), ["embeber"])
    ]


@pytest.mark.historia(121)
@pytest.mark.rf("RF-16")
def test_dos_guardados_con_la_misma_version_base_dan_409_y_un_solo_trabajo(
    cliente, pieza_pendiente, url_bd
):
    url = f"/api/piezas/{pieza_pendiente}/ficha"

    codigos = [cliente.put(url, json=EDICION_COMPLETA).status_code for _ in range(2)]

    assert codigos == [200, 409]
    assert len(_indexaciones(url_bd, pieza_pendiente)) == 1


@pytest.mark.rf("RF-15")
def test_los_listados_dejan_afuera_a_las_piezas_retiradas(cliente, pieza_pendiente, url_bd):
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
            " borrada_motivo = 'Subida por error' WHERE pieza_id = %s",
            (MARIA, pieza_pendiente),
        )

    assert cliente.get("/api/piezas").json() == {"piezas": []}
    assert cliente.get("/api/piezas?estado=Pendiente").json() == {"piezas": []}


MARIA_FIRMANDO = Principal(
    usuario_id=UUID(MARIA),
    nombre="María",
    email="maria@fie.undef.edu.ar",
    rol=Rol.RGC,
    metodo=MetodoAutenticacion.DEV,
    vence_el=datetime(2100, 1, 1, tzinfo=UTC),
)


class IndexadorEspia:
    """Anota lo que `reflejar` le pide; con `falla`, esa operación revienta."""

    def __init__(self, falla: str | None = None) -> None:
        self.pedidos: list[tuple[str, UUID]] = []
        self._falla = falla

    def _anotar(self, operacion: str, tx, pieza_id: UUID) -> None:
        # Lo pide dentro de la transacción de la firma, con la Pieza ya escrita.
        estado = tx.execute(
            "SELECT estado FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,)
        ).fetchone()["estado"]
        self.pedidos.append((operacion, estado))
        if operacion == self._falla:
            raise RuntimeError(f"{operacion} falló")

    def desindexar(self, tx, pieza_id):
        self._anotar("desindexar", tx, pieza_id)

    def encolar(self, tx, pieza_id):
        self._anotar("encolar", tx, pieza_id)


def _validacion(cliente, indexador, *, bloque_habilitado: bool = False) -> Validacion:
    estado = cliente.app.state
    return Validacion(
        bd=estado.bd,
        fichas=estado.fichas,
        indexador=indexador,
        lotes=estado.lotes,
        auditoria=estado.auditoria,
        marcar_fuente_retirada=estado.efemerides.marcar_fuente_retirada,
        bloque_habilitado=bloque_habilitado,
    )


@pytest.mark.historia(121, 122)
def test_reflejar_recibe_el_cambio_de_indexabilidad_de_cada_transicion(cliente, pieza_pendiente):
    espia = IndexadorEspia()
    validacion = _validacion(cliente, espia)
    pieza_id = UUID(pieza_pendiente)
    pedidos_por_paso = []

    for version_base, edicion in enumerate(
        [
            COMPLETA,  # Pendiente -> Validada
            replace(COMPLETA, resumen="Acta corregida."),  # Validada -> Validada
            replace(COMPLETA, origen=None),  # Validada -> Incompleta
            replace(COMPLETA, origen=None, resumen="Otra."),  # Incompleta -> Incompleta
        ],
        start=1,
    ):
        espia.pedidos.clear()
        validacion.guardar_ficha(
            pieza_id=pieza_id,
            version_base=version_base,
            edicion=edicion,
            principal=MARIA_FIRMANDO,
        )
        pedidos_por_paso.append(list(espia.pedidos))

    assert pedidos_por_paso == [
        [("encolar", "Validada")],
        [("desindexar", "Validada"), ("encolar", "Validada")],
        [("desindexar", "Incompleta")],
        [],
    ]


@pytest.mark.historia(121)
@pytest.mark.parametrize("falla", ["encolar", "desindexar"])
def test_una_falla_al_reflejar_revierte_la_firma(cliente, pieza_pendiente, falla):
    pieza_id = UUID(pieza_pendiente)
    if falla == "desindexar":
        _validacion(cliente, IndexadorEspia()).guardar_ficha(
            pieza_id=pieza_id, version_base=1, edicion=COMPLETA, principal=MARIA_FIRMANDO
        )
    antes = cliente.app.state.fichas.obtener_ficha(pieza_id)

    with pytest.raises(RuntimeError, match=falla):
        _validacion(cliente, IndexadorEspia(falla=falla)).guardar_ficha(
            pieza_id=pieza_id,
            version_base=antes.version,
            edicion=replace(COMPLETA, resumen="Firma que no tiene que quedar."),
            principal=MARIA_FIRMANDO,
        )

    despues = cliente.app.state.fichas.obtener_ficha(pieza_id)
    assert (despues.version, despues.campos) == (antes.version, antes.campos)
    assert cliente.app.state.fichas.obtener_pieza(pieza_id).fila.estado is antes.campos.estado


@pytest.mark.historia(280)
def test_validacion_no_deja_firmar_a_quien_no_tiene_validar(cliente, pieza_pendiente):
    # Lo verifica `validacion`, no solo la ruta (DEC-124): ningún otro camino firma sin VALIDAR.
    tecnico = replace(
        MARIA_FIRMANDO, usuario_id=UUID(ADMINISTRADOR_TECNICO), rol=Rol.ADMIN_TECNICO
    )
    espia = IndexadorEspia()

    with pytest.raises(PermisoInsuficiente):
        _validacion(cliente, espia).guardar_ficha(
            pieza_id=UUID(pieza_pendiente), version_base=1, edicion=COMPLETA, principal=tecnico
        )

    vigente = cliente.app.state.fichas.obtener_ficha(UUID(pieza_pendiente))
    assert (vigente.version, vigente.origen_version) == (1, "modelo")
    assert espia.pedidos == []


# --- Aprobación en bloque (108; CU-07, DEC-22, DEC-135, R-28) ---------------------------------

PROPIETARIO = "Secretaría de Extensión Universitaria"
COMUNES = {"tipo": "acta", "tipo_actividad": "Evento Académico"}
"""Lo único que el bloque aplica, además del contexto adicional (DEC-135)."""


@pytest.fixture
def cliente_bloque(url_app, capa_modelos, directorio_originales):
    """La app con `HV_BLOQUE_HABILITADO=true`: los tests la encienden (R-28)."""
    with en_marcha(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        bloque_habilitado=True,
    ) as cliente:
        try:
            yield entrar(cliente, MARIA)
        finally:
            capa_modelos.liberar()


def _lote(cliente, **cambios) -> str:
    respuesta = cliente.post("/api/lotes", json=LOTE | cambios)
    assert respuesta.status_code == 201, respuesta.text
    return str(respuesta.json()["lote"]["lote_id"])


def _subir_al_lote(cliente, lote_id, nombre, contenido=None) -> str:
    respuesta = cliente.post(
        "/api/piezas",
        data={"lote_id": lote_id},
        files={"archivo": (nombre, contenido or PDF + nombre.encode(), "application/pdf")},
    )
    assert respuesta.status_code == 201, respuesta.text
    return str(respuesta.json()["pieza_id"])


def _pendientes(cliente, lote_id, cuantas) -> list[str]:
    piezas = [_subir_al_lote(cliente, lote_id, f"acta-{n}.pdf") for n in range(cuantas)]
    for pieza_id in piezas:
        esperar_estado(cliente, pieza_id, "Pendiente")
    return piezas


def _previa(cliente, lote_id, clase="documento"):
    respuesta = cliente.post(f"/api/lotes/{lote_id}/aprobaciones", json={"clase": clase})
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _vistas(previa) -> list[dict[str, object]]:
    return [{"pieza_id": e["pieza_id"], "version": e["version"]} for e in previa["elegibles"]]


def _confirmar(cliente, lote_id, aprobacion_id, piezas, **cuerpo):
    return cliente.post(
        f"/api/lotes/{lote_id}/aprobaciones/{aprobacion_id}/confirmacion",
        json=COMUNES | cuerpo | {"piezas": piezas},
    )


def _rechazos(rechazadas) -> set[tuple[str, str, str | None]]:
    return {(r["pieza_id"], r["motivo"], r.get("campo")) for r in rechazadas}


def _filas(url_bd, piezas):
    """Por Pieza: estado, modo, tipo, tipo de actividad, firma, versión vigente, su origen y su
    aprobación en bloque."""
    with psycopg.connect(url_bd) as conexion:
        return {
            str(fila[0]): fila[1:]
            for fila in conexion.execute(
                "SELECT p.pieza_id, p.estado, p.modo_validacion, p.tipo, p.tipo_actividad,"
                " p.validada_por::text, f.version, f.origen_version, f.aprobacion_id::text"
                " FROM acervo.pieza p JOIN acervo.ficha f ON f.pieza_id = p.pieza_id"
                " AND f.vigente WHERE p.pieza_id = ANY(%s::uuid[])",
                (list(piezas),),
            ).fetchall()
        }


def _contar(url_bd, consulta, *parametros) -> int:
    with psycopg.connect(url_bd) as conexion:
        fila = conexion.execute(consulta, parametros).fetchone()
    assert fila is not None
    return int(fila[0])


def _indexaciones_de(url_bd, piezas) -> int:
    return _contar(
        url_bd,
        "SELECT count(*) FROM acervo.trabajo_procesamiento WHERE tipo = 'indexacion'"
        " AND pieza_id = ANY(%s::uuid[])",
        list(piezas),
    )


@pytest.mark.historia(125, 126, 127, 128)
@pytest.mark.rf("RF-18")
def test_bloque_previa_y_confirmacion(cliente_bloque, capa_modelos, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    comunes = _pendientes(cliente, lote_id, 8)
    alerta = _subir_al_lote(cliente, lote_id, "inyeccion.pdf", PDF_CON_INYECCION)
    esperar_estado(cliente, alerta, "Pendiente")
    capa_modelos.resumen = ""
    sin_resumen = _subir_al_lote(cliente, lote_id, "sin-resumen.pdf")
    esperar_estado(cliente, sin_resumen, "Pendiente")
    vieja, *firmables = comunes

    previa = _previa(cliente, lote_id)

    assert {e["pieza_id"] for e in previa["elegibles"]} == set(comunes)
    assert _rechazos(previa["rechazadas"]) == {
        (alerta, "alerta_inyeccion", None),
        (sin_resumen, "falta_campo", "resumen"),
    }
    # De una, la persona vio la v1 y otra guardó la v2 antes de que confirmara: una Pendiente
    # no tiene otra versión vieja posible (solo la v1 es Pendiente).
    otra_persona = entrar(cliente, JORGE).put(
        f"/api/piezas/{vieja}/ficha", json=edicion_sin("origen")
    )
    assert otra_persona.json()["version"] == 2
    vistas = _vistas(previa) + [
        {"pieza_id": alerta, "version": 1},
        {"pieza_id": sin_resumen, "version": 1},
    ]

    respuesta = _confirmar(
        entrar(cliente, MARIA),
        lote_id,
        previa["aprobacion_id"],
        vistas,
        contexto_adicional="Colación de 1975.",
    )

    assert respuesta.status_code == 200, respuesta.text
    resultado = respuesta.json()
    assert sorted(resultado["aprobadas"]) == sorted(firmables)
    assert _rechazos(resultado["rechazadas"]) == {
        (vieja, "version_desactualizada", None),
        (alerta, "alerta_inyeccion", None),
        (sin_resumen, "falta_campo", "resumen"),
    }
    assert resultado["duracion_s"] >= 0
    filas = _filas(url_bd, comunes + [alerta, sin_resumen])
    firmada = ("Validada", "bloque", "acta", "Evento Académico", MARIA, 2, "persona")
    assert {filas[p][:7] for p in firmables} == {firmada}
    assert {filas[p][7] for p in firmables} == {previa["aprobacion_id"]}
    for pieza_id in (alerta, sin_resumen):
        assert (filas[pieza_id][0], filas[pieza_id][5]) == ("Pendiente", 1)
    # La vieja quedó como la dejó la otra persona: sin versión del bloque.
    assert (filas[vieja][0], filas[vieja][1], filas[vieja][5], filas[vieja][7]) == (
        "Incompleta",
        None,
        2,
        None,
    )
    ficha = cliente.get(f"/api/piezas/{firmables[0]}/ficha").json()
    assert "Colación de 1975." in ficha["contexto"]
    assert ficha["frontmatter"]["propietario"] == PROPIETARIO
    with psycopg.connect(url_bd) as conexion:
        aprobacion = conexion.execute(
            "SELECT n_aprobadas, n_rechazadas, duracion_s IS NOT NULL"
            " FROM acervo.aprobacion_en_bloque WHERE aprobacion_id = %s",
            (previa["aprobacion_id"],),
        ).fetchone()
        eventos = conexion.execute(
            "SELECT objeto_id, despues->'aprobadas' FROM acervo.auditoria"
            " WHERE accion = 'bloque.aprobacion'"
        ).fetchall()
        assert divergencias(conexion) == []
    assert aprobacion == (7, 3, True)
    assert [(objeto, sorted(aprobadas)) for objeto, aprobadas in eventos] == [
        (previa["aprobacion_id"], sorted(firmables))
    ]
    assert _indexaciones_de(url_bd, firmables) == 7
    assert _indexaciones_de(url_bd, [vieja, alerta, sin_resumen]) == 0
    # Sin mediciones por Pieza: la medición del bloque es `duracion_s` (DEC-146 punto 14).
    assert _contar(url_bd, "SELECT count(*) FROM acervo.medicion_validacion") == 0


@pytest.mark.historia(126, 127)
@pytest.mark.rf("RF-18")
def test_bloque_sin_propietario_heredado_deja_todas_afuera(cliente_bloque, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente)
    piezas = _pendientes(cliente, lote_id, 2)

    previa = _previa(cliente, lote_id)
    confirmacion = _confirmar(
        cliente,
        lote_id,
        previa["aprobacion_id"],
        [{"pieza_id": p, "version": 1} for p in piezas],
    )

    assert previa["elegibles"] == []
    sin_propietario = {(p, "falta_campo", "propietario") for p in piezas}
    assert _rechazos(previa["rechazadas"]) == sin_propietario
    assert confirmacion.status_code == 200, confirmacion.text
    assert confirmacion.json()["aprobadas"] == []
    assert _rechazos(confirmacion.json()["rechazadas"]) == sin_propietario
    assert {fila[0] for fila in _filas(url_bd, piezas).values()} == {"Pendiente"}


@pytest.mark.historia(126)
@pytest.mark.rf("RF-18")
@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("origen", "Otro archivo"),
        ("fecha_inicio", "1975-05-03"),
        ("precision_fecha", "dia"),
        ("propietario", PROPIETARIO),
        ("carreras", []),
    ],
)
def test_el_cuerpo_del_bloque_no_acepta_lo_heredado(cliente_bloque, campo, valor):
    lote_id = _lote(cliente_bloque, propietario=PROPIETARIO)
    previa = _previa(cliente_bloque, lote_id)

    respuesta = _confirmar(
        cliente_bloque,
        lote_id,
        previa["aprobacion_id"],
        [{"pieza_id": str(uuid4()), "version": 1}],
        **{campo: valor},
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["type"].endswith("/pedido-invalido")


@pytest.mark.historia(126)
@pytest.mark.rf("RF-18")
def test_el_bloque_rechaza_un_tipo_fuera_del_catalogo_sin_firmar(cliente_bloque, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    [pieza_id] = _pendientes(cliente, lote_id, 1)
    previa = _previa(cliente, lote_id)

    respuesta = _confirmar(
        cliente, lote_id, previa["aprobacion_id"], _vistas(previa), tipo="pergamino"
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["type"].endswith("/tipo-desconocido")
    assert _filas(url_bd, [pieza_id])[pieza_id][0] == "Pendiente"


@pytest.fixture
def cliente_bloque_con_video(url_app, capa_modelos, directorio_originales):
    """Sin trabajador (los documentos quedan Recibidas) y con el análisis de video doble."""
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        analizar_video=AnalizadorFalso(),
        bloque_habilitado=True,
    )
    cliente = TestClient(app, headers=CABECERAS)
    yield entrar(cliente, MARIA)
    app.state.bd.cerrar()


@pytest.mark.historia(125)
@pytest.mark.rf("RF-18")
def test_videos_y_recibidas_quedan_fuera_del_bloque(cliente_bloque_con_video):
    cliente = cliente_bloque_con_video
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    recibida = _subir_al_lote(cliente, lote_id, "acta.pdf")
    video = cliente.post(
        "/api/piezas/video", params={"nombre": "colacion.mp4", "lote_id": lote_id}, content=MP4
    ).json()["pieza_id"]

    documentos = _previa(cliente, lote_id, "documento")
    videos = cliente.post(f"/api/lotes/{lote_id}/aprobaciones", json={"clase": "video"})

    # Solo Pendientes de una clase: el video no entra en la previa de los documentos.
    assert (documentos["elegibles"], _rechazos(documentos["rechazadas"])) == (
        [],
        {(recibida, "procesando", None)},
    )
    # Un video no se firma sin verlo (DEC-135): ni siquiera abre una previa.
    assert videos.status_code == 422
    assert (videos.json()["type"].endswith("/pedido-invalido"), videos.json()["campo"]) == (
        True,
        "clase",
    )
    assert video not in {e["pieza_id"] for e in documentos["elegibles"]}


@pytest.mark.historia(125, 128)
@pytest.mark.rf("RF-18")
def test_una_pieza_que_no_estaba_en_la_previa_no_se_firma_aunque_la_manden(
    cliente_bloque, capa_modelos, url_bd
):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    [vista] = _pendientes(cliente, lote_id, 1)
    capa_modelos.demorar()
    tardia = _subir_al_lote(cliente, lote_id, "tardia.pdf")
    previa = _previa(cliente, lote_id)
    capa_modelos.liberar()
    esperar_estado(cliente, tardia, "Pendiente")

    respuesta = _confirmar(
        cliente,
        lote_id,
        previa["aprobacion_id"],
        [{"pieza_id": vista, "version": 1}, {"pieza_id": tardia, "version": 1}],
    )

    assert _rechazos(previa["rechazadas"]) == {(tardia, "procesando", None)}
    assert respuesta.json()["aprobadas"] == [vista]
    assert _rechazos(respuesta.json()["rechazadas"]) == {
        (tardia, "version_desactualizada", None)
    }
    assert _filas(url_bd, [tardia])[tardia][:2] == ("Pendiente", None)


@pytest.mark.historia(125)
@pytest.mark.rf("RF-18")
def test_confirma_solo_quien_abrio_la_previa(cliente_bloque, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    [pieza_id] = _pendientes(cliente, lote_id, 1)
    previa = _previa(cliente, lote_id)

    respuesta = _confirmar(entrar(cliente, JORGE), lote_id, previa["aprobacion_id"], _vistas(previa))

    assert respuesta.status_code == 409
    assert respuesta.json()["type"].endswith("/aprobacion-no-abierta")
    assert _filas(url_bd, [pieza_id])[pieza_id][0] == "Pendiente"


@pytest.mark.historia(280)
@pytest.mark.rf("RF-18")
def test_un_rol_sin_validar_no_abre_ni_confirma_un_bloque(cliente_bloque, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    [pieza_id] = _pendientes(cliente, lote_id, 1)
    previa = _previa(cliente, lote_id)
    entrar(cliente, DIRECTORA_DE_CARRERA)

    respuestas = [
        cliente.post(f"/api/lotes/{lote_id}/aprobaciones", json={"clase": "documento"}),
        _confirmar(cliente, lote_id, previa["aprobacion_id"], _vistas(previa)),
    ]

    assert [r.status_code for r in respuestas] == [403, 403]
    assert _filas(url_bd, [pieza_id])[pieza_id][0] == "Pendiente"
    assert _contar(url_bd, "SELECT count(*) FROM acervo.aprobacion_en_bloque") == 1


class EncolarFallaEnLa:
    """El indexador de la firma: la `n`-ésima indexación revienta."""

    def __init__(self, n: int) -> None:
        self._n = n
        self.encoladas = 0

    def desindexar(self, tx, pieza_id):
        raise AssertionError("el bloque parte de Pendiente: no hay nada que desindexar")

    def encolar(self, tx, pieza_id):
        self.encoladas += 1
        if self.encoladas == self._n:
            raise RuntimeError("encolar falló")


def _piezas_vistas(previa) -> tuple[PiezaVista, ...]:
    return tuple(PiezaVista(UUID(e["pieza_id"]), e["version"]) for e in previa["elegibles"])


def _confirmar_directo(validacion, lote_id, previa):
    return validacion.confirmar_en_bloque(
        aprobacion_id=UUID(previa["aprobacion_id"]),
        lote_id=UUID(lote_id),
        principal=MARIA_FIRMANDO,
        tipo="acta",
        tipo_actividad="Evento Académico",
        contexto_adicional=None,
        piezas=_piezas_vistas(previa),
    )


@pytest.mark.historia(128)
@pytest.mark.rf("RF-18")
def test_una_falla_al_encolar_la_cuarta_deja_todo_como_estaba(cliente_bloque, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    piezas = _pendientes(cliente, lote_id, 5)
    previa = _previa(cliente, lote_id)
    validacion = _validacion(cliente, EncolarFallaEnLa(4), bloque_habilitado=True)

    with pytest.raises(RuntimeError, match="encolar"):
        _confirmar_directo(validacion, lote_id, previa)

    assert {fila[:2] for fila in _filas(url_bd, piezas).values()} == {("Pendiente", None)}
    assert _indexaciones_de(url_bd, piezas) == 0
    assert _contar(
        url_bd,
        "SELECT count(*) FROM acervo.aprobacion_en_bloque WHERE confirmada_el IS NOT NULL",
    ) == 0
    assert _contar(
        url_bd, "SELECT count(*) FROM acervo.auditoria WHERE accion = 'bloque.aprobacion'"
    ) == 0


@pytest.mark.historia(128)
@pytest.mark.rf("RF-16", "RF-18")
def test_dos_confirmaciones_concurrentes_firman_una_sola_vez_cada_pieza(cliente_bloque, url_bd):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    piezas = _pendientes(cliente, lote_id, 3)
    previas = [_previa(cliente, lote_id) for _ in range(2)]
    validacion = cliente.app.state.validacion
    largada = threading.Barrier(2)

    def confirmar(previa):
        largada.wait(5)
        return _confirmar_directo(validacion, lote_id, previa)

    with ThreadPoolExecutor(2) as hilos:
        resultados = list(hilos.map(confirmar, previas))

    firmadas = [p for r in resultados for p in r.aprobadas]
    assert sorted(map(str, firmadas)) == sorted(piezas)
    perdedora = next(r for r in resultados if not r.aprobadas)
    assert {r.motivo.value for r in perdedora.rechazadas} == {"version_desactualizada"}
    assert {fila[5] for fila in _filas(url_bd, piezas).values()} == {2}
    assert _indexaciones_de(url_bd, piezas) == 3
    # La misma aprobación no se confirma dos veces.
    repetida = _confirmar(cliente, lote_id, previas[0]["aprobacion_id"], _vistas(previas[0]))
    assert repetida.status_code == 409
    assert repetida.json()["type"].endswith("/aprobacion-no-abierta")


@pytest.mark.historia(125, 128)
@pytest.mark.rf("RF-16", "RF-18")
def test_una_pieza_editada_entre_la_previa_y_la_confirmacion_sale_del_bloque(
    cliente_bloque, url_bd
):
    cliente = cliente_bloque
    lote_id = _lote(cliente, propietario=PROPIETARIO)
    editada, otra = _pendientes(cliente, lote_id, 2)
    previa = _previa(cliente, lote_id)
    guardado = entrar(cliente, JORGE).put(
        f"/api/piezas/{editada}/ficha", json=edicion_sin("origen")
    )
    assert guardado.json()["estado"] == "Incompleta"

    respuesta = _confirmar(
        entrar(cliente, MARIA), lote_id, previa["aprobacion_id"], _vistas(previa)
    )

    assert respuesta.json()["aprobadas"] == [otra]
    assert _rechazos(respuesta.json()["rechazadas"]) == {
        (editada, "version_desactualizada", None)
    }
    assert _filas(url_bd, [editada])[editada][:2] == ("Incompleta", None)


@pytest.mark.historia(125)
@pytest.mark.rf("RF-18")
def test_deshabilitada_la_previa_y_la_confirmacion_dan_409_sin_firmar(
    cliente, pieza_pendiente, url_bd
):
    lote_id = _lote(entrar(cliente, MARIA))
    url = f"/api/lotes/{lote_id}/aprobaciones"

    previa = cliente.post(url, json={"clase": "documento"})
    confirmacion = cliente.post(
        f"{url}/{uuid4()}/confirmacion",
        json=COMUNES | {"piezas": [{"pieza_id": pieza_pendiente, "version": 1}]},
    )

    for respuesta in (previa, confirmacion):
        assert respuesta.status_code == 409
        assert respuesta.json()["type"].endswith("/bloque-deshabilitado")
    assert _contar(url_bd, "SELECT count(*) FROM acervo.aprobacion_en_bloque") == 0
    assert _filas(url_bd, [pieza_pendiente])[pieza_pendiente][0] == "Pendiente"
    assert cliente.get("/api/yo").json()["sistema"]["bloque_habilitado"] is False


@pytest.mark.historia(125)
@pytest.mark.rf("RF-18")
def test_yo_dice_si_el_bloque_esta_habilitado(cliente_bloque):
    assert cliente_bloque.get("/api/yo").json()["sistema"]["bloque_habilitado"] is True
