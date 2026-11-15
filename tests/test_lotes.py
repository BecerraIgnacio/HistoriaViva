"""El Lote mínimo (026): se crea con su Contexto común y sus Piezas lo heredan.

Por HTTP contra Postgres real, con el trabajador y la capa falsa: lo único que se mira por
SQL es lo que la API no muestra (la copia congelada del trabajo) o lo que sería lento armar por
HTTP (un Lote con 99 Piezas).
"""

from __future__ import annotations

import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from capa_modelos_falsa import IMAGEN, RESUMEN_FIJO
from aplicacion import entrar
from espera import esperar_error_procesamiento, esperar_estado
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.dominio.errores import LimiteExcedido, PermisoInsuficiente
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.lote import ContextoDeLote
from historia_viva.dominio.permisos import Rol, puede_cargar_en_lote_ajeno
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.interruptor import Freno, cambiar
from historia_viva.lotes import Lotes
from test_autor_y_carreras import carreras_restauradas  # noqa: F401 (fixture)
from test_fichas import divergencias
from test_interruptor import TOMAS
from validacion import (
    ADMINISTRADOR_TECNICO,
    DIRECTORA_DE_CARRERA,
    EDICION_COMPLETA,
    JORGE,
    MARIA,
    PDF,
    abrir,
    mediciones,
)

pytestmark = pytest.mark.confirma("la app confirma desde su propio pool y su trabajador")

LOTE = {
    "nombre": "Colaciones 1970-1979",
    "origen": "Archivo fotográfico SEU",
    "fecha_inicio": "1970-01-01",
    "precision_fecha": "decada",
    "fecha_fin": "1979-12-31",
}
"""Lo obligatorio del Lote: nombre, origen y Fecha (DEC-18, DEC-113)."""

HEREDADOS = ["origen", "fecha_inicio", "fecha_fin", "precision_fecha"]


def _crear(cliente, persona=MARIA, **cambios):
    entrar(cliente, persona)
    return cliente.post("/api/lotes", json=LOTE | cambios)


@pytest.mark.rf("RF-01")
@pytest.mark.historia(65)
def test_crear_lote_con_contexto_comun(cliente):
    respuesta = _crear(cliente, evento="Colación de grados", lugar="Patio central")

    assert respuesta.status_code == 201
    lote = respuesta.json()["lote"]
    assert respuesta.headers["Location"] == f"/api/lotes/{lote['lote_id']}"
    assert {campo: lote[campo] for campo in LOTE} == LOTE
    assert (lote["evento"], lote["lugar"], lote["creado_por"]) == (
        "Colación de grados",
        "Patio central",
        MARIA,
    )
    leido = cliente.get(f"/api/lotes/{lote['lote_id']}").json()["lote"]
    assert leido == lote | {"contexto_editable": True, "por_completar": []}


@pytest.mark.rf("RF-01")
@pytest.mark.historia(65)
def test_evento_y_lugar_son_opcionales_y_la_fecha_se_normaliza(cliente):
    respuesta = _crear(cliente, fecha_inicio="1975-05-03")

    assert respuesta.status_code == 201
    lote = respuesta.json()["lote"]
    assert (lote["fecha_inicio"], lote["evento"], lote["lugar"]) == ("1970-01-01", None, None)


@pytest.mark.rf("RF-01")
@pytest.mark.historia(65)
@pytest.mark.parametrize(
    ("cambios", "tipo", "campo"),
    [
        ({"origen": "  "}, "pedido-invalido", "origen"),
        ({"nombre": ""}, "pedido-invalido", "nombre"),
        ({"origen": "\x07\x1b\x7f"}, "pedido-invalido", "origen"),
        ({"evento": "\x07 \x1b"}, "pedido-invalido", "evento"),
        ({"fecha_inicio": None}, "pedido-invalido", "fecha_inicio"),
        ({"precision_fecha": None}, "pedido-invalido", "precision_fecha"),
        ({"fecha_fin": None}, "pedido-invalido", "fecha_fin"),
        (
            {"precision_fecha": "rango", "fecha_inicio": "1979-12-31", "fecha_fin": "1970-01-01"},
            "fecha-invalida",
            "fecha_fin",
        ),
        ({"fecha_fin": "1975-12-31"}, "fecha-invalida", "fecha_fin"),
    ],
    ids=[
        "origen-vacio",
        "nombre-vacio",
        "origen-solo-controles",
        "evento-solo-controles",
        "sin-inicio",
        "sin-precision",
        "sin-fin",
        "fin-antes-del-inicio",
        "fin-que-contradice-la-precision",
    ],
)
def test_un_lote_sin_origen_ni_fecha_se_rechaza_con_el_campo(cliente, cambios, tipo, campo):
    pedido = {k: v for k, v in (LOTE | cambios).items() if v is not None}
    entrar(cliente, MARIA)

    respuesta = cliente.post("/api/lotes", json=pedido)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (f"/errores/{tipo}", campo)


