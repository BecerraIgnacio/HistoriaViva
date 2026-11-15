"""El banco de ingesta (ticket 137; RF-07, RF-08, RF-28; DEC-132).

Las medidas son funciones puras; `medir_ingesta` corre sobre el corpus de prueba del repo
(`corpus_de_ingesta/`: el cartel de Cyber.AR y una página impresa) con la capa falsa y un reloj
fijo; el comando `banco ingesta`, como `hv_app`. La corrida con el modelo real la hace una
persona en la PC (`banco ingesta --corpus tests/corpus_de_ingesta --hardware "…"`).
"""

from __future__ import annotations

import json
import shutil
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any

import pytest
import capa_modelos_falsa
from capa_modelos_falsa import DESCRIPCION_FIJA, TEXTO_VISIBLE_FIJO, CapaModelosFalsa
from test_banco import acervo  # noqa: F401  (fixture)

from historia_viva import composicion
from historia_viva.banco.comparacion import Banco, CandidatoDeReferencia
from historia_viva.banco.ingesta import (
    CorridaDeIngesta,
    elegir,
    frases_sin_coincidencia,
    leer_corpus,
    lineas_basura,
    medir_ingesta,
    tabla_ingesta,
    tasa_de_error,
)
from historia_viva.banco.referencia import ReferenciaInvalida
from historia_viva.capa_modelos.configuracion import validar
from historia_viva.capa_modelos.contrato import Capacidad
from historia_viva.composicion import main
from historia_viva.configuracion import ConfiguracionDeModelos


@pytest.fixture(autouse=True)
def describir_en_prosa(monkeypatch) -> None:
    """El banco se prueba con la descripción en prosa de la capa falsa (`DESCRIPCION_FIJA`),
    aunque la fila vigente pida Afirmaciones (144): esas las prueba `test_afirmaciones`."""
    monkeypatch.setattr(capa_modelos_falsa.entradas, "esquema_de", lambda plantilla: None)

pytestmark = pytest.mark.rf("RF-28")

CORPUS = Path(__file__).parent / "corpus_de_ingesta"
CARTEL, PAGINA = "cartel-cyber-ar-2026.jpg", "memoria-guerra-1912-estado-mayor.jpg"


# Las medidas, puras.


@pytest.mark.rf("RF-07")
def test_la_tasa_de_error_es_la_distancia_de_edicion_sobre_la_referencia():
    assert tasa_de_error("Cyber.AR  2026", "Cyber.AR\n2026") == 0.0  # los espacios se colapsan
    assert tasa_de_error("| CONGRESO", "I CONGRESO") == pytest.approx(1 / 10)
    assert tasa_de_error("", "abcd") == 1.0
    assert tasa_de_error("algo", "  ") is None


@pytest.mark.rf("RF-07")
def test_una_linea_basura_es_una_linea_sin_letras():
    assert lineas_basura("CRONOGRAMA\n——— 0\n\n> ==\n18:30 - 21:45 hs") == pytest.approx(2 / 4)
    assert lineas_basura("  \n") is None


def test_las_frases_sin_hecho_esperado_se_listan_para_revisar():
    texto = ("El cartel anuncia el HACKATHON Cyber.AR 2026. Varias personas con ropa formal"
             " trabajan en una oficina.\nAv. Cabildo 15")

    assert frases_sin_coincidencia(texto, ("Hackathon Cyber.AR 2026", "Av. Cabildo 15")) == (
        "Varias personas con ropa formal trabajan en una oficina.",
    )
    # 139: la clase con que empieza la respuesta no es una frase que revisar.
    assert frases_sin_coincidencia("Cartel o gráfico. Tiene un logo.\nOtra", ()) == (
        "Tiene un logo.", "Otra",
    )


# El corpus.


def test_el_corpus_de_prueba_trae_el_cartel_y_una_pagina_impresa_con_su_etiqueta():
    cartel, pagina = leer_corpus(CORPUS)

    assert (cartel.archivo, pagina.archivo) == (CARTEL, PAGINA)
    assert {"Hackathon Cyber.AR 2026", "I Congreso de Ciberdefensa", "Av. Cabildo 15",
            "Facultad Militar Conjunta"} <= set(cartel.hechos_esperados)  # está en un logo
    # El cartel tiene pictogramas de personas y laptops: «personas» o «pantalla» solas
    # castigarían una descripción correcta.
    assert {"personas vestidas con ropa formal", "entorno de oficina",
            "Universidad Nacional de Educación a Distancia"} <= set(cartel.invenciones)
    assert cartel.transcripcion and "info.cyber.ar@fie.undef.edu.ar" in cartel.transcripcion
    assert pagina.transcripcion and "Estado Mayor del Ejército" in pagina.transcripcion


