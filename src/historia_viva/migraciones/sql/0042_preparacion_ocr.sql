-- 140: la preparación de la imagen y el umbral de confianza del OCR, explícitos en la fila de
-- `transcribir` (RF-07, DEC-132). Así quedan en la configuración efectiva de cada trabajo y en el
-- informe del banco de ingesta, y se cambian sin tocar código. Qué hace cada uno y cómo se midió
-- está en `capa_modelos/ocr.py`: gris, escala ×2, inversión si el fondo es oscuro, sin
-- binarización y sin umbral (cada umbral probado subió el error del corpus del banco).
-- Lo que la fila ya tenga gana: no pisa un cambio del Administrador técnico.
UPDATE acervo.configuracion_capacidad
   SET parametros = '{"ocr_escala": 2, "ocr_invertir": true, "ocr_binarizar": false,
                      "ocr_confianza_minima": 0}'::jsonb || parametros,
       version = version + 1,
       actualizada_el = now()
 WHERE capacidad = 'transcribir' AND motor = 'local';
