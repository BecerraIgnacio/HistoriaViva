"""M3: los tiempos de la Conversación (074; spec, módulo 27 `banco`; RNF-10, DEC-90, DEC-128, R-20).

Las mediciones, con reloj y Conversación falsos: ningún test de la suite mide con el reloj de
pared, así que una máquina cargada no los vuelve rojos. Después, el turno sin persistir contra
Postgres real y `banco m3` por comando con la capa falsa (el único doble). La búsqueda sin
filtros sobre 28 000 fragmentos, que sí mide de verdad, lleva la marca `real` y no corre en la
suite: la corre una persona con la máquina libre (`docs/mediciones/rnf-10.md`, sección 7).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from capa_modelos_falsa import REFORMULADA_FIJA
from test_busqueda import (  # noqa: F401 — fixtures de Piezas Validadas e indexadas
    LABORATORIO,
    bd,
    indexacion,
    trabajador,
    validada,
)

from historia_viva import composicion
from historia_viva.auditoria import Actor, TipoDeActor
from historia_viva.banco.m3 import (
    OBJETIVO_MS,
    CargaDeFoto,
    CargaNoArranca,
    ResultadoM3,
    TurnoMedido,
    medir,
    resumir,
    tabla_m3,
)
from historia_viva.banco.referencia import ConsultaDeReferencia
from historia_viva.capa_modelos.contrato import ETIQUETA_HISTORIAL, Plantilla
from historia_viva.capa_modelos.ollama import MotorOllama
from historia_viva.composicion import main
from historia_viva.conversacion import EventoDeConversacion, Turno, TurnoEnMarcha
from historia_viva.dominio.tipos import FiltroDeBusqueda
from historia_viva.interruptor import frenar_todo

pytestmark = [pytest.mark.rnf("RNF-10"), pytest.mark.historia(204)]


# --------------------------------------------------------- reloj y Conversación falsos ---


class Reloj:
    def __init__(self) -> None:
        self.s = 100.0

    def __call__(self) -> float:
        return self.s


def _consulta(n: int) -> ConsultaDeReferencia:
    return ConsultaDeReferencia(
        consulta_id=f"c{n}", texto=f"pregunta {n}", piezas_esperadas=("r1",),
        filtro=FiltroDeBusqueda(), conjunto="prueba", escrita_por=None, sin_evidencia=False,
    )


class ConversacionFalsa:
    """Tarda 2 s hasta `fuentes`, 1 s más hasta el primer token y 0,5 s por token después; la
    reformulación (con historial) suma 3 s antes de las fuentes, como en R-20."""

    def __init__(self, reloj: Reloj, *, falla: str | None = None) -> None:
        self.reloj = reloj
        self.falla = falla
        self.historiales: list[tuple[str, tuple[Turno, ...]]] = []

    def __call__(
        self, texto: str, filtro: FiltroDeBusqueda, historial: Sequence[Turno]
    ) -> TurnoEnMarcha:
        self.historiales.append((texto, tuple(historial)))
        return TurnoEnMarcha(self._eventos(texto, bool(historial)), soltar=lambda: None)

    def _eventos(self, texto: str, con_historial: bool) -> Generator[EventoDeConversacion]:
        self.reloj.s += 2.0 + (3.0 if con_historial else 0.0)
        yield EventoDeConversacion("fuentes", {"fichas": []})
        if texto == self.falla:
            self.reloj.s += 4.0
            yield EventoDeConversacion("error", {"type": "/errores/redaccion"})
            return
        for palabra in ("respuesta ", "de ", texto):
            self.reloj.s += 1.0 if palabra == "respuesta " else 0.5
            yield EventoDeConversacion("token", {"texto": palabra})
        self.reloj.s += 0.25
        yield EventoDeConversacion("citas", {})
        yield EventoDeConversacion("fin", {})


def test_resumir_da_percentiles_maximo_y_parte_bajo_15_s_a_mano():
    # 20 valores de 1 a 20 s: p50 = 10,5 s y p95 = 19,05 s (lineal, como numpy); 14 bajo 15 s.
    resumen = resumir([s * 1000.0 for s in range(1, 21)] + [None])

    assert (resumen.n, resumen.p50, resumen.maximo) == (20, 10_500, 20_000)
    assert resumen.p95 == pytest.approx(19_050)
    assert resumen.bajo_objetivo == 14 / 20
    assert resumir([]) == resumir([None])
    assert resumir([]).n == 0 and resumir([]).p95 is None


@pytest.mark.historia(139, 253)
def test_mide_hasta_fuentes_no_hasta_el_ultimo_token_e_informa_primer_token_y_total():
    reloj = Reloj()

    [medido, *_] = medir(ConversacionFalsa(reloj), [_consulta(1)], n=1, reloj=reloj)

    # fuentes 2 s; primer token 2 + 1; total 2 + 1 + 0,5 + 0,5 + 0,25.
    assert (medido.fuentes_ms, medido.primer_token_ms, medido.total_ms) == (2000, 3000, 4250)
    assert (medido.turnos_previos, medido.final) == (0, "fin")


def test_la_serie_con_historial_va_de_1_a_6_turnos_previos_y_se_informa_aparte():
    reloj = Reloj()
    conversacion = ConversacionFalsa(reloj)
    consultas = [_consulta(n) for n in range(3)]  # menos que 7: la serie vuelve a empezar

    muestras = medir(conversacion, consultas, n=3, reloj=reloj)

    assert [m.turnos_previos for m in muestras] == [0, 0, 0, 1, 2, 3, 4, 5, 6]
    serie = conversacion.historiales[3:]
    assert [texto for texto, _ in serie] == [f"pregunta {n % 3}" for n in range(1, 7)]
    # El historial lleva las respuestas que dio la serie, empezando por la del primer turno.
    assert serie[1][1] == (
        Turno("pregunta 0", "respuesta de pregunta 0"),
        Turno("pregunta 1", "respuesta de pregunta 1"),
    )
    resultado = ResultadoM3("A", tuple(muestras), cpu="x", num_thread={})
    assert resultado.a_fuentes(con_historial=False).p95 == 2000
    assert resultado.a_fuentes(con_historial=True).p50 == 5000  # la reformulación, antes
    assert resultado.a_fuentes(con_historial=True).n == 6


def test_un_turno_en_error_no_entra_en_los_percentiles_y_su_respuesta_no_va_al_historial():
    reloj = Reloj()
    conversacion = ConversacionFalsa(reloj, falla="pregunta 0")

    muestras = medir(conversacion, [_consulta(0), _consulta(1)], n=2, reloj=reloj)
    resultado = ResultadoM3("C", tuple(muestras), cpu="x", num_thread={})

    # c0 falla siempre: el primer turno y los turnos 2, 4 y 6 de la serie, que vuelve a empezar.
    assert [m.final == "error" for m in muestras] == [True, False] + [False, True] * 3
    assert resultado.errores == 4 and resultado.a_fuentes(con_historial=False).n == 1
    assert conversacion.historiales[2][1] == (Turno("pregunta 0", None),)
    assert "4 turno(s) terminaron en `error`" in tabla_m3(resultado, hash_referencia="h")


def test_b_descarga_antes_de_cada_turno_y_a_calienta_con_una_descartable():
    reloj = Reloj()
    descargas: list[int] = []
    conversacion = ConversacionFalsa(reloj)

    frio = medir(
        conversacion, [_consulta(0)], n=5, reloj=reloj,
        antes_de_cada_turno=lambda: descargas.append(len(conversacion.historiales)),
    )
    # Una sola consulta: n se corta en las que hay; 1 de primer turno y 6 con historial.
    assert len(frio) == 7 and descargas == list(range(7))

    conversacion.historiales.clear()
    caliente = medir(conversacion, [_consulta(0)], n=1, reloj=reloj, calentar=True)
    assert len(caliente) == 7 and len(conversacion.historiales) == 8


@pytest.mark.historia(139, 253)
def test_el_p95_de_a_y_c_se_compara_con_15_s_y_b_se_informa_sin_comparar():
    lentos = tuple(TurnoMedido(f"c{n}", 0, 16_000, None, 20_000, "fin") for n in range(3))

    for condicion, esperado in (("A", False), ("C", False), ("B", None)):
        resultado = ResultadoM3(condicion, lentos, cpu="i7, 12 hilos", num_thread={"responder": 6})
        assert resultado.bajo_el_objetivo(con_historial=False) is esperado
        tabla = tabla_m3(resultado, hash_referencia="h")
        assert ("se informa |" in tabla) == (condicion == "B")
        # Se informa: ni «cumple», ni «pasa», ni un veredicto.
        assert not any(palabra in tabla.lower() for palabra in ("cumple", "pasa |", "aprob"))
        assert "no es un corte de pasa o no pasa" in tabla
    assert OBJETIVO_MS == 15_000


# ------------------------------------------------------------------- carga de la foto ---


def test_la_carga_de_c_arranca_antes_de_medir_y_se_detiene_al_terminar():
    describiendo = threading.Event()

    def describir() -> None:
        describiendo.set()
        time.sleep(0.005)

    carga = CargaDeFoto(describir)
    reloj = Reloj()
    activa_en_cada_turno: list[bool] = []
    conversacion = ConversacionFalsa(reloj)

    def enviar(
        texto: str, filtro: FiltroDeBusqueda, historial: Sequence[Turno]
    ) -> TurnoEnMarcha:
        activa_en_cada_turno.append(carga.activa and carga.vueltas > 0)
        return conversacion(texto, filtro, historial)

    with carga:
        medir(enviar, [_consulta(0)], n=1, reloj=reloj)
    vueltas = carga.vueltas
    time.sleep(0.05)

    assert activa_en_cada_turno == [True] * 7
    assert not carga.activa and carga.vueltas == vueltas >= 1
    assert carga.informe() == {"vueltas": vueltas, "errores": 0}


def test_si_la_primera_descripcion_falla_c_no_se_mide_y_la_carga_queda_detenida():
    def describir() -> None:
        raise RuntimeError("sin modelo de visión")

    carga = CargaDeFoto(describir)
    with pytest.raises(CargaNoArranca), carga:
        pytest.fail("no tendría que entrar")
    assert not carga.activa


@pytest.mark.rf("RF-01")
def test_sacar_de_memoria_descarga_con_keep_alive_0_cada_modelo_cargado():
    pedidos: list[tuple[str, Any]] = []

    def ollama(pedido: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(pedido.content) if pedido.content else None
        pedidos.append((pedido.url.path, cuerpo))
        if pedido.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "a:1"}, {"name": "b:2"}]})
        return httpx.Response(200, json={"done": True, "done_reason": "unload"})

    motor = MotorOllama(url="http://ollama", cliente=httpx.Client(transport=httpx.MockTransport(ollama)))

    assert motor.sacar_de_memoria() == ["a:1", "b:2"]
    assert pedidos == [
        ("/api/ps", None),
        ("/api/generate", {"model": "a:1", "keep_alive": 0}),
        ("/api/generate", {"model": "b:2", "keep_alive": 0}),
    ]


# --------------------------------------------------------------------- Postgres real ---


CONTAR = (
    "SELECT (SELECT count(*) FROM acervo.conversacion), "
    "(SELECT count(*) FROM acervo.mensaje_conversacion),"
    " (SELECT count(*) FROM acervo.mensaje_cita), (SELECT count(*) FROM acervo.auditoria)"
)


@pytest.mark.rf("RF-47")
@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_un_mensaje_sin_persistir_no_deja_conversacion_mensajes_ni_auditoria(
    cliente, validada, capa_modelos, tx
):
    validada(LABORATORIO)
    antes = tx.execute(CONTAR).fetchone()
    conversaciones = cliente.app.state.conversaciones

    eventos = list(conversaciones.enviar_mensaje(
        None, principal=None, texto="laboratorio de ensayos", filtro=FiltroDeBusqueda(),
        persistir=False, historial=(Turno("¿Qué se creó?", "Un laboratorio [1]."),),
    ))

    assert next(e.tipo for e in eventos) == "fuentes" and eventos[-1].tipo == "fin"
    assert eventos[0].datos["consulta_reformulada"] == REFORMULADA_FIJA
    assert tx.execute(CONTAR).fetchone() == antes
    # El historial pasado llega a `reformular` armado como el de la base.
    [reformulacion] = [
        i for i in capa_modelos.invocaciones if i.plantilla is Plantilla.REFORMULAR_CON_HISTORIAL
    ]
    [historial] = [b for b in reformulacion.bloques if b.etiqueta == ETIQUETA_HISTORIAL]
    assert historial.contenido == "Persona: ¿Qué se creó?\nHistoria Viva: Un laboratorio [1]."


@pytest.mark.rf("RF-47")
def test_un_turno_guardado_no_acepta_historial_de_quien_llama(cliente):
    with pytest.raises(ValueError, match="sin historial"):
        cliente.app.state.conversaciones.enviar_mensaje(
            UUID(int=1), principal=None, texto="laboratorio", filtro=FiltroDeBusqueda(),
            historial=(Turno("inyectada"),),
        )


def _referencia(directorio: Path, pieza_id: str) -> Path:
    directorio.mkdir()
    (directorio / "o.pdf").write_bytes(b"%PDF-1.4")
    (directorio / "piezas.csv").write_text(
        f"referencia_id,archivo,pieza_id\nr1,o.pdf,{pieza_id}\n", encoding="utf-8"
    )
    (directorio / "consultas.csv").write_text(
        "consulta_id,texto,piezas_esperadas,desde,hasta,conjunto,sin_evidencia\n"
        "c1,Laboratorio de ensayos,r1,,,prueba,\n"
        "c2,Acta del Consejo,r1,,,prueba,\n"
        "c3,¿Y en qué año?,,,,prueba,true\n",  # de control: no entra
        encoding="utf-8",
    )
    return directorio


@pytest.fixture
def banco_con_la_capa_falsa(monkeypatch, url_app, capa_modelos, tmp_path) -> Iterator[None]:
    """El entorno del comando, con la capa falsa en lugar de los Ollama de `motores`."""
    monkeypatch.setattr(composicion, "capa_para", lambda filas, **_: capa_modelos.para(filas))
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(tmp_path / "referencia"))
    yield


@pytest.mark.historia(162)
@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app, el trabajador y el freno confirman desde sus propios pools")
def test_banco_m3_corre_con_el_procesamiento_frenado_y_no_guarda_nada(
    banco_con_la_capa_falsa, cliente, validada, tmp_path, capsys, tx
):
    referencia = _referencia(tmp_path / "referencia", validada(LABORATORIO))
    with cliente.app.state.bd.transaccion() as freno:
        frenar_todo(freno, por=Actor(TipoDeActor.CLI, "prueba"), motivo="Medir M3 con la cola frenada")
    antes = tx.execute(CONTAR).fetchone()

    assert main(["banco", "m3", "--condicion", "A", "--cpu", "i7-1255U, 12 hilos"]) == 0

    assert tx.execute(CONTAR).fetchone() == antes
    [corrida] = (referencia / "corridas").glob("m3-A-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    # 2 consultas con Pieza esperada (A pide 30: van las que hay) y la serie de 6.
    assert [m["turnos_previos"] for m in datos["muestras"]] == [0, 0, 1, 2, 3, 4, 5, 6]
    assert [m["consulta_id"] for m in datos["muestras"]][:3] == ["c1", "c2", "c2"]
    assert datos["a_fuentes"]["primer_turno"]["n"] == 2
    assert datos["a_fuentes"]["con_historial"]["n"] == 6
    assert (datos["cpu"], datos["carga"], datos["errores"]) == ("i7-1255U, 12 hilos", None, 0)
    assert "responder" in datos["num_thread"]
    assert set(datos["completa"]["primer_turno"]) == {"n", "p50", "p95", "maximo", "bajo_objetivo"}
    tabla = capsys.readouterr().out
    assert "condición A (en caliente)" in tabla and "| primer turno | 2 |" in tabla


@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_banco_m3_c_describe_la_foto_mientras_mide_y_b_descarga_antes_de_cada_turno(
    banco_con_la_capa_falsa, validada, tmp_path, monkeypatch, capsys
):
    referencia = _referencia(tmp_path / "referencia", validada(LABORATORIO))
    foto = tmp_path / "foto.png"
    foto.write_bytes(b"\x89PNG\r\n\x1a\n")
    descargas: list[object] = []
    monkeypatch.setattr(composicion, "_sacar_de_memoria", descargas.append)

    assert main(["banco", "m3", "--condicion", "C"]) == 2  # sin foto no hay C
    assert main(["banco", "m3", "--condicion", "C", "--foto", str(foto)]) == 0
    [corrida] = (referencia / "corridas").glob("m3-C-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    assert datos["carga"]["vueltas"] >= 1
    assert datos["cpu"].endswith(f"{os.cpu_count()} hilos lógicos")
    assert "describir_imagen" in datos["num_thread"]
    assert "Carga de describir_imagen:" in capsys.readouterr().out

    assert main(["banco", "m3", "--condicion", "B", "--consultas", "1"]) == 0
    assert len(descargas) == 1 + 6  # antes de cada turno medido, sin descartable
    [frio] = (referencia / "corridas").glob("m3-B-*.json")
    assert json.loads(frio.read_text(encoding="utf-8"))["p95_bajo_el_objetivo"] == {
        "primer_turno": None, "con_historial": None,
    }


# ------------------------------------------- medición de verdad: solo con `-m real` ---

FRAGMENTOS_AL_CIERRE = 28_000
"""El volumen al cierre (spec §7); con él, 056 midió de 3,8 a 35 s con la PC cargada."""
UMBRAL_HNSW_MS = 300
"""DEC-07: por encima, la búsqueda exacta no alcanza y va la migración de HNSW (spec §5)."""


@pytest.mark.real
def test_la_busqueda_sin_filtros_sobre_28000_fragmentos_queda_bajo_300_ms(tx):
    """Pendiente de 056. La corre una persona con la máquina libre (CPU < 10 % durante 10 s):

        uv run --no-sync pytest -m real -s -p no:cacheprovider tests/test_banco_m3.py

    La misma forma de consulta que `Indice.buscar` sin filtros (búsqueda exacta, solo el modelo
    vigente, `LIMIT 50` como 056), sobre fragmentos sintéticos en `tx`, que se revierte."""
    metadata = (
        '{"pieza_id": "p", "ficha_id": "f", "tipo": "acta", "tipo_actividad": "Transferencia",'
        ' "carreras": [], "fecha_inicio": "1970-01-01", "fecha_fin": "1979-12-31",'
        ' "modelo_embedding": "m@0123456789ab", "via_ingreso": "archivo"}'
    )
    tx.execute(
        "INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)"
        " SELECT gen_random_uuid()::text, 'texto', %s::jsonb,"
        "  (SELECT array_agg(random())::vector(1024) FROM generate_series(1, 1024) WHERE g > 0)"
        " FROM generate_series(1, %s) g",
        (metadata, FRAGMENTOS_AL_CIERRE),
    )
    consulta = "[" + ",".join(["0.5"] * 1024) + "]"
    tiempos: list[float] = []
    for _ in range(1 + 7):  # la primera calienta la caché y no cuenta
        comienzo = time.perf_counter()
        tx.execute(
            "SELECT node_id, embedding <=> %s::vector AS distancia FROM acervo.data_fragmento"
            " WHERE metadata_->>'modelo_embedding' = %s ORDER BY distancia LIMIT 50",
            (consulta, "m@0123456789ab"),
        ).fetchall()
        tiempos.append((time.perf_counter() - comienzo) * 1000)
    medidos = sorted(tiempos[1:])
    print(f"\n{FRAGMENTOS_AL_CIERRE} fragmentos, sin filtros: " + ", ".join(
        f"{t:.0f} ms" for t in medidos) + f" (p50 {medidos[3]:.0f} ms)")
    assert medidos[3] < UMBRAL_HNSW_MS, "va la migración de HNSW (spec §5, DEC-07)"
