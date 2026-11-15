"""El cuerpo de la Ficha y su Contexto: viajan separados y se guardan en un solo markdown.

Es el único dueño del formato de secciones: el Contexto va al final, bajo `## Contexto`
(ADR-0001). La pantalla y la API manejan las dos partes por separado.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from historia_viva.dominio.originales import ClaseDeOriginal

__all__ = [
    "ENCABEZADO_AFIRMACIONES",
    "ENCABEZADO_TEXTO_VISIBLE",
    "CuerpoDeFicha",
    "HojaVista",
    "MarcaDeHoja",
    "Segmento",
    "escapar_marcas",
    "escribir_afirmaciones",
    "marca_de_imagen",
    "tiene_afirmaciones",
    "segmentos",
    "segundos_de_marca",
    "ubicacion_de_imagen",
]

_ENCABEZADO = "## Contexto"
_CORTE = re.compile(rf"^{_ENCABEZADO}[ \t]*$", re.M)

ENCABEZADO_TEXTO_VISIBLE = "## Texto visible"
"""Bajo qué encabezado va el OCR de una fotografía, dentro del cuerpo de la Ficha.

Es la misma convención que `## Contexto`: el cuerpo es un markdown y sus partes se
distinguen por encabezado, no por campos separados. Quien valida ve las dos cosas en el
mismo bloque —rotulado "Descripción de la imagen" (`pantalla-validacion.md` §2)— y sabe cuál
describió el modelo y cuál leyó de la foto. **Si el OCR no devolvió nada, el encabezado
tampoco se escribe**: un encabezado vacío es trabajo de borrado para quien valida."""
_CORTE_TEXTO_VISIBLE = re.compile(rf"^{ENCABEZADO_TEXTO_VISIBLE}[ \t]*$", re.M)
ENCABEZADO_AFIRMACIONES = "## Afirmaciones"
"""Bajo qué encabezado van, una por línea (`- `), las Afirmaciones aceptadas de una foto al firmar
(ADR-0007). Va dentro del principal, antes de `## Texto visible`: se indexa como el resto."""
_CORTE_AFIRMACIONES = re.compile(rf"^{ENCABEZADO_AFIRMACIONES}[ \t]*$", re.M)
_BLOQUE_DE_AFIRMACIONES = re.compile(
    rf"^{ENCABEZADO_AFIRMACIONES}[ \t]*(?:\n[ \t]*$|\n- .*$)*\n?", re.M
)
"""El encabezado y lo que escribe `escribir_afirmaciones`: líneas en blanco y líneas `- `. Lo que
una persona escriba después de eso no es parte del bloque y no se borra."""
_MARCA_DE_PAGINA = re.compile(r"^--- Página (\d+) ---[ \t]*$", re.M)
"""La que pone `transcribir` al empezar cada página de un PDF o un Word."""
_MARCA_DE_IMAGEN = re.compile(
    r"^--- Lectura automática · (?:Página (\d+) · |Hoja «(.+)» · )?Imagen (\d+) ---[ \t]*$", re.M
)
"""Abre el bloque con el OCR de una imagen embebida (143): lo que sigue, hasta la marca siguiente,
es «Lectura automática» de esa imagen (Tesseract, no un modelo) y no texto del archivo."""
_LINEA_DE_HOJA = re.compile(r"^--- Hoja «.*$", re.M)
"""Candidata a marca de hoja: la confirma `MarcaDeHoja.leer`. Se escapa en todo texto ajeno: el
OCR de una imagen entra también al cuerpo de una planilla (143)."""
_TITULO = re.compile(r"^#{1,6} \S.*$", re.M)
"""Un título del documento, como lo escribe `transcribir` desde un Word (142)."""
_MARCAS_AJENAS = re.compile(
    "|".join(
        f"(?={marca.pattern})"
        for marca in (
            _CORTE,
            _CORTE_TEXTO_VISIBLE,
            _MARCA_DE_PAGINA,
            _CORTE_AFIRMACIONES,
            _MARCA_DE_IMAGEN,
            _LINEA_DE_HOJA,
        )
    ),
    re.M,
)
"""El comienzo de cada línea que `partir` o `segmentos` leerían como estructura, y el encabezado
de las Afirmaciones, que `escribir_afirmaciones` reescribe (144)."""
_LINEAS_EN_BLANCO_INICIALES = re.compile(r"\A(?:[ \t]*\n)+")


def escapar_marcas(texto: str) -> str:
    """`texto` con cada línea que este módulo lee como estructura escapada con `\\`
    (`\\## Contexto`, `\\## Texto visible`, `\\--- Página N ---`). Todo texto ajeno pasa por
    acá antes de entrar al cuerpo: el de un archivo (`transcribir`, 142) y el de un Agente.
    Así nunca se lee como el Contexto de una persona ni hace mentir la página de una Cita: las
    únicas marcas de página son las que el extractor pone después de escapar.

    Escapa sobre el texto ya limpio (R-36): fines de línea a `\\n`, sin controles salvo `\\n` y
    `\\t`, y en NFC. Si limpiara después, un `\\x07## Contexto`, un `## Contexto\\r` o una «á»
    descompuesta en `Página` revivirían como marca."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    sin_controles = "".join(
        caracter
        for caracter in texto
        if caracter in "\n\t" or unicodedata.category(caracter) != "Cc"
    )
    # NFC después de quitar los controles: uno entre una letra y su tilde impide componerla.
    return _MARCAS_AJENAS.sub(lambda _: "\\", unicodedata.normalize("NFC", sin_controles))


