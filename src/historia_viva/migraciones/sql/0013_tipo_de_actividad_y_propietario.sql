-- 023: tipo de actividad y propietario, obligatorios para Validar (DEC-126). Del spec: la parte
-- de la 0018 que es de este ticket; `autor` y `pieza_carrera` son de 024, `lote.propietario` y
-- `lote_carrera` de 082, y su `RENAME` ya no hace falta.
--
-- Las dos columnas son proyección del frontmatter de la Ficha vigente (DEC-143): las escribe solo
-- `fichas`. Nulas en Recibida, Pendiente e Incompleta.

ALTER TABLE acervo.pieza
    -- Baja lógica en el catálogo (no hay `DELETE`): la FK no invalida una Ficha firmada.
    ADD COLUMN tipo_actividad text REFERENCES acervo.tipo_actividad (nombre),
    ADD COLUMN propietario text CHECK (propietario IS NULL OR btrim(propietario) <> ''),
    DROP CONSTRAINT pieza_validada_completa,
    -- Los nueve de DEC-126, en su orden; reemplaza a los siete de DEC-13.
    ADD CONSTRAINT pieza_validada_completa CHECK (
        estado <> 'Validada'
        OR (tipo IS NOT NULL AND tipo_actividad IS NOT NULL
            AND fecha_inicio IS NOT NULL AND fecha_fin IS NOT NULL
            AND precision_fecha IS NOT NULL AND origen IS NOT NULL
            AND propietario IS NOT NULL AND resumen IS NOT NULL
            AND archivo_original_id IS NOT NULL)
    );

COMMENT ON COLUMN acervo.pieza.tipo_actividad IS
    'Del catálogo tipo_actividad; «No es una actividad» vale. Obligatorio para Validar (DEC-126).';
COMMENT ON COLUMN acervo.pieza.propietario IS
    'Quién custodia o aportó la Pieza. Obligatorio para Validar; se hereda del Lote (DEC-126).';
