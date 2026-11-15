-- 151: subir sin crear el Lote antes. Arrastrar varios archivos crea el Lote con un nombre provisorio
-- y sin Origen ni Fecha, que se piden después, mientras las Piezas se procesan. La Fecha va entera o
-- no va (como en la Pieza). Ajusta DEC-113: la Fecha del Lote deja de ser obligatoria al crearlo.
ALTER TABLE acervo.lote
    ALTER COLUMN origen DROP NOT NULL,
    ALTER COLUMN fecha_inicio DROP NOT NULL,
    ALTER COLUMN fecha_fin DROP NOT NULL,
    ALTER COLUMN precision_fecha DROP NOT NULL,
    ADD CONSTRAINT lote_fecha_entera CHECK (
        (fecha_inicio IS NULL) = (fecha_fin IS NULL)
        AND (fecha_inicio IS NULL) = (precision_fecha IS NULL)
    );

-- Completar el Contexto tarde copia lo nuevo a las Pendientes con una versión `sistema` (151): una
-- Pieza que leyó un modelo o una persona no pasa por eso a «Requiere análisis humano». Lo requiere
-- la Pieza cuyas versiones son todas `sistema` (un video, una completada a mano; 034).
CREATE OR REPLACE VIEW acervo.v_lote_resumen AS
SELECT
    l.lote_id,
    l.nombre,
    count(p.pieza_id)                                   AS n_piezas,
    count(*) FILTER (WHERE p.estado = 'Recibida')       AS n_recibida,
    count(*) FILTER (WHERE p.estado = 'Pendiente')      AS n_pendiente,
    count(*) FILTER (WHERE p.estado = 'Incompleta')     AS n_incompleta,
    count(*) FILTER (WHERE p.estado = 'Validada')       AS n_validada,
    count(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM acervo.ficha f WHERE f.pieza_id = p.pieza_id
    ) AND NOT EXISTS (
        SELECT 1 FROM acervo.ficha f
        WHERE f.pieza_id = p.pieza_id AND f.origen_version <> 'sistema'
    ))                                                  AS n_analisis_humano
FROM acervo.lote l
LEFT JOIN acervo.pieza p ON p.lote_id = l.lote_id AND p.borrada_el IS NULL
GROUP BY l.lote_id;
