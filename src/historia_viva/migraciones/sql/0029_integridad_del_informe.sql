-- 067: la integridad del informe se recalcula al exportar (spec, módulo 33; DEC-128, R-21).
--
-- Se calcula al generar y otra vez al exportar, que la escribe en el documento: `hv_app` la
-- actualiza también en un Aprobado. No es contenido firmado: la huella no la incluye.

GRANT UPDATE (integridad) ON acervo.informe TO hv_app;
