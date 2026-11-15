-- 028: una persona sube un `.txt` y es una Pieza de clase `texto` (decisión del usuario, 028).
--
-- Antes la clase `texto` era solo la del Aporte. El `.txt` subido pasa por la cola como un
-- documento (su texto es el cuerpo y se pide el resumen), así que puede estar `Recibida`; el
-- Aporte sigue sin plan y nace Pendiente. `pieza_clase_mime` no cambia: `texto` ↔ `text/plain`.

ALTER TABLE acervo.pieza
    DROP CONSTRAINT pieza_sin_plan_no_recibida,
    -- Lo que no tiene plan de modelos nace Pendiente (DEC-10): el video y el Aporte.
    ADD CONSTRAINT pieza_sin_plan_no_recibida
        CHECK ((clase_original <> 'video' AND via_ingreso <> 'aporte') OR estado <> 'Recibida'),
    DROP CONSTRAINT pieza_aporte_coherente,
    -- Un Aporte es texto y no lo sube una persona; el texto ya no es solo del Aporte.
    ADD CONSTRAINT pieza_aporte_coherente CHECK (
        (via_ingreso <> 'aporte' OR clase_original = 'texto')
        AND (via_ingreso <> 'aporte' OR creada_por IS NULL)
    );
