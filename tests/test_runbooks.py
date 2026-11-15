"""Los runbooks R1 a R8 de operación diaria (ticket 115): sus cuatro secciones, que ninguno
borra volúmenes y que cada comando de la imagen que nombran existe con sus flags."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from historia_viva.composicion import main

pytestmark = pytest.mark.historia(235)

DOCS = Path(__file__).resolve().parents[1] / "docs"
DIARIOS = sorted(DOCS.glob("runbook-r[1-8]-*.md"))
SECCIONES = ("## Cuándo", "## Pasos", "## Verificación", "## Si falla")
BORRA_VOLUMENES = re.compile(r"down\s+(-v|--volumes)|volume\s+(rm|prune)|prune\s+--volumes")
COMPOSE = yaml.safe_load((DOCS.parent / "compose.yml").read_text(encoding="utf-8"))
# `banco` es la imagen con `banco` ya puesto en el entrypoint: `dc run --rm banco comparar …`.
PREFIJO = {"web": [], "banco": COMPOSE["services"]["banco"]["entrypoint"][3:]}


def _bloques(texto: str) -> list[str]:
    """Las líneas de los bloques de código, con las continuaciones de PowerShell (`) unidas."""
    lineas = "\n".join(re.findall(r"```powershell\n(.*?)```", texto, re.S))
    return lineas.replace("`\n", " ").splitlines()


def test_son_los_ocho_con_sus_cuatro_secciones_y_dicen_runbook_rn():
    assert [p.name.split("-")[1] for p in DIARIOS] == [f"r{n}" for n in range(1, 9)]
    for pagina in DIARIOS:
        texto = pagina.read_text(encoding="utf-8")
        assert all(s in texto for s in SECCIONES), pagina.name
        assert texto.startswith(f"# Runbook R{pagina.name.split('-')[1][1:]}:"), pagina.name
        assert "## Registro de ejecución" in texto, pagina.name


def test_ningun_paso_borra_volumenes():
    for pagina in DIARIOS:
        assert not [
            linea for linea in _bloques(pagina.read_text(encoding="utf-8"))
            if BORRA_VOLUMENES.search(linea)
        ], pagina.name


def test_cada_comando_de_la_imagen_existe_con_sus_flags(capsys):
    comandos: set[tuple[str, ...]] = set()
    for pagina in DIARIOS:
        texto = pagina.read_text(encoding="utf-8")
        # Los bloques y el código en línea del texto (`dc run --rm web interruptor estado`).
        for linea in [*_bloques(texto), *re.findall(r"`([^`\n]+)`", texto)]:
            llamada = re.search(r"\bdc run --rm (?:-\S+ \S+ )*(web|banco) (.+)", linea)
            if llamada is None:
                continue
            palabras = [*PREFIJO[llamada[1]], *llamada[2].split("#")[0].split()]
            sub = tuple(_hasta_el_primer_argumento(palabras))
            flags = tuple(p for p in palabras if p.startswith("--"))
            comandos.add((*sub, *flags))
    assert len(comandos) >= 12
    for comando in sorted(comandos):
        sub = tuple(p for p in comando if not p.startswith("--"))
        with pytest.raises(SystemExit) as salida:
            main([*sub, "--help"])
        ayuda = capsys.readouterr().out
        assert salida.value.code == 0, comando
        assert all(f in ayuda for f in comando if f.startswith("--")), (comando, ayuda)


def test_el_banco_corre_en_su_servicio_sin_montar_la_referencia_a_mano():
    # Ticket 132: la referencia la montan `web` y `banco` desde el `.env`.
    a_mano = re.compile(
        r"-e HV_REFERENCIA|:/referencia\b|\bweb (python -m historia_viva\.composicion )?banco\b"
    )
    for pagina in (DOCS / "runbook-r8-cambiar-de-modelo.md",):
        texto = pagina.read_text(encoding="utf-8")
        assert "dc run --rm banco " in texto, pagina.name
        assert not a_mano.search(texto), pagina.name


def _hasta_el_primer_argumento(palabras: list[str]) -> list[str]:
    sub = []
    for palabra in palabras:
        if not re.fullmatch(r"[a-z][a-z-]*", palabra):
            break
        sub.append(palabra)
    return sub
