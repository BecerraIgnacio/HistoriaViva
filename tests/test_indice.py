"""El índice vectorial por su interface (spec, módulo `indice`; historia 213, RF-20, RNF-08).

Postgres real con pgvector y la aplicación como `hv_app`. `buscar` consulta con `PGVectorStore`,
así que cada búsqueda prueba también el spike S-16: la biblioteca lee las filas que `reemplazar`
escribió con SQL propio en la transacción de quien llama.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from historia_viva.bd import BaseNoDisponible, Bd
from historia_viva.dominio.originales import ClaseDeOriginal
from historia_viva.dominio.tipos import FiltroDeBusqueda, ViaDeIngreso

# `historia_viva.indice` y SQLAlchemy se importan adentro de cada uso: llama-index tarda decenas
# de segundos en cargar y cada proceso de xdist recolecta toda la suite (ticket 130). El grupo
# junta estos tests en un solo proceso, el único que lo paga.
if TYPE_CHECKING:
    from historia_viva.indice import FragmentoHallado, FragmentoIndexado, Indice

pytestmark = [
    pytest.mark.historia(213),
    pytest.mark.xdist_group("indice"),
    pytest.mark.confirma("`buscar` lee por la conexión de `PGVectorStore`, no por la de `bd`"),
]

MODELO = "embebedor@0123456789ab"


@pytest.fixture(scope="module")
def bd(url_app: str) -> Iterator[Bd]:
    bd = Bd(url_app, tamano_pool=2)
    yield bd
    bd.cerrar()


def nuevo_indice(bd: Bd, dsn: str) -> Indice:
    from historia_viva.indice import Indice

    return Indice(bd=bd, dsn=dsn)


@pytest.fixture(scope="module")
def indice(bd: Bd, url_app: str) -> Indice:
    return nuevo_indice(bd, url_app)


def fragmento(
    pieza_id: UUID,
    *,
    orden: int = 0,
    desde: str = "1970-01-01",
    hasta: str = "1979-12-31",
    tipo: str = "acta",
    tipo_actividad: str = "Transferencia",
    carreras: tuple[str, ...] = (),
    via: ViaDeIngreso = ViaDeIngreso.ARCHIVO,
    modelo: str = MODELO,
    vector: Sequence[float] = (1.0, 0.0),
    texto: str = "Acta de fundación del laboratorio de ensayos",
) -> FragmentoIndexado:
    from historia_viva.indice import FragmentoIndexado

    return FragmentoIndexado(
        fragmento_id=uuid5(pieza_id, str(orden)),
        pieza_id=pieza_id,
        ficha_id=uuid5(pieza_id, "ficha"),
        tipo=tipo,
        tipo_actividad=tipo_actividad,
        carreras=carreras,
        fecha_inicio=date.fromisoformat(desde),
        fecha_fin=date.fromisoformat(hasta),
        clase_original=ClaseDeOriginal.DOCUMENTO,
        ubicacion={"pagina": orden + 1},
        inicio=orden * 100,
        fin=orden * 100 + 99,
        texto=texto,
        modelo_embedding=modelo,
        via_ingreso=via,
        vector=vector,
    )


def indexar(bd: Bd, indice: Indice, *fragmentos: FragmentoIndexado) -> None:
    with bd.transaccion() as tx:
        for pieza_id in {f.pieza_id for f in fragmentos}:
            indice.reemplazar(tx, pieza_id, [f for f in fragmentos if f.pieza_id == pieza_id])


def buscar(
    indice: Indice, vector: Sequence[float] = (1.0, 0.0), **filtro: Any
) -> list[FragmentoHallado]:
    return indice.buscar(vector, modelo_embedding=MODELO, filtro=FiltroDeBusqueda(**filtro), k=50)


@pytest.mark.rnf("RNF-08")
def test_buscar_lee_lo_que_reemplazar_escribio_en_la_transaccion_de_quien_llama(bd, indice):
    from historia_viva.indice import FragmentoHallado

    pieza = uuid4()
    escrito = fragmento(pieza, carreras=("ing-informatica", "ing-mecanica"))

    indexar(bd, indice, escrito)
    hallados = buscar(indice)

    sin_vector = {k: v for k, v in asdict(escrito).items() if k != "vector"}
    assert hallados == [FragmentoHallado(**sin_vector, puntaje=pytest.approx(1.0))]


def _fecha(texto: str | None) -> date | None:
    return date.fromisoformat(texto) if texto else None


@pytest.mark.rf("RF-20")
@pytest.mark.parametrize(
    ("desde", "hasta", "aparece"),
    [
        ("1975-01-01", "1975-12-31", True),
        ("1965-01-01", "1970-01-01", True),
        ("1979-12-31", "1985-12-31", True),
        ("1960-01-01", "1969-12-31", False),
        ("1980-01-01", "1985-12-31", False),
        ("1979-12-31", None, True),
        ("1980-01-01", None, False),
        (None, "1970-01-01", True),
        (None, "1969-12-31", False),
    ],
    ids=[
        "adentro", "borde-inicio", "borde-fin", "antes", "despues", "solo-desde-en-el-borde",
        "solo-desde-despues", "solo-hasta-en-el-borde", "solo-hasta-antes",
    ],
)
def test_una_pieza_de_los_70_aparece_si_su_rango_toca_el_pedido(bd, indice, desde, hasta, aparece):
    pieza = uuid4()
    indexar(bd, indice, fragmento(pieza, desde="1970-01-01", hasta="1979-12-31"))

    hallados = buscar(indice, desde=_fecha(desde), hasta=_fecha(hasta))

    assert [h.pieza_id for h in hallados] == ([pieza] if aparece else [])


# Tres Piezas que se distinguen por cada filtro: la A tiene dos Carreras y la C ninguna.
PIEZAS = {nombre: uuid5(UUID(int=0), nombre) for nombre in "ABC"}


@pytest.fixture
def tres_piezas(bd: Bd, indice: Indice) -> None:
    indexar(
        bd, indice,
        fragmento(PIEZAS["A"], tipo="acta", tipo_actividad="Transferencia",
                  carreras=("ing-informatica", "ing-mecanica")),
        fragmento(PIEZAS["B"], tipo="fotografia", tipo_actividad="Consultoría",
                  carreras=("ing-electronica",), via=ViaDeIngreso.APORTE),
        fragmento(PIEZAS["C"], tipo="acta", tipo_actividad="Consultoría"),
    )


@pytest.mark.rf("RF-20")
@pytest.mark.parametrize(
    ("filtro", "esperadas"),
    [
        ({}, "ABC"),
        ({"tipos": ("acta",)}, "AC"),
        ({"tipos": ("acta", "fotografia")}, "ABC"),
        ({"tipos_actividad": ("Consultoría",)}, "BC"),
        ({"carreras": ("ing-informatica",)}, "A"),
        ({"carreras": ("ing-mecanica",)}, "A"),
        ({"carreras": ("ing-electronica", "ing-mecanica")}, "AB"),
        ({"carreras": ("ing-civil",)}, ""),
        ({"via_ingreso": ViaDeIngreso.APORTE}, "B"),
        ({"tipos": ("acta",), "tipos_actividad": ("Consultoría",)}, "C"),
    ],
    ids=[
        "sin-filtro", "un-tipo", "dos-tipos", "tipo-de-actividad", "una-de-sus-dos-carreras",
        "la-otra-de-sus-dos-carreras", "cualquiera-de-dos-carreras", "carrera-sin-piezas",
        "via-de-ingreso", "tipo-y-tipo-de-actividad",
    ],
)
def test_el_filtro_deja_solo_lo_que_coincide(tres_piezas, indice, filtro, esperadas):
    hallados = buscar(indice, **filtro)

    assert sorted(h.pieza_id for h in hallados) == sorted(PIEZAS[n] for n in esperadas)


# Los filtros van como parámetros: una comilla, unos dos puntos, un porcentaje o una barra en un
# nombre del catálogo no pueden cambiar la consulta.
@pytest.mark.rf("RF-20")
@pytest.mark.parametrize(
    "valor",
    ["Charla de O'Higgins", "Taller :invitado", "Ciclo 100% FIE", r"Barra \ y \:dos"],
    ids=["comilla", "dos-puntos", "porcentaje", "barra"],
)
def test_un_valor_con_signos_de_sql_filtra_como_cualquier_otro(bd, indice, valor):
    pieza = uuid4()
    indexar(
        bd, indice, fragmento(pieza, tipo_actividad=valor, carreras=(valor,)), fragmento(uuid4())
    )

    hallados = buscar(indice, tipos_actividad=(valor,), carreras=(valor,))

    assert [h.pieza_id for h in hallados] == [pieza]


@pytest.mark.rf("RF-20")
def test_un_filtro_hostil_no_trae_lo_que_no_coincide(bd, indice):
    indexar(bd, indice, fragmento(uuid4()))

    assert buscar(indice, tipos_actividad=("x') OR true OR ('",)) == []
    assert buscar(indice, carreras=("x'] OR true OR array['",)) == []


@pytest.fixture
def al_conectar() -> Iterator[list[str]]:
    """Sentencias que corre cada conexión nueva de un motor de SQLAlchemy, antes de usarse."""
    from sqlalchemy import event
    from sqlalchemy.pool import Pool

    sentencias: list[str] = []

    def correr(conexion: Any, _registro: Any) -> None:
        for sentencia in sentencias:
            conexion.execute(sentencia)
        conexion.commit()

    event.listen(Pool, "connect", correr)
    yield sentencias
    event.remove(Pool, "connect", correr)


# Con `standard_conforming_strings = off`, la barra escapa la comilla dentro de un literal: ningún
# escape a mano aguanta eso, solo los parámetros.
@pytest.mark.rf("RF-20")
def test_un_filtro_hostil_no_trae_nada_aunque_la_barra_escape_comillas(bd, url_app, al_conectar):
    indexar(bd, nuevo_indice(bd, url_app), fragmento(uuid4()))
    al_conectar.append("SET standard_conforming_strings = off")
    indice = nuevo_indice(bd, url_app)

    assert buscar(indice, tipos_actividad=(r"x\') OR true --",)) == []
    assert buscar(indice, carreras=(r"x\'] OR true --",)) == []


def _textos(indice: Indice) -> list[str]:
    return sorted(h.texto for h in buscar(indice))


@pytest.mark.rnf("RNF-08")
def test_reemplazar_dos_veces_lo_mismo_no_duplica(bd, indice):
    pieza = uuid4()
    version = [fragmento(pieza, orden=n, texto=f"página {n}") for n in range(3)]

    indexar(bd, indice, *version)
    indexar(bd, indice, *version)

    assert _textos(indice) == ["página 0", "página 1", "página 2"]


@pytest.mark.rnf("RNF-08")
def test_reemplazar_con_menos_fragmentos_borra_los_que_sobran(bd, indice):
    pieza = uuid4()
    indexar(bd, indice, *[fragmento(pieza, orden=n, texto=f"página {n}") for n in range(3)])

    indexar(bd, indice, fragmento(pieza, texto="la única página"))

    assert _textos(indice) == ["la única página"]


@pytest.mark.rnf("RNF-08")
def test_si_la_transaccion_de_quien_llama_se_revierte_no_queda_nada(bd, indice):
    with pytest.raises(RuntimeError), bd.transaccion() as tx:
        indice.reemplazar(tx, (pieza := uuid4()), [fragmento(pieza)])
        raise RuntimeError("la transición de la Pieza falló")

    assert buscar(indice) == []


@pytest.mark.rnf("RNF-08")
def test_otra_conexion_ve_la_version_anterior_hasta_el_commit(bd, indice):
    pieza = uuid4()
    indexar(bd, indice, *[fragmento(pieza, orden=n, texto=f"vieja {n}") for n in range(2)])

    with bd.transaccion() as tx:
        indice.reemplazar(tx, pieza, [fragmento(pieza, texto="nueva")])
        # `buscar` lee por la conexión de `PGVectorStore`, no por la de `tx`.
        en_el_medio = _textos(indice)

    assert en_el_medio == ["vieja 0", "vieja 1"]
    assert _textos(indice) == ["nueva"]


def _hasta_que_otra_conexion_espere_un_candado(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        for _ in range(100):
            if conexion.execute(
                "SELECT 1 FROM pg_stat_activity"
                " WHERE wait_event_type = 'Lock' AND datname = current_database()"
            ).fetchone():
                return
            time.sleep(0.05)
    pytest.fail("La segunda transacción nunca esperó a la primera.")


@pytest.mark.rnf("RNF-08")
def test_dos_reemplazar_a_la_vez_sobre_la_misma_pieza_no_chocan(bd, indice, url_app):
    pieza = uuid4()

    with ThreadPoolExecutor(1) as hilo:
        with bd.transaccion() as tx:
            indice.reemplazar(tx, pieza, [fragmento(pieza, texto="primera")])
            segunda = hilo.submit(indexar, bd, indice, fragmento(pieza, texto="segunda"))
            _hasta_que_otra_conexion_espere_un_candado(url_app)
        segunda.result()

    assert _textos(indice) == ["segunda"]


@pytest.mark.rnf("RNF-08")
def test_reemplazar_rechaza_un_fragmento_de_otra_pieza(bd, indice):
    pieza, otra = uuid4(), uuid4()

    with pytest.raises(ValueError, match=str(otra)), bd.transaccion() as tx:
        indice.reemplazar(tx, pieza, [fragmento(pieza), fragmento(otra)])


@pytest.mark.rnf("RNF-08")
def test_quitar_desindexa_toda_la_pieza_y_nada_mas(bd, indice):
    retirada, otra = uuid4(), uuid4()
    indexar(bd, indice, fragmento(retirada, orden=0), fragmento(retirada, orden=1), fragmento(otra))

    with bd.transaccion() as tx:
        quitados = indice.quitar(tx, retirada)

    assert quitados == 2
    assert [h.pieza_id for h in buscar(indice)] == [otra]


@pytest.mark.rf("RF-15")
@pytest.mark.historia(203)
def test_ids_de_piezas_devuelve_las_que_tienen_algun_fragmento_de_cualquier_modelo(bd, indice):
    from historia_viva.indice import ids_de_piezas

    una, otra, quitada = uuid4(), uuid4(), uuid4()
    indexar(
        bd, indice, fragmento(una, orden=0), fragmento(una, orden=1),
        fragmento(otra, modelo="otro@0123456789ab"), fragmento(quitada),
    )
    with bd.transaccion() as tx:
        indice.quitar(tx, quitada)

    with bd.transaccion() as tx:
        assert ids_de_piezas(tx) == {una, otra}


@pytest.mark.historia(138)
def test_de_pieza_devuelve_todos_sus_fragmentos_de_cualquier_modelo_en_orden(bd, indice):
    pieza, otra = uuid4(), uuid4()
    escritos = [
        fragmento(pieza, orden=1, modelo="otro@0123456789ab", texto="segunda"),
        fragmento(pieza, orden=0, texto="primera"),
    ]
    indexar(bd, indice, *escritos, fragmento(otra))

    leidos = indice.de_pieza(pieza)

    sin_vector = [{k: v for k, v in asdict(f).items() if k != "vector"} for f in escritos[::-1]]
    assert [asdict(f) for f in leidos] == sin_vector
    assert indice.de_pieza(uuid4()) == []


def _eje(*ejes: int, dimension: int = 384) -> list[float]:
    return [1.0 if i in ejes else 0.0 for i in range(dimension)]


@pytest.mark.rf("RF-20")
def test_un_vector_de_384_se_rellena_y_conserva_el_orden_de_similitud(bd, indice):
    # Contra el último eje: si el relleno cortara o corriera el vector, el orden cambiaría.
    igual, a_45_grados, perpendicular = uuid4(), uuid4(), uuid4()
    indexar(
        bd, indice,
        fragmento(perpendicular, vector=_eje(0)),
        fragmento(igual, vector=_eje(383)),
        fragmento(a_45_grados, vector=_eje(0, 383)),
    )

    hallados = buscar(indice, _eje(383))

    assert [(h.pieza_id, h.puntaje) for h in hallados] == [
        (igual, pytest.approx(1.0)),
        (a_45_grados, pytest.approx(0.5**0.5)),
        (perpendicular, pytest.approx(0.0)),
    ]


@pytest.mark.rf("RF-20")
def test_un_vector_de_mas_de_1024_se_rechaza(bd, indice):
    pieza = uuid4()

    with pytest.raises(ValueError, match="1025"):
        buscar(indice, _eje(0, dimension=1025))
    with pytest.raises(ValueError, match="1025"), bd.transaccion() as tx:
        indice.reemplazar(tx, pieza, [fragmento(pieza, vector=_eje(0, dimension=1025))])


@pytest.mark.rf("RF-20")
def test_los_fragmentos_de_otro_modelo_no_se_ven_y_dejan_el_indice_incompleto(bd, indice):
    vigente, vieja = uuid4(), uuid4()
    indexar(
        bd, indice,
        fragmento(vigente),
        fragmento(vieja, orden=0, modelo="anterior@ffffffffffff"),
        fragmento(vieja, orden=1, modelo="anterior@ffffffffffff"),
    )

    hallados = buscar(indice)
    estado = indice.estado(MODELO)

    assert [h.pieza_id for h in hallados] == [vigente]
    assert (estado.fragmentos, estado.de_otro_modelo, estado.incompleto) == (3, 2, True)


@pytest.mark.rf("RF-20")
def test_con_un_solo_modelo_el_indice_esta_completo(bd, indice):
    indexar(bd, indice, fragmento(uuid4()))

    estado = indice.estado(MODELO)

    assert (estado.fragmentos, estado.de_otro_modelo, estado.incompleto) == (1, 0, False)


# Spike S-16 (044): lo que la biblioteca manda a la base, capturado en el motor de SQLAlchemy.
@pytest.fixture
def sentencias() -> Iterator[list[tuple[str, str, Any]]]:
    """`(driver, sentencia, parámetros)` de cada sentencia que ejecuta un motor de SQLAlchemy."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    capturadas: list[tuple[str, str, Any]] = []

    def capturar(conexion: Any, _cursor: Any, sentencia: str, parametros: Any, *_: Any) -> None:
        capturadas.append((conexion.dialect.driver, sentencia, parametros))

    event.listen(Engine, "before_cursor_execute", capturar)
    yield capturadas
    event.remove(Engine, "before_cursor_execute", capturar)


