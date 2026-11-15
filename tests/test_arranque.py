"""El arranque de la pila al iniciar sesión en la PC (spec §2.2, historias 224 y 226, ticket 113).

`docker compose up -d` lo prueba una persona con la PC: acá, la tarea de Windows y la espera a
Docker, con un doble de `Correr`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree

import pytest

from historia_viva.respaldo import COMPOSE, RespaldoMalConfigurado
from historia_viva.respaldo.arranque import arrancar, comando_compose, main, programar

pytestmark = pytest.mark.historia(226)

TAREA = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def test_la_tarea_levanta_la_pila_al_iniciar_sesion_sin_ventana():
    pedidos: list[bytes] = []

    def schtasks(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        assert list(comando[:5]) == ["schtasks", "/Create", "/F", "/TN", r"Historia Viva\Arranque"]
        pedidos.append(Path(comando[6]).read_bytes())
        return 0, ""

    programar(
        schtasks, python=r"C:\hv\python.exe", entorno=Path(r"D:\hv\.env"), usuario=r"PC\alumno"
    )

    tarea = ElementTree.fromstring(pedidos[0])
    assert tarea.findtext(f"{TAREA}Triggers/{TAREA}LogonTrigger/{TAREA}UserId") == r"PC\alumno"
    assert tarea.findtext(f".//{TAREA}Exec/{TAREA}Command") == r"%SystemRoot%\System32\conhost.exe"
    assert tarea.findtext(f".//{TAREA}Exec/{TAREA}Arguments") == (
        r'--headless "C:\hv\python.exe" -m historia_viva.respaldo.arranque --entorno "D:\hv\.env"'
        " arrancar"
    )
    # En la sesión de quien opera, donde corre Docker Desktop, y sin elevar.
    assert tarea.findtext(f".//{TAREA}Principal/{TAREA}LogonType") == "InteractiveToken"
    assert tarea.findtext(f".//{TAREA}Principal/{TAREA}RunLevel") == "LeastPrivilege"
    assert tarea.findtext(f".//{TAREA}DisallowStartIfOnBatteries") == "false"


class Docker:
    """Doble de `Correr`: `docker info` falla las primeras `caido` veces."""

    def __init__(self, caido: int) -> None:
        self.caido = caido
        self.comandos: list[list[str]] = []

    def __call__(self, comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        self.comandos.append(list(comando))
        if comando[1] == "info" and self.caido:
            self.caido -= 1
            return 1, "error during connect"
        return 0, ""


def test_espera_a_que_docker_responda_y_recien_entonces_levanta_la_pila():
    docker, esperas = Docker(caido=3), []

    assert arrancar(docker, ("docker", "compose"), dormir=esperas.append) == (0, "")

    assert docker.comandos[-1] == ["docker", "compose", "up", "-d"]
    assert len(esperas) == 3


def test_sin_docker_a_tiempo_no_levanta_nada():
    docker = Docker(caido=1000)

    codigo, error = arrancar(docker, ("docker", "compose"), espera_s=20, dormir=lambda _: None)

    assert codigo == 1
    assert "Docker no respondió en 20 s" in error
    assert ["docker", "compose", "up", "-d"] not in docker.comandos


def test_levanta_con_el_env_de_la_pc_y_nunca_con_uno_del_onedrive(tmp_path: Path, capsys):
    pc = tmp_path / "pc"
    pc.mkdir()
    entorno = pc / ".env"
    entorno.write_text(f"HV_ORIGINALES={pc / 'originales'}\n", encoding="utf-8")

    assert comando_compose(entorno, {}) == (
        "docker", "compose", "--env-file", str(entorno), "--file", str(COMPOSE),
    )  # fmt: skip

    nube = tmp_path / "OneDrive"
    nube.mkdir()
    (nube / ".env").write_text(f"HV_ORIGINALES={pc / 'originales'}\n", encoding="utf-8")
    with pytest.raises(RespaldoMalConfigurado, match="dentro del OneDrive"):
        comando_compose(nube / ".env", {})
    assert main(["--entorno", str(nube / ".env"), "arrancar"]) == 2
    assert "dentro del OneDrive" in capsys.readouterr().err
