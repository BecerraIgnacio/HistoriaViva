"""Reintentar una Pieza fallida y `reprocesar --fallidos`, con el tope de 6 para todos (033).

`web` corre sin trabajador al lado: cada test toma los trabajos a mano con `procesar_siguiente`,
así ningún reintento se procesa (ni vuelve a fallar) a espaldas de lo que el test mira.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from aplicacion import CABECERAS, _bd, app_sin_trabajador, entrar
from capa_modelos_falsa import CapaModelosFalsa
from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.bd import Bd
from historia_viva.composicion import armar_trabajador, main
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.errores import LimiteExcedido, TransicionInvalida
from historia_viva.interruptor import Freno, cambiar
from historia_viva.procesamiento.cola import Cola
from historia_viva.procesamiento.trabajador import Trabajador
from validacion import ADMINISTRADOR_TECNICO, EDICION_COMPLETA, JORGE, MARIA, PDF

pytestmark = [
    pytest.mark.rf("RF-43"),
    pytest.mark.confirma("`web` y el trabajador confirman desde sus pools"),
]

TOMAS = Actor(TipoDeActor.PERSONA, "tibarra@fie.undef.edu.ar", UUID(ADMINISTRADOR_TECNICO))


@dataclass
class Banco:
    web: TestClient
    trabajador: Trabajador
    trabajador_bd: Bd
    capa: CapaModelosFalsa
    url: str

    def subir_fallida(
        self, nombre: str = "acta.pdf", contenido: bytes = PDF, lote_id: str | None = None
    ) -> str:
        """Una Pieza Recibida, suelta o de `lote_id`, cuyo primer trabajo terminó `fallido`."""
        self.capa.romper(LimiteExcedido("transcribir", "tiempo", 300, 301))
        respuesta = self.web.post(
            "/api/piezas",
            data={} if lote_id is None else {"lote_id": lote_id},
            files={"archivo": (nombre, contenido, "application/pdf")},
        )
        assert respuesta.status_code == 201
        assert self.trabajador.procesar_siguiente() is True
        pieza_id: str = respuesta.json()["pieza_id"]
        return pieza_id

    def fallar_antes(self, pieza_id: str, veces: int) -> None:
        """Suma `veces` trabajos `fallido` viejos a la Pieza, como si se hubiera reintentado."""
        with psycopg.connect(self.url) as conexion:
            for _ in range(veces):
                conexion.execute(
                    "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, estado,"
                    " tiempo_maximo_ms, configuracion_efectiva, encolado_el, terminado_el, error)"
                    " SELECT pieza_id, tipo, 'fallido', tiempo_maximo_ms, configuracion_efectiva,"
                    " encolado_el - interval '1 hour', now(), 'falló antes'"
                    " FROM acervo.trabajo_procesamiento WHERE pieza_id = %s LIMIT 1",
                    (pieza_id,),
                )

    def fallar_antes_en_indexacion(self, pieza_id: str, veces: int) -> None:
        """Suma `veces` indexaciones `fallido` viejas de la misma versión que la última."""
        with psycopg.connect(self.url) as conexion:
            for _ in range(veces):
                conexion.execute(
                    "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, estado, ficha_id,"
                    " tiempo_maximo_ms, configuracion_efectiva, encolado_el, terminado_el, error)"
                    " SELECT pieza_id, tipo, 'fallido', ficha_id, tiempo_maximo_ms,"
                    " configuracion_efectiva, encolado_el - interval '1 hour', now(), 'antes'"
                    " FROM acervo.trabajo_procesamiento WHERE pieza_id = %s"
                    " AND tipo = 'indexacion' LIMIT 1",
                    (pieza_id,),
                )

    def trabajos(self, pieza_id: str) -> list[tuple[str, str | None]]:
        """`(estado, solicitado_por)` de cada trabajo de la Pieza, del más viejo al más nuevo."""
        with psycopg.connect(self.url) as conexion:
            return [
                (estado, None if solicitado_por is None else str(solicitado_por))
                for estado, solicitado_por in conexion.execute(
                    "SELECT estado, solicitado_por FROM acervo.trabajo_procesamiento"
                    " WHERE pieza_id = %s ORDER BY encolado_el",
                    (pieza_id,),
                )
            ]


@pytest.fixture
def banco(url_app, capa_modelos, directorio_originales) -> Iterator[Banco]:
    web = TestClient(
        app_sin_trabajador(
            url_app,
            capa_para=capa_modelos.para,
            validar=capa_modelos.validar,
            directorio_originales=directorio_originales,
        ),
        headers=CABECERAS,
    )
    entrar(web, MARIA)
    # El pool de la suite: abrir conexiones nuevas en Windows tarda y, con `-n`, vence el pool.
    bd = _bd(url_app)
    try:
        yield Banco(
            web=web,
            trabajador=armar_trabajador(
                bd=bd, capa_para=capa_modelos.para, directorio_originales=directorio_originales
            ),
            trabajador_bd=bd,
            capa=capa_modelos,
            url=url_app,
        )
    finally:
        bd.cerrar()


@pytest.mark.historia(82)
def test_reintentar_una_fallida_encola_un_trabajo_con_quien_lo_pidio(banco):
    pieza_id = banco.subir_fallida()

    respuesta = banco.web.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 202
    assert respuesta.json()["procesamiento_pausado"] is False
    assert banco.trabajos(pieza_id) == [("fallido", None), ("encolado", MARIA)]


@pytest.mark.historia(83)
def test_septimo_reproceso_409_tope_de_reprocesos_para_cualquier_rol_y_la_api_no_acepta_forzar(
    banco,
):
    entrar(banco.web, JORGE)
    pieza_id = banco.subir_fallida()
    banco.fallar_antes(pieza_id, 5)
    # El sexto reproceso entra y vuelve a fallar: el intento original más 6 reprocesos.
    assert banco.web.post(f"/api/piezas/{pieza_id}/reproceso").status_code == 202
    assert banco.trabajador.procesar_siguiente() is True

    # Quien la subió (Coordinador) y el RGC: el séptimo es 409 para los dos.
    for persona in (JORGE, MARIA):
        entrar(banco.web, persona)
        respuesta = banco.web.post(
            f"/api/piezas/{pieza_id}/reproceso?forzar=true", json={"forzar": True}
        )

        assert respuesta.status_code == 409
        cuerpo = respuesta.json()
        assert cuerpo["type"] == "/errores/tope-de-reprocesos"
        assert (cuerpo["reprocesos"], cuerpo["tope"]) == (6, 6)
    assert len(banco.trabajos(pieza_id)) == 7


@pytest.mark.historia(82)
def test_reintentar_la_pieza_de_otro_exige_haberla_subido_o_ser_rgc(banco):
    pieza_de_maria = banco.subir_fallida()
    entrar(banco.web, JORGE)

    respuesta = banco.web.post(f"/api/piezas/{pieza_de_maria}/reproceso")

    assert respuesta.status_code == 403
    assert respuesta.json()["type"] == "/errores/permiso-insuficiente"
    assert banco.trabajos(pieza_de_maria) == [("fallido", None)]


@pytest.mark.historia(82)
def test_una_retirada_no_se_reintenta_ni_por_la_api_ni_por_el_comando(banco, comando, capsys):
    pieza_id = banco.subir_fallida()
    with psycopg.connect(banco.url) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET borrada_el = now(), borrada_por = creada_por,"
            " borrada_motivo = 'Duplicada de otra acta' WHERE pieza_id = %s",
            (pieza_id,),
        )

    assert banco.web.post(f"/api/piezas/{pieza_id}/reproceso").status_code == 404
    assert comando(["reprocesar", "--fallidos"]) == 0

    assert "0 encolado" in capsys.readouterr().out
    assert banco.trabajos(pieza_id) == [("fallido", None)]


@pytest.mark.historia(82)
def test_una_pieza_que_ya_no_esta_recibida_responde_409_con_su_estado(banco):
    pieza_id = banco.subir_fallida()
    # Como si se hubiera completado a mano (034): el trabajo fallido queda como historial.
    with psycopg.connect(banco.url) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET estado = 'Pendiente' WHERE pieza_id = %s", (pieza_id,)
        )

    respuesta = banco.web.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 409
    assert respuesta.json()["estado_actual"] == "Pendiente"
    assert "Recibida a Recibida" not in respuesta.json()["detail"]


@pytest.mark.historia(84, 105)
def test_con_el_procesamiento_frenado_el_reintento_se_acepta_y_espera_al_motor(banco):
    pieza_id = banco.subir_fallida()
    assert banco.web.get(f"/api/piezas/{pieza_id}").json()["estado_procesamiento"] == "fallido"
    banco.capa.arreglar()
    with banco.trabajador_bd.transaccion() as tx:
        cambiar(tx, por=TOMAS, cambios={Freno.PROCESAMIENTO: False}, motivo="Corrida del banco")

    respuesta = banco.web.post(f"/api/piezas/{pieza_id}/reproceso")

    assert respuesta.status_code == 202
    assert respuesta.json()["procesamiento_pausado"] is True
    assert banco.trabajador.procesar_siguiente() is False
    detalle = banco.web.get(f"/api/piezas/{pieza_id}").json()
    # «Esperando al motor», no «falló»: el trabajo sigue encolado y el freno está puesto.
    assert (detalle["estado_procesamiento"], detalle["procesamiento_pausado"]) == (
        "encolado",
        True,
    )
    assert detalle["error_procesamiento"] is None


@pytest.fixture
def comando(url_app, directorio_originales, monkeypatch):
    """`main` de `composicion` como lo corre el Administrador técnico, como `hv_app`."""
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_ORIGINALES", str(directorio_originales))
    return main


def _ultimo_estado(banco: Banco, pieza_id: str) -> str:
    return banco.trabajos(pieza_id)[-1][0]


@pytest.mark.historia(83)
@pytest.mark.rnf("RNF-25")
def test_seg_forzar_auditado(banco, comando, capsys):
    en_el_tope = banco.subir_fallida("tope.pdf", PDF + b"1")
    banco.fallar_antes(en_el_tope, 6)  # el original más 6 reprocesos
    otra = banco.subir_fallida("otra.pdf", PDF + b"2")

    assert comando(["reprocesar", "--fallidos"]) == 0

    salida = capsys.readouterr().out
    # No aborta: encola la que puede y lista la que llegó al tope (R-42).
    assert "1 encolado" in salida
    assert en_el_tope in salida
    assert _ultimo_estado(banco, otra) == "encolado"
    assert _ultimo_estado(banco, en_el_tope) == "fallido"
    assert banco.trabajos(otra)[-1][1] is None  # lo pidió el comando, no una persona

    assert comando(["reprocesar", "--fallidos", "--forzar"]) == 0

    assert _ultimo_estado(banco, en_el_tope) == "encolado"
    with psycopg.connect(banco.url) as conexion:
        eventos = conexion.execute(
            "SELECT actor_tipo, despues, motivo FROM acervo.auditoria"
            " WHERE accion = 'pieza.reproceso' AND objeto_id = %s",
            (en_el_tope,),
        ).fetchall()
    ((actor_tipo, despues, motivo),) = eventos
    assert (actor_tipo, despues["forzar"], despues["tipo"]) == ("cli", True, "procesamiento")
    assert motivo


@pytest.mark.historia(82)
def test_reprocesar_fallidos_por_lote_y_por_tipo(banco, comando, capsys):
    lote_id = banco.web.post("/api/lotes", json=LOTE).json()["lote"]["lote_id"]
    del_lote = [banco.subir_fallida(f"l{n}.pdf", PDF + bytes([n]), lote_id) for n in range(2)]
    suelta = banco.subir_fallida("suelta.pdf", PDF + b"s")
    indexada = _con_indexacion_fallida(banco)

    assert comando(["reprocesar", "--fallidos", "--lote", lote_id]) == 0

    assert [_ultimo_estado(banco, p) for p in del_lote] == ["encolado", "encolado"]
    assert _ultimo_estado(banco, suelta) == "fallido"
    assert _indexaciones(banco, indexada) == ["fallido"]

    assert comando(["reprocesar", "--fallidos", "--tipo", "indexacion"]) == 0

    assert _indexaciones(banco, indexada) == ["fallido", "encolado"]
    assert _ultimo_estado(banco, suelta) == "fallido"
    capsys.readouterr()


LOTE = {
    "nombre": "Colaciones 1970-1979",
    "origen": "Archivo fotográfico SEU",
    "fecha_inicio": "1970-01-01",
    "precision_fecha": "decada",
    "fecha_fin": "1979-12-31",
}


def _con_indexacion_fallida(banco: Banco) -> str:
    """Una Pieza procesada con una indexación `fallido`, escrita por SQL (la indexa 046)."""
    banco.capa.arreglar()
    pieza_id: str = banco.web.post(
        "/api/piezas", files={"archivo": ("indexar.pdf", PDF + b"i", "application/pdf")}
    ).json()["pieza_id"]
    assert banco.trabajador.procesar_siguiente() is True
    with psycopg.connect(banco.url) as conexion:
        conexion.execute(
            "INSERT INTO acervo.trabajo_procesamiento (pieza_id, tipo, estado, ficha_id,"
            " tiempo_maximo_ms, configuracion_efectiva, terminado_el, error)"
            " SELECT t.pieza_id, 'indexacion', 'fallido', f.ficha_id, t.tiempo_maximo_ms,"
            " t.configuracion_efectiva, now(), 'el motor de embeddings se cortó'"
            " FROM acervo.trabajo_procesamiento t JOIN acervo.ficha f USING (pieza_id)"
            " WHERE t.pieza_id = %s",
            (pieza_id,),
        )
    return pieza_id


def _indexaciones(banco: Banco, pieza_id: str) -> list[str]:
    with psycopg.connect(banco.url) as conexion:
        return [
            estado
            for (estado,) in conexion.execute(
                "SELECT estado FROM acervo.trabajo_procesamiento"
                " WHERE pieza_id = %s AND tipo = 'indexacion' ORDER BY encolado_el",
                (pieza_id,),
            )
        ]


@pytest.mark.historia(82)
def test_dos_reintentos_a_la_vez_dejan_un_solo_trabajo_activo(banco):
    pieza_id = banco.subir_fallida()
    bd = _bd(banco.url)
    cola = Cola(bd=bd)
    configuracion = ConfiguracionDeModelos(bd=bd, validar=banco.capa.validar).instantanea()

    def reintentar(tx) -> None:
        cola.reprocesar(
            tx,
            UUID(pieza_id),
            tipo_mime="application/pdf",
            configuracion_efectiva=configuracion,
            solicitado_por=UUID(JORGE),
        )

    def segundo() -> None:
        with bd.transaccion() as tx:
            reintentar(tx)

    try:
        with ThreadPoolExecutor(1) as hilo:
            with bd.transaccion() as tx:
                reintentar(tx)
                # El segundo leyó el fallido antes de este COMMIT: lo frena el índice único.
                pendiente = hilo.submit(segundo)
                _esperar_bloqueado(banco.url)
            with pytest.raises(TransicionInvalida):
                pendiente.result(timeout=10)
    finally:
        bd.cerrar()
    assert [estado for estado, _ in banco.trabajos(pieza_id)] == ["fallido", "encolado"]


def _esperar_bloqueado(url: str) -> None:
    limite = time.monotonic() + 10
    with psycopg.connect(url, autocommit=True) as conexion:
        while not (conexion.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE wait_event_type = 'Lock' AND datname = current_database()"
        ).fetchone() or (0,))[0]:
            assert time.monotonic() < limite, "el segundo reintento nunca esperó al primero"
            time.sleep(0.02)


@pytest.mark.historia(83)
def test_las_indexaciones_originales_de_otras_versiones_no_consumen_el_tope(
    banco, comando, capsys
):
    pieza_id = _con_indexacion_fallida(banco)  # la indexación original de la v1 falló
    banco.fallar_antes_en_indexacion(pieza_id, 5)  # y 5 reprocesos de la v1
    assert banco.web.put(f"/api/piezas/{pieza_id}/ficha", json=EDICION_COMPLETA).status_code == 200
    # La indexación original de la v2, la que encoló validar (047), falla: es su primer
    # intento, no un reproceso.
    with psycopg.connect(banco.url) as conexion:
        actualizados = conexion.execute(
            "UPDATE acervo.trabajo_procesamiento SET estado = 'fallido', terminado_el = now(),"
            " error = 'se cortó' WHERE pieza_id = %s AND tipo = 'indexacion'"
            " AND estado = 'encolado'",
            (pieza_id,),
        ).rowcount
    assert actualizados == 1

    assert comando(["reprocesar", "--fallidos", "--tipo", "indexacion"]) == 0

    assert "1 encolado" in capsys.readouterr().out
    assert _indexaciones(banco, pieza_id)[-1] == "encolado"
