-- 153: el `.doc` y el `.xls` entran y el procesamiento los lee convertidos (DEC-41 revisada).
--
-- La Pieza guarda el MIME de su original, que se conserva tal como se subió (RF-06): un documento
-- puede ser `application/msword` y una planilla `application/vnd.ms-excel`. El HEIC es
-- `image/heic` y ya entraba en `image/%`.

ALTER TABLE acervo.pieza
    DROP CONSTRAINT pieza_clase_mime,
    ADD CONSTRAINT pieza_clase_mime CHECK (CASE clase_original
        WHEN 'imagen' THEN tipo_mime LIKE 'image/%'
        WHEN 'documento' THEN tipo_mime IN (
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'application/msword')
        WHEN 'planilla' THEN split_part(tipo_mime, ';', 1) IN (
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/vnd.ms-excel',
            'text/csv')
        WHEN 'video' THEN tipo_mime IN ('video/mp4','video/quicktime','video/webm')
        WHEN 'texto' THEN split_part(tipo_mime, ';', 1) = 'text/plain'
    END);
