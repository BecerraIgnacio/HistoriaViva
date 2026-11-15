-- 058: quién pidió «Generar ahora». El planificador lo anota como `generada_por` de lo que
-- genera ese pedido (`origen_generacion = 'manual'`, spec §1.21), y lo deja nulo con el resto
-- del pedido al terminar.

ALTER TABLE acervo.programacion_efemerides
    ADD COLUMN pedido_manual_por uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    ADD CONSTRAINT programacion_pedido_con_quien
        CHECK ((pedido_manual_el IS NULL) = (pedido_manual_por IS NULL));

GRANT UPDATE (pedido_manual_por) ON acervo.programacion_efemerides TO hv_app;
