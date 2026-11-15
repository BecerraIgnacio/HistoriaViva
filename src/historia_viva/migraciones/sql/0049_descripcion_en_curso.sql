-- 148: la descripción de una imagen embebida está en curso mientras su trabajo está encolado o en
-- curso. Si el trabajo falló, la imagen sigue `encolada` en `descripcion_de_imagen` pero la firma
-- ya no la espera: la pantalla la muestra fallida y se vuelve a pedir. `fichas` lo lee de esta
-- vista; la cola sigue siendo solo de `procesamiento`.
CREATE VIEW acervo.v_descripcion_en_curso AS
SELECT pieza_id, imagen
FROM acervo.trabajo_procesamiento
WHERE tipo = 'descripcion' AND estado IN ('encolado', 'en_curso');

GRANT SELECT ON acervo.v_descripcion_en_curso TO hv_app;