@pytest.mark.rf("RF-01")
def test_el_contexto_del_lote_queda_en_nfc_y_sin_caracteres_de_control(cliente):
    """R-36: lo que entra como texto se guarda en NFC y sin controles; uno entre la letra y su
    tilde no impide componerla."""
    nfd = unicodedata.normalize("NFD", "Colación")
    partida = "Colacio\x07\u0301n"

    lote = _crear(
        cliente,
        nombre=f"{nfd}\x1b 1975",
        origen=f"Archivo\x07 {nfd}",
        evento=f"{partida} de grados",
        lugar=f"Sala {nfd}\x7f",
    ).json()["lote"]

    assert (lote["nombre"], lote["origen"], lote["evento"], lote["lugar"]) == (
        "Colación 1975",
        "Archivo Colación",
        "Colación de grados",
        "Sala Colación",
    )


@pytest.mark.rf("RF-01")
def test_un_lote_que_no_existe_responde_404(cliente):
    entrar(cliente, MARIA)

    respuesta = cliente.get("/api/lotes/00000000-0000-4000-8000-000000000000")

    assert respuesta.status_code == 404
    assert respuesta.json()["type"] == "/errores/lote-no-encontrado"


@pytest.mark.rf("RF-50")
@pytest.mark.rf("RF-52")
@pytest.mark.historia(65)
@pytest.mark.historia(284)
def test_propietario_y_carreras_son_opcionales_en_el_lote_y_se_devuelven(cliente):
    sin = _crear(cliente)
    con = _crear(cliente, propietario="Biblioteca FIE", carreras=["ing-mecanica", "ing-civil"])

    assert sin.status_code == 201
    assert (sin.json()["lote"]["propietario"], sin.json()["lote"]["carreras"]) == (None, [])
    assert con.status_code == 201
    lote = con.json()["lote"]
    assert (lote["propietario"], lote["carreras"]) == ("Biblioteca FIE", ["ing-civil", "ing-mecanica"])
    leido = cliente.get(f"/api/lotes/{lote['lote_id']}").json()["lote"]
    assert leido == lote | {"contexto_editable": True, "por_completar": []}


@pytest.mark.rf("RF-50")
@pytest.mark.historia(284)
@pytest.mark.parametrize(
    "carreras",
    [["ing-naval"], ["ing-mecanica"], ["ing-civil", "ing-civil"]],
    ids=["inexistente", "inactiva", "repetida"],
)
def test_una_carrera_fuera_de_la_lista_rechaza_el_lote(
    cliente, _conexion_del_dueno, carreras_restauradas, carreras
):
    with _conexion_del_dueno.transaction():
        _conexion_del_dueno.execute(
            "UPDATE acervo.carrera SET activo = false WHERE carrera_id = 'ing-mecanica'"
        )

    respuesta = _crear(cliente, carreras=carreras)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["campo"]) == (
        "/errores/carrera-desconocida",
        "carreras",
    )


def _subir(cliente, lote_id, nombre="acta.pdf", contenido=PDF):
    return cliente.post(
        "/api/piezas",
        data={} if lote_id is None else {"lote_id": lote_id},
        files={"archivo": (nombre, contenido, "application/octet-stream")},
    )


