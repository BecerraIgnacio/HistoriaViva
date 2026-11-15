"use strict";
const $ = (id) => document.getElementById(id);
const loteId = new URLSearchParams(location.search).get("lote");

// Un solo sondeo de contadores (spec P4): 3 s mientras hay Piezas leyéndose, 10 s después.
const MS_PROCESANDO = 3000;
const MS_QUIETO = 10000;

const NOMBRE_DEL_CAMPO = {
  tipo: "Tipo de Pieza",
  tipo_actividad: "Tipo de actividad",
  fecha_inicio: "Fecha",
  fecha_fin: "Fecha",
  precision_fecha: "Fecha",
  origen: "Origen",
  propietario: "Propietario",
  resumen: "Resumen",
  archivo_original: "Archivo original",
};

let permisos = [];
let pausado = false;
let motorCaido = false;
let firma = null;
let enCursoAntes = null;
let sondeo = null;
let enVuelo = false; // Un «Reintentar» no arranca otra carga encima de la del sondeo.
let saludLeidaEl = 0;
let bloqueHabilitado = false;
let bloqueListo = false;
let previa = null; // La última previa del bloque: su aprobación y las versiones vistas.
const nombres = {}; // pieza_id → nombre del archivo, de la lista del Lote.

const salirPorSesionVencida = () => { location.href = "/?error=sesion-vencida"; };

// Lo que Reintentar y Completar a mano (`piezas.js`) hacen después en esta pantalla.
const PANTALLA = {
  recargar: () => { firma = null; return cargar(); },
  avisar: (texto) => { $("anuncio").textContent = texto; },
  sesionVencida: salirPorSesionVencida,
};

const faltan = (campos) => [...new Set(campos.map((c) => NOMBRE_DEL_CAMPO[c] || c))].join(", ");
const plural = (n, uno, varios) => `${n} ${n === 1 ? uno : varios}`;

function mostrarError(texto) {
  clearTimeout(sondeo);
  $("cargando").hidden = true;
  $("lote").hidden = true;
  $("texto-error").textContent = texto;
  $("error").hidden = false;
}

async function cargarSesion() {
  const respuesta = await fetch("/api/yo");
  if (respuesta.status === 401) {
    salirPorSesionVencida();
    return false;
  }
  if (!respuesta.ok) throw new Error(`/api/yo respondió ${respuesta.status}`);
  const yo = await respuesta.json();
  permisos = yo.permisos;
  pausado = yo.sistema.procesamiento_habilitado === false;
  bloqueHabilitado = yo.sistema.bloque_habilitado === true;
  $("franja-interruptor").hidden = !pausado;
  return true;
}

function pintarLote({ lote, progreso, puede_cargar: puedeCargar }) {
  $("titulo").textContent = lote.nombre;
  $("contexto").textContent = [
    lote.evento, lote.lugar, lote.origen,
    lote.fecha_inicio && `${lote.fecha_inicio} a ${lote.fecha_fin}`, lote.propietario,
  ].filter(Boolean).join(" · ") || "Sin Contexto todavía.";
  pintarCompletar(lote, progreso);
  $("estado-lote").textContent = progreso.estado;
  const carga = `/?lote=${encodeURIComponent(lote.lote_id)}`;
  const cargar = permisos.includes("CARGAR");
  $("cargar-mas").href = carga;
  $("cargar-mas").hidden = !cargar || !puedeCargar || progreso.total === 0;
  $("cargar-archivos").href = carga;
  $("cargar-archivos").hidden = !cargar;
  $("vacio").hidden = progreso.total !== 0;
  $("progreso").hidden = progreso.total === 0;

  const n = progreso.por_estado;
  $("conteos").textContent = `Progreso: ${plural(progreso.total, "Pieza", "Piezas")}   `
    + `Recibidas ${n.Recibida} · Pendientes ${n.Pendiente} · Incompletas ${n.Incompleta}`
    + ` · Validadas ${n.Validada}`;
  const esperan = n.Recibida - progreso.fallidas;
  $("espera").textContent = textoDeEspera(esperan, progreso.estimacion);
  $("espera").hidden = !esperan;
  const listas = progreso.total - n.Recibida;
  $("barra").max = progreso.total || 1;
  $("barra").value = listas;
  $("texto-barra").textContent = `${listas} de ${progreso.total} listas para validar`;
  $("aparte").textContent = [
    progreso.fallidas && plural(progreso.fallidas, "falló", "fallaron"),
    progreso.para_analisis_humano
      && plural(progreso.para_analisis_humano, "requiere análisis humano",
        "requieren análisis humano"),
    progreso.sin_indexar
      && plural(progreso.sin_indexar, "Validada no indexada: todavía no es buscable",
        "Validadas no indexadas: todavía no son buscables"),
  ].filter(Boolean).join(" · ");

  // Un único anuncio, al cierre: cuando no queda ninguna Recibida leyéndose (las fallidas
  // esperan una decisión, no al motor).
  const enCurso = n.Recibida - progreso.fallidas;
  if (enCursoAntes > 0 && enCurso === 0) $("anuncio").textContent = `Listo: ${listas} de ${progreso.total}`;
  enCursoAntes = enCurso;
  return enCurso;
}

