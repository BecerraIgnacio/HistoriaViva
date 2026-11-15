-- 096: los Agentes externos y sus tokens (spec §1.14, la 0010 del spec; RF-26, DEC-32).
--
-- N tokens por agente: la rotación con solape no cabe en «una fila, un hash». Se guarda el
-- SHA-256 del token, nunca el token. El `CHECK` de alcances ya admite `efemerides:leer` (R-43 a).
-- `creado_por` y `revocado_por` son nulos cuando actúa un comando (`emitir-token`,
-- `revocar-token`): quién fue queda en `auditoria` con actor `cli`, como en 018. Rotar programa
-- la revocación de los anteriores (`revocado_el` futuro): `hv_app` no toca `vence_el`.

CREATE TABLE acervo.agente_externo (
    agente_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre     text NOT NULL UNIQUE,
    scopes     text[] NOT NULL CHECK (
        cardinality(scopes) >= 1
        AND scopes <@ ARRAY['fragmentos:leer','piezas:leer','aportes:escribir','efemerides:leer']
    ),
    creado_el  timestamptz NOT NULL DEFAULT now(),
    creado_por uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT
);

CREATE TABLE acervo.token_agente (
    token_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agente_id     uuid NOT NULL REFERENCES acervo.agente_externo (agente_id) ON DELETE RESTRICT,
    token_hash    text NOT NULL UNIQUE,
    -- El id público del token (`hv_` + 8 hex): lo que se ve en listados, logs y auditoría y lo
    -- que nombra `revocar-token`.
    token_prefijo text NOT NULL UNIQUE CHECK (token_prefijo ~ '^hv_[0-9a-f]{8}$'),
    creado_el     timestamptz NOT NULL DEFAULT now(),
    creado_por    uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    vence_el      timestamptz,
    ultimo_uso_el timestamptz,
    revocado_el   timestamptz,
    revocado_por  uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    CONSTRAINT token_agente_revocado_por_con_fecha
        CHECK (revocado_por IS NULL OR revocado_el IS NOT NULL)
);

COMMENT ON TABLE acervo.token_agente IS
    'Tokens de Agentes (DEC-32): SHA-256 del token, nunca el token. Revocar es marcar, nunca DELETE. vencido se deriva de vence_el.';

CREATE INDEX token_por_agente ON acervo.token_agente (agente_id);

GRANT SELECT, INSERT ON acervo.agente_externo TO hv_app;
GRANT SELECT, INSERT ON acervo.token_agente TO hv_app;
GRANT UPDATE (ultimo_uso_el, revocado_el, revocado_por) ON acervo.token_agente TO hv_app;
