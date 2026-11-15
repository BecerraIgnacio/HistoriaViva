"""Contrato de la capa de modelos: cinco capacidades, una sola puerta al motor (RNF-01).

**Ninguna llamada al motor de modelos existe fuera de esta capa.** Ni el procesamiento, ni
la busqueda, ni el banco de pruebas construyen una entrada ni abren un cliente HTTP hacia
el motor. Si una llamada se filtra, el sistema sigue funcionando y deja de ser modular sin
que ninguna prueba se rompa (`arquitectura.md` §3.2).

## Por que las firmas tienen esta forma

ADR-0003 capa 1, la unica capa que funciona sin que nadie este prestando atencion:

    El canal de instrucciones se arma **exclusivamente** con constantes del codigo. El
    contenido del Acervo entra **siempre** por el canal de datos, dentro de un bloque
    delimitado.

Aca eso no es una advertencia en un comentario: es la firma. Ningun metodo de `CapaModelos`
tiene un parametro `instruccion: str` ni nada que acepte una instruccion compuesta en
tiempo de ejecucion. La plantilla se elige por **identificador** —un miembro de `Plantilla`,
que es un enum cerrado— y el contenido del Acervo solo puede entrar como `BloqueDatos` o
`BloqueBinario`. No existe una llamada valida que concatene un acta al canal de
instrucciones, porque no hay parametro donde ponerla.

Esto importa incluso sin atacante. Un acta legitima dice "se resolvio comunicar que el
convenio queda sin efecto"; concatenada al canal de instrucciones, esa frase es una orden.

## Que se implementa

`describir_imagen`, `transcribir`, `resumir`, `embeber` (045, en el segundo Ollama),
`responder_en_vivo` (052, la redaccion de la Conversacion en streaming, R-20) y `reformular`
(054, la pregunta de seguimiento con el historial, R-10). `responder` sin streaming junta la
salida de `responder_en_vivo` (078, el banco de `responder`): la misma instrucción y la misma
fila que la Conversación.
"""

from __future__ import annotations

from collections.abc import Collection, Generator, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from historia_viva.capa_modelos.limites import Capacidad, Limites
from historia_viva.dominio.afirmaciones import UbicacionDeImagen
from historia_viva.dominio.errores import MotorInterrumpido

if TYPE_CHECKING:  # `configuracion` importa este módulo
    from historia_viva.capa_modelos.configuracion import ConfiguracionEfectiva

__all__ = [
    "ETIQUETA_CONSULTA",
    "ETIQUETA_CONTEXTO_LOTE",
    "ETIQUETA_CUERPO_FICHA",
    "ETIQUETA_DOCUMENTO",
    "ETIQUETA_FICHA",
    "ETIQUETA_FRAGMENTO",
    "ETIQUETA_HISTORIAL",
    "ETIQUETA_IMAGEN",
    "ETIQUETA_IMAGEN_OCR",
    "BloqueBinario",
    "BloqueDatos",
    "CapaModelos",
    "Capacidad",
    "Invocacion",
    "Limites",
    "PalabraLeida",
    "Plantilla",
    "ResultadoDescripcion",
    "ResultadoTexto",
    "ResultadoTranscripcion",
    "ResultadoVectores",
    "TramoDeRespuesta",
    "UsoEmbedding",
    "juntar",
]

ETIQUETA_DOCUMENTO = "documento"
"""Texto extraido de un PDF o un DOCX."""

ETIQUETA_IMAGEN_OCR = "imagen_ocr"
"""Texto visible transcripto de una fotografia."""

ETIQUETA_CUERPO_FICHA = "cuerpo_ficha"
"""El cuerpo de la Ficha: transcripcion o descripcion mas el Contexto de una persona."""

ETIQUETA_CONTEXTO_LOTE = "contexto_lote"
"""El Contexto común del Lote, que declaró una persona: solo lo recibe `resumir` (DEC-19)."""

ETIQUETA_IMAGEN = "imagen"
"""Los bytes de una imagen. Solo aparece en `Invocacion.bloques` como registro de
auditoria, con el contenido reemplazado por su huella: el binario no se copia al registro."""

ETIQUETA_CONSULTA = "consulta"
"""La pregunta que escribio una persona. Es entrada de usuario, no del Acervo, y por el
mismo motivo va por el canal de datos: tampoco es una instruccion."""

