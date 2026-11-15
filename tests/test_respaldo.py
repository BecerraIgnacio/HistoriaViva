"""Backups automáticos fuera del OneDrive (historia 238, RNF-17, DEC-144, R-33).

El script corre en la PC, fuera de los contenedores: `pg_dump` dentro de `postgres` con
`docker compose exec` y restic contra el repositorio de `RESTIC_REPOSITORY`. Sin Docker, el
volcado se prueba con el `pg_dump` del PATH contra la base de pruebas; restic, con un doble que
anota los comandos, y de punta a punta solo si el binario está en el PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from historia_viva.migraciones import aplicar
from historia_viva.respaldo import (
    Respaldo,
    RespaldoFallido,
    RespaldoMalConfigurado,
    componer,
    correr_con,
    leer_entorno,
    main,
    programar,
)

pytestmark = pytest.mark.rnf("RNF-17")

sin_pg_dump = pytest.mark.skipif(
    any(shutil.which(h) is None for h in ("pg_dump", "pg_restore", "psql")),
    reason="sin pg_dump, pg_restore ni psql en el PATH (los de pgserver: pginstall/bin)",
)

FRAGMENTO = """
INSERT INTO acervo.data_fragmento (node_id, text, metadata_, embedding)
VALUES ('fragmento-respaldado', 'texto', %s, array_fill(0.5::real, ARRAY[1024])::vector)
"""
METADATOS = (
    '{"pieza_id": "p", "ficha_id": "f", "tipo": "t", "tipo_actividad": "a", "carreras": [],'
    ' "fecha_inicio": null, "fecha_fin": null, "modelo_embedding": "m", "via_ingreso": "archivo"}'
)


@sin_pg_dump
def test_el_volcado_lleva_el_indice_vectorial_y_los_globales(
    base_vacia: str, url_superusuario: str, tmp_path: Path
):
    aplicar(base_vacia)
    with psycopg.connect(base_vacia, autocommit=True) as conexion:
        conexion.execute(FRAGMENTO, (METADATOS,))
    dsn = make_conninfo(url_superusuario, dbname=conninfo_to_dict(base_vacia)["dbname"])
    respaldo = Respaldo(
        volcados=tmp_path / "volcados",
        originales=tmp_path / "originales",
        compose=(),
        pg=(),
        dsn=dsn,
        correr=correr_con(os.environ),
    )

    volcado = respaldo.volcar("manual")

    restaurado = subprocess.run(
        ["pg_restore", "--file=-", "--table=data_fragmento", str(volcado.base)],
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert "fragmento-respaldado" in restaurado
    assert "CREATE ROLE hv_app" in volcado.globales.read_text(encoding="utf-8")


class Consola:
    """Doble de `Correr`: anota cada comando, escribe unos conteos vacíos en la salida y falla en
    el que contenga `falla`."""

    def __init__(self, falla: str | None = None) -> None:
        self.comandos: list[list[str]] = []
        self.falla = falla

    def __call__(self, comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        self.comandos.append(list(comando))
        if salida:
            salida.write_bytes(b'{"conteos": {}}')
        if self.falla and self.falla in " ".join(comando):
            return 1, "Fatal: unable to open repository"
        return 0, ""

    def de(self, programa: str) -> list[list[str]]:
        return [c for c in self.comandos if programa in c]


def respaldo_con(consola: Consola, tmp_path: Path, **cambios: Any) -> Respaldo:
    return Respaldo(
        volcados=tmp_path / "volcados",
        originales=tmp_path / "originales",
        compose=("docker", "compose"),
        pg=("docker", "compose", "exec", "-T", "postgres"),
        dsn="postgresql://historia@/historia_viva",
        correr=consola,
        **cambios,
    )


@pytest.mark.historia(238)
def test_un_respaldo_vuelca_y_guarda_volcados_y_originales_en_restic(tmp_path: Path):
    consola = Consola()

    respaldo_con(consola, tmp_path).respaldar("programado")

    programas = [c[5] if c[0] == "docker" else c[:2] for c in consola.comandos]
    # La referencia de la restauración (041), antes y después de `pg_dump`: los conteos con
    # `psql` y las 3 consultas con la búsqueda de `web` (116).
    assert programas[:7] == [
        "pg_dumpall", "psql", "python", "pg_dump", "psql", "python", ["restic", "backup"],
    ]  # fmt: skip
    [consultas, _] = consola.de("consultas-de-referencia")
    assert consultas[2:] == ["exec", "-T", "web", "python", "-m", "historia_viva.composicion",
                             "consultas-de-referencia"]  # fmt: skip
    [backup] = consola.de("backup")
    assert backup[2:] == [
        "--tag", "historia-viva", "--tag", "programado",
        str(tmp_path / "volcados"), str(tmp_path / "originales"),
    ]  # fmt: skip


@pytest.mark.historia(230)
def test_sin_referencia_se_respalda_igual(tmp_path: Path):
    consola = Consola(falla="psql")

    volcado = respaldo_con(consola, tmp_path).respaldar("antes-de-migrar")

    assert consola.de("backup")  # bloquearía `migrar` y los snapshots automáticos
    referencia = json.loads(volcado.referencia.read_text(encoding="utf-8"))
    assert referencia == {"error": "psql salió con 1: Fatal: unable to open repository"}


@pytest.mark.historia(230)
def test_sin_la_busqueda_los_conteos_quedan_y_el_error_va_en_las_consultas(tmp_path: Path):
    consola = Consola(falla="consultas-de-referencia")  # `web` u `ollama-embeddings` caídos

    volcado = respaldo_con(consola, tmp_path).respaldar("programado")

    referencia = json.loads(volcado.referencia.read_text(encoding="utf-8"))
    assert referencia == {
        "conteos": {},
        "consultas": {"error": "la búsqueda salió con 1: Fatal: unable to open repository"},
        "estable": True,
    }


class Reloj:
    def __init__(self) -> None:
        self.hora = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.hora


@pytest.mark.historia(238)
def test_mas_de_26_h_sin_snapshot_se_ve_en_el_estado(tmp_path: Path):
    reloj = Reloj()
    respaldo = respaldo_con(Consola(), tmp_path, ahora=reloj)
    assert respaldo.estado().al_dia is False

    respaldo.respaldar("programado")
    reloj.hora += timedelta(hours=26)
    assert respaldo.estado().al_dia is True

    reloj.hora += timedelta(hours=1)
    estado = respaldo.estado()
    assert estado.al_dia is False
    assert "27 h sin snapshot" in estado.informe


@pytest.mark.historia(238)
def test_una_muestra_del_repositorio_se_verifica_cada_semana(tmp_path: Path):
    reloj, consola = Reloj(), Consola()
    respaldo = respaldo_con(consola, tmp_path, ahora=reloj)

    for _ in range(8):  # un snapshot por día: verifica el primero y el octavo
        respaldo.respaldar("programado")
        reloj.hora += timedelta(days=1)

    assert consola.de("check") == 2 * [["restic", "check", "--read-data-subset=5%"]]
    assert "Última verificación: 2026-10-19" in respaldo.estado().informe


@pytest.mark.historia(238)
def test_un_respaldo_fallido_queda_en_el_registro_y_en_el_estado(tmp_path: Path):
    reloj = Reloj()
    respaldo_con(Consola(), tmp_path, ahora=reloj).respaldar("programado")
    reloj.hora += timedelta(hours=4)
    respaldo = respaldo_con(Consola(falla="backup"), tmp_path, ahora=reloj)

    with pytest.raises(RespaldoFallido, match="restic backup salió con 1"):
        respaldo.respaldar("antes-de-migrar")

    estado = respaldo.estado()
    assert estado.al_dia is False  # aunque el último bueno tenga 4 h: `estado` sale con 1
    assert "hace 4 h" in estado.informe  # el último bueno sigue siendo el de antes
    assert "antes-de-migrar falló: restic backup salió con 1: Fatal: unable" in estado.informe


def entorno_de_la_pc(directorio: Path, **cambios: str) -> Path:
    """Un `.env` como el de la PC, con todo fuera del OneDrive salvo lo que cambie el test."""
    valores = {
        "HV_VOLCADOS": str(directorio / "volcados"),
        "HV_ORIGINALES": str(directorio / "originales"),
        "RESTIC_REPOSITORY": str(directorio / "disco-externo" / "restic"),
        "RESTIC_PASSWORD": "clave-de-prueba",
        "POSTGRES_PASSWORD": "abc123",
        **cambios,
    }
    directorio.mkdir(parents=True, exist_ok=True)
    archivo = directorio / ".env"
    archivo.write_text(
        "# comentario\n" + "".join(f"{k}={v}\n" for k, v in valores.items()), encoding="utf-8"
    )
    return archivo


@pytest.mark.historia(238)
def test_toma_los_destinos_del_entorno_de_la_pc(tmp_path: Path):
    archivo = entorno_de_la_pc(tmp_path / "pc")

    respaldo = componer(archivo, {})

    assert respaldo.volcados == tmp_path / "pc" / "volcados"
    assert respaldo.originales == tmp_path / "pc" / "originales"
    assert respaldo.pg[:4] == ("docker", "compose", "--env-file", str(archivo))
    assert respaldo.pg[-3:] == ("exec", "-T", "postgres")


@pytest.mark.historia(238)
@pytest.mark.parametrize(
    "variable", ["HV_VOLCADOS", "HV_ORIGINALES", "RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE"]
)
def test_se_niega_a_usar_un_destino_dentro_del_onedrive(tmp_path: Path, variable: str):
    nube = tmp_path / "nube"  # la carpeta que Windows publica en %OneDrive%
    archivo = entorno_de_la_pc(tmp_path / "pc", **{variable: str(nube / "hv")})

    with pytest.raises(RespaldoMalConfigurado, match=f"{variable}.*OneDrive"):
        componer(archivo, {"OneDrive": str(nube)})


@pytest.mark.historia(238)
def test_se_niega_si_el_entorno_vive_en_el_onedrive(tmp_path: Path):
    archivo = entorno_de_la_pc(tmp_path / "OneDrive - Exolgan S.A" / "mvp")

    with pytest.raises(RespaldoMalConfigurado, match=r"\.env.*OneDrive"):
        componer(archivo, {})


@pytest.mark.historia(238)
@pytest.mark.parametrize(
    "variable", ["HV_VOLCADOS", "HV_ORIGINALES", "RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE"]
)
@pytest.mark.parametrize(
    ("ruta", "problema"),
    [
        ("volcados", "no es absoluta"),  # la tarea programada corre desde C:\Windows
        (r"\\wsl$\Ubuntu\home\hv", "WSL"),
        (r"\\wsl.localhost\Ubuntu\home\hv", "WSL"),
    ],
)
def test_se_niega_a_usar_una_ruta_relativa_o_dentro_de_wsl(
    tmp_path: Path, variable: str, ruta: str, problema: str
):
    archivo = entorno_de_la_pc(tmp_path / "pc", **{variable: ruta})

    with pytest.raises(RespaldoMalConfigurado, match=rf"{variable}.*{problema}"):
        componer(archivo, {})


@pytest.mark.historia(238)
def test_el_entorno_saca_solo_las_comillas_que_envuelven_el_valor(tmp_path: Path):
    comando = r'"C:\Program Files\KeePassXC\keepassxc-cli.exe" show -s base.kdbx restic'
    archivo = tmp_path / ".env"
    archivo.write_text(
        f"RESTIC_PASSWORD_COMMAND={comando}\n"
        'HV_VOLCADOS="D:\\hv\\volcados"\n'
        "HV_ORIGINALES='\"a\" b'\n"
        'HV_CLAVE="a" b "c"\n',
        encoding="utf-8",
    )

    assert leer_entorno(archivo) == {
        "RESTIC_PASSWORD_COMMAND": comando,
        "HV_VOLCADOS": r"D:\hv\volcados",
        "HV_ORIGINALES": '"a" b',
        "HV_CLAVE": '"a" b "c"',
    }


@pytest.mark.historia(238)
def test_sin_contrasena_de_restic_no_respalda(tmp_path: Path):
    archivo = entorno_de_la_pc(tmp_path / "pc", RESTIC_PASSWORD="")

    with pytest.raises(RespaldoMalConfigurado, match="RESTIC_PASSWORD"):
        componer(archivo, {})


@pytest.mark.historia(238)
def test_migrar_respalda_antes_y_no_migra_sin_respaldo(tmp_path: Path):
    consola = Consola()
    respaldo_con(consola, tmp_path).migrar()

    assert consola.comandos[-1] == ["docker", "compose", "run", "--rm", "migrar"]
    assert ["--tag", "antes-de-migrar"] == consola.de("backup")[0][4:6]

    consola = Consola(falla="backup")
    with pytest.raises(RespaldoFallido):
        respaldo_con(consola, tmp_path).migrar()
    assert consola.de("migrar") == []


TAREA = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
LUNES_A_SABADO = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


@pytest.mark.historia(238)
def test_la_tarea_programada_respalda_cada_4_h_habiles_y_a_las_2(tmp_path: Path):
    pedidos: list[tuple[list[str], bytes]] = []

    def schtasks(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        pedidos.append((list(comando), Path(comando[comando.index("/XML") + 1]).read_bytes()))
        return 0, ""

    programar(schtasks, python=r"C:\hv\python.exe", entorno=Path(r"D:\hv\.env"))

    [(comando, xml)] = pedidos
    assert comando[:2] == ["schtasks", "/Create"]
    tarea = ElementTree.fromstring(xml)
    disparos = {
        (
            disparo.findtext(f"{TAREA}StartBoundary", "")[11:16],
            tuple(d.tag.removeprefix(TAREA) for d in disparo.iterfind(f".//{TAREA}DaysOfWeek/*")),
        )
        for disparo in tarea.iterfind(f"{TAREA}Triggers/{TAREA}CalendarTrigger")
    }
    assert disparos == {
        ("08:00", LUNES_A_SABADO),
        ("12:00", LUNES_A_SABADO),
        ("16:00", LUNES_A_SABADO),
        ("20:00", LUNES_A_SABADO),
        ("02:00", ()),  # todos los días
    }
    # Sin ventana: cerrarla a mitad del volcado cortaría el respaldo.
    assert tarea.findtext(f".//{TAREA}Exec/{TAREA}Command") == (
        r"%SystemRoot%\System32\conhost.exe"
    )
    assert tarea.findtext(f".//{TAREA}Exec/{TAREA}Arguments") == (
        r'--headless "C:\hv\python.exe" -m historia_viva.respaldo --entorno "D:\hv\.env"'
        " respaldar --motivo programado"
    )
    # Si la PC estaba apagada a la hora, corre al volver.
    assert tarea.findtext(f".//{TAREA}StartWhenAvailable") == "true"
    # En la sesión de quien opera, donde corre Docker Desktop, y sin elevar.
    principal = tarea.find(f"{TAREA}Principals/{TAREA}Principal[@id='Author']")
    assert principal is not None
    assert principal.findtext(f"{TAREA}LogonType") == "InteractiveToken"
    assert principal.findtext(f"{TAREA}RunLevel") == "LeastPrivilege"


@pytest.mark.historia(238)
def test_el_operador_ve_el_estado_y_el_rechazo_por_comando(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    archivo = entorno_de_la_pc(tmp_path / "pc")
    hace_30_h = datetime.now(UTC) - timedelta(hours=30)
    respaldo_con(Consola(), tmp_path / "pc", ahora=lambda: hace_30_h).respaldar("programado")

    assert main(["--entorno", str(archivo), "estado"]) == 1
    assert "ATENCIÓN: 30 h sin snapshot" in capsys.readouterr().out

    en_la_nube = entorno_de_la_pc(tmp_path / "OneDrive" / "pc")
    assert main(["--entorno", str(en_la_nube), "respaldar", "--motivo", "manual"]) == 2
    assert "dentro del OneDrive" in capsys.readouterr().err


@sin_pg_dump
@pytest.mark.skipif(shutil.which("restic") is None, reason="sin restic en el PATH")
@pytest.mark.historia(238)
def test_de_punta_a_punta_contra_un_repositorio_restic_local(
    base_vacia: str, url_superusuario: str, tmp_path: Path
):
    """El mismo script que con el disco externo cifrado: solo cambia `RESTIC_REPOSITORY`."""
    aplicar(base_vacia)
    originales = tmp_path / "originales"
    (originales / "ab").mkdir(parents=True)
    (originales / "ab" / "original-de-prueba").write_bytes(b"original")
    variables = {
        **os.environ,
        "RESTIC_REPOSITORY": str(tmp_path / "disco-externo"),
        "RESTIC_PASSWORD": "clave-de-prueba",
    }
    respaldo = Respaldo(
        volcados=tmp_path / "volcados",
        originales=originales,
        compose=(),
        pg=(),
        dsn=make_conninfo(url_superusuario, dbname=conninfo_to_dict(base_vacia)["dbname"]),
        correr=correr_con(variables),
    )

    respaldo.iniciar()
    volcado = respaldo.respaldar("manual")

    listado = subprocess.run(
        ["restic", "ls", "latest"], env=variables, capture_output=True, check=True, text=True
    ).stdout
    assert volcado.base.name in listado
    assert volcado.globales.name in listado
    assert "original-de-prueba" in listado
    estado = respaldo.estado()
    assert estado.al_dia
    assert "Última verificación" in estado.informe


@pytest.mark.historia(238)
def test_docker_y_restic_corren_sin_abrir_ventana(monkeypatch: pytest.MonkeyPatch):
    pedidos: list[dict[str, Any]] = []

    def run(comando: Sequence[str], **opciones: Any) -> subprocess.CompletedProcess[bytes]:
        pedidos.append(opciones)
        return subprocess.CompletedProcess(comando, 0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    correr_con({})(["restic", "snapshots"], None)

    sin_ventana = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # solo existe en Windows
    assert [p["creationflags"] for p in pedidos] == [sin_ventana]


@pytest.mark.historia(238)
def test_sin_docker_la_corrida_falla_y_queda_anotada(tmp_path: Path):
    respaldo = Respaldo(
        volcados=tmp_path / "volcados",
        originales=tmp_path / "originales",
        compose=(),
        pg=("docker-que-no-esta",),
        dsn="postgresql://historia@/historia_viva",
        correr=correr_con(os.environ),
    )

    with pytest.raises(RespaldoFallido, match="docker-que-no-esta"):
        respaldo.respaldar("programado")
    assert "programado falló: pg_dumpall salió con 127" in respaldo.estado().informe
    assert list((tmp_path / "volcados").glob("*.parcial")) == []


@pytest.mark.historia(238)
def test_cada_semana_poda_el_repositorio_y_los_volcados_de_mas_de_7_dias(tmp_path: Path):
    reloj, consola = Reloj(), Consola()
    respaldo = respaldo_con(consola, tmp_path, ahora=reloj)

    for _ in range(8):  # uno cada 25 h: poda el primero y el octavo, 7 días y 7 h después
        respaldo.respaldar("programado")
        reloj.hora += timedelta(days=1, hours=1)

    assert consola.de("forget") == 2 * [[
        "restic", "forget", "--prune", "--tag", "historia-viva", "--keep-within", "2d",
        "--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "3",
    ]]  # fmt: skip
    # La poda del 19-oct borró el volcado del 12-oct, que ya está en los snapshots.
    dias = {archivo.name[:8] for archivo in (tmp_path / "volcados").glob("*.dump")}
    assert dias == {f"202610{dia}" for dia in range(13, 20)}
    assert (tmp_path / "volcados" / "registro.jsonl").exists()


@pytest.mark.historia(238)
def test_una_poda_fallida_queda_en_el_estado_y_no_borra_volcados(tmp_path: Path):
    reloj = Reloj()
    respaldo = respaldo_con(Consola(falla="forget"), tmp_path, ahora=reloj)

    with pytest.raises(RespaldoFallido, match="restic forget salió con 1"):
        respaldo.respaldar("programado")

    assert list((tmp_path / "volcados").glob("*.dump"))
    estado = respaldo.estado()
    assert estado.al_dia is False
    assert "poda periodica falló" in estado.informe
