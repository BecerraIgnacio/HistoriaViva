"""El manual en `/ayuda` (historia 244): `docs/manual.md` y los runbooks que enlaza, servidos
como páginas de la web.

El Markdown se pasa a HTML acá, con un subconjunto chico (títulos, párrafos, listas anidadas,
tablas, bloques de código, `código`, **negrita**, enlaces e imágenes) y **todo el texto
escapado**: un `<script>` o un `<img onerror>` escrito en el Markdown se ve como texto, nunca se
ejecuta. Un enlace solo sale si su destino es un ancla, otra página de `docs/`, una ruta de la
web o `https://`; si no, queda el texto. La página no trae scripts ni estilos en línea (CSP).

Los documentos viven en `docs/`, fuera de la imagen: el compose los monta de solo lectura en
`/app/docs`, que es la misma ruta relativa al paquete que en el repositorio.
"""

from __future__ import annotations

import re
import unicodedata
from html import escape
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

__all__ = ["DIRECTORIO_DOCS", "anclas", "enlaces", "pagina", "router"]

# shortcut: supone el paquete instalado editable (`uv sync`), como `matriz`; si deja de serlo,
# pasar la ruta por el entorno.
DIRECTORIO_DOCS = Path(__file__).resolve().parents[3] / "docs"
_SHELL = Path(__file__).resolve().parent.parent / "web" / "ayuda.html"

