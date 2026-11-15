-- 013: las tres capacidades de R1 corren con su fila (RF-32, DEC-87).
--
-- Los nombres de modelos viven acá, no en el código. `embeber`, `responder` y
-- `redactar_efemeride` las siembran sus tickets (045, 052, 057). `num_ctx` = tokens de entrada
-- + de salida + 1024 para la instrucción; `describir_imagen` no acota la entrada y cuenta 4096
-- para la imagen.
-- `num_thread` no va: lo elige Ollama hasta que el Administrador técnico lo fije en la fila.
-- Temperatura baja: una propuesta de catalogación no gana nada con variedad y pierde
-- reproducibilidad.
INSERT INTO acervo.configuracion_capacidad (capacidad, motor, modelo, instruccion_id, parametros)
VALUES
    -- Elegido en el banco de visión del 7-oct: el único que no inventa una escena ante una
    -- página impresa. La variante `-instruct`, porque la otra razona antes de responder.
    ('describir_imagen', 'ollama', 'qwen3-vl:2b-instruct', 'describir_fotografia',
     '{"temperatura": 0.2, "num_ctx": 5632, "max_tokens_salida": 512, "timeout_s": 180}'),
    ('transcribir', 'local', NULL, NULL,
     '{"timeout_s": 300}'),
    ('resumir', 'ollama', 'qwen2.5:3b-instruct-q4_K_M', 'resumir_ficha',
     '{"temperatura": 0.2, "num_ctx": 9324, "max_tokens_entrada": 8000, "max_tokens_salida": 300, "timeout_s": 90}');
