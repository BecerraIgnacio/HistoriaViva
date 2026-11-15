-- 026: el Lote mínimo (spec §1.5 y la 0003 del spec, sin `propietario` ni `lote_carrera`, que
-- son de 082). Sin columna de estado: se deriva de sus Piezas (DEC-18). El tope de 100 Piezas
-- queda en la aplicación, con el Lote bloqueado (R-27).

CREATE TABLE acervo.lote (
    lote_id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre          text        NOT NULL CHECK (btrim(nombre) <> ''),
    origen          text        NOT NULL CHECK (btrim(origen) <> ''),
    -- La Fecha es obligatoria en el Lote (DEC-113), con las mismas reglas que en la Pieza.
    fecha_inicio    date        NOT NULL,
    fecha_fin       date        NOT NULL,
    precision_fecha text        NOT NULL
        CHECK (precision_fecha IN ('dia','mes','anio','decada','rango')),
    evento          text,
    lugar           text,
    creado_por      uuid        NOT NULL REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    creado_el       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT lote_fechas_ordenadas CHECK (fecha_fin >= fecha_inicio),
    CONSTRAINT lote_precision_coherente CHECK (
        (precision_fecha = 'dia'
            AND fecha_inicio = fecha_fin)
        OR (precision_fecha = 'mes'
            AND date_part('day', fecha_inicio) = 1
            AND date_part('year', fecha_inicio) = date_part('year', fecha_fin)
            AND date_part('month', fecha_inicio) = date_part('month', fecha_fin)
            AND date_part('month', fecha_fin + 1) <> date_part('month', fecha_fin))
        OR (precision_fecha = 'anio'
            AND date_part('month', fecha_inicio) = 1 AND date_part('day', fecha_inicio) = 1
            AND date_part('month', fecha_fin) = 12  AND date_part('day', fecha_fin) = 31
            AND date_part('year', fecha_inicio) = date_part('year', fecha_fin))
        OR (precision_fecha = 'decada'
            AND date_part('month', fecha_inicio) = 1 AND date_part('day', fecha_inicio) = 1
            AND date_part('month', fecha_fin) = 12  AND date_part('day', fecha_fin) = 31
            AND date_part('year', fecha_inicio)::int % 10 = 0
            AND date_part('year', fecha_fin)::int = date_part('year', fecha_inicio)::int + 9)
        OR (precision_fecha = 'rango'
            AND fecha_fin >= fecha_inicio)
    )
);

CREATE INDEX lote_por_creador ON acervo.lote (creado_por, creado_el DESC);

-- La herencia es una copia, no una referencia: la Ficha tiene que ser autocontenida (ADR-0001).
ALTER TABLE acervo.pieza
    ADD COLUMN lote_id uuid REFERENCES acervo.lote (lote_id) ON DELETE RESTRICT,
    ADD COLUMN campos_heredados text[] NOT NULL DEFAULT '{}',
    ADD COLUMN contexto_heredado text;

COMMENT ON COLUMN acervo.pieza.lote_id IS 'Nulo = Pieza suelta.';
COMMENT ON COLUMN acervo.pieza.campos_heredados IS
    'Cuáles de origen, fecha_inicio, fecha_fin y precision_fecha vinieron del Lote (DEC-126).';
COMMENT ON COLUMN acervo.pieza.contexto_heredado IS
    'Contexto precargado del Lote (líneas Evento: y Lugar:) hasta la v1, que lo pone en su Contexto (DEC-113).';

CREATE INDEX pieza_lote ON acervo.pieza (lote_id) WHERE lote_id IS NOT NULL;

-- UPDATE también lo pide el `SELECT ... FOR UPDATE` con que se cuenta el tope.
GRANT SELECT, INSERT, UPDATE ON acervo.lote TO hv_app;