def test_una_etiqueta_mala_se_rechaza_con_todos_sus_problemas(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "etiquetas.json").write_text(json.dumps([
        {"archivo": "../afuera.jpg"},
        {"archivo": "a.jpg", "hechos_esperados": "no es lista", "transcripcion": "falta.txt"},
    ]), encoding="utf-8")

    with pytest.raises(ReferenciaInvalida) as error:
        leer_corpus(tmp_path)

    assert error.value.errores == (
        "etiquetas.json, Pieza 1: falta el archivo «../afuera.jpg» en el directorio.",
        "etiquetas.json, Pieza 2: «hechos_esperados» tiene que ser una lista de textos.",
        "etiquetas.json, Pieza 2: falta la transcripción «falta.txt» en el directorio.",
    )
    with pytest.raises(ReferenciaInvalida, match="Falta etiquetas.json"):
        leer_corpus(tmp_path / "otro")


# La corrida, con la capa falsa: números deterministas.


@pytest.fixture
def pagina_leida(monkeypatch) -> None:
    """La capa falsa lee la página impresa tal cual su transcripción; el cartel, con el texto
    visible fijo."""
    pagina = (CORPUS / PAGINA).read_bytes()
    texto = (CORPUS / "memoria-guerra-1912-estado-mayor.txt").read_text(encoding="utf-8")
    monkeypatch.setitem(capa_modelos_falsa._TEXTO_VISIBLE_POR_IMAGEN, pagina, texto)


def _corrida(
    acervo: Any, modelo: str, chino: bool, paso_s: float, corpus: Path = CORPUS
) -> CorridaDeIngesta:
    vigente = ConfiguracionDeModelos(bd=None, validar=lambda _: None).vigente(acervo)  # type: ignore[arg-type]
    banco = Banco(validar=partial(validar, motores=("ollama", "ollama_embeddings")),
                  fabrica_capa=CapaModelosFalsa().para)
    candidato = CandidatoDeReferencia(Capacidad.DESCRIBIR_IMAGEN, modelo, chino, {})
    capa, efectiva = banco.preparar(vigente, candidato)
    reloj = count(0.0, paso_s)
    return medir_ingesta(
        capa, candidato, efectiva, leer_corpus(corpus), corpus,
        tope_transcribir_s=vigente[Capacidad.TRANSCRIBIR].parametros["timeout_s"],
        reloj=lambda: next(reloj),
    )


@pytest.mark.rf("RF-08")
def test_con_la_capa_falsa_la_corrida_da_numeros_deterministas(acervo, pagina_leida):
    corrida = _corrida(acervo, "gemma3:4b", False, 0.5)
    cartel, pagina = corrida.mediciones

    # «Fotografía de un grupo de personas frente al edificio de la Facultad.»
    assert cartel.descripcion == DESCRIPCION_FIJA
    assert cartel.puntaje.invenciones == ("fotografía",)
    assert cartel.puntaje.cobertura == 0.0
    assert cartel.tasa_de_error == tasa_de_error(
        TEXTO_VISIBLE_FIJO, (CORPUS / "cartel-cyber-ar-2026.txt").read_text(encoding="utf-8")
    )
    assert cartel.lineas_basura == 0.0
    assert cartel.sin_coincidencia == (DESCRIPCION_FIJA,)
    assert pagina.puntaje.invenciones == ("personas", "edificio")
    assert (pagina.puntaje.cobertura, pagina.tasa_de_error) == (1.0, 0.0)
    assert {m.tiempo_ms for m in corrida.mediciones} == {500}
    assert (corrida.invenciones, corrida.con_invenciones, corrida.cobertura) == (3, 2, 0.5)
    assert corrida.sobre_el_tope == 0
    assert _corrida(acervo, "gemma3:4b", False, 0.5) == corrida


@pytest.mark.rf("RF-08")
def test_un_texto_plano_es_su_cuerpo_sin_pasar_por_un_modelo(acervo, tmp_path):
    (tmp_path / "acta.txt").write_text("Acta del Consejo Académico de 1975.", encoding="utf-8")
    (tmp_path / "etiquetas.json").write_text(json.dumps([{
        "archivo": "acta.txt", "hechos_esperados": ["Consejo Académico"], "invenciones": ["1980"],
    }]), encoding="utf-8")

    [acta] = _corrida(acervo, "gemma3:4b", False, 0.5, tmp_path).mediciones

    assert (acta.descripcion, acta.leido) == (None, "Acta del Consejo Académico de 1975.")
    assert (acta.puntaje.cobertura, acta.tope_s) == (1.0, None)


