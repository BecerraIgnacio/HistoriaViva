"""El cruce de las Afirmaciones con la lectura automática y su riesgo (145, ADR-0007).

El OCR no inventa: cada palabra que devuelve está en la imagen. El modelo de visión lee bien
pero inventa. Una Afirmación queda **respaldada** solo si todos sus datos verificables —nombres
propios, siglas, números y fechas— aparecen en las palabras del OCR, y guarda dónde están. Si
falta uno, o si no tiene ninguno, queda **sin respaldo**: las palabras comunes no respaldan.

El cruce es determinista y no llama a ningún modelo. Compara sin tildes, sin mayúsculas y sin
signos, y exige que el dato entero coincida con palabras enteras y seguidas del OCR: «18:30–21:45»
se respalda con «18:30 - 21:45», pero «15» no se respalda con «2015» ni «Universidad Nacional de
Educación a Distancia» con «Universidad de la Defensa Nacional».
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "TIPO_FOTOGRAFIA",
    "MotivoDeConfirmacion",
    "PalabraDeLaLectura",
    "Region",
    "Respaldo",
    "confirmacion_para_aceptar",
    "cruzar",
    "datos_verificables",
    "respaldo",
]

TIPO_FOTOGRAFIA = "Fotografía"
"""La clase de 139 donde las personas se ven y no se leen: nunca tienen respaldo en el OCR."""


class PalabraDeLaLectura(Protocol):
    """Lo que el cruce usa de una `PalabraLeida` (140): el texto y su caja en la imagen."""

    @property
    def texto(self) -> str: ...
    @property
    def izquierda(self) -> int: ...
    @property
    def arriba(self) -> int: ...
    @property
    def ancho(self) -> int: ...
    @property
    def alto(self) -> int: ...


@dataclass(frozen=True)
class Region:
    """Dónde está un dato en la imagen: la caja que junta sus palabras, en píxeles de la imagen
    como la muestra el navegador (EXIF aplicado, 140), y lo que leyó ahí el OCR."""

    texto: str
    izquierda: int
    arriba: int
    ancho: int
    alto: int

    def a_json(self) -> dict[str, object]:
        return dict(vars(self))

    @classmethod
    def de_json(cls, datos: dict[str, object]) -> Region:
        # Ignora claves que ya no existen: los respaldos guardados antes de pony13 traen "pagina".
        campos = {k: v for k, v in datos.items() if k in cls.__dataclass_fields__}
        return cls(**campos)  # type: ignore[arg-type]


Respaldo = tuple[Region, ...]
"""Una región por dato verificable de la Afirmación, en el orden en que aparecen en ella."""


class MotivoDeConfirmacion(StrEnum):
    """Por qué aceptar una Afirmación sin respaldo pide confirmación (Buçinca et al., 2021)."""

    DATOS = "datos"
    """Nombra una sigla, una fecha, un número o un nombre propio que el OCR no leyó."""

    PERSONAS = "personas"
    """Habla de personas en una imagen que no es una foto: ahí una persona inventada es el error
    típico (los íconos del cartel de Cyber.AR)."""


# --- Normalización ----------------------------------------------------------------------------


def _normal(texto: str) -> str:
    """Sin tildes, en minúscula y solo letras y cifras: «Cyber.AR» y «CYBER.AR» dan «cyberar».
    La coma o el punto decimal quedan como «§»: «1,5» no es «15», pero «1.500» es «1500». Los dos
    puntos de una hora quedan como «¤», también sueltos: «20:00» y «20 : 00» no son «2000»."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    con_marcas = _HORA.sub("¤", _DECIMAL.sub("§", sin_tildes.casefold().strip()))
    return re.sub(r"[^a-z0-9§¤]", "", con_marcas)


_DECIMAL = re.compile(r"(?<=\d)[.,](?=\d{1,2}(?!\d))")
"""Una coma o un punto entre cifras con una o dos después: decimal, no separador de miles."""

_HORA = re.compile(r"(?<=\d):(?=\d)|(?<=\d):$|^:(?=\d)|^:$")
"""Los dos puntos de una hora: entre cifras, o al borde de una palabra del OCR que la partió
(«18:» «30», «18» «:» «30»)."""


_FECHAS = frozenset(
    "lunes martes miercoles jueves viernes sabado domingo enero febrero marzo abril mayo junio "
    "julio agosto septiembre setiembre octubre noviembre diciembre".split()
)
"""Días y meses: son parte de una fecha aunque vayan en minúscula."""

_CONECTORES = frozenset("de del la las los el y e a".split())
"""Unen las palabras de un nombre propio: «Facultad de Ingeniería del Ejército»."""

_CONECTORES_DE_FECHA = frozenset(("de", "del"))
"""Unen el día, el mes y el año de una fecha: «15 de marzo de 2026». No se buscan en el OCR."""