def test_la_tienda_conecta_con_psycopg_3_y_no_crea_nada(bd, url_app, sentencias):
    indexar(bd, nuevo_indice(bd, url_app), fragmento(uuid4()))

    # Una tienda nueva: su primera consulta es la que haría el setup de la biblioteca.
    hallados = buscar(nuevo_indice(bd, url_app))

    assert len(hallados) == 1
    assert [(driver, sentencia.split()[0]) for driver, sentencia, _ in sentencias] == [
        ("psycopg", "SELECT")
    ]


# DEC-145: la conexión de la tienda espera un candado y queda ociosa lo mismo que las de `bd`.
@pytest.mark.historia(55)
def test_la_tienda_conecta_con_los_mismos_limites_que_bd(bd, url_app):
    from sqlalchemy import event
    from sqlalchemy.pool import Pool

    ajustes = []

    def leer(conexion: Any, _registro: Any) -> None:
        ajustes.append(conexion.execute(
            "SELECT current_setting('lock_timeout'),"
            " current_setting('idle_in_transaction_session_timeout')"
        ).fetchone())
        conexion.rollback()

    event.listen(Pool, "connect", leer)
    try:
        buscar(nuevo_indice(bd, url_app))
    finally:
        event.remove(Pool, "connect", leer)

    assert ajustes == [("5s", "1min")]


