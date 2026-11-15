-- 084: la 0006 del spec (`v_lote_resumen`). Una fila por Lote con los conteos de sus Piezas no
-- retiradas (decisión 12 de la auditoría: una retirada no cuenta). No calcula el estado: lo
-- deriva `dominio.estado_de_lote` sobre estos conteos, y las fallidas las da la cola (DEC-110).
-- La lee solo `fichas`. Suma al spec `n_analisis_humano`: Ficha vigente `sistema` (034).

CREATE VIEW acervo.v_lote_resumen AS
SELECT
    l.lote_id,
    l.nombre,
    count(p.pieza_id)                                   AS n_piezas,
    count(*) FILTER (WHERE p.estado = 'Recibida')       AS n_recibida,
    count(*) FILTER (WHERE p.estado = 'Pendiente')      AS n_pendiente,
    count(*) FILTER (WHERE p.estado = 'Incompleta')     AS n_incompleta,
    count(*) FILTER (WHERE p.estado = 'Validada')       AS n_validada,
    count(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM acervo.ficha f
        WHERE f.pieza_id = p.pieza_id AND f.vigente AND f.origen_version = 'sistema'
    ))                                                  AS n_analisis_humano
FROM acervo.lote l
LEFT JOIN acervo.pieza p ON p.lote_id = l.lote_id AND p.borrada_el IS NULL
GROUP BY l.lote_id;

GRANT SELECT ON acervo.v_lote_resumen TO hv_app;
