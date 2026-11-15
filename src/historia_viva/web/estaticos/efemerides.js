"use strict";
// P8 Efemérides (061): lista por estado y por difusión, revisión y «Aprobar», que firma; 062 suma la edición con
// `version_base` (modal M1 ante un 409) y el descarte con motivo; 065, la pestaña Relatos, con la misma revisión
// sobre `/api/relatos`. El texto lo redactó un modelo: es contenido no confiable y, como todo lo que llega del
// servidor, se pinta con `textContent` (RNF-21).
const CSRF = { "X-HV": "1" };
const $ = (id) => document.getElementById(id);
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
const ESTADOS = { Propuesta: "○ Propuesta", Aprobada: "✓ Aprobada", Descartada: "✕ Descartada" };
// Lo que cambia entre las dos pestañas; la revisión es la misma (DEC-147).
const PESTANAS = {
  efemerides: {
    api: "/api/efemerides", id: "efemeride_id", lista: "efemerides", fechas: true,
    titulo: "Efemérides", tituloLectura: "Efemérides aprobadas", El: "La efeméride", esta: "esta efeméride",
    propuesta: "Propuesta",
    vacio: {
      Propuesta: "No hay efemérides propuestas. El Administrador técnico puede generarlas desde Administración.",
      Aprobada: "No hay efemérides aprobadas.",
      Descartada: "No hay efemérides descartadas.",
    },
    sinResultados: "No hay efemérides con ese filtro.",
    cargandoLista: "Cargando efemérides…", errorLista: "No pudimos cargar las efemérides.",
    cargando: "Cargando la efeméride…", errorDetalle: "No pudimos cargar la efeméride.",
    nota: "Texto del modelo: verificá que no invente nada y que siga la plantilla.",
    fuentes: "Fichas fuente",
    confirmar: "Vas a aprobar esta efeméride. Queda firmada a tu nombre y el Agente 1 podrá pedirla.",
    descarte: "Queda descartada con tu nombre y el motivo, y no se vuelve a generar sola.",
  },
  relatos: {
    api: "/api/relatos", id: "relato_id", lista: "relatos", fechas: false,
    titulo: "Relatos", tituloLectura: "Relatos aprobados", El: "El relato", esta: "este relato",
    propuesta: "Propuesto",
    vacio: {
      Propuesta: "No hay relatos propuestos. Se proponen desde una respuesta de la Conversación.",
      Aprobada: "No hay relatos aprobados.",
      Descartada: "No hay relatos descartados.",
    },
    sinResultados: "No hay relatos con ese filtro.",
    cargandoLista: "Cargando relatos…", errorLista: "No pudimos cargar los relatos.",
    cargando: "Cargando el relato…", errorDetalle: "No pudimos cargar el relato.",
    nota: "Texto de la respuesta de la Conversación: verificá que no invente nada.",
    fuentes: "Fichas fuente (las Citas de la respuesta original)",
    confirmar: "Vas a aprobar este relato con sus Citas. Queda firmado a tu nombre y el Agente 1 podrá pedirlo.",
    descarte: "Queda descartado con tu nombre y el motivo. La respuesta se puede volver a proponer desde la Conversación.",
  },
};

let yo = null;
let revisa = false; // REVISAR_EFEMERIDES; sin él, solo las Aprobadas y en lectura (R-05)
let p = PESTANAS.efemerides; // la pestaña abierta
let abierta = null;
const botones = new Map(); // id de la efeméride o del Relato → su botón en la lista
const idDe = (e) => e[p.id];
const url = (e, sufijo = "") => `${p.api}/${encodeURIComponent(idDe(e))}${sufijo}`;

const fechaDe = (e) => (e.tipo === "dia" ? `Un día como hoy: ${e.dia} de ${MESES[e.mes - 1]}` : `En ${MESES[e.mes - 1]}`);
const momento = (iso) => new Date(iso).toLocaleString("es-AR", { dateStyle: "long", timeStyle: "short" });
const fichas = (n) => `${n} ${n === 1 ? "Ficha" : "Fichas"}`;