def _nodos(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for hijo in plan.get("Plans", []):
        yield from _nodos(hijo)


@pytest.mark.rf("RF-20")
def test_la_tienda_filtra_antes_de_ordenar_y_compara_las_fechas_como_texto(
    bd, indice, url_app, sentencias
):
    indexar(bd, indice, fragmento(uuid4(), carreras=("ing-informatica",)))

    buscar(
        indice, desde=date(1975, 1, 1), hasta=date(1975, 12, 31), tipos=("acta",),
        tipos_actividad=("Transferencia",), carreras=("ing-informatica",),
        via_ingreso=ViaDeIngreso.ARCHIVO,
    )
    [(consulta, parametros)] = [(s, p) for _, s, p in sentencias if "<=>" in s]
    with psycopg.connect(url_app) as conexion:
        fila = conexion.execute(
            ("EXPLAIN (FORMAT JSON) " + consulta).encode(), parametros
        ).fetchone()
    assert fila is not None
    [orden] = [n for n in _nodos(fila[0][0]["Plan"]) if n["Node Type"] == "Sort"]
    debajo_del_orden = " AND ".join(
        n.get(clave, "") for n in _nodos(orden) for clave in ("Filter", "Recheck Cond", "Index Cond")
    )

    # Los valores viajan como parámetros, no dentro del texto de la consulta.
    for valor in ["1975-12-31", "Transferencia", "ing-informatica", "archivo", MODELO]:
        assert valor not in consulta
    assert "<=>" in orden["Sort Key"][0]
    for condicion in [
        "(metadata_ ->> 'fecha_inicio'::text) <= '1975-12-31'::text",
        "(metadata_ ->> 'fecha_fin'::text) >= '1975-01-01'::text",
        "metadata_['carreras'::text] ?| '{ing-informatica}'::text[]",
        "(metadata_ ->> 'tipo'::text) = 'acta'::text",
        "(metadata_ ->> 'tipo_actividad'::text) = 'Transferencia'::text",
        "(metadata_ ->> 'via_ingreso'::text) = 'archivo'::text",
        f"(metadata_ ->> 'modelo_embedding'::text) = '{MODELO}'::text",
    ]:
        assert condicion in debajo_del_orden


# DEC-143: el índice es derivado y reconstruible; ningún dato del Acervo se borra.
def test_hv_app_borra_solo_en_la_tabla_del_indice(url_app):
    borra = {}
    with psycopg.connect(url_app, autocommit=True) as conexion:
        tablas = conexion.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'acervo' ORDER BY 1"
        ).fetchall()
        for (tabla,) in tablas:
            sentencia = sql.SQL("DELETE FROM acervo.{} WHERE false").format(sql.Identifier(tabla))
            try:
                conexion.execute(sentencia)
                borra[tabla] = True
            except psycopg.errors.InsufficientPrivilege:
                borra[tabla] = False

    assert len(borra) > 1
    assert [tabla for tabla, puede in borra.items() if puede] == ["data_fragmento"]


