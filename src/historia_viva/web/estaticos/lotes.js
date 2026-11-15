"use strict";
// P10 Lotes (085): `GET /api/lotes` con cursor, filtros «Mis Lotes / Todos» y por estado, y un
// solo sondeo de contadores cada 10 s con un anuncio del cambio agregado, no por fila.
const $ = (id) => document.getElementById(id);
const TOPE_DEL_LOTE = 100; // Fijo, sin variable de entorno (R-27).
const MS_SONDEO = 10000;
const PAGINA = 100;
const MAXIMO = 200; // El `limite` más alto que acepta la API.
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];

let lotes = [];
let siguiente = null;
let sondeo = null;
let enVuelo = false;
let pedido = 0; // El último pedido de la lista: una respuesta de uno anterior se descarta.

const salirPorSesionVencida = () => { location.href = "/?error=sesion-vencida"; };
const plural = (n, uno, varios) => `${n} ${n === 1 ? uno : varios}`;

function fechaLlana(lote) {
  if (!lote.fecha_inicio) return "Sin Fecha todavía";  // Lote armado al arrastrar varios (151)
  const [a, m, d] = lote.fecha_inicio.split("-").map(Number);
  if (lote.precision_fecha === "dia") return `${d} de ${MESES[m - 1]} de ${a}`;
  if (lote.precision_fecha === "mes") return `${MESES[m - 1]} de ${a}`;
  if (lote.precision_fecha === "anio") return `En ${a}`;
  if (lote.precision_fecha === "decada") return `Década de ${a}`;
  return `${lote.fecha_inicio.slice(0, 4)}–${lote.fecha_fin.slice(0, 4)}`;
}

function filtros() {
  const query = new URLSearchParams();
  if (document.querySelector('input[name="quien"]:checked').value === "yo") query.set("creado_por", "yo");
  if ($("estado").value) query.set("estado", $("estado").value);
  return query;
}

const hayFiltros = () => filtros().size > 0;

async function leer(query) {
  const respuesta = await fetch(`/api/lotes?${query}`);
  if (respuesta.status === 401) {
    salirPorSesionVencida();
    return null;
  }
  if (!respuesta.ok) throw new Error(`/api/lotes respondió ${respuesta.status}`);
  return respuesta.json();
}

function celda(clase, ...contenido) {
  const span = document.createElement("span");
  span.className = clase;
  span.append(...contenido);
  return span;
}

// «Abrir» lleva al detalle (P4); «Cargar más archivos», a P3 en estado «Lote existente».
function enlace(texto, loteId, abrir) {
  const a = document.createElement("a");
  a.className = "boton";
  a.href = abrir ? `/lote.html?lote=${encodeURIComponent(loteId)}` : `/?lote=${encodeURIComponent(loteId)}`;
  a.textContent = texto;
  return a;
}

function fila(lote) {
  const li = document.createElement("li");
  const nombre = document.createElement("strong");
  nombre.textContent = lote.nombre;
  const creado = new Date(lote.creado_el).toLocaleDateString("es-AR");
  const origen = celda("aviso", `${lote.origen || "Sin origen todavía"} · Creado por ${lote.creado_por_nombre} el ${creado}`);
  const n = lote.por_estado;
  const conteos = celda("aviso",
    `Rec. ${n.Recibida} · Pend. ${n.Pendiente} · Inc. ${n.Incompleta} · Val. ${n.Validada}`);
  const espera = textoDeEspera(n.Recibida - lote.fallidas, lote.estimacion);
  const acciones = celda("accion", enlace("Abrir", lote.lote_id, true));
  if (lote.puede_cargar) {
    acciones.append(enlace("Cargar más archivos", lote.lote_id, false));
  } else {
    const apagado = document.createElement("button");
    apagado.type = "button";
    apagado.className = "boton";
    apagado.disabled = true;
    apagado.textContent = "Cargar más archivos";
    acciones.append(apagado, celda("aviso", `Este Lote ya tiene ${TOPE_DEL_LOTE} Piezas.`));
  }
  li.append(
    celda("nombre", nombre, origen),
    celda("fecha", fechaLlana(lote)),
    celda("propietario", lote.propietario || "Sin propietario"),
    celda(`estado estado-lote-${lote.estado.replace(" ", "-")}`, lote.estado),
    celda("piezas", `${lote.total} de ${TOPE_DEL_LOTE}`, conteos, ...(espera ? [celda("aviso", espera)] : [])),
    acciones,
  );
  return li;
}

