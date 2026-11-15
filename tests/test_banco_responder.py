"""El banco de `responder` (RF-28, DEC-132, R-14; ticket 078).

La puntuación es pura, con casos hechos a mano. `Banco.comparar` corre con la fila vigente de la
semilla, el `validar` real, la capa falsa por modelo y fragmentos fijos; el comando `banco
comparar --capacidad responder`, como `hv_app`, sobre un mini corpus de Postgres real indexado
con el `embeber` determinista de la capa falsa, así que los fragmentos salen de la búsqueda de la
Conversación. El último test corre contra un Ollama real (`-m real`) y se saltea sin él.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Mapping
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from capa_modelos_falsa import CapaModelosFalsa
from test_banco import (  # noqa: F401  (fixtures)
    CONSULTAS_CSV,
    PIEZAS_CSV,
    _referencia,
    _validada,
    acervo,
    fichas,
)
from test_banco_comparacion import MOMENTO, vigente  # noqa: F401  (fixture)
from test_busqueda import ACTAS, bd, indexacion, trabajador, validada  # noqa: F401

from historia_viva import composicion
from historia_viva.banco.comparacion import (
    Banco,
    CandidatoDeReferencia,
    escribir_comparacion,
    leer_candidatos,
    puntuar_respuesta,
)
from historia_viva.banco.referencia import (
    ConjuntoDeReferencia,
    ConsultaDeReferencia,
    PiezaDeReferencia,
    ReferenciaInvalida,
    cargar_referencia,
)
from historia_viva.bd import Bd
from historia_viva.capa_modelos.configuracion import ConfiguracionDeCapacidad, validar
from historia_viva.capa_modelos.contrato import (
    ETIQUETA_FRAGMENTO,
    BloqueDatos,
    CapaModelos,
    Capacidad,
    Limites,
    TramoDeRespuesta,
)
from historia_viva.capa_modelos.local import capa_para
from historia_viva.composicion import Entorno, main, motores
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.tipos import FiltroDeBusqueda

pytestmark = pytest.mark.rf("RF-28")

HECHOS = ("laboratorio de ensayos", "Consejo Académico", "1975")
SIN_EVIDENCIA = "No encontré evidencia suficiente en el Acervo."


# Puntuación de `responder`: sin modelo juez.


def test_las_citas_la_cobertura_y_las_invenciones_se_puntuan_con_casos_hechos_a_mano():
    bien = puntuar_respuesta(
        "El Consejo Académico creó el laboratorio de ensayos en 1975 [1][2].", 2, HECHOS, ()
    )
    ajena = puntuar_respuesta(
        "El Consejo Académico creó el laboratorio de ensayos en 1975 [1] [7].", 5, HECHOS, ()
    )
    parcial = puntuar_respuesta("Se creó un laboratorio de ensayos [1].", 5, HECHOS, ())
    inventa = puntuar_respuesta(
        "El Rectorado y el Consejo Académico crearon el laboratorio de ensayos en 1975 [1].", 5,
        HECHOS, ("Rectorado",),
    )
    sin_citas = puntuar_respuesta(
        "El Consejo Académico creó el laboratorio de ensayos en 1975.", 5, HECHOS, ()
    )

    assert (bien.cobertura, bien.citas_validas, bien.acierto) == (1.0, 2, True)
    # [7] no es ninguno de los 5 fragmentos recuperados: la pantalla la tacharía.
    assert [(c.marca, c.verificada) for c in ajena.citas or ()] == [(1, True), (7, False)]
    assert (ajena.cobertura, ajena.acierto) == (1.0, False)
    assert parcial.presentes == ("laboratorio de ensayos",)
    assert parcial.cobertura == pytest.approx(1 / 3) and not parcial.acierto
    assert (inventa.cobertura, inventa.invenciones, inventa.acierto) == (
        1.0, ("Rectorado",), False,
    )
    # Sin una Cita verificada la Conversación la muestra como «sin evidencia».
    assert sin_citas.citas == () and not sin_citas.acierto


def test_una_consulta_de_control_acierta_solo_si_declara_que_no_hay_evidencia():
    def control(texto: str) -> bool:
        return puntuar_respuesta(texto, 5, (), (), control=True).acierto

    assert control(SIN_EVIDENCIA)
    assert control(f"  {SIN_EVIDENCIA.rstrip('.')}\n")  # la regla de la Conversación
    assert not control("En 1950 se creó el laboratorio de ensayos [1].")
    assert not control(f"{SIN_EVIDENCIA} Aunque en 1950 se creó un laboratorio [1].")
    # La frase fija en una consulta con evidencia no acierta: no cubre nada.
    assert not puntuar_respuesta(SIN_EVIDENCIA, 5, HECHOS, ()).acierto


@pytest.mark.historia(168)
@pytest.mark.parametrize(
    ("cabecera", "fila", "error"),
    [
        ("consulta_id,texto,piezas_esperadas,conjunto", "c1,¿Qué convenio?,r1,prueba", None),
        (CONSULTAS_CSV.strip(), "c1,¿Y el de 1950?,,,,,prueba,true", None),
        (CONSULTAS_CSV.strip(), "c1,¿Y el de 1950?,,,,,prueba,sí", "«sin_evidencia» tiene que"),
        (CONSULTAS_CSV.strip(), "c1,¿Qué convenio?,r1,,,,prueba,1", "«sin_evidencia» tiene que"),
    ],
    ids=["sin-la-columna", "true", "si", "uno"],
)
def test_cargar_referencia_acepta_sin_evidencia_optativa_y_rechaza_lo_que_no_es_booleano(
    acervo, fichas, tmp_path, cabecera, fila, error
):
    directorio = _referencia(
        tmp_path / "referencia", piezas=PIEZAS_CSV.format(pieza=_validada(acervo, fichas)),
        consultas=f"{cabecera}\n{fila}\n",
    )

    if error:
        with pytest.raises(ReferenciaInvalida, match=error):
            cargar_referencia(acervo, directorio, fichas=fichas)
        return
    [consulta] = cargar_referencia(acervo, directorio, fichas=fichas).consultas
    assert consulta.sin_evidencia is fila.endswith("true")


# `Banco.comparar` con la capa falsa y fragmentos fijos.


class _CapaQueResponde(CapaModelosFalsa):
    """La capa falsa con una respuesta por pregunta, como si cada modelo redactara la suya."""

    def __init__(self, respuestas: Mapping[str, str]) -> None:
        super().__init__()
        self.respuestas = respuestas

    def responder_en_vivo(
        self,
        *,
        consulta: BloqueDatos,
        fragmentos: tuple[BloqueDatos, ...],
        limites: Limites | None = None,
    ) -> Generator[TramoDeRespuesta]:
        self.respuesta = self.respuestas.get(consulta.contenido, "")
        return super().responder_en_vivo(consulta=consulta, fragmentos=fragmentos,
                                         limites=limites)


class _Fabrica:
    """`capa_para` con una `_CapaQueResponde` por modelo de `responder`."""

    def __init__(self, respuestas: Mapping[str, Mapping[str, str]]) -> None:
        self.respuestas = respuestas
        self.capas: dict[str, _CapaQueResponde] = {}

    def __call__(
        self, filas: Mapping[Capacidad, ConfiguracionDeCapacidad], **_: Any
    ) -> CapaModelos:
        modelo = filas[Capacidad.RESPONDER].modelo or ""
        capa = _CapaQueResponde(self.respuestas.get(modelo, {}))
        self.capas[modelo] = capa
        return capa.para(filas)


QUE = "¿Quién creó el laboratorio de ensayos?"
CONTROL = "¿Qué pasó en la fiesta de egresados de 1950?"
SIN_FRAGMENTOS = "¿Y el acta perdida?"
RESPUESTAS = {
    "qwen2.5:3b-instruct-q4_K_M": {
        QUE: "El Consejo Académico creó el laboratorio de ensayos en 1975 [1].",
        CONTROL: SIN_EVIDENCIA,
    },
    "llama3:8b": {
        QUE: "El Consejo Académico creó el laboratorio de ensayos en 1975 [1].",
        CONTROL: "En 1950 hubo una fiesta con orquesta [2].",
    },
    "mistral:7b": {
        QUE: "El Consejo Académico creó el laboratorio de ensayos [4].",
        CONTROL: SIN_EVIDENCIA,
    },
}
"""qwen acierta las dos; llama inventa en la de control; mistral cita un fragmento ajeno."""

FRAGMENTOS = {
    QUE: ("Acta del Consejo Académico de 1975: se crea el laboratorio de ensayos.", "Otra acta."),
    CONTROL: ("Acta del laboratorio de hidráulica.", "Acta del laboratorio de química."),
}


def _conjunto() -> ConjuntoDeReferencia:
    pieza = PiezaDeReferencia("r1", "acta.pdf", None, "acta", HECHOS[:2], ("Rectorado",), None,
                              ("1975",))
    sin_hechos = PiezaDeReferencia("r2", "foto.jpg", None, "fotografia", (), (), None, ())

    def consulta(
        consulta_id: str, texto: str, *esperadas: str, control: bool = False,
        conjunto: str = "ajuste",
    ) -> ConsultaDeReferencia:
        return ConsultaDeReferencia(consulta_id, texto, esperadas, FiltroDeBusqueda(), conjunto,
                                    None, control)

    return ConjuntoDeReferencia("h", (pieza, sin_hechos), (
        consulta("c1", QUE, "r1"),
        consulta("c2", CONTROL, control=True),
        consulta("c3", "¿Qué muestra la foto?", "r2"),  # sin hechos: no se puntúa
        consulta("c4", SIN_FRAGMENTOS, "r1"),  # sin fragmentos: no se llama al modelo
        consulta("c5", "¿Y la fiesta de 1940?", control=True),  # de control, sin fragmentos
        consulta("c6", QUE, "r1", conjunto="prueba"),  # de la corrida oficial (F-9): no entra
    ))


def _candidato(modelo: str, chino: bool = False, **parametros: Any) -> CandidatoDeReferencia:
    return CandidatoDeReferencia(Capacidad.RESPONDER, modelo, chino, parametros)


def _banco(fabrica: _Fabrica) -> Banco:
    reloj = count(0.0, 0.25)
    return Banco(validar=partial(validar, motores=("ollama", "ollama_embeddings")),
                 fabrica_capa=fabrica, reloj=lambda: next(reloj),
                 recuperar=lambda consulta: FRAGMENTOS.get(consulta.texto, ()))


@pytest.mark.historia(169)
def test_cada_candidato_responde_las_consultas_con_los_fragmentos_recuperados(
    vigente, tmp_path
):
    fabrica = _Fabrica(RESPUESTAS)

    evaluaciones = _banco(fabrica).comparar(
        vigente, _conjunto(), tmp_path,
        [_candidato("qwen2.5:3b-instruct-q4_K_M", chino=True),
         _candidato("llama3:8b", num_ctx=8192, max_tokens_entrada=6568),
         _candidato("mistral:7b")],
        repeticiones=2, momento=MOMENTO,
    )

    qwen, llama, mistral = (e.resumen for e in evaluaciones)
    assert [(m.referencia_id, m.repeticion) for m in evaluaciones[0].mediciones] == [
        ("c1", 1), ("c2", 1), ("c1", 2), ("c2", 2)
    ]
    # La pregunta y sus fragmentos van como bloques, en el orden de la búsqueda: el primero es [1].
    invocacion = fabrica.capas["llama3:8b"].invocaciones[0]
    assert [(b.etiqueta, b.contenido) for b in invocacion.bloques[1:]] == [
        (ETIQUETA_FRAGMENTO, f) for f in FRAGMENTOS[QUE]
    ]
    assert invocacion.limites.tope_tokens_entrada == 6568
    assert (qwen.acierto, qwen.cobertura, qwen.citas_validas) == (1.0, 1.0, 1.0)
    assert (qwen.controles, qwen.controles_acertados) == (2, 2)
    # La de control no baja la cobertura; la Cita [2] de llama es de un fragmento recuperado.
    assert (llama.acierto, llama.cobertura, llama.citas_validas) == (0.5, 1.0, 1.0)
    assert llama.controles_acertados == 0
    # [4] no está entre los 2 fragmentos: desacierto aunque cubra lo esperado.
    assert (mistral.acierto, mistral.citas_validas, mistral.controles_acertados) == (0.5, 0.0, 2)

    rutas, tabla = escribir_comparacion(tmp_path / "corridas", Capacidad.RESPONDER, evaluaciones,
                                        hash_referencia="h", momento=MOMENTO)

    datos = json.loads(rutas[0].read_text(encoding="utf-8"))
    assert [(f["modelo"], f["ganador"], f["mejor_no_chino"]) for f in datos["tabla"]] == [
        ("llama3:8b", False, True), ("mistral:7b", False, False),
        ("qwen2.5:3b-instruct-q4_K_M", True, False),
    ]
    medicion = datos["evaluaciones"][2]["mediciones"][0]
    assert medicion["citas"] == [{"marca": 4, "verificada": False}]
    assert datos["evaluaciones"][0]["mediciones"][1]["sin_evidencia"] is True
    assert "| 100,0 % | 2 de 2 |" in tabla
    # Las que la búsqueda dejó sin fragmentos se avisan, también si son de control.
    assert datos["sin_fragmentos"] == [
        {"consulta_id": "c4", "control": False}, {"consulta_id": "c5", "control": True},
    ]
    assert "afuera de esta corrida:** c4, c5 (de control)." in tabla
    assert "queda propuesto el mejor no chino, llama3:8b" in tabla


def test_sin_consultas_de_ajuste_que_puntuar_la_referencia_no_sirve_para_responder(
    vigente, tmp_path
):
    banco = Banco(validar=partial(validar, motores=("ollama", "ollama_embeddings")),
                  fabrica_capa=_Fabrica(RESPUESTAS))  # sin búsqueda: ningún fragmento

    with pytest.raises(ReferenciaInvalida, match="no tiene consultas de ajuste que puntuar"):
        banco.comparar(vigente, _conjunto(), tmp_path, [_candidato("mistral:7b")],
                       momento=MOMENTO)


def test_la_lista_del_paquete_trae_a_llama_3_y_mistral_para_responder():
    candidatos = {c.modelo: c for c in leer_candidatos(Path("/no"))
                  if c.capacidad is Capacidad.RESPONDER}

    assert {m: c.origen_chino for m, c in candidatos.items()} == {
        "qwen2.5:3b-instruct-q4_K_M": True, "llama3:8b": False, "mistral:7b": False,
    }
    # LLaMA 3 tiene 8K de contexto: entrada + 600 de salida + 1024 de instrucción.
    assert candidatos["llama3:8b"].parametros == {"num_ctx": 8192, "max_tokens_entrada": 6568}


# El comando, como `hv_app`, con la búsqueda de la Conversación sobre Postgres real.


@pytest.mark.historia(169)
@pytest.mark.xdist_group("indice")
@pytest.mark.confirma("la app y el trabajador confirman desde sus propios pools")
def test_banco_comparar_responder_por_comando_recupera_con_la_busqueda_e_informa_ganador(
    monkeypatch, url_app, validada, tmp_path, capsys
):
    quimica = validada(ACTAS[2])  # «Acta del laboratorio de química, única sesión.»
    referencia = _referencia(
        tmp_path / "referencia",
        piezas="referencia_id,archivo,pieza_id,tipo,hechos_esperados,invenciones\n"
        f"r1,acta.pdf,{quimica},acta,laboratorio de química,Rectorado\n",
        consultas="consulta_id,texto,piezas_esperadas,conjunto,sin_evidencia\n"
        "c1,Laboratorio de química,r1,ajuste,\n"
        f"c2,{CONTROL},,ajuste,true\n"
        "c3,Laboratorio de química,r1,prueba,\n",  # de prueba: no entra (F-9)
    )
    fabrica = _Fabrica({
        "qwen2.5:3b-instruct-q4_K_M": {"Laboratorio de química": "Hubo un laboratorio de"
                                       " química [1].", CONTROL: SIN_EVIDENCIA},
        "llama3:8b": {"Laboratorio de química": "Sesionó el laboratorio de química [1].",
                      CONTROL: "Hubo una fiesta [1]."},
        "mistral:7b": {"Laboratorio de química": "Sesionó un acta [3].", CONTROL: "Hubo baile."},
    })
    monkeypatch.setattr(composicion, "capa_para", fabrica)
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(referencia))

    assert main(["banco", "comparar", "--capacidad", "responder", "--repeticiones", "1"]) == 0

    salida = capsys.readouterr()
    assert "Ganador: qwen2.5:3b-instruct-q4_K_M." in salida.out
    assert "queda propuesto el mejor no chino, llama3:8b" in salida.out
    assert "mistral:7b · c2 · repetición 1" in salida.err
    assert "· c3 ·" not in salida.err
    # El fragmento que vio cada modelo es el de la Ficha indexada, el que vería la Conversación.
    [_, *fragmentos] = fabrica.capas["mistral:7b"].invocaciones[0].bloques
    assert [f.contenido for f in fragmentos] == [ACTAS[2]]
    [corrida] = (referencia / "corridas").glob("comparar-responder-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    assert {e["modelo"] for e in datos["evaluaciones"]} == set(RESPUESTAS)
    assert all({"ganador", "mejor_no_chino"} <= set(f) for f in datos["tabla"])


# Con Ollama real (`-m real`).

_LOCAL = "http://localhost:11434"
_ACTA = (
    "Acta del Consejo Académico del 12 de marzo de 1975. Se resolvió crear el laboratorio de"
    " ensayos de materiales de la Facultad, a cargo del Ing. Juan Pérez."
)


@pytest.mark.real
@pytest.mark.historia(169)
def test_llama_3_y_mistral_corren_como_candidatos_de_responder_con_ollama_real(url_app, tmp_path):
    candidatos = [c for c in leer_candidatos(tmp_path) if c.capacidad is Capacidad.RESPONDER
                  and c.modelo in ("llama3:8b", "mistral:7b")]
    variables = {"HV_MOTOR_URL": _LOCAL, "HV_MOTOR_EMBEDDINGS_URL": _LOCAL, **os.environ}
    motores_ = motores(Entorno.desde_variables(variables))
    faltan = motores_["ollama"].faltantes([c.modelo for c in candidatos])
    if faltan is None or faltan:
        pytest.skip("Necesita un Ollama real con llama3:8b y mistral:7b descargados.")
    bd_ = Bd(url_app, tamano_pool=1)
    try:
        vigente_ = ConfiguracionDeModelos(bd=bd_, validar=lambda _: None).vigente()
    finally:
        bd_.cerrar()
    banco = Banco(validar=partial(validar, motores=motores_),
                  fabrica_capa=partial(capa_para, motores=motores_),
                  recuperar=lambda consulta: (_ACTA,))
    pieza = PiezaDeReferencia("r1", "acta.txt", None, "acta",
                              ("laboratorio de ensayos", "1975"), ("Rectorado",), None, ())
    conjunto = ConjuntoDeReferencia("h", (pieza,), (
        ConsultaDeReferencia("c1", "¿Cuándo se creó el laboratorio de ensayos?", ("r1",),
                             FiltroDeBusqueda(), "ajuste", None, False),
        ConsultaDeReferencia("c2", CONTROL, (), FiltroDeBusqueda(), "ajuste", None, True),
    ))

    evaluaciones = banco.comparar(vigente_, conjunto, tmp_path, candidatos, momento=MOMENTO)

    assert [e.candidato.modelo for e in evaluaciones] == ["llama3:8b", "mistral:7b"]
    for e in evaluaciones:
        assert len(e.mediciones) == 6
        assert all(m.error is None and m.tiempo_ms > 0 and m.tokens_salida > 0
                   for m in e.mediciones)
    rutas, tabla = escribir_comparacion(tmp_path / "corridas", Capacidad.RESPONDER, evaluaciones,
                                        hash_referencia="h", momento=MOMENTO)
    datos = json.loads(rutas[0].read_text(encoding="utf-8"))
    [ganador] = [f for f in datos["tabla"] if f["ganador"]]
    assert ganador["mejor_no_chino"]  # los dos son no chinos: coinciden
    print(tabla)