_COMIENZOS = frozenset(
    "el la los las un una unos unas en de del con por para hay se es son al y o este esta "
    "estos estas ese esa esos esas su sus lo sobre segun desde entre tiene muestra aparece "
    "abajo arriba debajo encima detras delante junto dos tres cuatro cinco varias varios "
    "algunas algunos otra otro otras otros muchas muchos cada ambos ambas".split()
)
"""Palabras que van con mayúscula solo por abrir la oración: no empiezan un nombre propio."""

_PERSONAS = frozenset(
    "persona personas gente hombre hombres mujer mujeres nino ninos nina ninas chico chicos "
    "chica chicas joven jovenes publico asistente asistentes participante participantes "
    "orador oradores oradora oradoras disertante disertantes alumno alumnos alumna alumnas "
    "estudiante estudiantes cadete cadetes soldado soldados senor senores senora senoras "
    "individuo individuos docente docentes profesor profesores profesora profesoras "
    "autoridad autoridades integrante integrantes funcionario funcionarios".split()
)

_FIN_DE_FRASE = re.compile(r"[.:;!?]$")
_SIGNOS = "«»\"'“”‘’()[]¿¡,.;:!?·=|/—–-"
"""Lo que se le quita a cada palabra de los bordes antes de mirarla."""


def _tiene_mayuscula(palabra: str) -> bool:
    return palabra[:1].isupper()


def _marcada(palabra: str) -> bool:
    """Una palabra que es un dato aunque abra la oración: sigla, mayúscula interna o cifra."""
    letras = [c for c in palabra if c.isalpha()]
    return (
        sum(c.isupper() for c in letras) >= 2
        or any(c.isdigit() for c in palabra)
        or bool(re.search(r"\w\.\w", palabra))
    )


def _de_fecha(palabra: str) -> bool:
    """Un día o un mes («Martes», «septiembre»)."""
    return _normal(palabra) in _FECHAS


def _numero(palabra: str) -> bool:
    return palabra[:1].isdigit()


def datos_verificables(afirmacion: str, *, contar_el_comienzo: bool = True) -> tuple[str, ...]:
    """Los nombres propios (un nombre de varias palabras es un solo dato), las siglas, los
    números, los correos y las fechas de una Afirmación, sin repetir. Un día o un mes junto a su
    número es un solo dato: «Martes 15», «15 de marzo de 2026»; así «Miércoles 15» no se respalda
    con «MIÉRCOLES 16» y «Av. Cabildo 15».

    `contar_el_comienzo`: la palabra con mayúscula que abre la oración (o sigue a «·» o «:») y
    no es una de `_COMIENZOS` cuenta como nombre propio («Macri inaugura…»). Para el respaldo
    cuenta: de más, una respaldada solo puede pasar a sin respaldo. Para decidir si aceptar pide
    confirmación no («Personas vestidas…» no nombra a nadie).
    """
    datos: list[str] = []
    nombre: list[str] = []
    nombre_abre = False  # si el nombre en curso empezó abriendo la oración
    fecha: list[str] = []
    de_pendiente: list[str] = []  # «de» o «del» después de una fecha, por si sigue otra parte
    abre_frase = True

    def cerrar_nombre() -> None:
        while nombre and nombre[-1].casefold() in _CONECTORES:
            nombre.pop()
        # Una sola palabra que abre la oración es un nombre propio solo si se pide o si está
        # marcada.
        if nombre and (
            len(nombre) > 1 or not nombre_abre or contar_el_comienzo or _marcada(nombre[0])
        ):
            datos.append(" ".join(nombre))
        nombre.clear()

    def cerrar_fecha() -> None:
        if fecha:
            datos.append(" ".join(fecha))
        fecha.clear()
        de_pendiente.clear()

    def sumar_a_la_fecha(palabra: str) -> None:
        """El número y el día o mes que van pegados (o unidos por «de») son un dato; dos
        números seguidos no («15 18:30» son el día y la hora)."""
        if fecha and (_de_fecha(palabra) or _de_fecha(fecha[-1])):
            fecha.extend(de_pendiente)
        else:
            cerrar_fecha()
        de_pendiente.clear()
        fecha.append(palabra)

    for cruda in afirmacion.split():
        palabra = cruda.strip(_SIGNOS)
        if not palabra:  # «·», «=», «-»: un separador abre otra frase
            cerrar_nombre()
            cerrar_fecha()
            abre_frase = True
            continue
        if cruda[0] != palabra[0]:  # «(UNDEF)» o «"Cyber.AR"» no siguen el nombre de antes
            cerrar_nombre()
            cerrar_fecha()
        clave = _normal(palabra)
        if _numero(palabra) and nombre and nombre[-1].casefold() not in _CONECTORES:
            # Pegado a un nombre propio es parte de él: «Cabildo 15», «Hackathon Cyber.AR 2026».
            nombre.append(palabra)
            cerrar_nombre()
        elif _de_fecha(palabra) or _numero(palabra):
            cerrar_nombre()
            sumar_a_la_fecha(palabra)
        elif fecha and palabra.casefold() in _CONECTORES_DE_FECHA and not de_pendiente:
            de_pendiente.append(palabra)
        elif _marcada(palabra) and not _tiene_mayuscula(palabra):
            # Lo que se escribe en minúscula pero es un nombre: un correo o una dirección web
            # («info.cyber.ar@fie.undef.edu.ar»).
            cerrar_nombre()
            cerrar_fecha()
            datos.append(palabra)
        elif _tiene_mayuscula(palabra) and not (abre_frase and clave in _COMIENZOS):
            cerrar_fecha()
            if not nombre:
                nombre_abre = abre_frase
            nombre.append(palabra)
        elif nombre and palabra.casefold() in _CONECTORES:
            nombre.append(palabra)
        else:
            cerrar_nombre()
            cerrar_fecha()
        abre_frase = bool(_FIN_DE_FRASE.search(cruda))
        if cruda[-1] != palabra[-1]:  # una coma, un paréntesis o un punto cierran el dato
            cerrar_nombre()
            cerrar_fecha()
    cerrar_nombre()
    cerrar_fecha()
    unicos: dict[str, str] = {}
    for dato in datos:
        unicos.setdefault(_normal(dato), dato)
    unicos.pop("", None)
    return tuple(unicos.values())


