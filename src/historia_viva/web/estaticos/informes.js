"use strict";
// P9 Informes (066): lista, «+ Nuevo informe» y el detalle del Borrador; 068 suma «Aprobar» y M4; 069, «Descartar»,
// «Generar de nuevo», «Reemplazado» y el aviso «Cambió después de aprobar»; 070 y 071, «Exportar .docx» y «Exportar .xlsx».
// Todo texto del Acervo o del servidor se pinta con `textContent`; los enlaces llevan los ids
// escapados (RNF-21).
const CSRF = { "X-HV": "1" };
const $ = (id) => document.getElementById(id);
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
const ESTADOS = { Borrador: "○ Borrador", Aprobado: "✓ Aprobado", Exportado: "✓ Exportado" };
const ROLES = { coordinador: "Coordinador de Extensión", rgc: "RGC" };
const OTRO_ROL = { coordinador: "rgc", rgc: "coordinador" };

const nombreDeCarrera = {}; // slug → nombre (las dadas de baja se muestran por su slug)
let permisos = [];
let yo = null;
let abierto = null;
let informeAbierto = null;

const diaLlano = (iso) => {
  const [a, m, d] = iso.split("-").map(Number);
  return `${d} de ${MESES[m - 1]} de ${a}`;
};

// «2018 a 2022» si el período son años enteros; si no, de día a día.
function periodo(desde, hasta) {
  if (desde.endsWith("-01-01") && hasta.endsWith("-12-31")) {
    const [a, b] = [desde.slice(0, 4), hasta.slice(0, 4)];
    return a === b ? a : `${a} a ${b}`;
  }
  return `del ${diaLlano(desde)} al ${diaLlano(hasta)}`;
}

// M2 sobre las Piezas del informe (DIR-38): el porcentaje se trunca, así 94,99 % no se lee 95 %.
const META_INTEGRIDAD = 0.95;
const porcentaje = (valor) => `${Math.floor(Math.round(valor * 10000) / 100)} %`;
const integridadBaja = ({ valor }) => valor !== null && valor < META_INTEGRIDAD;

// Solo un Borrador que no se reemplazó ni se descartó admite aprobar, descartar o generar de nuevo (R-22).
const vigente = (informe) =>
  informe.estado === "Borrador" && !informe.reemplazado_por && !informe.descartado_el;

function estadoComoTexto(informe) {
  if (informe.reemplazado_por) return "↻ Reemplazado";
  if (informe.descartado_el) return "Descartado";
  return ESTADOS[informe.estado] || informe.estado;
}

// E9: «Cambió después de aprobar: la Pieza “acta-2021-04.pdf” fue retirada. …»
function avisoDeCambios(cambios) {
  const partes = cambios.map(({ nombre_archivo: nombre, retirada }) => retirada
    ? `la Pieza “${nombre}” fue retirada` : `la Ficha de la Pieza “${nombre}” cambió`);
  return `Cambió después de aprobar: ${partes.join("; ")}. Generá un Borrador nuevo para incorporar el cambio.`;
}

function integridadEnLista(integridad) {
  if (integridad.valor === null) return "—";
  const aviso = integridadBaja(integridad) ? " · Aviso: menos de 95 %" : "";
  return porcentaje(integridad.valor) + aviso;
}

const carrerasComoTexto = (carreras) =>
  carreras.length ? carreras.map((c) => nombreDeCarrera[c] || c).join(", ") : "Todas";

function sesionVencida() {
  location.href = "/?error=sesion-vencida";
}

function elemento(etiqueta, texto, clase) {
  const nodo = document.createElement(etiqueta);
  if (texto !== undefined) nodo.textContent = texto;
  if (clase) nodo.className = clase;
  return nodo;
}

