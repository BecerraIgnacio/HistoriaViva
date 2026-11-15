-- 123: `pieza.publicable` y `replicacion_drive` (spec §1.23, la 0022 del spec; R-26, DEC-146
-- punto 15). Las dos se activan con un tercero: `publicable` con C31 y la réplica con C14/C34.
--
-- `publicable` es proyección del frontmatter de la Ficha vigente (ADR-0001, DEC-143): la escribe
-- solo `fichas`. Falsa, la Pieza no sale por `/v1` ni es fuente de efemérides o Relatos nuevos;
-- las personas la siguen viendo (auditoría, decisión 15 B). Toda Pieza existente queda publicable.

ALTER TABLE acervo.pieza ADD COLUMN publicable boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN acervo.pieza.publicable IS
    'Proyección del frontmatter (DEC-143). Falsa: fuera de /v1 y de efemérides y Relatos nuevos.';

-- Una fila por original. Replicar no cambia el estado de la Pieza; con la réplica apagada la tabla
-- queda vacía. Los estados son los de la máquina de `soporte/investigacion/07-drive.md` (DRV-14).
CREATE TABLE acervo.replicacion_drive (
    pieza_id        uuid PRIMARY KEY REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    estado          text NOT NULL DEFAULT 'pendiente'
                        CHECK (estado IN ('pendiente','subiendo','replicado','fallido')),
    drive_file_id   text,
    intentos        integer NOT NULL DEFAULT 0 CHECK (intentos >= 0),
    proximo_intento timestamptz,
    ultimo_error    text,
    -- Arriendo de 30 min renovable por trozo.
    arrendado_hasta timestamptz,
    replicado_el    timestamptz,
    CONSTRAINT replicacion_drive_replicado_con_archivo
        CHECK ((estado = 'replicado') = (drive_file_id IS NOT NULL))
);

CREATE INDEX replicacion_cola ON acervo.replicacion_drive (proximo_intento)
    WHERE estado IN ('pendiente','fallido');

-- Sin DELETE (ADR-0004, control 1).
GRANT SELECT, INSERT, UPDATE ON acervo.replicacion_drive TO hv_app;