// ── Completar el Lote (151) ───────────────────────────────────────────────
// Lo vacío del Lote (`por_completar`) se pide mientras sus Piezas se procesan. El sondeo
// repinta cada 3 s: el formulario solo se rearma si cambió qué falta, para no pisar lo tipeado.
let faltabaAntes = null;
let nombreAntes = null;

function pintarCompletar(lote, progreso) {
  const faltan = lote.por_completar || [];
  const formulario = $("completar");
  formulario.hidden = !permisos.includes("CARGAR") || !faltan.length;
  if (formulario.hidden) {
    faltabaAntes = null;
    return;
  }
  $("completar-tarde").hidden = progreso.total === 0;
  if (faltabaAntes === faltan.join() && nombreAntes === lote.nombre) return;
  faltabaAntes = faltan.join();
  nombreAntes = lote.nombre;
  $("completar-nombre").value = lote.nombre;
  for (const caja of formulario.querySelectorAll("[data-campo]")) {
    caja.hidden = !faltan.includes(caja.dataset.campo);
  }
  mostrarHasta();
}

function mostrarHasta() {
  const rango = !$("completar-desde").hidden && $("completar-precision").value === "rango";
  $("completar-hasta").hidden = $("rotulo-completar-hasta").hidden = !rango;
}

$("completar-precision").addEventListener("change", mostrarHasta);