async function pintarVacio() {
  // Sin filtros, o con filtros y sin ningún Lote en el sistema: «Todavía no hay Lotes».
  let ninguno = !hayFiltros();
  if (!ninguno) {
    const todos = await leer(new URLSearchParams({ limite: "1" }));
    if (!todos) return;
    ninguno = todos.lotes.length === 0;
  }
  $("sin-lotes").hidden = !ninguno;
  $("sin-coincidencias").hidden = ninguno;
}

function pintar() {
  $("lotes").replaceChildren(...lotes.map(fila));
  $("mas").hidden = !siguiente;
  $("sin-lotes").hidden = true;
  $("sin-coincidencias").hidden = true;
  if (!lotes.length) return pintarVacio();
}

function mostrarError() {
  $("cargando").hidden = true;
  $("error").hidden = false;
}

// Cambió el estado o algún conteo de un Lote: se anuncia el total, no fila por fila.
function anunciarCambios(antes, despues) {
  const firma = (lote) => JSON.stringify([lote.estado, lote.por_estado]);
  const previas = new Map(antes.map((lote) => [lote.lote_id, firma(lote)]));
  const cambiaron = despues.filter((l) => previas.has(l.lote_id) && previas.get(l.lote_id) !== firma(l));
  if (cambiaron.length) {
    $("anuncio").textContent = `${plural(cambiaron.length, "Lote cambió", "Lotes cambiaron")} de estado o de conteos.`;
  }
}

function programarSondeo() {
  clearTimeout(sondeo);
  sondeo = setTimeout(() => cargar({ sondear: true }), MS_SONDEO);
}

// `sondear`: relee lo que ya está en pantalla, hasta `MAXIMO`, y anuncia lo que cambió. Un
// cambio de filtro pide aunque haya otro pedido en vuelo; el sondeo no.
async function cargar({ sondear = false } = {}) {
  if (sondear && enVuelo) return programarSondeo();
  const mio = ++pedido;
  enVuelo = true;
  clearTimeout(sondeo);
  const query = filtros();
  // shortcut: el sondeo relee como mucho `MAXIMO` Lotes; los que se sumaron con «Ver más»
  // por encima de ese número se pierden de la lista hasta volver a pedirlos. Paginar el
  // sondeo si la SEU llega a tener tantos.
  query.set("limite", String(sondear ? Math.min(MAXIMO, Math.max(PAGINA, lotes.length)) : PAGINA));
  try {
    const pagina = await leer(query);
    if (mio !== pedido || !pagina) return;
    if (sondear) anunciarCambios(lotes, pagina.lotes);
    lotes = pagina.lotes;
    siguiente = pagina.siguiente;
    $("cargando").hidden = true;
    $("error").hidden = true;
    await pintar();
  } catch {
    if (mio === pedido && !sondear) mostrarError();
  } finally {
    if (mio === pedido) {
      enVuelo = false;
      programarSondeo();
    }
  }
}

async function cargarMas() {
  if (enVuelo) return;
  const mio = ++pedido;
  enVuelo = true;
  const query = filtros();
  query.set("cursor", siguiente);
  query.set("limite", String(PAGINA));
  try {
    const pagina = await leer(query);
    if (mio !== pedido || !pagina) return;
    lotes = lotes.concat(pagina.lotes);
    siguiente = pagina.siguiente;
    await pintar();
  } catch {
    if (mio === pedido) mostrarError();
  } finally {
    if (mio === pedido) enVuelo = false;
  }
}

for (const radio of document.querySelectorAll('input[name="quien"]')) {
  radio.addEventListener("change", () => cargar());
}
$("estado").addEventListener("change", () => cargar());
$("filtros").addEventListener("submit", (evento) => evento.preventDefault());
$("mas").addEventListener("click", cargarMas);
$("quitar-filtros").addEventListener("click", () => {
  document.querySelector('input[name="quien"][value="todos"]').checked = true;
  $("estado").value = "";
  cargar();
});
$("reintentar").addEventListener("click", () => {
  $("error").hidden = true;
  $("cargando").hidden = false;
  cargar();
});

(async () => {
  try {
    const respuesta = await fetch("/api/yo");
    if (respuesta.status === 401) return salirPorSesionVencida();
    if (!respuesta.ok) return mostrarError();
    if (!(await respuesta.json()).permisos.includes("CARGAR")) {
      $("cargando").textContent = "La lista de Lotes es de quien carga Piezas.";
      return;
    }
  } catch {
    return mostrarError();
  }
  cargar();
})();