# Historia 55: con la base caída, buscar falla cerrado como el resto, sin colgar el pedido.
@pytest.mark.historia(55)
def test_con_la_base_caida_buscar_dice_que_no_esta_disponible(bd, url_app):
    caida = nuevo_indice(bd, make_conninfo(url_app, port=1))

    with pytest.raises(BaseNoDisponible):
        buscar(caida)


# 050 (DEC-146 punto 12): sin vector, `listar` filtra igual que `buscar` y ordena por Fecha;
# dentro de una Pieza, en el orden del cuerpo.
@pytest.mark.rf("RF-20")
def test_listar_sin_vector_ordena_por_fecha_y_fragmento_hasta_el_limite(bd, indice):
    vieja, nueva, otra = uuid4(), uuid4(), uuid4()
    indexar(
        bd, indice,
        fragmento(nueva, orden=0, desde="1990-01-01", hasta="1990-12-31"),
        fragmento(vieja, orden=0, desde="1975-01-01", hasta="1975-12-31"),
        fragmento(vieja, orden=1, desde="1975-01-01", hasta="1975-12-31"),
        fragmento(otra, desde="1960-01-01", hasta="1960-12-31", tipo="fotografia"),
    )

    listados = indice.listar(
        filtro=FiltroDeBusqueda(tipos=("acta",)), modelo_embedding=MODELO, limite=2
    )

    assert [h.fragmento_id for h in listados] == [uuid5(vieja, "0"), uuid5(vieja, "1")]
    assert indice.listar(filtro=FiltroDeBusqueda(), modelo_embedding=MODELO, limite=10)[-1] \
        .pieza_id == nueva


