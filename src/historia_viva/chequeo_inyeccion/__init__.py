"""Capa 2 de ADR-0003: marca las frases de una versión de Ficha dirigidas al asistente.

Avisa y no bloquea (RF-10, DEC-26): la Ficha con alerta se firma igual. No puede ser un modelo,
porque un detector con modelo es inyectable; es una función pura sobre un catálogo cerrado.
La regla de diseño es detectar el imperativo dirigido al asistente, no el vocabulario: «queda
sin efecto» o «se dispone» no disparan nada.

Lo importa solo `fichas`, dentro de la única función que inserta versiones (`test_arquitectura`).
Compara sobre el texto normalizado (NFKC, sin invisibles, homoglifos plegados, sin mayúsculas ni
tildes, o con las tildes descompuestas para el voseo), en ventanas con solape y también con las
celdas de cada fila de tabla unidas; ubica y cita cada hallazgo sobre el texto como llegó. Sus
patrones no tienen cuantificadores sin tope: corre con la Pieza bloqueada. De `dominio` toma solo la marca de hoja (DEC-146 punto 11).
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import cache
from itertools import accumulate
from typing import Any

from historia_viva.dominio.cuerpo import MarcaDeHoja

__all__ = [
    "CATALOGO",
    "ETIQUETA_DEL_PATRON",
    "ETIQUETAS_DE_CONTROL",
    "TOPE_DE_HALLAZGOS",
    "VERSION_CATALOGO",
    "EtiquetaDeControl",
    "Hallazgo",
    "InformeInyeccion",
    "Modo",
    "Patron",
    "Severidad",
    "revisar",
]

VERSION_CATALOGO = "2026.10.3"
"""`AAAA.MM.n`: sube con cada cambio del catálogo, así un informe viejo se reinterpreta."""

TOPE_DE_HALLAZGOS = 50

ETIQUETAS_DE_CONTROL = ("### Instruction:", "[SYSTEM]", "<|im_start|>", "<|system|>", "[INST]")
"""La lista cerrada de DEC-133. Cada una tiene su patrón `de_control` en el catálogo, que la
reconoce con la misma normalización: mayúsculas, espacios, tildes y homoglifos."""


class Severidad(StrEnum):
    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"
    """Se guarda y se muestra plegada, pero no enciende la alerta."""


_SEVERIDADES = list(Severidad)


class Modo(StrEnum):
    ALERTAR = "alertar"
    """Lo que sube una persona: nunca lanza; un documento histórico legítimo no se pierde."""

    RECHAZAR = "rechazar"
    """El Aporte de un Agente externo: una etiqueta de control lo rechaza (DEC-133), porque el
    agente lo puede reenviar corregido."""


@dataclass(frozen=True)
class Patron:
    clave: str
    """Solo se agregan: un informe guardado nombra al patrón por esta clave."""

    categoria: str
    severidad: Severidad
    descripcion_humana: str
    regex: re.Pattern[str]
    original: bool = False
    """Busca en el texto tal como llegó: la normalización borra lo que busca."""

    tildes: bool = False
    """Busca en el texto normalizado con las tildes descompuestas (NFD): «actuá» es
    `actua\\u0301` y «actúa», `actu\\u0301a`."""

    de_control: bool = False
    """Reconoce una de las `ETIQUETAS_DE_CONTROL`."""

    retirado: bool = False
    """Ya no dispara, pero la clave queda en el catálogo: los informes viejos la nombran."""


@dataclass(frozen=True)
class Hallazgo:
    patron_id: str
    categoria: str
    severidad: Severidad
    seccion: str
    linea: int
    """Desde 1, dentro de su sección."""

    columna: int
    extracto: str
    """Lo que coincidió, hasta 80 caracteres."""

    hoja: str | None = None
    """En una planilla, la de la marca de hoja más cercana hacia arriba (DEC-112)."""

    fila: int | None = None
    """En una fila de tabla bajo una marca de hoja, la fila de la planilla."""


@dataclass(frozen=True)
class InformeInyeccion:
    version_catalogo: str
    hallazgos: tuple[Hallazgo, ...]
    """Los primeros `TOPE_DE_HALLAZGOS` por severidad, sección y línea: si hay uno que alerta,
    está."""

    total: int
    """Los hallazgos antes del tope."""

    @property
    def alerta(self) -> bool:
        return any(h.severidad is not Severidad.BAJA for h in self.hallazgos)

    @property
    def truncado(self) -> bool:
        return self.total > len(self.hallazgos)

    def a_json(self) -> dict[str, Any]:
        """Lo que se guarda por versión en `ficha.informe_inyeccion`."""
        return {
            "version_catalogo": self.version_catalogo,
            "alerta": self.alerta,
            "total": self.total,
            "truncado": self.truncado,
            "hallazgos": [asdict(h) for h in self.hallazgos],
        }

    @classmethod
    def desde_json(cls, d: Mapping[str, Any]) -> InformeInyeccion:
        """Lee lo que dejó `a_json`, de esta versión del catálogo o de una vieja: no consulta el
        catálogo, así un patrón retirado o un informe de 019 (sin `total`) se siguen leyendo."""
        hallazgos = tuple(
            Hallazgo(**{**h, "severidad": Severidad(h["severidad"])}) for h in d["hallazgos"]
        )
        return cls(d["version_catalogo"], hallazgos, d.get("total", len(hallazgos)))


class EtiquetaDeControl(Exception):
    """Un Aporte con etiquetas de control (DEC-133). Dice dónde, nunca el texto: el mensaje
    puede terminar en un registro o en la respuesta al agente.

    No deriva de `ErrorHistoriaViva`: DEC-146 punto 11 deja importar de `dominio` solo la marca
    de hoja. 101 la mapea aparte, a 422 `aporte_rechazado`."""

    def __init__(self, hallazgos: tuple[Hallazgo, ...]) -> None:
        self.hallazgos = hallazgos
        lugares = "; ".join(f"{h.seccion}, línea {h.linea}" for h in hallazgos)
        super().__init__(f"El texto trae etiquetas de control ({lugares}).")


_HOMOGLIFOS = str.maketrans(
    # Cirílico en minúscula y en mayúscula, griego en mayúscula y en minúscula.
    "\u0430\u0435\u0456\u0458\u043e\u0440\u0441\u0443\u0445\u0455\u04bb\u0501\u051b\u051d\u04cf"
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0423\u0425\u0405\u0406\u0408"
    "\u051a\u051c\u04c0"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7"
    "\u03bf\u03b9\u03bd\u03c1\u03b1\u03ba\u03c5\u03c7",
    "aeijopcyxshdqwl" "abekmhopctyxsijqwl" "abezhikmnoptyx" "oivpakux",
)
"""Las letras cirílicas y griegas que se leen como latinas, a la latina que imitan."""

_IMITADORAS = "".join(map(chr, _HOMOGLIFOS))
_LATINAS = "a-zA-Z\u00c0-\u024f"


def _patron(
    clave: str,
    categoria: str,
    severidad: Severidad,
    descripcion: str,
    regex: str,
    *,
    original: bool = False,
    tildes: bool = False,
    de_control: bool = False,
) -> Patron:
    return Patron(
        clave, categoria, severidad, descripcion, re.compile(regex, re.M),
        original=original, tildes=tildes, de_control=de_control,
    )


_ORDEN = "orden_al_asistente"
_ROL = "rol_de_chat"
_INVISIBLE = "caracter_invisible"

_OTRO_ROL = r"(?:\s{0,40},)?\s{1,40}(?:sos|eres|seras)\s{1,40}(?:un|una|el|la|mi)\b"
_ACTUAR_COMO = r"\s{1,40}(?:actuar|comportarte)\s{1,40}como\b"
# La última parte, solo letras: «1.5» no es un dominio.
_DOMINIO = r"[a-z0-9-]{1,30}(?:\.[a-z0-9-]{1,30}){0,3}\.[a-z]{1,30}"

CATALOGO: tuple[Patron, ...] = (
    # Salvo los `original` y los `tildes`, se escriben sobre el texto normalizado: sin mayúsculas
    # ni tildes. Cada alternativa empieza con un literal o con `^`, y no con `\b`: así `re` salta
    # de candidato en candidato en vez de probar el patrón entero en cada posición.
    _patron(
        "ignorar_instrucciones", _ORDEN, Severidad.ALTA,
        "Pide ignorar las instrucciones anteriores",
        r"(?:ignor[ae]|olvid[ae]|descart[ae]|omiti|pasa por alto|deja sin efecto)\s{1,40}"
        r"(?:todas\s{1,40})?(?:las|tus|sus)\s{1,40}"
        r"(?:instrucciones|indicaciones|reglas|ordenes)"
        r"\s{1,40}(?:anteriores|previas|de arriba|del sistema)",
    ),
    _patron(
        "asumir_otro_rol", _ORDEN, Severidad.ALTA,
        "Le asigna al asistente otro rol",
        # Exige el rol: «desde ahora tenés que presentar el DNI» se le dice a una persona.
        # `(?:\s{0,40},)?\s{1,40}` y no `\s{0,40},?\s{1,40}`: dos cuantificadores seguidos
        # sobre los mismos espacios retroceden de más, y esto corre con la Pieza bloqueada.
        f"a partir de ahora{_OTRO_ROL}|desde ahora{_OTRO_ROL}"
        f"|vas a{_ACTUAR_COMO}|tenes que{_ACTUAR_COMO}|debes{_ACTUAR_COMO}"
        r"|comportate\s{1,40}como\b|hace de cuenta que sos\b",
    ),
    _patron(
        "asumir_otro_rol_voseo", _ORDEN, Severidad.ALTA,
        "Le asigna al asistente otro rol con el voseo «actuá como»",
        # El voseo es una orden donde esté y sin artículo: «por favor actuá como administrador».
        # Cualquier tilde sobre la «a» («actuà», «actuâ»), no solo U+0301, aunque también haya
        # una sobre la «u» («actúá») o vengan apiladas. Las clases no se pisan: sigue lineal.
        r"actu[\u0300-\u036f]{0,40}a[\u0300-\u036f]{1,40}\s{1,40}como\b"
        # Sin ninguna tilde puede ser el voseo o «actúa», que lo dice un acta: exige el rol y
        # que no siga a una palabra. Con la tilde en la «u» («actu\u0301a») no dispara.
        r"|actua(?![\u0300-\u036f])(?<![a-z\u0300-\u036f] actua)(?<![a-z\u0300-\u036f]actua)"
        r"\s{1,40}como\s{1,40}(?:un|una|el|la|mi|si)\b",
        tildes=True,
    ),
    _patron(
        "revelar_instrucciones", _ORDEN, Severidad.ALTA,
        "Pide revelar las instrucciones del asistente",
        r"(?:revela|mostra|mostrame|decime|repeti|copia)\s{1,40}(?:tu|tus)\s{1,40}"
        r"(?:prompt|instrucciones|indicaciones)\b",
    ),
    _patron(
        "ignorar_instrucciones_en", _ORDEN, Severidad.ALTA,
        "Pide ignorar las instrucciones anteriores (inglés)",
        r"(?:ignore|disregard|forget)\s{1,40}(?:all\s{1,40})?"
        r"(?:the\s{1,40}|your\s{1,40}|any\s{1,40})?"
        r"(?:previous|prior|above|earlier|preceding)\s{1,40}"
        r"(?:instructions|directions|prompts?|rules)\b",
    ),
    _patron(
        "asumir_otro_rol_en", _ORDEN, Severidad.ALTA,
        "Le asigna al asistente otro rol (inglés)",
        r"from now on,?\s{1,40}you\s{1,40}(?:are|will\s{1,40}be)\s{1,40}(?:an?|the|my)\b"
        r"|act(?<![a-z]act)\s{1,40}as\s{1,40}if\s{1,40}you\b",
    ),
    _patron(
        "revelar_instrucciones_en", _ORDEN, Severidad.ALTA,
        "Pide revelar las instrucciones del asistente (inglés)",
        # «℡» (U+2121) es «tel» en NFKC, pero `_plegar` no expande a más de dos letras.
        r"(?:reveal|show|print|repeat|output|tell me|℡l me)\s{1,40}(?:me\s{1,40})?"
        r"(?:your\s{1,40}(?:system\s{1,40})?(?:prompt|instructions)"
        r"|the\s{1,40}system\s{1,40}prompt)\b",
    ),
    _patron(
        "rol_sys", _ROL, Severidad.ALTA, "Marca de rol de Llama", r"<<\s{0,40}/?sys\s{0,40}>>"
    ),
    _patron(
        "rol_inst", _ROL, Severidad.ALTA, "Marca de turno de Llama",
        r"\[\s{0,40}inst\s{0,40}\]", de_control=True,
    ),
    # Los cierres alertan igual, pero no están en la lista cerrada de DEC-133: no rechazan.
    _patron(
        "rol_inst_cierre", _ROL, Severidad.ALTA, "Cierre de turno de Llama",
        r"\[\s{0,40}/\s{0,40}inst\s{0,40}\]",
    ),
    # `\\?\|`: en una celda de planilla el `|` va escapado y el modelo lo lee igual.
    _patron(
        "rol_im_start", _ROL, Severidad.ALTA, "Marca de turno de ChatML",
        r"<\\?\|\s{0,40}im[ _]?start\s{0,40}\\?\|>", de_control=True,
    ),
    _patron(
        "rol_im_end", _ROL, Severidad.ALTA, "Cierre de turno de ChatML",
        r"<\\?\|\s{0,40}im[ _]?end\s{0,40}\\?\|>",
    ),
    _patron(
        "rol_instruction", _ROL, Severidad.ALTA, "Encabezado de instrucción de Alpaca",
        # De dos a seis `#`.
        r"###{0,4}\s{0,40}instruc(?:tion|cion)\s{0,40}:", de_control=True,
    ),
    _patron(
        "rol_system_corchetes", _ROL, Severidad.ALTA, "Marca de turno system entre corchetes",
        r"\[\s{0,40}system\s{0,40}\]", de_control=True,
    ),
    _patron(
        "rol_system_chatml", _ROL, Severidad.ALTA, "Marca de turno system de ChatML",
        r"<\\?\|\s{0,40}system\s{0,40}\\?\|>", de_control=True,
    ),
    # Al principio de la línea, con sangría o sin ella: «Operating system: Windows 95» no abre
    # un turno.
    _patron(
        "rol_system", _ROL, Severidad.MEDIA, "Abre un turno system",
        r"^[ \t]{0,40}system[ \t]{0,40}:",
    ),
    _patron(
        "rol_sistema", _ROL, Severidad.BAJA, "Empieza con «Sistema:»",
        r"^[ \t]{0,40}sistema[ \t]{0,40}:",
    ),
    _patron(
        "html_activo", "html_activo", Severidad.ALTA, "HTML o JavaScript que se ejecuta",
        r"<\s{0,40}(?:script|iframe|object|embed)\b|javascript:(?!\s)"
        r"|on(?:error|load|click|mouseover)\s{0,40}=",
    ),
    _patron(
        "imagen_remota", "enlace_markdown", Severidad.ALTA,
        "Imagen remota en markdown: puede sacar datos al cargarse",
        # `[^\[\]\n]` y no `[^\]\n]`: con 1 MB de `[` cada candidato recorría 200 caracteres.
        r"!\[[^\[\]\n]{0,200}\]\(\s{0,40}(?:https?:)?//",
    ),
    _patron(
        "etiqueta_del_constructor", "etiqueta_del_constructor", Severidad.ALTA,
        "Abre o cierra una etiqueta de datos del Constructor (ADR-0003, R-18)",
        # Sin texto libre adentro: «<fragmento ilegible>» es una convención de transcripción.
        r"<\s{0,40}/?\s{0,40}(?:documento|imagen_ocr|cuerpo_ficha|imagen|consulta|contexto_lote"
        r"|fragmento|ficha|historial)\s{0,40}/?\s{0,40}>",
    ),
    _patron(
        "enlace_enganoso", "enlace_markdown", Severidad.MEDIA,
        "Enlace que muestra un dominio y lleva a otro: usuario en la URL, punycode, una IP o un "
        "texto que es un dominio distinto del destino",
        # `(?<!!\[)`: una imagen remota ya es la alta.
        r"\[(?<!!\[)[^\[\]\n]{0,200}\]\(\s{0,40}(?:https?:)?//"
        r"(?:[^\s/()@]{0,100}@|(?:[^\s/()]{0,100}\.)?xn--|\d{1,3}(?:\.\d{1,3}){3}\b)"
        # El texto del enlace es un dominio y el destino empieza con otro. `(?=[a-z0-9])`: sin
        # eso, con 1 MB de `[` cada uno arma los grupos opcionales antes de fallar. `(?!www\.)`:
        # que el `www.` del texto no vuelva a probarse como parte del dominio.
        r"|\[(?<!!\[)(?=[a-z0-9])(?:https?://)?(?:www\.)?(?!www\.)"
        rf"(?P<dominio>{_DOMINIO})(?:/[^\s\[\]()]{{0,60}})?\]"
        r"\(\s{0,40}(?:https?:)?//(?!(?:www\.)?(?P=dominio)[/:?#)\s])",
    ),
    _patron(
        "enlace", "enlace_markdown", Severidad.BAJA, "Enlace en markdown",
        r"\[(?<!!\[)[^\[\]\n]{0,200}\]\(",
    ),
    _patron(
        "caracter_bidi", _INVISIBLE, Severidad.ALTA,
        "Control bidireccional o tag Unicode: muestra un texto y el modelo lee otro",
        r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]",
        original=True,
    ),
    _patron(
        "caracter_de_ancho_cero", _INVISIBLE, Severidad.MEDIA,
        "Carácter invisible de ancho cero",
        # Sin el guion condicional (U+00AD): un PDF partido en sílabas lo trae en cada línea.
        r"[\u180e\u200b-\u200d\u2060-\u2064\ufeff]",
        original=True,
    ),
    _patron(
        "homoglifo", "homoglifo", Severidad.MEDIA,
        "Letra cirílica o griega que imita a una latina dentro de una palabra",
        f"[{_IMITADORAS}](?:(?<=[{_LATINAS}][{_IMITADORAS}])|(?=[{_LATINAS}]))",
        original=True,
    ),
    _patron(
        "formula_de_planilla", "formula_de_planilla", Severidad.MEDIA,
        "Celda que empieza como una fórmula: al abrir la planilla, se ejecuta o sale a la red",
        # `=HYPERLINK(`, `@SUM(`, `=DDE(` o `=cmd|`: el `|` de una celda va escapado.
        r"\|(?<!\\\|)[ \t]{0,40}"
        r"(?:[=@][a-z_][a-z0-9_.]{0,40}\(|[=@+-][ \t]{0,40}cmd[ \t]{0,40}\\?\|)",
    ),
    _patron(
        "base64_largo", "base64_largo", Severidad.MEDIA,
        "Bloque largo en base64: el modelo lo puede decodificar y obedecer",
        r"(?<![a-z0-9+/])[a-z0-9+/]{100}",
    ),
)


ETIQUETA_DEL_PATRON: Mapping[str, str] = {
    "rol_instruction": "### Instruction:",
    "rol_system_corchetes": "[SYSTEM]",
    "rol_im_start": "<|im_start|>",
    "rol_system_chatml": "<|system|>",
    "rol_inst": "[INST]",
}
"""Qué etiqueta de `ETIQUETAS_DE_CONTROL` reconoce cada patrón `de_control`: el rechazo de un
Aporte la nombra así, y no por lo que escribió el agente (101)."""


def _plegar(caracter: str) -> tuple[str, str]:
    """Lo que queda de un carácter para comparar (NFKC, homoglifo a latino, sin mayúsculas):
    sin tildes y con las tildes descompuestas, así se indexa con `tildes`. Un invisible (`Cf`:
    ancho cero, bidi, tags) no deja nada."""
    if unicodedata.category(caracter) == "Cf":
        return "", ""
    plegado = unicodedata.normalize("NFKC", caracter).translate(_HOMOGLIFOS).casefold()
    if len(plegado) > 2:
        # «1⁄2», «ffi», «(10)» o una palabra entera en otra escritura (U+FDFA da 18): ningún
        # patrón los busca, y expandirlos multiplica lo que recorren todos. Quedan «fi» y «ss».
        return caracter, caracter
    descompuesto = unicodedata.normalize("NFD", plegado)
    return "".join(c for c in descompuesto if not unicodedata.combining(c)), descompuesto


def _normalizar(texto: str, plegados: Mapping[str, str]) -> str:
    """`texto` con cada carácter plegado; `plegados` trae los de `texto`."""
    if texto.isascii():
        return texto.lower()
    if all(len(plegado) <= 1 for plegado in plegados.values()):
        return texto.translate({ord(c): plegado for c, plegado in plegados.items()})
    # `join` y no `translate`: con un carácter que se expande (½ da «1⁄2»), `translate` es
    # cuadrático.
    return "".join(map(plegados.__getitem__, texto))


def _fines(texto: str, plegados: Mapping[str, str]) -> Sequence[int]:
    """Por cada carácter de `texto`, dónde termina en el normalizado: así un hallazgo se ubica
    y se cita sobre lo que escribió la persona. Se calcula solo si hay un hallazgo."""
    if all(len(plegado) == 1 for plegado in plegados.values()):
        return range(1, len(texto) + 1)
    # Cada carácter a un byte con su largo: recorrer en Python un millón de caracteres fuera de
    # Latin-1 crea un objeto por cada uno.
    largos = texto.translate({ord(c): chr(len(plegado)) for c, plegado in plegados.items()})
    return list(accumulate(largos.encode("latin-1")))


_PICTOGRAMAS = (
    "\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21ff\u2300-\u23ff\u24c2\u25a0-\u27bf"
    "\u2900-\u297f\u2b00-\u2bff\u3030\u303d\u3297\u3299\U0001f000-\U0001faff"
)
"""shortcut: rangos que cubren con holgura `Extended_Pictographic` y los tonos de piel (`re` no
tiene `\\p{...}`); afinarlos si un invisible entre dos de estos llega a esconder algo."""

_EMOJI_COMPUESTO = re.compile(
    # La unión de ancho cero entre dos pictogramas (mujer + birrete, bandera + arcoíris) y la
    # bandera de una subdivisión (U+1F3F4, región y subdivisión en tags, cancelación) arman un
    # emoji: no esconden texto.
    f"\u200d(?<=[{_PICTOGRAMAS}\ufe0f]\u200d)(?=[{_PICTOGRAMAS}])"
    # Solo las tres que recomienda Unicode (Inglaterra, Escocia, Gales): con cualquier región
    # y hasta cuatro tags, cinco banderas seguidas esconden «ignore all previous instructions».
    "|\U0001f3f4(?:"
    + "|".join("".join(chr(0xE0000 + ord(c)) for c in rgi) for rgi in ("gbeng", "gbsct", "gbwls"))
    + ")\U000e007f"
)


def _sin_emojis_compuestos(texto: str) -> str:
    """El texto con los invisibles de un emoji compuesto en blanco, del mismo largo: así los
    patrones `original` no los toman por texto escondido y una posición vale en los dos."""
    if "\u200d" not in texto and "\U0001f3f4" not in texto:
        return texto
    return _EMOJI_COMPUESTO.sub(lambda emoji: " " * len(emoji[0]), texto)


_VENTANA = 4000
_SOLAPE = 500
"""Más que lo que mira el patrón de más alcance, contando lo que anticipa (`test_chequeo_inyeccion`
lo fija)."""


def _coincidencias(regex: re.Pattern[str], texto: str) -> Iterator[re.Match[str]]:
    """La primera coincidencia de cada línea en cada ventana. Las ventanas acotan lo que un
    patrón recorre en una línea de 1 MB. Lo que empieza en el solape lo ve entero la ventana
    siguiente: acá saldría truncado, y una anticipación negativa dispararía de más."""
    for inicio in range(0, len(texto), _VENTANA - _SOLAPE):
        fin = min(inicio + _VENTANA, len(texto))
        tope = fin if fin == len(texto) else fin - _SOLAPE
        posicion = inicio
        while (coincidencia := regex.search(texto, posicion, fin)) and coincidencia.start() < tope:
            yield coincidencia
            posicion = texto.find("\n", coincidencia.start(), fin) + 1
            if not posicion:
                break
        if fin == len(texto):
            break


_FILA_DE_TABLA = re.compile(r"^[ \t]*\|.*$", re.M)
_SEPARADOR_DE_CELDAS = re.compile(r"(?<!\\)\|")
_LINEA_DE_GUIONES = re.compile(r"[ \t|:-]*")


def _aplanar(texto: str) -> str:
    """Cada fila de tabla con las celdas separadas por espacios: una frase partida entre celdas
    pasa un patrón por línea. Del mismo largo, así una posición vale en los dos."""
    if "|" not in texto:
        return texto
    return _FILA_DE_TABLA.sub(lambda fila: _SEPARADOR_DE_CELDAS.sub(" ", fila[0]), texto)


def _hojas(texto: str) -> dict[int, tuple[str, int | None]]:
    """Por línea, la hoja de la marca más cercana hacia arriba y, en una fila de tabla, la fila
    de la planilla: la de encabezado es `desde` y la de guiones no cuenta (DEC-112)."""
    ubicaciones: dict[int, tuple[str, int | None]] = {}
    hoja, fila = None, 0
    for numero, linea in enumerate(texto.split("\n"), start=1):
        if marca := MarcaDeHoja.leer(linea):
            hoja, fila = marca, marca.desde
        elif hoja is None:
            continue
        elif _FILA_DE_TABLA.fullmatch(linea) and not (
            "-" in linea and _LINEA_DE_GUIONES.fullmatch(linea)
        ):
            ubicaciones[numero] = (hoja.nombre, fila)
            fila += 1
        else:
            ubicaciones[numero] = (hoja.nombre, None)
    return ubicaciones


def _hallazgos(seccion: str, texto: str) -> Iterator[Hallazgo]:
    # Un `set` y un `_plegar` por carácter distinto, para las dos formas: con 1 MB fuera de
    # Latin-1 es un millón de cada uno.
    pares = cache(lambda: {c: _plegar(c) for c in set(texto)})
    plegados = cache(lambda tildes: {c: par[tildes] for c, par in pares().items()})

    @cache
    def buscados(tildes: bool) -> list[str]:
        if tildes and plegados(True) == plegados(False):
            return buscados(False)  # Sin tildes en el texto, las dos formas son la misma.
        normalizado = _normalizar(texto, plegados(tildes))
        aplanado = _aplanar(normalizado)
        return [normalizado] if aplanado == normalizado else [normalizado, aplanado]

    fines = cache(lambda tildes: _fines(texto, plegados(tildes)))
    original = cache(lambda: _sin_emojis_compuestos(texto))
    inicios_de_linea = [0, *(salto.end() for salto in re.finditer("\n", texto))]
    hojas = _hojas(texto) if "--- Hoja «" in texto else {}
    vistos = set()
    # De la más grave a la más leve: en un mismo lugar, una categoría da un solo hallazgo, el más
    # grave (un enlace engañoso no es además un `enlace` baja).
    for patron in sorted(
        (p for p in CATALOGO if not p.retirado), key=lambda p: _SEVERIDADES.index(p.severidad)
    ):
        textos = [original()] if patron.original else buscados(patron.tildes)
        for coincidencia in (c for b in textos for c in _coincidencias(patron.regex, b)):
            if patron.original:
                inicio, fin = coincidencia.span()
            else:
                ubicacion = fines(patron.tildes)
                inicio = bisect_right(ubicacion, coincidencia.start())
                fin = bisect_right(ubicacion, coincidencia.end() - 1) + 1
            if (patron.categoria, inicio) in vistos:
                continue
            vistos.add((patron.categoria, inicio))
            linea = bisect_right(inicios_de_linea, inicio)
            yield Hallazgo(
                patron.clave, patron.categoria, patron.severidad, seccion, linea,
                inicio - inicios_de_linea[linea - 1] + 1, _extracto(texto[inicio:fin]),
                *hojas.get(linea, (None, None)),
            )


def _extracto(coincidencia: str) -> str:
    """Hasta 80 caracteres, con cada invisible o control nombrado (`U+202E`): el extracto se
    guarda y se muestra, y un bidi crudo daría vuelta la pantalla que lo muestra."""
    return "".join(
        f"U+{ord(c):04X}" if unicodedata.category(c) in ("Cc", "Cf", "Cn") else c
        for c in coincidencia
    )[:80]


def revisar(
    secciones: Mapping[str, str], *, modo: Modo = Modo.ALERTAR
) -> InformeInyeccion:
    """Revisa cada sección por separado (`cuerpo`, `texto_visible`, `contexto`, `resumen`,
    `origen`) y ubica cada hallazgo por línea y columna. Línea es la del editor: corta solo en
    `\\n`, no en los `\\f` de un PDF que `splitlines` también toma como salto.

    Raises:
        EtiquetaDeControl: Solo con `Modo.RECHAZAR`, si hay una etiqueta de control.
    """
    orden = {seccion: posicion for posicion, seccion in enumerate(secciones)}
    hallazgos = sorted(
        (h for seccion, texto in secciones.items() for h in _hallazgos(seccion, texto)),
        key=lambda h: (_SEVERIDADES.index(h.severidad), orden[h.seccion], h.linea, h.columna),
    )
    de_control = {patron.clave for patron in CATALOGO if patron.de_control}
    if modo is Modo.RECHAZAR and (
        con_etiqueta := tuple(h for h in hallazgos if h.patron_id in de_control)
    ):
        raise EtiquetaDeControl(con_etiqueta)
    return InformeInyeccion(VERSION_CATALOGO, tuple(hallazgos[:TOPE_DE_HALLAZGOS]), len(hallazgos))
