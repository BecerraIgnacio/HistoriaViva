"""El Informe de acreditación en Borrador, sin modelo (066; spec, módulo 33 `informes`, DEC-131).

El armado se prueba sobre el módulo, en una transacción que se revierte (`tx`): las Piezas se
crean y se firman con el Módulo de Fichas, sin trabajador. Lo que mira la persona (permisos,
códigos, auditoría y cero llamadas a la capa) se prueba por HTTP, con Piezas confirmadas.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from docx import Document
from openpyxl import load_workbook
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from aplicacion import entrar
from historia_viva import banco
from historia_viva.auditoria import Accion, ObjetoRef, RegistroDeAuditoria
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.cuerpo import segmentos
from historia_viva.dominio.errores import ConsultaInvalida, PermisoInsuficiente
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.permisos import Rol
from historia_viva.dominio.tipos import EdicionFicha, MetodoAutenticacion, ViaDeIngreso
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.identidad import Principal
from historia_viva.indexacion import LineaDeContexto, fragmentar
from historia_viva.informes import (
    AprobadorRepetido,
    InformeDesactualizado,
    InformeNoAprobable,
    InformeNoAprobado,
    InformeNoEncontrado,
    Informes,
    RolYaAprobo,
)
from historia_viva.originales import OriginalTemporal
from validacion import ADMINISTRADOR_TECNICO, DIRECTORA_DE_CARRERA, JORGE, MARIA

pytestmark = pytest.mark.rf("RF-49")

URL_PUBLICA = "https://historiaviva.fie.undef.edu.ar"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _principal(persona: str, rol: Rol) -> Principal:
    return Principal(
        usuario_id=UUID(persona), nombre="X", email="x@fie", rol=rol,
        metodo=MetodoAutenticacion.DEV, vence_el=None,  # type: ignore[arg-type]
    )


COORDINADOR = _principal(JORGE, Rol.COORDINADOR)
RGC = _principal(MARIA, Rol.RGC)
DIRECTORA = _principal(DIRECTORA_DE_CARRERA, Rol.DIRECTOR_CARRERA)

Crear = Callable[..., UUID]


def _crear(tx: Any, fichas: ModuloFichasPostgres) -> Crear:
    """Una Pieza en el estado pedido, con su Fecha. Validada: firmada por María con el tipo de
    actividad y las Carreras pedidas. Las demás tienen la Fecha que les habría dado su Lote."""

    def crear(
        estado: Estado = Estado.VALIDADA,
        *,
        tipo_actividad: str = "Transferencia",
        fecha: date = date(2019, 5, 3),
        precision: PrecisionFecha = PrecisionFecha.DIA,
        carreras: tuple[str, ...] = (),
        tipo: str = "acta",
        resumen: str | None = None,
        nombre: str = "pieza.pdf",
    ) -> UUID:
        pieza_id = uuid4()
        planilla = nombre.endswith(".xlsx")
        fichas.crear_pieza(
            tx,
            pieza_id=pieza_id,
            nombre_archivo=nombre,
            original=OriginalTemporal(
                ruta=Path(nombre), tamano_bytes=1, sha256=uuid4().hex * 2,
                tipo_mime=XLSX if planilla else "application/pdf",
                clase=ClaseDeOriginal.PLANILLA if planilla else ClaseDeOriginal.DOCUMENTO,
            ),
            archivo_original=f"local://originales/{pieza_id}",
            creada_por=UUID(MARIA),
            via_ingreso=ViaDeIngreso.ARCHIVO,
        )
        # La Fecha que hereda una Pieza de su Lote: la Recibida ya la tiene.
        tx.execute(
            "UPDATE acervo.pieza SET fecha_inicio = %s, fecha_fin = %s, precision_fecha = 'dia'"
            " WHERE pieza_id = %s",
            (fecha, fecha, pieza_id),
        )
        if estado is Estado.RECIBIDA:
            return pieza_id
        fichas.completar_a_mano(tx, pieza_id)
        if estado is Estado.PENDIENTE:
            return pieza_id
        fichas.guardar_edicion(
            tx,
            pieza_id=pieza_id,
            version_base=1,
            edicion=EdicionFicha(
                tipo=tipo,
                tipo_actividad=tipo_actividad,
                fecha_inicio=fecha,
                precision_fecha=precision,
                origen=None if estado is Estado.INCOMPLETA else "Archivo de la SEU",
                propietario="Secretaría de Extensión Universitaria",
                carreras=carreras,
                resumen=resumen or f"{tipo_actividad} del {fecha:%d/%m/%Y}.",
                cuerpo="Texto.",
                contexto="",
            ),
            editada_por=UUID(MARIA),
            metodo=MetodoAutenticacion.DEV,
        )
        return pieza_id

    return crear


@pytest.fixture
def fichas() -> ModuloFichasPostgres:
    return ModuloFichasPostgres(bd=None)  # type: ignore[arg-type]  # solo métodos con `tx`


@pytest.fixture
def informes(fichas) -> Informes:
    return Informes(
        fichas=fichas, auditoria=RegistroDeAuditoria(bd=None),  # type: ignore[arg-type]
        url_publica=URL_PUBLICA,
        integridad=partial(
            banco.integridad, fichas=fichas,
            configuracion=ConfiguracionDeModelos(bd=None, validar=None),  # type: ignore[arg-type]
        ),
    )


def _indexar(
    tx: Any, fichas: ModuloFichasPostgres, pieza_id: UUID, *, digest: str = "0123456789ab"
) -> None:
    """Los fragmentos que dejaría `indexacion` con el modelo de `embeber`, escritos por SQL con
    la forma de `indice`: sin trabajador ni LlamaIndex, que tarda en cargar (ticket 130)."""
    ficha = fichas.leer_para_indexar(tx, pieza_id)
    assert ficha is not None
    [modelo] = tx.execute(
        "SELECT modelo FROM acervo.configuracion_capacidad WHERE capacidad = 'embeber'"
    ).fetchone().values()
    for f in fragmentar(ficha.ficha_id, segmentos(ficha.cuerpo), LineaDeContexto("acta", "x")):
        metadata = {
            "pieza_id": str(pieza_id), "ficha_id": str(ficha.ficha_id), "tipo": ficha.tipo,
            "tipo_actividad": ficha.tipo_actividad, "carreras": list(ficha.carreras),
            "fecha_inicio": ficha.fecha_inicio.isoformat(),
            "fecha_fin": ficha.fecha_fin.isoformat(),
            "clase_original": ficha.clase_original.value, "ubicacion": dict(f.ubicacion),
            "inicio": f.inicio, "fin": f.fin, "modelo_embedding": f"{modelo}@{digest}",
            "via_ingreso": ficha.via_ingreso.value,
        }
        tx.execute(
            "INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)"
            " VALUES (%s, %s, %s, %s::vector)",
            (str(f.fragmento_id), f.texto, Jsonb(metadata), str([1.0] + [0.0] * 1023)),
        )


def _corromper(tx: Any, pieza_id: UUID) -> None:
    """Edita el texto de sus fragmentos sin reindexar: falla P3 (contenido íntegro)."""
    tx.execute(
        "UPDATE acervo.data_fragmento SET text = 'otro texto' WHERE metadata_->>'pieza_id' = %s",
        (str(pieza_id),),
    )


@pytest.fixture
def acervo(tx) -> Iterator[Any]:
    """`tx` con las filas como diccionarios, como las entrega `bd`."""
    tx.row_factory = dict_row
    yield tx
    tx.row_factory = tuple_row


@pytest.fixture
def crear(acervo, fichas) -> Crear:
    return _crear(acervo, fichas)


def _generar(informes: Informes, tx: Any, **pedido: Any):
    return informes.generar(
        tx,
        desde=pedido.get("desde", date(2018, 1, 1)),
        hasta=pedido.get("hasta", date(2022, 12, 31)),
        carreras=pedido.get("carreras", ()),
        principal=pedido.get("principal", COORDINADOR),
        reemplaza=pedido.get("reemplaza"),
    )


def _ids(vista) -> list[tuple[str, list[UUID]]]:
    return [(g.tipo_actividad, [p.pieza_id for p in g.piezas]) for g in vista.grupos]


@pytest.mark.historia(269, 270)
def test_tres_tipos_de_actividad_dan_tres_grupos_en_el_orden_del_catalogo_y_por_fecha(
    acervo, crear, informes
):
    evento = crear(tipo_actividad="Evento Académico", fecha=date(2018, 3, 1))
    transferencia_2020 = crear(tipo_actividad="Transferencia", fecha=date(2020, 6, 1))
    transferencia_2019 = crear(tipo_actividad="Transferencia", fecha=date(2019, 6, 1))
    consultoria = crear(tipo_actividad="Consultoría", fecha=date(2021, 1, 1))
    # El mismo día: desempata el `pieza_id`.
    gemelas = sorted(
        crear(tipo_actividad="Consultoría", fecha=date(2021, 9, 9)) for _ in range(2)
    )
    # Ni «No es una actividad», ni una no Validada, ni una retirada, ni una fuera del período.
    crear(tipo_actividad="No es una actividad")
    crear(Estado.PENDIENTE)
    retirada = crear()
    acervo.execute(
        "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
        " borrada_motivo = 'Duplicada por error' WHERE pieza_id = %s",
        (MARIA, retirada),
    )
    crear(fecha=date(2017, 12, 31))

    vista = _generar(informes, acervo)

    assert _ids(vista) == [
        ("Transferencia", [transferencia_2019, transferencia_2020]),
        ("Consultoría", [consultoria, *gemelas]),
        ("Evento Académico", [evento]),
    ]
    assert vista.estado == "borrador"
    primera = vista.grupos[0].piezas[0]
    assert (primera.resumen, primera.fecha_inicio, primera.tipo) == (
        "Transferencia del 01/06/2019.", date(2019, 6, 1), "acta",
    )
    assert primera.cita == {
        "id": f"pza_{transferencia_2019}",
        "enlace": f"{URL_PUBLICA}/api/piezas/{transferencia_2019}/original",
    }


@pytest.mark.historia(269)
def test_el_periodo_toma_las_fechas_que_lo_solapan_y_el_filtro_las_carreras_pedidas(
    acervo, crear, informes
):
    # La década de 2010 empieza antes del período y lo solapa: entra.
    decada = crear(
        fecha=date(2015, 1, 1), precision=PrecisionFecha.DECADA, carreras=("ing-civil",)
    )
    civil = crear(fecha=date(2018, 1, 1), carreras=("ing-civil",))
    ambas = crear(fecha=date(2022, 12, 31), carreras=("ing-civil", "ing-informatica"))
    crear(fecha=date(2020, 1, 1), carreras=("ing-mecanica",))
    crear(fecha=date(2020, 1, 1))

    vista = _generar(informes, acervo, carreras=("ing-civil",))

    assert _ids(vista) == [("Transferencia", [decada, civil, ambas])]
    assert vista.carreras == ("ing-civil",)


@pytest.mark.historia(243)
@pytest.mark.rf("RF-42")
def test_una_pieza_no_publicable_sigue_en_el_informe(acervo, crear, informes):
    publicable = crear(fecha=date(2019, 5, 3))
    no_publicable = crear(fecha=date(2020, 5, 3))
    acervo.execute(
        "UPDATE acervo.pieza SET publicable = false WHERE pieza_id = %s", (no_publicable,)
    )

    assert _ids(_generar(informes, acervo)) == [("Transferencia", [publicable, no_publicable])]


@pytest.mark.historia(270)
def test_el_mismo_periodo_y_las_mismas_piezas_dan_el_mismo_contenido_y_la_misma_huella(
    acervo, crear, informes
):
    for fecha in (date(2019, 1, 1), date(2020, 1, 1), date(2021, 1, 1)):
        crear(fecha=fecha, tipo_actividad="Consultoría")
        crear(fecha=fecha)

    primero = _generar(informes, acervo)
    segundo = _generar(informes, acervo)

    assert primero.informe_id != segundo.informe_id
    assert (primero.grupos, primero.huella) == (segundo.grupos, segundo.huella)
    assert informes.obtener(acervo, primero.informe_id, principal=RGC).grupos == primero.grupos


@pytest.mark.historia(271, 272)
def test_los_faltantes_se_calculan_al_leer_y_lo_congelado_no_cambia(
    acervo, crear, informes, fichas
):
    planilla = crear(tipo="planilla", nombre="proceso-3.xlsx")
    recibida = crear(Estado.RECIBIDA, nombre="c-recibida.pdf")
    pendiente = crear(Estado.PENDIENTE, nombre="b-pendiente.pdf")
    incompleta = crear(Estado.INCOMPLETA, nombre="a-incompleta.pdf")
    crear(Estado.RECIBIDA, fecha=date(2010, 1, 1))  # fuera del período

    generado = _generar(informes, acervo)
    # La Incompleta se completa y queda Validada después de generar.
    fichas.guardar_edicion(
        acervo,
        pieza_id=incompleta,
        version_base=2,
        edicion=EdicionFicha(
            tipo="acta", tipo_actividad="Transferencia", fecha_inicio=date(2019, 5, 3),
            precision_fecha=PrecisionFecha.DIA, origen="Archivo de la SEU",
            propietario="SEU", resumen="Completa.", cuerpo="Texto.", contexto="",
        ),
        editada_por=UUID(MARIA),
        metodo=MetodoAutenticacion.DEV,
    )
    releido = informes.obtener(acervo, generado.informe_id, principal=COORDINADOR)

    assert _ids(generado) == [("Transferencia", [planilla])]
    assert [(f.pieza_id, f.estado, f.nombre_archivo) for f in generado.faltantes] == [
        (incompleta, "Incompleta", "a-incompleta.pdf"),
        (pendiente, "Pendiente", "b-pendiente.pdf"),
        (recibida, "Recibida", "c-recibida.pdf"),
    ]
    assert generado.faltantes[0].falta_para_validar == ("origen",)
    assert [f.pieza_id for f in releido.faltantes] == [pendiente, recibida]
    assert releido.grupos == generado.grupos


@pytest.mark.historia(269)
def test_un_periodo_sin_piezas_validadas_no_crea_el_borrador(acervo, crear, informes):
    crear(Estado.PENDIENTE)
    crear(tipo_actividad="No es una actividad")

    with pytest.raises(ConsultaInvalida, match="No hay Piezas Validadas que coincidan"):
        _generar(informes, acervo)
    assert acervo.execute("SELECT count(*) AS n FROM acervo.informe").fetchone() == {"n": 0}


@pytest.mark.historia(269)
def test_un_periodo_invertido_es_invalido(acervo, informes):
    with pytest.raises(ConsultaInvalida) as error:
        _generar(informes, acervo, desde=date(2022, 1, 1), hasta=date(2021, 1, 1))
    assert error.value.campo == "hasta"


@pytest.mark.historia(274)
def test_solo_quien_genera_informes_genera_y_quien_solo_consulta_no_lee_un_borrador(
    acervo, crear, informes
):
    crear()
    borrador = _generar(informes, acervo)

    with pytest.raises(PermisoInsuficiente):
        _generar(informes, acervo, principal=RGC)
    # No se entera de que existe (R-05, como las Piezas no Validadas en 038).
    with pytest.raises(InformeNoEncontrado):
        informes.obtener(acervo, borrador.informe_id, principal=DIRECTORA)
    assert informes.listar(acervo, principal=DIRECTORA) == []
    assert [i.informe_id for i in informes.listar(acervo, principal=RGC)] == [
        borrador.informe_id
    ]



@pytest.mark.historia(274)
def test_quien_solo_consulta_lee_un_aprobado_sin_sus_faltantes(acervo, crear, informes):
    crear()
    crear(Estado.PENDIENTE, nombre="falta.pdf")
    generado = _generar(informes, acervo)
    for persona, rol in ((JORGE, "coordinador"), (MARIA, "rgc")):
        acervo.execute(
            "INSERT INTO acervo.informe_aprobacion"
            " (informe_id, rol, aprobada_por, metodo_autenticacion, huella)"
            " VALUES (%s, %s, %s, 'dev', %s)",
            (generado.informe_id, rol, persona, generado.huella),
        )
    acervo.execute(
        "UPDATE acervo.informe SET estado = 'aprobado', aprobado_el = now()"
        " WHERE informe_id = %s",
        (generado.informe_id,),
    )

    para_la_directora = informes.obtener(acervo, generado.informe_id, principal=DIRECTORA)
    para_el_rgc = informes.obtener(acervo, generado.informe_id, principal=RGC)

    # Un rol que solo tiene CONSULTAR no ve Piezas no Validadas (R-05).
    assert (para_la_directora.grupos, para_la_directora.faltantes) == (generado.grupos, ())
    assert len(para_el_rgc.faltantes) == 1


@pytest.mark.historia(273)
def test_la_integridad_es_m2_sobre_las_piezas_del_informe_y_no_bloquea_generar(
    acervo, crear, informes, fichas
):
    indexadas = [crear(), crear(), crear()]
    for pieza_id in indexadas:
        _indexar(acervo, fichas, pieza_id)
    _corromper(acervo, indexadas[0])
    crear()  # Validada sin indexar: cuenta como incompleta
    afuera = crear(fecha=date(2010, 1, 1))  # fuera del período: no cuenta
    _corromper(acervo, afuera)

    vista = _generar(informes, acervo)

    assert vista.total_piezas == 4
    assert vista.integridad == 0.5
    assert informes.obtener(acervo, vista.informe_id, principal=COORDINADOR).integridad == 0.5


@pytest.mark.historia(273)
@pytest.mark.parametrize(
    "corrupcion",
    [
        "jsonb_set(metadata_, '{inicio}', '\"x\"')",
        "jsonb_set(metadata_, '{fecha_inicio}', '\"\"')",
        "jsonb_set(metadata_, '{ficha_id}', '\"no-es-un-uuid\"')",
        "jsonb_set(metadata_, '{ficha_id}', '123')",
        "metadata_ - 'clase_original'",
    ],
    ids=[
        "inicio-no-numerico", "fecha-vacia", "ficha-invalida", "ficha-numerica",
        "sin-clase-de-original",
    ],
)
def test_un_fragmento_con_metadatos_ilegibles_baja_la_integridad_sin_bloquear(
    acervo, crear, informes, fichas, corrupcion
):
    sana, corrompida = crear(), crear()
    for pieza_id in (sana, corrompida):
        _indexar(acervo, fichas, pieza_id)
    acervo.execute(
        f"UPDATE acervo.data_fragmento SET metadata_ = {corrupcion}"  # type: ignore[arg-type]
        " WHERE node_id = (SELECT min(node_id) FROM acervo.data_fragmento"
        " WHERE metadata_->>'pieza_id' = %s)",
        (str(corrompida),),
    )

    assert _generar(informes, acervo).integridad == 0.5


@pytest.mark.historia(273)
def test_los_fragmentos_de_un_modelo_que_ya_no_es_el_vigente_no_cuentan(
    acervo, crear, informes, fichas
):
    # A mitad de un `reindexar`: dos todavía con el digest anterior, una con el vigente.
    viejas, nueva = [crear(), crear()], crear()
    for pieza_id in viejas:
        _indexar(acervo, fichas, pieza_id, digest="viejo0000000")
    _indexar(acervo, fichas, nueva)
    otra = crear(fecha=date(2010, 1, 1))  # fuera del período: decide cuál es el vigente
    _indexar(acervo, fichas, otra)

    assert _generar(informes, acervo).integridad == pytest.approx(1 / 3, abs=1e-4)


@pytest.mark.historia(273)
def test_la_integridad_se_recalcula_sobre_las_mismas_piezas_al_exportar(
    acervo, crear, informes, fichas
):
    piezas = [crear(), crear()]
    for pieza_id in piezas:
        _indexar(acervo, fichas, pieza_id)
    generado = _generar(informes, acervo)
    _corromper(acervo, piezas[1])
    nueva = crear()  # entra al período después: no es del informe
    _indexar(acervo, fichas, nueva)

    recalculada = informes.recalcular_integridad(acervo, generado.informe_id)

    assert (generado.integridad, recalculada) == (1.0, 0.5)
    assert informes.obtener(acervo, generado.informe_id, principal=COORDINADOR).integridad == 0.5


# HTTP: la app confirma desde su propio pool.


@pytest.fixture
def validadas(cliente) -> Crear:
    """Crea Piezas confirmadas con el Módulo de Fichas de la app, sin trabajador."""
    bd, fichas = cliente.app.state.bd, cliente.app.state.fichas

    def crear(*args: Any, **kwargs: Any) -> UUID:
        with bd.transaccion() as tx:
            return _crear(tx, fichas)(*args, **kwargs)

    return crear


def _pedido(**cambios: Any) -> dict[str, Any]:
    return {"desde": "2018-01-01", "hasta": "2022-12-31", **cambios}


@pytest.mark.historia(269, 270, 274)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_el_coordinador_genera_un_borrador_sin_modelo_y_queda_auditado(
    cliente, validadas, capa_modelos
):
    pieza = validadas(carreras=("ing-civil",))
    validadas(Estado.PENDIENTE, nombre="falta.pdf")
    capa_modelos.invocaciones.clear()
    entrar(cliente, JORGE)

    respuesta = cliente.post("/api/informes", json=_pedido(carreras=["ing-civil"]))

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert respuesta.headers["Location"] == f"/api/informes/{cuerpo['informe_id']}"
    assert capa_modelos.invocaciones == []
    assert (cuerpo["estado"], cuerpo["carreras"], cuerpo["total_piezas"]) == (
        "Borrador", ["ing-civil"], 1,
    )
    [grupo] = cuerpo["grupos"]
    assert grupo["tipo_actividad"] == "Transferencia"
    assert grupo["piezas"][0]["pieza_id"] == str(pieza)
    assert grupo["piezas"][0]["citas"] == [{
        "id": f"pza_{pieza}", "enlace": f"http://localhost/api/piezas/{pieza}/original",
    }]
    assert cuerpo["faltantes"] == []  # la Pendiente no tiene la Carrera
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.INFORME_GENERACION
    )
    assert (evento.actor.usuario_id, evento.objeto) == (
        UUID(JORGE), ObjetoRef("informe", cuerpo["informe_id"])
    )
    assert evento.despues == {
        "desde": "2018-01-01", "hasta": "2022-12-31", "carreras": ["ing-civil"],
        "piezas": 1, "huella": cuerpo["huella"],
    }


@pytest.mark.historia(274)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_un_borrador_lo_leen_quienes_generan_o_aprueban_y_nadie_mas(cliente, validadas):
    validadas()
    entrar(cliente, MARIA)
    rechazo_al_rgc = cliente.post("/api/informes", json=_pedido())
    entrar(cliente, JORGE)
    informe_id = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]

    lecturas = {}
    for persona in (JORGE, MARIA, DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO):
        entrar(cliente, persona)
        lista = cliente.get("/api/informes").json()["informes"]
        detalle = cliente.get(f"/api/informes/{informe_id}")
        lecturas[persona] = ([i["informe_id"] for i in lista], detalle.status_code)

    assert rechazo_al_rgc.status_code == 403
    assert lecturas == {
        JORGE: ([informe_id], 200),
        MARIA: ([informe_id], 200),
        DIRECTORA_DE_CARRERA: ([], 404),
        ADMINISTRADOR_TECNICO: ([], 404),
    }
    entrar(cliente, MARIA)
    otro = cliente.get(f"/api/informes/{uuid4()}")
    assert (otro.status_code, otro.json()["type"], otro.json()["recurso"]) == (
        404, "/errores/recurso-no-encontrado", "informe",
    )


@pytest.mark.historia(269)
@pytest.mark.parametrize(
    ("pedido", "campo"),
    [
        (_pedido(desde="2023-01-01"), "hasta"),
        (_pedido(carreras=["ing-naval"]), "carreras"),
        (_pedido(), None),
    ],
    ids=["periodo-invertido", "carrera-desconocida", "sin-validadas"],
)
@pytest.mark.confirma("`entrar` abre la sesión desde el pool de la app")
def test_un_pedido_que_no_da_informe_responde_422(cliente, pedido, campo):
    entrar(cliente, JORGE)

    respuesta = cliente.post("/api/informes", json=pedido)

    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json().get("campo") == campo
    if campo is None:
        assert respuesta.json()["type"] == "/errores/pedido-invalido"
        assert respuesta.json()["detail"] == (
            "No hay Piezas Validadas que coincidan. No se generó el informe."
        )
    assert cliente.get("/api/informes").json()["informes"] == []


@pytest.mark.historia(273)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_la_integridad_va_en_la_lista_y_el_detalle_y_se_recalcula_ya_aprobado(
    cliente, validadas
):
    bd, fichas = cliente.app.state.bd, cliente.app.state.fichas
    piezas = [validadas() for _ in range(4)]
    with bd.transaccion() as tx:
        for pieza_id in piezas[:3]:
            _indexar(tx, fichas, pieza_id)  # la cuarta queda sin indexar
    entrar(cliente, JORGE)

    generado = cliente.post("/api/informes", json=_pedido())

    assert generado.status_code == 201, generado.text
    informe_id = generado.json()["informe_id"]
    esperada = {"valor": 0.75, "piezas_completas": 3, "piezas_total": 4}
    assert generado.json()["integridad"] == esperada
    assert cliente.get("/api/informes").json()["informes"][0]["integridad"] == esperada
    # Ya Aprobado, como al exportar (070), y por `hv_app`.
    with bd.transaccion() as tx:
        for persona, rol in ((JORGE, "coordinador"), (MARIA, "rgc")):
            tx.execute(
                "INSERT INTO acervo.informe_aprobacion"
                " (informe_id, rol, aprobada_por, metodo_autenticacion, huella)"
                " VALUES (%s, %s, %s, 'dev', %s)",
                (informe_id, rol, persona, generado.json()["huella"]),
            )
        tx.execute(
            "UPDATE acervo.informe SET estado = 'aprobado', aprobado_el = now()"
            " WHERE informe_id = %s",
            (informe_id,),
        )
        _corromper(tx, piezas[0])
        assert cliente.app.state.informes.recalcular_integridad(tx, UUID(informe_id)) == 0.5
    assert cliente.get(f"/api/informes/{informe_id}").json()["integridad"] == {
        "valor": 0.5, "piezas_completas": 2, "piezas_total": 4,
    }


# 068: la aprobación doble (RGC y Coordinador, en cualquier orden).


@pytest.mark.historia(275)
@pytest.mark.parametrize("primero", [COORDINADOR, RGC], ids=["coordinador-primero", "rgc-primero"])
def test_con_una_aprobacion_sigue_borrador_y_con_la_del_otro_rol_queda_aprobado(
    acervo, crear, informes, primero
):
    crear()
    generado = _generar(informes, acervo)
    segundo = RGC if primero is COORDINADOR else COORDINADOR

    con_una = informes.aprobar(acervo, generado.informe_id, principal=primero)
    con_dos = informes.aprobar(acervo, generado.informe_id, principal=segundo)

    assert con_una.estado == "borrador"
    assert [(a.rol, a.aprobada_por) for a in con_una.aprobaciones] == [
        (primero.rol.value, primero.usuario_id)
    ]
    assert con_dos.estado == "aprobado"
    assert {a.rol for a in con_dos.aprobaciones} == {"rgc", "coordinador"}
    firmas = acervo.execute(
        "SELECT metodo_autenticacion, huella FROM acervo.informe_aprobacion"
        " WHERE informe_id = %s",
        (generado.informe_id,),
    ).fetchall()
    assert firmas == [{"metodo_autenticacion": "dev", "huella": generado.huella}] * 2


def _otra_persona(tx: Any, rol: Rol) -> Principal:
    [usuario_id] = tx.execute(
        "INSERT INTO acervo.usuario (email, nombre, rol) VALUES (%s, 'Otra', %s)"
        " RETURNING usuario_id",
        (f"{uuid4().hex[:8]}@fie.undef.edu.ar", rol.value),
    ).fetchone().values()
    return _principal(str(usuario_id), rol)


@pytest.mark.historia(278)
def test_ni_la_misma_persona_dos_veces_ni_dos_del_mismo_rol_ni_un_exportado(
    acervo, crear, informes
):
    crear()
    borrador = _generar(informes, acervo).informe_id
    informes.aprobar(acervo, borrador, principal=COORDINADOR)

    with pytest.raises(AprobadorRepetido, match="Ya aprobaste este informe. Falta la"
                       " aprobación del RGC."):
        informes.aprobar(acervo, borrador, principal=COORDINADOR)
    with pytest.raises(RolYaAprobo, match="persona con rol RGC"):
        informes.aprobar(acervo, borrador, principal=_otra_persona(acervo, Rol.COORDINADOR))
    assert informes.obtener(acervo, borrador, principal=RGC).estado == "borrador"

    informes.aprobar(acervo, borrador, principal=RGC)
    acervo.execute(
        "UPDATE acervo.informe SET estado = 'exportado', exportado_por = %s,"
        " exportado_el = now() WHERE informe_id = %s",
        (JORGE, borrador),
    )
    with pytest.raises(InformeNoAprobable):
        informes.aprobar(acervo, borrador, principal=_otra_persona(acervo, Rol.RGC))
    with pytest.raises(PermisoInsuficiente):
        informes.aprobar(acervo, borrador, principal=DIRECTORA)
    with pytest.raises(InformeNoEncontrado):
        informes.aprobar(acervo, uuid4(), principal=RGC)


# 068 por HTTP: la app confirma desde su propio pool.


def _aprobar(cliente: Any, persona: str, informe_id: str) -> Any:
    entrar(cliente, persona)
    return cliente.post(f"/api/informes/{informe_id}/aprobaciones")


@pytest.mark.historia(275, 278)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_aprueban_el_rgc_y_el_coordinador_que_genero_y_cada_firma_queda_auditada(
    cliente, validadas
):
    validadas()
    entrar(cliente, JORGE)
    informe_id = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]

    rechazos = [
        _aprobar(cliente, persona, informe_id).status_code
        for persona in (DIRECTORA_DE_CARRERA, ADMINISTRADOR_TECNICO)
    ]
    del_coordinador = _aprobar(cliente, JORGE, informe_id)
    repetida = _aprobar(cliente, JORGE, informe_id)
    del_rgc = _aprobar(cliente, MARIA, informe_id)
    ya_aprobado = _aprobar(cliente, MARIA, informe_id)

    assert rechazos == [403, 403]
    assert del_coordinador.status_code == 201, del_coordinador.text
    assert del_coordinador.json()["estado"] == "Borrador"
    [firma] = del_coordinador.json()["aprobaciones"]
    assert (firma["rol"], firma["aprobada_por"], firma["metodo_autenticacion"]) == (
        "coordinador", JORGE, "dev",
    )
    assert firma["nombre"]
    assert (repetida.status_code, repetida.json()["type"], repetida.json()["detail"]) == (
        409, "/errores/aprobador-repetido",
        "Ya aprobaste este informe. Falta la aprobación del RGC.",
    )
    assert (del_rgc.status_code, del_rgc.json()["estado"]) == (201, "Aprobado")
    assert (ya_aprobado.status_code, ya_aprobado.json()["type"]) == (
        409, "/errores/transicion-invalida",
    )
    eventos = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.INFORME_APROBACION
    )
    huella = del_rgc.json()["huella"]
    assert sorted(
        (e.actor.usuario_id, e.objeto.id, e.despues["rol"], e.despues["estado"]) for e in eventos
    ) == sorted([
        (UUID(JORGE), informe_id, "coordinador", "borrador"),
        (UUID(MARIA), informe_id, "rgc", "aprobado"),
    ])
    assert {(e.despues["metodo_autenticacion"], e.despues["huella"]) for e in eventos} == {
        ("dev", huella)
    }


@pytest.mark.historia(278)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_dos_del_mismo_rol_responden_409_y_un_exportado_no_se_aprueba(
    cliente, validadas, padron_restaurado
):
    bd = cliente.app.state.bd
    validadas()
    with bd.transaccion() as tx:
        otro_coordinador = str(_otra_persona(tx, Rol.COORDINADOR).usuario_id)
    entrar(cliente, JORGE)
    informe_id = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    _aprobar(cliente, JORGE, informe_id)

    mismo_rol = _aprobar(cliente, otro_coordinador, informe_id)
    _aprobar(cliente, MARIA, informe_id)
    with bd.transaccion() as tx:
        tx.execute(
            "UPDATE acervo.informe SET estado = 'exportado', exportado_por = %s,"
            " exportado_el = now() WHERE informe_id = %s",
            (JORGE, informe_id),
        )
    exportado = _aprobar(cliente, otro_coordinador, informe_id)

    assert (mismo_rol.status_code, mismo_rol.json()["type"], mismo_rol.json()["detail"]) == (
        409, "/errores/informe-ya-aprobado-por-rol",
        "Hace falta la aprobación de una persona con rol RGC.",
    )
    assert (exportado.status_code, exportado.json()["type"]) == (
        409, "/errores/transicion-invalida",
    )


@pytest.mark.historia(274)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_quien_solo_consulta_lee_los_aprobados_y_exportados_y_no_un_borrador(
    cliente, validadas
):
    bd = cliente.app.state.bd
    validadas()
    entrar(cliente, JORGE)
    ids = [cliente.post("/api/informes", json=_pedido()).json()["informe_id"] for _ in range(3)]
    aprobado, exportado, borrador = ids
    for informe_id in (aprobado, exportado):
        _aprobar(cliente, JORGE, informe_id)
        _aprobar(cliente, MARIA, informe_id)
    with bd.transaccion() as tx:
        tx.execute(
            "UPDATE acervo.informe SET estado = 'exportado', exportado_por = %s,"
            " exportado_el = now() WHERE informe_id = %s",
            (JORGE, exportado),
        )
    entrar(cliente, DIRECTORA_DE_CARRERA)

    lista = cliente.get("/api/informes").json()["informes"]
    lecturas = {i: cliente.get(f"/api/informes/{i}").status_code for i in ids}

    assert sorted(i["informe_id"] for i in lista) == sorted([aprobado, exportado])
    # 066: un Borrador da 404, no 403: quien solo consulta no se entera de que existe.
    assert lecturas == {aprobado: 200, exportado: 200, borrador: 404}


@pytest.mark.historia(275, 278)
@pytest.mark.parametrize(
    ("personas", "codigos", "estado"),
    [
        ((JORGE, MARIA), [201, 201], "Aprobado"),
        ((JORGE, "otro-coordinador"), [201, 409], "Borrador"),
    ],
    ids=["roles-distintos", "mismo-rol"],
)
@pytest.mark.confirma("dos aprobaciones a la vez, cada una con su conexión")
def test_dos_aprobaciones_a_la_vez_dejan_un_estado_coherente(
    cliente, validadas, padron_restaurado, monkeypatch, personas, codigos, estado
):
    """Sin el bloqueo de la fila, las dos leerían cero firmas y el informe quedaría Borrador con
    las dos: la barrera las junta después de leer, y si una espera el bloqueo se rompe sola."""
    bd = cliente.app.state.bd
    validadas()
    with bd.transaccion() as tx:
        otro = str(_otra_persona(tx, Rol.COORDINADOR).usuario_id)
    personas = tuple(otro if p == "otro-coordinador" else p for p in personas)
    entrar(cliente, JORGE)
    informe_id = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    las_dos_leyeron = threading.Barrier(2)
    registrar = RegistroDeAuditoria.registrar

    def registrar_a_la_vez(self, tx, **datos):
        if datos["accion"] is Accion.INFORME_APROBACION:
            try:
                las_dos_leyeron.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass  # la otra espera el bloqueo de la fila: es lo que se prueba
        return registrar(self, tx, **datos)

    monkeypatch.setattr(RegistroDeAuditoria, "registrar", registrar_a_la_vez)
    clientes = [TestClient(cliente.app, headers=cliente.headers) for _ in personas]
    for otro_cliente, persona in zip(clientes, personas, strict=True):
        entrar(otro_cliente, persona)
    with ThreadPoolExecutor(2) as hilos:
        respuestas = list(hilos.map(
            lambda c: c.post(f"/api/informes/{informe_id}/aprobaciones"), clientes
        ))

    assert sorted(r.status_code for r in respuestas) == codigos
    final = cliente.get(f"/api/informes/{informe_id}").json()
    firmas = 2 if estado == "Aprobado" else 1
    assert (final["estado"], len(final["aprobaciones"])) == (estado, firmas)


# 069: Borrador desactualizado, regenerar, descartar y cambios posteriores (R-22).


def _editar(tx: Any, fichas: ModuloFichasPostgres, pieza_id: UUID) -> None:
    """Una persona corrige el resumen de una Validada: su Ficha vigente pasa a ser otra."""
    fichas.guardar_edicion(
        tx,
        pieza_id=pieza_id,
        version_base=2,
        edicion=EdicionFicha(
            tipo="acta", tipo_actividad="Transferencia", fecha_inicio=date(2019, 5, 3),
            precision_fecha=PrecisionFecha.DIA, origen="Archivo de la SEU",
            propietario="Secretaría de Extensión Universitaria", resumen="Corregido.",
            cuerpo="Texto.", contexto="",
        ),
        editada_por=UUID(MARIA),
        metodo=MetodoAutenticacion.DEV,
    )


def _retirar(tx: Any, fichas: ModuloFichasPostgres, pieza_id: UUID) -> None:
    fichas.marcar_borrada(tx, pieza_id, por=UUID(MARIA), motivo="Duplicada")


_CAMBIOS = {"edicion": _editar, "retiro": _retirar}


@pytest.mark.historia(269)
def test_regenerar_un_borrador_lo_reemplaza_y_sus_aprobaciones_no_pasan(acervo, crear, informes):
    crear()
    viejo = _generar(informes, acervo).informe_id
    informes.aprobar(acervo, viejo, principal=COORDINADOR)

    nuevo = _generar(informes, acervo, reemplaza=viejo)
    reemplazado = informes.obtener(acervo, viejo, principal=COORDINADOR)
    vigentes = {v.informe_id for v in informes.listar(acervo, principal=COORDINADOR)}

    assert (reemplazado.estado, reemplazado.reemplazado_por) == ("borrador", nuevo.informe_id)
    assert (nuevo.estado, nuevo.aprobaciones, nuevo.reemplazado_por) == ("borrador", (), None)
    assert nuevo.informe_id in vigentes and viejo not in vigentes
    with pytest.raises(InformeNoAprobable):
        informes.aprobar(acervo, viejo, principal=RGC)
    with pytest.raises(InformeNoAprobable, match="no se puede generar de nuevo"):
        _generar(informes, acervo, reemplaza=viejo)
    with pytest.raises(InformeNoEncontrado):
        _generar(informes, acervo, reemplaza=uuid4())
    informes.aprobar(acervo, nuevo.informe_id, principal=COORDINADOR)
    informes.aprobar(acervo, nuevo.informe_id, principal=RGC)
    with pytest.raises(InformeNoAprobable):  # un Aprobado no se regenera
        _generar(informes, acervo, reemplaza=nuevo.informe_id)


@pytest.mark.historia(269)
def test_un_borrador_descartado_deja_de_listarse_y_no_admite_aprobaciones(
    acervo, crear, informes
):
    crear()
    borrador = _generar(informes, acervo).informe_id
    aprobado = _generar(informes, acervo).informe_id
    informes.aprobar(acervo, aprobado, principal=COORDINADOR)
    informes.aprobar(acervo, aprobado, principal=RGC)

    with pytest.raises(PermisoInsuficiente):
        informes.descartar(acervo, borrador, principal=RGC)
    descartado = informes.descartar(acervo, borrador, principal=COORDINADOR)

    assert (descartado.estado, descartado.descartado_el is not None) == ("borrador", True)
    assert borrador not in {v.informe_id for v in informes.listar(acervo, principal=COORDINADOR)}
    with pytest.raises(InformeNoAprobable):
        informes.aprobar(acervo, borrador, principal=RGC)
    with pytest.raises(InformeNoAprobable, match="no se puede descartar"):
        informes.descartar(acervo, borrador, principal=COORDINADOR)
    with pytest.raises(InformeNoAprobable):
        informes.descartar(acervo, aprobado, principal=COORDINADOR)
    with pytest.raises(InformeNoEncontrado):
        informes.descartar(acervo, uuid4(), principal=COORDINADOR)


@pytest.mark.historia(275)
@pytest.mark.parametrize("cambio", list(_CAMBIOS))
def test_si_una_pieza_cambio_despues_de_generar_aprobar_da_desactualizado(
    acervo, crear, informes, fichas, cambio
):
    crear(nombre="sigue.pdf")
    cambiada = crear(nombre="acta-2021-04.pdf")
    borrador = _generar(informes, acervo).informe_id
    informes.aprobar(acervo, borrador, principal=COORDINADOR)

    _CAMBIOS[cambio](acervo, fichas, cambiada)

    with pytest.raises(InformeDesactualizado, match="La Pieza «acta-2021-04.pdf» cambió"):
        informes.aprobar(acervo, borrador, principal=RGC)
    releido = informes.obtener(acervo, borrador, principal=RGC)
    assert [c.pieza_id for c in releido.cambios] == [cambiada]
    assert (releido.estado, releido.cambio_despues_de_aprobar) == ("borrador", False)


@pytest.mark.historia(271)
@pytest.mark.parametrize("cambio", list(_CAMBIOS))
def test_un_aprobado_conserva_lo_congelado_y_avisa_que_cambio_despues_de_aprobar(
    acervo, crear, informes, fichas, cambio
):
    cambiada = crear(nombre="acta-2021-04.pdf")
    informe_id = _generar(informes, acervo).informe_id
    informes.aprobar(acervo, informe_id, principal=COORDINADOR)
    aprobado = informes.aprobar(acervo, informe_id, principal=RGC)

    _CAMBIOS[cambio](acervo, fichas, cambiada)
    releido = informes.obtener(acervo, informe_id, principal=DIRECTORA)

    assert aprobado.cambio_despues_de_aprobar is False
    assert (releido.estado, releido.cambio_despues_de_aprobar) == ("aprobado", True)
    [(nombre, retirada)] = [(c.nombre_archivo, c.retirada) for c in releido.cambios]
    assert (nombre, retirada) == ("acta-2021-04.pdf", cambio == "retiro")
    assert (releido.grupos, releido.huella, releido.aprobaciones) == (
        aprobado.grupos, aprobado.huella, aprobado.aprobaciones,
    )


@pytest.mark.historia(271)
def test_nuevas_validaciones_no_cambian_un_informe_ya_generado(acervo, crear, informes):
    crear()
    generado = _generar(informes, acervo)

    crear(nombre="nueva.pdf")
    informes.aprobar(acervo, generado.informe_id, principal=COORDINADOR)
    aprobado = informes.aprobar(acervo, generado.informe_id, principal=RGC)

    assert (aprobado.grupos, aprobado.huella, aprobado.total_piezas) == (
        generado.grupos, generado.huella, 1,
    )
    assert (aprobado.cambios, aprobado.cambio_despues_de_aprobar) == ((), False)


# 069 por HTTP: la app confirma desde su propio pool.


@pytest.mark.historia(269)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_generar_de_nuevo_y_descartar_por_http(cliente, validadas):
    validadas()
    entrar(cliente, JORGE)
    viejo = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]

    nuevo = cliente.post("/api/informes", json=_pedido(reemplaza=viejo))
    leido = cliente.get(f"/api/informes/{viejo}").json()
    otra_vez = cliente.post("/api/informes", json=_pedido(reemplaza=viejo))
    entrar(cliente, MARIA)
    del_rgc = cliente.post(f"/api/informes/{nuevo.json()['informe_id']}/descarte")
    entrar(cliente, JORGE)
    descarte = cliente.post(f"/api/informes/{nuevo.json()['informe_id']}/descarte")
    lista = [i["informe_id"] for i in cliente.get("/api/informes").json()["informes"]]

    assert nuevo.status_code == 201, nuevo.text
    assert (leido["estado"], leido["reemplazado_por"]) == ("Borrador", nuevo.json()["informe_id"])
    assert (otra_vez.status_code, otra_vez.json()["type"]) == (409, "/errores/transicion-invalida")
    assert del_rgc.status_code == 403
    assert descarte.status_code == 200, descarte.text
    assert (descarte.json()["estado"], bool(descarte.json()["descartado_el"])) == (
        "Borrador", True,
    )
    assert viejo not in lista and nuevo.json()["informe_id"] not in lista
    [evento] = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.INFORME_DESCARTE
    )
    assert (evento.actor.usuario_id, evento.objeto) == (
        UUID(JORGE), ObjetoRef("informe", nuevo.json()["informe_id"])
    )
    [generacion] = [
        e for e in RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
            accion=Accion.INFORME_GENERACION
        ) if e.objeto.id == nuevo.json()["informe_id"]
    ]
    assert generacion.despues["reemplaza"] == viejo


@pytest.mark.historia(271, 275)
@pytest.mark.confirma("la app confirma desde su propio pool")
def test_desactualizado_y_cambio_despues_de_aprobar_por_http(cliente, validadas):
    bd, fichas = cliente.app.state.bd, cliente.app.state.fichas
    validadas()
    retirada = validadas(nombre="acta-2021-04.pdf")
    entrar(cliente, JORGE)
    borrador = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    aprobado = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    for persona in (JORGE, MARIA):
        _aprobar(cliente, persona, aprobado)
    with bd.transaccion() as tx:
        _retirar(tx, fichas, retirada)

    desactualizado = _aprobar(cliente, JORGE, borrador)
    detalle = cliente.get(f"/api/informes/{aprobado}").json()
    descarte = cliente.post(f"/api/informes/{aprobado}/descarte")

    assert (desactualizado.status_code, desactualizado.json()["type"]) == (
        409, "/errores/informe-desactualizado",
    )
    assert "«acta-2021-04.pdf»" in desactualizado.json()["detail"]
    assert (detalle["estado"], detalle["cambio_despues_de_aprobar"]) == ("Aprobado", True)
    assert detalle["cambios"] == [
        {"pieza_id": str(retirada), "nombre_archivo": "acta-2021-04.pdf", "retirada": True}
    ]
    assert detalle["total_piezas"] == 2 and len(detalle["aprobaciones"]) == 2
    assert (descarte.status_code, descarte.json()["type"]) == (
        409, "/errores/transicion-invalida",
    )


# 070: exportar el informe a `.docx` (R-23, DEC-131).

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _texto(contenido: bytes) -> list[str]:
    """Los párrafos y, después, las celdas de cada tabla: lo que lee Google Docs."""
    documento = Document(io.BytesIO(contenido))
    return [p.text for p in documento.paragraphs] + [
        celda.text for tabla in documento.tables for fila in tabla.rows for celda in fila.cells
    ]


def _aprobado(informes: Informes, tx: Any, **pedido: Any) -> UUID:
    informe_id = _generar(informes, tx, **pedido).informe_id
    informes.aprobar(tx, informe_id, principal=COORDINADOR)
    informes.aprobar(tx, informe_id, principal=RGC)
    return informe_id


@pytest.mark.historia(276)
def test_el_docx_tiene_portada_un_capitulo_por_tipo_de_actividad_y_los_faltantes(
    acervo, crear, informes, fichas
):
    transferencia = crear(resumen="=Convenio con la UTN.", carreras=("ing-civil",))
    crear(tipo_actividad="Consultoría", fecha=date(2020, 3, 1), resumen="Asesoría.")
    for pieza_id in (transferencia,):
        _indexar(acervo, fichas, pieza_id)  # la de Consultoría queda sin indexar
    crear(Estado.PENDIENTE, nombre="acta-2021-04.pdf")
    informe_id = _aprobado(informes, acervo)

    archivo = informes.exportar(acervo, informe_id, "docx", principal=RGC)
    texto = _texto(archivo.contenido)

    assert (archivo.nombre, archivo.tipo_mime) == ("informe-2018-01-01-a-2022-12-31.docx", DOCX)
    assert texto[:5] == [
        "Informe de acreditación",
        "Período: del 01/01/2018 al 31/12/2022.",
        "Carreras: Todas.",
        "Integridad: 50 % (M2 sobre las 2 Piezas incluidas). Aviso: la integridad no llega a 95 %.",
        "Armado a partir de las Fichas validadas. Ningún modelo escribió este informe.",
    ]
    capitulos = [p.text for p in Document(io.BytesIO(archivo.contenido)).paragraphs
                 if p.style.name == "Heading 1"]
    assert capitulos == [
        "Aprobaciones", "Transferencia (1 Pieza)", "Consultoría (1 Pieza)", "Faltantes (1)",
    ]
    assert "3 de mayo de 2019 · acta" in texto
    assert "=Convenio con la UTN." in texto  # un tramo de texto, no una fórmula
    assert f"Cita: pza_{transferencia} · {URL_PUBLICA}/api/piezas/{transferencia}/original" in texto
    assert {"Coordinador de Extensión", "RGC", "De ensayo (sin OIDC)"} <= set(texto)
    assert "acta-2021-04.pdf" in texto
    assert not any(t.startswith("Cambió después de aprobar") for t in texto)


@pytest.mark.historia(276, 278)
def test_la_primera_exportacion_pasa_a_exportado_una_sola_vez_y_da_los_mismos_bytes(
    acervo, crear, informes
):
    crear()
    informe_id = _aprobado(informes, acervo)

    primera = informes.exportar(acervo, informe_id, "docx", principal=COORDINADOR)
    marca = acervo.execute(
        "SELECT estado, exportado_por, exportado_el FROM acervo.informe WHERE informe_id = %s",
        (informe_id,),
    ).fetchone()
    segunda = informes.exportar(acervo, informe_id, "docx", principal=RGC)

    assert (marca["estado"], marca["exportado_por"]) == ("exportado", UUID(JORGE))
    assert acervo.execute(
        "SELECT estado, exportado_por, exportado_el FROM acervo.informe WHERE informe_id = %s",
        (informe_id,),
    ).fetchone() == marca
    assert primera.contenido == segunda.contenido


@pytest.mark.historia(271, 276)
@pytest.mark.parametrize("cambio", list(_CAMBIOS))
def test_se_exporta_lo_congelado_y_avisa_que_cambio_despues_de_aprobar(
    acervo, crear, informes, fichas, cambio
):
    cambiada = crear(nombre="acta-2021-04.pdf", resumen="El resumen que se aprobó.")
    informe_id = _aprobado(informes, acervo)

    _CAMBIOS[cambio](acervo, fichas, cambiada)
    texto = _texto(informes.exportar(acervo, informe_id, "docx", principal=RGC).contenido)

    assert "El resumen que se aprobó." in texto
    assert "Corregido." not in texto
    que = "fue retirada" if cambio == "retiro" else "cambió"
    pieza = "la Pieza" if cambio == "retiro" else "la Ficha de la Pieza"
    assert (
        f"Cambió después de aprobar: {pieza} «acta-2021-04.pdf» {que}. Generá un Borrador"
        " nuevo para incorporar el cambio."
    ) in texto


@pytest.mark.historia(278)
def test_no_se_exporta_un_borrador_ni_sin_permiso_ni_uno_que_no_existe(
    acervo, crear, informes
):
    crear()
    borrador = _generar(informes, acervo).informe_id
    aprobado = _aprobado(informes, acervo)

    with pytest.raises(InformeNoAprobado) as error:
        informes.exportar(acervo, borrador, "docx", principal=COORDINADOR)
    with pytest.raises(PermisoInsuficiente):
        informes.exportar(acervo, aprobado, "docx", principal=DIRECTORA)
    with pytest.raises(InformeNoEncontrado):
        informes.exportar(acervo, uuid4(), "docx", principal=RGC)
    assert error.value.estado_actual == "Borrador"
    assert informes.obtener(acervo, aprobado, principal=RGC).estado == "aprobado"


@pytest.mark.historia(276, 277, 278)
@pytest.mark.confirma("la app confirma desde su propio pool")
@pytest.mark.parametrize(("formato", "tipo_mime"), [("docx", DOCX), ("xlsx", XLSX)])
def test_exportar_por_http_da_el_archivo_sin_cache_y_queda_auditado(
    cliente, validadas, formato, tipo_mime
):
    validadas()
    entrar(cliente, JORGE)
    borrador = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    aprobado = cliente.post("/api/informes", json=_pedido()).json()["informe_id"]
    for persona in (JORGE, MARIA):
        _aprobar(cliente, persona, aprobado)
    url = f"/api/informes/{aprobado}/exportacion?formato={formato}"

    entrar(cliente, DIRECTORA_DE_CARRERA)
    sin_permiso = cliente.get(url)
    entrar(cliente, JORGE)
    en_borrador = cliente.get(f"/api/informes/{borrador}/exportacion?formato={formato}")
    formato_invalido = cliente.get(f"/api/informes/{aprobado}/exportacion?formato=pdf")
    primera = cliente.get(url)
    entrar(cliente, MARIA)
    segunda = cliente.get(url)

    assert sin_permiso.status_code == 403
    assert (en_borrador.status_code, en_borrador.json()["type"]) == (
        409, "/errores/informe-no-aprobado",
    )
    assert en_borrador.json()["estado_actual"] == "Borrador"
    assert (formato_invalido.status_code, formato_invalido.json()["type"]) == (
        422, "/errores/pedido-invalido",
    )
    assert primera.status_code == 200, primera.text
    assert primera.headers["Content-Type"] == tipo_mime
    assert primera.headers["Cache-Control"] == "no-store"
    assert primera.headers["Content-Disposition"] == (
        f'attachment; filename="informe-2018-01-01-a-2022-12-31.{formato}"'
    )
    assert segunda.content == primera.content
    assert cliente.get(f"/api/informes/{aprobado}").json()["estado"] == "Exportado"
    eventos = RegistroDeAuditoria(bd=cliente.app.state.bd).listar(
        accion=Accion.INFORME_EXPORTACION
    )
    assert sorted((e.actor.usuario_id, e.despues["primera"]) for e in eventos) == sorted([
        (UUID(JORGE), True), (UUID(MARIA), False),
    ])
    assert {(e.objeto.id, e.despues["formato"]) for e in eventos} == {(aprobado, formato)}


# 071: exportar el informe a `.xlsx` (R-23, DEC-131).


@pytest.mark.historia(277)
def test_el_xlsx_tiene_resumen_piezas_y_faltantes_y_da_los_mismos_bytes(
    acervo, crear, informes, fichas
):
    transferencia = crear(resumen="=Convenio con la UTN.", carreras=("ing-civil",))
    crear(tipo_actividad="Consultoría", fecha=date(2020, 3, 1), resumen="Asesoría.")
    _indexar(acervo, fichas, transferencia)  # la de Consultoría queda sin indexar
    crear(Estado.PENDIENTE, nombre="@acta-2021-04.pdf")
    informe_id = _aprobado(informes, acervo)

    archivo = informes.exportar(acervo, informe_id, "xlsx", principal=RGC)
    otra = informes.exportar(acervo, informe_id, "xlsx", principal=COORDINADOR)
    libro = load_workbook(io.BytesIO(archivo.contenido))

    assert (archivo.nombre, archivo.tipo_mime) == ("informe-2018-01-01-a-2022-12-31.xlsx", XLSX)
    assert otra.contenido == archivo.contenido
    assert libro.sheetnames == ["Resumen", "Piezas", "Faltantes"]
    resumen = {fila[0]: fila[1:] for fila in libro["Resumen"].iter_rows(values_only=True)}
    assert resumen["Desde"][0] == datetime(2018, 1, 1)
    assert resumen["Piezas incluidas"][0] == 2
    assert resumen["Integridad"][0] == (
        "50 % (M2 sobre las 2 Piezas incluidas). Aviso: la integridad no llega a 95 %."
    )
    assert resumen["Aprobación RGC"][2] == "De ensayo (sin OIDC)"
    assert isinstance(resumen["Aprobación RGC"][1], datetime)
    assert "Aviso" not in resumen
    piezas = list(libro["Piezas"].iter_rows(min_row=2, values_only=True))
    assert [(p[0], p[5]) for p in piezas] == [
        ("Transferencia", "'=Convenio con la UTN."), ("Consultoría", "Asesoría."),
    ]
    assert piezas[0][9:] == (
        f"pza_{transferencia}", f"{URL_PUBLICA}/api/piezas/{transferencia}/original",
    )
    assert [f[0] for f in libro["Faltantes"].iter_rows(min_row=2, values_only=True)] == [
        "'@acta-2021-04.pdf",
    ]
    assert all(c.data_type != "f" for hoja in libro for fila in hoja.iter_rows() for c in fila)


@pytest.mark.historia(271, 277)
def test_el_xlsx_avisa_en_el_resumen_que_cambio_despues_de_aprobar(
    acervo, crear, informes, fichas
):
    cambiada = crear(nombre="acta-2021-04.pdf", resumen="El resumen que se aprobó.")
    informe_id = _aprobado(informes, acervo)

    _CAMBIOS["retiro"](acervo, fichas, cambiada)
    libro = load_workbook(io.BytesIO(
        informes.exportar(acervo, informe_id, "xlsx", principal=RGC).contenido
    ))

    resumen = {fila[0]: fila[1] for fila in libro["Resumen"].iter_rows(values_only=True)}
    assert resumen["Aviso"].startswith("Cambió después de aprobar: la Pieza «acta-2021-04.pdf»")
    assert next(libro["Piezas"].iter_rows(min_row=2, values_only=True))[5] == (
        "El resumen que se aprobó."
    )