@pytest.mark.rf("RF-20")
@pytest.mark.parametrize(
    ("filtro", "esperadas"),
    [
        ({"tipos": ("acta", "fotografia")}, "ABC"),
        ({"tipos_actividad": ("Consultoría",)}, "BC"),
        ({"carreras": ("ing-electronica", "ing-mecanica")}, "AB"),
        ({"desde": date(1980, 1, 1)}, ""),
    ],
    ids=["dos-tipos", "tipo-de-actividad", "cualquiera-de-dos-carreras", "fechas"],
)
def test_listar_filtra_como_buscar(tres_piezas, indice, filtro, esperadas):
    listados = indice.listar(filtro=FiltroDeBusqueda(**filtro), modelo_embedding=MODELO, limite=10)

    assert sorted(h.pieza_id for h in listados) == sorted(PIEZAS[n] for n in esperadas)


@pytest.mark.rf("RF-20")
def test_listar_no_devuelve_fragmentos_de_otro_modelo(bd, indice):
    vigente = uuid4()
    indexar(bd, indice, fragmento(vigente), fragmento(uuid4(), modelo="anterior@ffffffffffff"))

    listados = indice.listar(filtro=FiltroDeBusqueda(), modelo_embedding=MODELO, limite=10)

    assert [h.pieza_id for h in listados] == [vigente]


