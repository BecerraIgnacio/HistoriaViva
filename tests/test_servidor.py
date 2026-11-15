"""La receta del servidor de la universidad y su prueba de humo (ticket 121, runbook R16, historia
241; DEC-119, DEC-139, R-34, DIR-97).

Sin Docker a mano, lo que es de contenedores se prueba con `docker compose config` y leyendo los
archivos; la prueba de humo corre entera contra la app levantada en proceso con uvicorn, sobre una
base nacida como la del servidor (solo migraciones), con el proveedor OIDC falso de 036 como
servicio aparte. Lo que depende del servidor real queda en el runbook, para una persona.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

import prueba_de_humo
from aplicacion import sin_videos
from base_de_pruebas import CLAVE_APP
from capa_modelos_falsa import CapaModelosFalsa
from historia_viva.agentes import Credenciales, Scope
from historia_viva.api.app import crear_app
from historia_viva.auditoria import Actor, RegistroDeAuditoria, TipoDeActor
from historia_viva.bd import Bd
from historia_viva.composicion import (
    Entorno,
    EntornoInvalido,
    _trabajar,
    armar_grafo,
    armar_planificador,
    armar_trabajador,
    componer_web,
    estado_del_servidor,
    main,
)
from historia_viva.dominio.tipos import MetodoAutenticacion
from historia_viva.identidad.oidc import ConfiguracionOIDC
from historia_viva.migraciones import aplicar
from historia_viva.procesamiento.cola import TipoDeTrabajo
from proveedor_oidc_falso import ProveedorFalso
from test_informes import _aprobado, _crear, acervo, fichas, informes  # noqa: F401

pytestmark = pytest.mark.historia(241)

MVP = Path(__file__).resolve().parent.parent
SERVIDOR = MVP / "compose.servidor.yml"
HUMO = MVP / "compose.humo.yml"
RECETA = MVP / "docs" / "runbook-r16-receta-de-servidor.md"
GENERADOR = MVP / "generar-env-servidor.sh"

DOMINIO = "historiaviva.fie.undef.edu.ar"
URL = f"https://{DOMINIO}"

ENTORNO_DEL_SERVIDOR = {
    "HV_DESPLIEGUE": "servidor",
    "HV_AUTH": "oidc",
    "HV_URL_PUBLICA": URL,
    "HV_ORIGEN": URL,
    "HV_OIDC_CLIENTE_ID": "cliente.apps.googleusercontent.com",
    "HV_OIDC_SECRETO": "secreto",
    "HV_CLAVE_COOKIE_PREVIA": "c" * 64,
}
"""Lo que `compose.servidor.yml` le pasa a `web` con el `.env` completo."""

_SIN_DOCKER = pytest.mark.skipif(
    shutil.which("docker") is None, reason="sin Docker no hay `compose config`"
)


# --- La guarda de arranque (paso 3) -------------------------------------------------------------


@pytest.mark.parametrize(
    "variables",
    [
        {"HV_ENTORNO": "desarrollo"},
        {"HV_ENTORNO": "produccion"},
        {"HV_AUTH": "dev", "HV_ENTORNO": "desarrollo"},
        {"HV_AUTH": "dev"},
    ],
    ids=["entorno-desarrollo", "entorno-cualquiera", "dev-con-entorno", "dev"],
)
def test_en_el_servidor_ningun_comando_arranca_con_hv_entorno_ni_dev(variables):
    for despliegue in ("servidor", "humo"):
        with pytest.raises(EntornoInvalido, match="En el servidor"):
            Entorno.desde_variables({"HV_DESPLIEGUE": despliegue, **variables})


def test_hv_despliegue_solo_admite_servidor_o_humo():
    with pytest.raises(EntornoInvalido, match="HV_DESPLIEGUE"):
        Entorno.desde_variables({"HV_DESPLIEGUE": "produccion"})

    assert Entorno.desde_variables({}).servidor is False
    assert Entorno.desde_variables({"HV_DESPLIEGUE": ""}).servidor is False
    assert Entorno.desde_variables(ENTORNO_DEL_SERVIDOR).servidor is True
    assert Entorno.desde_variables({**ENTORNO_DEL_SERVIDOR, "HV_DESPLIEGUE": "humo"}).servidor


@pytest.mark.rnf("RNF-14")
@pytest.mark.parametrize("emisor", ["http://oidc-falso:9000", "http://accounts.google.com"])
def test_en_el_servidor_el_emisor_oidc_es_https(emisor):
    """Un `.env` equivocado no deja entrar por el proveedor falso de la prueba de humo: con
    `servidor` el emisor es https; solo `humo`, que pone `compose.humo.yml`, admite el falso."""
    with pytest.raises(EntornoInvalido, match="HV_OIDC_EMISOR es https"):
        Entorno.desde_variables({**ENTORNO_DEL_SERVIDOR, "HV_OIDC_EMISOR": emisor})

    humo = Entorno.desde_variables(
        {**ENTORNO_DEL_SERVIDOR, "HV_DESPLIEGUE": "humo", "HV_OIDC_EMISOR": emisor}
    )
    assert humo.oidc is not None and humo.oidc.emisor == emisor
    google = Entorno.desde_variables(
        {**ENTORNO_DEL_SERVIDOR, "HV_OIDC_EMISOR": "https://accounts.google.com"}
    )
    assert google.oidc is not None


@pytest.mark.parametrize(
    ("cambio", "motivo"),
    [
        ({"HV_OIDC_CLIENTE_ID": ""}, "HV_OIDC_CLIENTE_ID"),
        ({"HV_OIDC_SECRETO": ""}, "HV_OIDC_SECRETO"),
        ({"HV_CLAVE_COOKIE_PREVIA": ""}, "HV_CLAVE_COOKIE_PREVIA"),
        ({"HV_URL_PUBLICA": "", "HV_ORIGEN": ""}, "https"),
        ({"HV_URL_PUBLICA": f"http://{DOMINIO}", "HV_ORIGEN": f"http://{DOMINIO}"}, "https"),
        ({"HV_ORIGEN": "http://localhost"}, "HV_ORIGEN"),
        ({"HV_ENTORNO": "desarrollo"}, "HV_ENTORNO"),
    ],
    ids=["sin-cliente", "sin-secreto", "sin-clave-previa", "sin-url", "http", "otro-origen",
         "con-entorno"],
)
def test_en_el_servidor_web_aborta_si_falta_un_valor_obligatorio(
    cambio, motivo, monkeypatch, capsys, tmp_path
):
    """`web` sale con 2 antes de abrir la base o levantar uvicorn: lo que se ve en `dc ps`."""
    arrancada: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: arrancada.append(a))
    variables = {
        **ENTORNO_DEL_SERVIDOR,
        "HV_BD_URL": "postgresql://hv_app:x@invalid.invalid:5432/x",
        "HV_ORIGINALES": str(tmp_path),
        **cambio,
    }
    for variable, valor in variables.items():
        monkeypatch.setenv(variable, valor)

    assert main(["web"]) == 2
    assert motivo in capsys.readouterr().err
    assert arrancada == []


def test_en_el_servidor_migrar_aborta_con_hv_entorno_sin_tocar_la_base(monkeypatch, capsys):
    # Con `HV_ENTORNO=desarrollo`, `migrar` sembraría las personas de prueba (DEC-124).
    monkeypatch.setenv("HV_DESPLIEGUE", "servidor")
    monkeypatch.setenv("HV_ENTORNO", "desarrollo")
    monkeypatch.setenv("HV_BD_URL_MIGRADOR", "postgresql://hv_migrador:x@invalid.invalid:5432/x")

    assert main(["migrar"]) == 2
    assert "HV_ENTORNO" in capsys.readouterr().err


@pytest.mark.confirma("`componer_web` arma su propio pool, como en producción")
def test_en_el_servidor_web_arranca_con_el_entorno_completo(url_app, tmp_path):
    entorno = Entorno.desde_variables(
        {**ENTORNO_DEL_SERVIDOR, "HV_BD_URL": url_app, "HV_ORIGINALES": str(tmp_path)}
    )

    grafo = componer_web(entorno)
    try:
        assert grafo.oidc is not None
        assert (grafo.modo_de_ingreso, grafo.url_publica, grafo.origen) == (
            MetodoAutenticacion.OIDC, URL, URL
        )
    finally:
        grafo.bd.cerrar()


# --- El compose del servidor ------------------------------------------------------------------

OBLIGATORIAS_DEL_SERVIDOR = {
    "HV_AUTH": "oidc",
    "HV_URL_PUBLICA": URL,
    "HV_ORIGEN": URL,
    "HV_OIDC_CLIENTE_ID": "cliente",
    "HV_OIDC_SECRETO": "secreto",
    "HV_CLAVE_COOKIE_PREVIA": "previa",
    "HV_DOMINIO": DOMINIO,
    "HV_TLS": "internal",
    "HV_CERTIFICADOS": "/srv/historia-viva/certificados",
}
"""Las que `compose.servidor.yml` exige con `:?`, además de las del base."""


def _config(tmp_path: Path, *overrides: Path, **variables: str) -> dict[str, Any]:
    """`docker compose config` del servidor, sin heredar nada del entorno de quien corre, con
    todos los profiles: `run` arranca un servicio con profile sin activarlo."""
    archivos = [a for f in (MVP / "compose.yml", *overrides) for a in ("-f", str(f))]
    base = {
        "POSTGRES_PASSWORD": "a1",
        "HV_CLAVE_MIGRADOR": "b2",
        "HV_CLAVE_APP": "c3",
        "HV_ORIGINALES": str(tmp_path),
        **OBLIGATORIAS_DEL_SERVIDOR,
    }
    propias = {k: v for k, v in os.environ.items() if not k.startswith(("HV_", "POSTGRES_"))}
    salida = subprocess.run(
        ["docker", "compose", *archivos, "--profile", "*", "config", "--format", "json"],
        env={**propias, **base, **variables},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    configuracion: dict[str, Any] = json.loads(salida)
    return configuracion


@pytest.mark.rnf("RNF-02")
@_SIN_DOCKER
def test_seg_compose_del_servidor_publica_solo_caddy_en_80_y_443(tmp_path):
    servicios = _config(tmp_path, SERVIDOR)["services"]

    publicados = {
        nombre: sorted((p.get("host_ip", ""), p["published"], p["target"]) for p in s["ports"])
        for nombre, s in servicios.items()
        if s.get("ports")
    }
    # Spec §1.7, «Red»: en el servidor, el override publica `caddy` en 80 y 443 y nada más.
    assert publicados == {"caddy": [("", "443", 443), ("", "80", 80)]}
    assert all("network_mode" not in s and not s.get("privileged") for s in servicios.values())
    caddy = servicios["caddy"]
    assert caddy["command"][caddy["command"].index("--config") + 1] == (
        "/etc/caddy/Caddyfile.servidor"
    )
    assert caddy["environment"] == {"HV_DOMINIO": DOMINIO, "HV_TLS": "internal"}


@pytest.mark.rnf("RNF-14")
@_SIN_DOCKER
def test_seg_compose_del_servidor_marca_cada_servicio_de_la_app(tmp_path):
    servicios = _config(tmp_path, SERVIDOR, HV_ENTORNO="desarrollo")["services"]

    de_la_app = {n for n, s in servicios.items() if s.get("image") == "historiaviva-mvp-app"}
    assert de_la_app == {
        "web", "migrar", "modelos", "trabajador-procesamiento", "trabajador-indexacion",
        "trabajador-efemerides",
    }
    for nombre in de_la_app:
        entorno = servicios[nombre]["environment"]
        assert entorno["HV_DESPLIEGUE"] == "servidor", nombre
        # `HV_ENTORNO` llega tal cual del `.env`: la que aborta es la app (`Entorno`).
        assert entorno.get("HV_ENTORNO") in (None, "desarrollo"), nombre
    web = servicios["web"]["environment"]
    assert {v: web[v] for v in ("HV_AUTH", "HV_URL_PUBLICA", "HV_ORIGEN")} == {
        "HV_AUTH": "oidc", "HV_URL_PUBLICA": URL, "HV_ORIGEN": URL,
    }
    # Lo que `web` recibe del compose arma un `Entorno` de servidor sin errores.
    assert Entorno.desde_variables({**web, "HV_ENTORNO": ""}).servidor is True
    with pytest.raises(EntornoInvalido, match="HV_ENTORNO"):
        Entorno.desde_variables(web)
    # `HV_PROXY` vacía: sin `dev`, lo que llega de Caddy no cuenta como hecho en la PC.
    assert web["HV_PROXY"] == ""
    # Solo `compose.humo.yml` pone `humo`, y solo en `web`.
    humo = _config(tmp_path, SERVIDOR, HUMO, HV_HUMO_ADMIN="t@fie.undef.edu.ar")["services"]
    assert {n: humo[n]["environment"]["HV_DESPLIEGUE"] for n in de_la_app} == {
        n: "humo" if n == "web" else "servidor" for n in de_la_app
    }


@pytest.mark.historia(169)
@_SIN_DOCKER
def test_seg_el_servidor_no_lleva_la_referencia_ni_el_banco(tmp_path):
    """El banco es de la PC (DEC-132): aunque el `.env` traiga `HV_REFERENCIA`, el servidor no
    monta la referencia ni tiene el servicio `banco`."""
    config = _config(tmp_path, SERVIDOR, HV_REFERENCIA=str(tmp_path / "referencia"))
    web = config["services"]["web"]

    assert "banco" not in config["services"]
    assert "HV_REFERENCIA" not in web["environment"]
    assert [v["target"] for v in web["volumes"]] == ["/data/originales", "/app/docs"]
    assert "hvmvp_sin_referencia" not in config["volumes"]


@pytest.mark.rnf("RNF-14")
@_SIN_DOCKER
@pytest.mark.parametrize("variable", sorted(OBLIGATORIAS_DEL_SERVIDOR))
def test_seg_compose_del_servidor_no_levanta_sin_un_valor_obligatorio(tmp_path, variable):
    with pytest.raises(subprocess.CalledProcessError) as error:
        _config(tmp_path, SERVIDOR, **{variable: ""})

    assert variable in error.value.stderr


@pytest.mark.rnf("RNF-02")
@_SIN_DOCKER
def test_seg_sin_el_override_de_humo_no_hay_proveedor_falso(tmp_path):
    """El proveedor OIDC falso y el cliente de la prueba de humo viven en `compose.humo.yml`,
    fuera de la receta de producción (auditoría, decisión 7)."""
    servidor = _config(tmp_path, SERVIDOR)["services"]
    humo = _config(tmp_path, SERVIDOR, HUMO, HV_HUMO_ADMIN="t@fie.undef.edu.ar")["services"]

    assert "oidc-falso" not in servidor and "humo" not in servidor
    assert set(humo) - set(servidor) == {"oidc-falso", "humo"}
    assert humo["humo"]["profiles"] == ["humo"]
    assert "ports" not in humo["oidc-falso"] and "ports" not in humo["humo"]
    # Dentro de la red, el nombre del servidor es Caddy: `humo` entra por TLS, como la SEU.
    assert humo["caddy"]["networks"]["default"]["aliases"] == [DOMINIO]
    assert humo["humo"]["command"][humo["humo"]["command"].index("--url") + 1] == URL
    for texto in (SERVIDOR.read_text(encoding="utf-8"), (MVP / "compose.yml").read_text("utf-8")):
        assert "oidc-falso" not in texto and "prueba_de_humo" not in texto


def _activo(caddyfile: str) -> str:
    return "\n".join(linea.split("#")[0].rstrip() for linea in caddyfile.splitlines())


SITIO = re.compile(r"^(?!\{$)\S[^\n]*\{$", re.M)
"""La línea que abre un sitio: sin sangría y que no es el `{` de las opciones globales."""


def _sitio(activo: str) -> list[str]:
    """Las directivas del único sitio, sin las líneas en blanco."""
    inicio = SITIO.search(activo)
    assert inicio is not None
    return [linea.strip() for linea in activo[inicio.end():].splitlines() if linea.strip()]


@pytest.mark.rnf("RNF-21")
def test_seg_cabeceras_del_servidor():
    """`test_seg_cabeceras` contra el Caddyfile del servidor: el mismo sitio que el de la PC,
    con TLS en el nombre de la FIE y HSTS (spec §1.7)."""
    pc = _activo((MVP / "Caddyfile").read_text(encoding="utf-8"))
    servidor = _activo((MVP / "Caddyfile.servidor").read_text(encoding="utf-8"))

    assert SITIO.findall(servidor) == ["{$HV_DOMINIO} {"]
    # Lo que difiere es solo esto; el resto (topes por ruta, sin `Server`, el flujo de la
    # Conversación sin búfer) es el sitio de la PC línea por línea.
    propias = ["tls {$HV_TLS}", "header Strict-Transport-Security max-age=31536000"]
    de_la_pc = [d for d in _sitio(pc) if not d.startswith(("@por_ip", "redir @por_ip"))]
    assert [d for d in _sitio(servidor) if d not in propias] == de_la_pc
    assert all(p in _sitio(servidor) for p in propias)
    assert re.search(r"servers \{\s*timeouts \{\s*read_body 60m\s*\}\s*\}", servidor)
    assert len(re.findall(r"request_body", servidor)) == 2


CADDY = os.environ.get("HV_CADDY") or shutil.which("caddy")


@pytest.mark.rnf("RNF-21")
@pytest.mark.skipif(CADDY is None, reason="sin el binario de Caddy (HV_CADDY)")
def test_el_caddyfile_del_servidor_lo_acepta_caddy(tmp_path):
    assert CADDY
    resultado = subprocess.run(
        [CADDY, "adapt", "--adapter", "caddyfile", "--config", MVP / "Caddyfile.servidor"],
        env={**os.environ, "HV_DOMINIO": DOMINIO, "HV_TLS": "internal"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert resultado.returncode == 0, resultado.stderr
    assert DOMINIO in resultado.stdout


# --- Secretos nuevos (paso 3) -----------------------------------------------------------------

SECRETOS = {
    "POSTGRES_PASSWORD": 48, "HV_CLAVE_MIGRADOR": 48, "HV_CLAVE_APP": 48,
    "HV_CLAVE_COOKIE_PREVIA": 64,
}
"""Las claves que la receta genera, con su largo en hexadecimal (`openssl rand -hex 24/32`)."""


@pytest.mark.rnf("RNF-14")
def test_seg_la_receta_genera_secretos_nuevos_sin_leer_el_entorno_de_la_pc():
    """Revisión estática: el generador solo lee `.env.example` y cada secreto sale de
    `openssl rand`; la receta no copia nada del entorno de la PC (DEC-119, DEC-139)."""
    script = GENERADOR.read_text(encoding="utf-8")
    activo = "\n".join(linea for linea in script.splitlines() if not linea.lstrip().startswith("#"))

    assert re.findall(r'^ejemplo="\$\(dirname "\$0"\)/([^"]+)"$', activo, re.M) == [".env.example"]
    assert re.findall(r"<\s*(\S+)", activo) == ['"$ejemplo"']
    assert not re.search(r"\b(source|cp|scp|rsync|cat)\b|^\s*\.\s", activo, re.M)
    for secreto, largo in SECRETOS.items():
        variable = re.search(rf'\[{secreto}\]="\$(\w+)"', activo)
        assert variable, secreto
        assert re.search(rf"^{variable[1]}=\$\(secreto {largo // 2}\)$", activo, re.M), secreto
    assert "secreto() { openssl rand -hex \"$1\"; }" in activo
    # Entre el chequeo de que no existe y la escritura, otro no lo crea: `>` falla.
    assert "set -o noclobber" in activo
    receta = RECETA.read_text(encoding="utf-8")
    bloques = "\n".join(re.findall(r"```[a-z]*\n(.*?)```", receta, re.S))
    # Ningún comando de la receta toca la PC, su `.env`, sus volúmenes ni sus volcados.
    assert not re.search(
        r"D:\\|OneDrive|hvmvp_pgdata|HV_REFERENCIA|pg_restore|restic restore|scp |rsync", bloques
    ), bloques
    assert "generar-env-servidor.sh" in bloques
    # El paso 9 mira que el proveedor falso de la prueba de humo no corra en el servidor.
    paso_9 = receta[receta.index("### 9.") : receta.index("## Alternativa sin internet")]
    assert "docker ps -a --filter name=oidc-falso" in paso_9
    assert "printenv HV_DESPLIEGUE HV_OIDC_EMISOR" in paso_9


BASH = shutil.which("bash")
"""El de `PATH` (Git Bash en Windows): `subprocess` con "bash" a secas toma el de WSL."""


def _generar(destino: Path) -> dict[str, str]:
    assert BASH
    resultado = subprocess.run(
        [BASH, GENERADOR.as_posix(), destino.as_posix(), DOMINIO],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stderr
    return dict(
        linea.split("=", 1)
        for linea in destino.read_text(encoding="utf-8").splitlines()
        if linea and not linea.startswith("#")
    )


@pytest.mark.rnf("RNF-14")
@pytest.mark.skipif(
    BASH is None or shutil.which("openssl") is None, reason="sin bash u openssl"
)
def test_el_generador_arma_un_env_de_servidor_con_secretos_distintos_en_cada_corrida(tmp_path):
    uno, otro = _generar(tmp_path / "uno.env"), _generar(tmp_path / "otro.env")

    ejemplo = (MVP / ".env.example").read_text(encoding="utf-8")
    assert list(uno) == re.findall(r"^([A-Z_]+)=", ejemplo, re.M)
    for secreto, largo in SECRETOS.items():
        assert re.fullmatch(rf"[0-9a-f]{{{largo}}}", uno[secreto]), secreto
    secretos = [corrida[s] for corrida in (uno, otro) for s in SECRETOS]
    assert len(set(secretos)) == len(secretos)
    assert {v: uno[v] for v in (
        "HV_DESPLIEGUE", "HV_AUTH", "HV_ENTORNO", "HV_DOMINIO", "HV_URL_PUBLICA", "HV_ORIGEN",
    )} == {
        "HV_DESPLIEGUE": "servidor", "HV_AUTH": "oidc", "HV_ENTORNO": "", "HV_DOMINIO": DOMINIO,
        "HV_URL_PUBLICA": URL, "HV_ORIGEN": URL,
    }
    # No pisa un `.env` que ya existe: las claves de Postgres viven en el volumen.
    antes = (tmp_path / "uno.env").read_bytes()
    resultado = subprocess.run(
        [BASH, GENERADOR.as_posix(), (tmp_path / "uno.env").as_posix(), DOMINIO],
        capture_output=True, text=True,
        check=False,
    )
    assert resultado.returncode == 1 and "Ya existe" in resultado.stderr
    assert (tmp_path / "uno.env").read_bytes() == antes


@pytest.mark.rnf("RNF-14")
@pytest.mark.skipif(BASH is None, reason="sin bash")
def test_si_openssl_falla_el_generador_no_escribe_claves_vacias(tmp_path):
    falso = tmp_path / "bin"
    falso.mkdir()
    (falso / "openssl").write_bytes(b"#!/bin/sh\nexit 0\n")  # no escribe nada
    (falso / "openssl").chmod(0o755)
    assert BASH
    resultado = subprocess.run(
        [BASH, "-c", 'PATH="$PWD/bin:$PATH" bash "$0" "$@"', GENERADOR.as_posix(),
         (tmp_path / "servidor.env").as_posix(), DOMINIO],
        capture_output=True, text=True, encoding="utf-8", cwd=tmp_path,
        check=False,
    )

    assert resultado.returncode == 1, resultado.stdout
    assert "openssl no generó una clave" in resultado.stderr
    assert not (tmp_path / "servidor.env").exists()


# --- El servidor arranca vacío (paso 9) -------------------------------------------------------


@pytest.mark.rnf("RNF-14")
def test_seg_traspaso_vacio(base_vacia, monkeypatch, capsys):
    """La base del servidor recién migrada: 0 Piezas, 0 tokens, 0 firmas de ensayo y la API de
    Agentes apagada (DEC-119, DEC-139). Es lo que corre el paso 9 en el servidor real."""
    aplicar(base_vacia)
    monkeypatch.setenv("HV_BD_URL", make_conninfo(base_vacia, user="hv_app", password=CLAVE_APP))

    assert main(["estado-vacio"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Piezas: 0",
        "Tokens de Agente: 0",
        "Firmas de ensayo: 0",
        "API de Agentes: apagada",
        "Personas: 0",
        "El servidor está vacío.",
    ]


def test_con_el_ingreso_de_la_prueba_de_humo_el_servidor_no_cuenta_como_vacio(
    base_vacia, monkeypatch, capsys
):
    aplicar(base_vacia)
    monkeypatch.setenv("HV_BD_URL", make_conninfo(base_vacia, user="hv_app", password=CLAVE_APP))
    monkeypatch.setenv("HV_DESPLIEGUE", "humo")

    assert main(["estado-vacio"]) == 1
    salida = capsys.readouterr().out
    assert "Ingreso: el proveedor OIDC falso" in salida
    assert salida.splitlines()[-1] == "El servidor NO está vacío."


def test_el_estado_vacio_ve_cada_cosa_que_no_debio_viajar(acervo, fichas, informes):  # noqa: F811
    tx = acervo
    assert estado_del_servidor(tx).vacio

    _crear(tx, fichas)()  # una Validada firmada con `dev`
    con_pieza = estado_del_servidor(tx)
    assert (con_pieza.piezas, con_pieza.firmas_de_ensayo) == (1, 1)
    _aprobado(informes, tx)  # el RGC y la Coordinación, con `dev`
    tx.execute(
        "INSERT INTO acervo.efemeride (alcance, fecha_objetivo, difusion_desde, difusion_hasta,"
        " titulo, cuerpo, estado, configuracion_efectiva, origen_generacion, aprobada_por,"
        " aprobada_el, metodo_autenticacion) VALUES ('dia', '2026-05-03', '2026-05-03',"
        " '2026-05-03', 'Un día como hoy', 'Texto [1].', 'aprobada', '{}', 'programada',"
        " %s, now(), 'dev')",
        ("11111111-1111-4111-8111-111111111111",),
    )
    assert estado_del_servidor(tx).firmas_de_ensayo == 1 + 2 + 1
    credenciales = Credenciales(bd=None, auditoria=RegistroDeAuditoria(bd=None))  # type: ignore[arg-type]
    actor = Actor(TipoDeActor.CLI, "prueba")
    agente = credenciales.crear_agente(
        tx, nombre="Agente 1", scopes=frozenset({Scope("fragmentos:leer")}), por=actor
    )
    credenciales.emitir(tx, agente_id=agente, por=actor)
    tx.execute(
        "INSERT INTO acervo.evento_interruptor (clave, habilitado, motivo, actor_tipo,"
        " actor_etiqueta) VALUES ('api_agentes_habilitada', true, 'sesión', 'cli', 'prueba')"
    )

    final = estado_del_servidor(tx)
    assert (final.piezas, final.tokens, final.firmas_de_ensayo, final.api_agentes_habilitada) == (
        1, 1, 4, True
    )
    assert not final.vacio


@pytest.mark.rnf("RNF-14")
def test_la_cli_no_tiene_comando_de_traspaso_de_datos(capsys):
    """R-34: el servidor arranca vacío; ni exportar ni importar Piezas, Fichas o tokens."""
    with pytest.raises(SystemExit):
        main(["--help"])
    ayuda = capsys.readouterr().out
    comandos = set(re.search(r"\{([a-z,-]+)\}", ayuda)[1].split(","))  # type: ignore[index]

    assert "estado-vacio" in comandos
    assert not [c for c in comandos if re.search(r"traspas|export|import|copiar|volcar", c)]


# --- La prueba de humo, en proceso (paso 8) ---------------------------------------------------

ADMIN = "tecnico.humo@fie.undef.edu.ar"


def _puerto_libre() -> int:
    with socket.socket() as libre:
        libre.bind(("127.0.0.1", 0))
        return int(libre.getsockname()[1])


@contextmanager
def _sirviendo(app: FastAPI, puerto: int) -> Iterator[None]:
    """uvicorn en un hilo, como el contenedor: HTTP de verdad, con el ciclo de vida de la app."""
    servidor = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=puerto, log_config=None, lifespan="on")
    )
    hilo = threading.Thread(target=servidor.run, name=f"uvicorn-{puerto}", daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive() and time.monotonic() < limite, "uvicorn no arrancó"
        time.sleep(0.05)
    try:
        yield
    finally:
        servidor.should_exit = True
        hilo.join(30)


@contextmanager
def _trabajadores(url_app: str, capa: CapaModelosFalsa, originales: Path) -> Iterator[None]:
    """Los de `compose.yml`: la cola (procesamiento e indexación) y el planificador de
    efemérides, en sus hilos y con sus pools. Carga llama-index antes, como `main` para `web` y
    `trabajador --tipos indexacion`: si no, el primer trabajo de indexación lo importa con la
    transacción abierta y Postgres la corta por inactiva (ticket 130)."""
    from historia_viva.indice import precargar

    precargar()
    bd_cola = Bd(url_app, tamano_pool=2)
    trabajador = armar_trabajador(
        bd=bd_cola,
        capa_para=capa.para,
        directorio_originales=originales,
        tipos=(TipoDeTrabajo.PROCESAMIENTO, TipoDeTrabajo.INDEXACION),
    )
    bucle = asyncio.new_event_loop()
    parar = asyncio.Event()

    async def cola() -> None:
        await _trabajar(trabajador, bd_cola, parar)

    hilo_cola = threading.Thread(target=bucle.run_until_complete, args=(cola(),), daemon=True)
    bd_plan = Bd(url_app, tamano_pool=2)
    planificador = armar_planificador(bd=bd_plan, capa_para=capa.para, validar=capa.validar)
    detener = threading.Event()
    hilo_plan = threading.Thread(target=planificador.bucle, args=(detener,), daemon=True)
    hilo_cola.start()
    hilo_plan.start()
    try:
        yield
    finally:
        capa.liberar()
        detener.set()
        bucle.call_soon_threadsafe(parar.set)
        hilo_cola.join(60)
        hilo_plan.join(60)
        bucle.close()
        bd_plan.cerrar()


@pytest.mark.rnf("RNF-14")
def test_la_prueba_de_humo_pasa_contra_la_app_levantada_en_proceso(
    base_vacia, capa_modelos, tmp_path, monkeypatch
):
    """Los pasos 6, 8 y 9 de la receta sobre una base nacida como la del servidor: el primer
    Administrador técnico por el comando, la prueba de humo por HTTP con OIDC contra el proveedor
    falso como servicio aparte, y el estado vacío, que después de la prueba ya no lo está."""
    aplicar(base_vacia)
    url_app = make_conninfo(base_vacia, user="hv_app", password=CLAVE_APP)
    monkeypatch.setenv("HV_BD_URL", url_app)
    assert main(["estado-vacio"]) == 0
    assert main([
        "alta-persona", "--email", ADMIN, "--nombre", "Técnico de la prueba", "--rol",
        "admin_tecnico",
    ]) == 0
    puerto_app, puerto_oidc = _puerto_libre(), _puerto_libre()
    url, emisor = f"http://127.0.0.1:{puerto_app}", f"http://127.0.0.1:{puerto_oidc}"
    proveedor = ProveedorFalso(emisor=emisor, cliente_id="humo", secreto="secreto-humo")
    originales = tmp_path / "originales"
    grafo = armar_grafo(
        bd=Bd(url_app, tamano_pool=10),
        capa_para=capa_modelos.para,
        validar=capa_modelos.validar,
        directorio_originales=originales,
        avisar=lambda: None,  # el trabajador es otro hilo: se entera por sondeo, como en Docker
        modo_de_ingreso=MetodoAutenticacion.OIDC,
        origen=url,
        analizar_video=sin_videos,
        oidc=ConfiguracionOIDC(
            emisor=emisor, cliente_id="humo", secreto="secreto-humo",
            redireccion=f"{url}/auth/callback", clave_del_intento="i" * 64,
            dominios=("fie.undef.edu.ar",),
        ),
        url_publica=url,
    )
    pasos: list[str] = []

    with (
        _sirviendo(proveedor.app, puerto_oidc),
        _sirviendo(crear_app(grafo), puerto_app),
        _trabajadores(url_app, capa_modelos, originales),
    ):
        prueba_de_humo.correr(url=url, emisor=emisor, admin=ADMIN, espera=60, informar=pasos.append)

    assert pasos[-1] == "La prueba de humo pasó."
    assert [p.split(".")[0] for p in pasos[:-1]] == [str(n) for n in range(1, 9)]
    # El paso 9 la ve: por eso la prueba de humo corre solo en la VM. Firmó con OIDC: sin
    # firmas de ensayo.
    with psycopg.connect(url_app, row_factory=dict_row) as conexion:
        despues = estado_del_servidor(conexion)
    assert (despues.piezas, despues.tokens, despues.firmas_de_ensayo) == (1, 0, 0)
    assert main(["estado-vacio"]) == 1


def test_la_prueba_de_humo_dice_en_que_paso_falla(monkeypatch, capsys):
    def caida(**_: Any) -> None:
        raise prueba_de_humo.FallaDeHumo("La salud en «ok»: no pasó en 1 s.")

    monkeypatch.setattr(prueba_de_humo, "correr", caida)

    assert prueba_de_humo.main(["--url", URL, "--emisor", "http://oidc-falso:9000",
                                "--admin", ADMIN]) == 1
    assert capsys.readouterr().err == "FALLÓ: La salud en «ok»: no pasó en 1 s.\n"

