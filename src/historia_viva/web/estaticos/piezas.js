"use strict";
// Lo que comparten Mis Piezas, el detalle del Lote y la lista de Lotes: la espera estimada de un
// Lote y las acciones de una Recibida fallida (033, 034), estas solo en las dos primeras.

// La espera de un Lote (086, DEC-25) en minutos, solo mientras tiene Recibidas sin fallar
// (`esperan`); `segundos` es el `estimacion` de la API, `null` mientras no terminó ningún
// procesamiento.
function textoDeEspera(esperan, segundos) {
  if (!esperan) return "";
  if (segundos === null) return "Todavía no hay estimación";
  const minutos = Math.max(1, Math.ceil(segundos / 60));
  const horas = Math.floor(minutos / 60);
  return `Espera estimada: ${horas ? `${horas} h ` : ""}${minutos % 60} min`;
}

// «⚠ Alerta» de contenido sospechoso (109): glifo y palabra, nunca solo color. Avisa, no bloquea.
function marcaDeAlerta() {
  const alerta = document.createElement("span");
  alerta.className = "alerta";
  alerta.textContent = "⚠ Alerta";
  return alerta;
}

// Las acciones de una Recibida fallida, compartidas por Mis Piezas y el detalle del
// Lote. Cada pantalla dice qué hacer después: `recargar` su lista, `avisar` en su único aviso y
// qué pasa con la sesión vencida (`sesionVencida`). La fila tiene `.marca` y `.accion`.

// Reintentar para un fallo pasajero; Completar a mano para uno determinista, que reintentar no
// arregla (DEC-122). Los dos exigen CARGAR: la pantalla los muestra solo con ese permiso.
function accionesDeFallida(piezaId, fila, pantalla) {
  const reintentar = document.createElement("button");
  reintentar.type = "button";
  reintentar.className = "boton reintentar";
  reintentar.textContent = "Reintentar";
  reintentar.addEventListener("click", () => reintentarPieza(piezaId, fila, pantalla));
  const aMano = document.createElement("button");
  aMano.type = "button";
  aMano.className = "boton";
  aMano.textContent = "Completar a mano";
  aMano.addEventListener("click", () => completarPiezaAMano(piezaId, fila, pantalla));
  return [reintentar, aMano];
}

async function reintentarPieza(piezaId, fila, { recargar, avisar, sesionVencida }) {
  try {
    const respuesta = await fetch(`/api/piezas/${encodeURIComponent(piezaId)}/reproceso`, {
      method: "POST", headers: { "X-HV": "1" },
    });
    if (respuesta.status === 401) return sesionVencida();
    if (!respuesta.ok) {
      const problema = await respuesta.json().catch(() => ({}));
      // Seis fallos: otro intento solo lo fuerza el Administrador técnico por comando.
      if (problema.type === "/errores/tope-de-reprocesos") {
        fila.querySelector(".marca").textContent = problema.detail;
        // «Completar a mano» sigue: es la salida cuando ya no se puede reintentar (DEC-122).
        fila.querySelector(".accion .reintentar")?.remove();
        return;
      }
      // Ya no está Recibida (E1), u otra persona la reintentó antes (E3): un solo aviso para
      // la lista, nunca por fila, y la lista se recarga sola (recorrido R4).
      if (problema.type === "/errores/transicion-invalida") {
        avisar(problema.estado_actual === "Recibida"
          ? "Se actualizó la lista"
          : "Esta Pieza ya fue procesada. Actualizá la lista.");
      }
    }
    await recargar();
  } catch {
    // Sin red: la fila se queda como estaba, el botón se puede volver a apretar.
  }
}

// Pasa a Pendiente con la Ficha vacía y se valida en la pantalla partida, con la marca
// «Requiere análisis humano». Si el servidor la rechaza, el porqué queda en la fila.
async function completarPiezaAMano(piezaId, fila, { sesionVencida }) {
  try {
    const respuesta = await fetch(`/api/piezas/${encodeURIComponent(piezaId)}/completar-a-mano`, {
      method: "POST", headers: { "X-HV": "1" },
    });
    if (respuesta.ok) {
      location.href = `/validar.html?pieza=${encodeURIComponent(piezaId)}`;
      return;
    }
    if (respuesta.status === 401) return sesionVencida();
    const problema = await respuesta.json().catch(() => ({}));
    fila.querySelector(".marca").textContent = problema.detail || "No se pudo completar a mano.";
  } catch {
    // Sin red: la fila se queda como estaba, el botón se puede volver a apretar.
  }
}
