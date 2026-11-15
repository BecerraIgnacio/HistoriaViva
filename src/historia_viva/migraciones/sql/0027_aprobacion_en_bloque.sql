-- 108: la aprobación en bloque (spec §1.17, la 0015 del spec; DEC-22, DEC-135, DEC-146 puntos 8
-- y 14). «Entra; se activa con C29»: la tabla existe aunque `HV_BLOQUE_HABILITADO` esté en falso,
-- así activarla no cambia el esquema.
--
-- La previa abre la fila con el reloj del servidor; la confirmación la cierra con los conteos y la
-- base deriva `duracion_s`, la medición del bloque, que no pasa por `medicion_validacion` (el
-- `CHECK` de su modo ya admite solo `individual`, 0020). Se aparta del spec en `clase_original` y
-- `vistas`: la confirmación reevalúa contra la clase que eligió la previa y firma solo las
-- elegibles que la previa mostró, en la versión que mostró; la que no figura sigue Pendiente.

CREATE TABLE acervo.aprobacion_en_bloque (
    aprobacion_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lote_id        uuid NOT NULL REFERENCES acervo.lote (lote_id) ON DELETE RESTRICT,
    clase_original text NOT NULL
                       CHECK (clase_original IN ('imagen','documento','planilla','video','texto')),
    abierta_por    uuid NOT NULL REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    abierta_el     timestamptz NOT NULL,
    vistas         jsonb NOT NULL CHECK (jsonb_typeof(vistas) = 'object'),
    confirmada_el  timestamptz,
    n_aprobadas    integer CHECK (n_aprobadas >= 0),
    n_rechazadas   integer CHECK (n_rechazadas >= 0),
    duracion_s     integer GENERATED ALWAYS AS
                       (date_part('epoch', confirmada_el - abierta_el)::integer) STORED,
    CONSTRAINT aprobacion_orden_temporal CHECK (confirmada_el IS NULL OR confirmada_el >= abierta_el),
    CONSTRAINT aprobacion_conteos_al_confirmar CHECK (
        (confirmada_el IS NULL) = (n_aprobadas IS NULL)
        AND (confirmada_el IS NULL) = (n_rechazadas IS NULL)
    )
);

COMMENT ON COLUMN acervo.aprobacion_en_bloque.vistas IS
    'Las elegibles de la previa, {pieza_id: versión}: la confirmación firma solo esas, en esa versión.';
COMMENT ON COLUMN acervo.aprobacion_en_bloque.duracion_s IS
    'Segundos entre la previa y la confirmación: la medición del bloque, aparte del CSV por Pieza.';

-- Las versiones firmadas en bloque dicen de qué aprobación salieron.
ALTER TABLE acervo.ficha
    ADD COLUMN aprobacion_id uuid
        REFERENCES acervo.aprobacion_en_bloque (aprobacion_id) ON DELETE RESTRICT;

GRANT SELECT, INSERT, UPDATE ON acervo.aprobacion_en_bloque TO hv_app;
