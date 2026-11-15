"""M4: la checklist de la SEU de efemérides y Relatos (RNF-26; DEC-128, DEC-147, R-13).

Las cuentas se prueban en proceso con casos hechos a mano, armando el CSV como lo completaría la
SEU. Después, `textos_redactados` contra Postgres real en una transacción de solo lectura y el
comando `banco m4`, de la plantilla a la medición, como `hv_app`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from historia_viva.banco.m4 import (
    COLUMNAS_M4,
    CRITERIOS_M4,
    EvaluacionesInvalidas,
    escribir_m4,
    escribir_plantilla_m4,
    identificador,
    medir_m4,
    tabla_m4,
)
from historia_viva.composicion import main
from historia_viva.efemerides import Estado, TextoRedactado, textos_redactados
from historia_viva.exportacion import escribir_csv
from validacion import JORGE

pytestmark = [pytest.mark.historia(268), pytest.mark.rnf("RNF-26")]

MOMENTO = datetime(2026, 10, 19, 10, tzinfo=UTC)


def _texto(tipo: str = "efemeride", modelo: str | None = "redactor@1") -> TextoRedactado:
    return TextoRedactado(
        texto_id=uuid4(), tipo=tipo,  # type: ignore[arg-type]
        titulo="Un día como hoy", estado=Estado.PROPUESTA, modelo=modelo,
    )


def _csv(filas: list[list[object]]) -> bytes:
    return escribir_csv(COLUMNAS_M4, filas)


def _todas(
    texto: TextoRedactado, evaluador: str, puntaje: int, marca: str = "no"
) -> list[list[object]]:
    return [[identificador(texto), evaluador, c, puntaje, marca] for c in CRITERIOS_M4]


def _caso_a_mano() -> tuple[list[TextoRedactado], list[list[object]]]:
    """10 efemérides; Ana pone 4 en todo; Beto, 2 en la primera y 3 en las demás."""
    textos = [_texto() for _ in range(10)]
    filas = [f for t in textos for f in _todas(t, "Ana", 4)]
    filas += [f for i, t in enumerate(textos) for f in _todas(t, "Beto", 2 if i == 0 else 3)]
    return textos, filas


def test_m4_promedia_la_checklist_y_marca_cumplimiento():
    textos, filas = _caso_a_mano()

    [efemerides, por_modelo, relatos] = medir_m4(_csv(filas), "e.csv", textos).grupos

    r = efemerides.resultado
    primera = identificador(textos[0])
    assert (r.n_textos, r.n_evaluadores) == (10, 2)
    assert r.por_texto[primera] == 3.0  # (5 × 4 + 5 × 2) / 10
    assert r.promedio == pytest.approx(3.45)  # (3,0 + 9 × 3,5) / 10
    assert r.minimo == (primera, 3.0)
    assert r.por_evaluador == {"ana": 4.0, "beto": pytest.approx(2.9)}  # 145 / 50
    assert r.por_criterio == {c: pytest.approx(3.45) for c in sorted(CRITERIOS_M4)}
    assert r.acuerdo == (0.0, pytest.approx(0.9))  # 45 de 50 pares a un punto
    assert r.cumple is True
    assert por_modelo.modelo == "redactor@1" and por_modelo.resultado.cumple is True
    assert relatos.tipo == "relato" and relatos.resultado.cumple is None


def test_una_sola_marcada_no_verificable_no_cumple_aunque_el_promedio_pase():
    textos, filas = _caso_a_mano()
    filas[-1][4] = "sí"  # Beto, la última efeméride, un criterio

    medicion = medir_m4(_csv(filas), "e.csv", textos)

    r = medicion.grupos[0].resultado
    assert r.promedio == pytest.approx(3.45)
    assert r.no_verificables == (identificador(textos[-1]),)
    assert r.cumple is False
    assert "| Efemérides | 10 | 2 | 3,45 |" in tabla_m4(medicion)
    assert "| 1 | No cumple |" in tabla_m4(medicion)


@pytest.mark.parametrize(
    ("efemerides", "evaluadores", "aviso"),
    [
        (9, ("Ana", "Beto"), "Sin veredicto: faltan 1 efeméride con dos evaluadores |"),
        (10, ("Ana",), "Sin veredicto: faltan 10 efemérides con dos evaluadores y 1 evaluador |"),
        (10, ("Ana", " ANA"), "faltan 10 efemérides con dos evaluadores y 1 evaluador |"),
    ],
)
def test_sin_diez_textos_o_dos_evaluadores_no_hay_veredicto_y_dice_cuantos_faltan(
    efemerides, evaluadores, aviso
):
    textos = [_texto() for _ in range(efemerides)]
    criterios = list(CRITERIOS_M4)
    # Con dos nombres, cada uno pone sus criterios: si fueran la misma persona, no se repiten.
    filas = [
        [identificador(t), e, c, 4, ""] for t in textos for i, e in enumerate(evaluadores)
        for c in (criterios if len(evaluadores) == 1 else criterios[i::2])
    ]

    medicion = medir_m4(_csv(filas), "e.csv", textos)

    r = medicion.grupos[0].resultado
    assert r.cumple is None and r.promedio == 4.0
    assert aviso in tabla_m4(medicion)


def test_nueve_textos_de_una_persona_y_uno_de_otra_no_tienen_un_par_en_comun():
    textos = [_texto() for _ in range(10)]
    filas = [f for t in textos[:9] for f in _todas(t, "Ana", 4)] + _todas(textos[9], "Beto", 4)

    r = medir_m4(_csv(filas), "e.csv", textos).grupos[0].resultado

    assert (r.n_textos, r.n_evaluadores, r.n_con_dos_evaluadores) == (10, 2, 0)
    assert r.faltan == (10, 0)
    assert r.cumple is None


@pytest.mark.historia(302)
def test_los_relatos_se_miden_aparte_y_cada_tipo_por_modelo_de_redaccion():
    efemerides = [_texto(modelo="a@1") for _ in range(5)] + [_texto(modelo="b@1") for _ in range(5)]
    relatos = [_texto("relato", modelo="responde@1"), _texto("relato", modelo=None)]
    filas = [f for t in efemerides[:5] for e in ("Ana", "Beto") for f in _todas(t, e, 4)]
    filas += [f for t in efemerides[5:] for e in ("Ana", "Beto") for f in _todas(t, e, 2)]
    filas += _todas(relatos[0], "Ana", 1) + _todas(relatos[1], "Ana", 3, marca="sí")

    medicion = medir_m4(_csv(filas), "e.csv", efemerides + relatos)

    grupos = {(g.tipo, g.modelo, g.todos): g.resultado for g in medicion.grupos}
    assert list(grupos) == [
        ("efemeride", None, True), ("efemeride", "a@1", False), ("efemeride", "b@1", False),
        ("relato", None, True), ("relato", None, False), ("relato", "responde@1", False),
    ]
    assert grupos["efemeride", None, True].promedio == 3.0
    assert grupos["efemeride", None, True].cumple is True
    assert grupos["efemeride", "a@1", False].promedio == 4.0
    assert grupos["efemeride", "b@1", False].promedio == 2.0
    relatos_ = grupos["relato", None, True]
    assert (relatos_.n_textos, relatos_.promedio) == (2, 2.0)
    assert relatos_.no_verificables == (identificador(relatos[1]),)
    tabla = tabla_m4(medicion)
    assert "| Relatos · modelo sin registrar | 1 |" in tabla
    assert "Sin veredicto: faltan 10 Relatos con dos evaluadores y 1 evaluador" in tabla


@pytest.mark.historia(268, 302)
def test_m4_plantilla_csv_seguro_y_lectura(tmp_path):
    textos = [_texto(), _texto("relato")]

    csv_, lista = escribir_plantilla_m4(
        tmp_path, textos, evaluadores=("Ana", "=Beto"), momento=MOMENTO
    )

    lineas = csv_.read_text(encoding="utf-8-sig").splitlines()
    assert lineas[0] == ",".join(COLUMNAS_M4)
    assert len(lineas) == 1 + 2 * 2 * len(CRITERIOS_M4)
    assert lineas[1] == f"{identificador(textos[0])},Ana,exactitud,,"
    assert f"{identificador(textos[1])},'=Beto,pertinencia,," in lineas
    assert identificador(textos[1]).startswith("rel_")
    assert "Un día como hoy" in lista.read_text(encoding="utf-8")
    # Sin completar, todo está pendiente; completada, se relee tal cual salió.
    assert medir_m4(csv_.read_bytes(), csv_.name, textos).pendientes == 20
    completada = "\r\n".join([lineas[0], *(f"{linea[:-2]},4,no" for linea in lineas[1:])])
    medicion = medir_m4(completada.encode("utf-8-sig"), "evaluaciones.csv", textos)
    efemerides = medicion.grupos[0].resultado
    assert (medicion.pendientes, efemerides.n_textos, efemerides.promedio) == (0, 1, 4.0)
    assert efemerides.por_evaluador == {"'=beto": 4.0, "ana": 4.0}


def test_la_lectura_rechaza_puntajes_fuera_de_1_a_4_criterios_desconocidos_y_lo_demas():
    texto = _texto()
    id_ = identificador(texto)
    filas: list[list[object]] = [
        [id_, "Ana", "exactitud", 5, "no"],
        [id_, "Ana", "redaccion", 0, ""],
        [id_, "Ana", "plantilla", "3,5", ""],
        [id_, "Ana", "ortografia", 3, ""],
        [f"efe_{uuid4()}", "Ana", "pertinencia", 3, ""],
        [id_, "", "verificable", 3, ""],
        [id_, "Beto", "verificable", 3, "quizás"],
        [id_, "Beto", "exactitud", 3, ""],
        [id_, "Beto", "exactitud", 4, ""],
        [id_, "Beto", "redaccion", "", ""],  # sin completar: no es un error
        [f"rel_{uuid4()}", "Beto", "redaccion", "", ""],  # sin completar, pero no existe
        [id_, "Beto", "ortografia", "", "no"],
        [id_, " BETO", "exactitud", 3, ""],  # el mismo Beto
    ]

    with pytest.raises(EvaluacionesInvalidas) as error:
        medir_m4(_csv(filas), "e.csv", [texto])

    assert [e.split(":")[0] for e in error.value.errores] == [
        "Fila 2", "Fila 3", "Fila 4", "Fila 5", "Fila 6", "Fila 7", "Fila 8", "Fila 10",
        "Fila 12", "Fila 13", "Fila 14",
    ]
    detalle = "\n".join(error.value.errores)
    assert "«5» tiene que ir de 1 a 4" in detalle and "«3,5» tiene que ir de 1 a 4" in detalle
    assert "criterio desconocido «ortografia»" in detalle
    assert "no es una efeméride ni un Relato" in detalle
    assert "falta el evaluador" in detalle and "«quizás» tiene que ser sí o no" in detalle
    assert all("repite" in error.value.errores[i] for i in (7, 10))
    assert "no es una efeméride" in error.value.errores[8]
    assert "criterio desconocido" in error.value.errores[9]
    with pytest.raises(EvaluacionesInvalidas, match="Faltan las columnas: puntaje"):
        medir_m4(b"efemeride_id,evaluador,criterio,no_verificable\r\n", "e.csv", [texto])


def test_la_checklist_es_la_provisoria_hasta_c41():
    assert list(CRITERIOS_M4.values()) == [
        "Exactitud de los datos con cita", "Ninguna información no verificable",
        "Uso de la plantilla", "Redacción", "Pertinencia",
    ]


@pytest.mark.historia(205)
def test_toda_celda_de_la_corrida_de_m4_pasa_por_la_celda_segura(tmp_path):
    textos = [_texto(modelo="@modelo")]
    filas = _todas(textos[0], "-Ana", 4) + _todas(textos[0], "+Beto", 3)
    # Sin la celda segura a la entrada: así llegan si la SEU los tipea en otro programa.
    crudo = "\r\n".join([",".join(COLUMNAS_M4), *(",".join(map(str, f)) for f in filas)])

    json_, csv_, md = escribir_m4(
        tmp_path, medir_m4(crudo.encode(), "e.csv", textos), momento=MOMENTO
    )

    salida = csv_.read_text(encoding="utf-8-sig").splitlines()
    assert salida[0] == "tipo,texto_id,modelo,evaluadores,promedio,no_verificable"
    assert salida[1].startswith(f"efemeride,{identificador(textos[0])},'@modelo,")
    tabla = md.read_text(encoding="utf-8")
    assert all(f"| '{e} | " in tabla for e in ("-ana", "+beto"))
    datos = json.loads(json_.read_text(encoding="utf-8"))
    assert datos["checklist_provisoria"] is True
    assert datos["grupos"][0]["promedio"] == 3.5


# Contra Postgres real.


def _efemeride(conexion: Any, dia: int, modelo: str) -> str:
    fecha = date(2026, 5, 1) + timedelta(days=dia)
    return str(conexion.execute(
        "INSERT INTO acervo.efemeride (alcance, fecha_objetivo, difusion_desde, difusion_hasta,"
        " titulo, cuerpo, configuracion_efectiva, origen_generacion)"
        " VALUES ('dia', %s, %s, %s, 'Un día como hoy', 'Se fundó la Facultad [1].', %s,"
        " 'programada') RETURNING efemeride_id",
        (fecha, fecha, fecha,
         Jsonb({"por_capacidad": {"redactar_efemeride": {"modelo": modelo}}})),
    ).fetchone()[0])


def _relato(conexion: Any, modelo: str) -> str:
    conversacion = conexion.execute(
        "INSERT INTO acervo.conversacion (persona_id) VALUES (%s) RETURNING conversacion_id",
        (JORGE,),
    ).fetchone()[0]
    mensaje = conexion.execute(
        "INSERT INTO acervo.mensaje_conversacion (conversacion_id, orden, autor, contenido,"
        " estado) VALUES (%s, 2, 'historia_viva', 'Se creó [1].', 'completa')"
        " RETURNING mensaje_id",
        (conversacion,),
    ).fetchone()[0]
    return str(conexion.execute(
        "INSERT INTO acervo.relato (mensaje_id, titulo, cuerpo, configuracion_efectiva,"
        " propuesto_por) VALUES (%s, 'El laboratorio', 'Se creó [1].', %s, %s)"
        " RETURNING relato_id",
        (mensaje, Jsonb({"por_capacidad": {"responder": {"modelo": modelo}}}), JORGE),
    ).fetchone()[0])


@pytest.mark.historia(302)
def test_los_textos_se_leen_con_su_modelo_sin_escribir_en_la_base(tx):
    efemeride, relato = _efemeride(tx, 0, "redactor@1"), _relato(tx, "responde@1")
    tx.execute("SET transaction_read_only = on")  # si escribiera, Postgres lo rechazaría
    tx.row_factory = dict_row  # como las entrega `bd`

    try:
        textos = {str(t.texto_id): t for t in textos_redactados(tx)}
    finally:
        tx.row_factory = tuple_row

    assert (textos[efemeride].tipo, textos[efemeride].modelo) == ("efemeride", "redactor@1")
    assert (textos[relato].tipo, textos[relato].modelo) == ("relato", "responde@1")
    assert identificador(textos[relato]) == f"rel_{relato}"


@pytest.mark.historia(168, 302)
@pytest.mark.confirma("el comando lee con su propio pool lo que siembra el test")
def test_banco_m4_por_comando_de_la_plantilla_a_la_medicion(
    _conexion_del_dueno, url_app, tmp_path, monkeypatch, capsys
):
    efemeride = _efemeride(_conexion_del_dueno, 0, "redactor@1")
    relato = _relato(_conexion_del_dueno, "responde@1")
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(tmp_path))

    assert main(["banco", "m4"]) == 1
    assert "No está el archivo de evaluaciones" in capsys.readouterr().err
    assert main(["banco", "m4", "--plantilla", "--evaluador", "Ana", "--evaluador", "Beto"]) == 0

    [plantilla] = (tmp_path / "corridas").glob("m4-plantilla-*.csv")
    cabecera, *filas = plantilla.read_text(encoding="utf-8-sig").splitlines()
    assert len(filas) == 2 * 2 * len(CRITERIOS_M4)
    (tmp_path / "evaluaciones.csv").write_text(
        "\r\n".join([cabecera, *(f"{f[:-2]},{'2' if 'Beto' in f else '4'}," for f in filas)]),
        encoding="utf-8-sig",
    )
    assert main(["banco", "m4"]) == 0

    [corrida] = (tmp_path / "corridas").glob("m4-2*.json")
    grupos = json.loads(corrida.read_text(encoding="utf-8"))["grupos"]
    assert [(g["tipo"], g["modelo"], g["promedio"]) for g in grupos] == [
        ("efemeride", None, 3.0), ("efemeride", "redactor@1", 3.0),
        ("relato", None, 3.0), ("relato", "responde@1", 3.0),
    ]
    assert grupos[0]["por_texto"] == {f"efe_{efemeride}": 3.0}
    assert grupos[2]["por_texto"] == {f"rel_{relato}": 3.0}
    assert "Sin veredicto: faltan 9 efemérides con dos evaluadores" in capsys.readouterr().out
    (tmp_path / "evaluaciones.csv").write_text(
        f"{cabecera}\r\nefe_{efemeride},Ana,exactitud,7,\r\n", encoding="utf-8-sig"
    )
    assert main(["banco", "m4"]) == 1
    assert "«7» tiene que ir de 1 a 4" in capsys.readouterr().err
    with _conexion_del_dueno.transaction():  # las evaluaciones no viven en la base (DEC-97)
        assert _conexion_del_dueno.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'evaluacion%'"
        ).fetchone()[0] == 0