// ── Lista ────────────────────────────────────────────────────────────────────────────────
async function cargarLista() {
  $("estado-lista").textContent = "Cargando los informes…";
  const respuesta = await fetch("/api/informes");
  if (respuesta.status === 401) return sesionVencida();
  if (!respuesta.ok) {
    $("estado-lista").textContent = "No pudimos cargar los informes.";
    return;
  }
  const { informes } = await respuesta.json();
  const filas = informes.map((informe) => {
    const fila = document.createElement("tr");
    const abrir = elemento("button", "Abrir");
    abrir.type = "button";
    abrir.setAttribute("aria-label", `Abrir el informe ${periodo(informe.desde, informe.hasta)}`);
    abrir.addEventListener("click", () => abrirInforme(informe.informe_id));
    const accion = elemento("td");
    accion.append(abrir);
    fila.append(
      elemento("td", periodo(informe.desde, informe.hasta)),
      elemento("td", carrerasComoTexto(informe.carreras)),
      elemento("td", ESTADOS[informe.estado] || informe.estado),
      elemento("td", integridadEnLista(informe.integridad)),
      elemento("td", `${informe.aprobaciones.length} de 2`),
      accion,
    );
    return fila;
  });
  $("informes").replaceChildren(...filas);
  $("tabla").hidden = !filas.length;
  $("estado-lista").textContent = filas.length ? "" : (permisos.includes("GENERAR_INFORME")
    ? "Todavía no hay informes."
    : "Todavía no hay informes. Los genera el Coordinador de Extensión.");
}

// ── Nuevo informe ────────────────────────────────────────────────────────────────────────
async function cargarCarreras() {
  const respuesta = await fetch("/api/carreras");
  if (!respuesta.ok) return;
  for (const { carrera_id: id, nombre } of (await respuesta.json()).carreras) {
    nombreDeCarrera[id] = nombre;
    const etiqueta = document.createElement("label");
    const casilla = document.createElement("input");
    casilla.type = "checkbox";
    casilla.value = id;
    etiqueta.append(casilla, document.createTextNode(nombre));
    $("carreras").append(etiqueta);
  }
}

function mostrarFormulario(visible) {
  $("formulario").hidden = !visible;
  $("nuevo").hidden = visible || !permisos.includes("GENERAR_INFORME");
  $("error-generar").hidden = true;
  if (visible) $("desde").focus();
  else $("titulo-lista").focus();
}

async function generar(evento) {
  evento.preventDefault();
  $("generar").disabled = true;
  $("error-generar").hidden = true;
  try {
    const carreras = [...$("carreras").querySelectorAll("input:checked")].map((c) => c.value);
    const respuesta = await fetch("/api/informes", {
      method: "POST",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify({ desde: $("desde").value, hasta: $("hasta").value, carreras }),
    });
    if (respuesta.status === 401) return sesionVencida();
    const cuerpo = await respuesta.json();
    if (!respuesta.ok) {
      // R22 E1 y los demás 422 traen el texto para la persona en `detail`.
      $("error-generar").textContent = cuerpo.detail || "No se pudo generar el informe.";
      $("error-generar").hidden = false;
      return;
    }
    mostrarFormulario(false);
    await cargarLista();
    mostrarDetalle(cuerpo);
  } finally {
    $("generar").disabled = false;
  }
}

// ── Detalle ──────────────────────────────────────────────────────────────────────────────
async function abrirInforme(informeId, foco) {
  abierto = informeId;
  $("lista").hidden = true;
  $("detalle").hidden = false;
  $("error-detalle").hidden = true;
  $("estado-detalle").textContent = "Cargando el informe…";
  const respuesta = await fetch(`/api/informes/${encodeURIComponent(informeId)}`);
  if (respuesta.status === 401) return sesionVencida();
  if (!respuesta.ok) {
    $("estado-detalle").textContent = "";
    $("error-detalle").hidden = false;
    return;
  }
  mostrarDetalle(await respuesta.json(), foco);
}

function piezaComoItem(pieza) {
  const item = document.createElement("li");
  const anio = pieza.fecha_inicio ? `${pieza.fecha_inicio.slice(0, 4)} · ` : "";
  item.append(document.createTextNode(`${anio}${pieza.resumen || ""} `));
  for (const cita of pieza.citas) {
    const enlace = elemento("a", cita.id, "cita");
    // Ruta propia, no el `enlace` absoluto: la pantalla solo arma rutas suyas (RNF-21).
    enlace.href = `/api/piezas/${encodeURIComponent(pieza.pieza_id)}/original`;
    item.append(document.createTextNode("Cita: "), enlace);
  }
  return item;
}

const momento = (iso) => new Date(iso).toLocaleString("es-AR", { dateStyle: "long", timeStyle: "short" });