def escribir_afirmaciones(principal: str, aceptadas: Sequence[str]) -> str:
    """`principal` con las Afirmaciones `aceptadas` bajo `## Afirmaciones`, antes del texto
    visible, en lugar del bloque que ya tuviera de una firma anterior. Sin aceptadas no queda
    bloque. Cada Afirmación es una sola línea (`capa_modelos.entradas.leer_descripcion`) y es
    texto ajeno: pasa por `escapar_marcas`, como lo que viene de un archivo (142). Solo se busca
    el bloque antes del texto visible: lo que leyó el OCR no se toca.

    En un documento o una planilla (las de sus imágenes embebidas, 148) el bloque va al
    principio, antes de la primera página, hoja o imagen, donde van los avisos: al final quedaría
    dentro de la última página o del bloque de su última imagen, y su Cita mentiría."""
    corte = _CORTE_TEXTO_VISIBLE.search(principal)
    fin = len(principal) if corte is None else corte.start()
    lineas = "\n".join(f"- {escapar_marcas(afirmacion)}" for afirmacion in aceptadas)
    bloque = f"{ENCABEZADO_AFIRMACIONES}\n\n{lineas}" if aceptadas else ""
    antes = _BLOQUE_DE_AFIRMACIONES.sub("", principal[:fin])
    marcas = (_MARCA_DE_PAGINA, _LINEA_DE_HOJA, _MARCA_DE_IMAGEN)
    if any(marca.search(antes) for marca in marcas):
        partes = (bloque, _recortar(antes), _recortar(principal[fin:]))
    else:
        partes = (_recortar(antes), bloque, _recortar(principal[fin:]))
    return "\n\n".join(parte for parte in partes if parte)


def tiene_afirmaciones(principal: str) -> bool:
    """Si `principal` ya tiene el bloque de Afirmaciones antes del texto visible: se escribió en
    una firma anterior y desde entonces es texto de la persona (144)."""
    corte = _CORTE_TEXTO_VISIBLE.search(principal)
    fin = len(principal) if corte is None else corte.start()
    return _CORTE_AFIRMACIONES.search(principal, 0, fin) is not None


def ubicacion_de_imagen(numero: int, *, pagina: int | None = None, hoja: str | None = None) -> str:
    """Dónde está una imagen embebida, como la da su extractor (143): «Página N · Imagen M» en
    un PDF, «Hoja «H» · Imagen M» en una planilla e «Imagen M» en un Word, que no tiene
    páginas."""
    if pagina is not None:
        return f"Página {pagina} · Imagen {numero}"
    if hoja is not None:
        return f"Hoja «{hoja}» · Imagen {numero}"
    return f"Imagen {numero}"


