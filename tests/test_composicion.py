"""La composición y sus comandos, el compose `historiaviva-mvp` y la base de la suite (historias
49, 50, 56, 64, 209 y 211, RNF-02, DEC-119).

El compose se prueba leyendo los archivos de despliegue: la suite no levanta contenedores. Los
comandos arman su grafo con `componer_*` contra Postgres real.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from aplicacion import CABECERAS, ORIGEN, entrar
from base_de_pruebas import CLAVE_APP
from historia_viva.api.app import crear_app
from historia_viva.bd import BaseNoDisponible
from historia_viva.composicion import (
    Entorno,
    EntornoInvalido,
    _trabajar,
    componer_planificador,
    componer_trabajador,
    componer_web,
    main,
    motores,
)
from historia_viva.dominio.errores import ConfiguracionInvalida
from historia_viva.procesamiento.trabajador import _ESPERA_AL_DETENER_S
from ollama_falso import OllamaFalso
from validacion import ADMINISTRADOR_TECNICO, MARIA, PDF

MVP = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((MVP / "compose.yml").read_text(encoding="utf-8"))
SERVICIOS = COMPOSE["services"]
PRUEBAS = yaml.safe_load((MVP / "compose.pruebas.yml").read_text(encoding="utf-8"))


@pytest.mark.historia(209)
def test_compose_define_los_servicios_del_mvp():
    assert COMPOSE["name"] == "historiaviva-mvp"
    assert set(SERVICIOS) == {
        "postgres",
        "migrar",
        "web",
        "trabajador-procesamiento",
        "trabajador-indexacion",
        "trabajador-efemerides",
        "ollama",
        "ollama-embeddings",
        "modelos",
        "caddy",
        # Con profile: `up` no lo arranca (runbook R8).
        "banco",
    }


@pytest.mark.historia(224, 225)
def test_el_ollama_de_embeddings_es_otro_residente_y_solo_lee_los_modelos():
    generacion, embeddings = SERVICIOS["ollama"], SERVICIOS["ollama-embeddings"]

    # DEC-06: la búsqueda no espera a que el de generación descargue la visión.
    assert embeddings["image"] == generacion["image"]
    assert embeddings["environment"]["OLLAMA_KEEP_ALIVE"] == "-1"
    # El runbook R10 la baja a 1 desde el `.env`.
    assert generacion["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "${OLLAMA_MAX_LOADED_MODELS:-2}"
    # Lee los modelos que baja `ollama` (`modelos`) y no los escribe ni los poda al arrancar.
    assert embeddings["volumes"] == ["hvmvp_modelos:/root/.ollama:ro"]
    assert embeddings["environment"]["OLLAMA_NOPRUNE"] == "1"
    assert embeddings["depends_on"]["ollama"] == {"condition": "service_healthy"}
    for servicio in ("web", "trabajador-procesamiento", "trabajador-indexacion"):
        assert (
            SERVICIOS[servicio]["environment"]["HV_MOTOR_EMBEDDINGS_URL"]
            == "http://ollama-embeddings:11434"
        )


@pytest.mark.rnf("RNF-02")
def test_los_dos_ollama_corren_sin_los_modelos_de_la_nube():
    # S-MOD-05: `validar` ya rechaza `nombre:cloud`; el motor tampoco los ofrece.
    for servicio in ("ollama", "ollama-embeddings"):
        assert SERVICIOS[servicio]["environment"]["OLLAMA_NO_CLOUD"] == "1"



@pytest.mark.historia(209)
def test_la_base_vive_en_una_red_interna_y_la_alcanza_quien_la_usa():
    # 167 (matriz del laboratorio): `postgres` no sale a Internet ni publica puertos.
    assert COMPOSE["networks"] == {"base": {"internal": True}}
    assert SERVICIOS["postgres"]["networks"] == ["base"]
    usan_la_base = {
        nombre
        for nombre, servicio in SERVICIOS.items()
        if "@postgres:5432/" in json.dumps(servicio.get("environment", {}))
    }
    assert usan_la_base == {
        "web", "migrar", "trabajador-procesamiento", "trabajador-indexacion",
        "trabajador-efemerides", "modelos", "banco",
    }
    for nombre in usan_la_base:
        assert "base" in SERVICIOS[nombre]["networks"], nombre
    # `migrar` solo habla con la base; los demás también con Ollama, Caddy o Internet.
    assert SERVICIOS["migrar"]["networks"] == ["base"]
    # Previsto: `ollama` baja modelos con `modelos` y sigue con salida, igual que el de embeddings.
    for nombre in ("ollama", "ollama-embeddings", "caddy"):
        assert "networks" not in SERVICIOS[nombre], nombre
    # La restaurada publica la base para los criterios: una red `internal` no publica puertos.
    # Como texto: `!reset` no es YAML que entienda `safe_load`.
    restauracion = (MVP / "compose.restauracion.yml").read_text(encoding="utf-8")
    assert re.search(r"\n  postgres:\n(    .*\n)*?    networks: \[base, default\]\n", restauracion)


@pytest.mark.historia(224)
def test_el_mapa_de_motores_tiene_el_ollama_de_generacion_y_el_de_embeddings():
    por_defecto = motores(Entorno.desde_variables({}))
    variables = {
        "HV_MOTOR_URL": "http://127.0.0.1:11434",
        "HV_MOTOR_EMBEDDINGS_URL": "http://127.0.0.1:11435",
    }
    propios = motores(Entorno.desde_variables(variables))

    assert {id_: m.url for id_, m in por_defecto.items()} == {
        "ollama": "http://ollama:11434",
        "ollama_embeddings": "http://ollama-embeddings:11434",
    }
    assert {id_: m.url for id_, m in propios.items()} == {
        "ollama": "http://127.0.0.1:11434",
        "ollama_embeddings": "http://127.0.0.1:11435",
    }


@pytest.mark.historia(50)
@pytest.mark.parametrize(
    ("servicio", "tipo"),
    [
        ("trabajador-procesamiento", "procesamiento"),
        ("trabajador-indexacion", "indexacion"),
        ("trabajador-efemerides", "efemeride"),  # el planificador (058, R-08)
    ],
)
def test_docker_compose_up_levanta_un_trabajador_por_tipo_aparte_de_web(servicio, tipo):
    trabajador = SERVICIOS[servicio]

    assert "profiles" not in trabajador
    assert trabajador["command"] == ["trabajador", "--tipos", tipo]
    assert trabajador["depends_on"]["migrar"] == {"condition": "service_completed_successfully"}


@pytest.mark.historia(50)
@pytest.mark.parametrize(
    "servicio", ["trabajador-procesamiento", "trabajador-indexacion", "trabajador-efemerides"]
)
def test_docker_stop_le_da_al_trabajador_tiempo_de_terminar_el_trabajo_en_curso(servicio):
    # Sin esto Docker manda SIGKILL a los 10 s y el trabajo queda `en_curso` hasta que vence
    # su arriendo.
    gracia = SERVICIOS[servicio]["stop_grace_period"]

    assert re.fullmatch(r"\d+s", gracia), gracia
    assert int(gracia.removesuffix("s")) > _ESPERA_AL_DETENER_S


@pytest.mark.rf("RF-32")
def test_el_servicio_modelos_lee_la_configuracion_vigente_y_no_nombra_modelos():
    modelos = SERVICIOS["modelos"]

    assert modelos["image"] == SERVICIOS["web"]["image"]
    assert modelos["command"] == ["modelos"]
    # Lee `configuracion_capacidad` como `hv_app`, con el esquema ya migrado.
    assert urlsplit(modelos["environment"]["HV_BD_URL"]).username == "hv_app"
    assert modelos["depends_on"]["migrar"] == {"condition": "service_completed_successfully"}
    assert modelos["depends_on"]["ollama"] == {"condition": "service_healthy"}
    assert modelos["restart"] == "no"
    assert not [v for v in modelos["environment"] if v.startswith("HV_MODELO")]


@pytest.mark.historia(49)
def test_web_arranca_despues_de_migrar_y_sin_ser_duena_de_la_base():
    migrar, web = SERVICIOS["migrar"], SERVICIOS["web"]

    assert migrar["restart"] == "no"
    # `migrar` corre como `hv_migrador` y nada más: no conoce la clave del superusuario. Con
    # `HV_ENTORNO=desarrollo` en `.env` siembra las personas de prueba (016).
    assert migrar["command"] == ["migrar"]
    assert migrar["environment"] == {
        "HV_BD_URL_MIGRADOR": migrar["environment"]["HV_BD_URL_MIGRADOR"],
        "HV_ENTORNO": "${HV_ENTORNO:-}",
        # En el servidor, con `HV_ENTORNO` no arranca (121, runbook R16).
        "HV_DESPLIEGUE": "${HV_DESPLIEGUE:-}",
    }
    assert migrar["environment"]["HV_BD_URL_MIGRADOR"].startswith("postgresql://hv_migrador:")
    assert web["depends_on"]["migrar"] == {"condition": "service_completed_successfully"}
    # Arrancar la aplicación nunca cambia el esquema: conecta como `hv_app`, no como dueño.
    assert web["environment"]["HV_BD_URL"].startswith("postgresql://hv_app:")


@pytest.mark.historia(257)
def test_la_aplicacion_corre_con_la_hora_de_argentina():
    # `date.today()` arma la ventana de efemérides: en UTC correría un día de 21 a 24 (058).
    for servicio in (
        "web", "trabajador-procesamiento", "trabajador-indexacion", "trabajador-efemerides"
    ):
        assert SERVICIOS[servicio]["environment"]["TZ"] == "America/Argentina/Buenos_Aires"
    assert "tzdata" in (MVP / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.historia(49)
def test_web_y_el_trabajador_conectan_como_hv_app_y_migrar_como_hv_migrador():
    roles = {
        servicio: urlsplit(SERVICIOS[servicio]["environment"][variable]).username
        for servicio, variable in [
            ("web", "HV_BD_URL"),
            ("trabajador-procesamiento", "HV_BD_URL"),
            ("trabajador-indexacion", "HV_BD_URL"),
            ("trabajador-efemerides", "HV_BD_URL"),
            ("migrar", "HV_BD_URL_MIGRADOR"),
        ]
    }

    assert roles == {
        "web": "hv_app",
        "trabajador-procesamiento": "hv_app",
        "trabajador-indexacion": "hv_app",
        "trabajador-efemerides": "hv_app",
        "migrar": "hv_migrador",
    }


@pytest.mark.historia(49)
def test_el_bootstrap_lo_corre_postgres_una_sola_vez_sobre_un_volumen_nuevo():
    postgres = SERVICIOS["postgres"]
    montajes = dict(volumen.removesuffix(":ro").split(":") for volumen in postgres["volumes"])
    script = montajes["./db/bootstrap.sh"]
    guion = (MVP / "db" / "bootstrap.sh").read_text(encoding="utf-8")

    # La imagen corre `docker-entrypoint-initdb.d` solo al inicializar un volumen vacío.
    assert script.startswith("/docker-entrypoint-initdb.d/")
    assert montajes["./db/bootstrap.sql"] in guion
    assert {"HV_CLAVE_MIGRADOR", "HV_CLAVE_APP"} <= set(postgres["environment"])
    assert all(f"${clave}" in guion for clave in ("HV_CLAVE_MIGRADOR", "HV_CLAVE_APP"))


@pytest.mark.historia(209)
def test_postgres_con_pgvector_en_imagen_fijada():
    assert re.fullmatch(
        r"pgvector/pgvector:[\w.-]*pg17[\w.-]*@sha256:[0-9a-f]{64}", SERVICIOS["postgres"]["image"]
    )


@pytest.mark.historia(209)
def test_toda_imagen_del_registro_fijada_por_digest():
    # SEG-04 (165): un tag se mueve; el digest es la imagen que se probó. La de la app se construye.
    fijada = r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}"
    # Por línea y no con `yaml`: los overrides llevan `!reset`, que `safe_load` no lee.
    imagenes = {
        f"{archivo.name}:{n}": imagen
        for archivo in MVP.glob("compose*.yml")
        for n, imagen in enumerate(
            re.findall(r"^\s*image:\s*(\S+)", archivo.read_text(encoding="utf-8"), re.M)
        )
        if imagen != SERVICIOS["web"]["image"]
    }
    # Toda variable `IMAGEN_*`: el `ARG` del Dockerfile, el valor por defecto del compose y el del
    # `.env.example`, que `generar-env-servidor.sh` copia y le gana al del compose (vacía, no).
    dockerfile = (MVP / "Dockerfile").read_text(encoding="utf-8")
    compose = (MVP / "compose.yml").read_text(encoding="utf-8")
    ejemplo = (MVP / ".env.example").read_text(encoding="utf-8")
    for donde, patron, texto in (
        ("Dockerfile", r"^ARG (IMAGEN_\w*)=(.*)$", dockerfile),
        ("compose.yml", r"\$\{(IMAGEN_\w*):-([^}]*)\}", compose),
        (".env.example", r"^(IMAGEN_\w*)=(.+)$", ejemplo),
    ):
        imagenes |= {f"{donde}:{v}": valor for v, valor in re.findall(patron, texto, re.M)}

    assert {"Dockerfile:IMAGEN_PYTHON", "compose.yml:IMAGEN_PYTHON"} <= imagenes.keys()
    assert re.search(r"^IMAGEN_PYTHON=$", ejemplo, re.M)
    assert {SERVICIOS[s]["image"] for s in ("postgres", "ollama", "caddy")} <= {*imagenes.values()}
    assert {d: i for d, i in imagenes.items() if not re.fullmatch(fijada, i)} == {}
    assert re.search(r'"uv==[\d.]+"', dockerfile)  # SEG-03: sin rango


@pytest.mark.historia(209)
def test_seg_ningun_servicio_gana_privilegios_y_la_app_no_tiene_capabilities():
    # 165: también `compose.pruebas.yml` y los overrides que suman servicios; los demás solo
    # cambian los de `compose.yml`.
    humo = yaml.safe_load((MVP / "compose.humo.yml").read_text(encoding="utf-8"))["services"]
    pruebas = {f"pruebas:{n}": s for n, s in PRUEBAS["services"].items()}
    servicios = {**SERVICIOS, **pruebas, **{n: s for n, s in humo.items() if n not in SERVICIOS}}
    de_la_app = {n for n, s in servicios.items() if s["image"] == SERVICIOS["web"]["image"]}

    assert {n for n, s in servicios.items()
            if "no-new-privileges:true" not in s.get("security_opt", [])} == set()
    # `humo` corre como root a propósito (lee la raíz de `tls internal`): conserva las suyas.
    assert {n for n in de_la_app if servicios[n].get("cap_drop") != ["ALL"]} == {"humo"}
    assert {"web", "migrar", "banco", "trabajador-procesamiento"} <= de_la_app
    assert all("cap_drop" not in SERVICIOS[n] for n in ("postgres", "ollama", "caddy"))


@pytest.mark.historia(209)
def test_caddy_pasa_range_y_text_event_stream_sin_buffer():
    caddyfile = (MVP / "Caddyfile").read_text(encoding="utf-8")
    directivas = {linea.split()[0] for linea in caddyfile.splitlines() if linea.strip()}

    assert re.search(r"reverse_proxy web:8000 \{\s*flush_interval -1\s*\}", caddyfile)
    # Comprimir o bufferizar cambiaría los bytes de un 206 y retendría el streaming.
    assert {"encode", "request_buffers", "response_buffers"}.isdisjoint(directivas)


@pytest.mark.historia(228)
def test_caddy_atiende_un_solo_origen():
    caddyfile = (MVP / "Caddyfile").read_text(encoding="utf-8")
    sitios = [linea for linea in caddyfile.splitlines() if re.match(r"[^\s#].*\{$", linea)]

    assert sitios == ["http://localhost, http://127.0.0.1 {"]
    # `HV_ORIGEN` admite uno: entrando por `127.0.0.1`, toda mutación daría 403.
    assert re.search(r"@(\w+) host 127\.0\.0\.1\s+redir @\1 http://localhost\{uri\}", caddyfile)


SESION_PRESENCIAL = MVP / "compose.sesion-presencial.yml"
IP_LAN = "192.168.43.10"  # la que el hotspot le da a la PC


def _compose_config(tmp_path: Path, *overrides: Path, **variables: str) -> dict:
    """`docker compose config` resuelto, con el `.env` mínimo del compose base."""
    archivos = [
        arg for archivo in (MVP / "compose.yml", *overrides) for arg in ("-f", str(archivo))
    ]
    minimas = {
        "POSTGRES_PASSWORD": "a1",
        "HV_CLAVE_MIGRADOR": "b2",
        "HV_CLAVE_APP": "c3",
        "HV_ORIGINALES": str(tmp_path),
    }
    entorno = {k: v for k, v in os.environ.items() if k != "HV_IP_LAN"}
    salida = subprocess.run(
        ["docker", "compose", *archivos, "config", "--format", "json"],
        env={**entorno, **minimas, **variables},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(salida)


def _publicados(config: dict) -> dict[str, set[tuple[str, str, int]]]:
    return {
        nombre: {(p.get("host_ip", ""), p["published"], p["target"]) for p in s["ports"]}
        for nombre, s in config["services"].items()
        if s.get("ports")
    }


_SIN_DOCKER = pytest.mark.skipif(
    shutil.which("docker") is None, reason="sin Docker no hay `compose config`"
)


@pytest.mark.historia(228)
@_SIN_DOCKER
def test_web_recibe_del_env_el_modo_de_ingreso_y_el_origen(tmp_path):
    config = _compose_config(
        tmp_path, HV_AUTH="dev", HV_ENTORNO="desarrollo", HV_ORIGEN="http://localhost"
    )
    entorno = config["services"]["web"]["environment"]

    assert {v: entorno.get(v) for v in ("HV_AUTH", "HV_ENTORNO", "HV_ORIGEN", "HV_PROXY")} == {
        "HV_AUTH": "dev",
        "HV_ENTORNO": "desarrollo",
        "HV_ORIGEN": "http://localhost",
        # Lo que llega del socket de Caddy cuenta como hecho en la PC (`/auth/dev`).
        "HV_PROXY": "caddy",
    }
    assert entorno["HV_BD_URL"].startswith("postgresql://hv_app:")


def _montajes_de_la_referencia(servicio: dict) -> dict[str, tuple[str, Path | str, bool]]:
    return {
        v["target"]: (v["type"], Path(v["source"]) if v["type"] == "bind" else v["source"],
                      v.get("read_only", False))
        for v in servicio["volumes"]
        if v["target"].startswith("/referencia")
    }


@pytest.mark.historia(169)
@pytest.mark.rf("RF-28")
@_SIN_DOCKER
def test_web_lee_la_referencia_del_banco_y_banco_escribe_solo_en_corridas(tmp_path):
    referencia = tmp_path / "referencia"
    config = _compose_config(
        tmp_path, COMPOSE_PROFILES="banco", HV_REFERENCIA=str(referencia)
    )
    web, banco = config["services"]["web"], config["services"]["banco"]

    # La nota «Banco: ganador …» de la pantalla de modelos (110) sale de `/referencia/corridas`.
    assert Entorno.desde_variables(web["environment"]).referencia == Path("/referencia")
    assert _montajes_de_la_referencia(web) == {"/referencia": ("bind", referencia, True)}
    assert banco["environment"]["HV_REFERENCIA"] == "/referencia"
    assert _montajes_de_la_referencia(banco) == {
        "/referencia": ("bind", referencia, True),
        "/referencia/corridas": ("bind", referencia / "corridas", False),
    }
    # Fuera del `up` normal: `dc run --rm banco comparar …` (runbook R8). No publica nada.
    assert banco["profiles"] == ["banco"]
    assert banco["restart"] == "no"
    assert banco["entrypoint"] == ["python", "-m", "historia_viva.composicion", "banco"]
    assert _publicados(config) == {"caddy": {("127.0.0.1", "80", 80)}}
    assert "hvmvp_sin_referencia" not in config["volumes"]


@pytest.mark.historia(169)
@_SIN_DOCKER
def test_sin_la_referencia_en_el_env_web_arranca_sin_nota_y_banco_dice_que_falta(tmp_path):
    config = _compose_config(tmp_path, COMPOSE_PROFILES="banco", HV_REFERENCIA="")
    web, banco = config["services"]["web"], config["services"]["banco"]

    # Vacía es ausente: sin directorio de corridas no hay nota, y `banco` sale con «Falta …».
    assert Entorno.desde_variables(web["environment"]).referencia is None
    assert banco["environment"]["HV_REFERENCIA"] == ""
    vacio = ("volume", "hvmvp_sin_referencia", True)
    assert _montajes_de_la_referencia(web) == {"/referencia": vacio}
    # De escritura: en uno vacío de solo lectura runc no crea `corridas` y no se llega al «Falta».
    escribible = (*vacio[:2], False)
    assert _montajes_de_la_referencia(banco) == {
        "/referencia": escribible, "/referencia/corridas": escribible,
    }


def _volumenes(compose: dict, proyecto: str) -> set[str]:
    """Nombres reales en Docker: sin `name:`, compose les antepone el nombre del proyecto."""
    return {
        (definicion or {}).get("name") or f"{proyecto}_{clave}"
        for clave, definicion in (compose.get("volumes") or {}).items()
    }


@pytest.mark.historia(56)
def test_base_y_volumenes_no_se_comparten_con_el_prototipo():
    # El Prototipo salió del repositorio, pero su base puede seguir en el Docker local: 55432 es
    # el puerto que rechaza la guarda de `conftest.py`.
    puertos_del_prototipo = {"55432"}
    for compose in (COMPOSE, PRUEBAS):
        del_mvp = _volumenes(compose, compose["name"])
        assert all(nombre.startswith("hvmvp_") for nombre in del_mvp), del_mvp
    # La base del MVP y la de desarrollo y pruebas viven cada una en su volumen.
    assert SERVICIOS["postgres"]["volumes"][0] == "hvmvp_pgdata:/var/lib/postgresql/data"
    postgres_de_pruebas = PRUEBAS["services"]["postgres"]
    assert postgres_de_pruebas["volumes"] == ["hvmvp_pgdata_pruebas:/var/lib/postgresql/data"]
    # Solo en loopback y en un puerto que no es el de la base del Prototipo.
    [puerto] = postgres_de_pruebas["ports"]
    assert puerto.startswith("127.0.0.1:")
    assert puerto.split(":")[1] not in puertos_del_prototipo


def _pytest(nodo: str, **entorno: str) -> subprocess.CompletedProcess[str]:
    sin_base = {clave: valor for clave, valor in os.environ.items() if clave != "HV_BD_URL_PRUEBAS"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", nodo],
        cwd=MVP,
        env={**sin_base, **entorno},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.historia(56)
def test_la_suite_se_niega_a_vaciar_la_base_del_prototipo():
    resultado = _pytest(
        "tests/test_salud.py",
        # Host inalcanzable: si la guarda se rompe, el pytest anidado no toca la base real del Prototipo.
        HV_BD_URL_PRUEBAS="postgresql://x:x@invalid.invalid:55432/x",
    )

    assert resultado.returncode == 2, resultado.stdout
    assert "base del Prototipo" in resultado.stdout


@pytest.mark.historia(211)
def test_los_tests_sin_base_corren_sin_postgres():
    resultado = _pytest("tests/test_composicion.py::test_postgres_con_pgvector_en_imagen_fijada")

    assert resultado.returncode == pytest.ExitCode.OK, resultado.stdout


@pytest.mark.historia(211)
def test_recolectar_la_suite_no_carga_llama_index(modulos_al_recolectar):
    # Cada proceso de xdist recolecta toda la suite: llama-index tarda decenas de segundos en
    # cargar y solo lo paga quien corre un test de `indice` (ticket 130).
    assert "historia_viva.api.app" in modulos_al_recolectar
    assert "historia_viva.procesamiento.trabajador" in modulos_al_recolectar
    assert sorted(m for m in modulos_al_recolectar if m.split(".")[0] == "llama_index") == []


@pytest.mark.rnf("RNF-02")
def test_compose_sin_puertos_publicados():
    publicados = {nombre: s["ports"] for nombre, s in SERVICIOS.items() if "ports" in s}

    assert publicados == {"caddy": ["127.0.0.1:80:80"]}
    assert all("network_mode" not in s for s in SERVICIOS.values())


@pytest.mark.historia(226)
@_SIN_DOCKER
def test_la_sesion_presencial_publica_solo_el_8000_en_la_ip_de_la_lan(tmp_path):
    config = _compose_config(tmp_path, SESION_PRESENCIAL, HV_IP_LAN=IP_LAN)

    assert _publicados(config) == {
        "caddy": {("127.0.0.1", "80", 80), (IP_LAN, "8000", 8000)},
    }
    # El 8000 lo atiende el Caddyfile de la sesión, que suma su sitio al de siempre.
    caddy = config["services"]["caddy"]
    # Un reinicio de la PC no vuelve a abrir el 8000 (riesgo 49).
    assert caddy["restart"] == "no"
    montajes = {
        Path(v["source"]).name: v["target"] for v in caddy["volumes"] if v["type"] == "bind"
    }
    assert montajes == {
        "Caddyfile": "/etc/caddy/Caddyfile",
        "Caddyfile.sesion-presencial": "/etc/caddy/Caddyfile.sesion-presencial",
    }
    assert caddy["command"][caddy["command"].index("--config") + 1] == (
        "/etc/caddy/Caddyfile.sesion-presencial"
    )


@pytest.mark.historia(226)
def test_el_sitio_de_la_sesion_presencial_enruta_solo_v1():
    """Lo que el test con Caddy real prueba, para cuando no hay Caddy a mano."""
    caddyfile = (MVP / "Caddyfile.sesion-presencial").read_text(encoding="utf-8")
    activo = "\n".join(linea.split("#")[0] for linea in caddyfile.splitlines())

    assert re.match(r"\s*import /etc/caddy/Caddyfile\n", activo)
    assert re.findall(r"^[^\s{}][^\n]*\{$", activo, re.MULTILINE) == ["http://:8000 {"]


@pytest.mark.historia(226)
@_SIN_DOCKER
def test_sin_la_ip_de_la_lan_la_sesion_presencial_no_levanta(tmp_path):
    # Vacía, Docker publicaría el 8000 en todas las interfaces.
    with pytest.raises(subprocess.CalledProcessError) as error:
        _compose_config(tmp_path, SESION_PRESENCIAL)

    assert "HV_IP_LAN" in error.value.stderr


@pytest.mark.rnf("RNF-02")
@_SIN_DOCKER
def test_sin_el_override_nada_escucha_fuera_de_127_0_0_1(tmp_path):
    # Lo que levanta `docker compose up` a secas: un `compose.override.yml` se sumaría solo.
    assert not (MVP / "compose.override.yml").exists()
    config = _compose_config(tmp_path, HV_IP_LAN=IP_LAN)

    assert _publicados(config) == {"caddy": {("127.0.0.1", "80", 80)}}


CADDY = os.environ.get("HV_CADDY") or shutil.which("caddy")


def _puerto_libre() -> int:
    with socket.socket() as libre:
        libre.bind(("127.0.0.1", 0))
        return int(libre.getsockname()[1])


@contextmanager
def caddy_de_la_sesion_en(tmp_path: Path, upstream: str, ip: str = "127.0.0.1") -> Iterator[int]:
    """Caddy real con `Caddyfile.sesion-presencial` delante de `upstream`, y su puerto en `ip`.
    Lo único cambiado: `web:8000` por `upstream`, el 8000 por uno libre y el sitio de siempre
    en loopback (en Docker, la IP de la LAN la pone el `ports:` del override)."""
    lan, local = _puerto_libre(), _puerto_libre()
    base = (MVP / "Caddyfile").read_text(encoding="utf-8").replace("web:8000", upstream)
    globales = f"{{\n\tadmin off\n\tdefault_bind 127.0.0.1\n\thttp_port {local}\n"
    base = base.replace("{\n", globales, 1)
    (tmp_path / "Caddyfile").write_text(base, encoding="utf-8")
    sesion = (MVP / "Caddyfile.sesion-presencial").read_text(encoding="utf-8")
    sesion = sesion.replace("/etc/caddy/Caddyfile", (tmp_path / "Caddyfile").as_posix())
    sesion = sesion.replace("web:8000", upstream)
    sesion = sesion.replace("http://:8000 {", f"http://:{lan} {{\n\tbind {ip}")
    configuracion = tmp_path / "Caddyfile.sesion-presencial"
    configuracion.write_text(sesion, encoding="utf-8")
    assert CADDY
    caddy = subprocess.Popen(
        [CADDY, "run", "--adapter", "caddyfile", "--config", configuracion],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        limite = time.monotonic() + 15
        while True:
            assert caddy.poll() is None, caddy.communicate()[1]
            with suppress(OSError), socket.create_connection((ip, lan), timeout=1):
                break
            assert time.monotonic() < limite
            time.sleep(0.1)
        yield lan
    finally:
        caddy.kill()
        caddy.wait()


@pytest.fixture
def caddy_de_la_sesion(tmp_path) -> Iterator[tuple[int, list[str]]]:
    """La sesión de Caddy delante de un `web` falso que anota lo que le llega."""
    recibidos: list[str] = []

    class Web(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            recibidos.append(self.path)
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    web = ThreadingHTTPServer(("127.0.0.1", 0), Web)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    try:
        with caddy_de_la_sesion_en(tmp_path, f"127.0.0.1:{web.server_port}") as lan:
            yield lan, recibidos
    finally:
        web.shutdown()
        web.server_close()


def _get(puerto: int, ruta: str) -> http.client.HTTPResponse:
    """Sin cliente que normalice: la ruta viaja tal cual (`..`, `%2e`)."""
    conexion = http.client.HTTPConnection("127.0.0.1", puerto, timeout=5)
    conexion.request("GET", ruta, headers={"Host": f"{IP_LAN}:8000"})
    respuesta = conexion.getresponse()
    respuesta.read()
    conexion.close()
    return respuesta


@pytest.mark.historia(226)
@pytest.mark.skipif(CADDY is None, reason="sin Caddy (`HV_CADDY` o en el PATH)")
def test_desde_la_lan_caddy_enruta_solo_v1(caddy_de_la_sesion):
    puerto, recibidos = caddy_de_la_sesion

    for ruta in ("/v1/fragmentos?q=convenio", "/v1/piezas/pza_x", "/v1"):
        respuesta = _get(puerto, ruta)
        assert respuesta.status == 200, ruta
        assert respuesta.getheader("Server") is None
    fuera = [
        "/",
        "/validar.html",
        "/estaticos/validar.js",
        "/api/yo",
        "/api/salud",
        "/auth/dev",
        "/auth/google",
        "/v1x",
        "/v1/../api/yo",
        "/v1/%2e%2e/api/yo",
        "//api/yo",
    ]
    assert {ruta: _get(puerto, ruta).status for ruta in fuera} == dict.fromkeys(fuera, 404)
    # El matcher compara la ruta limpia, pero Caddy reenvía la cruda: lo que importa es dónde
    # cae en `web` una vez resuelta.
    for ruta in ("/api/../v1/x", "/V1/x", "/estaticos/../v1"):
        _get(puerto, ruta)
    resueltas = [posixpath.normpath(unquote(urlsplit(ruta).path)) for ruta in recibidos]
    assert all(ruta == "/v1" or ruta.startswith("/v1/") for ruta in resueltas), recibidos


@pytest.mark.historia(49, 62)
def test_el_entorno_trae_dsn_por_rol_rutas_de_datos_autenticacion_y_url_publica():
    entorno = Entorno.desde_variables(
        {
            "HV_BD_URL": "postgresql://hv_app:x@postgres/historia_viva",
            "HV_BD_URL_MIGRADOR": "postgresql://hv_migrador:y@postgres/historia_viva",
            "HV_ORIGINALES": "/data/originales",
            "HV_AUTH": "dev",
            "HV_ENTORNO": "desarrollo",
            "HV_URL_PUBLICA": "https://historiaviva.fie.undef.edu.ar",
        }
    )

    assert entorno.dsn_app == "postgresql://hv_app:x@postgres/historia_viva"
    assert entorno.dsn_migrador == "postgresql://hv_migrador:y@postgres/historia_viva"
    assert entorno.originales == Path("/data/originales")
    assert (entorno.auth, entorno.entorno) == ("dev", "desarrollo")
    assert entorno.url_publica == "https://historiaviva.fie.undef.edu.ar"


@pytest.mark.historia(62)
def test_sin_variables_el_entorno_es_el_de_produccion_en_la_pc():
    entorno = Entorno.desde_variables({})

    # Spec «Variables de entorno»: sin `HV_ENTORNO` es producción y se entra con OIDC.
    assert (entorno.auth, entorno.entorno) == ("oidc", None)
    assert entorno.url_publica == "http://localhost"
    assert entorno.motor_url == "http://ollama:11434"
    assert (entorno.dsn_app, entorno.dsn_migrador, entorno.originales) == (None, None, None)


@pytest.mark.historia(228)
def test_el_env_de_ejemplo_arranca_en_oidc():
    """`cp .env.example .env` no deja a `web` abortando: el ejemplo es el del servidor."""
    lineas = (MVP / ".env.example").read_text(encoding="utf-8").splitlines()
    variables = dict(
        linea.split("=", 1) for linea in lineas if linea and not linea.startswith("#")
    )

    assert Entorno.desde_variables(variables).auth == "oidc"


@pytest.mark.historia(62)
def test_un_modo_de_ingreso_desconocido_no_arranca():
    with pytest.raises(EntornoInvalido, match="HV_AUTH"):
        Entorno.desde_variables({"HV_AUTH": "google"})


@pytest.mark.rf("RF-18")
@pytest.mark.historia(125)
def test_la_aprobacion_en_bloque_arranca_deshabilitada_y_otro_valor_no_arranca():
    # R-28: hasta que la SEU acepte C29 el bloque está apagado, también con el `.env` de ejemplo.
    assert Entorno.desde_variables({}).bloque_habilitado is False
    assert Entorno.desde_variables({"HV_BLOQUE_HABILITADO": "true"}).bloque_habilitado is True
    with pytest.raises(EntornoInvalido, match="HV_BLOQUE_HABILITADO"):
        Entorno.desde_variables({"HV_BLOQUE_HABILITADO": "si"})


@pytest.mark.rf("RF-18")
@pytest.mark.historia(125)
def test_hv_bloque_habilitado_llega_a_validacion(url_app, tmp_path):
    variables = {
        "HV_BD_URL": url_app,
        "HV_ORIGINALES": str(tmp_path),
        "HV_MOTOR_URL": MOTOR_CAIDO,
        "HV_MOTOR_EMBEDDINGS_URL": MOTOR_CAIDO,
    }
    grafos = [
        componer_web(Entorno.desde_variables(variables | {"HV_BLOQUE_HABILITADO": valor}))
        for valor in ("", "true")
    ]
    for grafo in grafos:
        grafo.bd.cerrar()

    assert [grafo.validacion.bloque_habilitado for grafo in grafos] == [False, True]


MOTOR_CAIDO = "http://127.0.0.1:1"


def _entorno(url_app: str, originales: Path, motor_url: str = MOTOR_CAIDO) -> Entorno:
    return Entorno.desde_variables(
        {
            "HV_BD_URL": url_app,
            "HV_ORIGINALES": str(originales),
            "HV_MOTOR_URL": motor_url,
            "HV_MOTOR_EMBEDDINGS_URL": MOTOR_CAIDO,
            "HV_ORIGEN": ORIGEN,
        }
    )


@pytest.mark.rnf("RNF-01")
@pytest.mark.confirma("la sesión del Administrador técnico la confirma la app")
def test_con_el_motor_caido_web_arranca_y_la_salud_dice_degradado(url_app, tmp_path):
    with TestClient(crear_app(componer_web(_entorno(url_app, tmp_path)))) as cliente:
        salud = entrar(cliente, ADMINISTRADOR_TECNICO).get("/api/salud").json()

    assert salud["estado"] == "degradado"
    assert salud["capa_modelos"] == "sin_respuesta"
    # `web` espera un trabajador de cada tipo de `TIPOS_DE_TRABAJO` y el planificador de
    # efemerides (059); sin latido, caído (035).
    assert salud["trabajadores"] == {
        tipo: {"vivo": False, "latido_el": None}
        for tipo in ("efemeride", "indexacion", "procesamiento")
    }


@pytest.mark.historia(64)
@pytest.mark.confirma("`componer_web` arma su propio pool, como en producción")
def test_dos_grafos_en_el_mismo_proceso_son_independientes(url_app, tmp_path):
    uno = componer_web(_entorno(url_app, tmp_path / "uno"))
    otro = componer_web(_entorno(url_app, tmp_path / "otro"))

    with TestClient(crear_app(uno)):
        pass  # apagar `uno` cierra su base
    with TestClient(crear_app(otro), headers=CABECERAS) as cliente:
        entrar(cliente, MARIA)
        subida = cliente.post(
            "/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}
        )

    assert subida.status_code == 201
    pieza_id = subida.json()["pieza_id"]
    assert (tmp_path / "otro" / pieza_id).is_file()
    assert not (tmp_path / "uno" / pieza_id).exists()


# Si un módulo armara un `Bd` o un `MotorOllama` al importarse, el arranque lo levanta.
_IMPORTAR_TODO = """
import importlib, pkgutil
import historia_viva
from historia_viva.bd import Bd
from historia_viva.capa_modelos.ollama import MotorOllama