ETIQUETA_FRAGMENTO = "fragmento"
"""Un fragmento recuperado del Acervo para redactar. El constructor de entradas los numera
(`<fragmento n="1">`): el numero lo pone el codigo, nunca el contenido."""

ETIQUETA_FICHA = "ficha"
"""Una Ficha fuente de una efemeride (fecha, tipo y resumen). Numerada como los fragmentos
(`<ficha n="1">`): el `[n]` del texto es la Ficha `n`."""

ETIQUETA_HISTORIAL = "historial"
"""Los turnos anteriores de una Conversacion, armados por el servidor desde la base: el cliente
no puede forjarlos (R-18). Tambien los del modelo son datos, no instrucciones."""


class Plantilla(StrEnum):
    """Identificadores de las plantillas del canal de instrucciones.

    **Un identificador, nunca el texto.** El texto de cada plantilla es una constante que
    vive en el implementador de `CapaModelos` y nadie fuera de el la ve, la compone ni la
    interpola. Que este parametro sea un enum cerrado es lo que hace imposible pasar una
    instruccion armada en tiempo de ejecucion: no existe el valor que la representaria.
    """

    DESCRIBIR_FOTOGRAFIA = "describir_fotografia"
    """Describir una fotografia del Acervo en espanol, para el cuerpo de la Ficha. La primera
    instruccion de `describir_imagen`: la linea de base del banco de ingesta (139)."""

    DESCRIBIR_IMAGEN_POR_CLASE = "describir_imagen_por_clase"
    """Clasificar la imagen (fotografia, cartel o grafico, pagina, otra) y describirla segun su
    clase, sin expandir siglas ni nombrar lo que no esta escrito (139)."""

    DESCRIBIR_IMAGEN_EN_AFIRMACIONES = "describir_imagen_en_afirmaciones"
    """La de 139 con salida estructurada: la clase, una lista de Afirmaciones y lo que no se
    puede determinar, con un esquema JSON en Ollama (`format`) (144, ADR-0007)."""

    TRANSCRIBIR_DOCUMENTO = "transcribir_documento"
    """Extraer el texto integro de un PDF o un DOCX, con marca de pagina."""

    TRANSCRIBIR_IMAGEN = "transcribir_imagen"
    """OCR del texto visible en una fotografia."""

    RESUMIR_FICHA = "resumir_ficha"
    """Resumen de 3 a 5 oraciones del cuerpo de la Ficha, situado con el Contexto del Lote si
    la Pieza es de uno (DEC-19)."""

    EMBEBER_FRAGMENTO = "embeber_fragmento"
    """Vectorizar un fragmento de Ficha para el indice."""

    EMBEBER_CONSULTA = "embeber_consulta"
    """Vectorizar la consulta de una persona."""

    RESPONDER_SOBRE_FRAGMENTOS = "responder_sobre_fragmentos"
    """Redactar un parrafo sobre los fragmentos recuperados, debajo de la lista."""

    RESPONDER_EN_CONVERSACION = "responder_en_conversacion"
    """Responder la pregunta de una Conversacion en texto plano, con Citas `[n]` a los
    fragmentos, o declarar en forma fija que no alcanzan (052)."""

    REDACTAR_EFEMERIDE_DIA = "redactar_efemeride_dia"
    """Efeméride de un día y mes sobre las Fichas de ese día en distintos años, con Citas `[n]`
    a las Fichas (057, R-10)."""

    REDACTAR_EFEMERIDE_MES = "redactar_efemeride_mes"
    """Efeméride de un mes sobre las Fichas con ese mes conocido en distintos años, con Citas
    `[n]` a las Fichas (059, R-10)."""

    REFORMULAR_CON_HISTORIAL = "reformular_con_historial"
    """Reescribir la pregunta de seguimiento de una Conversacion como una pregunta autonoma, con
    el historial. Corre con la fila de `responder` (R-10, 054)."""


class UsoEmbedding(StrEnum):
    """Para qué se embebe un texto: elige el prefijo de la fila (`prefijo_<uso>`)."""

    FRAGMENTO = "fragmento"
    """Un fragmento de Ficha que va al índice."""

    CONSULTA = "consulta"
    """La consulta de una persona, que se compara con los fragmentos."""


@dataclass(frozen=True)
class BloqueDatos:
    """Contenido del Acervo o entrada de una persona, por el canal de datos.

    Es la unica forma en que texto no confiable entra a una invocacion. La etiqueta le dice
    al implementador como delimitarlo y como escapar el delimitador; no es decoracion.
    """

    etiqueta: str
    """Una de las constantes `ETIQUETA_*` de este modulo."""

    contenido: str
    """El texto, tal cual. No se interpola en ninguna plantilla."""