def test_el_informe_anota_el_hardware_y_no_descarta_por_superar_el_tope(acervo, pagina_leida):
    lento = _corrida(acervo, "gemma3:4b", False, 10_000.0)  # cada Pieza, 10 000 s
    rapido = _corrida(acervo, "qwen3-vl:2b-instruct", True, 0.5)

    assert lento.sobre_el_tope == 2
    # Empatan en invenciones y cobertura: gana el más rápido, y el lento sigue compitiendo.
    assert elegir([lento, rapido]) == (1, 0)
    tabla = tabla_ingesta([lento, rapido], hardware="Core Ultra 7, sin GPU")
    assert "Hardware: Core Ultra 7, sin GPU." in tabla
    assert "queda propuesto el mejor no chino, gemma3:4b" in tabla
    assert "| gemma3:4b | no | 3 | 2 | 50,0 % |" in tabla


def test_una_pieza_cortada_pesa_antes_que_las_invenciones(acervo, pagina_leida):
    sano = _corrida(acervo, "gemma3:4b", False, 0.5)
    cortado = CorridaDeIngesta(sano.candidato, sano.configuracion_efectiva, tuple(
        m.__class__(**{**m.__dict__, "error": "tope de tiempo"}) if m.archivo == CARTEL else m
        for m in sano.mediciones
    ))
    nada = CorridaDeIngesta(sano.candidato, sano.configuracion_efectiva, tuple(
        m.__class__(**{**m.__dict__, "error": "sin motor"}) for m in sano.mediciones
    ))

    assert cortado.invenciones == 2 < sano.invenciones
    assert cortado.cobertura == 0.5  # la cortada cuenta 0
    assert elegir([cortado, sano]) == (1, 1)
    assert elegir([nada]) == (None, None)


# El comando, como `hv_app`.


@pytest.mark.historia(169)
def test_banco_ingesta_por_comando_escribe_el_informe(
    monkeypatch, url_app, tmp_path, capsys, pagina_leida
):
    corpus = shutil.copytree(CORPUS, tmp_path / "corpus")
    monkeypatch.setattr(composicion, "capa_para", lambda filas, **_: CapaModelosFalsa().para(filas))
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.delenv("HV_REFERENCIA", raising=False)

    assert main(["banco", "ingesta", "--corpus", str(corpus), "--modelo", "gpt-9"]) == 1
    assert "No están en candidatos.csv para describir_imagen: gpt-9" in capsys.readouterr().err
    assert main(["banco", "ingesta", "--corpus", str(corpus),
                 "--hardware", "Core Ultra 7, sin GPU"]) == 0

    salida = capsys.readouterr()
    assert "Hardware: Core Ultra 7, sin GPU." in salida.out
    assert "No hay candidato no chino que haya medido" in salida.out  # el vigente es chino
    assert f"· {CARTEL}:" in salida.err
    [informe] = (corpus / "corridas").glob("ingesta-*.json")
    [corrida] = json.loads(informe.read_text(encoding="utf-8"))["corridas"]
    assert (corrida["invenciones"], corrida["cobertura"], corrida["ganador"]) == (3, 0.5, True)
    assert corrida["mediciones"][0]["sin_coincidencia"] == [DESCRIPCION_FIJA]


@pytest.mark.historia(169)
@pytest.mark.rf("RF-08")
def test_banco_ingesta_mide_con_la_instruccion_vieja_como_linea_de_base(
    monkeypatch, url_app, tmp_path, capsys, pagina_leida
):
    # 139: la fila vigente nombra la instrucción nueva; `--instruccion` mide con la vieja.
    corpus = shutil.copytree(CORPUS, tmp_path / "corpus")
    monkeypatch.setattr(composicion, "capa_para", lambda filas, **_: CapaModelosFalsa().para(filas))
    monkeypatch.setenv("HV_BD_URL", url_app)

    def instruccion(*opcion: str) -> str:
        salida = tmp_path / "corridas" / "-".join(opcion or ("vigente",))
        assert main(["banco", "ingesta", "--corpus", str(corpus), "--salida", str(salida),
                     *opcion]) == 0
        [informe] = salida.glob("ingesta-*.json")
        [corrida] = json.loads(informe.read_text(encoding="utf-8"))["corridas"]
        return corrida["configuracion_efectiva"]["por_capacidad"]["describir_imagen"][
            "instruccion_id"]

    assert instruccion() == "describir_imagen_en_afirmaciones"  # 144 la cambió
    assert instruccion("--instruccion", "describir_imagen_por_clase") == "describir_imagen_por_clase"
    assert instruccion("--instruccion", "describir_fotografia") == "describir_fotografia"
    capsys.readouterr()
    assert main(["banco", "ingesta", "--corpus", str(corpus), "--instruccion", "resumir_ficha"]) == 1
    assert "no es una instrucción de describir_imagen" in capsys.readouterr().err
