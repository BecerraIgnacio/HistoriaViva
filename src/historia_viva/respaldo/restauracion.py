"""Restauración probada en un proyecto aparte (spec §2.3; historia 230, DEC-144).

Restaura el último snapshot de restic en `historiaviva-restauracion`: otro proyecto compose, con
sus propios volúmenes, otros puertos y sin Caddy (`compose.restauracion.yml`). Después verifica
los criterios de éxito contra lo que anotó el volcado en el origen y deja un informe.

    uv run python -m historia_viva.respaldo.restauracion restaurar --entorno D:\\hv\\.env
        --destino E:\\hv-restauracion
    docker compose exec web python -m historia_viva.respaldo.restauracion verificar-originales
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from psycopg.conninfo import make_conninfo

from historia_viva.bd import Bd
from historia_viva.dominio.tipos import OriginalGuardado
from historia_viva.fichas.postgres import ModuloFichasPostgres
from historia_viva.originales import AlmacenDeOriginales, OriginalNoDisponible
from historia_viva.respaldo import (
    COMPOSE,
    CONSULTAS_EN_WEB,
    DSN_EN_EL_CONTENEDOR,
    Correr,
    RespaldoFallido,
    RespaldoMalConfigurado,
    Volcado,
    _local,
    _problema,
    correr_con,
    leer_json,
    sql_de_referencia,
    variables_de,
)
from historia_viva.respaldo import componer as componer_respaldo

RTO = timedelta(hours=4)
MUESTRA = 20
"""Piezas al azar a las que se les verifica el hash y la apertura (spec §2.3)."""

TITULOS = {
    "conteos": "Conteo de Piezas por estado y de Fichas por versión",
    "originales": "verificar-originales",
    "muestra": f"{MUESTRA} Piezas al azar: hash del original y apertura",
    "consultas": "3 consultas de referencia",
    "roles": "hv_app sin DELETE sobre las tablas de dominio",
    "tiempo": "Tiempo total",
    "paso": "Paso de la restauración",
}
PROYECTO = "historiaviva-restauracion"
REPETIR = "repetí con un respaldo manual con la app quieta"

_TABLAS_QUE_HV_APP_BORRA = (
    "SELECT format('%I.%I', n.nspname, c.relname) AS tabla FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'acervo'"
    " AND c.relkind IN ('r', 'p') AND has_table_privilege('hv_app', c.oid, 'DELETE')"
    " ORDER BY 1"
)


class Criterio(NamedTuple):
    clave: str
    """Una de `TITULOS`."""
    pasa: bool | None
    """`None`: no comparable, porque el volcado no dejó una referencia estable."""
    detalle: str


@dataclass(frozen=True)
class Resultado:
    criterios: list[Criterio]
    informe: str

    @property
    def pasa(self) -> bool:
        return all(c.pasa is True for c in self.criterios)


@dataclass(frozen=True)
class VerificacionDeOriginales:
    faltantes: list[str]
    """Con Pieza, pero sin archivo o con otro SHA-256 que el de la base."""
    huerfanos: list[str]
    """En disco, sin Pieza."""
    sin_huella: int
    """Con Pieza sin SHA-256 en la base: de esos solo se vio que existan y abran."""


def verificar_originales(
    fichas: ModuloFichasPostgres, almacen: AlmacenDeOriginales
) -> VerificacionDeOriginales:
    """Cruza los originales de la base con los del disco, por su SHA-256."""
    registrados = fichas.originales_guardados()
    return VerificacionDeOriginales(
        faltantes=[o.identificador for o in registrados if not _intacto(almacen, o)],
        huerfanos=sorted(almacen.guardados() - {o.identificador for o in registrados}),
        sin_huella=sum(o.huella is None for o in registrados),
    )


def _intacto(almacen: AlmacenDeOriginales, original: OriginalGuardado) -> bool:
    """Lo abre como lo sirve la pantalla partida y compara su SHA-256 con la base."""
    try:
        respuesta = almacen.abrir(original)
    except OriginalNoDisponible:
        return False
    huella = hashlib.sha256()
    for bloque in respuesta.bloques:
        huella.update(bloque)
    return original.huella in (None, huella.hexdigest())


def verificar(
    bd: Bd,
    almacen: AlmacenDeOriginales,
    referencia: Mapping[str, Any],
    consultas: Mapping[str, Any],
) -> list[Criterio]:
    """Los criterios de éxito sobre la base y los originales restaurados, salvo el tiempo.
    `consultas` es lo que respondió la búsqueda de la restaurada (`CONSULTAS_EN_WEB`)."""
    # Diferido: `indice` carga llama-index, que recolectar la suite no paga (ticket 130).
    from historia_viva.indice import TABLA as TABLA_DEL_INDICE

    fichas = ModuloFichasPostgres(bd=bd)
    with bd.transaccion() as tx:
        fila = tx.execute(sql_de_referencia()).fetchone()
        assert fila is not None
        restaurada = {**fila["referencia"], "consultas": consultas}
        borra = [f["tabla"] for f in tx.execute(_TABLAS_QUE_HV_APP_BORRA).fetchall()]
    originales = verificar_originales(fichas, almacen)
    registrados = fichas.originales_guardados()
    elegidos = random.sample(registrados, min(MUESTRA, len(registrados)))
    no_abren = [o.identificador for o in elegidos if not _intacto(almacen, o)]
    de_dominio = [t for t in borra if t != TABLA_DEL_INDICE]
    huerfanos = (
        f"; {len(originales.huerfanos)} huérfanos para explicar: {', '.join(originales.huerfanos)}"
        if originales.huerfanos
        else "; 0 huérfanos"
    )
    return [
        _comparar("conteos", referencia, restaurada),
        Criterio(
            "originales",
            not originales.faltantes,
            f"{len(originales.faltantes)} faltantes{_lista(originales.faltantes)}{huerfanos};"
            f" {originales.sin_huella} sin huella",
        ),
        Criterio(
            "muestra",
            not no_abren,
            f"{len(elegidos) - len(no_abren)} de {len(elegidos)} abren con su hash"
            f"{_lista(no_abren)}",
        ),
        _comparar("consultas", referencia, restaurada),
        Criterio(
            "roles",
            not de_dominio,
            f"hv_app puede borrar en {', '.join(de_dominio)}" if de_dominio else "ninguna",
        ),
    ]


def _comparar(
    clave: str, referencia: Mapping[str, Any], restaurada: Mapping[str, Any]
) -> Criterio:
    if "error" in referencia:
        return Criterio(clave, None, f"no comparable ({referencia['error']}): {REPETIR}")
    if "error" in referencia[clave]:
        return Criterio(clave, None, f"no comparable ({referencia[clave]['error']}): {REPETIR}")
    if referencia.get("estable") is not True:
        motivo = "la base cambió durante el volcado"
        return Criterio(clave, None, f"no comparable ({motivo}): {REPETIR}")
    origen, restaurada = referencia[clave], restaurada[clave]
    if "error" in restaurada:
        return Criterio(clave, False, f"la restaurada no respondió: {restaurada['error']}")
    distintos = [
        f"{nombre}: origen {origen.get(nombre)}, restaurada {restaurada.get(nombre)}"
        for nombre in sorted(origen.keys() | restaurada.keys())
        if origen.get(nombre) != restaurada.get(nombre)
    ]
    return Criterio(clave, not distintos, "; ".join(distintos) or "idénticos al origen")


def _lista(identificadores: list[str]) -> str:
    return f": {', '.join(identificadores)}" if identificadores else ""


def _duracion(tiempo: timedelta) -> str:
    minutos = int(tiempo.total_seconds() // 60)
    return f"{minutos // 60} h {minutos % 60:02d} min"


def informe(
    criterios: list[Criterio], *, fecha: datetime, quien: str, tiempo: timedelta, volcado: str
) -> str:
    """Lo que se anota de cada restauración probada (spec §2.3)."""
    desvios = [f"- {TITULOS[c.clave]}: {c.detalle}" for c in criterios if c.pasa is not True]
    etiqueta = {True: "PASA ", False: "FALLA", None: "N/C  "}
    if any(c.pasa is False for c in criterios):
        resultado = "Resultado: FALLA; se corrige y se repite."
    elif desvios:
        resultado = f"Resultado: NO COMPARABLE; {REPETIR}."
    else:
        resultado = "Resultado: PASA"
    lineas = [
        "Restauración probada en historiaviva-restauracion (spec §2.3, DEC-144)",
        f"Fecha: {_local(fecha)}",
        f"Quién: {quien}",
        f"Volcado: {volcado}",
        f"Tiempo total: {_duracion(tiempo)} (RTO: {_duracion(RTO)})",
        "",
        *(f"{etiqueta[c.pasa]}  {TITULOS[c.clave]}: {c.detalle}" for c in criterios),
        "",
        "Desvíos:" if desvios else "Desvíos: ninguno",
        *desvios,
        resultado,
    ]
    return "\n".join(lineas) + "\n"


@dataclass(frozen=True)
class Restauracion:
    snapshot: Path
    """Donde restic deja el último snapshot, fuera del OneDrive."""
    volcados: Path
    """`HV_VOLCADOS` del origen, dentro de `snapshot`."""
    originales: Path
    """`HV_ORIGINALES` del origen, dentro de `snapshot`: el bind mount del proyecto aparte."""
    compose: tuple[str, ...]
    """`docker compose` del proyecto `historiaviva-restauracion`."""
    pg: tuple[str, ...]
    """Lo que antecede a `psql` y `pg_restore`: `docker compose exec -T postgres`."""
    dsn_pg: str
    volcados_en_pg: str
    """Donde ve `postgres` a `volcados`."""
    dsn: str
    """La base restaurada vista desde la PC, para los criterios."""
    correr: Correr
    ahora: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def restaurar(self, quien: str) -> Resultado:
        """Restaura el último snapshot como dice el runbook (globales primero, después la base,
        `migrar` y el proyecto) y verifica los criterios. El informe queda junto a `snapshot`.

        Raises:
            RespaldoFallido: Si un paso salió con error; el informe anota cuál.
        """
        inicio = self.ahora()
        volcado: Volcado | None = None
        try:
            # Cada prueba sobre un volumen nuevo, como el día que haga falta.
            self._paso(*self.compose, "down", "-v")
            self._paso("restic", "restore", "latest", "--target", str(self.snapshot))
            volcado = self.ultimo_volcado()
            self._paso(*self.compose, "up", "-d", "--wait", "postgres")
            # Sin ON_ERROR_STOP: los roles que ya creó el bootstrap dan error y se sigue.
            self._paso(*self.pg, "psql", "-X", "-q", "-f", self._en_pg(volcado.globales),
                       f"--dbname={self.dsn_pg}")  # fmt: skip
            # Sin `--clean`: el volumen es nuevo y no tiene `acervo`, y la limpieza de una vista
            # que pg_dump parte en dos (la del resumen del Lote, 084) falla sin el schema.
            self._paso(*self.pg, "pg_restore", f"--dbname={self.dsn_pg}",
                       self._en_pg(volcado.base))  # fmt: skip
            self._paso(*self.compose, "run", "--rm", "migrar")
            # Sin el trabajador: procesaría la cola y movería los conteos. `ollama-embeddings`
            # embebe las 3 consultas de referencia, con los modelos del origen.
            self._paso(*self.compose, "up", "-d", "--wait", "web", "ollama-embeddings")
        except RespaldoFallido as error:
            fallido = [Criterio("paso", False, str(error))]
            self._informar(fallido, inicio, quien, volcado)
            raise
        referencia = json.loads(volcado.referencia.read_text(encoding="utf-8"))
        consultas = leer_json(
            self.correr,
            (*self.compose, *CONSULTAS_EN_WEB),
            self.snapshot.parent / "consultas.json.parcial",
            "la búsqueda",
        )
        bd = Bd(self.dsn, tamano_pool=1)
        try:
            criterios = verificar(
                bd, AlmacenDeOriginales(self.originales), referencia, consultas
            )
        finally:
            bd.cerrar()
        return self._informar(criterios, inicio, quien, volcado)

    def _informar(
        self, criterios: list[Criterio], inicio: datetime, quien: str, volcado: Volcado | None
    ) -> Resultado:
        """Suma el tiempo y deja el informe junto a `snapshot`."""
        tiempo = self.ahora() - inicio
        a_tiempo = tiempo <= RTO
        criterios.append(
            Criterio(
                "tiempo",
                a_tiempo,
                _duracion(tiempo) if a_tiempo else f"{_duracion(tiempo)}: se redefine el RTO",
            )
        )
        nombre = volcado.base.name if volcado else "sin volcado"
        texto = informe(criterios, fecha=inicio, quien=quien, tiempo=tiempo, volcado=nombre)
        self.snapshot.parent.mkdir(parents=True, exist_ok=True)
        archivo = self.snapshot.parent / f"informe-{inicio:%Y%m%dT%H%M%SZ}.txt"
        archivo.write_text(texto, encoding="utf-8")
        return Resultado(criterios, texto)

    def ultimo_volcado(self) -> Volcado:
        """El más nuevo del snapshot: el nombre empieza por la hora."""
        bases = sorted(self.volcados.glob("*.dump"))
        if not bases:
            raise RespaldoFallido(f"El snapshot no trae ningún volcado en {self.volcados}.")
        nombre = bases[-1].name.removesuffix(".dump")
        volcado = Volcado(
            base=bases[-1],
            globales=self.volcados / f"{nombre}.globales.sql",
            referencia=self.volcados / f"{nombre}.referencia.json",
        )
        if faltan := [a.name for a in (volcado.globales, volcado.referencia) if not a.is_file()]:
            raise RespaldoFallido(
                f"Al volcado {nombre} le falta {', '.join(faltan)}: hacé un respaldo manual."
            )
        return volcado

    def _en_pg(self, archivo: Path) -> str:
        return f"{self.volcados_en_pg}/{archivo.name}"

    def _paso(self, *comando: str) -> None:
        codigo, error = self.correr(comando, None)
        if codigo:
            raise RespaldoFallido(f"«{' '.join(comando)}» salió con {codigo}: {error.strip()}")


COMPOSE_RESTAURACION = COMPOSE.with_name("compose.restauracion.yml")
PUERTO_DE_LA_BASE = 54330
"""El que publica `compose.restauracion.yml` para los criterios."""


def componer(archivo_entorno: Path, variables: Mapping[str, str], destino: Path) -> Restauracion:
    """La restauración con el `.env` de la PC: el mismo repositorio de restic y las mismas
    guardas que el respaldo, y el snapshot en `destino`.

    Raises:
        RespaldoMalConfigurado: Lo mismo que `respaldo.componer`, o si `destino` no es absoluto
            o cae dentro del OneDrive o de WSL, o si falta `POSTGRES_PASSWORD`.
    """
    respaldo = componer_respaldo(archivo_entorno, variables)
    todas = variables_de(archivo_entorno, variables)
    if problema := _problema(destino, todas):
        raise RespaldoMalConfigurado(
            f"La carpeta de la restauración ({destino}) {problema}: va con ruta absoluta, fuera"
            " del OneDrive y del disco virtual de WSL (DEC-144, R-33)."
        )
    if "POSTGRES_PASSWORD" not in todas:
        raise RespaldoMalConfigurado(f"Falta POSTGRES_PASSWORD en {archivo_entorno}.")
    snapshot = destino / "snapshot"
    volcados = _en_el_snapshot(snapshot, respaldo.volcados)
    originales = _en_el_snapshot(snapshot, respaldo.originales)
    # `COMPOSE_PROJECT_NAME` del entorno o del `.env` le gana al `name:` del yml: sin esto,
    # `up` recrearía los contenedores del origen y `down -v` borraría sus volúmenes.
    compose = (*respaldo.compose, "--file", str(COMPOSE_RESTAURACION), "--project-name", PROYECTO)
    return Restauracion(
        snapshot=snapshot,
        volcados=volcados,
        originales=originales,
        compose=compose,
        pg=(*compose, "exec", "-T", "postgres"),
        dsn_pg=DSN_EN_EL_CONTENEDOR,
        volcados_en_pg="/restauracion",
        dsn=make_conninfo(
            host="127.0.0.1",
            port=PUERTO_DE_LA_BASE,
            user="historia",
            password=todas["POSTGRES_PASSWORD"],
            dbname="historia_viva",
        ),
        correr=correr_con(
            {**todas, "HV_ORIGINALES": str(originales), "HV_VOLCADOS_RESTAURADOS": str(volcados)}
        ),
    )


def _en_el_snapshot(snapshot: Path, ruta: Path) -> Path:
    """Donde deja `restic restore` una carpeta respaldada: con su ruta completa debajo del
    destino, y la unidad sin los dos puntos (`C:\\hv` va a `snapshot\\C\\hv`).

    Raises:
        RespaldoMalConfigurado: Si la ruta no cae dentro del snapshot (una ruta UNC).
    """
    restaurada = snapshot.joinpath(ruta.drive.rstrip(":"), *ruta.parts[1:])
    if not restaurada.is_relative_to(snapshot):
        raise RespaldoMalConfigurado(
            f"{ruta} no se puede ubicar dentro del snapshot ({snapshot}): los originales y los"
            " volcados van en una unidad local."
        )
    return restaurada


def main(argv: Sequence[str] | None = None) -> int:
    """`restaurar` sale con 1 si falla un criterio o un paso; `verificar-originales`, si hay
    faltantes. Con 2, la configuración no sirve."""
    parser = argparse.ArgumentParser(
        prog="historia_viva.respaldo.restauracion",
        description="Restauración probada en un proyecto aparte (spec §2.3, DEC-144).",
    )
    comandos = parser.add_subparsers(dest="comando", required=True)
    restaurar = comandos.add_parser(
        "restaurar", help="restaura el último snapshot y verifica los criterios"
    )
    restaurar.add_argument("--entorno", type=Path, required=True, help="el .env de la PC")
    restaurar.add_argument(
        "--destino", type=Path, required=True, help="carpeta vacía, fuera del OneDrive"
    )
    restaurar.add_argument("--quien", default=getpass.getuser(), help="quien la hace")
    verificar = comandos.add_parser(
        "verificar-originales", help="faltantes y huérfanos, por SHA-256 contra la base"
    )
    verificar.add_argument("--bd", default=os.environ.get("HV_BD_URL"))
    verificar.add_argument("--originales", type=Path, default=os.environ.get("HV_ORIGINALES"))
    argumentos = parser.parse_args(argv)
    if argumentos.comando == "verificar-originales":
        if not argumentos.bd or not argumentos.originales:
            parser.error("falta --bd o --originales (o HV_BD_URL y HV_ORIGINALES)")
        return _verificar_originales(argumentos.bd, argumentos.originales)
    try:
        restauracion = componer(argumentos.entorno, os.environ, argumentos.destino.resolve())
        resultado = restauracion.restaurar(argumentos.quien)
    except RespaldoMalConfigurado as error:
        print(error, file=sys.stderr)
        return 2
    except RespaldoFallido as error:
        print(f"Falló: {error}", file=sys.stderr)
        return 1
    print(resultado.informe)
    return 0 if resultado.pasa else 1


def _verificar_originales(dsn: str, raiz: Path) -> int:
    bd = Bd(dsn, tamano_pool=1)
    try:
        verificacion = verificar_originales(ModuloFichasPostgres(bd=bd), AlmacenDeOriginales(raiz))
    finally:
        bd.cerrar()
    for faltante in verificacion.faltantes:
        print(f"Faltante: {faltante}")
    for huerfano in verificacion.huerfanos:
        print(f"Huérfano: {huerfano}")
    print(
        f"{len(verificacion.faltantes)} faltantes, {len(verificacion.huerfanos)} huérfanos y"
        f" {verificacion.sin_huella} sin huella (solo se vio que existe)."
    )
    return 1 if verificacion.faltantes else 0


if __name__ == "__main__":
    sys.exit(main())
