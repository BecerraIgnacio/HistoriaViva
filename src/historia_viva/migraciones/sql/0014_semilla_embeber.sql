-- 045: `embeber` corre en el segundo Ollama (DEC-06), con su fila (RF-32). En el spec, la 0008.
--
-- `bge-m3` es provisorio (DEC-03): multilingüe y de 1024 dimensiones nativas, las de la columna
-- del índice; el definitivo lo elige el banco (077). Sin prefijos: `bge-m3` no los usa (un e5
-- llevaría `passage: ` y `query: `). Sin instrucción: un modelo de embeddings no la tiene.
-- `timeout_s` es por lote de 16 textos y cuenta la carga en frío.
INSERT INTO acervo.configuracion_capacidad (capacidad, motor, modelo, instruccion_id, parametros)
VALUES
    ('embeber', 'ollama_embeddings', 'bge-m3', NULL,
     '{"dimension": 1024, "prefijo_fragmento": "", "prefijo_consulta": "", "timeout_s": 120}');