// Quién y cuándo aprobó cada rol, o «falta»; la firma hecha con `dev` se marca de ensayo.
function pintarAprobaciones(informe) {
  const aprobadas = Object.fromEntries(informe.aprobaciones.map((a) => [a.rol, a]));
  $("aprobaciones").replaceChildren(...Object.entries(ROLES).map(([rol, nombre]) => {
    const firma = aprobadas[rol];
    const ensayo = firma && firma.metodo_autenticacion === "dev" ? " (firma de ensayo)" : "";
    return elemento("li", firma
      ? `${nombre}: ✓ ${firma.nombre} · ${momento(firma.aprobada_el)}${ensayo}`
      : `${nombre}: falta`);
  }));
  const yaAprobo = informe.aprobaciones.some((a) => a.aprobada_por === yo.usuario_id);
  $("aprobar").hidden = !(permisos.includes("APROBAR_INFORME") && vigente(informe) && !yaAprobo);
}

// ── M4: aprobar ──────────────────────────────────────────────────────────────────────────
function abrirM4() {
  const informe = informeAbierto;
  const { integridad } = informe;
  $("titulo-m4").textContent =
    `¿Aprobar el informe ${periodo(informe.desde, informe.hasta)} · ${carrerasComoTexto(informe.carreras)}?`;
  $("m4-rol").textContent = `Aprobás como: ${ROLES[yo.rol] || yo.rol} (${yo.nombre})`;
  const piezas = integridad.piezas_total === 1 ? "1 Pieza Validada"
    : `${integridad.piezas_total} Piezas Validadas`;
  $("m4-piezas").textContent = integridad.valor === null ? `Incluye ${piezas}.`
    : `Incluye ${piezas}; integridad ${porcentaje(integridad.valor)}.`;
  const faltantes = informe.faltantes.length;
  $("m4-faltantes").textContent = faltantes === 1
    ? "1 Pieza del período no está Validada (faltante del informe)."
    : `${faltantes} Piezas del período no están Validadas (faltantes del informe).`;
  const otro = OTRO_ROL[yo.rol];
  const falta = !informe.aprobaciones.some((a) => a.rol === otro);
  $("m4-falta").textContent = falta
    ? `Falta la aprobación de una persona con rol ${ROLES[otro]}. Con las dos, el informe queda`
      + " Aprobado y se puede exportar."
    : "Con tu aprobación el informe queda Aprobado.";
  $("m4-aviso").hidden = !integridadBaja(integridad);
  $("m4-error").hidden = true;
  $("m4-actualizar").hidden = true;
  $("m4-aprobar").disabled = false;
  $("m4").showModal();
  $("m4-cancelar").focus();
}

function errorM4(texto, actualizar) {
  $("m4-error-texto").textContent = texto;
  $("m4-actualizar").hidden = !actualizar;
  $("m4-error").hidden = false;
}

async function aprobar() {
  $("m4-aprobar").disabled = true;
  try {
    const respuesta = await fetch(
      `/api/informes/${encodeURIComponent(informeAbierto.informe_id)}/aprobaciones`,
      { method: "POST", headers: CSRF },
    );
    if (respuesta.status === 401) return sesionVencida();
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (respuesta.status === 409) {
      // Las dos del catálogo traen el texto para la persona; lo demás es que otro lo cambió.
      const propio = ["/errores/aprobador-repetido", "/errores/informe-ya-aprobado-por-rol",
        "/errores/informe-desactualizado"];
      errorM4(propio.includes(cuerpo.type) ? cuerpo.detail
        : "Este informe cambió mientras lo revisabas.", true);
      return;
    }
    if (!respuesta.ok) {
      errorM4("No se pudo registrar la aprobación. El informe sigue como estaba.", false);
      $("m4-aprobar").disabled = false;
      return;
    }
    $("m4").close();
    await cargarLista();
    mostrarDetalle(cuerpo, "aprobar");
  } catch {
    errorM4("No se pudo registrar la aprobación. El informe sigue como estaba.", false);
    $("m4-aprobar").disabled = false;
  }
}

// ── Exportar (R-23) ─────────────────────────────────────────────────────────────────────
// Exportan quienes generan o aprueban; en un Borrador vigente se ve apagado y dice por qué.
function pintarExportar(informe) {
  const exporta = permisos.includes("GENERAR_INFORME") || permisos.includes("APROBAR_INFORME");
  const aprobado = informe.estado !== "Borrador";
  $("acciones-exportar").hidden = !exporta || !(aprobado || vigente(informe));
  for (const boton of document.querySelectorAll("[data-formato]")) boton.disabled = !aprobado;
  $("exportar-apagado").hidden = aprobado;
  $("error-exportar").hidden = true;
}

