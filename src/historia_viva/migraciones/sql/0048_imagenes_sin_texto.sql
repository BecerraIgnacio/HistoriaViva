-- 148 (ADR-0007): una imagen embebida en un PDF, un Word o una planilla que casi no tiene texto
-- (una foto, un gráfico) la describe el modelo de visión y sus Afirmaciones se deciden como las de
-- una foto. Hasta `imagenes_a_describir` por Pieza se describen solas; el resto espera el botón
-- «Describir esta imagen». Cada descripción es un trabajo de la cola, uno por imagen.
--
-- `descripcion_de_imagen` pasa a tener una fila por imagen de la v1: `imagen` 0 es la foto entera
-- (144); de 1 en adelante, las imágenes embebidas sin texto en el orden del documento, con su
-- ubicación en el original (`numero` y `pagina` u `hoja`, la de la marca de 143). No se guarda la
-- imagen: la miniatura y la descripción la vuelven a sacar del original por su ubicación, así la
-- supresión (`suprimir_pieza`) no tiene copias que borrar.
ALTER TABLE acervo.afirmacion DROP CONSTRAINT afirmacion_ficha_id_fkey;
ALTER TABLE acervo.resumen_rehecho DROP CONSTRAINT resumen_rehecho_ficha_id_fkey;
ALTER TABLE acervo.descripcion_de_imagen
    DROP CONSTRAINT descripcion_de_imagen_pkey,
    ADD COLUMN imagen smallint NOT NULL DEFAULT 0 CHECK (imagen >= 0),
    ADD COLUMN numero smallint CHECK (numero >= 1),
    ADD COLUMN pagina integer CHECK (pagina >= 1),
    ADD COLUMN hoja text,
    -- sin_describir: espera el botón. encolada: su trabajo no terminó (la firma espera).
    -- descrita: con sus Afirmaciones, o con `tipo_imagen` NULL si la descripción falló.
    ADD COLUMN estado text NOT NULL DEFAULT 'descrita'
        CHECK (estado IN ('sin_describir', 'encolada', 'descrita')),
    ADD PRIMARY KEY (ficha_id, imagen),
    ADD CONSTRAINT descripcion_de_imagen_ubicada CHECK (
        (imagen = 0) = (numero IS NULL) AND (pagina IS NULL OR hoja IS NULL)
    ),
    ADD CONSTRAINT descripcion_de_imagen_sin_clase_sin_describir CHECK (
        estado = 'descrita' OR tipo_imagen IS NULL
    );

-- El resumen rehecho (146) es solo de una foto: cuelga de su imagen 0, como antes de su fila única.
ALTER TABLE acervo.resumen_rehecho
    ADD COLUMN imagen smallint NOT NULL DEFAULT 0 CHECK (imagen = 0),
    ADD CONSTRAINT resumen_rehecho_ficha_id_fkey FOREIGN KEY (ficha_id, imagen)
        REFERENCES acervo.descripcion_de_imagen (ficha_id, imagen) ON DELETE CASCADE;

-- `orden` sigue siendo único en la Ficha (lo usa `PUT …/afirmaciones/{orden}`); `imagen` dice de
-- cuál salió cada una.
ALTER TABLE acervo.afirmacion
    ADD COLUMN imagen smallint NOT NULL DEFAULT 0,
    ADD CONSTRAINT afirmacion_de_su_imagen FOREIGN KEY (ficha_id, imagen)
        REFERENCES acervo.descripcion_de_imagen (ficha_id, imagen) ON DELETE CASCADE;

COMMENT ON TABLE acervo.descripcion_de_imagen IS
    'Lo que propuso describir_imagen para la v1: la foto entera (imagen 0, 144) o cada imagen embebida sin texto (148). tipo_imagen NULL: falló o no se describió.';

GRANT UPDATE (tipo_imagen, estado) ON acervo.descripcion_de_imagen TO hv_app;

-- La descripción de una imagen embebida es un trabajo de la cola: con su tope (el `timeout_s` de
-- `describir_imagen`, ADR-0004), sus generaciones y su arriendo. Uno por imagen: el índice único
-- impide encolarla dos veces; solo uno fallido deja lugar a otro.
ALTER TABLE acervo.trabajo_procesamiento
    DROP CONSTRAINT trabajo_procesamiento_tipo_check,
    ADD CONSTRAINT trabajo_procesamiento_tipo_check
        CHECK (tipo IN ('procesamiento', 'indexacion', 'resumen', 'descripcion')),
    ADD COLUMN imagen smallint CHECK (imagen >= 1),
    ADD CONSTRAINT trabajo_imagen_solo_descripcion CHECK ((tipo = 'descripcion') = (imagen IS NOT NULL));

COMMENT ON COLUMN acervo.trabajo_procesamiento.imagen IS
    'La imagen embebida que describe un trabajo de descripción (148): `descripcion_de_imagen.imagen`.';

CREATE UNIQUE INDEX trabajo_descripcion_unica ON acervo.trabajo_procesamiento (pieza_id, imagen)
    WHERE tipo = 'descripcion' AND estado IN ('encolado', 'en_curso', 'completado');

-- Cuántas se describen solas por Pieza, en la fila de `describir_imagen`: en esta PC sin GPU cada
-- una tarda minutos (Comments de 148). Lo que la fila ya tenga gana.
UPDATE acervo.configuracion_capacidad
   SET parametros = '{"imagenes_a_describir": 3}'::jsonb || parametros,
       version = version + 1,
       actualizada_el = now()
 WHERE capacidad = 'describir_imagen';