def marca_de_imagen(numero: int, *, pagina: int | None = None, hoja: str | None = None) -> str:
    """La línea que abre el bloque «Lectura automática» de una imagen embebida en el cuerpo."""
    return f"--- Lectura automática · {ubicacion_de_imagen(numero, pagina=pagina, hoja=hoja)} ---"


def _recortar(texto: str) -> str:
    """Saca las líneas en blanco de los bordes; la sangría de la primera línea es del texto
    (un bloque de código, un poema)."""
    return _LINEAS_EN_BLANCO_INICIALES.sub("", texto).rstrip()


@dataclass(frozen=True)
class CuerpoDeFicha:
    principal: str
    """La transcripción o la descripción, con el texto visible si lo hay."""

    contexto: str
    """Lo que solo sabe una persona. En blanco cuenta como vacío."""

    def __post_init__(self) -> None:
        # `partir` corta en el primer encabezado: si el principal trajera uno, lo que sigue
        # volvería como Contexto atribuido a la persona.
        if _CORTE.search(self.principal):
            raise ValueError(
                f"El cuerpo no puede tener una línea «{_ENCABEZADO}»: el Contexto va aparte."
            )

    def armar(self) -> str:
        principal, contexto = _recortar(self.principal), _recortar(self.contexto)
        if not contexto:
            return principal
        seccion = f"{_ENCABEZADO}\n\n{contexto}"
        return f"{principal}\n\n{seccion}" if principal else seccion

    def partir_texto_visible(self) -> tuple[str, str]:
        """`principal` partido en lo que va antes de `## Texto visible` y el texto visible de
        una foto: `chequeo_inyeccion` revisa cada uno como sección aparte."""
        corte = _CORTE_TEXTO_VISIBLE.search(self.principal)
        if corte is None:
            return self.principal, ""
        antes, despues = self.principal[: corte.start()], self.principal[corte.end() :]
        return _recortar(antes), _recortar(despues)

    @classmethod
    def partir(cls, texto: str) -> CuerpoDeFicha:
        corte = _CORTE.search(texto)
        if corte is None:
            return cls(_recortar(texto), "")
        return cls(_recortar(texto[: corte.start()]), _recortar(texto[corte.end() :]))

    @staticmethod
    def parsear_cuerpo(cuerpo: str) -> list[HojaVista]:
        """Las tablas de una planilla, por hoja (DEC-94): bajo cada marca, la primera línea de
        celdas es el encabezado y la que sigue, la de guiones. Lo que no es celda se ignora, y el
        bloque de una imagen (143) cierra la tabla: su texto no es una fila."""
        tablas: list[tuple[MarcaDeHoja, list[str]]] = []
        en_tabla = False
        for linea in cuerpo.split("\n"):
            if marca := MarcaDeHoja.leer(linea):
                tablas.append((marca, []))
                en_tabla = True
            elif _MARCA_DE_IMAGEN.fullmatch(linea):
                en_tabla = False
            elif en_tabla and linea.lstrip().startswith("|"):
                tablas[-1][1].append(linea)
        return [
            HojaVista(marca, _celdas(lineas[0]) if lineas else [], list(map(_celdas, lineas[2:])))
            for marca, lineas in tablas
        ]

    @staticmethod
    def avisos_de_planilla(cuerpo: str) -> list[str]:
        """Los avisos que `planillas` escribe antes de la primera hoja (topes, hojas ocultas),
        uno por línea: `parsear_cuerpo` no los ve."""
        avisos: list[str] = []
        # Las Afirmaciones aceptadas de sus imágenes también van antes de la primera hoja (148).
        for linea in _BLOQUE_DE_AFIRMACIONES.sub("", cuerpo).split("\n"):
            if MarcaDeHoja.leer(linea):
                break
            if linea.strip():
                avisos.append(linea.strip())
        return avisos