// Glifo y palabra; la Aprobada dice quién la firmó y cuándo, y si la firma fue de ensayo (DEC-139).
// El Relato nombra sus campos en masculino (`aprobado_por_nombre`, …).
function estadoComoTexto(e) {
  let texto = ESTADOS[e.estado] || e.estado;
  const firmante = e.aprobada_por_nombre ?? e.aprobado_por_nombre;
  const descartante = e.descartada_por_nombre ?? e.descartado_por_nombre;
  if (e.estado === "Aprobada" && firmante) {
    texto += ` · firmada por ${firmante}, ${momento(e.aprobada_el ?? e.aprobado_el)}`;
    if (e.metodo_autenticacion === "dev") texto += " (firma de ensayo)";
  }
  if (e.estado === "Descartada" && descartante) {
    texto = `✕ Descartada por ${descartante}: “${e.motivo_descarte}”`;
  }
  return texto;
}

function marcas(e) {
  const lista = [estadoComoTexto(e)];
  if (e.aviso_inyeccion) lista.push("⚠ Alerta en una Ficha fuente");
  if (e.fuente_retirada) lista.push("⚠ Fuente retirada");
  return lista;
}

function sesionVencida() {
  location.href = "/?error=sesion-vencida";
}

function elemento(etiqueta, texto, clase) {
  const nodo = document.createElement(etiqueta);
  if (texto !== undefined) nodo.textContent = texto;
  if (clase) nodo.className = clase;
  return nodo;
}

function mostrarFranja(efemerides) {
  if (efemerides.some((e) => e.plantilla_provisoria)) $("franja-plantilla").hidden = false;
}

// Un Relato no tiene fecha de efeméride: la fila dice cuándo se propuso y cuántas Fichas cita.
function encabezado(e) {
  if (!("relato_id" in e)) return [elemento("span", fechaDe(e))];
  const fecha = new Date(e.propuesto_el).toLocaleDateString("es-AR", { dateStyle: "long" });
  return [elemento("span", e.titulo), elemento("span", `Propuesto el ${fecha} · ${fichas(e.cantidad_fuentes)}`, "marca")];
}

// ── Lista ────────────────────────────────────────────────────────────────────────────────
function pintarFila(boton, e) {
  boton.replaceChildren(...encabezado(e), ...marcas(e).map((m) => elemento("span", m, "marca")));
}

async function cargarLista() {
  const estado = revisa ? document.querySelector('input[name="estado"]:checked').value : "Aprobada";
  const pedido = new URLSearchParams();
  if (estado) pedido.append("estado", estado);
  if (p.fechas && $("desde").value) pedido.set("desde", $("desde").value);
  if (p.fechas && $("hasta").value) pedido.set("hasta", $("hasta").value);
  $("error-lista").hidden = true;
  $("efemerides").replaceChildren();
  botones.clear();
  $("estado-lista").textContent = p.cargandoLista;
  let respuesta = null;
  try {
    respuesta = await fetch(`${p.api}?${pedido}`);
  } catch {
    // sin red: cae en el error de abajo
  }
  if (respuesta && respuesta.status === 401) return sesionVencida();
  if (respuesta && respuesta.status === 422) {
    $("estado-lista").textContent = (await respuesta.json()).detail; // el rango está al revés
    return;
  }
  if (!respuesta || !respuesta.ok) {
    $("estado-lista").textContent = "";
    $("error-lista").hidden = false;
    return;
  }
  const filas = (await respuesta.json())[p.lista];
  mostrarFranja(filas);
  $("efemerides").replaceChildren(...filas.map((e) => {
    const boton = elemento("button");
    boton.type = "button";
    boton.setAttribute("aria-current", String(abierta !== null && idDe(abierta) === idDe(e)));
    pintarFila(boton, e);
    boton.addEventListener("click", () => abrir(idDe(e)));
    botones.set(idDe(e), boton);
    const item = elemento("li");
    item.append(boton);
    return item;
  }));
  $("estado-lista").textContent = filas.length ? "" : p.vacio[estado] || p.sinResultados;
}

