-- 016: los cuatro roles del Director reemplazan a los dos del Prototipo (DEC-124, R-04). Del
-- spec: el `CHECK` de rol de la 0017.
--
-- Sin valor por defecto: dar de alta a una persona obliga a decir su rol. La base del MVP nace
-- sin personas (DEC-52); una base de desarrollo con personas del Prototipo hace fallar esta
-- migración en vez de adivinarles un rol.

ALTER TABLE acervo.usuario
    ALTER COLUMN rol DROP DEFAULT,
    DROP CONSTRAINT usuario_rol_check,
    ADD CONSTRAINT usuario_rol_check
        CHECK (rol IN ('rgc','coordinador','director_carrera','admin_tecnico'));

COMMENT ON COLUMN acervo.usuario.rol IS
    'Uno de los cuatro roles del Director (DEC-124). Qué permite cada uno lo decide `dominio`, no la base.';
