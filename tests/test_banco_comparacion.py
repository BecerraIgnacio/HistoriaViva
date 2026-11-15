"""La comparación de modelos del banco (RF-28, DEC-132, R-39; ticket 076).

La puntuación y la elección del ganador y del mejor no chino son funciones puras; `Banco.comparar`
corre con la fila vigente de la semilla, el `validar` real y la capa falsa por modelo; el comando
`banco comparar`, como `hv_app`. Los dos últimos tests corren contra un Ollama real (`-m real`) y
se saltean sin él: LLaMA 3 8B y Mistral 7B como candidatos de `resumir`, y la paridad con el banco
de visión del 7-oct.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any

import capa_modelos_falsa
import pytest
from capa_modelos_falsa import (
    DESCRIPCION_FIJA,
    IMAGEN,
    TEXTO_VISIBLE_FIJO,
    CapaModelosFalsa,
)
from test_banco import acervo, fichas  # noqa: F401  (fixtures)

from historia_viva import composicion
from historia_viva.banco import percentil
from historia_viva.banco.comparacion import (
    Banco,
    CandidatoDeReferencia,
    CandidatoInvalido,
    Resumen,
    elegir_ganador_y_no_chino,
    escribir_comparacion,
    leer_candidatos,
    puntuar,
)
from historia_viva.banco.referencia import ConjuntoDeReferencia, PiezaDeReferencia, cargar_referencia
from historia_viva.bd import Bd
from historia_viva.capa_modelos.configuracion import ConfiguracionDeCapacidad, validar
from historia_viva.capa_modelos.contrato import CapaModelos, Capacidad
from historia_viva.capa_modelos.local import capa_para
from historia_viva.composicion import Entorno, main, motores
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.procesamiento.manejador import cuerpo_de_la_foto


@pytest.fixture(autouse=True)
def describir_en_prosa(monkeypatch) -> None:
    """El banco se prueba con la descripción en prosa de la capa falsa (`DESCRIPCION_FIJA`),
    aunque la fila vigente pida Afirmaciones (144): esas las prueba `test_afirmaciones`."""
    monkeypatch.setattr(capa_modelos_falsa.entradas, "esquema_de", lambda plantilla: None)

pytestmark = pytest.mark.rf("RF-28")

MOMENTO = datetime(2026, 10, 19, 10, 0).astimezone()


# Puntuación de `resumir` (y de `describir_imagen`): sin modelo juez.


def test_un_hecho_presente_cuenta_sin_tildes_ni_mayusculas_y_por_palabras_enteras():
    puntaje = puntuar(
        "El CONSEJO ACADEMICO creó el laboratorio de ensayos; fotos en colores.",
        ("Consejo Académico", "laboratorio de ensayos", "color"),
        (),
    )

    assert puntaje.presentes == ("Consejo Académico", "laboratorio de ensayos")
    assert puntaje.faltan == ("color",)  # «colores» no es «color»
    assert puntaje.cobertura == pytest.approx(2 / 3)
    assert puntaje.acierto


def test_una_invencion_de_la_lista_hace_desacierto_aunque_cubra_todo():
    puntaje = puntuar("El Rectorado creó el laboratorio en 1975.", ("laboratorio", "1975"),
                      ("Rectorado", "vehículo"))

    assert puntaje.cobertura == 1.0
    assert puntaje.invenciones == ("Rectorado",)
    assert not puntaje.acierto


def test_los_datos_clave_cuentan_como_lo_esperado_y_bajo_el_umbral_no_acierta():
    pieza = PiezaDeReferencia("r1", "acta.txt", None, None, ("laboratorio de ensayos",), (), None,
                              ("1975", "Ing. Pérez"))

    puntaje = puntuar("Acta de 1975 sobre un laboratorio.",
                      (*pieza.hechos_esperados, *pieza.datos_clave), pieza.invenciones)

    assert puntaje.presentes == ("1975",)
    assert puntaje.faltan == ("laboratorio de ensayos", "Ing. Pérez")
    assert not puntaje.acierto  # 1/3 < 60 %


def test_una_salida_vacia_o_sin_hechos_medibles_no_es_acierto():
    assert not puntuar("", (), ("Pérez",)).acierto
    assert not puntuar("cualquier cosa", (), ()).acierto
    assert not puntuar("", ("laboratorio",), ()).acierto
    assert puntuar("", (), ()).cobertura == 0.0


# Ganador y mejor no chino: una función pura.


def _resumen(modelo: str, *, chino: bool, aciertos: int = 0, errores: int = 0,
             cobertura: float | None = 0.5, tiempo: float | None = 1000.0) -> Resumen:
    return Resumen(
        modelo=modelo, origen_chino=chino, digest=f"d-{modelo}", corrida="2026-10-19",
        mediciones=3, aciertos=aciertos, errores=errores, cobertura=cobertura,
        con_invenciones=0, tiempo_mediano_ms=tiempo, tiempo_p95_ms=tiempo, tokens_entrada=1.0,
        tokens_salida=1.0, tope_s=90,
    )


def test_si_gana_un_chino_se_marca_ademas_el_mejor_no_chino():
    filas = [_resumen("qwen", chino=True, aciertos=3), _resumen("llama", chino=False, aciertos=2),
             _resumen("mistral", chino=False, aciertos=1)]

    assert elegir_ganador_y_no_chino(filas) == (0, 1)


def test_si_gana_un_no_chino_es_tambien_el_mejor_no_chino():
    filas = [_resumen("qwen", chino=True, aciertos=1), _resumen("llama", chino=False, aciertos=3)]

    assert elegir_ganador_y_no_chino(filas) == (1, 1)


def test_sin_candidato_no_chino_no_hay_mejor_no_chino():
    filas = [_resumen("qwen", chino=True, aciertos=1), _resumen("qwen-vl", chino=True)]

    assert elegir_ganador_y_no_chino(filas) == (0, None)


def test_un_candidato_que_no_llego_a_medir_no_compite():
    filas = [_resumen("llama", chino=False, errores=3, cobertura=None, tiempo=None),
             _resumen("qwen", chino=True)]

    assert elegir_ganador_y_no_chino(filas) == (1, None)
    assert elegir_ganador_y_no_chino(filas[:1]) == (None, None)


def test_los_empates_se_desatan_por_cobertura_tiempo_y_nombre_sin_depender_del_orden():
    mas_cobertura = [_resumen("b", chino=False, aciertos=2, cobertura=0.9),
                     _resumen("a", chino=False, aciertos=2, cobertura=0.8)]
    mas_rapido = [_resumen("b", chino=False, aciertos=2, tiempo=900.0),
                  _resumen("a", chino=False, aciertos=2, tiempo=1000.0)]
    iguales = [_resumen("b", chino=True, aciertos=2), _resumen("a", chino=True, aciertos=2)]

    assert elegir_ganador_y_no_chino(mas_cobertura)[0] == 0
    assert elegir_ganador_y_no_chino(mas_rapido)[0] == 0
    assert elegir_ganador_y_no_chino(iguales) == (1, None)
    assert elegir_ganador_y_no_chino(iguales[::-1]) == (0, None)


# La lista de candidatos (R-39).


def test_la_lista_del_paquete_trae_a_llama_3_y_mistral_como_no_chinos_y_a_los_vigentes():
    candidatos = {(c.capacidad, c.modelo): c.origen_chino for c in leer_candidatos(Path("/no"))}

    assert candidatos[Capacidad.RESUMIR, "llama3:8b"] is False
    assert candidatos[Capacidad.RESUMIR, "mistral:7b"] is False
    assert candidatos[Capacidad.RESUMIR, "qwen2.5:3b-instruct-q4_K_M"] is True
    assert candidatos[Capacidad.DESCRIBIR_IMAGEN, "qwen3-vl:2b-instruct"] is True
    assert candidatos[Capacidad.DESCRIBIR_IMAGEN, "gemma3:4b"] is False


def test_la_lista_de_hv_referencia_manda_y_se_valida(tmp_path):
    (tmp_path / "candidatos.csv").write_text(
        "capacidad,modelo,origen_chino,parametros\n"
        'resumir,m1,false,"{""num_ctx"": 4096}"\n'
        "embeber_mal,m2,false,\n"
        "resumir,m3,quizas,[1]\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception) as error:
        leer_candidatos(tmp_path)

    assert "capacidad «embeber_mal» desconocida" in str(error.value)
    assert "«origen_chino» tiene que ser true o false" in str(error.value)
    assert "«parametros» tiene que ser un objeto JSON" in str(error.value)


# `Banco.comparar`: el candidato pasa por `validar` y por `capa_para`.

TEXTOS = {
    "qwen2.5:3b-instruct-q4_K_M": "El Consejo Académico creó el laboratorio de ensayos en 1975.",
    "llama3:8b": "En 1975 el Rectorado creó el laboratorio de ensayos.",
    "mistral:7b": "El Consejo Académico creó un laboratorio.",
}
"""qwen cubre todo; llama cubre 2 de 3 pero inventa el Rectorado; mistral cubre 1 de 3."""

PIEZAS_CSV = (
    "referencia_id,archivo,pieza_id,tipo,hechos_esperados,invenciones,texto_esperado,datos_clave\n"
    "r1,acta.txt,,acta,laboratorio de ensayos;Consejo Académico,Rectorado,"
    "El Consejo Académico creó el laboratorio de ensayos en 1975.,1975\n"
    "r2,foto.jpg,,fotografia,,,,\n"
)


def _referencia(directorio: Path, consultas: str = "") -> Path:
    directorio.mkdir(exist_ok=True)
    (directorio / "acta.txt").write_text("Acta.", encoding="utf-8")
    (directorio / "foto.jpg").write_bytes(b"\xff\xd8")
    (directorio / "piezas.csv").write_text(PIEZAS_CSV, encoding="utf-8")
    (directorio / "consultas.csv").write_text(
        "consulta_id,texto,piezas_esperadas,conjunto,sin_evidencia\n" + consultas, encoding="utf-8"
    )
    return directorio


class _Fabrica:
    """`capa_para` con la capa falsa: cada modelo resume con su texto, y registra las filas."""

    def __init__(self) -> None:
        self.filas: list[Mapping[Capacidad, ConfiguracionDeCapacidad]] = []
        self.capas: list[CapaModelosFalsa] = []

    def __call__(
        self, filas: Mapping[Capacidad, ConfiguracionDeCapacidad], **_: Any
    ) -> CapaModelos:
        self.filas.append(filas)
        capa = CapaModelosFalsa().para(filas)
        capa.resumen = TEXTOS.get(filas[Capacidad.RESUMIR].modelo or "", "")
        self.capas.append(capa)
        return capa


def _candidato(modelo: str, chino: bool = False, **parametros: Any) -> CandidatoDeReferencia:
    return CandidatoDeReferencia(Capacidad.RESUMIR, modelo, chino, parametros)


@pytest.fixture
def vigente(acervo) -> dict[Capacidad, ConfiguracionDeCapacidad]:
    return ConfiguracionDeModelos(bd=None, validar=lambda _: None).vigente(acervo)  # type: ignore[arg-type]


def _banco(fabrica: _Fabrica) -> Banco:
    reloj = count(0.0, 0.25)
    return Banco(validar=partial(validar, motores=("ollama", "ollama_embeddings")),
                 fabrica_capa=fabrica, reloj=lambda: next(reloj))


def test_cada_candidato_corre_con_su_fila_validada_tres_veces_y_guarda_tiempo_y_tokens(
    acervo, fichas, vigente, tmp_path
):
    referencia = cargar_referencia(acervo, _referencia(tmp_path), fichas=fichas)
    fabrica = _Fabrica()

    llama, qwen = _banco(fabrica).comparar(
        vigente, referencia, tmp_path,
        [_candidato("llama3:8b", num_ctx=8192, max_tokens_entrada=6868),
         _candidato("qwen2.5:3b-instruct-q4_K_M", chino=True)],
        momento=MOMENTO,
    )

    fila = fabrica.filas[0][Capacidad.RESUMIR]
    assert fila.modelo == "llama3:8b"
    assert fila.parametros == {**vigente[Capacidad.RESUMIR].parametros, "num_ctx": 8192,
                               "max_tokens_entrada": 6868}
    assert {c: f for c, f in fabrica.filas[0].items() if c is not Capacidad.RESUMIR} == {
        c: f for c, f in vigente.items() if c is not Capacidad.RESUMIR
    }
    # La capa corre con los topes del candidato, no con los de la fila vigente.
    [invocacion, *_] = fabrica.capas[0].invocaciones
    assert invocacion.limites.tope_tokens_entrada == 6868
    # Solo r1 tiene qué puntuar; tres repeticiones; el reloj avanza 250 ms por lectura.
    assert [(m.referencia_id, m.repeticion) for m in llama.mediciones] == [
        ("r1", 1), ("r1", 2), ("r1", 3)
    ]
    assert {(m.tiempo_ms, m.tokens_entrada, m.tokens_salida) for m in llama.mediciones} == {
        (250, 400, 40)
    }
    por_capacidad = llama.configuracion_efectiva["por_capacidad"]
    assert por_capacidad["resumir"]["modelo"] == "llama3:8b"
    assert llama.configuracion_efectiva["digest"] != qwen.configuracion_efectiva["digest"]
    assert (llama.resumen.acierto, llama.resumen.con_invenciones) == (0.0, 3)
    assert (qwen.resumen.acierto, qwen.resumen.cobertura) == (1.0, 1.0)


def test_un_candidato_que_no_valida_no_corre_y_tampoco_los_demas(
    acervo, fichas, vigente, tmp_path
):
    referencia = cargar_referencia(acervo, _referencia(tmp_path), fichas=fichas)
    fabrica = _Fabrica()

    with pytest.raises(CandidatoInvalido, match="mistral:7b.*\n.*num_ctx"):
        _banco(fabrica).comparar(
            vigente, referencia, tmp_path,
            [_candidato("llama3:8b"), _candidato("mistral:7b", num_ctx=100)], momento=MOMENTO,
        )

    assert all(not capa.invocaciones for capa in fabrica.capas)


def test_describir_imagen_compara_solo_las_imagenes_con_algo_que_puntuar(
    acervo, fichas, vigente, tmp_path
):
    referencia = cargar_referencia(acervo, _referencia(tmp_path), fichas=fichas)
    candidato = CandidatoDeReferencia(Capacidad.DESCRIBIR_IMAGEN, "gemma3:4b", False, {})

    with pytest.raises(Exception, match="no tiene Piezas que puntuar para describir_imagen"):
        _banco(_Fabrica()).comparar(vigente, referencia, tmp_path, [candidato], momento=MOMENTO)

    con_hechos = replace(referencia, piezas=(
        replace(referencia.piezas[1], hechos_esperados=("grupo de personas",),
                invenciones=("colores",)),
    ))
    [evaluacion] = _banco(_Fabrica()).comparar(
        vigente, con_hechos, tmp_path, [candidato], repeticiones=1, momento=MOMENTO
    )
    assert [(m.referencia_id, m.puntaje and m.puntaje.acierto) for m in evaluacion.mediciones] == [
        ("r2", True)
    ]


def test_una_pieza_con_solo_invenciones_no_se_compara(acervo, fichas, vigente, tmp_path):
    referencia = cargar_referencia(acervo, _referencia(tmp_path), fichas=fichas)
    solo_invenciones = replace(referencia, piezas=(
        replace(referencia.piezas[0], hechos_esperados=(), datos_clave=()),
    ))

    with pytest.raises(Exception, match="hacen falta hechos_esperados o datos_clave"):
        _banco(_Fabrica()).comparar(vigente, solo_invenciones, tmp_path,
                                    [_candidato("llama3:8b")], momento=MOMENTO)


def test_resumir_una_foto_sin_texto_esperado_resume_descripcion_y_texto_visible(
    acervo, fichas, vigente, tmp_path
):
    referencia = cargar_referencia(acervo, _referencia(tmp_path), fichas=fichas)
    (tmp_path / "foto.jpg").write_bytes(IMAGEN)
    foto = replace(referencia, piezas=(
        replace(referencia.piezas[1], hechos_esperados=("Facultad",)),
    ))
    fabrica = _Fabrica()

    _banco(fabrica).comparar(vigente, foto, tmp_path, [_candidato("llama3:8b")],
                             repeticiones=1, momento=MOMENTO)

    [resumen] = [i for c in fabrica.capas for i in c.invocaciones
                 if i.capacidad is Capacidad.RESUMIR]
    assert resumen.bloques[0].contenido == cuerpo_de_la_foto(DESCRIPCION_FIJA, TEXTO_VISIBLE_FIJO)


# La salida: solo se comparan corridas con la misma huella.


def _evaluaciones(acervo, fichas, vigente, directorio: Path, *modelos: tuple[str, bool]):
    referencia = cargar_referencia(acervo, directorio, fichas=fichas)
    evaluaciones = _banco(_Fabrica()).comparar(
        vigente, referencia, directorio, [_candidato(m, c) for m, c in modelos], momento=MOMENTO
    )
    return referencia.hash_referencia, evaluaciones


def test_la_tabla_junta_las_corridas_de_la_misma_huella_y_deja_afuera_las_otras(
    acervo, fichas, vigente, tmp_path
):
    directorio, salida = _referencia(tmp_path / "referencia"), tmp_path / "corridas"
    huella, primeras = _evaluaciones(acervo, fichas, vigente, directorio, ("llama3:8b", False),
                                     ("mistral:7b", False))
    escribir_comparacion(salida, Capacidad.RESUMIR, primeras, hash_referencia=huella,
                         momento=MOMENTO)
    otra = _referencia(tmp_path / "otra", consultas="c1,¿Qué?,,ajuste,true\n")
    otra_huella, de_otra = _evaluaciones(acervo, fichas, vigente, otra, ("llama3:8b", False))
    escribir_comparacion(salida, Capacidad.RESUMIR, de_otra, hash_referencia=otra_huella,
                         momento=MOMENTO + timedelta(minutes=1))

    _, segundas = _evaluaciones(acervo, fichas, vigente, directorio,
                                ("qwen2.5:3b-instruct-q4_K_M", True))
    rutas, tabla = escribir_comparacion(salida, Capacidad.RESUMIR, segundas,
                                        hash_referencia=huella,
                                        momento=MOMENTO + timedelta(minutes=2))

    datos = json.loads(rutas[0].read_text(encoding="utf-8"))
    assert datos["hash_referencia"] == huella
    assert datos["corridas_afuera"] == 1
    assert [(f["modelo"], f["ganador"], f["mejor_no_chino"]) for f in datos["tabla"]] == [
        ("llama3:8b", False, True), ("mistral:7b", False, False),
        ("qwen2.5:3b-instruct-q4_K_M", True, False),
    ]
    [evaluacion] = datos["evaluaciones"]
    assert evaluacion["configuracion_efectiva"]["por_capacidad"]["resumir"]["modelo"] == (
        "qwen2.5:3b-instruct-q4_K_M"
    )
    assert [m["acierto"] for m in evaluacion["mediciones"]] == [True] * 3
    assert "queda propuesto el mejor no chino, llama3:8b" in tabla
    assert "1 corrida(s) anteriores con otra huella" in tabla
    cabecera, *filas = rutas[1].read_text(encoding="utf-8-sig").splitlines()
    assert cabecera.startswith("modelo,origen_chino,acierto") and cabecera.endswith(
        "hash_referencia"
    )
    assert len(filas) == 3 and all(f.endswith(huella) for f in filas)
    assert rutas[2].read_text(encoding="utf-8") == tabla


def test_toda_celda_de_la_comparacion_pasa_por_la_celda_segura(tmp_path):
    fila = _resumen("=HYPERLINK(1)|x", chino=False, aciertos=1)
    (tmp_path / "comparar-resumir-1.json").write_text(json.dumps({
        "hash_referencia": "h", "evaluaciones": [{"resumen": fila.__dict__}],
    }), encoding="utf-8")

    rutas, tabla = escribir_comparacion(tmp_path, Capacidad.RESUMIR, [], hash_referencia="h",
                                        momento=MOMENTO)

    assert "'=HYPERLINK(1)\\|x" in tabla
    assert "'=HYPERLINK(1)|x" in rutas[1].read_text(encoding="utf-8-sig")
    assert "El ganador no es de origen chino" in tabla


# El comando, como `hv_app`.


@pytest.mark.historia(169)
def test_banco_comparar_por_comando_informa_ganador_y_mejor_no_chino(
    monkeypatch, url_app, tmp_path, capsys
):
    referencia = _referencia(tmp_path / "referencia")
    monkeypatch.setattr(composicion, "capa_para", _Fabrica())
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(referencia))

    assert main(["banco", "comparar", "--capacidad", "resumir", "--modelo", "gpt-9"]) == 1
    assert "No están en candidatos.csv para resumir: gpt-9" in capsys.readouterr().err
    assert main(["banco", "comparar", "--capacidad", "resumir", "--repeticiones", "1"]) == 0

    salida = capsys.readouterr()
    assert "Ganador: qwen2.5:3b-instruct-q4_K_M." in salida.out
    assert "queda propuesto el mejor no chino, llama3:8b" in salida.out
    assert "llama3:8b · r1 · repetición 1" in salida.err
    [corrida] = (referencia / "corridas").glob("comparar-resumir-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    assert {e["modelo"] for e in datos["evaluaciones"]} == set(TEXTOS)


# Con Ollama real (`-m real`).

_LOCAL = "http://localhost:11434"


def _con_ollama(url_app: str, modelos: list[str]) -> tuple[Banco, dict[Capacidad, Any]]:
    variables = {"HV_MOTOR_URL": _LOCAL, "HV_MOTOR_EMBEDDINGS_URL": _LOCAL, **os.environ}
    motores_ = motores(Entorno.desde_variables(variables))
    faltan = motores_["ollama"].faltantes(modelos)
    if faltan is None or faltan:
        pytest.skip(f"Necesita un Ollama real con {', '.join(modelos)} descargados.")
    bd = Bd(url_app, tamano_pool=1)
    try:
        vigente = ConfiguracionDeModelos(bd=bd, validar=lambda _: None).vigente()
    finally:
        bd.cerrar()
    banco = Banco(validar=partial(validar, motores=motores_),
                  fabrica_capa=partial(capa_para, motores=motores_))
    return banco, vigente


@pytest.mark.real
@pytest.mark.historia(169)
def test_llama_3_y_mistral_corren_como_candidatos_de_resumir_con_ollama_real(url_app, tmp_path):
    candidatos = [c for c in leer_candidatos(tmp_path) if c.capacidad is Capacidad.RESUMIR
                  and c.modelo in ("llama3:8b", "mistral:7b")]
    banco, vigente = _con_ollama(url_app, [c.modelo for c in candidatos])
    pieza = PiezaDeReferencia(
        "r1", "acta.txt", None, "acta", ("laboratorio de ensayos", "Consejo Académico"),
        ("Rectorado",),
        "Acta del Consejo Académico del 12 de marzo de 1975. Se resolvió crear el laboratorio de"
        " ensayos de materiales de la Facultad, a cargo del Ing. Juan Pérez.",
        ("1975", "Juan Pérez"),
    )

    evaluaciones = banco.comparar(vigente, ConjuntoDeReferencia("h", (pieza,), ()), tmp_path,
                                  candidatos, momento=MOMENTO)

    assert [e.candidato.modelo for e in evaluaciones] == ["llama3:8b", "mistral:7b"]
    for e in evaluaciones:
        assert len(e.mediciones) == 3
        assert all(m.error is None and m.tiempo_ms > 0 and m.tokens_salida > 0
                   for m in e.mediciones)
    rutas, tabla = escribir_comparacion(tmp_path / "corridas", Capacidad.RESUMIR, evaluaciones,
                                        hash_referencia="h", momento=MOMENTO)
    datos = json.loads(rutas[0].read_text(encoding="utf-8"))
    [ganador] = [f for f in datos["tabla"] if f["ganador"]]
    assert ganador["mejor_no_chino"]  # los dos son no chinos: coinciden
    print(tabla)


_RAIZ = Path(__file__).resolve().parents[2]
_FOTOS = _RAIZ / "docs" / "presentacion" / "piezas-demo"
_BANCO_DE_VISION = _RAIZ / "docs" / "mediciones" / "vision-2026-10-07-1724.json"
TOLERANCIA = 0.15


@pytest.mark.real
@pytest.mark.historia(169)
def test_la_paridad_reproduce_el_banco_de_vision_dentro_del_15_por_ciento(url_app, tmp_path):
    modelo = "qwen3-vl:2b-instruct"
    if not _BANCO_DE_VISION.is_file() or not _FOTOS.is_dir():
        pytest.skip("Paridad informativa: no están los resultados guardados ni las fotos demo.")
    guardados = {
        c["foto"]: c["pared_s"]
        for c in json.loads(_BANCO_DE_VISION.read_text(encoding="utf-8"))["corridas"]
        if c["modelo"] == modelo and "pared_s" in c
    }
    banco, vigente = _con_ollama(url_app, [modelo])
    piezas = tuple(
        PiezaDeReferencia(foto, foto, None, "fotografia", hechos, (), None, ())
        for foto, hechos in (
            ("cadetes-colegio-militar-c1930.jpg", ("blanco y negro", "uniformes")),
            ("memoria-guerra-1912-estado-mayor.jpg", ("Estado Mayor",)),
        )
        if foto in guardados
    )
    if not piezas:
        pytest.skip(f"Paridad informativa: el banco de visión no guardó {modelo}.")
    candidato = CandidatoDeReferencia(Capacidad.DESCRIBIR_IMAGEN, modelo, True, {})

    [evaluacion] = banco.comparar(vigente, ConjuntoDeReferencia("h", piezas, ()), _FOTOS,
                                  [candidato], momento=MOMENTO)

    medidos = {
        p.referencia_id: percentil(
            [m.tiempo_ms / 1000 for m in evaluacion.mediciones if m.referencia_id == p.referencia_id
             and m.error is None], 50)
        for p in piezas
    }
    print({foto: (medidos[foto], guardados[foto]) for foto in medidos})
    for foto, medido in medidos.items():
        assert medido is not None
        assert abs(medido - guardados[foto]) <= TOLERANCIA * guardados[foto], foto
    assert all(m.puntaje and m.puntaje.acierto for m in evaluacion.mediciones)