# --- Cruce ------------------------------------------------------------------------------------


def _coincidencias(
    dato: str, palabras: Sequence[PalabraDeLaLectura], claves: Sequence[str]
) -> list[Region]:
    """Cada tira de palabras seguidas del OCR que, juntas, dicen exactamente el dato. Una
    palabra que es solo signos («-», «·») puede quedar en el medio, no en los bordes."""
    objetivo = _normal(dato)
    if any(_de_fecha(p) for p in dato.split()):
        objetivo = _normal(" ".join(p for p in dato.split() if p.casefold() not in _CONECTORES_DE_FECHA))
        claves = ["" if c in _CONECTORES_DE_FECHA else c for c in claves]
    regiones: list[Region] = []
    for inicio, clave in enumerate(claves):
        if not clave or not objetivo.startswith(clave):
            continue
        junto = ""
        for fin in range(inicio, len(claves)):
            junto += claves[fin]
            if not objetivo.startswith(junto):
                break
            if junto == objetivo:
                tira = palabras[inicio : fin + 1]
                cajas = [p for p, c in zip(tira, claves[inicio : fin + 1], strict=True) if c]
                izquierda = min(p.izquierda for p in cajas)
                arriba = min(p.arriba for p in cajas)
                regiones.append(
                    Region(
                        texto=" ".join(p.texto for p in tira),
                        izquierda=izquierda,
                        arriba=arriba,
                        ancho=max(p.izquierda + p.ancho for p in cajas) - izquierda,
                        alto=max(p.arriba + p.alto for p in cajas) - arriba,
                    )
                )
                break
    return regiones


def _distancia(a: Region, b: Region) -> float:
    return abs(a.izquierda + a.ancho / 2 - b.izquierda - b.ancho / 2) + abs(
        a.arriba + a.alto / 2 - b.arriba - b.alto / 2
    )


def respaldo(afirmacion: str, palabras: Sequence[PalabraDeLaLectura]) -> Respaldo | None:
    """Las regiones de todos los datos verificables de la Afirmación, o `None` si falta alguno o
    si no tiene ninguno. Si un dato está más de una vez («2026» en el título y en el
    cronograma), va la vez más cercana al dato anterior: así la región es la de la Afirmación."""
    datos = datos_verificables(afirmacion)
    if not datos:
        return None
    claves = [_normal(p.texto) for p in palabras]
    elegidas: list[Region] = []
    for dato in datos:
        candidatas = _coincidencias(dato, palabras, claves)
        if not candidatas:
            return None
        anterior = elegidas[-1] if elegidas else candidatas[0]
        elegidas.append(min(candidatas, key=lambda r: _distancia(r, anterior)))
    return tuple(elegidas)


def cruzar(
    afirmaciones: Sequence[str], palabras: Sequence[PalabraDeLaLectura]
) -> tuple[Respaldo | None, ...]:
    """El respaldo de cada Afirmación, en su orden."""
    return tuple(respaldo(a, palabras) for a in afirmaciones)


# --- Riesgo -----------------------------------------------------------------------------------


def confirmacion_para_aceptar(
    afirmacion: str, *, respaldada: bool, tipo_imagen: str | None
) -> MotivoDeConfirmacion | None:
    """Si aceptar la Afirmación pide una confirmación explícita, y por qué. La fricción va solo
    donde el riesgo es alto: lo respaldado y lo demás es un clic."""
    if respaldada:
        return None
    if datos_verificables(afirmacion, contar_el_comienzo=False):
        return MotivoDeConfirmacion.DATOS
    if tipo_imagen != TIPO_FOTOGRAFIA and any(
        _normal(p) in _PERSONAS for p in afirmacion.split()
    ):
        return MotivoDeConfirmacion.PERSONAS
    return None
