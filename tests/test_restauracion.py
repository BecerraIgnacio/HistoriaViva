"""Restauración probada en un proyecto aparte (historia 230, DEC-144; spec §2.3).

Sin Docker, la restauración corre igual que en la PC salvo tres dobles: `docker compose` solo se
anota, `restic restore` copia los volcados y los originales del origen, y las 3 consultas de
referencia de `web` son una búsqueda exacta de vecinos con SQL (la búsqueda de verdad la ensaya
`test_busqueda`, runbook R13). `pg_dump`, `psql` y
`pg_restore` son los del PATH (los de pgserver: `pginstall/bin`), contra la base de pruebas: el
origen es una base migrada con un corpus de prueba y la restaurada, otra recién salida del
bootstrap, como el volumen nuevo de `historiaviva-restauracion`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
import yaml
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from base_de_pruebas import vaciar_base
from historia_viva.migraciones import aplicar
from historia_viva.respaldo import (
    Correr,
    Respaldo,
    RespaldoFallido,
    RespaldoMalConfigurado,
    correr_con,
)
from historia_viva.respaldo.restauracion import (
    TITULOS,
    Restauracion,
    _en_el_snapshot,
    componer,
    main,
)
from test_respaldo import entorno_de_la_pc

pytestmark = pytest.mark.historia(230)

MVP = Path(__file__).resolve().parent.parent

sin_herramientas_de_postgres = pytest.mark.skipif(
    any(shutil.which(h) is None for h in ("pg_dump", "pg_restore", "psql")),
    reason="sin pg_dump, pg_restore ni psql en el PATH (los de pgserver: pginstall/bin)",
)

VECINOS = (
    "SELECT coalesce(json_agg(node_id), '[]') AS vecinos FROM (SELECT node_id"
    " FROM acervo.data_fragmento ORDER BY embedding <=> (SELECT embedding"
    " FROM acervo.data_fragmento ORDER BY node_id LIMIT 1), node_id LIMIT 10) AS v"
)


def con_la_busqueda(correr: Correr, dsn: str) -> Correr:
    """`correr` con un doble de `consultas-de-referencia` en `web`: los 10 fragmentos más
    cercanos al primero, sobre la base `dsn`."""

    def doble(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        if "consultas-de-referencia" not in comando:
            return correr(comando, salida)
        assert salida is not None
        with psycopg.connect(dsn) as conexion:
            fila = conexion.execute(VECINOS).fetchone()
        assert fila is not None
        salida.write_text(json.dumps({"vecinos-en-el-indice": fila[0]}), encoding="utf-8")
        return 0, ""

    return doble

HACE_UN_RATO = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
PIEZAS = {
    UUID("00000000-0000-4000-8000-000000000001"): "Recibida",
    UUID("00000000-0000-4000-8000-000000000002"): "Pendiente",
    UUID("00000000-0000-4000-8000-000000000003"): "Pendiente",
}
SIN_ALERTA = '{"alerta": false, "version_catalogo": "0", "hallazgos": []}'
METADATOS = (
    '{"pieza_id": "p", "ficha_id": "f", "tipo": "t", "tipo_actividad": "a", "carreras": [],'
    ' "fecha_inicio": null, "fecha_fin": null, "modelo_embedding": "m", "via_ingreso": "archivo"}'
)


def _vector(*componentes: tuple[int, float]) -> str:
    valores = [0.0] * 1024
    for posicion, valor in componentes:
        valores[posicion] = valor
    return str(valores)


FRAGMENTOS = {  # el 1 queda cerca del 0; el 2, ortogonal
    "fragmento-0": _vector((0, 1.0)),
    "fragmento-1": _vector((0, 1.0), (1, 0.2)),
    "fragmento-2": _vector((2, 1.0)),
}


def sembrar(dsn: str, originales: Path) -> None:
    """Un corpus de prueba: tres Piezas con su original, tres versiones de Ficha y tres
    fragmentos del índice."""
    originales.mkdir(parents=True)
    with psycopg.connect(dsn, autocommit=True) as conexion:
        for orden, (pieza_id, estado) in enumerate(PIEZAS.items()):
            contenido = f"%PDF-1.4 original {orden}".encode()
            (originales / str(pieza_id)).write_bytes(contenido)
            conexion.execute(
                "INSERT INTO acervo.pieza (pieza_id, estado, clase_original, via_ingreso,"
                " tipo_mime, nombre_archivo, tamano_bytes, huella_original, archivo_original_id,"
                " creada_el) VALUES (%s, %s, 'documento', 'archivo', 'application/pdf',"
                " %s, %s, %s, %s, %s)",
                (
                    pieza_id, estado, f"acta-{orden}.pdf", len(contenido),
                    hashlib.sha256(contenido).hexdigest(), f"local://originales/{pieza_id}",
                    HACE_UN_RATO + timedelta(minutes=orden),
                ),
            )  # fmt: skip
        segunda, tercera = list(PIEZAS)[1:]
        conexion.execute(
            "INSERT INTO acervo.ficha (ficha_id, pieza_id, version, contenido_markdown,"
            " origen_version, vigente, informe_inyeccion) VALUES"
            " (gen_random_uuid(), %s, 1, 'Acta de una reunión', 'modelo', false, %s),"
            " (gen_random_uuid(), %s, 2, 'Acta del consejo de la Facultad', 'sistema', true, %s),"
            " (gen_random_uuid(), %s, 1, 'Foto de un acto', 'modelo', true, %s)",
            (
                segunda, SIN_ALERTA, segunda, SIN_ALERTA, tercera, SIN_ALERTA,
            ),  # fmt: skip
        )
        for node_id, vector in FRAGMENTOS.items():
            conexion.execute(
                "INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)"
                " VALUES (%s, 'texto', %s, %s::vector)",
                (node_id, METADATOS, vector),
            )


@pytest.fixture
def origen(base_vacia: str, url_superusuario: str, tmp_path: Path) -> Respaldo:
    """El respaldo de una base con el corpus de prueba, con el `pg_dump` del PATH."""
    aplicar(base_vacia)
    sembrar(base_vacia, tmp_path / "origen" / "originales")
    dsn = make_conninfo(url_superusuario, dbname=conninfo_to_dict(base_vacia)["dbname"])
    return Respaldo(
        volcados=tmp_path / "origen" / "volcados",
        originales=tmp_path / "origen" / "originales",
        compose=(),
        pg=(),
        dsn=dsn,
        correr=con_la_busqueda(correr_con(os.environ), dsn),
    )


@sin_herramientas_de_postgres
def test_el_volcado_anota_los_conteos_y_las_consultas_de_referencia(origen: Respaldo):
    volcado = origen.volcar("manual")

    referencia = json.loads(volcado.referencia.read_text(encoding="utf-8"))
    assert referencia["conteos"] == {
        "piezas_por_estado": {"Recibida": 1, "Pendiente": 2},
        "fichas_por_version": {"1": 2, "2": 1},
    }
    # Lo que respondió la búsqueda de `web` (acá, su doble).
    assert referencia["consultas"] == {
        "vecinos-en-el-indice": ["fragmento-0", "fragmento-1", "fragmento-2"],
    }


class Doble:
    """`Correr` de la restauración sin Docker ni restic: `docker compose` solo se anota y
    `restic restore` copia los volcados y los originales del origen al snapshot. `psql` y
    `pg_restore` corren de verdad. `sabotaje` se aplica a lo restaurado al pedir `migrar`."""

    def __init__(self, origen: Respaldo, restauracion: Restauracion) -> None:
        self.origen, self.restauracion = origen, restauracion
        self.comandos: list[list[str]] = []
        self.sabotaje: Callable[[Restauracion], None] | None = None
        self.falla: str | None = None
        self._real = con_la_busqueda(correr_con(os.environ), restauracion.dsn)

    def __call__(self, comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        self.comandos.append(list(comando))
        if self.falla in comando:
            return 1, "error: no se pudo"
        if comando[0] == "restic":
            shutil.copytree(self.origen.volcados, self.restauracion.volcados)
            shutil.copytree(self.origen.originales, self.restauracion.originales)
            return 0, ""
        if comando[0] == "docker" and "consultas-de-referencia" not in comando:
            if "migrar" in comando and self.sabotaje:
                self.sabotaje(self.restauracion)
            return 0, ""
        return self._real(comando, salida)

    def pasos(self) -> list[str]:
        return [" ".join(c[2:]) if c[0] == "docker" else c[0] for c in self.comandos]


class Reloj:
    def __init__(self, paso: timedelta = timedelta(minutes=1)) -> None:
        self.hora, self.paso = HACE_UN_RATO, paso

    def __call__(self) -> datetime:
        self.hora += self.paso
        return self.hora


@pytest.fixture
def restauracion(
    origen: Respaldo, url_superusuario: str, worker_id: str, tmp_path: Path
) -> tuple[Restauracion, Doble]:
    """La restauración sobre una base recién salida del bootstrap, como el volumen nuevo del
    proyecto aparte."""
    base = f"hv_restaurada_{worker_id}"
    vaciar_base(url_superusuario, base)
    dsn = make_conninfo(url_superusuario, dbname=base)
    snapshot = tmp_path / "restauracion" / "snapshot"
    restauracion = Restauracion(
        snapshot=snapshot,
        volcados=snapshot / "volcados",
        originales=snapshot / "originales",
        compose=("docker", "compose"),
        pg=(),
        dsn_pg=dsn,
        volcados_en_pg=str(snapshot / "volcados"),
        dsn=dsn,
        correr=lambda c, s: (0, ""),
        ahora=Reloj(),
    )
    doble = Doble(origen, restauracion)
    return replace(restauracion, correr=doble), doble


@sin_herramientas_de_postgres
def test_restaura_el_ultimo_respaldo_y_pasan_los_seis_criterios(
    origen: Respaldo, restauracion: tuple[Restauracion, Doble]
):
    replace(origen, ahora=lambda: HACE_UN_RATO).volcar("manual")
    ultimo = replace(origen, ahora=lambda: HACE_UN_RATO + timedelta(hours=4)).volcar("manual")
    proceso, doble = restauracion

    resultado = proceso.restaurar(quien="Ignacio")

    # Los globales antes que la base; `migrar` después; el proyecto se levanta sin Caddy.
    assert doble.pasos() == [
        "down -v",  # cada prueba con un volumen nuevo
        "restic",
        "up -d --wait postgres",
        "psql",
        "pg_restore",
        "run --rm migrar",
        "up -d --wait web ollama-embeddings",
        "exec -T web python -m historia_viva.composicion consultas-de-referencia",
    ]
    psql, pg_restore = doble.comandos[3], doble.comandos[4]
    assert psql[psql.index("-f") + 1].endswith(ultimo.globales.name)
    assert "--clean" not in pg_restore  # sobre un volumen nuevo
    assert pg_restore[-1].endswith(ultimo.base.name)
    assert [(c.clave, c.pasa) for c in resultado.criterios] == [
        ("conteos", True),
        ("originales", True),
        ("muestra", True),
        ("consultas", True),
        ("roles", True),
        ("tiempo", True),
    ]
    assert resultado.pasa
    for anotado in ("Quién: Ignacio", ultimo.base.name, "Tiempo total: 0 h 01 min"):
        assert anotado in resultado.informe
    assert "Desvíos: ninguno" in resultado.informe
    [archivo] = proceso.snapshot.parent.glob("informe-*.txt")
    assert archivo.read_text(encoding="utf-8") == resultado.informe


PRIMERA = next(iter(PIEZAS))


def _en_la_restaurada(sentencia: str) -> Callable[[Restauracion], None]:
    def sabotear(restauracion: Restauracion) -> None:
        with psycopg.connect(restauracion.dsn, autocommit=True) as conexion:
            conexion.execute(sentencia)

    return sabotear


def _borrar_original(restauracion: Restauracion) -> None:
    (restauracion.originales / str(PRIMERA)).unlink()


def _alterar_original(restauracion: Restauracion) -> None:
    (restauracion.originales / str(PRIMERA)).write_bytes(b"%PDF-1.4 otro")


@sin_herramientas_de_postgres
@pytest.mark.parametrize(
    ("sabotaje", "fallan"),
    [
        (_borrar_original, {"originales", "muestra"}),
        (_alterar_original, {"originales", "muestra"}),
        (
            _en_la_restaurada(
                f"UPDATE acervo.pieza SET estado = 'Pendiente' WHERE pieza_id = '{PRIMERA}'"
            ),
            {"conteos"},
        ),
        (
            _en_la_restaurada("DELETE FROM acervo.data_fragmento WHERE node_id = 'fragmento-1'"),
            {"consultas"},
        ),
        (_en_la_restaurada("GRANT DELETE ON acervo.ficha TO hv_app"), {"roles"}),
    ],
    ids=[
        "original-borrado",
        "original-alterado",
        "conteo-alterado",
        "consulta-distinta",
        "hv_app-borra",
    ],
)
def test_cada_criterio_falla_si_se_lo_sabotea(
    origen: Respaldo,
    restauracion: tuple[Restauracion, Doble],
    sabotaje: Callable[[Restauracion], None],
    fallan: set[str],
):
    origen.volcar("manual")
    proceso, doble = restauracion
    doble.sabotaje = sabotaje

    resultado = proceso.restaurar(quien="Ignacio")

    assert {c.clave for c in resultado.criterios if not c.pasa} == fallan
    assert "Resultado: FALLA" in resultado.informe
    for clave in fallan:
        assert f"- {TITULOS[clave]}: " in resultado.informe  # en los desvíos


@sin_herramientas_de_postgres
def test_si_la_busqueda_de_la_restaurada_no_responde_las_consultas_fallan(
    origen: Respaldo, restauracion: tuple[Restauracion, Doble]
):
    origen.volcar("manual")
    proceso, doble = restauracion
    doble.falla = "consultas-de-referencia"  # p. ej., sin los modelos del origen

    resultado = proceso.restaurar(quien="Ignacio")

    [consultas] = [c for c in resultado.criterios if not c.pasa]
    assert consultas.clave == "consultas"
    assert consultas.detalle == (
        "la restaurada no respondió: la búsqueda salió con 1: error: no se pudo"
    )
    assert "Resultado: FALLA" in resultado.informe


@sin_herramientas_de_postgres
def test_si_pasa_de_4_h_se_redefine_el_rto(
    origen: Respaldo, restauracion: tuple[Restauracion, Doble]
):
    origen.volcar("manual")
    proceso, _ = restauracion

    resultado = replace(proceso, ahora=Reloj(paso=timedelta(hours=4, minutes=1))).restaurar(
        quien="Ignacio"
    )

    [tiempo] = [c for c in resultado.criterios if not c.pasa]
    assert tiempo == ("tiempo", False, "4 h 01 min: se redefine el RTO")


@pytest.mark.skipif(shutil.which("docker") is None, reason="sin Docker no hay `compose config`")
def test_el_proyecto_aparte_no_comparte_volumenes_ni_puertos_y_no_tiene_caddy(tmp_path: Path):
    # `COMPOSE_PROJECT_NAME` le gana al `name:` del yml: sin `--project-name`, `up` recrearía
    # los contenedores del origen.
    archivo = entorno_de_la_pc(
        tmp_path / "pc",
        HV_CLAVE_MIGRADOR="b2",
        HV_CLAVE_APP="c3",
        COMPOSE_PROJECT_NAME="historiaviva-mvp",
    )
    restauracion = componer(archivo, {}, tmp_path / "restauracion")
    variables = {
        "COMPOSE_PROJECT_NAME": "historiaviva-mvp",
        "HV_ORIGINALES": str(tmp_path / "originales"),
        "HV_VOLCADOS_RESTAURADOS": str(tmp_path / "volcados"),
    }
    salida = subprocess.run(
        [*restauracion.compose, "config", "--format", "json"],
        env={**os.environ, **variables},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    compose = json.loads(salida)

    assert compose["name"] == "historiaviva-restauracion"
    assert "caddy" not in compose["services"]
    del_origen = yaml.safe_load((MVP / "compose.yml").read_text(encoding="utf-8"))
    propios = {v["name"] for v in compose["volumes"].values() if not v.get("external")}
    assert propios.isdisjoint(v["name"] for v in del_origen["volumes"].values())
    # Del origen, solo los modelos y de solo lectura, para embeber las 3 consultas de referencia.
    externos = {k: v["name"] for k, v in compose["volumes"].items() if v.get("external")}
    assert externos == {"modelos_del_origen": "hvmvp_modelos"}
    montan = {
        (nombre, m["read_only"])
        for nombre, s in compose["services"].items()
        for m in s.get("volumes", [])
        if m.get("source") == "modelos_del_origen"
    }
    assert montan == {("ollama-embeddings", True)}
    assert "depends_on" not in compose["services"]["ollama-embeddings"]  # sin `ollama`
    publicados = {
        (p["host_ip"], p["published"])
        for s in compose["services"].values()
        for p in s.get("ports", [])
    }
    assert publicados == {("127.0.0.1", "54330"), ("127.0.0.1", "8081")}  # el 80 es de Caddy
    montados = {m["target"]: m["source"] for m in compose["services"]["postgres"]["volumes"]}
    assert montados["/restauracion"] == str(tmp_path / "volcados")


def test_toma_del_entorno_de_la_pc_el_snapshot_y_el_proyecto(tmp_path: Path):
    archivo = entorno_de_la_pc(tmp_path / "pc")
    destino = tmp_path / "restauracion"

    restauracion = componer(archivo, {}, destino)

    snapshot = destino / "snapshot"
    assert restauracion.snapshot == snapshot
    # restic deja cada carpeta con su ruta completa: C:\hv\volcados va a snapshot\C\hv\volcados.
    volcados = tmp_path / "pc" / "volcados"
    unidad = volcados.drive.rstrip(":")
    assert restauracion.volcados == snapshot.joinpath(unidad, *volcados.parts[1:])
    assert restauracion.compose[-4:] == (
        "--file", str(MVP / "compose.restauracion.yml"),
        "--project-name", "historiaviva-restauracion",
    )  # fmt: skip
    assert restauracion.pg[-3:] == ("exec", "-T", "postgres")
    assert restauracion.volcados_en_pg == "/restauracion"
    assert conninfo_to_dict(restauracion.dsn) == {
        "host": "127.0.0.1", "port": "54330", "user": "historia", "password": "abc123",
        "dbname": "historia_viva",
    }  # fmt: skip


def test_se_niega_a_restaurar_dentro_del_onedrive(tmp_path: Path):
    archivo = entorno_de_la_pc(tmp_path / "pc")

    with pytest.raises(RespaldoMalConfigurado, match=r"restauración.*OneDrive"):
        componer(archivo, {}, tmp_path / "OneDrive - Exolgan S.A" / "restauracion")


@sin_herramientas_de_postgres
def test_verificar_originales_lista_faltantes_y_huerfanos(
    origen: Respaldo, base_vacia: str, capsys: pytest.CaptureFixture[str]
):
    (origen.originales / str(PRIMERA)).unlink()
    (origen.originales / "sin-pieza").write_bytes(b"suelto")
    with psycopg.connect(base_vacia, autocommit=True) as conexion:
        conexion.execute(
            "UPDATE acervo.pieza SET huella_original = NULL WHERE pieza_id = %s",
            (list(PIEZAS)[1],),
        )

    codigo = main(
        ["verificar-originales", "--bd", base_vacia, "--originales", str(origen.originales)]
    )

    salida = capsys.readouterr().out
    assert codigo == 1
    assert f"Faltante: local://originales/{PRIMERA}" in salida
    assert "Huérfano: local://originales/sin-pieza" in salida
    assert "1 faltantes, 1 huérfanos y 1 sin huella (solo se vio que existe)." in salida


@pytest.mark.skipif(sys.platform != "win32", reason="rutas de Windows")
def test_restic_restaura_cada_carpeta_con_su_ruta_completa_debajo_del_snapshot():
    snapshot = Path(r"E:\restauracion\snapshot")

    assert _en_el_snapshot(snapshot, Path(r"D:\hv\volcados")) == snapshot / "D" / "hv" / "volcados"
    # Una ruta UNC descartaría el snapshot y devolvería la del origen.
    with pytest.raises(RespaldoMalConfigurado, match=r"nas.*snapshot"):
        _en_el_snapshot(snapshot, Path(r"\\nas\hv\originales"))


@sin_herramientas_de_postgres
@pytest.mark.skipif(shutil.which("restic") is None, reason="sin restic en el PATH")
def test_restic_deja_el_snapshot_donde_lo_busca_la_restauracion(origen: Respaldo, tmp_path: Path):
    variables = {
        **os.environ,
        "RESTIC_REPOSITORY": str(tmp_path / "disco-externo"),
        "RESTIC_PASSWORD": "clave-de-prueba",
    }
    respaldo = replace(origen, correr=correr_con(variables))
    respaldo.iniciar()
    volcado = respaldo.respaldar("manual")
    snapshot = tmp_path / "restauracion" / "snapshot"

    codigo, error = correr_con(variables)(
        ["restic", "restore", "latest", "--target", str(snapshot)], None
    )

    assert codigo == 0, error
    restauracion = Restauracion(
        snapshot=snapshot,
        volcados=_en_el_snapshot(snapshot, origen.volcados),
        originales=_en_el_snapshot(snapshot, origen.originales),
        compose=(),
        pg=(),
        dsn_pg="",
        volcados_en_pg="",
        dsn="",
        correr=correr_con(variables),
    )
    assert restauracion.ultimo_volcado().base.name == volcado.base.name
    assert (restauracion.originales / str(PRIMERA)).is_file()


@sin_herramientas_de_postgres
def test_si_falla_un_paso_el_informe_anota_cual(
    origen: Respaldo, restauracion: tuple[Restauracion, Doble]
):
    origen.volcar("manual")
    proceso, doble = restauracion
    doble.falla = "pg_restore"

    with pytest.raises(RespaldoFallido, match="pg_restore"):
        proceso.restaurar(quien="Ignacio")

    [archivo] = proceso.snapshot.parent.glob("informe-*.txt")
    informe = archivo.read_text(encoding="utf-8")
    assert "Quién: Ignacio" in informe
    assert "FALLA  Paso de la restauración: «pg_restore --dbname=" in informe
    assert "Resultado: FALLA" in informe


def _app_viva(origen: Respaldo) -> Respaldo:
    """La app escribe una Pieza mientras corre `pg_dump`: la referencia de antes y la de
    después no coinciden."""
    real = origen.correr

    def correr(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        resultado = real(comando, salida)
        if comando[0] == "pg_dump":
            with psycopg.connect(origen.dsn, autocommit=True) as conexion:
                conexion.execute(
                    "INSERT INTO acervo.pieza (estado, clase_original, via_ingreso, tipo_mime,"
                    " nombre_archivo, tamano_bytes) VALUES ('Recibida', 'documento', 'archivo',"
                    " 'application/pdf', 'nueva.pdf', 1)"
                )
        return resultado

    return replace(origen, correr=correr)


def _busqueda_falla(origen: Respaldo) -> Respaldo:
    real = origen.correr

    def correr(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        if "consultas-de-referencia" in comando:
            return 1, "service \"web\" is not running"
        return real(comando, salida)

    return replace(origen, correr=correr)


@sin_herramientas_de_postgres
def test_con_la_busqueda_caida_en_el_origen_los_conteos_se_comparan_igual(
    origen: Respaldo, restauracion: tuple[Restauracion, Doble]
):
    _busqueda_falla(origen).volcar("programado")
    proceso, _ = restauracion

    resultado = proceso.restaurar(quien="Ignacio")

    estados = {c.clave: c.pasa for c in resultado.criterios}
    assert (estados["conteos"], estados["consultas"]) == (True, None)
    detalle = next(c.detalle for c in resultado.criterios if c.clave == "consultas")
    assert "la búsqueda salió con 1" in detalle


def _psql_falla(origen: Respaldo) -> Respaldo:
    real = origen.correr

    def correr(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        return (2, "psql: error de conexión") if comando[0] == "psql" else real(comando, salida)

    return replace(origen, correr=correr)


@sin_herramientas_de_postgres
@pytest.mark.parametrize(
    ("perturbar", "motivo"),
    [(_app_viva, "la base cambió durante el volcado"), (_psql_falla, "psql salió con 2")],
    ids=["app-viva", "psql-falla"],
)
def test_sin_referencia_estable_los_conteos_y_las_consultas_no_son_comparables(
    origen: Respaldo,
    restauracion: tuple[Restauracion, Doble],
    perturbar: Callable[[Respaldo], Respaldo],
    motivo: str,
):
    perturbar(origen).volcar("programado")
    proceso, _ = restauracion

    resultado = proceso.restaurar(quien="Ignacio")

    estados = {c.clave: c.pasa for c in resultado.criterios}
    assert estados == {
        "conteos": None,
        "originales": True,
        "muestra": True,
        "consultas": None,
        "roles": True,
        "tiempo": True,
    }
    assert not resultado.pasa
    detalle = next(c.detalle for c in resultado.criterios if c.clave == "conteos")
    assert motivo in detalle
    assert "repetí con un respaldo manual con la app quieta" in detalle
    assert "N/C    Conteo de Piezas" in resultado.informe
    assert "Resultado: NO COMPARABLE" in resultado.informe
