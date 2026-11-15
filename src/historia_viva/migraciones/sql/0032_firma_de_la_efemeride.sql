-- 061: la firma de la efeméride guarda con qué se entró, como la Ficha y la aprobación del
-- informe: lo aprobado con `dev` es una firma de ensayo y no viaja al servidor (DEC-139).

ALTER TABLE acervo.efemeride
    ADD COLUMN metodo_autenticacion text CHECK (metodo_autenticacion IN ('oidc','dev')),
    ADD CONSTRAINT efemeride_firma_con_metodo
        CHECK ((estado = 'aprobada') = (metodo_autenticacion IS NOT NULL));

GRANT UPDATE (metodo_autenticacion) ON acervo.efemeride TO hv_app;
