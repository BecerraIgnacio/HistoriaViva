"""Una Pieza Validada queda indexada por el trabajador de indexación (046; spec, módulo 19).

HTTP para subir y validar, Postgres real y el trabajador `--tipos indexacion` que arma
`composicion`, manejado a mano. El único doble es la capa, con `embeber` determinista. Validar
encola la indexación en la misma transacción (047).
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from fastapi.testclient import TestClient

from aplicacion import CABECERAS, app_sin_trabajador, entrar
from capa_modelos_falsa import CapaModelosFalsa
from espera import esperar_estado
from ollama_falso import OllamaFalso, digest_de
from historia_viva.auditoria import Accion, Actor, TipoDeActor
from historia_viva.bd import Bd
from historia_viva.capa_modelos.configuracion import validar as validar_filas
from historia_viva.capa_modelos.local import capa_para
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_CONSULTA,
    BloqueDatos,
    Capacidad,
    UsoEmbedding,
)
from historia_viva.composicion import armar_indexacion, armar_trabajador, main
from historia_viva.dominio.cuerpo import CuerpoDeFicha, MarcaDeHoja
from historia_viva.dominio.errores import FichaCambio, MotorInterrumpido
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import FiltroDeBusqueda, MetodoAutenticacion, ViaDeIngreso
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.indexacion import MotivoNoIndexada
from historia_viva.procesamiento.cola import Cola, EstadoDeTrabajo, TipoDeTrabajo
from historia_viva.procesamiento.trabajador import Trabajador
from test_fichas import COMPLETA
from test_ingesta import MP4, AnalizadorFalso
from validacion import (
    ADMINISTRADOR_TECNICO,
    CONTEXTO,
    DIRECTORA_DE_CARRERA,
    EDICION_COMPLETA,
    MARIA,
    PDF,
)

if TYPE_CHECKING:
    from historia_viva.indexacion import Indexacion

pytestmark = [
    pytest.mark.historia(121),
    # `inspeccionar` carga `indice`: un solo proceso de xdist paga llama-index (ticket 130).
    pytest.mark.xdist_group("indice"),
    pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools"),
]

APLICACION = "trabajador-de-test_indexacion"
MODELO_EMBEBIDO = "bge-m3@" + hashlib.sha256(b"bge-m3").hexdigest()[:12]
"""El de la fila sembrada de `embeber`, con el digest que inventa el doble."""

ACTOR = Actor(TipoDeActor.CLI, "test_indexacion")

CUERPO = (
    "--- Página 1 ---\n\nActa de fundación de la Facultad.\n\n"
    "--- Página 2 ---\n\nSe resolvió crear el laboratorio de ensayos."
)


class CapaQueMiraLaBase(CapaModelosFalsa):
    """Al embeber anota si el trabajador tiene una transacción abierta y corre `durante`."""

    def __init__(self, url_app: str) -> None:
        super().__init__()
        self._url_app = url_app
        self.transacciones_al_embeber: list[int] = []
        self.durante: Callable[[], None] = lambda: None

    def para(self, configuracion: Any) -> CapaQueMiraLaBase:
        """Como el `capa_para` real: valida las filas que recibe (también la instantánea de un
        trabajo) antes de armar la capa."""
        validar_filas(configuracion, ("ollama", "ollama_embeddings"), completa=False)
        super().para(configuracion)
        return self

    def embeber(self, **argumentos: Any) -> Any:
        self.transacciones_al_embeber.append(_transacciones_abiertas(self._url_app))
        self.durante()
        return super().embeber(**argumentos)


def _transacciones_abiertas(url_app: str) -> int:
    with psycopg.connect(url_app) as conexion:
        fila = conexion.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s"
            " AND datname = current_database() AND xact_start IS NOT NULL",
            (APLICACION,),
        ).fetchone()
    assert fila is not None
    return int(fila[0])


@pytest.fixture
def capa_modelos(url_app) -> CapaQueMiraLaBase:
    return CapaQueMiraLaBase(url_app)


@pytest.fixture
def bd(url_app) -> Iterator[Bd]:
    bd = Bd(make_conninfo(url_app, application_name=APLICACION), tamano_pool=4)
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


def validar(cliente, pieza_id: str, **cambios: object) -> None:
    url = f"/api/piezas/{pieza_id}/ficha"
    version = cliente.get(url).json()["version"]
    respuesta = cliente.put(url, json={**EDICION_COMPLETA, "version_base": version, **cambios})
    assert respuesta.status_code == 200, respuesta.text


def encolar(bd: Bd, indexacion: Indexacion, pieza_id: str) -> UUID | None:
    with bd.transaccion() as tx:
        return indexacion.encolar(tx, UUID(pieza_id))


@pytest.mark.rf("RF-14")
@pytest.mark.historia(122, 138, 215)
def test_tras_el_trabajo_la_validada_tiene_fragmentos_con_su_metadata_que_cubren_el_cuerpo(
    cliente, pieza_pendiente, indexacion, trabajador
):
    validar(cliente, pieza_pendiente, cuerpo=CUERPO, contexto=CONTEXTO, carreras=["ing-mecanica", "ing-civil"])

    assert trabajador.procesar_siguiente() is True

    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    cuerpo = CuerpoDeFicha(CUERPO, CONTEXTO).armar()
    assert [(f.ubicacion, f.texto.strip()) for f in informe.fragmentos] == [
        ({"seccion": "principal", "pagina": 1}, "--- Página 1 ---\n\nActa de fundación de la Facultad."),
        (
            {"seccion": "principal", "pagina": 2},
            "--- Página 2 ---\n\nSe resolvió crear el laboratorio de ensayos.",
        ),
        ({"seccion": "contexto"}, f"## Contexto\n\n{CONTEXTO}"),
    ]
    assert all(f.texto == cuerpo[f.inicio : f.fin] for f in informe.fragmentos)
    assert set().union(*(range(f.inicio, f.fin) for f in informe.fragmentos)) == set(
        range(len(cuerpo))
    )
    assert {
        (
            f.tipo,
            f.tipo_actividad,
            f.carreras,
            f.fecha_inicio,
            f.fecha_fin,
            f.clase_original,
            f.via_ingreso,
        )
        for f in informe.fragmentos
    } == {
        (
            "acta",
            "Evento Académico",
            ("ing-civil", "ing-mecanica"),
            date(1975, 5, 3),
            date(1975, 5, 3),
            ClaseDeOriginal.DOCUMENTO,
            ViaDeIngreso.ARCHIVO,
        )
    }
    assert informe.modelos == {MODELO_EMBEBIDO}
    assert informe.trabajo is EstadoDeTrabajo.COMPLETADO


def _por_cita(informe) -> dict[tuple[UUID, str], UUID]:
    """`pza_<id>` más la ubicación, la cita durable, y el `fragmento_id` que hoy la respalda."""
    return {(f.pieza_id, repr(sorted(f.ubicacion.items()))): f.fragmento_id for f in informe.fragmentos}


@pytest.mark.rnf("RNF-08")
@pytest.mark.historia(122)
def test_reindexar_otra_version_no_deja_restos_y_cambia_los_ids_pero_no_la_cita(
    cliente, pieza_pendiente, indexacion, trabajador
):
    pieza_id = UUID(pieza_pendiente)
    validar(cliente, pieza_pendiente, cuerpo=CUERPO, contexto=CONTEXTO)
    trabajador.procesar_siguiente()
    primera = indexacion.inspeccionar(pieza_id)

    validar(cliente, pieza_pendiente, cuerpo="--- Página 1 ---\n\nActa corregida.")
    trabajador.procesar_siguiente()
    segunda = indexacion.inspeccionar(pieza_id)

    assert [f.texto for f in segunda.fragmentos] == ["--- Página 1 ---\n\nActa corregida."]
    assert len({f.ficha_id for f in segunda.fragmentos}) == 1
    citas_primera, citas_segunda = _por_cita(primera), _por_cita(segunda)
    cita = (pieza_id, repr(sorted({"seccion": "principal", "pagina": 1}.items())))
    assert cita in citas_primera and cita in citas_segunda
    assert citas_primera[cita] != citas_segunda[cita]


@pytest.mark.rf("RF-15")
@pytest.mark.historia(121, 122)
def test_editar_una_validada_la_desindexa_en_el_pedido_y_la_reindexa_con_el_texto_nuevo(
    cliente, pieza_pendiente, indexacion, trabajador
):
    pieza_id = UUID(pieza_pendiente)
    validar(cliente, pieza_pendiente, cuerpo="El texto viejo.")
    trabajador.procesar_siguiente()
    assert [f.texto for f in indexacion.inspeccionar(pieza_id).fragmentos] == ["El texto viejo."]

    validar(cliente, pieza_pendiente, cuerpo="El texto nuevo.")

    editada = indexacion.inspeccionar(pieza_id)
    assert (editada.fragmentos, editada.trabajo) == ((), EstadoDeTrabajo.ENCOLADO)
    assert trabajador.procesar_siguiente() is True
    assert [f.texto for f in indexacion.inspeccionar(pieza_id).fragmentos] == ["El texto nuevo."]


@pytest.mark.rf("RF-15")
@pytest.mark.historia(121)
def test_quitarle_un_obligatorio_a_una_validada_la_deja_incompleta_y_sin_fragmentos_en_el_pedido(
    cliente, pieza_pendiente, indexacion, trabajador
):
    validar(cliente, pieza_pendiente)
    trabajador.procesar_siguiente()
    assert indexacion.inspeccionar(UUID(pieza_pendiente)).fragmentos != ()

    validar(cliente, pieza_pendiente, tipo=None)

    assert cliente.get(f"/api/piezas/{pieza_pendiente}").json()["estado"] == "Incompleta"
    assert indexacion.inspeccionar(UUID(pieza_pendiente)).fragmentos == ()
    assert trabajador.procesar_siguiente() is False  # nada que reindexar


@pytest.mark.historia(121)
def test_una_pieza_que_dejo_de_ser_validada_termina_completado_sin_fragmentos(
    cliente, pieza_pendiente, indexacion, trabajador
):
    validar(cliente, pieza_pendiente)
    validar(cliente, pieza_pendiente, tipo=None)  # quedó Incompleta antes de que corriera

    assert trabajador.procesar_siguiente() is True

    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert (informe.fragmentos, informe.trabajo) == ((), EstadoDeTrabajo.COMPLETADO)


@pytest.mark.rf("RF-15")
def test_una_pendiente_no_se_encola_ni_tiene_fragmentos(
    pieza_pendiente, bd, indexacion, trabajador
):
    assert encolar(bd, indexacion, pieza_pendiente) is None

    assert trabajador.procesar_siguiente() is False
    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert (informe.fragmentos, informe.trabajo) == ((), None)


def _trabajo(url_bd: str, pieza_id: str) -> dict[str, Any]:
    with psycopg.connect(url_bd, row_factory=psycopg.rows.dict_row) as conexion:
        fila = conexion.execute(
            "SELECT estado, generaciones FROM acervo.trabajo_procesamiento"
            " WHERE pieza_id = %s AND tipo = 'indexacion'",
            (pieza_id,),
        ).fetchone()
    assert fila is not None
    return fila


@pytest.mark.historia(121)
def test_embebe_sin_transaccion_y_si_la_ficha_cambia_reintenta_una_vez_con_la_nueva(
    cliente, pieza_pendiente, indexacion, trabajador, capa_modelos
):
    validar(cliente, pieza_pendiente)
    ediciones = iter([lambda: validar(cliente, pieza_pendiente, cuerpo="La versión nueva.")])
    capa_modelos.durante = lambda: next(ediciones, lambda: None)()

    assert trabajador.procesar_siguiente() is True

    assert capa_modelos.transacciones_al_embeber == [0, 0]
    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert [f.texto for f in informe.fragmentos] == ["La versión nueva."]
    assert informe.trabajo is EstadoDeTrabajo.COMPLETADO


@pytest.mark.historia(121)
def test_si_la_ficha_sigue_cambiando_el_trabajo_vuelve_encolado_sin_gastar_generacion(
    cliente, pieza_pendiente, indexacion, trabajador, capa_modelos, url_bd
):
    validar(cliente, pieza_pendiente)
    capa_modelos.durante = lambda: validar(cliente, pieza_pendiente)

    assert trabajador.procesar_siguiente() is True

    assert len(capa_modelos.transacciones_al_embeber) == 2  # no hay tercera
    assert _trabajo(url_bd, pieza_pendiente) == {"estado": "encolado", "generaciones": 0}
    assert indexacion.inspeccionar(UUID(pieza_pendiente)).fragmentos == ()
    assert trabajador.procesar_siguiente() is False  # no lo retoma en plena edición


def _pendiente(cliente, n: int) -> str:
    respuesta = cliente.post(
        "/api/piezas", files={"archivo": (f"acta-{n}.pdf", PDF + str(n).encode(), "application/pdf")}
    )
    pieza_id: str = respuesta.json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    return pieza_id


def _indexaciones(url_bd: str) -> dict[str, int]:
    with psycopg.connect(url_bd) as conexion:
        filas = conexion.execute(
            "SELECT pieza_id::text, count(*) FROM acervo.trabajo_procesamiento"
            " WHERE tipo = 'indexacion' AND estado = 'encolado' GROUP BY pieza_id"
        ).fetchall()
    return dict(filas)


def _buscar(bd: Bd, capa: CapaModelosFalsa) -> list[tuple[UUID, float]]:
    from historia_viva.indice import Indice

    [vector] = capa.embeber(
        textos=(BloqueDatos(etiqueta=ETIQUETA_CONSULTA, contenido="laboratorio de ensayos"),),
        uso=UsoEmbedding.CONSULTA,
    ).vectores
    hallados = Indice(bd=bd, dsn=bd.dsn).buscar(
        vector, modelo_embedding=MODELO_EMBEBIDO, filtro=FiltroDeBusqueda(), k=10
    )
    return [(h.fragmento_id, round(h.puntaje, 6)) for h in hallados]


@pytest.mark.rnf("RNF-08")
@pytest.mark.historia(167)
def test_reindexar_encola_una_por_validada_no_retirada_y_reconstruye_los_mismos_fragmentos(
    cliente, pieza_pendiente, bd, indexacion, trabajador, capa_modelos, url_app, url_bd,
    monkeypatch, capsys,
):
    validadas = [pieza_pendiente, _pendiente(cliente, 1), _pendiente(cliente, 2)]
    _pendiente(cliente, 3)  # Pendiente: no se indexa
    for n, pieza_id in enumerate(validadas):
        # Textos distintos: dos vectores iguales empatan y el orden entre ellos no está fijado.
        validar(cliente, pieza_id, cuerpo=CUERPO.replace("de", f"de {n}"))
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
            " borrada_motivo = 'prueba de reindexar' WHERE pieza_id = %s",
            (MARIA, validadas[2]),
        )
    while trabajador.procesar_siguiente():  # los que encoló validar; la retirada queda sin fragmentos
        pass
    monkeypatch.setenv("HV_BD_URL", url_app)

    assert main(["reindexar"]) == 0
    assert "2 Pieza(s)" in capsys.readouterr().out
    with bd.transaccion() as tx:
        assert indexacion.reconstruir(tx, actor=ACTOR) == 2  # reencolar reusa el trabajo activo
    assert _indexaciones(url_bd) == {validadas[0]: 1, validadas[1]: 1}
    while trabajador.procesar_siguiente():
        pass
    antes = [indexacion.inspeccionar(UUID(p)) for p in validadas]
    hallados_antes = _buscar(bd, capa_modelos)

    with psycopg.connect(url_bd) as conexion:
        conexion.execute("DELETE FROM acervo.data_fragmento")
    with bd.transaccion() as tx:
        indexacion.reconstruir(tx, actor=ACTOR)
    while trabajador.procesar_siguiente():
        pass

    assert [indexacion.inspeccionar(UUID(p)) for p in validadas] == antes
    assert [len(i.fragmentos) for i in antes] == [2, 2, 0]
    assert _buscar(bd, capa_modelos) == hallados_antes != []


@pytest.mark.historia(215)
def test_con_el_ollama_de_generacion_caido_el_trabajador_de_indexacion_indexa_con_el_suyo(
    cliente, pieza_pendiente, bd, indexacion, directorio_originales
):
    # El cortacircuito de cada tipo mira solo los motores de sus capacidades (DEC-86); la capa
    # es la real, con los dos Ollama de mentira en su borde HTTP.
    generacion, embeddings = OllamaFalso(), OllamaFalso()
    generacion.prendido = False
    embeddings.modelos_descargados = ["bge-m3"]
    motores = {
        "ollama": generacion.motor("http://ollama:11434"),
        "ollama_embeddings": embeddings.motor("http://ollama-embeddings:11434"),
    }
    validar(cliente, pieza_pendiente)

    trabajador = armar_trabajador(
        bd=bd,
        capa_para=partial(capa_para, motores=motores),
        directorio_originales=directorio_originales,
        tipos=(TipoDeTrabajo.INDEXACION,),
    )

    assert trabajador.procesar_siguiente() is True
    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert len(informe.fragmentos) == 1
    assert informe.modelos == {f"bge-m3@{digest_de('bge-m3')[:12]}"}
    assert embeddings.embeds() and not generacion.chats()


def _esperar_candado(url_app: str, tope_s: float = 10.0) -> None:
    """Hasta que una conexión del trabajador de estos tests espera un candado. Mira como
    `hv_app`: Postgres solo le muestra `wait_event_type` a otra sesión del mismo rol."""
    limite = time.monotonic() + tope_s
    with psycopg.connect(url_app, autocommit=True) as conexion:
        while not conexion.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE application_name = %s AND wait_event_type = 'Lock'",
            (APLICACION,),
        ).fetchone()[0]:  # type: ignore[index]
            assert time.monotonic() < limite, "`guardar` no esperó ningún candado"
            time.sleep(0.05)


@pytest.mark.historia(121)
@pytest.mark.parametrize("antes", ["Validada", "Incompleta"])
def test_guardar_espera_a_la_edicion_en_curso_y_reintenta_con_la_version_nueva(
    antes, cliente, pieza_pendiente, bd, indexacion, url_app
):
    # Dos conexiones (DEC-146 punto 4): una edición con la Pieza bloqueada y sin confirmar hace
    # esperar al `FOR SHARE` de `guardar`, que al seguir ve la versión nueva y lanza `FichaCambio`.
    pieza_id = UUID(pieza_pendiente)
    validar(cliente, pieza_pendiente)
    if antes == "Incompleta":
        validar(cliente, pieza_pendiente, tipo=None)  # el trabajo sigue encolado (DEC-91)
    trabajo = Cola(bd=bd).tomar([TipoDeTrabajo.INDEXACION])
    assert trabajo is not None
    resultado = indexacion.ejecutar(trabajo)
    fichas = ModuloFichasPostgres(bd=bd)
    with psycopg.connect(url_app, row_factory=psycopg.rows.dict_row) as otra:
        fichas.guardar_edicion(
            otra,
            pieza_id=pieza_id,
            version_base=fichas.obtener_ficha(pieza_id).version,
            edicion=replace(COMPLETA, cuerpo="La versión de la otra conexión."),
            editada_por=UUID(MARIA),
            metodo=MetodoAutenticacion.DEV,
        )

        def guardar() -> None:
            with bd.transaccion() as tx:
                indexacion.guardar(tx, trabajo, resultado)

        from historia_viva.indice import precargar

        precargar()  # llama-index fuera del hilo

        with ThreadPoolExecutor(1) as hilo:
            guardado = hilo.submit(guardar)
            try:
                _esperar_candado(url_app)  # espera al `COMMIT` de la otra conexión
                assert not guardado.done()
                otra.commit()
            finally:
                otra.rollback()  # si falló antes del commit, suelta la Pieza y el hilo termina
            with pytest.raises(FichaCambio):
                guardado.result(timeout=5)
    assert indexacion.inspeccionar(pieza_id).fragmentos == ()  # nada de la versión vieja (049)

    reintento = indexacion.ejecutar(trabajo)
    with bd.transaccion() as tx:
        indexacion.guardar(tx, trabajo, reintento)
    informe = indexacion.inspeccionar(pieza_id)
    assert [f.texto for f in informe.fragmentos] == ["La versión de la otra conexión."]
    assert informe.ficha_id == fichas.obtener_ficha(pieza_id).ficha_id


@pytest.mark.historia(167)
def test_reindexar_encola_sin_cargar_llama_index(url_app):
    codigo = (
        "import sys; from historia_viva.composicion import main; codigo = main(['reindexar']);"
        " print(codigo, any(m.split('.')[0] == 'llama_index' for m in sys.modules))"
    )

    salida = subprocess.run(
        [sys.executable, "-c", codigo],
        env={**os.environ, "HV_BD_URL": url_app},
        capture_output=True,
        text=True,
        check=False,
    )

    assert salida.stdout.splitlines()[-1] == "0 False", salida.stderr


@pytest.mark.historia(167)
def test_desindexar_no_carga_llama_index_con_la_pieza_bloqueada(url_app):
    """Editar una Validada o retirarla desindexa con la Pieza `FOR UPDATE`: si eso cargara
    llama-index (segundos, minutos con la suite en paralelo), la transacción quedaría ociosa con
    la Pieza tomada hasta que `idle_in_transaction_session_timeout` la corte y el PUT dé 503."""
    codigo = (
        "import sys, uuid; from historia_viva.bd import Bd;"
        " from historia_viva.composicion import armar_indexacion;"
        f" bd = Bd({url_app!r}, tamano_pool=1);"
        " indexacion = armar_indexacion(bd=bd, capa_para=None, validar=lambda _: None)\n"
        "with bd.transaccion() as tx: indexacion.desindexar(tx, uuid.uuid4())\n"
        "print(any(m.split('.')[0] == 'llama_index' for m in sys.modules))"
    )

    salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, check=False)

    assert salida.stdout.splitlines()[-1:] == ["False"], salida.stderr


# ------------------------------------------------------------------ planillas, video y Aportes ---

VENTAS = "\n".join(
    [
        MarcaDeHoja("Ventas", 20, 120, 120).a_texto(),
        "| Fecha | Concepto | Monto |",
        "| --- | --- | --- |",
        *(f"| 1998-03-{n % 28 + 1:02} | Cuota de la cooperadora | {n * 100} |" for n in range(100)),
    ]
)
NOTAS = f"{MarcaDeHoja('Notas', 1, 2, 2).a_texto()}\n| Nota |\n| --- |\n| Sin firmas |"
PLANILLA = f"Se omitió 1 hoja oculta.\n\n{VENTAS}\n\n{NOTAS}"
DESCRIPCION = (
    "Acto de colación en el aula magna.\n\n[00:12:30] Habla el rector.\n\n"
    "[01:02:05] Entrega de diplomas."
)


def _citas(cliente, pieza_id: str) -> list[tuple[str, str]]:
    """Las Citas de `GET /api/busqueda` sin texto, como las ve la Directora de Carrera."""
    entrar(cliente, DIRECTORA_DE_CARRERA)
    respuesta = cliente.get("/api/busqueda", params={"desde": "1975-01-01"})
    entrar(cliente, MARIA)
    assert respuesta.status_code == 200, respuesta.text
    [resultado] = [r for r in respuesta.json()["resultados"] if r["pieza_id"] == pieza_id]
    return [(c["ubicacion_legible"], c["enlace"]) for c in resultado["citas"]]


@pytest.mark.rf("RF-19")
@pytest.mark.rnf("RNF-08")
@pytest.mark.historia(135, 188)
def test_validar_una_planilla_deja_bloques_de_filas_con_su_hoja_que_reconstruyen_el_cuerpo(
    cliente, indexacion, trabajador
):
    entrar(cliente, MARIA)
    pieza_id: str = cliente.post(
        "/api/piezas",
        files={"archivo": ("cooperadora.csv", b"Fecha;Monto\n1998-03-01;100\n", "text/csv")},
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    validar(cliente, pieza_id, cuerpo=PLANILLA)

    assert trabajador.procesar_siguiente() is True

    fragmentos = indexacion.inspeccionar(UUID(pieza_id)).fragmentos
    assert "".join(f.texto for f in fragmentos) == PLANILLA
    assert fragmentos[0].ubicacion == {"seccion": "principal"}
    ventas = [f.ubicacion for f in fragmentos if f.ubicacion.get("hoja") == "Ventas"]
    assert len(ventas) > 1
    assert (ventas[0]["filas"][0], ventas[-1]["filas"][1]) == (20, 120)
    assert fragmentos[-1].ubicacion == {
        "seccion": "principal",
        "hoja": "Notas",
        "filas": [1, 2],
    }
    assert {(f.clase_original, f.via_ingreso) for f in fragmentos} == {
        (ClaseDeOriginal.PLANILLA, ViaDeIngreso.ARCHIVO)
    }
    hasta = ventas[0]["filas"][1]
    assert _citas(cliente, pieza_id)[1] == (
        f"Hoja Ventas · filas 20–{hasta}",
        f"/api/piezas/{pieza_id}/original",
    )


@pytest.mark.rf("RF-38")
@pytest.mark.historia(113, 188)
def test_las_filas_que_cita_la_busqueda_son_las_de_la_columna_fila_del_visor(
    cliente, indexacion, trabajador
):
    # Recorrido R7: la columna 1 dice el número de registro, así el visor y la Cita se
    # comparan contra la planilla y no entre sí.
    registros = ["id;Concepto", *(f"r{n};Cuota de la cooperadora" for n in range(2, 202))]
    entrar(cliente, MARIA)
    pieza_id: str = cliente.post(
        "/api/piezas",
        files={"archivo": ("socios.csv", "\n".join(registros).encode(), "text/csv")},
    ).json()["pieza_id"]
    esperar_estado(cliente, pieza_id, "Pendiente")
    validar(cliente, pieza_id, cuerpo=cliente.get(f"/api/piezas/{pieza_id}/ficha").json()["cuerpo"])
    assert trabajador.procesar_siguiente() is True

    [hoja] = cliente.get(f"/api/piezas/{pieza_id}/planilla").json()["hojas"]
    visor = {hoja["desde"] + k + 1: fila[0] for k, fila in enumerate(hoja["filas"])}
    assert visor[2] == "r2" and visor[201] == "r201"
    entrar(cliente, DIRECTORA_DE_CARRERA)
    respuesta = cliente.get("/api/busqueda", params={"desde": "1975-01-01"})
    [resultado] = [r for r in respuesta.json()["resultados"] if r["pieza_id"] == pieza_id]
    citas = [c for c in resultado["citas"] if c["ubicacion_legible"].startswith("Hoja socios")]
    assert len(citas) > 1
    for cita in citas:
        desde, hasta = map(int, cita["ubicacion_legible"].split("filas ")[1].split("–"))
        citadas = re.findall(r"^\| (r\d+) \|", cita["texto"], re.M)
        assert citadas == [visor[n] for n in range(desde, hasta + 1) if n in visor]


@pytest.fixture
def cliente_video(url_app, capa_modelos, directorio_originales):
    """Una app que acepta videos: el análisis es el doble de `test_ingesta` (no hay `ffprobe`)."""
    app = app_sin_trabajador(
        url_app,
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=directorio_originales,
        analizar_video=AnalizadorFalso(),
    )
    cliente = TestClient(app, headers=CABECERAS)
    entrar(cliente, MARIA)
    yield cliente
    app.state.bd.cerrar()


def _video(cliente) -> str:
    subida = cliente.post("/api/piezas/video", params={"nombre": "colacion.mp4"}, content=MP4)
    assert subida.status_code == 201, subida.text
    return str(subida.json()["pieza_id"])


@pytest.mark.rf("RF-19")
@pytest.mark.historia(135, 188)
def test_validar_un_video_deja_un_fragmento_por_marca_de_tiempo_con_su_marca(
    cliente_video, indexacion, trabajador
):
    pieza_id = _video(cliente_video)
    validar(cliente_video, pieza_id, cuerpo=DESCRIPCION)

    assert trabajador.procesar_siguiente() is True

    fragmentos = indexacion.inspeccionar(UUID(pieza_id)).fragmentos
    assert [(f.ubicacion, f.texto.strip()) for f in fragmentos] == [
        ({"seccion": "principal"}, "Acto de colación en el aula magna."),
        ({"seccion": "principal", "marca": "00:12:30"}, "[00:12:30] Habla el rector."),
        ({"seccion": "principal", "marca": "01:02:05"}, "[01:02:05] Entrega de diplomas."),
    ]
    assert {(f.clase_original, f.via_ingreso) for f in fragmentos} == {
        (ClaseDeOriginal.VIDEO, ViaDeIngreso.ARCHIVO)
    }
    original = f"/api/piezas/{pieza_id}/original"
    assert _citas(cliente_video, pieza_id) == [
        ("Texto", original),
        ("min 12:30", f"{original}#t=750"),
        ("min 1:02:05", f"{original}#t=3725"),
    ]


@pytest.mark.rf("RF-19")
@pytest.mark.historia(135)
def test_un_video_con_el_cuerpo_vacio_da_un_unico_fragmento_con_el_resumen_firmado(
    cliente_video, indexacion, trabajador
):
    pieza_id = _video(cliente_video)
    validar(cliente_video, pieza_id, cuerpo="", contexto="")

    assert trabajador.procesar_siguiente() is True

    [fragmento] = indexacion.inspeccionar(UUID(pieza_id)).fragmentos
    assert (fragmento.texto, fragmento.ubicacion) == (
        EDICION_COMPLETA["resumen"],
        {"seccion": "resumen"},
    )
    assert _citas(cliente_video, pieza_id) == [("Resumen", f"/api/piezas/{pieza_id}/original")]


@pytest.mark.rf("RF-39")
@pytest.mark.historia(107, 118, 188)
def test_recorrido_r8_el_video_de_mis_piezas_se_analiza_firma_y_la_busqueda_lo_cita_con_su_marca(
    cliente_video, trabajador
):
    pieza_id = _video(cliente_video)
    [fila] = [
        p
        for p in cliente_video.get("/api/piezas", params={"mias": "true"}).json()["piezas"]
        if p["pieza_id"] == pieza_id
    ]
    assert (fila["estado"], fila["requiere_analisis_humano"]) == ("Pendiente", True)
    url = f"/api/piezas/{pieza_id}/ficha"
    ficha = cliente_video.get(url).json()
    # Nadie propuso nada (ADR-0005): ni resumen ni descripción, y los nueve obligatorios de siempre.
    assert (ficha["frontmatter"]["resumen"], ficha["cuerpo"]) == (None, "")
    assert len(ficha["obligatorios"]) == 9

    sin_resumen = cliente_video.put(
        url, json={**EDICION_COMPLETA, "version_base": ficha["version"], "resumen": "", "cuerpo": ""}
    )
    assert sin_resumen.status_code == 200, sin_resumen.text
    assert (sin_resumen.json()["estado"], sin_resumen.json()["campos_faltantes"]) == (
        "Incompleta",
        ["resumen"],
    )
    validar(cliente_video, pieza_id, cuerpo=DESCRIPCION)
    assert trabajador.procesar_siguiente() is True

    assert ("min 12:30", f"/api/piezas/{pieza_id}/original#t=750") in _citas(cliente_video, pieza_id)


@pytest.mark.rf("RF-19")
@pytest.mark.historia(135)
def test_un_video_con_el_cuerpo_vacio_y_contexto_da_el_resumen_primero_y_el_contexto_detras(
    cliente_video, indexacion, trabajador
):
    pieza_id = _video(cliente_video)
    validar(cliente_video, pieza_id, cuerpo="", contexto=CONTEXTO)

    assert trabajador.procesar_siguiente() is True

    fragmentos = indexacion.inspeccionar(UUID(pieza_id)).fragmentos
    # Los dos empiezan en 0: `inspeccionar` los ordena por `inicio`, no por `orden`.
    assert {(f.ubicacion["seccion"], f.texto) for f in fragmentos} == {
        ("resumen", EDICION_COMPLETA["resumen"]),
        ("contexto", f"## Contexto\n\n{CONTEXTO}"),
    }


@pytest.mark.rf("RF-19")
@pytest.mark.historia(194)
def test_un_aporte_con_el_cuerpo_vacio_da_el_fragmento_del_resumen_y_un_documento_ninguno(
    cliente, url_bd, indexacion, trabajador, validada_en_blanco
):
    aporte, documento = validada_en_blanco(aporte=True), validada_en_blanco(aporte=False)

    assert trabajador.procesar_siguiente() is True
    assert trabajador.procesar_siguiente() is True

    [fragmento] = indexacion.inspeccionar(UUID(aporte)).fragmentos
    assert (fragmento.texto, fragmento.via_ingreso) == (EDICION_COMPLETA["resumen"], ViaDeIngreso.APORTE)
    assert indexacion.inspeccionar(UUID(documento)).fragmentos == ()


@pytest.fixture
def validada_en_blanco(cliente, url_bd):
    """Sube un PDF distinto y lo valida con el cuerpo vacío; con `aporte`, antes lo vuelve un
    Aporte por SQL (la puerta `/v1/aportes` todavía no existe)."""

    def validar_en_blanco(*, aporte: bool) -> str:
        entrar(cliente, MARIA)
        archivo = PDF + f"%{aporte}\n".encode()
        pieza_id: str = cliente.post(
            "/api/piezas", files={"archivo": ("pieza.pdf", archivo, "application/pdf")}
        ).json()["pieza_id"]
        esperar_estado(cliente, pieza_id, "Pendiente")
        if aporte:
            with psycopg.connect(url_bd, autocommit=True) as conexion:
                conexion.execute(
                    "UPDATE acervo.pieza SET via_ingreso = 'aporte', clase_original = 'texto',"
                    " tipo_mime = 'text/plain; charset=utf-8', creada_por = NULL"
                    " WHERE pieza_id = %s",
                    (pieza_id,),
                )
        validar(cliente, pieza_id, cuerpo="", contexto="")
        return pieza_id

    return validar_en_blanco


# ------------------------------------------ 049: cambio de modelo en vuelo, índice incompleto ---

OTRO_MODELO = "otro-embebedor"
MODELO_OTRO_EMBEBIDO = f"{OTRO_MODELO}@" + hashlib.sha256(OTRO_MODELO.encode()).hexdigest()[:12]
FILA_INVALIDA = (
    "UPDATE acervo.configuracion_capacidad SET modelo = 'otro-embebedor', motor = 'nadie'"
    " WHERE capacidad = 'embeber'"
)
CAMBIAR_MODELO = (
    "UPDATE acervo.configuracion_capacidad SET modelo = 'otro-embebedor'"
    " WHERE capacidad = 'embeber'"
)


def _ultima_indexacion(url_bd: str, pieza_id: str) -> dict[str, Any]:
    with psycopg.connect(url_bd, row_factory=psycopg.rows.dict_row) as conexion:
        fila = conexion.execute(
            "SELECT estado, ficha_id, configuracion_efectiva FROM acervo.trabajo_procesamiento"
            " WHERE pieza_id = %s AND tipo = 'indexacion' ORDER BY encolado_el DESC LIMIT 1",
            (pieza_id,),
        ).fetchone()
    assert fila is not None
    return fila


@pytest.mark.rf("RF-33")
@pytest.mark.historia(166)
def test_reusar_una_indexacion_encolada_la_refresca_con_el_modelo_y_la_version_vigentes(
    cliente, pieza_pendiente, indexacion, trabajador, editar_fila, url_bd
):
    validar(cliente, pieza_pendiente)
    editar_fila(CAMBIAR_MODELO)

    validar(cliente, pieza_pendiente, cuerpo="La versión nueva.")  # reusa el trabajo encolado

    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert _ultima_indexacion(url_bd, pieza_pendiente)["ficha_id"] == informe.ficha_id
    assert trabajador.procesar_siguiente() is True
    assert trabajador.procesar_siguiente() is False  # uno solo: ya corrió con el modelo vigente
    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert [f.texto for f in informe.fragmentos] == ["La versión nueva."]
    assert informe.modelos == {MODELO_OTRO_EMBEBIDO}


@pytest.mark.rf("RF-33")
@pytest.mark.historia(166)
def test_cambiar_el_modelo_con_la_indexacion_en_curso_deja_otra_con_el_modelo_vigente(
    cliente, pieza_pendiente, indexacion, trabajador, capa_modelos, editar_fila, url_bd
):
    validar(cliente, pieza_pendiente)
    # La compuerta: el cambio de fila llega con el trabajo `en_curso`, mientras embebe.
    capa_modelos.durante = lambda: editar_fila(CAMBIAR_MODELO)

    assert trabajador.procesar_siguiente() is True

    capa_modelos.durante = lambda: None
    assert indexacion.inspeccionar(UUID(pieza_pendiente)).modelos == {MODELO_EMBEBIDO}
    reencolada = _ultima_indexacion(url_bd, pieza_pendiente)
    assert reencolada["estado"] == "encolado"
    assert reencolada["configuracion_efectiva"]["por_capacidad"]["embeber"]["modelo"] == OTRO_MODELO
    assert trabajador.procesar_siguiente() is True
    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert informe.modelos == {MODELO_OTRO_EMBEBIDO}
    assert informe.trabajo is EstadoDeTrabajo.COMPLETADO
    assert trabajador.procesar_siguiente() is False


DETENER = ["interruptor", "detener", "procesamiento_habilitado", "--motivo", "mantenimiento"]
REANUDAR = ["interruptor", "reanudar", "procesamiento_habilitado"]


@pytest.mark.rf("RF-33")
@pytest.mark.historia(162, 167)
def test_reindexar_se_audita_con_el_actor_y_con_el_procesamiento_frenado_se_niega(
    cliente, pieza_pendiente, trabajador, url_app, url_bd, monkeypatch, capsys
):
    validar(cliente, pieza_pendiente)
    while trabajador.procesar_siguiente():
        pass
    monkeypatch.setenv("HV_BD_URL", url_app)
    auditoria = cliente.app.state.auditoria
    assert main(DETENER) == 0
    capsys.readouterr()

    assert main(["reindexar"]) == 1

    assert "procesamiento está detenido" in capsys.readouterr().err
    assert _indexaciones(url_bd) == {}
    assert auditoria.listar(accion=Accion.INDICE_RECONSTRUCCION) == []
    assert main(REANUDAR) == 0
    assert main(["reindexar"]) == 0
    [evento] = auditoria.listar(accion=Accion.INDICE_RECONSTRUCCION)
    assert (evento.actor.tipo, evento.actor.etiqueta) == (TipoDeActor.CLI, getpass.getuser())
    assert evento.despues == {"encoladas": 1}
    assert main(DETENER) == 0
    assert trabajador.procesar_siguiente() is False  # frenado no se toma nada
    assert main(REANUDAR) == 0
    assert trabajador.procesar_siguiente() is True


@pytest.mark.rf("RF-33")
@pytest.mark.historia(138)
def test_una_indexacion_fallida_no_frena_a_las_demas_enciende_el_indice_incompleto_y_se_recupera(
    cliente, pieza_pendiente, trabajador, capa_modelos, url_app, directorio_originales,
    monkeypatch,
):
    otra = _pendiente(cliente, 1)
    validar(cliente, pieza_pendiente)
    validar(cliente, otra, cuerpo="Otra acta de la Facultad.")
    capa_modelos.romper(MotorInterrumpido("El motor se cortó a mitad."), en=Capacidad.EMBEBER)
    assert trabajador.procesar_siguiente() is True  # la primera falla
    capa_modelos.arreglar()
    assert trabajador.procesar_siguiente() is True  # y la otra se indexa igual

    busqueda = cliente.get("/api/busqueda", params={"desde": "1900-01-01"}).json()
    salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert (busqueda["total"], busqueda["avisos"]) == (1, ["indice_incompleto"])
    assert salud["indice"] == {
        "indexables": 2,
        "con_fragmentos": 1,
        "sin_fragmentos": 1,
        "pendientes": 0,
        "fallidas": 1,
        "de_otro_modelo": 0,
        "afectadas": 1,
        "incompleto": True,
    }
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_ORIGINALES", str(directorio_originales))
    assert main(["reprocesar", "--fallidos", "--tipo", "indexacion"]) == 0
    assert trabajador.procesar_siguiente() is True
    recuperado = cliente.get("/api/salud").json()["indice"]
    assert (recuperado["con_fragmentos"], recuperado["incompleto"]) == (2, False)
    assert cliente.get("/api/busqueda", params={"desde": "1900-01-01"}).json()["avisos"] == []


@pytest.mark.historia(293, 296)
def test_no_indexadas_dice_por_que_cada_validada_no_es_buscable(
    cliente, pieza_pendiente, bd, indexacion, trabajador, capa_modelos, url_bd
):
    fallida, perdida, indexada, en_cola, pendiente = [pieza_pendiente] + [
        _pendiente(cliente, n) for n in range(1, 5)
    ]
    for pieza_id in (fallida, perdida, indexada):
        validar(cliente, pieza_id, cuerpo=f"Acta {pieza_id}.")
    capa_modelos.romper(MotorInterrumpido("El motor se cortó a mitad."), en=Capacidad.EMBEBER)
    assert trabajador.procesar_siguiente() is True  # la primera validada falla
    capa_modelos.arreglar()
    while trabajador.procesar_siguiente():
        pass
    validar(cliente, en_cola)
    # Índice perdido: el trabajo terminó y no quedan fragmentos.
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "DELETE FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = %s", (perdida,)
        )

    with bd.transaccion() as tx:
        motivos = indexacion.no_indexadas(
            tx, [UUID(p) for p in (fallida, perdida, indexada, en_cola, pendiente)]
        )

    assert motivos == {
        UUID(fallida): MotivoNoIndexada.FALLIDA,
        UUID(perdida): MotivoNoIndexada.SIN_TRABAJO,
        UUID(en_cola): MotivoNoIndexada.EN_COLA,
    }


def _fragmentos_de_otro_modelo(url_bd: str, pieza_id: str) -> None:
    """Los fragmentos de la Pieza pasan a ser de otro modelo, como antes de un `reindexar`."""
    with psycopg.connect(url_bd) as conexion:
        conexion.execute(
            "UPDATE acervo.data_fragmento SET metadata_ = jsonb_set(metadata_,"
            " '{modelo_embedding}', '\"e5-large@ffffffffffff\"') WHERE metadata_->>'pieza_id' = %s",
            (pieza_id,),
        )


@pytest.mark.rf("RF-33")
@pytest.mark.historia(138)
def test_la_salud_cuenta_los_fragmentos_de_otro_modelo_de_las_validadas_y_no_los_residuos(
    cliente, pieza_pendiente, trabajador, url_bd
):
    vigente, de_otro_modelo, retirada = pieza_pendiente, _pendiente(cliente, 1), _pendiente(cliente, 2)
    for pieza_id in (vigente, de_otro_modelo, retirada):
        validar(cliente, pieza_id, cuerpo=f"Acta {pieza_id}.")
    while trabajador.procesar_siguiente():
        pass
    with psycopg.connect(url_bd) as conexion:  # retirada a mano: sus fragmentos quedan de residuo
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = %s,"
            " borrada_motivo = 'residuo de prueba' WHERE pieza_id = %s",
            (MARIA, retirada),
        )
    _fragmentos_de_otro_modelo(url_bd, de_otro_modelo)
    _fragmentos_de_otro_modelo(url_bd, retirada)

    indice = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()["indice"]

    assert indice == {
        "indexables": 2,
        "con_fragmentos": 2,
        "sin_fragmentos": 0,
        "pendientes": 0,
        "fallidas": 0,
        "de_otro_modelo": 1,
        "afectadas": 1,
        "incompleto": True,
    }


@pytest.mark.historia(166)
def test_con_la_fila_de_embeber_invalida_el_gancho_avisa_y_no_pierde_los_fragmentos(
    cliente, pieza_pendiente, bd, indexacion, capa_modelos, editar_fila, caplog
):
    validar(cliente, pieza_pendiente)
    # La red de seguridad del gancho: una `indexacion` que valida al congelar (la del compose no
    # valida; ese camino lo cubre el test siguiente).
    trabajador = Trabajador(
        bd=bd,
        cola=Cola(bd=bd),
        manejadores={TipoDeTrabajo.INDEXACION: indexacion},
        habilitado=lambda: True,
    )
    capa_modelos.durante = lambda: editar_fila(FILA_INVALIDA)

    assert trabajador.procesar_siguiente() is True

    informe = indexacion.inspeccionar(UUID(pieza_pendiente))
    assert (informe.modelos, informe.trabajo) == ({MODELO_EMBEBIDO}, EstadoDeTrabajo.COMPLETADO)
    assert "No se pudo reencolar" in caplog.text


@pytest.mark.historia(166)
def test_con_la_fila_de_embeber_invalida_el_trabajador_reencola_y_la_corrida_siguiente_falla(
    cliente, pieza_pendiente, indexacion, trabajador, capa_modelos, editar_fila, url_bd
):
    validar(cliente, pieza_pendiente)
    capa_modelos.durante = lambda: editar_fila(FILA_INVALIDA)

    assert trabajador.procesar_siguiente() is True  # completa con el modelo viejo y reencola

    capa_modelos.durante = lambda: None
    assert indexacion.inspeccionar(UUID(pieza_pendiente)).modelos == {MODELO_EMBEBIDO}
    assert _ultima_indexacion(url_bd, pieza_pendiente)["estado"] == "encolado"
    # El cortacircuito todavía da al motor por vivo (10 s de caché); en frío no toma nada hasta
    # que se arregle la fila, y después falla igual: la instantánea congelada es la inválida.
    assert trabajador.procesar_siguiente() is True
    assert _ultima_indexacion(url_bd, pieza_pendiente)["estado"] == "fallido"
    assert indexacion.progreso().fallidas == 1
