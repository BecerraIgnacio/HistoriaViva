-- 019: la alerta de inyección de la Ficha (ADR-0003 capa 2, DEC-26). Del spec: la 0012
-- (`ficha_origen_e_informe`) y la columna `alerta_inyeccion` de la 0007.
--
-- `informe_inyeccion` queda sin default: un INSERT que lo omita falla, así ningún camino
-- guarda una versión sin pasar por `chequeo_inyeccion`. El default solo llena las filas que
-- ya existían.

ALTER TABLE acervo.ficha DROP CONSTRAINT ficha_origen_version_check;
ALTER TABLE acervo.ficha ADD CONSTRAINT ficha_origen_version_check
    CHECK (origen_version IN ('modelo','persona','agente_externo','sistema'));

ALTER TABLE acervo.ficha
    ADD COLUMN informe_inyeccion jsonb NOT NULL
        DEFAULT '{"alerta":false,"version_catalogo":"0","hallazgos":[]}';
ALTER TABLE acervo.ficha ALTER COLUMN informe_inyeccion DROP DEFAULT;

COMMENT ON COLUMN acervo.ficha.origen_version IS
    'modelo: propuesta, sin firmar. persona: firmada (ADR-0002). agente_externo: v1 de un Aporte. sistema: v1 vacía de un video o de «Completar a mano» (DEC-122).';
COMMENT ON COLUMN acervo.ficha.informe_inyeccion IS
    'Lo que encontró chequeo_inyeccion en esta versión: {version_catalogo, alerta, hallazgos}. Lo escribe la única función que inserta versiones (DEC-26).';

ALTER TABLE acervo.pieza ADD COLUMN alerta_inyeccion boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN acervo.pieza.alerta_inyeccion IS
    'Proyección del informe de la Ficha vigente (DEC-26). Avisa, no bloquea la firma.';
