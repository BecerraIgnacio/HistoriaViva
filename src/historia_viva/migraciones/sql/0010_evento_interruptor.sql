-- 018: el interruptor global (spec §1.15, la 0011 del spec; ADR-0004 control 4, DEC-88, DEC-136).
--
-- Append-only: el estado de cada freno es su último evento (DEC-27), sin caché, y la tabla es a la
-- vez su auditoría (no se duplica en `auditoria`, DEC-40). Detener exige un motivo de 5
-- caracteres o más. Quién cambió va como en `auditoria` (`actor_tipo`, `actor_etiqueta`), sin
-- `agente`: un Agente nunca mueve el freno (autonomía cero, ADR-0004).

CREATE TABLE acervo.evento_interruptor (
    evento_id      bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    clave          text NOT NULL
                   CHECK (clave IN ('procesamiento_habilitado','api_agentes_habilitada')),
    habilitado     boolean NOT NULL,
    motivo         text NOT NULL,
    actor_tipo     text NOT NULL CHECK (actor_tipo IN ('persona','cli','sistema')),
    actor_etiqueta text NOT NULL,
    usuario_id     uuid REFERENCES acervo.usuario (usuario_id) ON DELETE RESTRICT,
    en             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evento_interruptor_detener_con_motivo
        CHECK (habilitado OR length(btrim(motivo)) >= 5),
    CONSTRAINT evento_interruptor_persona_con_usuario
        CHECK ((actor_tipo = 'persona') = (usuario_id IS NOT NULL))
);

COMMENT ON TABLE acervo.evento_interruptor IS
    'Los dos frenos del interruptor global (ADR-0004): el estado es el último evento por clave. hv_app solo inserta y consulta.';

CREATE INDEX interruptor_estado ON acervo.evento_interruptor (clave, evento_id DESC);

-- La API de Agentes nace apagada: la enciende el Administrador técnico a mano (DEC-136).
INSERT INTO acervo.evento_interruptor (clave, habilitado, motivo, actor_tipo, actor_etiqueta)
VALUES ('procesamiento_habilitado', true,  'alta inicial', 'sistema', 'migracion'),
       ('api_agentes_habilitada',   false, 'alta inicial', 'sistema', 'migracion');

GRANT SELECT, INSERT ON acervo.evento_interruptor TO hv_app;