// El archivo llega por `fetch` para mostrar el error en la pantalla; la primera exportación lo
// deja Exportado, así que se relee el informe.
async function exportar(evento) {
  const boton = evento.currentTarget;
  const { informe_id: informeId, estado } = informeAbierto;
  $("error-exportar").hidden = true;
  boton.disabled = true;
  try {
    const respuesta = await fetch(`/api/informes/${encodeURIComponent(informeId)}/exportacion`
      + `?formato=${encodeURIComponent(boton.dataset.formato)}`);
    if (respuesta.status === 401) return sesionVencida();
    if (!respuesta.ok) {
      const cuerpo = await respuesta.json().catch(() => ({}));
      $("error-exportar").textContent = cuerpo.detail || "No se pudo exportar el informe.";
      $("error-exportar").hidden = false;
      return;
    }
    const disposicion = respuesta.headers.get("Content-Disposition") || "";
    const enlace = document.createElement("a");
    enlace.href = URL.createObjectURL(await respuesta.blob());
    enlace.download = (/filename="([^"]+)"/.exec(disposicion) || [])[1]
      || `informe.${boton.dataset.formato}`;
    enlace.click();
    setTimeout(() => URL.revokeObjectURL(enlace.href), 1000);
    if (estado === "Aprobado") {
      await cargarLista();
      await abrirInforme(informeId, boton.id);
    }
  } catch {
    $("error-exportar").textContent = "No se pudo exportar el informe.";
    $("error-exportar").hidden = false;
  } finally {
    boton.disabled = false;
  }
}

// ── Descartar y Generar de nuevo (R-22) ──────────────────────────────────────────────────
function errorBorrador(texto) {
  $("error-borrador").textContent = texto;
  $("error-borrador").hidden = false;
}

async function accionSobreBorrador(boton, pedido) {
  $("error-borrador").hidden = true;
  boton.disabled = true;
  try {
    const respuesta = await pedido();
    if (respuesta.status === 401) return sesionVencida();
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (!respuesta.ok) return errorBorrador(cuerpo.detail || "No se pudo. El informe sigue como estaba.");
    await cargarLista();
    mostrarDetalle(cuerpo);
  } catch {
    errorBorrador("No se pudo. El informe sigue como estaba.");
  } finally {
    boton.disabled = false;
  }
}

function generarDeNuevo() {
  const { desde, hasta, carreras, informe_id: reemplaza } = informeAbierto;
  return accionSobreBorrador($("generar-de-nuevo"), () => fetch("/api/informes", {
    method: "POST",
    headers: { ...CSRF, "Content-Type": "application/json" },
    body: JSON.stringify({ desde, hasta, carreras, reemplaza }),
  }));
}

function descartar() {
  if (!confirm("¿Descartar este Borrador? Deja de listarse y ya no se puede aprobar.")) return;
  return accionSobreBorrador($("descartar"), () => fetch(
    `/api/informes/${encodeURIComponent(informeAbierto.informe_id)}/descarte`,
    { method: "POST", headers: CSRF },
  ));
}

async function actualizar() {
  $("m4").close();
  await abrirInforme(informeAbierto.informe_id);
}