@dataclass(frozen=True)
class BloqueBinario:
    """Un archivo del Acervo por el canal de datos: imagen, PDF o DOCX.

    Existe porque `describir_imagen` y `transcribir` reciben bytes, no texto, y forzarlos a
    `BloqueDatos.contenido: str` obligaria a codificarlos en la misma cadena por la que
    podria colarse una instruccion. Separar los tipos mantiene la garantia estructural.
    """

    etiqueta: str
    """`ETIQUETA_IMAGEN` o `ETIQUETA_DOCUMENTO`."""

    contenido: bytes
    tipo_mime: str
    """`image/jpeg`, `image/png`, `application/pdf` o el MIME de DOCX."""

    nombre_archivo: str | None = None
    """Solo para diagnostico. **No entra al motor**: un nombre de archivo distorsiona la
    similitud y no aporta Contexto historico (`investigacion/rag.md`, hallazgo 3)."""


@dataclass(frozen=True)
class Invocacion:
    """Lo que registra la capa falsa para poder auditar (ADR-0003, verificacion).

    La suite de la Fase 1 prueba el aislamiento de instrucciones sobre esta estructura: la
    capa de modelos falsa verifica que el contenido del archivo le llego como bloque de
    datos delimitado y nunca como instruccion. Como `plantilla` es un identificador y
    `bloques` es lo unico que transporta contenido, la prueba es una comparacion, no una
    inspeccion de texto.
    """

    capacidad: Capacidad
    plantilla: Plantilla
    """IDENTIFICADOR de plantilla constante, nunca texto compuesto."""

    bloques: tuple[BloqueDatos, ...]
    """Los bloques del canal de datos, en el orden en que se enviaron. Un `BloqueBinario` se
    registra aca como `BloqueDatos` con su huella `sha256:...` en `contenido`: el registro
    documenta que se envio, sin copiar el binario."""

    limites: Limites
    """Los topes con los que se ejecuto. Toda invocacion nace con tope (ADR-0004)."""


@dataclass(frozen=True)
class ResultadoTexto:
    """Salida de una capacidad que produce texto."""

    texto: str
    invocacion: Invocacion
    """Con que se invoco al motor. Es lo que hace auditable a la capa."""

    duracion_ms: int
    tokens_entrada: int = 0
    tokens_salida: int = 0
    """Medidos, sin tope: el tope es por llamada (`limites`)."""


@dataclass(frozen=True)
class PalabraLeida:
    """Una palabra que el OCR leyó y llegó al cuerpo (140). La usa el cruce con las
    Afirmaciones (145)."""

    texto: str
    izquierda: int
    arriba: int
    ancho: int
    alto: int
    """En píxeles de la foto leída, con la rotación del EXIF aplicada (como la muestra el
    navegador). Solo de una foto: el cruce con las Afirmaciones corre solo ahí (145)."""

    confianza: float
    """De 0 a 100, la que da el motor."""


@dataclass(frozen=True)
class ResultadoDescripcion(ResultadoTexto):
    """Salida de `describir_imagen`. Con una instrucción de esquema (144) trae la clase y las
    Afirmaciones, y `texto` las junta una por línea, debajo de la clase (lo que mide el banco);
    con una de prosa, `tipo_imagen` es `None` y `texto` es la descripción."""

    tipo_imagen: str | None = None
    afirmaciones: tuple[str, ...] = ()
    no_determinable: tuple[str, ...] = ()
    """Lo que el modelo dice que no se puede saber por la imagen: no es una Afirmación."""


@dataclass(frozen=True)
class ResultadoTranscripcion:
    """Salida de `transcribir` (DEC-146 punto 7). No es un modelo de lenguaje: no hay tokens."""

    texto: str
    """En UTF-8 NFC y sin caracteres de control salvo salto de línea y tabulación (R-36)."""

    avisos: tuple[str, ...]
    truncada: bool
    digest: str | None
    """Lo que recibe `resumir` de una planilla; `None` en lo demás."""

    configuracion_efectiva: ConfiguracionEfectiva
    """La fila de `transcribir` con la que se leyó."""

    palabras: tuple[PalabraLeida, ...] = ()
    """Las del OCR de una foto, con su lugar y su confianza; vacío en lo demás."""

    imagenes_sin_texto: tuple[UbicacionDeImagen, ...] = ()
    """Las imágenes embebidas que el OCR leyó con casi nada de texto (una foto, un gráfico): las
    describe el modelo de visión (148)."""