@pytest.mark.rf("RF-01")
@pytest.mark.historia(66)
@pytest.mark.historia(69)
@pytest.mark.historia(70)
def test_una_pieza_del_lote_nace_con_lo_heredado_y_editar_el_origen_lo_marca_persona(
    cliente, url_bd
):
    lote = _crear(cliente, evento="Colación de grados", lugar="Patio central").json()["lote"]

    subida = _subir(cliente, lote["lote_id"])

    assert subida.status_code == 201
    pieza_id = subida.json()["pieza_id"]
    assert (subida.json()["lote_id"], subida.json()["campos_heredados"]) == (
        lote["lote_id"],
        HEREDADOS,
    )
    esperar_estado(cliente, pieza_id, "Pendiente")
    apertura = abrir(cliente, pieza_id)
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    frontmatter = ficha["frontmatter"]
    assert {campo: frontmatter[campo] for campo in HEREDADOS} == {
        campo: lote[campo] for campo in HEREDADOS
    }
    assert (frontmatter["tipo"], frontmatter["lote"]) == (None, lote["lote_id"])
    assert ficha["contexto"] == "Evento: Colación de grados\nLugar: Patio central"
    assert ficha["campos_heredados"] == HEREDADOS
    assert ficha["procedencia"] == {
        "tipo": "vacio",
        "tipo_actividad": "vacio",
        "fecha_inicio": "lote",
        "fecha_fin": "lote",
        "precision_fecha": "lote",
        "origen": "lote",
        "propietario": "vacio",
        "resumen": "modelo",
    }
    # Un Lote sin propietario no lo hereda: la Pieza lo sigue pidiendo (DEC-126).
    assert "propietario" in ficha["faltantes"]

    editada = EDICION_COMPLETA | {
        "fecha_inicio": "1970-01-01",
        "precision_fecha": "decada",
        "origen": "Archivo de Rectorado",
        "resumen": RESUMEN_FIJO,
        "contexto": ficha["contexto"],
        "medicion_id": apertura["medicion_id"],
    }
    assert cliente.put(f"/api/piezas/{pieza_id}/ficha", json=editada).status_code == 200
    procedencia = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["procedencia"]
    assert (procedencia["origen"], procedencia["fecha_inicio"], procedencia["tipo"]) == (
        "persona",
        "lote",
        "persona",
    )
    # El origen heredado no lo propuso el modelo: editarlo no es corregirlo (M1, spec ~1942).
    assert mediciones(cliente)[0]["campos_modificados"] == ["tipo", "tipo_actividad", "propietario"]
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


@pytest.mark.rf("RF-50")
@pytest.mark.rf("RF-52")
@pytest.mark.historia(69)
@pytest.mark.historia(71)
@pytest.mark.historia(283)
@pytest.mark.historia(284)
@pytest.mark.historia(287)
def test_propietario_y_carreras_se_heredan_y_corregirlos_en_una_pieza_no_toca_el_lote(
    cliente, url_bd
):
    lote = _crear(
        cliente, propietario="Biblioteca FIE", carreras=["ing-mecanica", "ing-civil"]
    ).json()["lote"]
    heredados = [*HEREDADOS, "propietario", "carreras"]

    subidas = [_subir(cliente, lote["lote_id"], f"acta-{i}.pdf", PDF + str(i).encode()) for i in (1, 2)]

    assert [subida.json()["campos_heredados"] for subida in subidas] == [heredados, heredados]
    una, otra = (subida.json()["pieza_id"] for subida in subidas)
    for pieza_id in (una, otra):
        esperar_estado(cliente, pieza_id, "Pendiente")
    ficha = cliente.get(f"/api/piezas/{una}/ficha").json()
    frontmatter = ficha["frontmatter"]
    assert (frontmatter["propietario"], frontmatter["carreras"]) == (
        "Biblioteca FIE",
        ["ing-civil", "ing-mecanica"],
    )
    # El tipo de Pieza y el tipo de actividad no se heredan nunca (DEC-126).
    assert (frontmatter["tipo"], frontmatter["tipo_actividad"]) == (None, None)
    assert ficha["campos_heredados"] == heredados
    assert ficha["procedencia"]["propietario"] == "lote"
    assert "propietario" not in ficha["faltantes"]
    assert cliente.get(f"/api/piezas/{una}").json()["carreras"] == ["ing-civil", "ing-mecanica"]

    def validar(pieza_id, **cambios):
        apertura = abrir(cliente, pieza_id)
        editada = EDICION_COMPLETA | {
            "fecha_inicio": lote["fecha_inicio"],
            "precision_fecha": lote["precision_fecha"],
            "origen": lote["origen"],
            "propietario": lote["propietario"],
            "carreras": lote["carreras"],
            "resumen": RESUMEN_FIJO,
            "contexto": ficha["contexto"],
            "medicion_id": apertura["medicion_id"],
        } | cambios
        assert cliente.put(f"/api/piezas/{pieza_id}/ficha", json=editada).status_code == 200
        return mediciones(cliente)[0]["campos_modificados"]

    corregidos = validar(una, propietario="Donación Pérez", carreras=["ing-civil"])

    vigente = cliente.get(f"/api/piezas/{una}/ficha").json()
    assert (vigente["frontmatter"]["propietario"], vigente["frontmatter"]["carreras"]) == (
        "Donación Pérez",
        ["ing-civil"],
    )
    assert vigente["procedencia"]["propietario"] == "persona"
    # Lo heredado no lo propuso el modelo: corregirlo no cuenta en M1 (spec ~1942).
    assert corregidos == ["tipo", "tipo_actividad"]
    leido = cliente.get(f"/api/lotes/{lote['lote_id']}").json()["lote"]
    assert leido == lote | {"contexto_editable": False, "por_completar": []}
    de_la_otra = cliente.get(f"/api/piezas/{otra}/ficha").json()["frontmatter"]
    assert (de_la_otra["propietario"], de_la_otra["carreras"]) == (
        "Biblioteca FIE",
        ["ing-civil", "ing-mecanica"],
    )
    # Las Carreras de la v1 sin cambios tampoco son una corrección (024).
    assert validar(otra) == ["tipo", "tipo_actividad"]
    with psycopg.connect(url_bd) as conexion:
        assert divergencias(conexion) == []