# 044 dejó la tienda esperando 30 s un lugar del pool y saliendo con `TimeoutError` de SQLAlchemy:
# con la espera de `bd`, el pedido que no consigue conexión falla cerrado antes que el candado.
@pytest.mark.historia(55)
def test_con_el_pool_de_la_tienda_agotado_buscar_dice_que_no_esta_disponible(
    bd, indice, url_bd
):
    indexar(bd, indice, fragmento(uuid4()))
    buscar(indice)  # la tienda ya conectó una vez

    def medir() -> tuple[float, type[BaseException] | None]:
        comienzo = time.monotonic()
        try:
            buscar(indice)
        except Exception as error:  # noqa: BLE001
            return time.monotonic() - comienzo, type(error)
        return time.monotonic() - comienzo, None

    with psycopg.connect(url_bd) as candado:
        # Cada búsqueda toma su conexión y espera el candado (5 s); las que sobran, el pool.
        candado.execute("LOCK TABLE acervo.data_fragmento IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=20) as hilos:
            resultados = list(hilos.map(lambda _: medir(), range(20)))
        candado.rollback()

    assert {error for _, error in resultados} == {BaseNoDisponible}
    assert min(duracion for duracion, _ in resultados) < 4.5


@pytest.mark.rf("RF-20")
def test_listar_con_tope_por_pieza_deja_lugar_a_las_demas(bd, indice):
    larga, otra = uuid4(), uuid4()
    indexar(
        bd, indice,
        *(fragmento(larga, orden=n, desde="1960-01-01", hasta="1960-12-31") for n in range(5)),
        fragmento(otra, desde="1990-01-01", hasta="1990-12-31"),
    )

    listados = indice.listar(
        filtro=FiltroDeBusqueda(), modelo_embedding=MODELO, limite=3, por_pieza=2
    )

    assert [(h.pieza_id, h.inicio) for h in listados] == [(larga, 0), (larga, 100), (otra, 0)]


@pytest.mark.rf("RF-20")
def test_el_modelo_mayoritario_sale_del_indice_por_el_nombre_del_modelo(bd, indice):
    indexar(
        bd, indice,
        fragmento(uuid4(), modelo="bge-m3@aaaaaaaaaaaa"),
        fragmento(uuid4(), modelo="bge-m3@bbbbbbbbbbbb"),
        fragmento(uuid4(), modelo="bge-m3@bbbbbbbbbbbb"),
        *(fragmento(uuid4(), modelo="e5@cccccccccccc") for _ in range(3)),
    )

    assert indice.modelo_mayoritario("bge-m3@") == "bge-m3@bbbbbbbbbbbb"
    assert indice.modelo_mayoritario("bge_m3@") is None
