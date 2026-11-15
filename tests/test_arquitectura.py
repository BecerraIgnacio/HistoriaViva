"""Fronteras entre módulos (historia 212, DEC-71): cada uno es la única puerta a lo suyo.

Un ticket suma una regla agregando una `Regla` a `REGLAS`, con el requisito que protege y un
violador real; las reglas que ya están no se tocan. Cada regla corre contra el código y contra
su violador, y un módulo permitido que no existe hace fallar la suite: así una regla no queda
pasando en vano porque el módulo cambió de nombre.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_fichas import PROYECCION

pytestmark = pytest.mark.historia(212)

PAQUETE = Path(__file__).resolve().parent.parent / "src" / "historia_viva"


@dataclass(frozen=True)
class Regla:
    nombre: str
    requisito: pytest.MarkDecorator
    patron: re.Pattern[str]
    permitidos: tuple[str, ...]
    """Módulos de primer nivel de `historia_viva` donde el patrón está permitido."""
    violador: str
    """Código que la regla tiene que atrapar fuera de `permitidos`."""
    prohibidos: tuple[str, ...] = ()
    """Si hay, la regla es «X no usa Y»: el patrón se busca solo en estos módulos."""


REGLAS = (
    Regla(
        "sql_de_fichas_solo_en_fichas",
        pytest.mark.rnf("RNF-09"),
        re.compile(r"\bacervo\.(pieza|ficha)\b"),
        ("fichas",),
        'conexion.execute("SELECT id FROM acervo.pieza")\n',
    ),
    # Si `pymupdf` o `pytesseract` se filtran fuera de la capa, `transcribir` deja de ser la
    # única puerta al OCR. `docx` pasó a su propia regla en 070: también lo escribe `exportacion`.
    Regla(
        "extraccion_solo_en_capa_modelos",
        pytest.mark.rnf("RNF-01"),
        re.compile(r"^\s*(import|from)\s+(pymupdf|pytesseract)\b", re.M),
        ("capa_modelos",),
        "import pymupdf\n",
    ),
    # `httpx` es el cliente HTTP hacia el motor, desde 036 hacia el proveedor OIDC (RF-34) y
    # desde 124 hacia Drive (DRV-04).
    Regla(
        "motor_solo_en_capa_modelos",
        pytest.mark.rnf("RNF-01"),
        re.compile(r"^\s*(import|from)\s+httpx\b", re.M),
        ("capa_modelos", "identidad", "drive"),
        "from httpx import Client\n",
    ),
    # Cambiar un modelo no toca ningún módulo fuera de la capa.
    Regla(
        "nombres_de_modelos_solo_en_capa_modelos",
        pytest.mark.rnf("RNF-01"),
        re.compile(r"\b(qwen|moondream|llava|gemma|llama\d|bge-m3)", re.I),
        ("capa_modelos",),
        'MODELO = "qwen2.5:3b-instruct-q4_K_M"\n',
    ),
    # La API y el dominio no hablan con la base: la conexión es de `bd`, que traduce la base
    # caída a `BaseNoDisponible` (falla cerrado).
    Regla(
        "psycopg_nunca_en_api_ni_dominio",
        pytest.mark.historia(55),
        re.compile(r"^\s*(import|from)\s+psycopg", re.M),
        (),
        "from psycopg.rows import dict_row\n",
        prohibidos=("api", "dominio"),
    ),
    # DEC-48: las firmas de archivo están en un solo lugar; otra copia admitiría por su cuenta.
    Regla(
        "firmas_de_archivo_solo_en_originales",
        pytest.mark.rf("RF-04"),
        re.compile(r"%PDF-|\\x89PNG|\\xff\\xd8\\xff|PK\\x03\\x04"),
        ("originales",),
        'if contenido.startswith(b"%PDF-"):\n',
    ),
    # El `os.replace` del original lo pide solo `ingesta`, dentro de la transacción de la
    # Pieza y antes del `COMMIT`; si otro lo pidiera, quedaría un original sin Pieza.
    Regla(
        "confirmar_original_solo_en_ingesta",
        pytest.mark.historia(231),
        re.compile(r"\.confirmar\("),
        ("ingesta",),
        "originales.confirmar(temporal, pieza_id=pieza_id)\n",
    ),
    # DEC-47, N13: el original viaja como flujo; leerlo entero son hasta 2 GiB en memoria.
    Regla(
        "ingesta_y_api_no_leen_un_original_entero",
        pytest.mark.rnf("RNF-15"),
        re.compile(r"\.read\(\)|\.read_bytes\("),
        (),
        "contenido = flujo.read()\n",
        prohibidos=("api", "ingesta"),
    ),
    # DEC-140: la vía de ingreso es `ViaDeIngreso`; «procedencia» queda para de dónde sale cada
    # dato de la Ficha.
    Regla(
        "via_de_ingreso_no_es_procedencia",
        pytest.mark.rf("RF-02"),
        re.compile(r"\bOrigenDePieza\b|\bprocedencia\b[^\n]*('(archivo|aporte)'|ViaDeIngreso)"),
        (),
        "conexion.execute(\"SELECT pieza_id FROM pieza WHERE procedencia = 'aporte'\")\n",
    ),
    # DEC-95: la clasificación de contenedores OOXML y OLE2 vive en `originales`, junto a la
    # guarda de ZIP; en otro módulo admitiría un contenedor sin pasar por la guarda.
    Regla(
        "contenedores_ooxml_solo_en_originales",
        pytest.mark.rnf("RNF-20"),
        re.compile(r"word/document\.xml|xl/workbook\.|vbaProject|EncryptedPackage|\\xd0\\xcf\\x11"),
        ("originales",),
        'if "word/document.xml" in comprimido.namelist():\n',
    ),
    # DEC-95, DEC-94: la guarda del Word no puede depender de `planillas`; de ahí `originales`
    # solo toma `clasificar`, para el texto delimitado.
    Regla(
        "originales_no_depende_de_planillas_para_la_guarda",
        pytest.mark.rnf("RNF-20"),
        re.compile(
            r"^\s*(import\s+[\w.]*planillas\b"
            r"|from\s+[\w.]*planillas\s+import\s+(?!clasificar\s*$)"
            r"|from\s+[\w.]+\s+import\s+[^\n]*\bplanillas\b)",
            re.M,
        ),
        (),
        "from historia_viva.planillas import verificar_zip\n",
        prohibidos=("originales",),
    ),
    # 010: el original se lee por `abrir`, de a tramos; abrir su ruta se saltea el 404
    # `original-no-disponible` (DEC-112) y deja leerlo entero.
    Regla(
        "originales_se_leen_por_abrir",
        pytest.mark.rnf("RNF-15"),
        re.compile(r"\._?ruta\("),
        ("originales",),
        "contenido = self._originales.ruta(original.identificador).read_bytes()\n",
    ),
    # 012: `fichas` no conoce la cola ni el índice; `ingesta` y `validacion` orquestan.
    Regla(
        "fichas_no_importa_procesamiento_ni_indexacion",
        pytest.mark.rnf("RNF-09"),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(procesamiento|indexacion)\b"
            r"|from\s+[\w.]*\b(procesamiento|indexacion)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(procesamiento|indexacion)\b)",
            re.M,
        ),
        (),
        "from historia_viva import (\n    fichas,\n    procesamiento,\n)\n",
        prohibidos=("fichas",),
    ),
    # 013: `configuracion` es el único dueño de la tabla; otro lector la leería sin validarla.
    Regla(
        "sql_de_configuracion_capacidad_solo_en_configuracion",
        pytest.mark.rf("RF-32"),
        re.compile(r"\bacervo\.configuracion_capacidad\b"),
        ("configuracion",),
        'tx.execute("SELECT modelo FROM acervo.configuracion_capacidad")\n',
    ),
    # 013: cambiar de modelo es editar su fila; un nombre en el código sería otra fuente.
    Regla(
        "nombres_de_modelos_solo_en_la_semilla",
        pytest.mark.rf("RF-32"),
        re.compile(r"\b(qwen|moondream|llava|gemma|llama\d|mistral|bge-m3)", re.I),
        (),
        'MODELO = "qwen2.5:3b-instruct-q4_K_M"\n',
    ),
    # 013: solo `composicion` conoce el motor concreto; el resto recibe el mapa de motores.
    Regla(
        "motor_concreto_solo_en_composicion",
        pytest.mark.rnf("RNF-01"),
        re.compile(r"\bMotorOllama\("),
        ("composicion",),
        'motor = MotorOllama(url="http://ollama:11434")\n',
    ),
    # 014: el trabajo corre con la instantánea que congeló al encolar (la pide `ingesta`); si la
    # cola o el trabajador leyeran la fila, una edición alcanzaría a un trabajo ya encolado.
    Regla(
        "procesamiento_no_lee_la_configuracion",
        pytest.mark.rf("RF-32"),
        re.compile(r"\.(vigente|instantanea)\("),
        (),
        "capa = self._capa_para(self._configuracion.vigente())\n",
        prohibidos=("procesamiento",),
    ),
    # 044: `indice` es la única puerta a LlamaIndex y a su tabla (DEC-01); si cambia la biblioteca
    # o pgvector, o entra HNSW, cambia solo ahí.
    Regla(
        "llama_index_solo_en_indice",
        pytest.mark.rnf("RNF-09"),
        re.compile(r"^\s*(import|from)\s+llama_index\b", re.M),
        ("indice",),
        "from llama_index.vector_stores.postgres import PGVectorStore\n",
    ),
    Regla(
        "tabla_del_indice_solo_en_indice",
        pytest.mark.rnf("RNF-09"),
        re.compile(r"\bdata_fragmento\b"),
        ("indice",),
        'tx.execute("DELETE FROM acervo.data_fragmento WHERE node_id = %s")\n',
    ),
    # 019 (DEC-26): el chequeo de inyección corre solo al insertar una versión; otro módulo que
    # lo importara podría mostrar o guardar una alerta que la Ficha no tiene.
    Regla(
        "solo_fichas_importa_chequeo_inyeccion",
        pytest.mark.rf("RF-10"),
        re.compile(
            r"^\s*(import\s+[\w.]*\bchequeo_inyeccion\b"
            r"|from\s+[\w.]*\bchequeo_inyeccion\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bchequeo_inyeccion\b)",
            re.M,
        ),
        ("fichas",),
        "from historia_viva import (\n    chequeo_inyeccion,\n)\n",
    ),
    # 015: el esquema lo cambia solo `migrar`, como `hv_migrador`; si otro módulo importara
    # `migraciones`, arrancar la aplicación podría cambiarlo (historia 49, DEC-105).
    Regla(
        "migraciones_solo_las_importa_composicion",
        pytest.mark.historia(49),
        re.compile(
            r"^\s*(import\s+historia_viva\.migraciones\b|from\s+historia_viva\.migraciones\b"
            r"|from\s+historia_viva\s+import\s+[^\n]*\bmigraciones\b)",
            re.M,
        ),
        ("composicion", "migraciones"),
        "from historia_viva.migraciones import aplicar\n",
    ),
    # 028: un CSV hostil se lee en un solo lugar, con los topes y el dialecto de `planillas`.
    Regla(
        "csv_reader_solo_en_planillas",
        pytest.mark.rf("RF-38"),
        re.compile(
            r"\bcsv\.(reader|DictReader)\b|^\s*from\s+csv\s+import\s+[^\n]*\b(reader|DictReader)\b",
            re.M,
        ),
        ("planillas",),
        "filas = csv.reader(texto, delimiter=';')\n",
    ),
    # 028: `planillas` es puro (solo `dominio`): lo importan la admisión y la transcripción.
    Regla(
        "planillas_no_importa_capa_modelos_bd_fichas_ni_api",
        pytest.mark.rf("RF-38"),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(capa_modelos|bd|fichas|api)\b"
            r"|from\s+[\w.]*\b(capa_modelos|bd|fichas|api)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(capa_modelos|bd|fichas|api)\b)",
            re.M,
        ),
        (),
        "from historia_viva import (\n    dominio,\n    bd,\n)\n",
        prohibidos=("planillas",),
    ),
    # 029: `procesamiento` es el único dueño de la cola (spec, módulo 5); otro que la escribiera
    # se saltearía el arriendo y las generaciones.
    Regla(
        "sql_de_trabajo_procesamiento_solo_en_procesamiento",
        pytest.mark.rnf("RNF-25"),
        re.compile(r"\bacervo\.trabajo_procesamiento\b"),
        ("procesamiento",),
        'tx.execute("SELECT estado FROM acervo.trabajo_procesamiento WHERE pieza_id = %s")\n',
    ),
    # 029 (DEC-08): el manejador de cada tipo lo inyecta `composicion`; la cola no conoce a
    # quien la usa.
    Regla(
        "procesamiento_no_importa_lotes_ni_indexacion",
        pytest.mark.historia(215),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(lotes|indexacion)\b"
            r"|from\s+[\w.]*\b(lotes|indexacion)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(lotes|indexacion)\b)",
            re.M,
        ),
        (),
        "from historia_viva.indexacion import ManejadorDeIndexacion\n",
        prohibidos=("procesamiento",),
    ),
    # 017: `auditoria` es lo único que toca su tabla; otro módulo podría escribir un evento sin
    # pasar por la guarda de secretos.
    Regla(
        "sql_de_auditoria_solo_en_auditoria",
        pytest.mark.rf("RF-45"),
        re.compile(r"\bacervo\.auditoria\b"),
        ("auditoria",),
        'tx.execute("INSERT INTO acervo.auditoria (accion) VALUES (%s)", (accion,))\n',
    ),
    # 020 (DEC-146 punto 11): `chequeo_inyeccion` es puro; de `dominio` toma solo la marca de
    # hoja. Con otra dependencia, recalibrar el catálogo dejaría de ser un cambio aislado.
    Regla(
        "chequeo_inyeccion_solo_importa_la_marca_de_hoja",
        pytest.mark.rf("RF-10"),
        re.compile(
            r"^\s*(import\s+historia_viva\b|from\s+\.|from\s+historia_viva\b"
            r"(?!\.dominio\.cuerpo\s+import\s+MarcaDeHoja[ \t]*$))",
            re.M,
        ),
        (),
        "from historia_viva.dominio.cuerpo import MarcaDeHoja, CuerpoDeFicha\n",
        prohibidos=("chequeo_inyeccion",),
    ),
    # 028 (DEC-43): el dialecto lo decide la consistencia de columnas, no una heurística ajena.
    Regla(
        "sin_sniffer_ni_charset_normalizer",
        pytest.mark.historia(97),
        re.compile(r"\bSniffer\(|^\s*(import|from)\s+charset_normalizer\b", re.M),
        (),
        "dialecto = csv.Sniffer().sniff(muestra)\n",
    ),
    # 016 (DEC-124): «quién puede» lo decide la tabla de `dominio` (`Rol.puede`); un rol
    # nombrado en otro módulo (`Rol.RGC`, `Rol("rgc")`, o su valor comparado con `==`, `!=` o
    # `in`) sería una segunda matriz que nadie mantiene.
    Regla(
        "la_matriz_de_permisos_solo_en_dominio",
        pytest.mark.rf("RF-53"),
        re.compile(
            r"\bRol\.(RGC|COORDINADOR|DIRECTOR_CARRERA|ADMIN_TECNICO)\b"
            r"|(==|!=)\s*[\"'](rgc|coordinador|director_carrera|admin_tecnico)[\"']"
            r"|[\"'](rgc|coordinador|director_carrera|admin_tecnico)[\"']\s*(==|!=)"
            r"|\bin\s*[(\[{][^)\]}]*[\"'](rgc|coordinador|director_carrera|admin_tecnico)[\"']"
            r"|\bRol\(\s*[\"'](rgc|coordinador|director_carrera|admin_tecnico)[\"']"
        ),
        ("dominio",),
        "if principal.rol is Rol.ADMIN_TECNICO:\n",
    ),
    # 027 (DEC-46): todo CSV sale del único escritor, que neutraliza las fórmulas; otro
    # `csv.writer` exportaría un `=HYPERLINK(...)` tal cual.
    Regla(
        "csv_writer_solo_en_exportacion",
        pytest.mark.historia(197),
        re.compile(
            r"\bcsv\.(writer|DictWriter)\b|^\s*from\s+csv\s+import\s+[^\n]*\b(writer|DictWriter)\b",
            re.M,
        ),
        ("exportacion",),
        "from csv import DictWriter\n",
    ),
    # 070 (R-23, spec A5): python-docx lee en `capa_modelos` (la transcripción del Word) y
    # escribe solo en `exportacion`, el único armador del `.docx`, sin campos ni vínculos.
    Regla(
        "docx_solo_en_capa_modelos_y_exportacion",
        pytest.mark.rf("RF-49"),
        re.compile(r"^\s*(import|from)\s+docx\b", re.M),
        ("capa_modelos", "exportacion"),
        "from docx import Document",
    ),
    # 071 (R-23, spec A5): XlsxWriter escribe el `.xlsx` del informe solo en `exportacion`, que
    # pasa cada texto por `celda_segura`; otro escritor dejaría pasar un `=HYPERLINK(...)`.
    Regla(
        "xlsxwriter_solo_en_exportacion",
        pytest.mark.rf("RF-49"),
        re.compile(r"^\s*(import|from)\s+xlsxwriter\b", re.M),
        ("exportacion",),
        "import xlsxwriter\n",
    ),
    # 021 (R-25): `catalogos` es la única puerta a los tres catálogos; otro lector validaría la
    # baja lógica a su manera. Con o sin esquema: `hv_app` tiene `acervo` en su `search_path`.
    Regla(
        "sql_de_catalogos_solo_en_catalogos",
        pytest.mark.rf("RF-51"),
        re.compile(
            r"\b(from|join|into|update|table)\s+(acervo\.)?(carrera|tipo_actividad|tipo_pieza)\b",
            re.I,
        ),
        ("catalogos",),
        'tx.execute("SELECT nombre FROM tipo_actividad WHERE activo")\n',
    ),
    # 024 (R-25): `pieza_carrera` es proyección de la Ficha y la escribe solo `fichas`.
    Regla(
        "sql_de_pieza_carrera_solo_en_fichas",
        pytest.mark.rf("RF-50"),
        re.compile(r"\b(from|join|into|update|table)\s+(acervo\.)?pieza_carrera\b", re.I),
        ("fichas",),
        'tx.execute("SELECT carrera_id FROM acervo.pieza_carrera WHERE pieza_id = %s")\n',
    ),
    # 018 (ADR-0004): `interruptor` es la única puerta a su tabla; otro que escribiera se
    # saltearía el motivo y podría encender la API de Agentes de rebote (DEC-136).
    Regla(
        "sql_de_evento_interruptor_solo_en_interruptor",
        pytest.mark.rf("RF-30"),
        re.compile(r"\bacervo\.evento_interruptor\b"),
        ("interruptor",),
        'tx.execute("INSERT INTO acervo.evento_interruptor (clave) VALUES (%s)", (clave,))\n',
    ),
    # 018 (DEC-86): la capa de modelos no sabe del freno, y el trabajador lo recibe como
    # `Callable` de `composicion`: el falla cerrado vive en un solo lugar.
    Regla(
        "capa_modelos_y_procesamiento_no_importan_interruptor",
        pytest.mark.rf("RF-30"),
        re.compile(
            r"^\s*(import\s+[\w.]*\binterruptor\b"
            r"|from\s+[\w.]*\binterruptor\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\binterruptor\b)",
            re.M,
        ),
        (),
        "from historia_viva.interruptor import habilitado\n",
        prohibidos=("capa_modelos", "procesamiento"),
    ),
    # 026: `lotes` es la única puerta a `lote` (y a `lote_carrera`, 082); otro que lo leyera
    # cargaría sin pasar por el tope ni por el permiso de Lote ajeno.
    Regla(
        "sql_de_lote_solo_en_lotes",
        pytest.mark.rf("RF-01"),
        re.compile(r"\bacervo\.lote(_carrera)?\b"),
        ("lotes",),
        'tx.execute("SELECT origen FROM acervo.lote WHERE lote_id = %s", (lote_id,))\n',
    ),
    # 083 (decisión de 018): `procesamiento_pausado` de la subida, suelta o del Lote, se calcula
    # en la ruta; `ingesta` encola igual y no sabe del freno.
    Regla(
        "ingesta_no_importa_interruptor",
        pytest.mark.historia(84),
        re.compile(
            r"^\s*(import\s+[\w.]*\binterruptor\b"
            r"|from\s+[\w.]*\binterruptor\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\binterruptor\b)",
            re.M,
        ),
        (),
        "from historia_viva.interruptor import Freno, habilitado_en\n",
        prohibidos=("ingesta",),
    ),
    # 040 (DEC-144, R-33): volcar y respaldar pasa solo por `respaldo`, que se niega a dejar
    # nada dentro del OneDrive; otro volcado quedaría sin esa guarda y sin registro.
    Regla(
        "pg_dump_y_restic_solo_en_respaldo",
        pytest.mark.rnf("RNF-17"),
        re.compile(r"[\"'](pg_dump|pg_dumpall|restic)[\"']"),
        ("respaldo",),
        'subprocess.run(["pg_dump", "--format=custom"], stdout=archivo)\n',
    ),
    # 045 (DEC-06): a qué Ollama va cada capacidad lo dice la columna `motor` de su fila; un id
    # escrito en otro módulo mandaría `embeber` al de generación o al revés.
    Regla(
        "ids_de_motores_solo_en_composicion",
        pytest.mark.rnf("RNF-01"),
        re.compile(r"[\"']ollama(_embeddings)?[\"']"),
        ("composicion",),
        'motor = self._motores["ollama_embeddings"]\n',
    ),
    # 088 (DEC-42): un `.xlsx` hostil se lee en un solo lugar, endurecido y con sus topes.
    Regla(
        "openpyxl_y_defusedxml_solo_en_planillas",
        pytest.mark.rnf("RNF-20"),
        re.compile(r"^\s*(import|from)\s+(openpyxl|defusedxml)\b", re.M),
        ("planillas",),
        "from openpyxl import load_workbook\n",
    ),
    # 088 (DEC-42, DEC-112): de una fórmula se lee el valor que guardó Excel; sin `data_only`,
    # openpyxl devuelve la fórmula y quien la mostrara o la recalculara la estaría ejecutando.
    Regla(
        "las_planillas_se_leen_con_los_valores_guardados",
        pytest.mark.historia(95),
        re.compile(
            r"\bdata_only\s*=\s*False\b|\bload_workbook\((?![^)]*\bdata_only\s*=\s*True\b)"
        ),
        (),
        "libro = load_workbook(origen, read_only=True)\n",
    ),
    # 031 (DEC-37): `identidad` es el único dueño de las sesiones; otro que las leyera se
    # saltearía el hash del id, los vencimientos y la baja.
    Regla(
        "sql_de_sesion_solo_en_identidad",
        pytest.mark.historia(87),
        re.compile(r"\b(from|join|into|update|table)\s+(acervo\.)?sesion\b", re.I),
        ("identidad",),
        'tx.execute("SELECT usuario_id FROM sesion WHERE sesion_id = %s", (cookie,))\n',
    ),
    # 025 (DEC-21): `validacion` orquesta la firma y la API la traduce a HTTP; si importara
    # `api` o `web`, firmar dependería de cómo se pide.
    Regla(
        "validacion_no_importa_api_ni_web",
        pytest.mark.rf("RF-15"),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(api|web)\b"
            r"|from\s+[\w.]*\b(api|web)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(api|web)\b)",
            re.M,
        ),
        (),
        "from historia_viva.api.problemas import Problema\n",
        prohibidos=("validacion",),
    ),
    # 041 (DEC-144): `pg_restore --clean` borra la base donde restaura; solo lo pide `respaldo`,
    # contra el proyecto `historiaviva-restauracion`, que no comparte volúmenes con el origen.
    Regla(
        "pg_restore_solo_en_respaldo",
        pytest.mark.historia(230),
        re.compile(r"[\"']pg_restore[\"']"),
        ("respaldo",),
        'subprocess.run(["pg_restore", "--clean", "--dbname", dsn, volcado])\n',
    ),
    # 032 (R-02, R-03): el padrón cambia solo por `identidad.Padron`; otro que escribiera en
    # `usuario` otorgaría `admin_tecnico` o dejaría sin titular a un permiso sin mirar las reglas.
    Regla(
        "escribir_el_padron_solo_en_identidad",
        pytest.mark.historia(150),
        re.compile(r"\b(insert\s+into|update)\s+(acervo\.)?usuario\b", re.I),
        ("identidad",),
        'tx.execute("UPDATE acervo.usuario SET rol = %s WHERE usuario_id = %s", (rol, uid))\n',
    ),
    # 096 (DEC-32): `agentes` es el único dueño de los tokens; otro que los leyera se saltearía
    # el hash, el vencimiento y la revocación.
    Regla(
        "sql_de_tokens_solo_en_agentes",
        pytest.mark.rf("RF-26"),
        re.compile(
            r"\b(from|join|into|update|table)\s+(acervo\.)?(agente_externo|token_agente)\b", re.I
        ),
        ("agentes",),
        'tx.execute("SELECT agente_id FROM acervo.token_agente WHERE token_hash = %s", (h,))\n',
    ),
    # 092 (DEC-16): `ffprobe` corre solo en `video`, con su endurecimiento en un lugar.
    Regla(
        "ffprobe_solo_en_video",
        pytest.mark.rf("RF-39"),
        re.compile(r"[\"']ffprobe[\"']"),
        ("video",),
        'subprocess.run(["ffprobe", "-show_streams", str(ruta)])\n',
    ),
    # 092: un proceso hijo sobre contenido hostil solo en `video`; `respaldo` corre `pg_dump`. 153
    # suma `conversion`, que convierte el `.doc`, el `.xls` y el HEIC aislado.
    Regla(
        "subprocess_solo_en_video_y_respaldo",
        pytest.mark.rf("RF-39"),
        re.compile(r"^\s*(import|from)\s+subprocess\b", re.M),
        ("video", "respaldo", "conversion"),
        "import subprocess\n",
    ),
    # 092 (DEC-94, ADR-0005): `ingesta` recibe `analizar` inyectado y ningún módulo de modelos
    # ve el video ni sus datos: a `video` lo importa solo `composicion`.
    Regla(
        "video_solo_lo_importa_composicion",
        pytest.mark.historia(101),
        re.compile(
            r"^\s*(import\s+historia_viva\.video\b"
            r"|from\s+historia_viva\.video\b"
            r"|from\s+historia_viva\s+import\s+(\([^)]*|[^\n]*)\bvideo\b)",
            re.M,
        ),
        ("composicion", "video"),
        "from historia_viva import (\n    bd,\n    video,\n)\n",
    ),
    # 094 (DEC-146 punto 13): el contador de subidas simultáneas vive en `ingesta`; otro que
    # contara en `api` u `originales` no liberaría el cupo junto con la subida.
    Regla(
        "demasiadas_subidas_solo_en_ingesta",
        pytest.mark.rnf("RNF-22"),
        re.compile(r"\bDemasiadasSubidas\("),
        ("ingesta",),
        "raise DemasiadasSubidas(maximo=2)\n",
    ),
    # 033 (R-30, DEC-137): superar el tope de 6 reprocesos es solo por comando; la API nunca
    # pasa `forzar`, así ningún parámetro del pedido puede llegar hasta ahí.
    Regla(
        "la_api_nunca_fuerza_el_tope_de_reprocesos",
        pytest.mark.historia(83),
        re.compile(r"\bforzar\s*="),
        (),
        "ingesta.reprocesar(pieza_id, por=principal.actor, forzar=pedido.forzar)\n",
        prohibidos=("api",),
    ),
    # 090 (DEC-42, ADR-0003): el XML de un Word o de una planilla lo escribió cualquiera; un
    # parser de lxml que resolviera entidades abriría el XXE y el billion laughs.
    Regla(
        "los_parsers_de_xml_no_resuelven_entidades",
        pytest.mark.rnf("RNF-20"),
        re.compile(r"\bXMLParser\((?![^)]*\bresolve_entities\s*=\s*False\b)"),
        (),
        "parser = etree.XMLParser(target=destino, no_network=True)\n",
    ),
    # 035 (DEC-106, N15): `procesamiento` es el único dueño del latido; la salud lo lee por él.
    Regla(
        "sql_de_latido_trabajador_solo_en_procesamiento",
        pytest.mark.rnf("RNF-25"),
        re.compile(r"\bacervo\.latido_trabajador\b"),
        ("procesamiento",),
        'tx.execute("SELECT trabajador, latido_el FROM acervo.latido_trabajador")\n',
    ),
    # 037 (DEC-33): `/v1` tiene su propio formato de error; si `api_agentes` importara `api`, el
    # RFC 9457 de la web se colaría en el contrato con el Agente 1.
    Regla(
        "api_agentes_no_importa_api",
        pytest.mark.historia(182),
        re.compile(
            r"^\s*(import\s+[\w.]*\bapi\b"
            r"|from\s+[\w.]*\bapi\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bapi\b)",
            re.M,
        ),
        (),
        "from historia_viva.api.problemas import Problema\n",
        prohibidos=("api_agentes",),
    ),
    # 097 (spec, módulo 23): `/v1` solo traduce; la base es de `bd` (y de `agentes` para los
    # tokens) y la sesión de las personas no autentica a un Agente externo.
    Regla(
        "api_agentes_no_importa_psycopg_ni_identidad",
        pytest.mark.historia(182),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(psycopg|identidad)\b"
            r"|from\s+[\w.]*\b(psycopg|identidad)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(psycopg|identidad)\b)",
            re.M,
        ),
        (),
        "from historia_viva.identidad import Principal\n",
        prohibidos=("api_agentes",),
    ),
    # 050 (DEC-136, DEC-146 punto 12): la consulta del Acervo no se pausa ni sabe del índice
    # en construcción; el modelo vigente y el aviso le llegan de `composicion`.
    Regla(
        "busqueda_no_importa_interruptor_ni_indexacion",
        pytest.mark.rf("RF-30"),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(interruptor|indexacion)\b"
            r"|from\s+[\w.]*\b(interruptor|indexacion)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(interruptor|indexacion)\b)",
            re.M,
        ),
        (),
        "from historia_viva.interruptor import habilitado_en\n",
        prohibidos=("busqueda",),
    ),
    # 050 (R-14): `busqueda` recupera y nunca redacta; la redacción es de `conversacion`.
    Regla(
        "busqueda_no_llama_a_responder",
        pytest.mark.rf("RF-22"),
        re.compile(r"\.responder(_en_vivo)?\("),
        (),
        "texto = self._capa().responder(consulta=consulta, fragmentos=fragmentos)\n",
        prohibidos=("busqueda",),
    ),
    # 051 (DEC-136): la Conversación sigue con el procesamiento frenado y no sabe del índice
    # en construcción; el aviso le llega por `busqueda`.
    Regla(
        "conversacion_no_importa_interruptor_ni_indexacion",
        pytest.mark.rf("RF-47"),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(interruptor|indexacion)\b"
            r"|from\s+[\w.]*\b(interruptor|indexacion)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(interruptor|indexacion)\b)",
            re.M,
        ),
        (),
        "from historia_viva import indexacion\n",
        prohibidos=("conversacion",),
    ),
    # 051 (N22): las tres tablas de la Conversación son de `conversacion`.
    Regla(
        "sql_de_conversacion_solo_en_conversacion",
        pytest.mark.rf("RF-47"),
        re.compile(r"\bacervo\.(conversacion|mensaje_conversacion|mensaje_cita)\b"),
        ("conversacion",),
        'tx.execute("SELECT contenido FROM acervo.mensaje_conversacion")\n',
    ),
    # 054 (R-18, amenaza 34): el historial lo arma `conversacion` desde la base; nadie más arma
    # un bloque `historial` que llegue al modelo.
    Regla(
        "bloque_historial_solo_en_conversacion",
        pytest.mark.rnf("RNF-04"),
        re.compile(r"\bETIQUETA_HISTORIAL\b"),
        ("conversacion", "capa_modelos"),
        "BloqueDatos(ETIQUETA_HISTORIAL, pedido.historial)\n",
    ),
    # 055 (R-18): el tope de preguntas por persona lo cuenta `conversacion` en la base; un
    # limitador en `api` contaría por proceso y otra puerta al turno lo saltearía.
    Regla(
        "tope_de_consultas_solo_en_conversacion",
        pytest.mark.rf("RF-47"),
        re.compile(r"\bLimiteDeConsultas\("),
        ("conversacion",),
        "raise LimiteDeConsultas(60)\n",
    ),
    # 084 (spec, tabla de vistas): `v_lote_resumen` la lee solo `fichas`; el progreso lo compone
    # `lotes` con las fallidas de la cola (DEC-110), y otro lector armaría un estado aparte.
    Regla(
        "v_lote_resumen_solo_en_fichas",
        pytest.mark.historia(72),
        re.compile(r"\bv_lote_resumen\b"),
        ("fichas",),
        'tx.execute("SELECT n_recibida FROM acervo.v_lote_resumen WHERE lote_id = %s", (l,))\n',
    ),
    # 105 (DEC-143, ADR-0004 control 1): retirar no borra; el único `DELETE` es el del índice,
    # que es derivado y se reconstruye.
    Regla(
        "delete_from_solo_en_indice",
        pytest.mark.rf("RF-29"),
        re.compile(r"\bDELETE\s+FROM\b", re.I),
        ("indice",),
        'tx.execute("DELETE FROM acervo.pieza WHERE pieza_id = %s", (pieza_id,))\n',
    ),
    # 105 (spec, módulo 18): el trabajador no tiene camino de código para retirar.
    Regla(
        "procesamiento_no_importa_retiro",
        pytest.mark.rf("RF-29"),
        re.compile(
            r"^\s*(import\s+[\w.]*\bretiro\b"
            r"|from\s+[\w.]*\bretiro\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bretiro\b)",
            re.M,
        ),
        (),
        "from historia_viva.retiro import Retiro\n",
        prohibidos=("procesamiento",),
    ),
    # 106 (DEC-138): restaurar pasa por `retiro`, que comprueba la huella y el Lote y reencola;
    # otro que quitara la marca devolvería una Pieza sin esos controles.
    Regla(
        "solo_retiro_restaura",
        pytest.mark.historia(295),
        re.compile(r"\bdesmarcar_borrada\b"),
        ("fichas", "retiro"),
        "fichas.desmarcar_borrada(tx, pieza_id)\n",
    ),
    # 105 (DEC-29, N16): ningún router declara el verbo `DELETE`; retirar es `POST …/retiro`.
    Regla(
        "ningun_router_usa_delete",
        pytest.mark.rf("RF-29"),
        re.compile(r"\.delete\(|methods\s*=\s*\[[^\]]*[\"']DELETE[\"']"),
        (),
        '@router.delete("/{pieza_id}")\n',
        prohibidos=("api",),
    ),
    # 100 (DEC-17, DEC-146 punto 13): el Aporte es una puerta de `ingesta`, dueña de sus tablas y
    # de su idempotencia; `/v1` solo traduce.
    Regla(
        "sql_de_aporte_solo_en_ingesta",
        pytest.mark.rf("RF-25"),
        re.compile(r"\bacervo\.aporte(_pieza_fuente|_fragmento_fuente)?\b"),
        ("ingesta",),
        'tx.execute("SELECT cuerpo_huella FROM acervo.aporte WHERE idempotency_key = %s")\n',
    ),
    # 057 (spec, módulo 32): las efemérides no usan la cola, no validan ni indexan y no dependen
    # de la Conversación (que es quien las llama para los Relatos).
    Regla(
        "efemerides_no_importa_validacion_indexacion_procesamiento_ni_conversacion",
        pytest.mark.rf("RF-48"),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(validacion|indexacion|procesamiento|conversacion)\b"
            r"|from\s+[\w.]*\b(validacion|indexacion|procesamiento|conversacion)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)"
            r"\b(validacion|indexacion|procesamiento|conversacion)\b)",
            re.M,
        ),
        (),
        "from historia_viva.procesamiento.cola import Cola\n",
        prohibidos=("efemerides",),
    ),
    # 057: las tablas de efemérides son de `efemerides`.
    Regla(
        "sql_de_efemerides_solo_en_efemerides",
        pytest.mark.rf("RF-48"),
        re.compile(r"\bacervo\.(efemeride|efemeride_fuente|programacion_efemerides)\b"),
        ("efemerides",),
        'tx.execute("SELECT cuerpo FROM acervo.efemeride")\n',
    ),
    # 064 (DEC-147): las tablas de los Relatos son de `efemerides`.
    Regla(
        "sql_de_relatos_solo_en_efemerides",
        pytest.mark.rf("RF-55"),
        re.compile(r"\bacervo\.(relato|relato_fuente)\b"),
        ("efemerides",),
        'tx.execute("SELECT cuerpo FROM acervo.relato")\n',
    ),
    # 064 (DEC-147, R-42): un Relato nace solo de una respuesta de la Conversación, que verifica
    # que es propia, completa y con Citas; otro que llamara a `efemerides` se saltearía eso.
    Regla(
        "solo_conversacion_propone_relatos_a_efemerides",
        pytest.mark.rf("RF-55"),
        re.compile(r"efemerides\.proponer_relato\("),
        ("conversacion",),
        "request.app.state.efemerides.proponer_relato(tx, mensaje_id=m, titulo=t)\n",
    ),
    # 063 (auditoría de tickets, decisión 11): `validacion` marca «fuente retirada» con la
    # función que le inyecta `composicion`; si importara `efemerides`, firmar dependería de ella.
    Regla(
        "validacion_no_importa_efemerides",
        pytest.mark.rf("RF-48"),
        re.compile(
            r"^\s*(import\s+[\w.]*\befemerides\b"
            r"|from\s+[\w.]*\befemerides\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\befemerides\b)",
            re.M,
        ),
        (),
        "from historia_viva.efemerides import Efemerides\n",
        prohibidos=("validacion",),
    ),
    # 107 (R-12): lo mismo para `retiro`, que marca con la función que le inyecta `composicion`.
    Regla(
        "retiro_no_importa_efemerides",
        pytest.mark.rf("RF-29"),
        re.compile(
            r"^\s*(import\s+[\w.]*\befemerides\b"
            r"|from\s+[\w.]*\befemerides\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\befemerides\b)",
            re.M,
        ),
        (),
        "from historia_viva.efemerides import Efemerides\n",
        prohibidos=("retiro",),
    ),
    # 066 (DEC-131): sin modelo no hay alucinación en un documento de acreditación.
    Regla(
        "informes_no_importa_capa_modelos",
        pytest.mark.rf("RF-49"),
        re.compile(
            r"^\s*(import\s+[\w.]*\bcapa_modelos\b"
            r"|from\s+[\w.]*\bcapa_modelos\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bcapa_modelos\b)",
            re.M,
        ),
        (),
        "from historia_viva.capa_modelos.contrato import Capacidad\n",
        prohibidos=("informes",),
    ),
    # 067 (DEC-128): la integridad es `banco.integridad` y llega por `composicion`; si `informes`
    # importara `banco`, el informe dependería del banco de pruebas.
    Regla(
        "informes_no_importa_banco",
        pytest.mark.historia(273),
        re.compile(
            r"^\s*(import\s+[\w.]*\bbanco\b"
            r"|from\s+[\w.]*\bbanco\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bbanco\b)",
            re.M,
        ),
        (),
        "from historia_viva import banco\n",
        prohibidos=("informes",),
    ),
    # 066: las tres tablas del informe son de `informes`.
    Regla(
        "sql_de_informes_solo_en_informes",
        pytest.mark.rf("RF-49"),
        re.compile(r"\bacervo\.(informe|informe_pieza|informe_aprobacion)\b"),
        ("informes",),
        'tx.execute("SELECT huella FROM acervo.informe")\n',
    ),
    # 085 (DEC-18): el estado del Lote lo deriva `lotes` con `dominio`, para el detalle y para la
    # lista; otro que lo calculara sobre sus propios conteos podría no contar igual las retiradas.
    Regla(
        "estado_de_lote_solo_en_lotes",
        pytest.mark.historia(73),
        re.compile(r"\bestado_de_lote\b"),
        ("dominio", "lotes"),
        "estado = estado_de_lote(conteos)\n",
    ),
    # 108 (spec, módulo 17): `validacion` es dueña de `aprobacion_en_bloque`; otro que la abriera
    # o la cerrara se saltearía `HV_BLOQUE_HABILITADO` (R-28) y la regla única del bloque.
    Regla(
        "sql_de_aprobacion_en_bloque_solo_en_validacion",
        pytest.mark.rf("RF-18"),
        re.compile(r"\bacervo\.aprobacion_en_bloque\b"),
        ("validacion",),
        'tx.execute("UPDATE acervo.aprobacion_en_bloque SET confirmada_el = now()")\n',
    ),
    # 072 (DEC-97, N12): el banco no tiene tablas ni SQL; lee por `fichas`, `indice` e `informes`.
    Regla(
        "banco_sin_sql_propio",
        pytest.mark.historia(205),
        re.compile(r"\.execute(many)?\(|\bacervo\.\w"),
        (),
        'tx.execute("SELECT pieza_id FROM acervo.data_fragmento")\n',
        prohibidos=("banco",),
    ),
    # 072 (DEC-96, N11): medir con la cola frenada es el uso del freno; `banco` no lo consulta, y
    # lo arma `composicion.componer_banco`.
    Regla(
        "banco_no_importa_composicion_ni_interruptor",
        pytest.mark.historia(162),
        re.compile(
            r"^\s*(import\s+[\w.]*\b(composicion|interruptor)\b"
            r"|from\s+[\w.]*\b(composicion|interruptor)\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b(composicion|interruptor)\b)",
            re.M,
        ),
        (),
        "from historia_viva.interruptor import habilitado\n",
        prohibidos=("banco",),
    ),
    # 165 (SEG-11): `nltk` 3.10.3 tiene un CVE sin parche en sus APIs de modelos; llega por
    # `llama-index-core` y la app no lo usa. Que siga así hasta que salga la versión corregida.
    Regla(
        "nltk_nunca_en_la_app",
        pytest.mark.rnf("RNF-20"),
        re.compile(r"^\s*(import|from)\s+nltk\b", re.M),
        (),
        "from nltk.data import load\n",
    ),
    # 103 (C33): la alerta de inyección es para quien valida o revisa; `/v1` no la lee de nada
    # (Piezas, fragmentos, efemérides ni Relatos).
    Regla(
        "api_agentes_no_lee_la_alerta_de_inyeccion",
        pytest.mark.rf("RF-48"),
        re.compile(r"\.alerta_inyeccion\b|\[\s*[\"']alerta_inyeccion[\"']\s*\]"),
        (),
        '"alerta": efemeride.alerta_inyeccion,\n',
        prohibidos=("api_agentes",),
    ),
    # 073 (DEC-89): M1 mide la lista que ve la persona, la de `busqueda.buscar`; el banco no
    # consulta el índice por su cuenta.
    Regla(
        "banco_mide_m1_por_busqueda",
        pytest.mark.rnf("RNF-11"),
        re.compile(r"\bIndice\b|\.buscar_vectores\(|indice\(\)\.buscar\("),
        (),
        "from historia_viva.indice import Indice\n",
        prohibidos=("banco",),
    ),
    # 074 (spec, `enviar_mensaje`): un turno sin guardar no audita ni cuenta topes; solo lo pide
    # el banco para medir M3, y es la única puerta para un historial que no sale de la base.
    Regla(
        "turno_sin_guardar_solo_en_banco",
        pytest.mark.historia(204),
        re.compile(r"\bpersistir\s*=\s*False\b"),
        ("banco", "conversacion"),
        "conversaciones.enviar_mensaje(None, principal=None, texto=t, filtro=f, persistir=False)\n",
    ),
    # 124 (DRV-14): `drive` es dueño de `replicacion_drive`; otro que la escribiera se saltearía
    # el arriendo y podría duplicar un archivo en Drive.
    Regla(
        "sql_de_replicacion_drive_solo_en_drive",
        pytest.mark.historia(240),
        re.compile(r"\bacervo\.replicacion_drive\b"),
        ("drive",),
        "tx.execute(\"UPDATE acervo.replicacion_drive SET estado = 'pendiente'\")\n",
    ),
    # 125 (RF-42, runbook R14): la supresión efectiva es lo único que borra datos del Acervo; la
    # llama solo `supresion`, con `hv_migrador`, y a `supresion` solo la arma el comando.
    Regla(
        "suprimir_pieza_solo_en_supresion",
        pytest.mark.rf("RF-42"),
        re.compile(r"\bsuprimir_pieza\b"),
        ("supresion",),
        'tx.execute("SELECT acervo.suprimir_pieza(%s)", (pieza_id,))\n',
    ),
    Regla(
        "supresion_solo_desde_el_comando",
        pytest.mark.rf("RF-42"),
        re.compile(
            r"^\s*(import\s+[\w.]*\bsupresion\b"
            r"|from\s+[\w.]*\bsupresion\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bsupresion\b)",
            re.M,
        ),
        ("supresion", "composicion"),
        "from historia_viva.supresion import Supresion\n",
    ),
    # 125 (auditoría, decisión 16 B): el aviso de DNI, CUIL o CBU es de `dominio` y lo consulta
    # `validacion`, no `chequeo_inyeccion` ni cada router; `supresion` lo usa con el motivo.
    Regla(
        "aviso_de_datos_personales_desde_validacion",
        pytest.mark.rf("RF-42"),
        re.compile(r"\bdetectar_datos_personales\b"),
        ("dominio", "validacion", "supresion"),
        "avisos = detectar_datos_personales(cuerpo)\n",
    ),
    # 110 (DEC-132): la nota «Banco: ganador X · mejor no chino Y» llega por `composicion`
    # (`eleccion_del_banco`); si `api` importara `banco`, `web` dependería del banco de pruebas.
    Regla(
        "api_no_importa_banco",
        pytest.mark.rf("RF-32"),
        re.compile(
            r"^\s*(import\s+[\w.]*\bbanco\b"
            r"|from\s+[\w.]*\bbanco\b"
            r"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\bbanco\b)",
            re.M,
        ),
        (),
        "from historia_viva.banco.comparacion import eleccion_del_banco\n",
        prohibidos=("api",),
    ),
    # 120 (historia 221): `pytest` es de desarrollo y la imagen no lo trae; solo lo importa la
    # `matriz`, que recolecta la suite. En otro módulo, la aplicación no arrancaría en la imagen.
    Regla(
        "pytest_solo_en_matriz",
        pytest.mark.historia(221),
        re.compile(r"^\s*(import|from)\s+pytest\b", re.M),
        ("matriz",),
        "import pytest\n",
    ),
    # 153 (DEC-41 revisada): LibreOffice y el decodificador de HEIC abren contenido hostil y
    # corren solo en `conversion`, con su aislamiento en un lugar.
    Regla(
        "libreoffice_y_heic_solo_en_conversion",
        pytest.mark.rf("RF-04"),
        re.compile(r"[\"'](soffice|libreoffice)[\"']|^\s*(import|from)\s+pillow_heif\b", re.M),
        ("conversion",),
        'subprocess.run(["soffice", "--headless", "--convert-to", "docx", str(ruta)])\n',
    ),
    # 153: el conversor lo recibe `originales` inyectado; solo `composicion` lo arma.
    Regla(
        "conversion_solo_la_importa_composicion",
        pytest.mark.rf("RF-05"),
        re.compile(
            r"^\s*(import\s+historia_viva\.conversion\b"
            r"|from\s+historia_viva\.conversion\b"
            r"|from\s+historia_viva\s+import\s+(\([^)]*|[^\n]*)\bconversion\b)",
            re.M,
        ),
        ("composicion", "conversion"),
        "from historia_viva import banco, conversion\n",
    ),
    # 144 (ADR-0007): las Afirmaciones y su decisión las guarda `fichas`, la única puerta a la
    # Ficha, y solo `fichas` escribe las aceptadas en el cuerpo, al firmar.
    Regla(
        "sql_de_afirmaciones_solo_en_fichas",
        pytest.mark.rf("RF-14"),
        re.compile(r"\bacervo\.(afirmacion|descripcion_de_imagen)\b"),
        ("fichas",),
        "tx.execute(\"UPDATE acervo.afirmacion SET estado = 'aceptada'\")\n",
    ),
    Regla(
        "afirmaciones_al_cuerpo_solo_en_fichas",
        pytest.mark.rf("RF-14"),
        re.compile(r"\bescribir_afirmaciones\b"),
        ("dominio", "fichas"),
        "principal = escribir_afirmaciones(principal, aceptadas)\n",
    ),
    # 145 (ADR-0007): las Afirmaciones se cruzan con el OCR una sola vez, al procesar la Pieza, y
    # sin otro modelo: el respaldo queda guardado con la v1 y nadie lo recalcula después.
    Regla(
        "cruce_con_el_ocr_solo_al_procesar",
        pytest.mark.rf("RF-08"),
        re.compile(r"\bcruzar\s*\("),
        ("dominio", "procesamiento"),
        "respaldos = cruzar(afirmaciones, palabras)\n",
    ),
    # 146: el resumen rehecho es una propuesta de la Ficha y lo guarda `fichas`, que le pasa el
    # chequeo de inyección; el trabajo que lo rehace no escribe la tabla.
    Regla(
        "sql_de_resumen_rehecho_solo_en_fichas",
        pytest.mark.rf("RF-09"),
        re.compile(r"\bacervo\.resumen_rehecho\b"),
        ("fichas",),
        'tx.execute("INSERT INTO acervo.resumen_rehecho (ficha_id, texto) VALUES (%s, %s)")\n',
    ),
)


def _vigilado(regla: Regla, modulo: str) -> bool:
    return modulo in regla.prohibidos if regla.prohibidos else modulo not in regla.permitidos


def infractores(regla: Regla, paquete: Path) -> list[str]:
    return sorted(
        ruta.relative_to(paquete).as_posix()
        for ruta in paquete.rglob("*.py")
        if _vigilado(regla, ruta.relative_to(paquete).parts[0])
        and regla.patron.search(ruta.read_text(encoding="utf-8"))
    )


def modulos_inexistentes(regla: Regla, paquete: Path) -> list[str]:
    declarados = (*regla.permitidos, *regla.prohibidos)
    return [m for m in declarados if not (paquete / m / "__init__.py").is_file()]


POR_REGLA = [pytest.param(regla, id=regla.nombre, marks=regla.requisito) for regla in REGLAS]


@pytest.mark.parametrize("regla", POR_REGLA)
def test_el_codigo_respeta_la_regla(regla: Regla):
    assert infractores(regla, PAQUETE) == []


@pytest.mark.parametrize("regla", POR_REGLA)
def test_la_regla_atrapa_a_su_violador(regla: Regla, tmp_path: Path):
    for modulo in (*regla.permitidos, *regla.prohibidos, "otro_modulo"):
        (tmp_path / modulo).mkdir()
        (tmp_path / modulo / "violador.py").write_text(regla.violador, encoding="utf-8")

    atrapados = sorted(regla.prohibidos) or ["otro_modulo"]
    assert infractores(regla, tmp_path) == [f"{m}/violador.py" for m in atrapados]


@pytest.mark.parametrize("regla", POR_REGLA)
def test_los_modulos_declarados_existen(regla: Regla):
    assert modulos_inexistentes(regla, PAQUETE) == []


def test_migraciones_se_importa_a_si_mismo(tmp_path: Path):
    [regla] = [r for r in REGLAS if r.nombre == "migraciones_solo_las_importa_composicion"]
    (tmp_path / "migraciones").mkdir()
    (tmp_path / "migraciones" / "sql.py").write_text(regla.violador, encoding="utf-8")

    assert infractores(regla, tmp_path) == []


@pytest.mark.rf("RF-53")
@pytest.mark.parametrize(
    "comparacion",
    [
        "if persona.rol is Rol.RGC:",
        "if rol == 'coordinador':",
        "if 'rgc' == rol:",
        "if fila['rol'] != \"admin_tecnico\":",
        "if rol in ('rgc', 'coordinador'):",
        "if persona.rol not in {\"director_carrera\"}:",
        "if rol is Rol('rgc'):",
    ],
)
def test_la_matriz_de_permisos_atrapa_cada_forma_de_comparar_un_rol(comparacion: str):
    [regla] = [r for r in REGLAS if r.nombre == "la_matriz_de_permisos_solo_en_dominio"]

    assert regla.patron.search(comparacion)


def test_un_modulo_declarado_que_no_existe_se_detecta():
    regla = Regla("x", pytest.mark.rnf("RNF-01"), re.compile("x"), ("fichas", "no_existe"), "x")

    assert modulos_inexistentes(regla, PAQUETE) == ["no_existe"]


# 012 (DEC-26, DEC-143, N9): una sola función inserta versiones de Ficha y, en la misma
# transacción, escribe su proyección en `pieza`; una segunda dejaría una Pieza que diverge.
INSERTA_VERSION = re.compile(r"INSERT\s+INTO\s+acervo\.ficha\b", re.I)


# Y el `UPDATE` de las columnas que proyectan la Ficha va con ese `INSERT`: en otra función, la
# Pieza cambia sin Ficha nueva. Las demás columnas de `pieza` las puede escribir cualquiera.
ESCRIBE_PROYECCION = re.compile(
    rf"UPDATE\s+acervo\.pieza\s+SET\b(?:(?!\bWHERE\b).)*?\b({'|'.join(PROYECCION.values())})\s*=",
    re.I | re.S,
)


def funciones_con(patron: re.Pattern[str], paquete: Path) -> list[str]:
    """`archivo:función` de cada aparición de `patron`, con la función más interna."""
    halladas = []
    for ruta in sorted(paquete.rglob("*.py")):
        texto = ruta.read_text(encoding="utf-8")
        funciones = [
            nodo
            for nodo in ast.walk(ast.parse(texto))
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for hallazgo in patron.finditer(texto):
            linea = texto.count("\n", 0, hallazgo.start()) + 1
            duenas = [f for f in funciones if f.lineno <= linea <= (f.end_lineno or f.lineno)]
            duena = min(duenas, key=lambda f: (f.end_lineno or f.lineno) - f.lineno, default=None)
            nombre = "<módulo>" if duena is None else duena.name
            halladas.append(f"{ruta.relative_to(paquete).as_posix()}:{nombre}")
    return halladas


@pytest.mark.rnf("RNF-09")
def test_una_sola_funcion_inserta_versiones_de_ficha():
    assert funciones_con(INSERTA_VERSION, PAQUETE) == ["fichas/postgres.py:_insertar_version"]


@pytest.mark.rnf("RNF-09")
def test_una_segunda_insercion_de_versiones_se_detecta(tmp_path: Path):
    (tmp_path / "otro.py").write_text(
        "def guardar(tx):\n"
        "    def adentro():\n"
        '        tx.execute("insert into acervo.ficha (pieza_id) VALUES (%s)")\n',
        encoding="utf-8",
    )

    assert funciones_con(INSERTA_VERSION, tmp_path) == ["otro.py:adentro"]


@pytest.mark.rnf("RNF-09")
def test_una_sola_funcion_escribe_la_proyeccion_en_pieza():
    # 152: con la Pieza Recibida no hay Ficha vigente de la que diverger; `completar_en_recibida`
    # escribe los campos duros en la fila, como la herencia del Lote en `crear_pieza`, y su
    # `UPDATE` exige `estado = Recibida`.
    assert funciones_con(ESCRIBE_PROYECCION, PAQUETE) == [
        "fichas/postgres.py:completar_en_recibida", "fichas/postgres.py:_insertar_version"
    ]


@pytest.mark.rnf("RNF-09")
def test_otra_escritura_de_la_proyeccion_se_detecta(tmp_path: Path):
    (tmp_path / "otro.py").write_text(
        "def marcar(tx):\n"
        '    tx.execute("UPDATE acervo.pieza SET borrada_el = now() WHERE estado = %s")\n'
        "def reabrir(tx):\n"
        '    tx.execute("UPDATE acervo.pieza SET borrada_el = NULL,"\n'
        '               " estado = %s WHERE pieza_id = %s")\n',
        encoding="utf-8",
    )

    assert funciones_con(ESCRIBE_PROYECCION, tmp_path) == ["otro.py:reabrir"]


# 024 (DEC-143): las Carreras vigentes son proyección del frontmatter; se escriben junto con la
# versión de la Ficha, nunca aparte.
ESCRIBE_PIEZA_CARRERA = re.compile(r"(INSERT\s+INTO|UPDATE)\s+(acervo\.)?pieza_carrera\b", re.I)


@pytest.mark.rf("RF-50")
def test_solo_la_funcion_que_inserta_versiones_escribe_pieza_carrera():
    escritoras = set(funciones_con(ESCRIBE_PIEZA_CARRERA, PAQUETE))

    # 082 (spec §1.19): la Pieza de un Lote nace con sus Carreras, como con el origen y la
    # Fecha; todavía `Recibida`, no hay versión de la que diverger.
    # 152: la escritura vive en `_escribir_carreras`, que llaman `_insertar_version` y, con la
    # Pieza Recibida, `completar_en_recibida`.
    assert escritoras == {"fichas/postgres.py:_escribir_carreras", "fichas/postgres.py:crear_pieza"}


# 152: `_escribir_carreras` es la escritura de `pieza_carrera` fuera de `crear_pieza`; llamarla
# desde otro lado escribiría Carreras sin versión nueva y sin exigir `Recibida`.
LLAMA_A_ESCRIBIR_CARRERAS = re.compile(r"(?<!def )\b_escribir_carreras\(")


@pytest.mark.rf("RF-50")
def test_escribir_carreras_la_llaman_solo_la_version_y_la_recibida():
    assert set(funciones_con(LLAMA_A_ESCRIBIR_CARRERAS, PAQUETE)) == {
        "fichas/postgres.py:_insertar_version", "fichas/postgres.py:completar_en_recibida"
    }


@pytest.mark.rf("RF-50")
def test_otra_llamada_a_escribir_carreras_se_detecta(tmp_path: Path):
    (tmp_path / "otro.py").write_text(
        "def _escribir_carreras(tx, pieza_id, carreras):\n"
        "    pass\n"
        "def colar(tx):\n"
        "    _escribir_carreras(tx, None, [])\n",
        encoding="utf-8",
    )

    assert funciones_con(LLAMA_A_ESCRIBIR_CARRERAS, tmp_path) == ["otro.py:colar"]


# 019 (DEC-26): la función que inserta versiones corre el chequeo; con la columna sin default,
# ningún camino (propuesta, edición, aporte, planilla, video) guarda una versión sin su informe.
# 101 (DEC-133): la otra es la que rechaza un Aporte con etiquetas de control antes de guardar
# nada; no deja informe ni alerta. 146: la tercera revisa el resumen rehecho, que es una propuesta
# y no una versión; al guardarlo en una versión vuelve a pasar por `_insertar_version`.
CORRE_EL_CHEQUEO = re.compile(r"(?<!def )\brevisar\(")


@pytest.mark.rf("RF-10")
def test_el_chequeo_de_inyeccion_corre_en_la_funcion_que_inserta_versiones():
    # 148: y la alerta de las Afirmaciones de una imagen embebida, que llegan sin versión nueva.
    assert funciones_con(CORRE_EL_CHEQUEO, PAQUETE / "fichas") == [
        "postgres.py:guardar_resumen_rehecho",
        "postgres.py:etiquetas_de_control",
        "postgres.py:alerta_de_afirmaciones",
        "postgres.py:_insertar_version",
    ]


# 039 (RNF-21): los encabezados de seguridad los pone un solo middleware; otra ruta que los
# fijara podría aflojar la CSP sin que se note.
ENCABEZADO_DE_SEGURIDAD = re.compile(
    r"Content-Security-Policy|Referrer-Policy|Permissions-Policy"
    r"|Cross-Origin-(Opener|Resource)-Policy",
    re.I,
)


def fijan_encabezados_de_seguridad(paquete: Path) -> list[str]:
    return sorted(
        ruta.relative_to(paquete).as_posix()
        for ruta in paquete.rglob("*.py")
        if ruta.relative_to(paquete).as_posix() != "api/cabeceras.py"
        and ENCABEZADO_DE_SEGURIDAD.search(ruta.read_text(encoding="utf-8"))
    )


@pytest.mark.rnf("RNF-21")
def test_solo_api_cabeceras_fija_los_encabezados_de_seguridad(tmp_path: Path):
    for modulo in ("api", "api_agentes"):
        (tmp_path / modulo).mkdir()
    (tmp_path / "api" / "cabeceras.py").write_text('"Referrer-Policy"\n', encoding="utf-8")
    (tmp_path / "api_agentes" / "v1.py").write_text(
        'respuesta.headers["Content-Security-Policy"] = "default-src *"\n', encoding="utf-8"
    )

    assert (PAQUETE / "api" / "cabeceras.py").is_file()
    assert fijan_encabezados_de_seguridad(PAQUETE) == []
    assert fijan_encabezados_de_seguridad(tmp_path) == ["api_agentes/v1.py"]


# 076 (R-39, DEC-132): fuera del código, un nombre de modelo vive solo en la semilla y en la lista
# de candidatos del banco; en un `.py` sigue sin poder estar en ningún lado (regla de 013).
NOMBRE_DE_MODELO = next(
    r.patron for r in REGLAS if r.nombre == "nombres_de_modelos_solo_en_la_semilla"
)
LISTA_DE_CANDIDATOS = "banco/candidatos.csv"


def nombran_modelos_fuera_de_la_semilla(paquete: Path) -> list[str]:
    return sorted(
        nombre
        for ruta in paquete.rglob("*")
        if ruta.is_file() and ruta.suffix not in (".py", ".pyc")
        and not (nombre := ruta.relative_to(paquete).as_posix()).startswith("migraciones/sql/")
        and nombre != LISTA_DE_CANDIDATOS
        and NOMBRE_DE_MODELO.search(ruta.read_text(encoding="utf-8", errors="ignore"))
    )


@pytest.mark.rf("RF-28")
def test_los_nombres_de_modelos_solo_en_la_semilla_y_en_la_lista_de_candidatos(tmp_path: Path):
    for archivo in ("migraciones/sql/0001.sql", LISTA_DE_CANDIDATOS, "web/plantilla.html"):
        (tmp_path / archivo).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / archivo).write_text("resumir,llama3:8b,false,\n", encoding="utf-8")

    assert NOMBRE_DE_MODELO.search((PAQUETE / LISTA_DE_CANDIDATOS).read_text(encoding="utf-8"))
    assert nombran_modelos_fuera_de_la_semilla(PAQUETE) == []
    assert nombran_modelos_fuera_de_la_semilla(tmp_path) == ["web/plantilla.html"]


# 078 (R-14): el banco de `responder` verifica las Citas y lee «sin evidencia» con las mismas
# funciones que la Conversación; una copia podría dar por buena una Cita que la pantalla tacha.
@pytest.mark.rf("RF-28")
def test_el_banco_de_responder_verifica_citas_con_las_funciones_de_la_conversacion():
    from historia_viva import conversacion
    from historia_viva.banco import comparacion
    from historia_viva.dominio import citas

    assert comparacion.verificar_citas is citas.verificar_citas is conversacion.verificar_citas
    assert comparacion.declara_sin_evidencia is conversacion.declara_sin_evidencia
    definen = sorted(
        ruta.relative_to(PAQUETE).as_posix()
        for ruta in PAQUETE.rglob("*.py")
        if re.search(r"^\s*def verificar_citas\b", ruta.read_text(encoding="utf-8"), re.M)
    )
    assert definen == ["dominio/citas.py"]


# 137 (DEC-132): el banco de ingesta puntúa con la misma `puntuar` que `banco comparar` y arma el
# cuerpo de una foto con la `cuerpo_de_la_foto` de `procesamiento`; no le pide a un modelo que
# juzgue la salida (ni `resumir` ni `responder`).
@pytest.mark.rf("RF-28")
def test_el_banco_de_ingesta_mide_como_produccion_y_sin_modelo_juez():
    from historia_viva.banco import comparacion, ingesta
    from historia_viva.procesamiento import manejador

    assert ingesta.puntuar is comparacion.puntuar
    assert ingesta.cuerpo_de_la_foto is manejador.cuerpo_de_la_foto
    fuente = (PAQUETE / "banco" / "ingesta.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(resumir|responder|responder_en_vivo)\(", fuente)


# 140 (RF-07): el OCR queda detrás de `MotorOcr`; Tesseract se importa solo en `capa_modelos/ocr`,
# así otro motor entra sin tocar `transcribir`.
@pytest.mark.rf("RF-07")
def test_tesseract_se_importa_solo_en_el_modulo_de_ocr():
    importan = sorted(
        ruta.relative_to(PAQUETE).as_posix()
        for ruta in PAQUETE.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+pytesseract\b", ruta.read_text(encoding="utf-8"), re.M)
    )
    assert importan == ["capa_modelos/ocr.py"]


# 143 (RF-07): las imágenes embebidas de un PDF y un Word se sacan solo en `capa_modelos/imagenes`
# (las de la planilla, en `planillas`, dueño del `.xlsx`), y el OCR lo llaman solo `transcribir` y
# ese módulo: así el texto de cada imagen pasa por `escapar_marcas` y entra bajo su marca.
@pytest.mark.rf("RF-07")
def test_las_imagenes_embebidas_se_sacan_y_se_leen_en_un_solo_lugar():
    def usan(patron: str) -> list[str]:
        return sorted(
            ruta.relative_to(PAQUETE).as_posix()
            for ruta in PAQUETE.rglob("*.py")
            if re.search(patron, ruta.read_text(encoding="utf-8"))
        )

    assert usan(r"\bget_image_info\(|\bextract_image\(|[\"']a:blip[\"']") == [
        "capa_modelos/imagenes.py"
    ]
    assert usan(r"\bocr\.leer\(") == ["capa_modelos/imagenes.py", "capa_modelos/local.py"]
