-- 145 (ADR-0007): cada Afirmación se cruza con las palabras del OCR al procesar la Pieza. Queda
-- respaldada solo si todos sus nombres propios, siglas, números y fechas están en lo leído, y
-- guarda dónde: una región por dato, en píxeles de la imagen como la muestra el navegador, con lo
-- que leyó ahí el OCR (`dominio.cruce.Region`). NULL: sin respaldo, también en las Afirmaciones
-- anteriores a 145, que nadie cruzó. El cruce no llama a ningún modelo.
ALTER TABLE acervo.afirmacion
    ADD COLUMN respaldo jsonb CHECK (
        respaldo IS NULL OR (jsonb_typeof(respaldo) = 'array' AND jsonb_array_length(respaldo) > 0)
    );

COMMENT ON COLUMN acervo.afirmacion.respaldo IS
    'Dónde están en la imagen los datos de la Afirmación según el OCR (145). NULL: sin respaldo.';
