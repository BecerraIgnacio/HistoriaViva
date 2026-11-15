-- 100: el Aporte de un Agente externo (spec §1.16, la 0014 del spec; DEC-17, DEC-140, DEC-146
-- punto 8).
--
-- El Aporte es una Pieza de clase `texto` con `via_ingreso = aporte`; la idempotencia vive acá,
-- sin tabla aparte y sin vencimiento: `(agente_id, idempotency_key)` es única y `cuerpo_huella`
-- distingue el reintento (misma huella) de la clave reutilizada (otra). `hv_app` solo inserta y
-- lee: ni el Aporte ni sus fuentes cambian después de recibidos.
--
-- Se aparta del spec en `url_publicacion`: el contrato congelado con el Agente 1 (1.1.0-rc2)
-- deja la fecha y la URL opcionales cada una por su lado, así que no va el `CHECK` que exigía la
-- fecha con la URL. `tipo_contenido` y `canal` toman los valores que cerró el contrato (C16).

CREATE TABLE acervo.aporte (
    aporte_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agente_id         uuid NOT NULL REFERENCES acervo.agente_externo (agente_id) ON DELETE RESTRICT,
    idempotency_key   text NOT NULL CHECK (
        length(idempotency_key) BETWEEN 8 AND 128 AND idempotency_key ~ '^[A-Za-z0-9._:-]+$'
    ),
    cuerpo_huella     text NOT NULL CHECK (cuerpo_huella ~ '^[0-9a-f]{64}$'),
    pieza_id          uuid NOT NULL UNIQUE REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    titulo            text NOT NULL,
    cuerpo            text NOT NULL,
    tipo_contenido    text NOT NULL
                          CHECK (tipo_contenido IN ('gacetilla','post','comunicado','newsletter')),
    canal             text CHECK (canal IN ('instagram','linkedin','web','correo')),
    fecha_publicacion date,
    url_publicacion   text,
    recibido_el       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT aporte_clave_unica UNIQUE (agente_id, idempotency_key)
);

COMMENT ON COLUMN acervo.aporte.cuerpo_huella IS
    'SHA-256 del JSON canónico del pedido, ya normalizado (R-36): misma huella con la misma clave es un reintento.';
COMMENT ON COLUMN acervo.aporte.cuerpo IS
    'Copia de auditoría de lo recibido, normalizada; el original de la Pieza es el texto recibido.';

-- Sin FK a fragmento: su id cambia con cada versión de la Ficha (DEC-04) y el reemplazo borra la
-- fila vieja; solo se valida la forma (DEC-146 punto 19).
CREATE TABLE acervo.aporte_fragmento_fuente (
    aporte_id    uuid NOT NULL REFERENCES acervo.aporte (aporte_id) ON DELETE RESTRICT,
    fragmento_id uuid NOT NULL,
    PRIMARY KEY (aporte_id, fragmento_id)
);

CREATE TABLE acervo.aporte_pieza_fuente (
    aporte_id uuid NOT NULL REFERENCES acervo.aporte (aporte_id) ON DELETE RESTRICT,
    pieza_id  uuid NOT NULL REFERENCES acervo.pieza (pieza_id) ON DELETE RESTRICT,
    PRIMARY KEY (aporte_id, pieza_id)
);

GRANT SELECT, INSERT ON acervo.aporte TO hv_app;
GRANT SELECT, INSERT ON acervo.aporte_fragmento_fuente TO hv_app;
GRANT SELECT, INSERT ON acervo.aporte_pieza_fuente TO hv_app;
