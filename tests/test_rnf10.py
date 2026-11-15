"""El guion manual de RNF-10 (056; S-14, S-15, S-24): el informe por condición con los tres
tiempos guardados de cada respuesta, contra el objetivo de 15 s sin cortar (DEC-128).

Las respuestas se escriben con SQL en `tx`, como las dejaría `conversacion`; el resumen es puro.
La medición con Ollama real la hace una persona con `docs/mediciones/rnf-10.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from historia_viva.banco.rnf10 import Condicion, informe, main, resumir
from historia_viva.conversacion import TiempoGuardado, tiempos_guardados
from validacion import DIRECTORA_DE_CARRERA

pytestmark = [pytest.mark.rnf("RNF-10"), pytest.mark.historia(204)]

COMIENZO = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def _efectiva(num_thread: int | None) -> dict[str, Any]:
    parametros = {} if num_thread is None else {"num_thread": num_thread}
    fila = {"motor": "ollama", "modelo": "m", "instruccion_id": None, "parametros": parametros}
    return {"digest": "d", "por_capacidad": {"responder": fila, "embeber": dict(fila)}}


def _conversacion(tx, respuestas: list[tuple[int, str, tuple[int, int | None, int], int]]) -> UUID:
    """Una Conversación con sus turnos: cada respuesta es (minuto, estado, tiempos, num_thread)."""
    [(conversacion,)] = tx.execute(
        "INSERT INTO acervo.conversacion (persona_id) VALUES (%s) RETURNING conversacion_id",
        (DIRECTORA_DE_CARRERA,),
    ).fetchall()
    for turno, (minuto, estado, (fuentes, primer_token, total), hilos) in enumerate(respuestas):
        cuando = COMIENZO + timedelta(minutes=minuto)
        tx.execute(
            "INSERT INTO acervo.mensaje_conversacion"
            " (conversacion_id, orden, autor, contenido, filtros, creado_el)"
            " VALUES (%s, %s, 'persona', 'una pregunta', '{}'::jsonb, %s)",
            (conversacion, 2 * turno + 1, cuando),
        )
        tx.execute(
            "INSERT INTO acervo.mensaje_conversacion (conversacion_id, orden, autor, contenido,"
            " estado, configuracion_efectiva, ms_hasta_fuentes, ms_hasta_primer_token,"
            " ms_hasta_respuesta, creado_el)"
            " VALUES (%s, %s, 'historia_viva', 'texto', %s, %s, %s, %s, %s, %s)",
            (conversacion, 2 * turno + 2, estado, Jsonb(_efectiva(hilos)), fuentes,
             primer_token, total, cuando),
        )
    return conversacion


def test_lee_los_tiempos_guardados_de_las_respuestas_de_la_ventana(tx):
    dentro = _conversacion(
        tx, [(1, "completa", (900, 2100, 9000), 4), (3, "sin_evidencia", (700, None, 700), 4)]
    )
    _conversacion(tx, [(90, "completa", (1, 2, 3), 4)])  # fuera de la ventana

    tiempos = tiempos_guardados(tx, desde=COMIENZO, hasta=COMIENZO + timedelta(hours=1))

    assert tiempos == (
        TiempoGuardado(dentro, 2, "completa", 900, 2100, 9000, {"responder": 4, "embeber": 4},
                       con_historial=False),
        TiempoGuardado(dentro, 4, "sin_evidencia", 700, None, 700, {"responder": 4, "embeber": 4},
                       con_historial=True),
    )


def test_el_reintento_del_primer_turno_sigue_siendo_primer_turno(tx):
    """Persona 1, `error` 2 y su reintento 3: sin pregunta anterior no hay historial."""
    [(conversacion,)] = tx.execute(
        "INSERT INTO acervo.conversacion (persona_id) VALUES (%s) RETURNING conversacion_id",
        (DIRECTORA_DE_CARRERA,),
    ).fetchall()
    for orden, autor, estado in [
        (1, "persona", None), (2, "historia_viva", "error"), (3, "historia_viva", "completa"),
        (4, "persona", None), (5, "historia_viva", "completa"),
    ]:
        tx.execute(
            "INSERT INTO acervo.mensaje_conversacion (conversacion_id, orden, autor, contenido,"
            " filtros, estado, ms_hasta_fuentes, ms_hasta_primer_token, ms_hasta_respuesta,"
            " creado_el) VALUES (%s, %s, %s, 'x', %s, %s, %s, %s, %s, %s)",
            (conversacion, orden, autor, Jsonb({}) if autor == "persona" else None, estado,
             *((None,) * 3 if autor == "persona" else (1, 2, 3)), COMIENZO),
        )

    tiempos = tiempos_guardados(tx, desde=COMIENZO, hasta=COMIENZO + timedelta(hours=1))

    assert [(t.orden, t.con_historial) for t in tiempos] == [(2, False), (3, False), (5, True)]


def test_sin_configuracion_guardada_el_num_thread_queda_vacio(tx):
    conversacion = _conversacion(tx, [(1, "sin_evidencia", (700, None, 700), 4)])
    tx.execute(
        "UPDATE acervo.mensaje_conversacion SET configuracion_efectiva = NULL"
        " WHERE conversacion_id = %s AND autor = 'historia_viva'",
        (conversacion,),
    )

    [tiempo] = tiempos_guardados(tx, desde=COMIENZO, hasta=COMIENZO + timedelta(hours=1))

    assert tiempo.num_thread == {}


def test_resumir_da_p50_p95_maximo_y_la_parte_bajo_el_objetivo():
    resumen = resumir([*range(1000, 21000, 1000)])  # 20 valores: 1 s a 20 s

    assert (resumen.n, resumen.p50, resumen.p95, resumen.maximo) == (20, 10000, 19000, 20000)
    assert resumen.bajo_objetivo == 0.7  # 1 s a 14 s; 15 s ya no está por debajo
    assert resumir([None, None]).n == 0
    assert resumir([]).p95 is None


def _tiempo(orden: int, estado: str, fuentes: int, primer_token: int | None, total: int,
            hilos: int | None = 4) -> TiempoGuardado:
    """`hilos=None`: la fila de la capacidad no fija `num_thread` y lo elige Ollama."""
    num_thread = {"responder": hilos, "embeber": hilos}
    conversacion = UUID(int=1)
    return TiempoGuardado(
        conversacion, orden, estado, fuentes, primer_token, total, num_thread,
        con_historial=orden > 2,
    )


@pytest.mark.historia(139, 253)
def test_el_informe_separa_condiciones_y_turnos_y_no_corta():
    condiciones = [
        Condicion("A", (
            _tiempo(2, "completa", 1200, 3000, 9000),
            _tiempo(4, "completa", 2500, 4000, 16000),
            _tiempo(2, "error", 1100, None, 1500),  # no cuenta: la redacción falló
            # Sin redacción no se guarda la configuración: no dice nada de `num_thread`.
            TiempoGuardado(UUID(int=2), 2, "sin_evidencia", 1000, None, 1000, {},
                           con_historial=False),
        )),
        Condicion("C", (_tiempo(2, "completa", 16500, 20000, 40000, hilos=6),)),
    ]

    texto = informe(condiciones, cpu="Intel Core i7-1255U, 12 hilos")

    assert "Intel Core i7-1255U, 12 hilos" in texto
    assert "| A | primer turno | 2 | 1,0 s | 1,2 s | 1,2 s | 100 % | 3,0 s | 9,0 s |" in texto
    assert "| A | con historial | 1 | 2,5 s | 2,5 s | 2,5 s | 100 % | 4,0 s | 16,0 s |" in texto
    assert "| C | primer turno | 1 | 16,5 s | 16,5 s | 16,5 s | 0 % | 20,0 s | 40,0 s |" in texto
    assert "| C | con historial | 0 | — | — | — | — | — | — |" in texto
    assert "A: 1 respuesta(s) en `error` o `interrumpida` no cuentan." in texto
    assert "num_thread A: embeber 4, responder 4" in texto
    assert "num_thread C: embeber 6, responder 6" in texto
    assert "objetivo, no corte" in texto


def test_el_informe_avisa_si_num_thread_no_quedo_fijo():
    condiciones = [Condicion("A", (
        _tiempo(2, "completa", 1000, 2000, 3000, hilos=4),
        _tiempo(2, "completa", 1000, 2000, 3000, hilos=None),
    ))]

    texto = informe(condiciones, cpu="x")

    assert "num_thread A: embeber 4 / sin fijar, responder 4 / sin fijar" in texto
    assert "`num_thread` no quedó fijo" in texto


def test_el_comando_imprime_el_informe_de_cada_ventana(url_app, monkeypatch, capsys):
    monkeypatch.setenv("HV_BD_URL", url_app)

    codigo = main([
        "--cpu", "i7-1255U, 12 hilos",
        "--condicion", "A", "2000-01-01T10:00+00:00", "2000-01-01T11:00+00:00",
    ])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert "CPU: i7-1255U, 12 hilos" in salida
    assert "| A | primer turno | 0 | — |" in salida


def test_el_comando_rechaza_una_hora_sin_zona(url_app, monkeypatch, capsys):
    """Sin zona, Postgres la leería en la de la sesión y la ventana se correría."""
    monkeypatch.setenv("HV_BD_URL", url_app)

    codigo = main(["--cpu", "x", "--condicion", "A", "2000-01-01T10:00", "2000-01-01T11:00"])

    assert codigo == 2
    assert "zona horaria" in capsys.readouterr().err