function mostrarDetalle(informe, foco = "titulo-detalle") {
  abierto = informe.informe_id;
  informeAbierto = informe;
  $("lista").hidden = true;
  $("detalle").hidden = false;
  $("estado-detalle").textContent = "";
  $("titulo-detalle").textContent =
    `Informe ${periodo(informe.desde, informe.hasta)} · ${carrerasComoTexto(informe.carreras)}`;
  $("estado-informe").textContent = estadoComoTexto(informe);
  $("reemplazado").hidden = !informe.reemplazado_por;
  $("aviso-cambios").hidden = !informe.cambio_despues_de_aprobar;
  if (informe.cambio_despues_de_aprobar) $("aviso-cambios").textContent = avisoDeCambios(informe.cambios);
  $("acciones-borrador").hidden = !(vigente(informe) && permisos.includes("GENERAR_INFORME"));
  $("error-borrador").hidden = true;
  const { integridad } = informe;
  const piezas = integridad.piezas_total === 1 ? "la Pieza incluida"
    : `las ${integridad.piezas_total} Piezas incluidas`;
  $("integridad").textContent = integridad.valor === null ? ""
    : `Integridad: ${porcentaje(integridad.valor)} (M2 sobre ${piezas})`;
  $("aviso-integridad").hidden = !integridadBaja(integridad);
  // Los faltantes son otra cosa (Piezas sin validar): esto es el índice de las incluidas.
  const fallan = integridad.piezas_total - integridad.piezas_completas;
  const total = integridad.piezas_total;
  const piezasNoPasan = `${total === 1 ? "Pieza" : "Piezas"} no ${fallan === 1 ? "pasa" : "pasan"}`;
  $("aviso-integridad").textContent = `Aviso: la integridad no llega a 95 %: ${fallan} de `
    + `${total} ${piezasNoPasan} M2 (sin indexar o con el índice alterado).`;
  pintarAprobaciones(informe);
  pintarExportar(informe);
  const grupos = informe.grupos.map((grupo) => {
    const bloque = document.createElement("section");
    const cantidad = grupo.piezas.length === 1 ? "1 Pieza" : `${grupo.piezas.length} Piezas`;
    const lista = document.createElement("ul");
    lista.append(...grupo.piezas.map(piezaComoItem));
    bloque.append(elemento("h3", `${grupo.tipo_actividad} (${cantidad})`), lista);
    return bloque;
  });
  $("grupos").replaceChildren(...grupos);
  const faltantes = informe.faltantes;
  // Quien solo consulta no ve Piezas no Validadas (R-05): la API no le manda faltantes.
  const veFaltantes = permisos.some((permiso) => permiso !== "CONSULTAR");
  $("titulo-faltantes").hidden = $("faltantes").hidden = !veFaltantes;
  $("titulo-faltantes").textContent = faltantes.length
    ? `Faltantes (${faltantes.length}): Piezas del período que no están Validadas`
    : informe.carreras.length
      ? "Faltantes (0): ninguna Pieza del período sin validar tiene esas Carreras"
      : "Faltantes (0): todas las Piezas del período están Validadas";
  $("faltantes").replaceChildren(...faltantes.map((faltante) => {
    const item = elemento("li", `${faltante.nombre_archivo} · ${faltante.estado} (no está Validada) `);
    const ver = elemento("a", "Ver la Pieza");
    ver.href = `/validar.html?pieza=${encodeURIComponent(faltante.pieza_id)}`;
    item.append(ver);
    return item;
  }));
  // Tras aprobar, el foco vuelve a «Aprobar» o, si ya aprobó, al encabezado de aprobaciones.
  $(foco === "aprobar" && $("aprobar").hidden ? "titulo-aprobaciones" : foco).focus();
}

function volver() {
  abierto = null;
  $("detalle").hidden = true;
  $("lista").hidden = false;
  $("titulo-lista").focus();
}

// ── Inicio ───────────────────────────────────────────────────────────────────────────────
async function iniciar() {
  const respuestaYo = await fetch("/api/yo");
  if (respuestaYo.status === 401) return sesionVencida();
  const persona = await respuestaYo.json();
  yo = persona;
  permisos = persona.permisos;
  $("persona").textContent = `${persona.nombre} · ${persona.rol}`;
  $("nuevo").hidden = !permisos.includes("GENERAR_INFORME");
  await cargarCarreras();
  await cargarLista();
}

$("nuevo").addEventListener("click", () => mostrarFormulario(true));
$("cancelar").addEventListener("click", () => mostrarFormulario(false));
$("formulario").addEventListener("submit", generar);
$("volver").addEventListener("click", volver);
$("reintentar").addEventListener("click", () => abrirInforme(abierto));
$("aprobar").addEventListener("click", abrirM4);
$("m4-cancelar").addEventListener("click", () => $("m4").close());
$("m4-aprobar").addEventListener("click", aprobar);
$("m4-actualizar").addEventListener("click", actualizar);
$("generar-de-nuevo").addEventListener("click", generarDeNuevo);
$("descartar").addEventListener("click", descartar);
document.querySelectorAll("[data-formato]").forEach((b) => b.addEventListener("click", exportar));
$("ver-nuevo").addEventListener("click", () => abrirInforme(informeAbierto.reemplazado_por));
$("m4").addEventListener("close", () => {
  if (!$("detalle").hidden && document.activeElement === document.body) $("aprobar").focus();
});
iniciar();