$("completar").addEventListener("submit", async (evento) => {
  evento.preventDefault();
  const resultado = $("completar-resultado");
  const visible = (id) => !$(id).hidden && $(id).value.trim();
  const cambios = {};
  if ($("completar-nombre").value.trim() && $("completar-nombre").value.trim() !== nombreAntes) {
    cambios.nombre = $("completar-nombre").value.trim();
  }
  if (visible("completar-origen")) cambios.origen = visible("completar-origen");
  if (visible("completar-propietario")) cambios.propietario = visible("completar-propietario");
  if (visible("completar-desde")) {
    cambios.fecha_inicio = visible("completar-desde");
    cambios.precision_fecha = $("completar-precision").value;
    // Con las otras precisiones el fin lo deriva el servidor.
    if (cambios.precision_fecha === "rango") cambios.fecha_fin = visible("completar-hasta") || null;
  }
  if (!Object.keys(cambios).length) {
    resultado.textContent = "Completá al menos un dato.";
    return;
  }
  let respuesta;
  try {
    respuesta = await fetch(`/api/lotes/${encodeURIComponent(loteId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-HV": "1" },
      body: JSON.stringify(cambios),
    });
  } catch {
    resultado.textContent = "No se pudo guardar. Probá de nuevo.";
    return;
  }
  if (respuesta.status === 401) return salirPorSesionVencida();
  const cuerpo = await respuesta.json().catch(() => ({}));
  if (!respuesta.ok) {
    resultado.textContent = cuerpo.detail || "No se pudo guardar. Probá de nuevo.";
    return;
  }
  resultado.textContent = "Guardado en el Lote: se copió a las Piezas que no lo tenían.";
  faltabaAntes = null;
  firma = null;
  cargar();
});

function enlace(texto, piezaId, clase) {
  const a = document.createElement("a");
  a.href = `/validar.html?pieza=${encodeURIComponent(piezaId)}`;
  a.className = clase;
  a.textContent = texto;
  return a;
}

// La columna derecha es texto, no color (spec P4). «Esperando al motor» no es «falló»: el
// trabajo sigue encolado y se retoma solo (DEC-146 punto 16, R-30).
// shortcut: la fila no dice si el trabajo está encolado o en curso; con el freno puesto o el
// motor caído toda Recibida no fallida dice «esperando al motor», hasta que la fila lo traiga.
function marcar(pieza, fila, marca, accion) {
  const validar = permisos.includes("VALIDAR");
  if (pieza.estado === "Recibida" && pieza.fallida) {
    marca.textContent = "● Recibida · falló";
    marca.className = "marca error";
    if (permisos.includes("CARGAR")) accion.append(...accionesDeFallida(pieza.pieza_id, fila, PANTALLA));
    return;
  }
  if (pieza.estado === "Recibida") {
    marca.textContent = pausado
      ? "● Recibida · esperando al motor (procesamiento detenido)"
      : motorCaido ? "● Recibida · esperando al motor" : "● Recibida · procesando";
    // Los campos duros no los propone ningún modelo: se completan mientras se lee (152).
    if (validar) accion.append(enlace("Completar mientras se lee", pieza.pieza_id, "enlace"));
    else accion.textContent = "Todavía sin Ficha";
    return;
  }
  if (pieza.estado === "Validada") {
    marca.textContent = pieza.no_indexada ? "✓ Validada · No indexada" : "✓ Validada";
    accion.append(enlace("Ver la Ficha", pieza.pieza_id, "enlace"));
    return;
  }
  if (pieza.estado === "Incompleta") {
    marca.textContent = `◐ Incompleta · Falta: ${faltan(pieza.campos_faltantes)}`;
  } else {
    marca.textContent = pieza.requiere_analisis_humano ? "▶ Requiere análisis humano" : "○ Pendiente";
  }
  // Sin VALIDAR toda Ficha se abre en solo lectura (DEC-124, R-05).
  const texto = pieza.estado === "Incompleta" ? "Continuar" : "Validar";
  accion.append(validar ? enlace(texto, pieza.pieza_id, "boton") : enlace("Ver la Ficha", pieza.pieza_id, "enlace"));
}

function fila(pieza) {
  const li = document.createElement("li");
  const nombre = document.createElement("span");
  nombre.className = "nombre";
  nombre.textContent = pieza.resumen ? `${pieza.nombre_archivo} · «${pieza.resumen}»` : pieza.nombre_archivo;
  const marca = document.createElement("span");
  marca.className = `marca estado-${pieza.estado}`;
  const accion = document.createElement("span");
  accion.className = "accion";
  if (pieza.alerta_inyeccion) nombre.append(" ", marcaDeAlerta());
  li.append(marca, nombre, accion);
  marcar(pieza, li, marca, accion);
  return li;
}

function pintarParcial(piezas, porEstado) {
  const sinValidar = piezas.filter((p) => p.estado === "Pendiente" || p.estado === "Incompleta");
  const campos = faltan(sinValidar.flatMap((p) => p.campos_faltantes));
  $("parcial").hidden = !(porEstado.Validada && sinValidar.length && !porEstado.Recibida);
  $("parcial").textContent = `${plural(porEstado.Validada, "Validada", "Validadas")}. `
    + `${sinValidar.length} ${sinValidar.length === 1 ? "sigue" : "siguen"} sin validar`
    + (campos ? `: falta ${campos}` : "");
}

async function cargarPiezas(progreso) {
  const respuesta = await fetch(`/api/piezas?lote_id=${encodeURIComponent(loteId)}`);
  if (!respuesta.ok) return;
  const { piezas } = await respuesta.json();
  for (const pieza of piezas) nombres[pieza.pieza_id] = pieza.nombre_archivo;
  // Encolado sin freno: espera al motor solo si el motor no responde. La salud se lee al
  // cargar y después cada `MS_QUIETO` como mucho, no en cada sondeo de 3 s.
  const esperan = piezas.some((p) => p.estado === "Recibida" && !p.fallida);
  if (!pausado && esperan && Date.now() - saludLeidaEl >= MS_QUIETO) await revisarSalud();
  $("piezas").replaceChildren(...piezas.map(fila));
  pintarParcial(piezas, progreso.por_estado);
}

async function revisarSalud() {
  saludLeidaEl = Date.now();
  try {
    motorCaido = (await (await fetch("/api/salud")).json()).estado !== "ok";
  } catch {
    // La salud es informativa: si no contesta, no se avisa nada.
  }
  $("franja-degradado").hidden = !motorCaido;
}

async function cargar() {
  if (enVuelo) return;
  enVuelo = true;
  try {
    await cargarYa();
  } finally {
    enVuelo = false;
  }
}

async function cargarYa() {
  clearTimeout(sondeo);
  let respuesta;
  try {
    respuesta = await fetch(`/api/lotes/${encodeURIComponent(loteId)}`);
  } catch {
    return mostrarError("No pudimos cargar el Lote.");
  }
  if (respuesta.status === 401) return salirPorSesionVencida();
  if (!respuesta.ok) {
    return mostrarError(respuesta.status === 404 || respuesta.status === 422
      ? "Ese Lote no existe."
      : "No pudimos cargar el Lote.");
  }
  const vista = await respuesta.json();
  $("cargando").hidden = true;
  $("error").hidden = true;
  $("lote").hidden = false;
  const enCurso = pintarLote(vista);
  prepararBloque(vista.lote);
  // La lista se relee si cambió algún contador o si hay Piezas leyéndose.
  const nueva = JSON.stringify(vista.progreso);
  if (nueva !== firma || enCurso > 0) await cargarPiezas(vista.progreso);
  firma = nueva;
  sondeo = setTimeout(cargar, enCurso > 0 ? MS_PROCESANDO : MS_QUIETO);
}

// ── Aprobación en bloque (108, CU-07) ─────────────────────────────────────
// Con `HV_BLOQUE_HABILITADO` en falso solo se explica por qué no está (R-28): ni campos ni
// casillas ni botón. Encendida: clase y campos comunes → previa → casillas → confirmación.
const MOTIVOS = {
  alerta_inyeccion: "Validala de a una: tiene una alerta de inyección",
  afirmaciones_sin_decidir: "Validala de a una: tiene Afirmaciones sin decidir",
  requiere_analisis_humano: "Validala de a una: requiere análisis humano",
  procesando: "Todavía se está leyendo",
  no_pendiente: "Ya no está Pendiente",
  version_desactualizada: "Cambió mientras revisabas",
  borrada: "Se retiró del Acervo",
};
const motivo = (r) => (r.motivo === "falta_campo"
  ? `No se puede: falta ${NOMBRE_DEL_CAMPO[r.campo] || r.campo}`
  : MOTIVOS[r.motivo] || r.motivo);
const nombreDe = (piezaId) => nombres[piezaId] || piezaId;
const marcadas = () => [...$("bloque-piezas").querySelectorAll("input:checked")];
const enviar = (url, cuerpo) => fetch(url, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-HV": "1" },
  body: JSON.stringify(cuerpo),
});

function heredado(termino, valor) {
  const dt = document.createElement("dt");
  dt.textContent = termino;
  const dd = document.createElement("dd");
  dd.textContent = `${valor} · Heredado del Lote`;
  $("bloque-heredados").append(dt, dd);
}

async function prepararBloque(lote) {
  if (bloqueListo || !permisos.includes("VALIDAR")) return;
  bloqueListo = true;
  $("bloque").hidden = false;
  $("bloque-deshabilitado").hidden = bloqueHabilitado;
  $("bloque-habilitado").hidden = !bloqueHabilitado;
  if (!bloqueHabilitado) return;
  heredado("Origen", lote.origen || "sin origen todavía: completalo en el Lote");
  heredado("Fecha", lote.fecha_inicio
    ? `${lote.fecha_inicio} a ${lote.fecha_fin}` : "sin Fecha todavía: completala en el Lote");
  heredado("Propietario", lote.propietario || "sin propietario: esas Piezas se validan de a una");
  try {
    const [{ tipos }, { tipos_actividad: actividades }, { carreras }] = await Promise.all(
      ["/api/tipos", "/api/tipos-actividad", "/api/carreras"]
        .map(async (url) => (await fetch(url)).json()),
    );
    // Una Carrera dada de baja no viene en la lista: se muestra su id (DEC-127).
    const nombreDeCarrera = Object.fromEntries(carreras.map((c) => [c.carrera_id, c.nombre]));
    heredado("Carreras", lote.carreras.length
      ? lote.carreras.map((id) => nombreDeCarrera[id] || id).join(", ")
      : "ninguna");
    for (const tipo of tipos) $("bloque-tipo").add(new Option(tipo, tipo));
    for (const actividad of actividades) $("bloque-actividad").add(new Option(actividad, actividad));
  } catch {
    $("bloque-resultado").textContent = "No pudimos cargar los tipos: recargá la página.";
  }
}

function contarMarcadas() {
  const n = marcadas().length;
  $("bloque-aprobar").textContent = `Aprobar en bloque (${n})`;
  $("bloque-aprobar").disabled = n === 0;
}

function filaDeBloque(piezaId, texto, elegible) {
  const li = document.createElement("li");
  if (elegible) {
    const casilla = document.createElement("input");
    casilla.type = "checkbox";
    casilla.id = `bloque-${piezaId}`;
    casilla.value = piezaId;
    casilla.addEventListener("change", contarMarcadas);
    const etiqueta = document.createElement("label");
    etiqueta.htmlFor = casilla.id;
    etiqueta.textContent = `${nombreDe(piezaId)} · Se valida`;
    li.append(casilla, etiqueta);
  } else {
    li.className = "afuera";
    li.textContent = `${nombreDe(piezaId)} · ${texto}`;
  }
  return li;
}

async function problemaDe(respuesta) {
  if (respuesta.status === 401) salirPorSesionVencida();
  const problema = await respuesta.json().catch(() => ({}));
  return problema.detail || "No se pudo completar la aprobación en bloque.";
}

$("bloque-previa").addEventListener("click", async () => {
  $("bloque-resultado").textContent = "";
  const respuesta = await enviar(`/api/lotes/${encodeURIComponent(loteId)}/aprobaciones`,
    { clase: $("bloque-clase").value });
  if (!respuesta.ok) {
    $("bloque-resultado").textContent = await problemaDe(respuesta);
    return;
  }
  previa = await respuesta.json();
  $("bloque-piezas").replaceChildren(
    ...previa.elegibles.map((e) => filaDeBloque(e.pieza_id, "", true)),
    ...previa.rechazadas.map((r) => filaDeBloque(r.pieza_id, motivo(r), false)),
  );
  const afuera = previa.rechazadas.length;
  $("bloque-afuera").textContent = afuera
    ? `${afuera === 1 ? "Esta no entra" : `Estas ${afuera} no entran`} en el bloque: validalas de a una.`
    : "";
  if (!previa.elegibles.length && !afuera) {
    $("bloque-afuera").textContent = "No hay Piezas de esa clase en el Lote.";
  }
  $("bloque-lista").hidden = false;
  contarMarcadas();
});

$("bloque-todas").addEventListener("click", () => {
  for (const casilla of $("bloque-piezas").querySelectorAll("input")) casilla.checked = true;
  contarMarcadas();
});

$("bloque-aprobar").addEventListener("click", () => {
  if (!$("bloque-tipo").value || !$("bloque-actividad").value) {
    $("bloque-resultado").textContent = "Elegí el tipo de Pieza y el tipo de actividad.";
    return;
  }
  const n = marcadas().length;
  $("bloque-aviso").textContent = n === 1
    ? "Vas a firmar 1 Ficha sin abrirla."
    : `Vas a firmar ${n} Fichas sin abrirlas una por una.`;
  $("bloque-firmar").textContent = `Firmar ${n}`;
  $("bloque-confirmar").showModal();
});

$("bloque-cancelar").addEventListener("click", () => $("bloque-confirmar").close());

$("bloque-firmar").addEventListener("click", async () => {
  $("bloque-confirmar").close();
  const version = Object.fromEntries(previa.elegibles.map((e) => [e.pieza_id, e.version]));
  const contexto = $("bloque-contexto").value.trim();
  const respuesta = await enviar(
    `/api/lotes/${encodeURIComponent(loteId)}/aprobaciones/${previa.aprobacion_id}/confirmacion`,
    {
      tipo: $("bloque-tipo").value,
      tipo_actividad: $("bloque-actividad").value,
      ...(contexto && { contexto_adicional: contexto }),
      piezas: marcadas().map((c) => ({ pieza_id: c.value, version: version[c.value] })),
    },
  );
  if (!respuesta.ok) {
    $("bloque-resultado").textContent = await problemaDe(respuesta);
    return;
  }
  const { aprobadas, rechazadas } = await respuesta.json();
  previa = null;
  $("bloque-lista").hidden = true;
  // No «siguen Pendientes»: una que cambió mientras se revisaba puede haber quedado Incompleta.
  const siguen = rechazadas.length
    ? ` ${rechazadas.length} no se ${rechazadas.length === 1 ? "firmó" : "firmaron"}: `
      + rechazadas.map((r) => `${nombreDe(r.pieza_id)} (${motivo(r)})`).join("; ")
    : "";
  $("bloque-resultado").textContent = `${plural(aprobadas.length, "Validada", "Validadas")}.${siguen}`;
  firma = null;
  cargar();
});

$("reintentar-carga").addEventListener("click", () => {
  $("error").hidden = true;
  $("cargando").hidden = false;
  cargar();
});

(async () => {
  if (!loteId) return mostrarError("Falta el Lote: volvé a Mis Piezas y abrilo desde ahí.");
  try {
    if (!(await cargarSesion())) return;
  } catch {
    return mostrarError("No pudimos cargar el Lote.");
  }
  await revisarSalud();
  cargar();
})();
