-- 143: los topes de las imágenes embebidas de un PDF, un Word o una planilla, explícitos en la fila
-- de `transcribir` (RF-07, ADR-0004): cuántas pasan por el OCR en una Pieza y cuánto tiempo hay
-- para leerlas, dentro del tope total de `transcribir`. Al llegar a cualquiera se corta con aviso
-- en la Ficha. Por qué tienen estos valores está en `capa_modelos/imagenes.py`.
-- Lo que la fila ya tenga gana: no pisa un cambio del Administrador técnico.
UPDATE acervo.configuracion_capacidad
   SET parametros = '{"imagenes_maximo": 30, "imagenes_tiempo_maximo_s": 120}'::jsonb
                    || parametros,
       version = version + 1,
       actualizada_el = now()
 WHERE capacidad = 'transcribir' AND motor = 'local';