// ── Revisión ─────────────────────────────────────────────────────────────────────────────
async function abrir(id) {
  abierta = { [p.id]: id };
  for (const [otro, boton] of botones) boton.setAttribute("aria-current", String(otro === id));
  $("detalle").hidden = false;
  $("error-detalle").hidden = true;
  $("estado-detalle").textContent = p.cargando;
  let respuesta = null;
  try {
    respuesta = await fetch(url(abierta));
  } catch {
    // sin red: cae en el error de abajo
  }
  if (respuesta && respuesta.status === 401) return sesionVencida();
  if (!respuesta || !respuesta.ok) {
    $("estado-detalle").textContent = "";
    $("error-detalle").hidden = false;
    return;
  }
  mostrarDetalle(await respuesta.json());
  $("titulo-detalle").focus();
}

function fuenteComoItem(fuente, indice) {
  const item = elemento("li");
  const anio = fuente.fecha_inicio ? ` · ${fuente.fecha_inicio}` : "";
  // El Relato trae el `[n]` de cada fuente: puede saltear números (065).
  const n = fuente.posicion ?? indice + 1;
  item.append(document.createTextNode(`[${n}] ${fuente.resumen || "Sin resumen"}${anio} `));
  if (fuente.alerta_inyeccion) item.append(document.createTextNode("⚠ Alerta de contenido sospechoso "));
  if (fuente.retirada) item.append(elemento("strong", "Retirada del Acervo "));
  const original = elemento("a", "Abrir el original");
  // Ruta propia, no la `url_original` del servidor: la pantalla solo arma rutas suyas (RNF-21).
  original.href = `/api/piezas/${encodeURIComponent(fuente.pieza_id)}/original`;
  original.target = "_blank";
  original.rel = "noopener";
  item.append(original);
  return item;
}

function mostrarDetalle(e) {
  abierta = e;
  mostrarFranja([e]);
  $("estado-detalle").textContent = "";
  $("titulo-detalle").textContent = e.titulo;
  $("estado-efemeride").textContent = marcas(e).join(" · ");
  $("aviso-inyeccion").hidden = !e.aviso_inyeccion;
  $("aviso-retirada").hidden = !e.fuente_retirada;
  $("texto").value = e.texto;
  const editor = e.editada_por ?? e.editado_por;
  $("marca-edicion").textContent = editor && editor === yo.email ? "Editado por vos" : "";
  $("fuentes").replaceChildren(...e.fuentes.map(fuenteComoItem));
  actualizarAcciones();
  const boton = botones.get(idDe(e));
  if (boton) pintarFila(boton, e);
}

// ── Editar (062) ─────────────────────────────────────────────────────────────────────────
// Solo una Propuesta se edita (R-12). «Aprobar» firma lo guardado: se apaga sin texto o con cambios sin guardar.
function actualizarAcciones() {
  const editable = revisa && abierta.estado === "Propuesta";
  const texto = $("texto").value;
  const vacio = !texto.trim();
  const cambiado = texto !== abierta.texto;
  $("texto").readOnly = !editable;
  for (const id of ["guardar", "aprobar", "descartar"]) $(id).hidden = !editable;
  $("guardar").disabled = vacio || !cambiado;
  $("aprobar").disabled = vacio || cambiado;
  $("pendiente").textContent = !editable ? "" : vacio ? `Falta: texto de ${p.El.toLowerCase()}`
    : cambiado ? "Guardá los cambios antes de aprobar." : "";
}