_NOMBRE = re.compile(r"[a-z0-9][a-z0-9-]*")
_TITULO = re.compile(r"(#{1,4})\s+(.*)")
_ITEM = re.compile(r"( *)([-*]|\d+\.) +(.*)")
_SEPARADOR = re.compile(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?")
_ENLACE = re.compile(r"(!?)\[([^\]]+)\]\(([^)\s]+)\)")
_NEGRITA = re.compile(r"\*\*(.+?)\*\*")
_DESTINO_MD = re.compile(r"([a-z0-9][a-z0-9-]*)\.md(#[a-z0-9-]+)?")
_CAPTURA = re.compile(r"capturas/[a-z0-9][a-z0-9-]*\.png")

router = APIRouter(include_in_schema=False)


def _slug(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", sin_tildes.lower()).strip("-")


def _destino(url: str) -> str | None:
    """El `href` de un enlace del Markdown, o None si no es uno que la web sirva."""
    if m := _DESTINO_MD.fullmatch(url):
        return ("/ayuda" if m[1] == "manual" else f"/ayuda/{m[1]}") + (m[2] or "")
    # `//otro.sitio` es un enlace a otro sitio con el esquema de la página, no una ruta de la web.
    if re.fullmatch(r"#[a-z0-9-]+|/(?!/)[A-Za-z0-9/._#?=-]*|https://[^\s\"'<>*]+", url):
        return url
    return None


def _en_linea(texto: str) -> str:
    """Código, enlaces, imágenes y negrita de una línea; el resto, escapado."""
    codigos: list[str] = []

    def apartar(m: re.Match[str]) -> str:
        codigos.append(f"<code>{escape(m[1])}</code>")
        return f"\x00{len(codigos) - 1}\x00"

    texto = re.sub(r"`([^`]+)`", apartar, texto)
    trozos, desde = [], 0
    for m in _ENLACE.finditer(texto):
        trozos.append(escape(texto[desde : m.start()]))
        imagen, rotulo, url = m[1], escape(m[2]), m[3]
        if imagen and _CAPTURA.fullmatch(url):
            trozos.append(f'<img src="/ayuda/{url}" alt="{rotulo}">')
        elif not imagen and (href := _destino(url)):
            trozos.append(f'<a href="{escape(href)}">{rotulo}</a>')
        else:
            trozos.append(rotulo)
        desde = m.end()
    trozos.append(escape(texto[desde:]))
    html = _NEGRITA.sub(r"<strong>\1</strong>", "".join(trozos))
    return re.sub(r"\x00(\d+)\x00", lambda m: codigos[int(m[1])], html)


def _sangria(linea: str) -> int:
    return len(linea) - len(linea.lstrip(" "))


def _empieza_bloque(linea: str) -> bool:
    texto = linea.strip()
    return bool(_TITULO.fullmatch(texto) or texto.startswith(("```", "|")) or _ITEM.fullmatch(linea))


def _bloques(lineas: list[str], ids: dict[str, int]) -> str:
    salida: list[str] = []
    i, n = 0, len(lineas)
    while i < n:
        linea, texto = lineas[i], lineas[i].strip()
        if not texto:
            i += 1
        elif texto.startswith("```"):
            sangria, j = _sangria(linea), i + 1
            while j < n and not lineas[j].strip().startswith("```"):
                j += 1
            codigo = "\n".join(l[sangria:] if not l[:sangria].strip() else l.lstrip() for l in lineas[i + 1 : j])
            salida.append(f'<pre tabindex="0"><code>{escape(codigo)}</code></pre>')
            i = j + 1
        elif m := _TITULO.fullmatch(texto):
            nivel, slug = len(m[1]), _slug(m[2]) or "seccion"
            ids[slug] = ids.get(slug, 0) + 1
            ancla = slug if ids[slug] == 1 else f"{slug}-{ids[slug]}"
            salida.append(f'<h{nivel} id="{ancla}">{_en_linea(m[2])}</h{nivel}>')
            i += 1
        elif texto.startswith("|") and i + 1 < n and _SEPARADOR.fullmatch(lineas[i + 1].strip()):
            filas = []
            while i < n and lineas[i].strip().startswith("|"):
                filas.append([c.strip() for c in lineas[i].strip().strip("|").split("|")])
                i += 1
            cabecera, cuerpo = filas[0], filas[2:]
            th = "".join(f'<th scope="col">{_en_linea(c)}</th>' for c in cabecera)
            tr = "".join("<tr>" + "".join(f"<td>{_en_linea(c)}</td>" for c in f) + "</tr>" for f in cuerpo)
            salida.append(f'<div class="tabla" tabindex="0"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>')
        elif m := _ITEM.fullmatch(linea):
            ordenada, base = m[2][0].isdigit(), len(m[1])
            items: list[str] = []
            while i < n and (m := _ITEM.fullmatch(lineas[i])) and len(m[1]) == base and m[2][0].isdigit() == ordenada:
                contenido_desde = m.start(3)
                contenido = [m[3]]
                i += 1
                while i < n:
                    actual = lineas[i]
                    if not actual.strip():
                        k = i
                        while k < n and not lineas[k].strip():
                            k += 1
                        if k < n and _sangria(lineas[k]) >= contenido_desde:
                            contenido.extend([""] * (k - i))
                            i = k
                            continue
                        break
                    if _sangria(actual) >= contenido_desde:
                        contenido.append(actual[contenido_desde:])
                    elif _sangria(actual) > base and not _ITEM.fullmatch(actual):
                        contenido.append(actual.strip())
                    elif not _empieza_bloque(actual) and contenido[-1].strip():
                        contenido.append(actual.strip())  # continuación perezosa del párrafo
                    else:
                        break
                    i += 1
                items.append(f"<li>{_bloques(contenido, ids)}</li>")
            etiqueta = "ol" if ordenada else "ul"
            salida.append(f"<{etiqueta}>{''.join(items)}</{etiqueta}>")
        else:
            parrafo = [texto]
            i += 1
            while i < n and lineas[i].strip() and not _empieza_bloque(lineas[i]):
                parrafo.append(lineas[i].strip())
                i += 1
            salida.append(f"<p>{_en_linea(' '.join(parrafo))}</p>")
    return "".join(salida)


def html_de(markdown: str) -> str:
    """El Markdown del manual o de un runbook, como HTML sin nada activo."""
    return _bloques(markdown.splitlines(), {})


def anclas(markdown: str) -> set[str]:
    """Los `id` que tendrán los títulos de la página."""
    return set(re.findall(r'<h[1-4] id="([^"]+)"', html_de(markdown)))


def enlaces(markdown: str) -> list[tuple[bool, str]]:
    """`(es_imagen, destino)` de cada enlace del Markdown, fuera de los bloques de código."""
    sin_codigo = re.sub(r"```.*?```", "", markdown, flags=re.S)
    sin_codigo = re.sub(r"`[^`\n]*`", "", sin_codigo)
    return [(bool(m[1]), m[3]) for m in _ENLACE.finditer(sin_codigo)]


def pagina(nombre: str) -> str:
    """La página completa de `docs/<nombre>.md` dentro de la plantilla de `/ayuda`."""
    markdown = (DIRECTORIO_DOCS / f"{nombre}.md").read_text(encoding="utf-8")
    titulo = next((m[2] for l in markdown.splitlines() if (m := _TITULO.fullmatch(l)) and m[1] == "#"), "Ayuda")
    shell = _SHELL.read_text(encoding="utf-8")
    shell = shell.replace("<title>Historia Viva — Ayuda</title>", f"<title>{escape(titulo)} — Historia Viva</title>")
    return re.sub(r'(<main id="manual">).*?(</main>)', lambda m: m[1] + html_de(markdown) + m[2], shell, flags=re.S)


def _existe(nombre: str) -> bool:
    return bool(_NOMBRE.fullmatch(nombre)) and (DIRECTORIO_DOCS / f"{nombre}.md").is_file()


@router.get("/ayuda")
def manual() -> HTMLResponse:
    if not _existe("manual"):
        raise HTTPException(404)
    return HTMLResponse(pagina("manual"))


@router.get("/ayuda/capturas/{archivo}")
def captura(archivo: str) -> FileResponse:
    if not _CAPTURA.fullmatch(f"capturas/{archivo}") or not (DIRECTORIO_DOCS / "capturas" / archivo).is_file():
        raise HTTPException(404)
    return FileResponse(DIRECTORIO_DOCS / "capturas" / archivo, media_type="image/png")


@router.get("/ayuda/{nombre}")
def documento(nombre: str) -> HTMLResponse:
    if not _existe(nombre):
        raise HTTPException(404)
    return HTMLResponse(pagina(nombre))