@pytest.mark.rf("RF-08")
@pytest.mark.historia(78)
def test_el_contexto_llega_a_resumir_y_no_a_describir_imagen_y_el_trabajo_lo_congela(
    cliente, capa_modelos, url_bd
):
    lote = _crear(cliente, evento="Colación de grados", lugar="Patio central").json()["lote"]
    capa_modelos.demorar()
    pieza_id = _subir(cliente, lote["lote_id"], "foto.jpg", IMAGEN).json()["pieza_id"]
    # Lo que cambie el Lote después de encolar no alcanza al trabajo: tiene su copia.
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute("UPDATE acervo.lote SET evento = 'Otro evento'")
    capa_modelos.liberar()
    esperar_estado(cliente, pieza_id, "Pendiente")

    por_capacidad = {i.capacidad: i for i in capa_modelos.invocaciones}
    assert [b.etiqueta for b in por_capacidad[Capacidad.DESCRIBIR_IMAGEN].bloques] == ["imagen"]
    resumir = por_capacidad[Capacidad.RESUMIR]
    assert [b.etiqueta for b in resumir.bloques] == ["cuerpo_ficha", "contexto_lote"]
    contexto = resumir.bloques[1].contenido
    assert "Origen: Archivo fotográfico SEU" in contexto
    assert "Evento: Colación de grados" in contexto
    with psycopg.connect(url_bd) as conexion:
        (congelado,) = conexion.execute(
            "SELECT contexto_lote FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchone()
        (configuracion,) = conexion.execute(
            "SELECT configuracion_efectiva FROM acervo.ficha WHERE pieza_id = %s AND version = 1",
            (pieza_id,),
        ).fetchone()
    assert congelado == {
        campo: lote[campo] for campo in (*HEREDADOS, "evento", "lugar")
    }
    # La v1 registra la instrucción que de verdad se usó (014).
    assert resumir.plantilla == configuracion["por_capacidad"]["resumir"]["instruccion_id"]


@pytest.mark.rf("RF-08")
def test_el_reintento_de_una_pieza_del_lote_tambien_congela_su_contexto(
    cliente, capa_modelos, url_bd
):
    lote = _crear(cliente, evento="Colación de grados").json()["lote"]
    capa_modelos.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
    pieza_id = _subir(cliente, lote["lote_id"]).json()["pieza_id"]
    esperar_error_procesamiento(cliente, pieza_id)
    capa_modelos.arreglar()

    assert cliente.post(f"/api/piezas/{pieza_id}/reproceso").status_code == 202
    esperar_estado(cliente, pieza_id, "Pendiente")

    (resumir,) = [i for i in capa_modelos.invocaciones if i.capacidad is Capacidad.RESUMIR]
    assert [b.etiqueta for b in resumir.bloques] == ["cuerpo_ficha", "contexto_lote"]
    with psycopg.connect(url_bd) as conexion:
        congelados = conexion.execute(
            "SELECT contexto_lote FROM acervo.trabajo_procesamiento WHERE pieza_id = %s",
            (pieza_id,),
        ).fetchall()
    esperado = {campo: lote[campo] for campo in (*HEREDADOS, "evento", "lugar")}
    assert [c for (c,) in congelados] == [esperado, esperado]


@pytest.mark.rf("RF-08")
def test_una_pieza_del_lote_cuyo_texto_trae_su_propio_contexto_se_procesa(cliente):
    """El `## Contexto` del archivo queda escapado en el cuerpo (spec: el cuerpo no crea esa sección, 142):
    el Contexto es solo el del Lote."""
    lote = _crear(cliente, evento="Colación de grados").json()["lote"]
    texto = b"Acta de la colacion.\n\n## Contexto\n\nNota del archivista.\n"

    pieza_id = _subir(cliente, lote["lote_id"], "acta.txt", texto).json()["pieza_id"]

    esperar_estado(cliente, pieza_id, "Pendiente")
    ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
    assert (ficha["cuerpo"], ficha["contexto"]) == (
        "Acta de la colacion.\n\n\\## Contexto\n\nNota del archivista.",
        "Evento: Colación de grados",
    )


@pytest.mark.rf("RF-08")
def test_una_pieza_suelta_no_lleva_contexto(cliente, capa_modelos):
    entrar(cliente, MARIA)
    subida = _subir(cliente, None)

    assert (subida.status_code, subida.json()["lote_id"], subida.json()["campos_heredados"]) == (
        201,
        None,
        [],
    )
    esperar_estado(cliente, subida.json()["pieza_id"], "Pendiente")
    resumir = [i for i in capa_modelos.invocaciones if i.capacidad is Capacidad.RESUMIR]
    assert [b.etiqueta for b in resumir[0].bloques] == ["cuerpo_ficha"]


def _llenar(url_bd, lote_id, cuantas):
    """Piezas ya cargadas en el Lote, por SQL: subir 99 por HTTP no prueba nada más."""
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute(
            "INSERT INTO acervo.pieza (lote_id, clase_original, tipo_mime, nombre_archivo,"
            " tamano_bytes, creada_por) SELECT %s, 'documento', 'application/pdf',"
            " 'previa-' || n || '.pdf', 1, %s FROM generate_series(1, %s) AS n",
            (lote_id, MARIA, cuantas),
        )


def _piezas_del_lote(url_bd, lote_id):
    with psycopg.connect(url_bd) as conexion:
        (total,) = conexion.execute(
            "SELECT count(*) FROM acervo.pieza WHERE lote_id = %s", (lote_id,)
        ).fetchone()
    return total


@pytest.mark.historia(75)
def test_la_pieza_101_responde_lote_lleno_y_el_tope_es_fijo(cliente, url_bd, monkeypatch):
    # R-27: el tope no se cambia por entorno (la variable del spec viejo ya no existe).
    monkeypatch.setenv("HV_LOTE_MAX_PIEZAS", "1000")
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    _llenar(url_bd, lote_id, 100)

    respuesta = _subir(cliente, lote_id)

    assert respuesta.status_code == 422
    assert (respuesta.json()["type"], respuesta.json()["tope"]) == ("/errores/lote-lleno", 100)
    assert _piezas_del_lote(url_bd, lote_id) == 100


@pytest.mark.historia(75)
def test_una_pieza_retirada_no_cuenta_contra_el_tope(cliente, url_bd):
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    _llenar(url_bd, lote_id, 100)
    with psycopg.connect(url_bd, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
            " borrada_motivo = 'Subida por error' WHERE pieza_id ="
            " (SELECT pieza_id FROM acervo.pieza WHERE lote_id = %s LIMIT 1)",
            (MARIA, lote_id),
        )

    assert _subir(cliente, lote_id).status_code == 201


@pytest.mark.historia(290)
@pytest.mark.parametrize("motivo", ["lote-no-encontrado", "permiso-insuficiente", "lote-lleno"])
def test_un_rechazo_del_lote_deja_archivo_rechazo_con_su_motivo(
    cliente, url_bd, monkeypatch, motivo
):
    """DEC-106: todo rechazo escribe `archivo.rechazo` con el motivo."""
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    if motivo == "lote-no-encontrado":
        lote_id = str(uuid4())
    elif motivo == "permiso-insuficiente":
        # Hoy todo rol con CARGAR carga en un Lote ajeno (R-06): se le saca ese permiso.
        monkeypatch.setattr(
            "historia_viva.api.piezas.puede_cargar_en_lote_ajeno", lambda rol: False
        )
        entrar(cliente, JORGE)
    else:
        _llenar(url_bd, lote_id, 100)

    respuesta = _subir(cliente, lote_id)

    assert respuesta.json()["type"] == f"/errores/{motivo}"
    with psycopg.connect(url_bd) as conexion:
        rechazos = conexion.execute(
            "SELECT motivo, despues->>'lote_id' FROM acervo.auditoria"
            " WHERE accion = 'archivo.rechazo'"
        ).fetchall()
    assert rechazos == [(motivo, lote_id)]


@pytest.mark.historia(75)
def test_con_99_de_dos_cargas_simultaneas_entra_exactamente_una(cliente, url_bd, monkeypatch):
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    _llenar(url_bd, lote_id, 99)
    con_lugar = Lotes.con_lugar
    las_dos_llegaron = threading.Barrier(2)

    def a_la_vez(self, tx, lote_id, **datos):
        las_dos_llegaron.wait(timeout=10)
        return con_lugar(self, tx, lote_id, **datos)

    monkeypatch.setattr(Lotes, "con_lugar", a_la_vez)
    with ThreadPoolExecutor(2) as hilos:
        respuestas = list(
            hilos.map(
                lambda i: _subir(cliente, lote_id, f"acta-{i}.pdf", PDF + str(i).encode()),
                range(2),
            )
        )

    assert sorted(r.status_code for r in respuestas) == [201, 422]
    assert _piezas_del_lote(url_bd, lote_id) == 100


@pytest.mark.historia(76)
def test_cargan_en_un_lote_ajeno_solo_el_rgc_y_el_coordinador_y_leerlo_es_abierto(cliente):
    lote_id = _crear(cliente, persona=MARIA).json()["lote"]["lote_id"]

    respuestas = {}
    for persona in (JORGE, ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA):
        entrar(cliente, persona)
        subida = _subir(cliente, lote_id, f"acta-{persona}.pdf", PDF + persona.encode())
        leido = cliente.get(f"/api/lotes/{lote_id}")
        respuestas[persona] = (subida.status_code, leido.status_code)

    assert respuestas == {
        JORGE: (201, 200),
        ADMINISTRADOR_TECNICO: (403, 200),
        DIRECTORA_DE_CARRERA: (403, 200),
    }


@pytest.mark.historia(76)
def test_sin_permiso_de_lote_ajeno_carga_solo_quien_lo_creo(url_bd):
    """Hoy los dos roles con CARGAR cargan en un Lote ajeno (R-06): la regla del creador se
    prueba en `con_lugar`, por si un rol nuevo carga sin ese permiso."""
    lotes = Lotes(
        fichas=ModuloFichasPostgres(bd=None),  # type: ignore[arg-type]
        cola=None,  # type: ignore[arg-type]
        indexacion=None,  # type: ignore[arg-type]
        auditoria=None,  # type: ignore[arg-type]
    )
    contexto = ContextoDeLote(
        origen="Archivo",
        fecha_inicio=date(1975, 1, 1),
        fecha_fin=date(1975, 12, 31),
        precision_fecha=PrecisionFecha.ANIO,
    )
    with (
        psycopg.connect(url_bd, row_factory=dict_row) as tx,
        tx.transaction(force_rollback=True),
    ):
        lote = lotes.crear(tx, nombre="Lote", contexto=contexto, creado_por=UUID(MARIA))

        with pytest.raises(PermisoInsuficiente):
            lotes.con_lugar(tx, lote.lote_id, cargante=UUID(JORGE), puede_en_lote_ajeno=False)
        assert lotes.con_lugar(tx, lote.lote_id, cargante=UUID(MARIA), puede_en_lote_ajeno=False)
        assert lotes.con_lugar(tx, lote.lote_id, cargante=UUID(JORGE), puede_en_lote_ajeno=True)


@pytest.mark.historia(76)
@pytest.mark.parametrize("rol", list(Rol))
def test_cargan_en_un_lote_ajeno_el_rgc_y_el_coordinador(rol):
    assert puede_cargar_en_lote_ajeno(rol) is (rol in {Rol.RGC, Rol.COORDINADOR})


@pytest.mark.rf("RF-02")
def test_un_lote_id_despues_del_archivo_se_rechaza_y_no_deja_pieza(cliente):
    """El original se guarda mientras llega: un `lote_id` que viene después no puede quedar
    ignorado y dejar suelta una Pieza del Lote."""
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    frontera = "frontera-de-prueba"
    cuerpo = (
        (
            f"--{frontera}\r\nContent-Disposition: form-data; name=\"archivo\";"
            ' filename="acta.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
        ).encode()
        + PDF
        + (
            f"\r\n--{frontera}\r\nContent-Disposition: form-data; name=\"lote_id\"\r\n\r\n"
            f"{lote_id}\r\n--{frontera}--\r\n"
        ).encode()
    )

    respuesta = cliente.post(
        "/api/piezas",
        content=cuerpo,
        headers={"Content-Type": f"multipart/form-data; boundary={frontera}"},
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["campo"] == "lote_id"
    assert cliente.get("/api/piezas").json()["piezas"] == []


# ── Carga múltiple en el Lote (083) ──────────────────────────────────────────────


def _multipart(lote_id, nombre="acta.pdf", contenido=PDF, *, cerrado=True):
    frontera = "frontera-de-prueba"
    cuerpo = (
        f'--{frontera}\r\nContent-Disposition: form-data; name="lote_id"\r\n\r\n{lote_id}\r\n'
        f'--{frontera}\r\nContent-Disposition: form-data; name="archivo";'
        f' filename="{nombre}"\r\nContent-Type: application/pdf\r\n\r\n'
    ).encode() + contenido
    if cerrado:
        cuerpo += f"\r\n--{frontera}--\r\n".encode()
    return cuerpo, {"Content-Type": f"multipart/form-data; boundary={frontera}"}


def _subir_sobre_el_tope(cliente, lote_id):
    """Un PDF que declara 41 MiB: el tope de su clase (40 MiB) lo corta por `Content-Length`."""
    cuerpo, cabeceras = _multipart(lote_id, "enorme.pdf")
    return cliente.post(
        "/api/piezas",
        content=cuerpo,
        headers=cabeceras | {"Content-Length": str(41 * 1024 * 1024)},
    )


@pytest.mark.rf("RF-04")
@pytest.mark.historia(67)
@pytest.mark.historia(68)
@pytest.mark.historia(69)
@pytest.mark.historia(79)
def test_r2_cincuenta_archivos_de_a_dos_con_un_rechazo_uno_sobre_el_tope_y_un_duplicado(
    cliente, url_bd
):
    """Recorrido R2: cada archivo va en su pedido, de a dos; lo que no entra no frena al resto."""
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    subidas = {f"foto-{i:04}.pdf": PDF + str(i).encode() for i in range(47)}

    def subir(nombre):
        if nombre == "nota.exe":
            return _subir(cliente, lote_id, nombre, b"MZ\x90\x00" + b"\x00" * 60)
        if nombre == "enorme.pdf":
            return _subir_sobre_el_tope(cliente, lote_id)
        if nombre == "repetida.pdf":
            return _subir(cliente, lote_id, nombre, subidas["foto-0003.pdf"])
        return _subir(cliente, lote_id, nombre, subidas[nombre])

    # El duplicado va al final: la previa ya subió.
    orden = list(subidas)
    nombres = [*orden[:20], "nota.exe", *orden[20:40], "enorme.pdf", *orden[40:], "repetida.pdf"]
    with ThreadPoolExecutor(2) as hilos:
        respuestas = dict(zip(nombres, hilos.map(subir, nombres), strict=True))

    assert respuestas["nota.exe"].status_code == 415
    assert respuestas["enorme.pdf"].status_code == 413
    repetida = respuestas["repetida.pdf"]
    previa = respuestas["foto-0003.pdf"].json()
    assert repetida.status_code == 200
    assert (repetida.json()["pieza_id"], repetida.json()["duplicada"]) == (previa["pieza_id"], True)
    nuevas = [respuestas[nombre] for nombre in subidas]
    assert {r.status_code for r in nuevas} == {201}
    assert {r.json()["duplicada"] for r in nuevas} == {False}
    assert _piezas_del_lote(url_bd, lote_id) == 47
    for respuesta in nuevas:
        pieza_id = respuesta.json()["pieza_id"]
        esperar_estado(cliente, pieza_id, "Pendiente")
        ficha = cliente.get(f"/api/piezas/{pieza_id}/ficha").json()
        assert ficha["campos_heredados"] == HEREDADOS


@pytest.mark.historia(79)
def test_el_mismo_archivo_en_el_mismo_lote_devuelve_la_pieza_existente_y_no_cuenta(
    cliente, url_bd
):
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    primera = _subir(cliente, lote_id).json()

    otra_vez = _subir(cliente, lote_id, "acta (copia).pdf")

    assert otra_vez.status_code == 200
    cuerpo = otra_vez.json()
    assert {k: cuerpo[k] for k in ("pieza_id", "lote_id", "nombre_archivo", "duplicada")} == {
        "pieza_id": primera["pieza_id"],
        "lote_id": lote_id,
        "nombre_archivo": "acta.pdf",
        "duplicada": True,
    }
    assert _piezas_del_lote(url_bd, lote_id) == 1
    # Un reintento no es un rechazo: no deja `archivo.rechazo`.
    with psycopg.connect(url_bd) as conexion:
        rechazos = conexion.execute(
            "SELECT count(*) FROM acervo.auditoria WHERE accion = 'archivo.rechazo'"
        ).fetchone()
    assert rechazos == (0,)


@pytest.mark.historia(79)
def test_el_reintento_en_un_lote_lleno_devuelve_la_pieza_que_ya_estaba(cliente, url_bd):
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    primera = _subir(cliente, lote_id).json()
    _llenar(url_bd, lote_id, 99)

    otra_vez = _subir(cliente, lote_id)

    assert (otra_vez.status_code, otra_vez.json()["pieza_id"]) == (200, primera["pieza_id"])


@pytest.mark.historia(80)
@pytest.mark.parametrize("donde", ["otro-lote", "suelta"])
def test_el_mismo_archivo_en_otro_lote_o_suelto_es_409_con_la_pieza_y_su_lote(
    cliente, url_bd, donde
):
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    previa = _subir(cliente, lote_id).json()
    destino = _crear(cliente).json()["lote"]["lote_id"] if donde == "otro-lote" else None

    respuesta = _subir(cliente, destino)

    assert respuesta.status_code == 409
    problema = respuesta.json()
    assert problema["type"] == "/errores/pieza-duplicada"
    assert (problema["pieza_id"], problema["lote_id"]) == (previa["pieza_id"], lote_id)
    if destino is not None:
        assert _piezas_del_lote(url_bd, destino) == 0


@pytest.mark.historia(79)
def test_dos_subidas_simultaneas_del_mismo_archivo_al_mismo_lote_dejan_una_pieza(
    cliente, url_bd, monkeypatch
):
    """La segunda choca con `pieza_huella_unica` y se traduce igual que un reintento (DEC-106)."""
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    buscar = ModuloFichasPostgres.buscar_por_huella
    las_dos_buscaron = threading.Barrier(2)

    def buscar_a_la_vez(self, tx, sha256):
        previa = buscar(self, tx, sha256)
        if previa is None:
            las_dos_buscaron.wait(timeout=10)
        return previa

    monkeypatch.setattr(ModuloFichasPostgres, "buscar_por_huella", buscar_a_la_vez)
    with ThreadPoolExecutor(2) as hilos:
        repetida, creada = sorted(
            hilos.map(lambda _: _subir(cliente, lote_id), range(2)),
            key=lambda respuesta: respuesta.status_code,
        )

    assert (repetida.status_code, creada.status_code) == (200, 201)
    assert repetida.json()["pieza_id"] == creada.json()["pieza_id"]
    assert _piezas_del_lote(url_bd, lote_id) == 1


@pytest.mark.historia(67)
def test_cancelar_una_subida_a_mitad_de_camino_no_deja_pieza_ni_original(
    cliente, url_bd, directorio_originales
):
    """«Cancelar lo que falta» corta el pedido: el servidor ve un multipart sin terminar."""
    lote_id = _crear(cliente).json()["lote"]["lote_id"]
    cuerpo, cabeceras = _multipart(lote_id, cerrado=False)

    respuesta = cliente.post("/api/piezas", content=cuerpo, headers=cabeceras)

    assert respuesta.status_code == 422
    assert _piezas_del_lote(url_bd, lote_id) == 0
    assert [r for r in directorio_originales.rglob("*") if r.is_file()] == []


@pytest.mark.historia(77)
@pytest.mark.historia(84)
def test_con_el_procesamiento_frenado_las_piezas_del_lote_se_aceptan_recibidas_y_avisan(
    cliente, capa_modelos
):
    with cliente.app.state.bd.transaccion() as tx:
        cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Corrida del banco")
    lote_id = _crear(cliente).json()["lote"]["lote_id"]

    respuestas = [
        _subir(cliente, lote_id, f"acta-{i}.pdf", PDF + str(i).encode()) for i in range(3)
    ]

    assert [r.status_code for r in respuestas] == [201] * 3
    assert {(r.json()["estado"], r.json()["procesamiento_pausado"]) for r in respuestas} == {
        ("Recibida", True)
    }
    # Respondió sin pasar por el modelo: con el freno puesto nadie lo llamó.
    assert capa_modelos.invocaciones == []