async function guardar() {
  $("guardar").disabled = true;
  let respuesta = null;
  try {
    respuesta = await fetch(url(abierta), {
      method: "PUT",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify({ version_base: abierta.version, texto: $("texto").value }),
    });
  } catch {
    // sin red: cae en el error de abajo
  }
  if (respuesta && respuesta.status === 401) return sesionVencida();
  const cuerpo = respuesta ? await respuesta.json().catch(() => ({})) : {};
  if (respuesta && respuesta.ok) {
    mostrarDetalle(cuerpo);
    $("estado-detalle").textContent = "Cambios guardados.";
    return;
  }
  if (respuesta && cuerpo.type === "/errores/version-desactualizada") {
    $("conflicto-texto").textContent = `${cuerpo.detail} Lo que escribiste sigue en el texto.`;
    $("conflicto").showModal();
  } else {
    // Ya no está Propuesta, o el texto no es válido: el servidor dice qué pasó; lo escrito queda.
    $("pendiente").textContent = cuerpo.detail || "No se pudieron guardar los cambios. Lo escrito sigue en el texto.";
  }
  $("guardar").disabled = false;
}

// M1: «Aplicar» trae la versión nueva y deja lo tuyo encima, sin guardar; «Descartar» se queda con la nueva.
async function resolverConflicto(aplicar) {
  const borrador = $("texto").value;
  $("conflicto").close();
  await abrir(idDe(abierta));
  if (!aplicar || !abierta.fuentes) return;
  if (abierta.estado !== "Propuesta") {
    $("pendiente").textContent = `${p.El} ya no está ${p.propuesta}: tus cambios no se pueden guardar.`;
    return;
  }
  $("texto").value = borrador;
  actualizarAcciones();
  $("texto").focus();
}

// ── Descartar (062) ──────────────────────────────────────────────────────────────────────
function abrirDescarte() {
  $("motivo").value = "";
  $("descarte-error").hidden = true;
  $("confirmar-descarte").disabled = false;
  $("dialogo-descarte").showModal();
  $("motivo").focus();
}

function errorAlDescartar(texto) {
  $("descarte-error").textContent = texto;
  $("descarte-error").hidden = false;
  $("confirmar-descarte").disabled = false;
}

async function descartar() {
  const motivo = $("motivo").value.trim();
  if (motivo.length < 5 || motivo.length > 500) return errorAlDescartar("El motivo tiene que tener entre 5 y 500 caracteres.");
  $("confirmar-descarte").disabled = true;
  let respuesta = null;
  try {
    respuesta = await fetch(url(abierta, "/descarte"), {
      method: "POST",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify({ motivo }),
    });
  } catch {
    // sin red: cae en el error de abajo
  }
  if (respuesta && respuesta.status === 401) return sesionVencida();
  const cuerpo = respuesta ? await respuesta.json().catch(() => ({})) : {};
  if (!respuesta || !respuesta.ok) return errorAlDescartar(cuerpo.detail || `No se pudo descartar. ${p.El} sigue ${p.propuesta}.`);
  $("dialogo-descarte").close();
  mostrarDetalle(cuerpo);
  $("titulo-detalle").focus();
}

// ── Aprobar ──────────────────────────────────────────────────────────────────────────────
function abrirConfirmacion() {
  $("confirmar-ensayo").hidden = yo.modo_auth !== "dev";
  $("confirmar-error").hidden = true;
  $("actualizar").hidden = true;
  $("confirmar-aprobar").disabled = false;
  $("confirmar").showModal();
  $("cancelar").focus();
}

function errorAlAprobar(texto, actualizar) {
  $("confirmar-error-texto").textContent = texto;
  $("actualizar").hidden = !actualizar;
  $("confirmar-error").hidden = false;
}

async function aprobar() {
  $("confirmar-aprobar").disabled = true;
  try {
    const respuesta = await fetch(url(abierta, "/aprobacion"), {
      method: "POST",
      headers: { ...CSRF, "Content-Type": "application/json" },
      body: JSON.stringify({ version_base: abierta.version }),
    });
    if (respuesta.status === 401) return sesionVencida();
    const cuerpo = await respuesta.json().catch(() => ({}));
    if (respuesta.status === 409) {
      // Otra persona la aprobó o la cambió: el texto del servidor dice qué pasó.
      errorAlAprobar(cuerpo.detail || `${p.El} cambió durante la revisión.`, true);
      return;
    }
    if (!respuesta.ok) {
      errorAlAprobar(`No se pudo registrar la aprobación. ${p.El} sigue ${p.propuesta}.`, false);
      $("confirmar-aprobar").disabled = false;
      return;
    }
    $("confirmar").close();
    // La fila queda en la lista con su firma hasta que se vuelva a filtrar.
    mostrarDetalle(cuerpo);
    $("titulo-detalle").focus();
  } catch {
    errorAlAprobar(`No se pudo registrar la aprobación. ${p.El} sigue ${p.propuesta}.`, false);
    $("confirmar-aprobar").disabled = false;
  }
}

