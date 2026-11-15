-- 043: mediciones ampliadas (spec §1.12, 0009 del spec; DEC-92, DEC-146 punto 14).
--
-- Tres columnas al final, que el CSV exporta después de las 13 del Prototipo. `abrir` copia
-- `clase_original` de la Pieza, como `nombre_archivo`; `cerrar` copia el método de la sesión y el
-- modo de la firma: la instrumentación no hace `JOIN` con `pieza`. El modo solo admite
-- `individual`: la firma en bloque no abre mediciones por Pieza.

ALTER TABLE acervo.medicion_validacion
    ADD COLUMN clase_original       text,
    ADD COLUMN metodo_autenticacion text
        CONSTRAINT medicion_metodo_conocido CHECK (metodo_autenticacion IN ('oidc','dev')),
    ADD COLUMN modo_validacion      text
        CONSTRAINT medicion_modo_individual CHECK (modo_validacion IN ('individual'));

UPDATE acervo.medicion_validacion m
   SET clase_original = p.clase_original
  FROM acervo.pieza p
 WHERE p.pieza_id = m.pieza_id;

ALTER TABLE acervo.medicion_validacion
    ALTER COLUMN clase_original SET NOT NULL,
    ADD CONSTRAINT medicion_clase_conocida
        CHECK (clase_original IN ('imagen','documento','planilla','video','texto'));
