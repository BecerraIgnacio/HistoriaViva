-- =====================================================================
-- Historia Viva -- MVP. Migración 0002: permisos de `hv_app`.
--
-- GRANT tabla por tabla, sin ALTER DEFAULT PRIVILEGES: concedería en
-- silencio lo que se olvidó revocar. Cada migración posterior concede los
-- de sus propias tablas. `hv_app` no borra nada (ADR-0004, DEC-31) ni
-- tiene permiso alguno sobre `schema_migracion`.
-- =====================================================================

GRANT USAGE ON SCHEMA acervo TO hv_app;

GRANT SELECT, INSERT, UPDATE ON acervo.usuario               TO hv_app;
GRANT SELECT, INSERT, UPDATE ON acervo.sesion                TO hv_app;
GRANT SELECT, INSERT, UPDATE ON acervo.pieza                 TO hv_app;
GRANT SELECT, INSERT, UPDATE ON acervo.trabajo_procesamiento TO hv_app;
GRANT SELECT, INSERT, UPDATE ON acervo.medicion_validacion   TO hv_app;

-- Una versión de Ficha no cambia: lo único que se toca es que deje de ser
-- la vigente. El trigger `ficha_inmutable` es la segunda capa (RNF-07).
GRANT SELECT, INSERT ON acervo.ficha TO hv_app;
GRANT UPDATE (vigente) ON acervo.ficha TO hv_app;

-- Las filas las siembra una migración; la aplicación solo las edita.
GRANT SELECT, UPDATE ON acervo.configuracion_capacidad TO hv_app;

-- Los tipos de Pieza los agrega `hv_migrador` (DEC-105).
GRANT SELECT ON acervo.tipo_pieza TO hv_app;

-- Append-only: ni la propia aplicación reescribe su historial (RF-45).
GRANT SELECT, INSERT ON acervo.auditoria TO hv_app;

REVOKE DELETE, TRUNCATE, REFERENCES ON
    acervo.usuario, acervo.sesion, acervo.tipo_pieza, acervo.pieza, acervo.ficha,
    acervo.trabajo_procesamiento, acervo.medicion_validacion,
    acervo.configuracion_capacidad, acervo.auditoria, acervo.schema_migracion
FROM hv_app;
