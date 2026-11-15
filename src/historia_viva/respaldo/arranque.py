"""El arranque de la pila al iniciar sesión en la PC (spec §2.2, higiene de la PC; ticket 113).

Docker Desktop inicia con Windows; esta tarea espera a que su motor responda y hace
`docker compose up -d` con el `.env` de la PC, fuera del OneDrive. Corre en la PC, como el
respaldo, y comparte con él la lectura del `.env` y la guarda del OneDrive.

    uv run python -m historia_viva.respaldo.arranque --entorno D:\\hv\\.env programar  # una vez
    uv run python -m historia_viva.respaldo.arranque --entorno D:\\hv\\.env arrancar
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

from historia_viva.respaldo import (
    COMPOSE,
    Correr,
    RespaldoMalConfigurado,
    _problema,
    correr_con,
    variables_de,
)

__all__ = ["arrancar", "comando_compose", "main", "programar", "tarea_de_arranque"]

NOMBRE_DE_LA_TAREA = r"Historia Viva\Arranque"
ESPERA_A_DOCKER_S = 600
"""Docker Desktop tarda en levantar WSL después del inicio de sesión; más que esto es una falla."""


def comando_compose(archivo_entorno: Path, variables: Mapping[str, str]) -> tuple[str, ...]:
    """`docker compose` con el `.env` de la PC.

    Raises:
        RespaldoMalConfigurado: Si el `.env` o `HV_ORIGINALES` caen dentro del OneDrive o del
            disco virtual de WSL, o falta `HV_ORIGINALES`.
    """
    todas = variables_de(archivo_entorno, variables)
    if "HV_ORIGINALES" not in todas:
        raise RespaldoMalConfigurado(f"Falta HV_ORIGINALES en {archivo_entorno}.")
    rutas = {".env": archivo_entorno.resolve(), "HV_ORIGINALES": Path(todas["HV_ORIGINALES"])}
    if mal := [f"{n} ({r}) {p}" for n, r in rutas.items() if (p := _problema(r, todas))]:
        raise RespaldoMalConfigurado(f"{'; '.join(mal)}: no se levanta la pila (DEC-144, R-33).")
    return ("docker", "compose", "--env-file", str(archivo_entorno), "--file", str(COMPOSE))


def arrancar(
    correr: Correr,
    compose: Sequence[str],
    *,
    espera_s: float = ESPERA_A_DOCKER_S,
    dormir: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Espera al motor de Docker y levanta la pila; sin motor a tiempo, no la levanta."""
    for _ in range(max(1, int(espera_s // 5))):
        if correr(["docker", "info"], None)[0] == 0:
            return correr([*compose, "up", "-d"], None)
        dormir(5)
    return 1, f"Docker no respondió en {espera_s:.0f} s: la pila no se levantó."


def tarea_de_arranque(python: str, entorno: Path, usuario: str) -> str:
    """Al iniciar sesión `usuario`, en su sesión (donde corre Docker Desktop) y sin ventana."""
    argumentos = (
        f'--headless "{python}" -m historia_viva.respaldo.arranque --entorno "{entorno}" arrancar'
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Historia Viva: docker compose up -d al iniciar sesión (ticket 113).</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <UserId>{escape(usuario)}</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(usuario)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%SystemRoot%\\System32\\conhost.exe</Command>
      <Arguments>{escape(argumentos)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def programar(correr: Correr, *, python: str, entorno: Path, usuario: str) -> tuple[int, str]:
    """Crea o reemplaza la tarea de arranque con `schtasks`."""
    with tempfile.TemporaryDirectory() as directorio:
        xml = Path(directorio) / "tarea.xml"
        xml.write_text(tarea_de_arranque(python, entorno, usuario), encoding="utf-16")
        crear = ["schtasks", "/Create", "/F", "/TN", NOMBRE_DE_LA_TAREA, "/XML", str(xml)]
        return correr(crear, None)


def main(argv: Sequence[str] | None = None) -> int:
    """Sale con el código de `docker compose` o `schtasks`, y con 2 si el `.env` no sirve."""
    parser = argparse.ArgumentParser(
        prog="historia_viva.respaldo.arranque",
        description="Arranque de Historia Viva al iniciar sesión en la PC.",
    )
    parser.add_argument("--entorno", type=Path, required=True, help="el .env de la PC")
    comandos = parser.add_subparsers(dest="comando", required=True)
    comandos.add_parser("arrancar", help="espera a Docker y hace `docker compose up -d`")
    comandos.add_parser("programar", help="crea la tarea de Windows al iniciar sesión")
    argumentos = parser.parse_args(argv)
    try:
        compose = comando_compose(argumentos.entorno, os.environ)
    except RespaldoMalConfigurado as mal:
        print(mal, file=sys.stderr)
        return 2
    correr = correr_con(variables_de(argumentos.entorno, os.environ))
    if argumentos.comando == "programar":
        usuario = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
        codigo, error = programar(
            correr, python=sys.executable, entorno=argumentos.entorno.resolve(), usuario=usuario
        )
        print(error or f"Tarea «{NOMBRE_DE_LA_TAREA}» programada.", file=sys.stderr)
        return codigo
    codigo, error = arrancar(correr, compose)
    print(error, end="", file=sys.stderr)
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
