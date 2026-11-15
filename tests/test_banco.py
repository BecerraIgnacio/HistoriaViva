"""El banco: `medir_m2` por sus casos, la referencia y el comando (spec, módulo 27 `banco`;
DEC-96, DEC-97, DEC-128, R-21, DIR-38).

`medir_m2` es pura y se prueba en proceso: las Piezas se arman en memoria con su Ficha y los
fragmentos que les daría `fragmentar`, y cada caso corrompe una parte. Después, lo mismo contra
Postgres real (los fragmentos se escriben por SQL con la forma de `indice`, como en
`test_informes`), `cargar_referencia` y el comando `banco` como `hv_app`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row, tuple_row
from test_informes import _crear, _indexar, _retirar

from aplicacion import entrar
from historia_viva.banco import PiezaAMedir, ResultadoM2, medir_m2
from historia_viva.banco.aceptacion import Aceptacion, IndexacionPendiente, escribir_m2
from historia_viva.banco.referencia import ReferenciaInvalida, cargar_referencia, huella
from historia_viva.composicion import main
from historia_viva.configuracion import ConfiguracionDeModelos
from historia_viva.dominio.cuerpo import segmentos
from historia_viva.dominio.estados import Estado
from historia_viva.dominio.fechas import PrecisionFecha
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import (
    CamposFicha,
    EdicionFicha,
    FichaVigente,
    MetodoAutenticacion,
    ViaDeIngreso,
)
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.indexacion import Indexacion, LineaDeContexto, fragmentar
from historia_viva.indice import Fragmento
from historia_viva.procesamiento.cola import Cola
from validacion import JORGE, MARIA

pytestmark = [pytest.mark.historia(273), pytest.mark.rf("RF-49")]

MODELO = "embebedor@0123456789ab"
CUERPO = (
    "--- Página 1 ---\n\nActa de la jornada de transferencia.\n\n"
    "--- Página 2 ---\n\nFirmaron el convenio.\n\n## Contexto\n\nLo contó el decano."
)


def _pieza(
    *,
    cuerpo: str = CUERPO,
    validada: bool = True,
    huella: str | None = None,
    modelo: str = MODELO,
    **campos: object,
) -> PiezaAMedir:
    """Una Validada completa e indexada como lo haría `indexacion`; `campos` pisa la Ficha."""
    pieza_id, ficha_id = uuid4(), uuid4()
    ficha = CamposFicha(
        id=pieza_id,
        tipo="acta",
        tipo_actividad="Transferencia",
        fecha_inicio=date(2019, 5, 3),
        fecha_fin=date(2019, 5, 3),
        precision_fecha=PrecisionFecha.DIA,
        origen="Archivo de la SEU",
        propietario="Secretaría de Extensión Universitaria",
        resumen="Convenio de transferencia.",
        estado=Estado.VALIDADA,
        archivo_original=f"local://originales/{pieza_id}",
    )
    ficha = dataclasses.replace(ficha, **campos)  # type: ignore[arg-type]
    fragmentos = tuple(
        Fragmento(
            fragmento_id=f.fragmento_id,
            pieza_id=pieza_id,
            ficha_id=ficha_id,
            tipo="acta",
            tipo_actividad="Transferencia",
            carreras=(),
            fecha_inicio=date(2019, 5, 3),
            fecha_fin=date(2019, 5, 3),
            clase_original=ClaseDeOriginal.DOCUMENTO,
            ubicacion=f.ubicacion,
            inicio=f.inicio,
            fin=f.fin,
            texto=f.texto,
            modelo_embedding=modelo,
            via_ingreso=ViaDeIngreso.ARCHIVO,
        )
        for f in fragmentar(ficha_id, segmentos(cuerpo), LineaDeContexto("acta", "2019"))
    )
    return PiezaAMedir(
        pieza_id=pieza_id,
        validada=validada,
        ficha=FichaVigente(
            ficha_id=ficha_id,
            pieza_id=pieza_id,
            version=2,
            origen_version="persona",
            campos=ficha,
            cuerpo=cuerpo,
            editada_por=uuid4(),
            editada_el=datetime(2026, 10, 1, tzinfo=UTC),
            metodo_autenticacion=None,
        ),
        huella=huella or uuid4().hex * 2,
        fragmentos=fragmentos,
    )


def _con(pieza: PiezaAMedir, *fragmentos: Fragmento) -> PiezaAMedir:
    return dataclasses.replace(pieza, fragmentos=fragmentos)


def test_las_piezas_completas_e_indexadas_dan_cien_por_ciento():
    piezas = [_pieza(), _pieza()]

    resultado = medir_m2(piezas)

    assert (resultado.valor, resultado.piezas_completas, resultado.piezas_total) == (1.0, 2, 2)
    assert resultado.fallas == {}
    assert len(piezas[0].fragmentos) == 3  # dos páginas y el Contexto


def _otro(fragmento: Fragmento, **cambios: object) -> Fragmento:
    return dataclasses.replace(fragmento, **cambios)  # type: ignore[arg-type]


def _corromper(caso: str) -> PiezaAMedir:
    pieza = _pieza()
    primero, segundo, tercero = pieza.fragmentos
    match caso:
        case "retirada":
            return _pieza(validada=False)
        case "sin_indexar":
            return _con(pieza)
        case "sin_fecha":
            return _pieza(fecha_inicio=None)
        case "origen_en_blanco":
            return _pieza(origen="  ")
        case "sin_propietario":
            return _pieza(propietario=None)
        case "fragmento_de_otro_tipo_de_actividad":
            return _con(pieza, primero, _otro(segundo, tipo_actividad="Consultoría"), tercero)
        case "fragmento_de_otro_tipo":
            return _con(pieza, primero, _otro(segundo, tipo="carta"), tercero)
        case "fragmento_con_otro_inicio":
            return _con(pieza, primero, _otro(segundo, fecha_inicio=date(2019, 5, 4)), tercero)
        case "fragmento_con_otro_fin":
            return _con(pieza, primero, _otro(segundo, fecha_fin=date(2019, 5, 4)), tercero)
        case "metadatos_ilegibles":
            return dataclasses.replace(pieza, fragmentos_ilegibles=1)
        case "fragmento_duplicado":
            return _con(pieza, primero, segundo, _otro(segundo, fragmento_id=uuid4()), tercero)
        case "fragmento_de_otra_version":
            return _con(pieza, primero, _otro(segundo, ficha_id=uuid4()), tercero)
        case "texto_editado":
            return _con(pieza, primero, _otro(segundo, texto="Otra cosa."), tercero)
        case "fragmento_borrado":
            return _con(pieza, primero, tercero)
        case "otro_modelo":
            viejo = _otro(segundo, modelo_embedding="viejo@ba9876543210")
            return _con(pieza, primero, viejo, tercero)
    raise AssertionError(caso)


@pytest.mark.parametrize(
    ("caso", "falla"),
    [
        ("retirada", "no_validada"),
        ("sin_indexar", "p2.sin_fragmentos"),
        ("sin_fecha", "p1.fecha"),
        ("origen_en_blanco", "p1.origen"),
        ("sin_propietario", "p1.propietario"),
        ("fragmento_de_otro_tipo_de_actividad", "p1.fragmento"),
        ("fragmento_de_otro_tipo", "p1.fragmento"),
        ("fragmento_con_otro_inicio", "p1.fragmento"),
        ("fragmento_con_otro_fin", "p1.fragmento"),
        ("metadatos_ilegibles", "p3.metadatos"),
        ("fragmento_duplicado", "p2.repetido"),
        ("fragmento_de_otra_version", "p2.version_no_vigente"),
        ("texto_editado", "p3.texto"),
        ("fragmento_borrado", "p3.cobertura"),
        ("otro_modelo", "p3.modelo"),
    ],
)
def test_cada_parte_de_m2_baja_el_valor_cuando_se_la_corrompe(caso, falla):
    corrompida = _corromper(caso)

    resultado = medir_m2([_pieza(), corrompida])

    assert (resultado.valor, resultado.piezas_completas, resultado.piezas_total) == (0.5, 1, 2)
    assert resultado.fallas == {corrompida.pieza_id: (falla,)}


def test_dos_validadas_con_el_mismo_original_fallan_las_dos():
    gemelas = [_pieza(huella="a" * 64), _pieza(huella="a" * 64)]

    resultado = medir_m2([_pieza(), *gemelas])

    assert resultado.fallas == {p.pieza_id: ("p2.huella_repetida",) for p in gemelas}


def test_una_validada_sin_indexar_se_informa_aparte_y_no_impide_medir():
    sin_indexar = _con(_pieza())

    resultado = medir_m2([_pieza(), _pieza(), sin_indexar])

    assert resultado.valor == pytest.approx(2 / 3)
    assert resultado.sin_indexar == (sin_indexar.pieza_id,)


def test_los_demas_obligatorios_se_informan_aparte_sin_bajar_el_valor():
    sin_resumen = _pieza(resumen=None, precision_fecha=None)
    sin_tipo = _pieza(tipo="")

    resultado = medir_m2([sin_resumen, sin_tipo, _pieza()])

    assert resultado.valor == 1.0
    assert resultado.p1_resto == {
        sin_resumen.pieza_id: ("precision_fecha", "resumen"),
        sin_tipo.pieza_id: ("tipo",),
    }


def test_el_unico_fragmento_de_un_video_sin_texto_es_su_resumen_firmado():
    pieza = _pieza(cuerpo="")
    resumen = Fragmento(
        **{
            **dataclasses.asdict(_pieza().fragmentos[0]),
            "pieza_id": pieza.pieza_id,
            "ficha_id": pieza.ficha.ficha_id,  # type: ignore[union-attr]
            "ubicacion": {"seccion": "resumen"},
            "inicio": 0,
            "fin": 0,
            "texto": "Convenio de transferencia.",
            "clase_original": ClaseDeOriginal.VIDEO,
        }
    )

    assert medir_m2([_con(pieza, resumen)]).valor == 1.0
    assert medir_m2([_con(pieza, _otro(resumen, texto="Otro resumen."))]).fallas == {
        pieza.pieza_id: ("p3.texto",)
    }


def test_la_huella_repetida_cuenta_solo_entre_validadas():
    validada = _pieza(huella="b" * 64)
    retirada = _pieza(huella="b" * 64, validada=False)

    assert medir_m2([validada, retirada]).fallas == {retirada.pieza_id: ("no_validada",)}


def test_el_unico_modelo_es_el_vigente_y_sin_el_el_mas_frecuente():
    viejas = [_pieza(modelo="viejo@ba9876543210") for _ in range(2)]
    nueva = _pieza()

    con_vigente = medir_m2([*viejas, nueva], modelo=MODELO)
    sin_vigente = medir_m2([*viejas, nueva])

    # A mitad de un `reindexar`: lo que todavía no se reembebió es lo que falla.
    assert con_vigente.fallas == {p.pieza_id: ("p3.modelo",) for p in viejas}
    assert sin_vigente.fallas == {nueva.pieza_id: ("p3.modelo",)}


# 072: M2 contra Postgres real, la referencia y el comando `banco`.

PAGINAS = "--- Página 1 ---\n\nActa de la jornada.\n\n--- Página 2 ---\n\nFirmaron el convenio."


@pytest.fixture
def acervo(tx) -> Iterator[Any]:
    """`tx` con las filas como diccionarios, como las entrega `bd`."""
    tx.row_factory = dict_row
    yield tx
    tx.row_factory = tuple_row


@pytest.fixture
def fichas() -> ModuloFichasPostgres:
    return ModuloFichasPostgres(bd=None)  # type: ignore[arg-type]  # solo métodos con `tx`


@pytest.fixture
def indexacion(fichas) -> Indexacion:
    """Solo lo que corre en la transacción de quien llama: `encolar` y `pendientes`."""
    return Indexacion(
        bd=None, indice=None, capa_para=None, auditoria=None,  # type: ignore[arg-type]
        fichas=fichas, cola=Cola(bd=None),  # type: ignore[arg-type]
        configuracion=ConfiguracionDeModelos(bd=None, validar=lambda _: None),  # type: ignore[arg-type]
    )


@pytest.fixture
def aceptacion(fichas, indexacion) -> Aceptacion:
    return Aceptacion(
        fichas=fichas,
        configuracion=ConfiguracionDeModelos(bd=None, validar=lambda _: None),  # type: ignore[arg-type]
        pendientes=indexacion.pendientes,
    )


def _editar(
    tx: Any, fichas: ModuloFichasPostgres, pieza_id: UUID, version_base: int, **cambios: Any
) -> None:
    edicion = {
        "tipo": "acta", "tipo_actividad": "Transferencia", "fecha_inicio": date(2019, 5, 3),
        "precision_fecha": PrecisionFecha.DIA, "origen": "Archivo de la SEU",
        "propietario": "Secretaría de Extensión Universitaria", "resumen": "Convenio.",
        "cuerpo": PAGINAS, "contexto": "Lo contó el decano.", **cambios,
    }
    fichas.guardar_edicion(
        tx, pieza_id=pieza_id, version_base=version_base, edicion=EdicionFicha(**edicion),
        editada_por=UUID(MARIA), metodo=MetodoAutenticacion.DEV,
    )


def _validada(tx: Any, fichas: ModuloFichasPostgres, *, indexada: bool = True) -> UUID:
    """Una Validada de dos páginas y Contexto (tres fragmentos), indexada por SQL como `indice`."""
    pieza_id = _crear(tx, fichas)()
    _editar(tx, fichas, pieza_id, version_base=2)
    if indexada:
        _indexar(tx, fichas, pieza_id)
    return pieza_id


def _corromper_en_la_base(
    caso: str, tx: Any, fichas: ModuloFichasPostgres, pieza_id: UUID
) -> None:
    segundo = (
        "(SELECT node_id FROM acervo.data_fragmento WHERE metadata_->>'pieza_id' = %(p)s"
        " ORDER BY (metadata_->>'inicio')::int OFFSET 1 LIMIT 1)"
    )
    parametros = {"p": str(pieza_id)}
    match caso:
        case "fragmento_borrado":
            tx.execute(f"DELETE FROM acervo.data_fragmento WHERE node_id = {segundo}", parametros)
        case "fragmento_duplicado":
            tx.execute(
                "INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)"
                " SELECT gen_random_uuid()::text, text, metadata_, embedding"
                f" FROM acervo.data_fragmento WHERE node_id = {segundo}",
                parametros,
            )
        case "ficha_editada_sin_reindexar":
            _editar(tx, fichas, pieza_id, version_base=3, resumen="Corregido.")
        case "tipo_en_blanco":
            tx.execute(
                "UPDATE acervo.data_fragmento"
                " SET metadata_ = jsonb_set(metadata_, '{tipo}', to_jsonb(''::text))"
                f" WHERE node_id = {segundo}",
                parametros,
            )


@pytest.mark.historia(203)
@pytest.mark.rnf("RNF-12")
@pytest.mark.parametrize(
    ("caso", "falla"),
    [
        ("fragmento_borrado", "p3.cobertura"),
        ("fragmento_duplicado", "p2.repetido"),
        ("ficha_editada_sin_reindexar", "p2.version_no_vigente"),
        ("tipo_en_blanco", "p1.fragmento"),
    ],
)
def test_m2_cada_parte_falla_al_corromper_la_base(acervo, fichas, aceptacion, caso, falla):
    sana, corrompida = _validada(acervo, fichas), _validada(acervo, fichas)
    assert aceptacion.medir_m2(acervo).valor == 1.0

    _corromper_en_la_base(caso, acervo, fichas, corrompida)
    resultado = aceptacion.medir_m2(acervo)

    assert (resultado.valor, resultado.piezas_total) == (0.5, 2)
    assert resultado.fallas == {corrompida: (falla,)}
    assert sana in resultado.piezas


@pytest.mark.historia(203)
@pytest.mark.rf("RF-15")
def test_m2_global_se_niega_con_indexaciones_pendientes_y_sobre_una_lista_no(
    acervo, fichas, indexacion, aceptacion
):
    indexadas = [_validada(acervo, fichas), _validada(acervo, fichas)]
    sin_indexar = _validada(acervo, fichas, indexada=False)
    indexacion.encolar(acervo, sin_indexar)

    with pytest.raises(IndexacionPendiente):
        aceptacion.medir_m2(acervo)
    del_informe = aceptacion.medir_m2(acervo, [*indexadas, sin_indexar])

    assert del_informe.valor == pytest.approx(2 / 3)
    assert del_informe.sin_indexar == (sin_indexar,)


@pytest.mark.historia(203)
@pytest.mark.rf("RF-15")
def test_m2_marca_las_indexadas_que_no_son_validadas_no_retiradas(acervo, fichas, aceptacion):
    validada, retirada = _validada(acervo, fichas), _validada(acervo, fichas)
    _retirar(acervo, fichas, retirada)  # sin desindexar: sus fragmentos quedan en el índice

    resultado = aceptacion.medir_m2(acervo)

    assert resultado.violaciones_rf15 == (retirada,)
    assert (resultado.piezas, resultado.valor) == ((validada,), 1.0)
    # Sobre una lista que las incluye a todas, el mismo valor que sobre todas.
    assert aceptacion.medir_m2(acervo, [validada]).valor == resultado.valor


def _referencia(
    directorio: Path, *, piezas: str, consultas: str, originales: tuple[str, ...] = ("acta.pdf",)
) -> Path:
    directorio.mkdir(exist_ok=True)
    for original in originales:
        (directorio / original).write_bytes(b"%PDF-1.4")
    (directorio / "piezas.csv").write_text(piezas, encoding="utf-8")
    (directorio / "consultas.csv").write_text(consultas, encoding="utf-8")
    return directorio


PIEZAS_CSV = "referencia_id,archivo,pieza_id,tipo\nr1,acta.pdf,{pieza},acta\n"
CONSULTAS_CSV = (
    "consulta_id,texto,piezas_esperadas,desde,hasta,tipo_actividad,conjunto,sin_evidencia\n"
)


@pytest.mark.historia(168)
def test_cargar_referencia_lee_las_fichas_sin_escribir_y_calcula_la_huella(
    acervo, fichas, tmp_path
):
    pieza = _validada(acervo, fichas)
    directorio = _referencia(
        tmp_path / "referencia",
        piezas=PIEZAS_CSV.format(pieza=pieza),
        consultas=CONSULTAS_CSV
        + "c1,¿Qué convenio?,r1,2019-01-01,2019-12-31,Transferencia,prueba,\n"
        + "c2,¿Y el de 1950?,,,,,prueba,true\n",
    )
    acervo.execute("SET TRANSACTION READ ONLY")  # si escribiera, Postgres lo rechazaría

    conjunto = cargar_referencia(acervo, directorio, fichas=fichas)

    assert [c.consulta_id for c in conjunto.consultas] == ["c1", "c2"]
    assert conjunto.piezas[0].pieza_id == pieza
    assert conjunto.hash_referencia == huella(directorio)
    (directorio / "consultas.csv").write_text(CONSULTAS_CSV, encoding="utf-8")
    assert huella(directorio) != conjunto.hash_referencia


@pytest.mark.historia(168)
@pytest.mark.parametrize(
    ("consulta", "originales", "error"),
    [
        ("c1,x,r1,2020-01-01,,,prueba,", ("acta.pdf",), "el filtro excluye a su Pieza esperada r1"),
        ("c1,x,r1,,2019-05-02,,prueba,", ("acta.pdf",), "el filtro excluye a su Pieza esperada r1"),
        ("c1,x,r1,,,Consultoría,prueba,", ("acta.pdf",), "su tipo de actividad es «Transferencia»"),
        ("c1,x,,,,,prueba,", ("acta.pdf",), "no tiene Pieza esperada"),
        ("c1,x,r9,,,,prueba,", ("acta.pdf",), "r9 no está en piezas.csv"),
        ("c1,x,r1,,,,prueba,", (), "falta el archivo «acta.pdf»"),
    ],
    ids=["desde", "hasta", "tipo-de-actividad", "sin-pieza", "pieza-desconocida", "sin-original"],
)
def test_cargar_referencia_rechaza_lo_que_no_sirve_sin_escribir(
    acervo, fichas, tmp_path, consulta, originales, error
):
    pieza = _validada(acervo, fichas)
    directorio = _referencia(
        tmp_path / "referencia", piezas=PIEZAS_CSV.format(pieza=pieza),
        consultas=CONSULTAS_CSV + consulta + "\n", originales=originales,
    )
    acervo.execute("SET TRANSACTION READ ONLY")

    with pytest.raises(ReferenciaInvalida) as rechazo:
        cargar_referencia(acervo, directorio, fichas=fichas)

    assert any(error in e for e in rechazo.value.errores), rechazo.value.errores


@pytest.mark.historia(168)
def test_cargar_referencia_sin_los_archivos_se_niega(acervo, fichas, tmp_path):
    with pytest.raises(ReferenciaInvalida) as rechazo:
        cargar_referencia(acervo, tmp_path, fichas=fichas)

    assert rechazo.value.errores == (
        f"Falta piezas.csv en {tmp_path}.", f"Falta consultas.csv en {tmp_path}.",
    )


@pytest.mark.historia(197, 205)
def test_toda_celda_de_la_salida_pasa_por_la_celda_segura(tmp_path):
    pieza = uuid4()
    resultado = ResultadoM2(
        piezas_total=1, fallas={pieza: ('=HYPERLINK("x")',)}, p1_resto={pieza: ("+resumen",)},
        piezas=(pieza,), modelo="@modelo",
    )

    json_, csv_, md = escribir_m2(
        tmp_path, resultado, hash_referencia="-hash", configuracion_efectiva={},
        informe_id=None, momento=datetime(2026, 10, 19, 10, tzinfo=UTC),
    )

    assert csv_.read_text(encoding="utf-8-sig").splitlines() == [
        "pieza_id,cumple,fallas,p1_resto", f"{pieza},false,\"'=HYPERLINK(\"\"x\"\")\",'+resumen",
    ]
    tabla = md.read_text(encoding="utf-8")
    assert all(f"'{c}" in tabla for c in ("=HYPERLINK", "+resumen", "-hash", "@modelo"))
    assert json.loads(json_.read_text(encoding="utf-8"))["valor"] == 0.0


# Por el comando, como `hv_app`, con la app confirmando desde su propio pool.

DETENER = ["interruptor", "detener", "procesamiento_habilitado", "--motivo", "Corrida del banco"]


@pytest.mark.historia(162, 168, 205)
@pytest.mark.rnf("RNF-12")
@pytest.mark.confirma("la app y el comando confirman desde su propio pool")
def test_banco_m2_por_comando_con_la_cola_frenada_da_el_valor_de_p9(
    cliente, url_app, tmp_path, monkeypatch, capsys
):
    bd, fichas = cliente.app.state.bd, cliente.app.state.fichas
    referencia = _referencia(
        tmp_path / "referencia", piezas="referencia_id,archivo\n", consultas=CONSULTAS_CSV
    )
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(referencia))
    assert main(DETENER) == 0  # el banco corre igual (DEC-96)
    with bd.transaccion() as tx:
        piezas = [_crear(tx, fichas)() for _ in range(4)]
        for pieza_id in piezas[:3]:
            _indexar(tx, fichas, pieza_id)
    entrar(cliente, JORGE)
    generado = cliente.post("/api/informes", json={"desde": "2018-01-01", "hasta": "2022-12-31"})
    informe_id = generado.json()["informe_id"]
    with bd.transaccion() as tx:
        cliente.app.state.indexacion.encolar(tx, piezas[3])  # la cuarta, en cola
    capsys.readouterr()

    assert main(["banco", "m2"]) == 1
    assert "en cola o en curso" in capsys.readouterr().err
    assert main(["banco", "m2", "--informe", informe_id]) == 0

    [corrida] = (referencia / "corridas").glob("m2-*.json")
    datos = json.loads(corrida.read_text(encoding="utf-8"))
    p9 = cliente.get(f"/api/informes/{informe_id}").json()["integridad"]
    assert datos["valor"] == p9["valor"] == 0.75
    assert datos["sin_indexar"] == [str(piezas[3])]
    assert datos["hash_referencia"] == huella(referencia)
    assert "embeber" in datos["configuracion_efectiva"]["por_capacidad"]
    assert {r.suffix for r in (referencia / "corridas").iterdir()} == {".json", ".csv", ".md"}
    assert "| 75,0 % | 3 | 4 |" in capsys.readouterr().out
    with bd.transaccion() as tx:  # sin tablas del banco (DEC-97)
        assert tx.execute(
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_name LIKE ANY"
            " (ARRAY['%referencia%', 'evaluacion%'])"
        ).fetchone()["n"] == 0


def test_banco_m2_de_un_informe_inexistente_sale_con_error(
    url_app, tmp_path, monkeypatch, capsys
):
    referencia = _referencia(
        tmp_path / "referencia", piezas="referencia_id,archivo\n", consultas=CONSULTAS_CSV
    )
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_REFERENCIA", str(referencia))
    informe_id = uuid4()

    assert main(["banco", "m2", "--informe", str(informe_id)]) == 1

    assert f"No existe el informe {informe_id}." in capsys.readouterr().err
    assert not (referencia / "corridas").exists()