def _celdas(linea: str) -> list[str]:
    """Una línea `| a | b\\|c |`: `\\` escapa el carácter que sigue; lo que va después del
    último `|` no es una celda."""
    celdas: list[str] = []
    actual: list[str] = []
    escapado = False
    for caracter in linea.strip()[1:]:
        if escapado:
            actual.append(caracter)
            escapado = False
        elif caracter == "\\":
            escapado = True
        elif caracter == "|":
            celdas.append("".join(actual).strip())
            actual = []
        else:
            actual.append(caracter)
    return celdas


_MARCA_DE_HOJA = re.compile(r"--- Hoja «(.+)» \(filas (\d+)–(\d+) de (\d+)\)(?: · (.+))? ---")


@dataclass(frozen=True)
class MarcaDeHoja:
    """Abre cada hoja de una planilla en el cuerpo (DEC-74, DEC-112). Es de la gramática de
    marcas, la única que comparten `planillas`, `chequeo_inyeccion` y el visor. Una línea de
    celdas empieza con `|`, así que una celda nunca abre una hoja."""

    nombre: str
    desde: int
    """Las filas de la planilla que siguen en la tabla, desde la de encabezado."""

    hasta: int
    total: int
    anotacion: str | None = None
    """Lo que `planillas` anota en la marca, después de ` · `: el aviso de tope y, en un CSV, la
    codificación y el separador detectados."""

    def a_texto(self) -> str:
        anotacion = f" · {self.anotacion}" if self.anotacion else ""
        return (
            f"--- Hoja «{self.nombre}» (filas {self.desde}–{self.hasta} de {self.total})"
            f"{anotacion} ---"
        )

    @classmethod
    def leer(cls, linea: str) -> MarcaDeHoja | None:
        # Sin el cierre, la anotación recorre la línea entera por cada «» (filas…)» y la lectura
        # se vuelve cuadrática: corre con la Pieza bloqueada.
        if not linea.endswith(" ---"):
            return None
        marca = _MARCA_DE_HOJA.fullmatch(linea)
        if marca is None:
            return None
        nombre, desde, hasta, total, anotacion = marca.groups()
        return cls(nombre, int(desde), int(hasta), int(total), anotacion)


@dataclass(frozen=True)
class HojaVista:
    """Una hoja leída del cuerpo: su marca, el encabezado y las filas, celda por celda."""

    marca: MarcaDeHoja
    columnas: list[str]
    filas: list[list[str]]


@dataclass(frozen=True)
class Segmento:
    """Un tramo del cuerpo que ningún fragmento cruza: una página, una hoja, lo que sigue a una
    marca de tiempo o una sección (DEC-05)."""

    inicio: int
    """Dónde empieza en el cuerpo; `texto == cuerpo[inicio:inicio + len(texto)]`."""

    texto: str
    ubicacion: Mapping[str, object]
    """`seccion` (`principal`, `texto_visible` o `contexto`) y, en el principal, la `pagina` de
    un documento, la `hoja` de una planilla o la `marca` («HH:MM:SS») de un video, más la
    `imagen` del bloque de una imagen embebida (143): con `pza_<id>` es la cita durable (DEC-146
    punto 9)."""


_MARCA_DE_TIEMPO = re.compile(r"\[(\d{2}):([0-5]\d):([0-5]\d)\]")
"""`[HH:MM:SS]`, siempre con horas (DEC-112): la escribe quien valida un video."""


