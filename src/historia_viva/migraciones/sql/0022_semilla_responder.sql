-- 052: `responder` redacta la respuesta de la Conversación en streaming, con su fila (RF-32,
-- R-20). En el spec, parte de la 0019.
--
-- Provisorio hasta el banco de `responder` (078): el modelo de `resumir`, en el mismo Ollama y
-- con el mismo `num_ctx`, para que Ollama no lo recargue al pasar de una capacidad a la otra
-- (DEC-06). `num_ctx` = 7700 de entrada (la pregunta y hasta 5 fragmentos) + 600 de salida +
-- 1024 para la instrucción. `timeout_s` cuenta leer la entrada en CPU, que con 7700 tokens es
-- lo que más tarda.
INSERT INTO acervo.configuracion_capacidad (capacidad, motor, modelo, instruccion_id, parametros)
VALUES
    ('responder', 'ollama', 'qwen2.5:3b-instruct-q4_K_M', 'responder_en_conversacion',
     '{"temperatura": 0.2, "num_ctx": 9324, "max_tokens_entrada": 7700, "max_tokens_salida": 600, "timeout_s": 180}');
