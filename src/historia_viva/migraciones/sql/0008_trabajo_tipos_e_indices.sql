-- 029: la cola por tipo (spec §1.9, la 0005 del spec sin `latido_trabajador`, que es de 035).
--
-- Dos tipos de trabajo sobre la misma tabla (DEC-08), generaciones contadas al tomar con tope 2
-- (DEC-30), índices únicos parciales por tipo (DEC-09) y la toma ordenada por prioridad y
-- llegada (DEC-25). Lo que no tiene plan no entra a la cola: lo corta la base, no un `if`.

ALTER TABLE acervo.trabajo_procesamiento
    ADD COLUMN ficha_id uuid REFERENCES acervo.ficha (ficha_id) ON DELETE RESTRICT,
    ADD COLUMN prioridad smallint NOT NULL DEFAULT 0,
    ADD COLUMN contexto_lote jsonb,
    ADD COLUMN solicitado_por uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    -- Una generación se cuenta al tomar: 2 es un intento más la recuperación de un arriendo.
    ALTER COLUMN tope_generaciones SET DEFAULT 2,
    ADD CONSTRAINT trabajo_ficha_solo_indexacion
        CHECK ((tipo = 'indexacion') = (ficha_id IS NOT NULL));

-- Los activos de antes nacieron con tope 1: con la generación contada al tomar, un `encolado`
-- que ya se tomó una vez quedaría `fallido` sin volver a correr.
UPDATE acervo.trabajo_procesamiento SET tope_generaciones = 2
    WHERE estado IN ('encolado','en_curso') AND tope_generaciones < 2;

COMMENT ON COLUMN acervo.trabajo_procesamiento.ficha_id IS
    'La versión que indexa un trabajo de indexación: si la vigente ya es otra, el trabajo es viejo.';
COMMENT ON COLUMN acervo.trabajo_procesamiento.prioridad IS
    '0 = Pieza suelta o reproceso individual; 1 = Pieza de Lote. Se toma por (prioridad, encolado_el) (DEC-25).';
COMMENT ON COLUMN acervo.trabajo_procesamiento.contexto_lote IS
    'Contexto del Lote congelado al encolar (DEC-19); solo en el procesamiento de una Pieza con Lote.';
COMMENT ON COLUMN acervo.trabajo_procesamiento.solicitado_por IS
    'Quién pidió un reproceso manual (tope de 6 por Pieza, DEC-137).';
COMMENT ON COLUMN acervo.trabajo_procesamiento.generaciones IS
    'Se suma al tomar el trabajo; la recuperación de un arriendo vencido no suma (DEC-146 punto 2).';

-- Un procesamiento por Pieza (también completado); una indexación activa, reindexar es legítimo.
DROP INDEX acervo.trabajo_idempotente_por_pieza;
CREATE UNIQUE INDEX trabajo_procesamiento_unico ON acervo.trabajo_procesamiento (pieza_id)
    WHERE tipo = 'procesamiento' AND estado IN ('encolado','en_curso','completado');
CREATE UNIQUE INDEX trabajo_indexacion_activo ON acervo.trabajo_procesamiento (pieza_id)
    WHERE tipo = 'indexacion' AND estado IN ('encolado','en_curso');

DROP INDEX acervo.trabajo_cola;
CREATE INDEX trabajo_cola ON acervo.trabajo_procesamiento (tipo, prioridad, encolado_el, trabajo_id)
    WHERE estado = 'encolado';
CREATE INDEX trabajo_arriendo ON acervo.trabajo_procesamiento (arriendo_vence_el)
    WHERE estado = 'en_curso';

-- El video y el Aporte no tienen plan de modelos: nacen Pendientes y nunca se procesan.
CREATE FUNCTION acervo.trabajo_sin_plan() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tipo = 'procesamiento' AND EXISTS (
        SELECT 1 FROM acervo.pieza
        WHERE pieza_id = NEW.pieza_id AND (clase_original = 'video' OR via_ingreso = 'aporte')
    ) THEN
        RAISE EXCEPTION 'trabajo_sin_plan: la Pieza % no tiene plan de procesamiento', NEW.pieza_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trabajo_sin_plan BEFORE INSERT ON acervo.trabajo_procesamiento
    FOR EACH ROW EXECUTE FUNCTION acervo.trabajo_sin_plan();