def segmentos(
    cuerpo: str, clase: ClaseDeOriginal = ClaseDeOriginal.DOCUMENTO
) -> tuple[Segmento, ...]:
    """El cuerpo partido en sus páginas, hojas, marcas de tiempo, títulos y secciones, sin
    dejar nada afuera: cada tramo empieza con su marca o su encabezado y la unión de los tramos
    es el cuerpo entero.

    Corta como `CuerpoDeFicha.partir`: en el primer `## Contexto` y, antes de él, en el primer
    `## Texto visible`. Las marcas cuentan solo en el principal y solo las de su clase: la de
    hoja en una planilla y la de tiempo en un video (en un acta, «[10:00:00]» es texto). La de
    una imagen embebida corta en cualquier clase, con la ubicación que lleva escrita. Los
    títulos (`# …`) cortan solo en un documento, sin cambiar la página del tramo.
    """
    contexto = _CORTE.search(cuerpo)
    fin_contexto = len(cuerpo) if contexto is None else contexto.start()
    texto_visible = _CORTE_TEXTO_VISIBLE.search(cuerpo, 0, fin_contexto)
    fin_principal = fin_contexto if texto_visible is None else texto_visible.start()
    cortes: list[tuple[int, dict[str, object]]] = [(0, {"seccion": "principal"})]
    cortes += [
        (marca.start(), {"seccion": "principal", "pagina": int(marca[1])})
        for marca in _MARCA_DE_PAGINA.finditer(cuerpo, 0, fin_principal)
    ]
    if clase is ClaseDeOriginal.PLANILLA:
        cortes += [
            (linea.start(), {"seccion": "principal", "hoja": hoja.nombre})
            for linea in _LINEA_DE_HOJA.finditer(cuerpo, 0, fin_principal)
            if (hoja := MarcaDeHoja.leer(linea[0]))
        ]
    cortes += [
        (marca.start(), _ubicacion_de_la_imagen(marca))
        for marca in _MARCA_DE_IMAGEN.finditer(cuerpo, 0, fin_principal)
    ]
    if clase is ClaseDeOriginal.VIDEO:
        cortes += [
            (marca.start(), {"seccion": "principal", "marca": marca[0][1:-1]})
            for marca in _MARCA_DE_TIEMPO.finditer(cuerpo, 0, fin_principal)
        ]
    cortes.sort(key=lambda corte: corte[0])
    if clase is ClaseDeOriginal.DOCUMENTO:
        cortes = _con_titulos(cuerpo, cortes, fin_principal)
    if texto_visible is not None:
        cortes.append((texto_visible.start(), {"seccion": "texto_visible"}))
    if contexto is not None:
        cortes.append((contexto.start(), {"seccion": "contexto"}))
    fines = [inicio for inicio, _ in cortes[1:]] + [len(cuerpo)]
    return tuple(
        Segmento(inicio, cuerpo[inicio:fin], ubicacion)
        for (inicio, ubicacion), fin in zip(cortes, fines, strict=True)
        if fin > inicio
    )


def _con_titulos(
    cuerpo: str, cortes: list[tuple[int, dict[str, object]]], fin_principal: int
) -> list[tuple[int, dict[str, object]]]:
    """`cortes` más uno en cada título, con la ubicación del tramo que lo contiene (su página):
    el Fragmento no cruza un título (142). No corta si antes solo hay marcas y títulos: un
    título no queda solo ni separado de la marca de su página."""
    fines = [inicio for inicio, _ in cortes[1:]] + [fin_principal]
    resultado: list[tuple[int, dict[str, object]]] = []
    for (inicio, ubicacion), fin in zip(cortes, fines, strict=True):
        resultado.append((inicio, ubicacion))
        for titulo in _TITULO.finditer(cuerpo, inicio, fin):
            antes = cuerpo[resultado[-1][0] : titulo.start()]
            marcas = _MARCA_DE_IMAGEN.sub("", _MARCA_DE_PAGINA.sub("", antes))
            if _TITULO.sub("", marcas).strip():
                resultado.append((titulo.start(), ubicacion))
    return resultado


def _ubicacion_de_la_imagen(marca: re.Match[str]) -> dict[str, object]:
    pagina, hoja, numero = marca.groups()
    ubicacion: dict[str, object] = {"seccion": "principal", "imagen": int(numero)}
    if pagina is not None:
        ubicacion["pagina"] = int(pagina)
    if hoja is not None:
        ubicacion["hoja"] = hoja
    return ubicacion


def segundos_de_marca(marca: str) -> int:
    """Los segundos de una marca de tiempo «HH:MM:SS» (sin corchetes), para el `#t=` del
    enlace al video."""
    horas, minutos, segundos = map(int, marca.split(":"))
    return horas * 3600 + minutos * 60 + segundos