@dataclass(frozen=True)
class ResultadoVectores:
    """Salida de `embeber`: un vector por texto, en el mismo orden."""

    vectores: tuple[tuple[float, ...], ...]
    """De la dimension de la fila (hasta 1024); el relleno a 1024 lo hace solo `indice`."""

    modelo: str
    """`modelo@digest12`, con el digest de `/api/tags`. Sin este dato no se sabe si el indice
    es homogeneo tras cambiar de modelo, ni tras un `pull` nuevo del mismo tag."""

    duracion_ms: int


@dataclass(frozen=True)
class TramoDeRespuesta:
    """Un tramo de `responder_en_vivo`: un token, o el cierre con el resultado completo."""

    token: str = ""
    resultado: ResultadoTexto | None = None
    """Solo en el ultimo tramo, que no trae token."""


def juntar(tramos: Iterable[TramoDeRespuesta]) -> ResultadoTexto:
    """El resultado completo de una redaccion en vivo: lo que devuelve `responder`.

    Raises:
        MotorInterrumpido: Si los tramos terminan sin el cierre.
    """
    for tramo in tramos:
        if tramo.resultado is not None:
            return tramo.resultado
    raise MotorInterrumpido("la redacción terminó sin el resultado completo")


class CapaModelos(Protocol):
    """Las cinco capacidades, como funciones de dominio.

    En todos los metodos `limites` admite `None`, y `None` significa los topes de la fila de
    la capacidad en `configuracion_capacidad`, nunca "sin tope": no existe forma de invocar
    sin limite (ADR-0004, limite duro 3). Igual con `plantilla` en `describir_imagen` y
    `resumir`: `None` es la instruccion de la fila (RF-32).

    Ningun metodo recibe una instruccion. Si algun implementador agregara un parametro que
    aceptara texto libre en el canal de instrucciones, romperia ADR-0003 capa 1 y este
    Protocol dejaria de describirlo.
    """

    def describir_imagen(
        self,
        *,
        imagen: BloqueBinario,
        plantilla: Plantilla | None = None,
        limites: Limites | None = None,
    ) -> ResultadoDescripcion:
        """Propone una descripcion en espanol de una fotografia (user story 7): con la
        instruccion de esquema, como Afirmaciones (144).

        Raises:
            LimiteExcedido: Si supera su tope de tiempo o de tokens.
            DescripcionFueraDeEsquema: Si la instruccion pide esquema y la respuesta no lo
                cumple.
        """
        ...

    def transcribir(
        self,
        *,
        archivo: BloqueBinario,
        plantilla: Plantilla = Plantilla.TRANSCRIBIR_DOCUMENTO,
        limites: Limites | None = None,
    ) -> ResultadoTranscripcion:
        """Extrae el texto integro de un documento, o el texto visible de una imagen.

        No es un LLM (`arquitectura.md` §7.4). Vive detras de esta interfaz para que
        sustituirlo por un modelo el dia que haya GPU sea un cambio de configuracion.

        Raises:
            LimiteExcedido: Si supera su tope de tiempo o de tamano.
        """
        ...

    def resumir(
        self,
        *,
        cuerpo: BloqueDatos,
        contexto: BloqueDatos | None = None,
        plantilla: Plantilla | None = None,
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        """Propone el resumen de 3 a 5 oraciones del frontmatter (user story 8).

        Args:
            cuerpo: El cuerpo de la Ficha, con etiqueta `ETIQUETA_CUERPO_FICHA`.
            contexto: El Contexto del Lote, con etiqueta `ETIQUETA_CONTEXTO_LOTE`; `None` en
                una Pieza suelta.

        Raises:
            LimiteExcedido: Si supera su tope de tiempo o de tokens.
        """
        ...

    def embeber(
        self, *, textos: tuple[BloqueDatos, ...], uso: UsoEmbedding
    ) -> ResultadoVectores:
        """Vectoriza fragmentos de Ficha o una consulta, con el modelo y el prefijo de la fila.

        No recibe instruccion: el modelo de embeddings no la tiene, y el prefijo es un dato de
        la fila, no del Acervo. Ningun texto se trunca (`truncate: false`): uno que no entra
        en el contexto del modelo es un error del motor, no un vector de medio fragmento.

        Raises:
            MotorNoDisponible: Si el motor no responde o el modelo no esta descargado; nunca
                devuelve una lista vacia en su lugar.
            LimiteExcedido: Si un lote supera el `timeout_s` de la fila.
            ConfiguracionInvalida: Si el modelo devuelve otra dimension que la de la fila.
        """
        ...

    def responder(
        self,
        *,
        consulta: BloqueDatos,
        fragmentos: tuple[BloqueDatos, ...],
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        """La respuesta de `responder_en_vivo`, entera, sin streaming: la usa el banco de
        `responder` (078) para medir lo mismo que ve la Conversacion. Sin `plantilla`: la
        instruccion es la de la fila, como en vivo (`RESPONDER_SOBRE_FRAGMENTOS` no tiene
        texto).

        La consulta de la persona entra como `BloqueDatos` por el mismo motivo que el
        Acervo: es entrada no confiable, no una instruccion.

        Raises:
            MotorInterrumpido, MotorNoAlcanzable, LimiteExcedido: Los de `responder_en_vivo`.
        """
        ...

    def responder_en_vivo(
        self,
        *,
        consulta: BloqueDatos,
        fragmentos: tuple[BloqueDatos, ...],
        limites: Limites | None = None,
    ) -> Generator[TramoDeRespuesta]:
        """Redacta la respuesta de un turno de la Conversacion y la entrega a medida que el
        motor la produce, con la instruccion de la fila de `responder` (R-20). Cerrarlo antes
        del final corta la generacion (R-17).

        Args:
            consulta: La pregunta, con etiqueta `ETIQUETA_CONSULTA`.
            fragmentos: Los recuperados, con etiqueta `ETIQUETA_FRAGMENTO`, en el orden en que
                se citan: el primero es `[1]`.

        Raises:
            MotorInterrumpido: Si el motor corta a mitad: despues de los tokens ya entregados.
            MotorNoAlcanzable: Si el pedido no llego al motor.
            LimiteExcedido: Si supera su tope de tiempo o de tokens de salida.
        """
        ...

    def redactar_efemeride(
        self,
        *,
        fichas: tuple[BloqueDatos, ...],
        plantilla: Plantilla | None = None,
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        """Redacta una efemeride sobre las Fichas fuente (057, R-10). `plantilla` elige la
        variante de la familia `REDACTAR_EFEMERIDE`; `None` es la de la fila.

        Args:
            fichas: Con etiqueta `ETIQUETA_FICHA`, en el orden en que se citan: la primera
                es `[1]`.

        Raises:
            MotorNoAlcanzable: Si el pedido no llego al motor.
            LimiteExcedido: Si supera su tope de tiempo o de tokens de salida.
        """
        ...

    def reformular(
        self,
        *,
        pregunta: BloqueDatos,
        historial: BloqueDatos,
        limites: Limites | None = None,
    ) -> ResultadoTexto:
        """La pregunta de seguimiento reescrita como una pregunta autonoma, para recuperar con
        ella. Una llamada corta, sin streaming, con la fila de `responder` y la instruccion
        `REFORMULAR_CON_HISTORIAL`, que no es la de la fila (R-10).

        Args:
            pregunta: La pregunta nueva, con etiqueta `ETIQUETA_CONSULTA`. Va primero: si la
                entrada pasa su tope, se trunca el historial y no la pregunta.
            historial: Los turnos anteriores, con etiqueta `ETIQUETA_HISTORIAL`.

        Raises:
            MotorNoAlcanzable, MotorInterrumpido: Si el motor no responde.
            LimiteExcedido: Si supera su tope de tiempo o de tokens de salida.
        """
        ...

    def verificar_salud(self, capacidades: Collection[Capacidad] | None = None) -> bool:
        """Si responden los motores de `capacidades` (sin ellas, los de todas), sin invocar
        ninguna capacidad.

        La usan `GET /api/salud` para decidir `capa_modelos: sin_respuesta` (ADR-0004,
        interruptor global) y el cortacircuito de cada trabajador, con las capacidades de su
        tipo (DEC-86): una comprobacion liviana y sin tope de tiempo ni de tokens propios.
        """
        ...

    def modelos_faltantes(self) -> tuple[Capacidad, ...]:
        """Las capacidades cuyo modelo no esta descargado en un motor que responde.

        Es un aviso de `GET /api/salud`, no un bloqueo: la aplicacion arranca igual
        (historia 165).
        """
        ...