def armado_al_importar(*args, **kwargs):
    raise AssertionError("se armó un objeto al importar")

Bd.__init__ = MotorOllama.__init__ = armado_al_importar
for modulo in pkgutil.walk_packages(historia_viva.__path__, "historia_viva."):
    if not modulo.name.endswith(".__main__"):  # un `__main__` corre su comando, a propósito
        importlib.import_module(modulo.name)
"""


@pytest.mark.historia(64)
def test_ningun_modulo_arma_objetos_al_importarse():
    sin_variables = {k: v for k, v in os.environ.items() if not k.startswith("HV_")}

    resultado = subprocess.run(
        [sys.executable, "-c", _IMPORTAR_TODO], env=sin_variables, capture_output=True, text=True,
        check=False,
    )

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.historia(50)
@pytest.mark.confirma("`componer_web` arma su propio pool, como en producción")
def test_web_no_procesa_y_el_trabajador_arranca_con_el_motor_caido(url_app, tmp_path):
    entorno = _entorno(url_app, tmp_path)
    with TestClient(crear_app(componer_web(entorno)), headers=CABECERAS) as cliente:
        entrar(cliente, MARIA)
        pieza_id = cliente.post(
            "/api/piezas", files={"archivo": ("acta.pdf", PDF, "application/pdf")}
        ).json()["pieza_id"]
        # Un proceso aparte: reiniciar `web` no corta un trabajo (historia 50).
        trabajador, bd = componer_trabajador(entorno, ["procesamiento"])
        try:
            # Arranca, pero con el motor caído el cortacircuito no lo deja tomar (DEC-86).
            assert trabajador.procesar_siguiente() is False
        finally:
            bd.cerrar()
        detalle = cliente.get(f"/api/piezas/{pieza_id}").json()

    assert (detalle["estado"], detalle["error_procesamiento"]) == ("Recibida", None)


@pytest.mark.historia(50)
def test_al_apagarse_el_trabajador_cierra_su_base(url_app, tmp_path):
    trabajador, bd = componer_trabajador(_entorno(url_app, tmp_path), ["procesamiento"])

    async def apagar_en_marcha() -> None:
        tarea = asyncio.create_task(_trabajar(trabajador, bd))
        await asyncio.sleep(0.2)
        tarea.cancel()  # lo que hace `asyncio.run` con Ctrl+C
        with suppress(asyncio.CancelledError):
            await tarea

    try:
        asyncio.run(apagar_en_marcha())
        with pytest.raises(BaseNoDisponible):
            bd.ping()
    finally:
        bd.cerrar()


@pytest.mark.historia(50)
def test_trabajador_acepta_solo_tipos_conocidos(capsys):
    with pytest.raises(SystemExit) as salida:
        main(["trabajador", "--tipos", "relato"])

    assert salida.value.code == 2
    assert "'relato'" in capsys.readouterr().err


@pytest.mark.historia(257)
@pytest.mark.confirma("el pedido se marca confirmado: el planificador lo lee desde su pool")
def test_el_planificador_arranca_con_el_motor_caido_y_deja_el_pedido_marcado(
    url_app, tmp_path, _conexion_del_dueno
):
    _conexion_del_dueno.execute(
        "UPDATE acervo.programacion_efemerides SET pedido_manual_el = now(),"
        " pedido_manual_por = %s",
        (ADMINISTRADOR_TECNICO,),
    )
    planificador, bd = componer_planificador(_entorno(url_app, tmp_path))
    try:
        # El cortacircuito del motor de `redactar_efemeride` no lo deja correr (DEC-86).
        assert planificador.ejecutar_una_vez() == 0
    finally:
        bd.cerrar()

    [(marcado,)] = _conexion_del_dueno.execute(
        "SELECT pedido_manual_el FROM acervo.programacion_efemerides"
    ).fetchall()
    assert marcado is not None


@pytest.mark.historia(50, 257)
@pytest.mark.parametrize(
    ("tipos", "arrancados"),
    [
        (["efemeride"], ["planificador"]),
        # El planificador también se activa junto a la cola (spec l.468 y l.5277).
        (["procesamiento", "efemeride"], ["trabajador", "planificador"]),
    ],
)
def test_trabajador_con_efemeride_arranca_el_planificador(
    tipos, arrancados, url_app, monkeypatch, tmp_path
):
    llegaron: list[str] = []

    def doble(nombre: str):
        async def arrancar(proceso, bd, parar):
            assert isinstance(parar, asyncio.Event)
            llegaron.append(nombre)
            bd.cerrar()

        return arrancar

    monkeypatch.setattr("historia_viva.composicion._trabajar", doble("trabajador"))
    monkeypatch.setattr("historia_viva.composicion._planificar", doble("planificador"))
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_ORIGINALES", str(tmp_path))
    monkeypatch.setenv("HV_MOTOR_URL", MOTOR_CAIDO)

    assert main(["trabajador", "--tipos", *tipos]) == 0
    assert llegaron == arrancados


@pytest.mark.historia(49)
@pytest.mark.parametrize("comando", [["web"], ["trabajador", "--tipos", "procesamiento"]])
def test_web_y_el_trabajador_sin_su_dsn_no_arrancan(comando, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("HV_BD_URL", raising=False)
    monkeypatch.setenv("HV_ORIGINALES", str(tmp_path))

    assert main(comando) == 2
    assert "HV_BD_URL" in capsys.readouterr().err


@pytest.mark.historia(49)
def test_migrar_conecta_como_hv_migrador_aunque_el_entorno_traiga_hv_app(
    base_vacia, monkeypatch
):
    # `hv_app` no puede crear el esquema: si `migrar` usara `HV_BD_URL`, fallaría.
    monkeypatch.setenv("HV_BD_URL", make_conninfo(base_vacia, user="hv_app", password=CLAVE_APP))
    monkeypatch.setenv("HV_BD_URL_MIGRADOR", base_vacia)

    assert main(["migrar"]) == 0


@pytest.mark.historia(49)
def test_web_conecta_como_hv_app(url_app, url_bd, tmp_path):
    entorno = Entorno.desde_variables(
        {"HV_BD_URL": url_app, "HV_BD_URL_MIGRADOR": url_bd, "HV_ORIGINALES": str(tmp_path)}
    )
    grafo = componer_web(entorno)

    try:
        with grafo.bd.transaccion() as tx:
            fila = tx.execute("SELECT current_user AS rol").fetchone()
    finally:
        grafo.bd.cerrar()

    assert fila == {"rol": "hv_app"}


@pytest.mark.historia(50)
def test_el_comando_trabajador_arranca_con_el_motor_caido(url_app, tmp_path):
    salida = tmp_path / "salida.txt"
    variables = {
        "HV_BD_URL": url_app,
        "HV_ORIGINALES": str(tmp_path),
        "HV_MOTOR_URL": MOTOR_CAIDO,
        "HV_MOTOR_EMBEDDINGS_URL": MOTOR_CAIDO,
    }
    comando = ["trabajador", "--tipos", "procesamiento"]
    # A un archivo y no a un pipe: leerlo no bloquea si el proceso no escribe.
    with salida.open("w") as archivo:
        proceso = subprocess.Popen(
            [sys.executable, "-m", "historia_viva.composicion", *comando],
            env={**os.environ, **variables},
            stdout=archivo,
            stderr=subprocess.STDOUT,
        )
        try:
            limite = time.monotonic() + 90  # bajo carga, importar tarda 30-40 s
            while "en marcha" not in salida.read_text() and proceso.poll() is None:
                assert time.monotonic() < limite, salida.read_text()
                time.sleep(0.1)
            sigue_corriendo = proceso.poll() is None
        finally:
            proceso.kill()
            proceso.wait()

    assert sigue_corriendo, salida.read_text()


@pytest.fixture
def ollama_en_red() -> Iterator[tuple[OllamaFalso, str]]:
    """El `OllamaFalso` detrás de un puerto de verdad: el comando arma su propio motor."""
    motor = OllamaFalso()

    class Manejador(BaseHTTPRequestHandler):
        def _responder(self) -> None:
            cuerpo = self.rfile.read(int(self.headers.get("content-length") or 0))
            pedido = httpx.Request(self.command, f"http://motor{self.path}", content=cuerpo)
            respuesta = motor(pedido)
            self.send_response(respuesta.status_code)
            self.send_header("content-length", str(len(respuesta.content)))
            self.end_headers()
            self.wfile.write(respuesta.content)

        do_GET = do_POST = _responder

        def log_message(self, *args: object) -> None:
            pass

    servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    yield motor, f"http://127.0.0.1:{servidor.server_port}"
    servidor.shutdown()
    servidor.server_close()


def _bajados(motor: OllamaFalso) -> list[str]:
    return [json.loads(p.content)["model"] for p in motor.peticiones if p.url.path == "/api/pull"]


@pytest.mark.rf("RF-32")
def test_modelos_baja_los_que_faltan_de_la_configuracion_vigente(
    url_app, editar_fila, ollama_en_red, monkeypatch
):
    motor, url = ollama_en_red
    # Cambiar de modelo es editar su fila (DEC-87): lo que se baja sale de ahí.
    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET modelo = 'vision-nueva'"
        " WHERE capacidad = 'describir_imagen'"
    )
    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET modelo = 'texto-de-prueba'"
        " WHERE capacidad = 'resumir'"
    )
    # `responder` usa el modelo de `resumir` (052).
    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET modelo = 'texto-de-prueba'"
        " WHERE capacidad = 'responder'"
    )
    # `redactar_efemeride` usa el de `responder` (057).
    editar_fila(
        "UPDATE acervo.configuracion_capacidad SET modelo = 'texto-de-prueba'"
        " WHERE capacidad = 'redactar_efemeride'"
    )
    motor.modelos_descargados = ["texto-de-prueba"]
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_MOTOR_URL", url)
    # Los dos Ollama comparten los modelos y solo el de generación los escribe: el de
    # `embeber` también se baja por él, y el de embeddings no se consulta.
    monkeypatch.setenv("HV_MOTOR_EMBEDDINGS_URL", MOTOR_CAIDO)

    assert main(["modelos"]) == 0
    assert _bajados(motor) == ["bge-m3", "vision-nueva"]
    assert main(["modelos"]) == 0
    assert _bajados(motor) == ["bge-m3", "vision-nueva"]  # la segunda vez no falta nada


@pytest.mark.rf("RF-32")
def test_modelos_con_el_motor_caido_falla_con_un_mensaje_claro(url_app, monkeypatch, capsys):
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_MOTOR_URL", MOTOR_CAIDO)

    assert main(["modelos"]) == 1
    assert "no responde" in capsys.readouterr().err


@pytest.mark.rf("RF-32")
def test_modelos_con_la_base_caida_falla_con_un_mensaje_claro(monkeypatch, capsys):
    monkeypatch.setenv("HV_BD_URL", "postgresql://hv_app:x@127.0.0.1:1/historia_viva")

    assert main(["modelos"]) == 1
    assert "La base de datos no responde" in capsys.readouterr().err


_INSTRUCCION_DE_OTRA_CAPACIDAD = "SET instruccion_id = 'resumir_ficha'"


@pytest.mark.rf("RF-32")
@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        (_INSTRUCCION_DE_OTRA_CAPACIDAD, "describir_imagen.instruccion_id"),
        ("SET modelo = ''", "describir_imagen.modelo"),
        ("SET motor = 'vllm'", "describir_imagen.motor"),
    ],
)
@pytest.mark.parametrize(
    "comando",
    [
        ["web"],
        ["trabajador", "--tipos", "procesamiento"],
        ["trabajador", "--tipos", "efemeride"],
        ["modelos"],
    ],
)
def test_con_una_fila_invalida_los_comandos_fallan_con_un_mensaje_claro(
    comando, cambio, campo, url_app, editar_fila, monkeypatch, capsys, tmp_path
):
    editar_fila(
        f"UPDATE acervo.configuracion_capacidad {cambio} WHERE capacidad = 'describir_imagen'"
    )
    monkeypatch.setenv("HV_BD_URL", url_app)
    monkeypatch.setenv("HV_ORIGINALES", str(tmp_path))
    monkeypatch.setenv("HV_MOTOR_URL", MOTOR_CAIDO)
    # Si la base no responde en `ESPERA_S`, `web` y el trabajador arrancan igual (sin validar) y
    # el test quedaría colgado en uvicorn o en el bucle de la cola: que falle en su lugar.
    for arranque in (
        "uvicorn.run",
        "historia_viva.composicion._trabajar",
        "historia_viva.composicion._planificar",
    ):
        monkeypatch.setattr(arranque, _no_debia_arrancar)

    assert main(comando) == 1
    assert campo in capsys.readouterr().err


def _no_debia_arrancar(*_args: object, **_kwargs: object) -> None:
    pytest.fail("arrancó sin ver la fila inválida: la base no respondió a tiempo")


@pytest.mark.rf("RF-32")
def test_sin_la_fila_de_una_capacidad_de_r1_web_no_arranca(url_app, borrar_fila, tmp_path):
    borrar_fila("resumir")

    with pytest.raises(ConfiguracionInvalida, match="resumir: falta la fila"):
        componer_web(_entorno(url_app, tmp_path))


@pytest.mark.rf("RF-32")
def test_una_fila_invalida_impide_componer_web_y_el_trabajador(url_app, editar_fila, tmp_path):
    editar_fila(
        f"UPDATE acervo.configuracion_capacidad {_INSTRUCCION_DE_OTRA_CAPACIDAD}"
        " WHERE capacidad = 'describir_imagen'"
    )
    entorno = _entorno(url_app, tmp_path)

    with pytest.raises(ConfiguracionInvalida, match=r"describir_imagen\.instruccion_id"):
        componer_web(entorno)
    with pytest.raises(ConfiguracionInvalida, match=r"describir_imagen\.instruccion_id"):
        componer_trabajador(entorno, ["procesamiento"])


@pytest.mark.historia(224)
@pytest.mark.rnf("RNF-19")
def test_cada_servicio_rota_sus_logs_y_ninguno_monta_el_socket_de_docker():
    # Spec §2.4: driver `local`, 5 archivos de 20 MB.
    registros = {nombre: s.get("logging") for nombre, s in SERVICIOS.items()}
    rotado = {"driver": "local", "options": {"max-size": "20m", "max-file": "5"}}
    assert registros == dict.fromkeys(SERVICIOS, rotado)
    # Con el socket, un contenedor comprometido manda sobre Docker y sobre la PC (spec §2.7).
    for compose in MVP.glob("compose*.yml"):
        assert "docker.sock" not in compose.read_text(encoding="utf-8"), compose.name


TECHO = MVP / "compose.techo.yml"


@pytest.mark.historia(237)
@pytest.mark.rnf("RNF-03")
@_SIN_DOCKER
def test_el_override_de_techo_entra_en_8_gb_con_un_solo_modelo_de_generacion(tmp_path):
    config = _compose_config(tmp_path, TECHO)
    servicios = config["services"]
    # Los de una sola vez (`migrar`, `modelos`) terminan antes de la tanda.
    permanentes = {n: s for n, s in servicios.items() if s.get("restart") != "no"}

    assert all("mem_limit" in s for s in permanentes.values()), sorted(permanentes)
    assert sum(int(s["mem_limit"]) for s in permanentes.values()) < 8 * 2**30
    assert servicios["ollama"]["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert servicios["ollama"]["cpus"] == 4
    # Recorta, no publica nada nuevo.
    assert _publicados(config) == {"caddy": {("127.0.0.1", "80", 80)}}


@pytest.mark.rnf("RNF-19")
def test_el_log_es_json_y_no_lleva_la_consulta_ni_el_mensaje_de_una_excepcion():
    from historia_viva.composicion.registro import RegistroJson

    acceso = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("172.18.0.5:4000", "GET", "/v1/fragmentos?q=convenio%20secreto", "1.1", 200), None,
    )  # fmt: skip
    try:
        raise ValueError("texto de la Ficha")
    except ValueError:
        falla = logging.LogRecord(
            "historia_viva.x", logging.ERROR, "", 0, "Falló el trabajo %s.", ("t1",),
            sys.exc_info(),
        )  # fmt: skip
    falla.pieza_id = "p1"
    falla.texto = "lo que no está en la lista blanca"

    linea_acceso = json.loads(RegistroJson().format(acceso))
    linea_falla = json.loads(RegistroJson().format(falla))

    assert linea_acceso["evento"] == "GET /v1/fragmentos 200"
    assert {k: linea_falla[k] for k in ("nivel", "evento", "pieza_id", "excepcion")} == {
        "nivel": "ERROR",
        "evento": "Falló el trabajo t1.",
        "pieza_id": "p1",
        "excepcion": "ValueError",
    }
    assert "test_composicion.py" in linea_falla["traza"][0]
    todo = json.dumps([linea_acceso, linea_falla], ensure_ascii=False)
    for contenido in ("secreto", "texto de la Ficha", "lista blanca"):
        assert contenido not in todo


@pytest.mark.rnf("RNF-19")
def test_los_procesos_de_la_app_escriben_json_por_stdout():
    codigo = (
        "import logging; from historia_viva.composicion.registro import configurar_registro;"
        " configurar_registro(); logging.getLogger('historia_viva.x').info('en marcha')"
    )
    hecho = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True, cwd=MVP
    )

    assert hecho.stderr == ""
    assert json.loads(hecho.stdout)["evento"] == "en marcha"
