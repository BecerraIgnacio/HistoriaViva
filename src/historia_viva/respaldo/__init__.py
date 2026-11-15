"""Backups fuera del OneDrive (spec §2.3; historia 238, RNF-17, DEC-144, R-33).

Corre en la PC, no en un contenedor (ninguno monta el socket de Docker): vuelca la base y los
globales con `pg_dump` dentro de `postgres` y guarda los volcados y los originales con restic en
`RESTIC_REPOSITORY`, un disco externo cifrado. Cambiar de disco es cambiar esa variable.

    uv run python -m historia_viva.respaldo --entorno D:\\hv\\.env iniciar    # una vez por disco
    uv run python -m historia_viva.respaldo --entorno D:\\hv\\.env programar  # tarea de Windows
    uv run python -m historia_viva.respaldo --entorno D:\\hv\\.env respaldar --motivo manual
    uv run python -m historia_viva.respaldo --entorno D:\\hv\\.env migrar     # runbook R2
    uv run python -m historia_viva.respaldo --entorno D:\\hv\\.env estado     # sale con 1 si > 26 h
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from xml.sax.saxutils import escape

from historia_viva.fichas.postgres import SQL_CONTEOS

__all__ = [
    "Correr",
    "Estado",
    "Respaldo",
    "RespaldoFallido",
    "RespaldoMalConfigurado",
    "Volcado",
    "componer",
    "correr_con",
    "main",
    "programar",
]

SIN_SNAPSHOT_TOLERADO = timedelta(hours=26)
"""Spec §2.4. Entre dos corridas programadas pasan a lo sumo 24 h (el domingo solo corre la de
las 02:00): el resto es margen para una corrida lenta."""

VERIFICAR_CADA = timedelta(days=7)
MUESTRA_VERIFICADA = "5%"
"""`restic check --read-data-subset` (spec §2.3)."""

PODAR_CADA = timedelta(days=7)
RETENCION = ("--keep-within", "2d", "--keep-daily", "14", "--keep-weekly", "8",
             "--keep-monthly", "3")  # fmt: skip
"""`restic forget --prune` (ticket 113): todo lo de 48 h y después uno por día, semana y mes.
Corta: un dato suprimido (R14) o el corpus real (R17) no sobrevive meses en los backups."""
VOLCADOS_LOCALES = timedelta(days=7)
"""Los volcados de `HV_VOLCADOS` más viejos que esto se borran: ya están en los snapshots."""



def sql_de_referencia() -> str:
    """Los conteos que la restauración probada compara con el origen (spec §2.3). Viajan con
    cada volcado: la restauración puede ser en otra máquina."""
    return f"SELECT json_build_object('conteos', ({SQL_CONTEOS})) AS referencia"


CONSULTAS_EN_WEB = (
    "exec", "-T", "web", "python", "-m", "historia_viva.composicion", "consultas-de-referencia",
)  # fmt: skip
"""Las 3 consultas de referencia por la búsqueda de `web` (ticket 116): embeber la consulta
necesita `ollama-embeddings`, que la PC no ve. Va después de `docker compose`."""

Correr = Callable[[Sequence[str], Path | None], tuple[int, str]]
"""Corre un comando y devuelve su código y su error estándar; con una ruta, la salida va ahí."""

_SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # solo existe en Windows
"""`docker` y `restic` abren su propia consola aunque la tarea programada corra sin ventana."""


def correr_con(variables: Mapping[str, str]) -> Correr:
    """El `Correr` real, con `variables` como entorno completo de cada comando."""

    def correr(comando: Sequence[str], salida: Path | None) -> tuple[int, str]:
        try:
            with salida.open("wb") if salida else nullcontext() as destino:
                hecho = subprocess.run(
                    comando,
                    stdout=destino,
                    stderr=subprocess.PIPE,
                    env=dict(variables),
                    creationflags=_SIN_VENTANA,
                    check=False,
                )
        except OSError as error:  # sin Docker o sin restic: una corrida fallida más, anotada
            return 127, f"{comando[0]}: {error}"
        return hecho.returncode, hecho.stderr.decode(errors="replace")

    return correr


def leer_json(
    correr: Correr, comando: Sequence[str], temporal: Path, programa: str
) -> dict[str, Any]:
    """La última línea que `comando` escribe en su salida, como JSON (antes puede haber líneas
    del registro de la app); `{"error": ...}` si falla o no se lee."""
    codigo, error = correr(comando, temporal)
    try:
        if codigo:
            return {"error": f"{programa} salió con {codigo}: {error.strip()}"}
        lineas = temporal.read_text(encoding="utf-8").strip().splitlines() or [""]
        leido: dict[str, Any] = json.loads(lineas[-1])
        return leido
    except (OSError, ValueError) as falla:
        return {"error": f"{programa} no dejó un JSON legible: {falla}"}
    finally:
        temporal.unlink(missing_ok=True)


@dataclass(frozen=True)
class Volcado:
    base: Path
    """`pg_dump` en formato personalizado: la base entera, índice vectorial incluido."""
    globales: Path
    """`pg_dumpall --globals-only`: los roles, que `pg_restore` necesita antes que la base."""
    referencia: Path
    """`sql_de_referencia()` y `CONSULTAS_EN_WEB` en el origen, en JSON, con `estable` (igual antes
    y después de `pg_dump`), o `{"error": ...}` si no salió."""


@dataclass(frozen=True)
class Corrida:
    cuando: datetime
    accion: str
    """`snapshot`, `verificacion` o `poda`."""
    motivo: str
    ok: bool
    detalle: str

    def a_json(self) -> dict[str, object]:
        return {**asdict(self), "cuando": self.cuando.isoformat()}

    @classmethod
    def de_json(cls, fila: dict[str, Any]) -> Corrida:
        return cls(**{**fila, "cuando": datetime.fromisoformat(fila["cuando"])})


class Estado(NamedTuple):
    informe: str
    al_dia: bool


class RespaldoFallido(Exception):
    """Un paso del respaldo salió con error; el mensaje dice cuál."""


@dataclass(frozen=True)
class Respaldo:
    volcados: Path
    """Donde quedan los volcados y el registro, fuera del OneDrive (`HV_VOLCADOS`)."""
    originales: Path
    compose: tuple[str, ...]
    """`docker compose` con el `.env` de la PC."""
    pg: tuple[str, ...]
    """Lo que antecede a `pg_dump`: en la PC, `docker compose exec -T postgres`."""
    dsn: str
    correr: Correr
    ahora: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def iniciar(self) -> None:
        """`restic init`, una vez por repositorio (y por disco)."""
        self._restic("init")

    def respaldar(self, motivo: str) -> Volcado:
        """Vuelca y guarda un snapshot con los volcados y los originales. Los modelos de Ollama
        no entran: se vuelven a bajar."""
        try:
            volcado = self.volcar(motivo)
            self._restic("backup", "--tag", "historia-viva", "--tag", motivo,
                         str(self.volcados), str(self.originales))  # fmt: skip
        except RespaldoFallido as error:
            self._anotar("snapshot", motivo, error)
            raise
        self._anotar("snapshot", motivo)
        verificada = self._ultimo("verificacion", ok=True)
        if verificada is None or self.ahora() - verificada.cuando >= VERIFICAR_CADA:
            self.verificar()
        podada = self._ultimo("poda", ok=True)
        if podada is None or self.ahora() - podada.cuando >= PODAR_CADA:
            self.podar()
        return volcado

    def migrar(self) -> int:
        """Runbook R2: `migrar` corre recién con el snapshot del estado previo guardado.

        Raises:
            RespaldoFallido: Si no se pudo respaldar; entonces no se migra.
        """
        self.respaldar("antes-de-migrar")
        codigo, error = self.correr([*self.compose, "run", "--rm", "migrar"], None)
        print(error, end="", file=sys.stderr)
        return codigo

    def verificar(self) -> None:
        """Lee una muestra de los datos del repositorio: un snapshot ilegible se descubre acá y
        no el día que hace falta restaurar."""
        try:
            self._restic("check", f"--read-data-subset={MUESTRA_VERIFICADA}")
        except RespaldoFallido as error:
            self._anotar("verificacion", "periodica", error)
            raise
        self._anotar("verificacion", "periodica")

    def podar(self) -> None:
        """Olvida los snapshots fuera de `RETENCION` y borra los volcados locales de más de 7
        días. Corre después de un snapshot: lo que borra de `HV_VOLCADOS` ya quedó guardado."""
        try:
            self._restic("forget", "--prune", "--tag", "historia-viva", *RETENCION)
        except RespaldoFallido as error:
            self._anotar("poda", "periodica", error)
            raise
        limite = f"{self.ahora() - VOLCADOS_LOCALES:%Y%m%dT%H%M%SZ}"
        for archivo in self.volcados.iterdir():
            # Los de `volcar`: la hora UTC al principio del nombre, que se ordena como texto.
            if re.match(r"\d{8}T\d{6}Z-", archivo.name) and archivo.name[:16] < limite:
                archivo.unlink()
        self._anotar("poda", "periodica")

    def estado(self) -> Estado:
        """Lo que ve quien opera: el último snapshot, y si pasaron más de 26 h desde él. No está
        al día tampoco si la última corrida falló, aunque el último snapshot bueno sea reciente."""
        al_dia, lineas = False, ["ATENCIÓN: no hay ningún snapshot registrado."]
        if ultimo := self._ultimo("snapshot", ok=True):
            antiguedad = self.ahora() - ultimo.cuando
            al_dia = antiguedad <= SIN_SNAPSHOT_TOLERADO
            horas = int(antiguedad.total_seconds() // 3600)
            lineas = [
                f"Último snapshot: {_local(ultimo.cuando)} ({ultimo.motivo}), hace {horas} h."
            ]
            if not al_dia:
                lineas.insert(0, f"ATENCIÓN: {horas} h sin snapshot (más de 26 h).")
        if verificada := self._ultimo("verificacion", ok=True):
            lineas.append(f"Última verificación: {_local(verificada.cuando)}.")
        if corridas := self._corridas():
            c = corridas[-1]
            resultado = "bien" if c.ok else f"falló: {c.detalle}"
            lineas.append(f"Última corrida: {_local(c.cuando)} {c.accion} {c.motivo} {resultado}")
            al_dia = al_dia and c.ok
        return Estado("\n".join(lineas), al_dia)

    @property
    def registro(self) -> Path:
        """Una línea JSON por corrida, con su resultado."""
        return self.volcados / "registro.jsonl"

    def _anotar(self, accion: str, motivo: str, error: RespaldoFallido | None = None) -> None:
        corrida = Corrida(self.ahora(), accion, motivo, ok=error is None, detalle=str(error or ""))
        self.volcados.mkdir(parents=True, exist_ok=True)
        with self.registro.open("a", encoding="utf-8") as registro:
            registro.write(json.dumps(corrida.a_json(), ensure_ascii=False) + "\n")

    def _corridas(self) -> list[Corrida]:
        try:
            lineas = self.registro.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [Corrida.de_json(json.loads(linea)) for linea in lineas if linea.strip()]

    def _ultimo(self, accion: str, *, ok: bool) -> Corrida | None:
        return next(
            (c for c in reversed(self._corridas()) if c.accion == accion and c.ok == ok), None
        )

    def _restic(self, *argumentos: str) -> None:
        codigo, error = self.correr(["restic", *argumentos], None)
        if codigo:
            raise RespaldoFallido(f"restic {argumentos[0]} salió con {codigo}: {error.strip()}")

    def volcar(self, motivo: str) -> Volcado:
        """La base y los globales en `volcados`, con la hora y el motivo en el nombre."""
        self.volcados.mkdir(parents=True, exist_ok=True)
        nombre = f"{self.ahora():%Y%m%dT%H%M%SZ}-{motivo}"
        volcado = Volcado(
            base=self.volcados / f"{nombre}.dump",
            globales=self.volcados / f"{nombre}.globales.sql",
            referencia=self.volcados / f"{nombre}.referencia.json",
        )
        temporal = volcado.referencia.with_name(volcado.referencia.name + ".parcial")
        self._a_archivo(["pg_dumpall", "--globals-only"], volcado.globales)
        # La referencia no corre en la transacción de `pg_dump`: antes y después; si la app
        # escribió entre medio, no coinciden y la restauración no la compara.
        antes = self._referencia(temporal)
        self._a_archivo(["pg_dump", "--format=custom"], volcado.base)
        despues = self._referencia(temporal)
        if error := antes.get("error") or despues.get("error"):
            anotada: dict[str, Any] = {"error": error}
        else:
            a, d = antes["referencia"], despues["referencia"]
            # Con la búsqueda caída, los conteos (solo de `postgres`) se comparan igual.
            consultas = next(
                (c for c in (a["consultas"], d["consultas"]) if "error" in c), d["consultas"]
            )
            iguales = "error" in consultas or a["consultas"] == d["consultas"]
            anotada = {
                **d, "consultas": consultas, "estable": a["conteos"] == d["conteos"] and iguales
            }
        temporal.write_text(json.dumps(anotada, ensure_ascii=False), encoding="utf-8")
        temporal.replace(volcado.referencia)
        return volcado

    def _referencia(self, temporal: Path) -> dict[str, Any]:
        """Los conteos y las 3 consultas en el origen, o por qué no salieron: sin ellos se
        respalda igual. Si solo falla la búsqueda, el error queda bajo `consultas`."""
        psql = [*self.pg, "psql", "-X", "-A", "-t", "-c", sql_de_referencia(),
                f"--dbname={self.dsn}"]  # fmt: skip
        conteos = leer_json(self.correr, psql, temporal, "psql")
        if "error" in conteos:
            return conteos
        consultas = leer_json(
            self.correr, [*self.compose, *CONSULTAS_EN_WEB], temporal, "la búsqueda"
        )
        return {"referencia": {**conteos, "consultas": consultas}}

    def _a_archivo(self, comando: list[str], destino: Path) -> None:
        # A un temporal: un volcado cortado nunca queda con el nombre de uno bueno.
        temporal = destino.with_name(destino.name + ".parcial")
        codigo, error = self.correr([*self.pg, *comando, f"--dbname={self.dsn}"], temporal)
        if codigo:
            temporal.unlink(missing_ok=True)
            raise RespaldoFallido(f"{comando[0]} salió con {codigo}: {error.strip()}")
        temporal.replace(destino)


def _local(cuando: datetime) -> str:
    return f"{cuando.astimezone():%Y-%m-%d %H:%M}"


class RespaldoMalConfigurado(Exception):
    """Falta una variable o un destino cae dentro del OneDrive: no se respalda nada."""


COMPOSE = Path(__file__).resolve().parents[3] / "compose.yml"

DSN_EN_EL_CONTENEDOR = "postgresql://historia@/historia_viva"
"""El superusuario por el socket de `postgres`, sin clave: `pg_dumpall` lee los roles."""

_CONTRASENAS_DE_RESTIC = ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND")
_ONEDRIVE = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
_WSL = ("\\\\wsl$\\", "\\\\wsl.localhost\\")
"""Spec §2.2: el vhdx crece y no se achica."""


def leer_entorno(archivo: Path) -> dict[str, str]:
    """Las líneas `CLAVE=valor` del `.env`, como las lee compose; vacía es ausente. Saca solo un
    par de comillas que envuelva el valor entero: las de `"C:\\Program Files\\…" show …` en
    `RESTIC_PASSWORD_COMMAND` son de restic.

    shortcut: sin interpolación ni comillas escapadas; alcanza para claves en hexadecimal y
    rutas, cambiar si el `.env` empieza a necesitarlas."""
    variables = {}
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        clave, igual, valor = linea.partition("=")
        if igual and not clave.lstrip().startswith("#"):
            valor = valor.strip()
            if valor[:1] in ("'", '"') and valor.find(valor[0], 1) == len(valor) - 1:
                valor = valor[1:-1]
            variables[clave.strip()] = valor
    return {clave: valor for clave, valor in variables.items() if valor}


def variables_de(archivo_entorno: Path, variables: Mapping[str, str]) -> dict[str, str]:
    """Las del `.env` con las del proceso encima, como en compose."""
    return {**leer_entorno(archivo_entorno), **{k: v for k, v in variables.items() if v}}


def componer(archivo_entorno: Path, variables: Mapping[str, str]) -> Respaldo:
    """El respaldo de la PC con el `.env` de `archivo_entorno`; `variables` (las del proceso)
    mandan sobre el archivo, como en compose.

    Raises:
        RespaldoMalConfigurado: Si falta una variable, si una ruta del `.env` no es absoluta o si
            el `.env`, los volcados, los originales, el repositorio o el archivo de la contraseña
            caen dentro del OneDrive o del disco virtual de WSL.
    """
    todas = variables_de(archivo_entorno, variables)
    faltan = [v for v in ("HV_VOLCADOS", "HV_ORIGINALES", "RESTIC_REPOSITORY") if v not in todas]
    if not any(v in todas for v in _CONTRASENAS_DE_RESTIC):
        faltan.append(" o ".join(_CONTRASENAS_DE_RESTIC))
    if faltan:
        raise RespaldoMalConfigurado(f"Falta {', '.join(faltan)} en {archivo_entorno}.")
    rutas = {
        ".env": archivo_entorno.resolve(),  # el de la línea de comandos, relativo a quien opera
        "HV_VOLCADOS": Path(todas["HV_VOLCADOS"]),
        "HV_ORIGINALES": Path(todas["HV_ORIGINALES"]),
        "RESTIC_REPOSITORY": Path(todas["RESTIC_REPOSITORY"].removeprefix("local:")),
    }
    if "RESTIC_PASSWORD_FILE" in todas:
        rutas["RESTIC_PASSWORD_FILE"] = Path(todas["RESTIC_PASSWORD_FILE"])
    if mal := [f"{n} ({r}) {p}" for n, r in rutas.items() if (p := _problema(r, todas))]:
        raise RespaldoMalConfigurado(
            f"{'; '.join(mal)}. El entorno, los originales y los backups van con ruta absoluta,"
            " fuera del OneDrive y del disco virtual de WSL (DEC-144, R-33)."
        )
    compose = ("docker", "compose", "--env-file", str(archivo_entorno), "--file", str(COMPOSE))
    return Respaldo(
        volcados=rutas["HV_VOLCADOS"],
        originales=rutas["HV_ORIGINALES"],
        compose=compose,
        pg=(*compose, "exec", "-T", "postgres"),
        dsn=DSN_EN_EL_CONTENEDOR,
        correr=correr_con(todas),
    )


def _problema(ruta: Path, variables: Mapping[str, str]) -> str | None:
    if not ruta.is_absolute():
        # Compose la resolvería contra `mvp/` y la tarea programada contra `C:\Windows`.
        return "no es absoluta"
    if ruta.drive.casefold().startswith(_WSL):
        return "dentro del disco virtual de WSL"
    return "dentro del OneDrive" if _en_el_onedrive(ruta, variables) else None


def _en_el_onedrive(ruta: Path, variables: Mapping[str, str]) -> bool:
    ruta = ruta.resolve()
    raices = [Path(variables[v]).resolve() for v in _ONEDRIVE if v in variables]
    # También por nombre: la tarea programada puede correr sin las variables de la sesión.
    return any(ruta.is_relative_to(raiz) for raiz in raices) or any(
        parte.casefold().startswith("onedrive") for parte in ruta.parts
    )


NOMBRE_DE_LA_TAREA = r"Historia Viva\Respaldo"
HORAS_HABILES = ("08:00", "12:00", "16:00", "20:00")
"""Cada 4 h hábiles, de lunes a sábado (spec §2.3); además, todos los días a las 02:00."""


def tarea_programada(python: str, entorno: Path) -> str:
    """La tarea de Windows: corre en la sesión de quien opera, que es donde corre Docker, y sin
    ventana (`conhost --headless`): cerrarla a mitad del volcado cortaría el respaldo. El
    `pythonw.exe` del venv de uv no sirve: es el mismo lanzador de consola que `python.exe`.
    `conhost` no devuelve el código de salida: el resultado está en el registro y en `estado`."""
    lunes_a_sabado = "".join(
        f"<{dia}/>" for dia in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
    )
    disparos = "".join(
        f"<CalendarTrigger><StartBoundary>2026-01-01T{hora}:00</StartBoundary><ScheduleByWeek>"
        f"<WeeksInterval>1</WeeksInterval><DaysOfWeek>{lunes_a_sabado}</DaysOfWeek>"
        "</ScheduleByWeek></CalendarTrigger>"
        for hora in HORAS_HABILES
    )
    argumentos = (
        f'--headless "{python}" -m historia_viva.respaldo --entorno "{entorno}"'
        " respaldar --motivo programado"
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Historia Viva: volcado y snapshot con restic (DEC-144).</Description>
  </RegistrationInfo>
  <Triggers>
    {disparos}
    <CalendarTrigger>
      <StartBoundary>2026-01-01T02:00:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%SystemRoot%\\System32\\conhost.exe</Command>
      <Arguments>{escape(argumentos)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def programar(correr: Correr, *, python: str, entorno: Path) -> tuple[int, str]:
    """Crea o reemplaza la tarea programada con `schtasks`."""
    with tempfile.TemporaryDirectory() as directorio:
        xml = Path(directorio) / "tarea.xml"
        xml.write_text(tarea_programada(python, entorno), encoding="utf-16")
        crear = ["schtasks", "/Create", "/F", "/TN", NOMBRE_DE_LA_TAREA, "/XML", str(xml)]
        return correr(crear, None)


MOTIVOS = ("programado", "antes-de-sesion", "despues-de-sesion", "manual")
"""Los de `respaldar --motivo`; el de antes de `migrar` lo pone el comando `migrar`."""


def main(argv: Sequence[str] | None = None) -> int:
    """Los comandos del respaldo; salen con 1 si algo falló o el último snapshot es viejo y con
    2 si la configuración no sirve."""
    parser = argparse.ArgumentParser(
        prog="historia_viva.respaldo",
        description="Backups de Historia Viva con restic, fuera del OneDrive (spec §2.3).",
    )
    parser.add_argument(
        "--entorno", type=Path, required=True, help="el .env de la PC, fuera del OneDrive"
    )
    comandos = parser.add_subparsers(dest="comando", required=True)
    comandos.add_parser("iniciar", help="crea el repositorio de restic, una sola vez")
    respaldar = comandos.add_parser("respaldar", help="volcado de la base y snapshot")
    respaldar.add_argument("--motivo", choices=MOTIVOS, required=True)
    comandos.add_parser("migrar", help="respalda y recién entonces corre `migrar` (runbook R2)")
    comandos.add_parser("estado", help="el último snapshot, la última verificación y corrida")
    comandos.add_parser("programar", help="crea la tarea programada de Windows")
    argumentos = parser.parse_args(argv)
    try:
        respaldo = componer(argumentos.entorno, os.environ)
        if argumentos.comando == "estado":
            estado = respaldo.estado()
            print(estado.informe)
            return 0 if estado.al_dia else 1
        if argumentos.comando == "programar":
            entorno = argumentos.entorno.resolve()
            codigo, error = programar(respaldo.correr, python=sys.executable, entorno=entorno)
            print(error or f"Tarea «{NOMBRE_DE_LA_TAREA}» programada.", file=sys.stderr)
            return codigo
        if argumentos.comando == "iniciar":
            respaldo.iniciar()
            print("Repositorio de restic creado.")
            return 0
        if argumentos.comando == "migrar":
            return respaldo.migrar()
        volcado = respaldo.respaldar(argumentos.motivo)
        print(f"Respaldo {argumentos.motivo}: {volcado.base.name} y snapshot guardados.")
        return 0
    except RespaldoMalConfigurado as error:
        print(error, file=sys.stderr)
        return 2
    except RespaldoFallido as error:
        print(f"Falló: {error}", file=sys.stderr)
        return 1