async function actualizar() {
  $("confirmar").close();
  const id = idDe(abierta);
  await cargarLista();
  await abrir(id);
}

// ── Pestañas (065) ───────────────────────────────────────────────────────────────────────
function elegirPestana(nombre) {
  p = PESTANAS[nombre];
  abierta = null;
  for (const otra of Object.keys(PESTANAS)) $(`pestana-${otra}`).setAttribute("aria-pressed", String(otra === nombre));
  $("detalle").hidden = true;
  $("franja-plantilla").hidden = true;
  $("filtro-fechas").hidden = !p.fechas;
  $("titulo-lista").textContent = revisa ? p.titulo : p.tituloLectura;
  $("error-lista-texto").textContent = p.errorLista;
  $("error-detalle-texto").textContent = p.errorDetalle;
  $("nota-texto").textContent = p.nota;
  $("titulo-fuentes").textContent = p.fuentes;
  $("titulo-confirmar").textContent = `¿Aprobar ${p.esta}?`;
  $("texto-confirmar").textContent = p.confirmar;
  $("titulo-descarte").textContent = `¿Descartar ${p.esta}?`;
  $("texto-descarte").textContent = p.descarte;
  $("conflicto-titulo").textContent = `Otra persona modificó ${p.esta} mientras editabas`;
}

async function cambiarPestana(nombre) {
  elegirPestana(nombre);
  await cargarLista();
}

// ── Inicio ───────────────────────────────────────────────────────────────────────────────
// `?relato=<id>` abre la pestaña Relatos con ese Relato: es el enlace de «Quedó propuesto.» en P6.
async function iniciar() {
  const respuesta = await fetch("/api/yo");
  if (respuesta.status === 401) return sesionVencida();
  yo = await respuesta.json();
  revisa = yo.permisos.includes("REVISAR_EFEMERIDES");
  $("persona").textContent = `${yo.nombre} · ${yo.rol}`;
  $("filtro-estado").hidden = !revisa;
  const relato = new URLSearchParams(location.search).get("relato");
  elegirPestana(relato ? "relatos" : "efemerides");
  await cargarLista();
  if (relato) await abrir(relato);
}

$("filtros").addEventListener("change", cargarLista);
$("filtros").addEventListener("submit", (evento) => evento.preventDefault());
$("reintentar").addEventListener("click", cargarLista);
$("reintentar-detalle").addEventListener("click", () => abrir(idDe(abierta)));
$("pestana-efemerides").addEventListener("click", () => cambiarPestana("efemerides"));
$("pestana-relatos").addEventListener("click", () => cambiarPestana("relatos"));
$("aprobar").addEventListener("click", abrirConfirmacion);
$("cancelar").addEventListener("click", () => $("confirmar").close());
$("confirmar-aprobar").addEventListener("click", aprobar);
$("actualizar").addEventListener("click", actualizar);
$("texto").addEventListener("input", actualizarAcciones);
$("guardar").addEventListener("click", guardar);
$("conflicto-aplicar").addEventListener("click", () => resolverConflicto(true));
$("conflicto-descartar").addEventListener("click", () => resolverConflicto(false));
$("descartar").addEventListener("click", abrirDescarte);
$("cancelar-descarte").addEventListener("click", () => $("dialogo-descarte").close());
$("confirmar-descarte").addEventListener("click", descartar);
$("confirmar").addEventListener("close", () => {
  if (!$("aprobar").hidden && document.activeElement === document.body) $("aprobar").focus();
});
iniciar();
