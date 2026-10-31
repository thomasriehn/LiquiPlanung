"use strict";
// Systemeinstellungen (nur Admin): Microsoft Graph (SharePoint), KI-Zugang,
// lokale Ablage. Geheimnisse kommen nie zum Browser zurück – nur ihr Status.

const sysGeaendert = { graph: false, ki: false };    // ungespeicherte Eingaben je Formular

function sysGeheimText(g, umgebungsName) {
  if (!g) return "";
  if (g.aus_umgebung) return `aus der Umgebungsvariable ${umgebungsName} (hat Vorrang vor einem gespeicherten Wert)`;
  if (g.gesetzt && !g.lesbar) return "gespeichert, aber mit dem aktuellen Schlüssel nicht lesbar – bitte neu eingeben";
  if (g.gesetzt) return "Ein Wert ist gespeichert.";
  return "noch nicht gespeichert";
}

function sysStatusHtml(konfiguriert, textJa, textNein) {
  return konfiguriert
    ? `<span class="status-badge freigegeben">${esc(textJa)}</span>`
    : `<span class="status-badge offen">${esc(textNein)}</span>`;
}

// Status eines Abschnitts (Badge, Secret-Hinweis, Platzhalter) – Eingaben bleiben unberührt
function sysGraphStatus(s) {
  const g = s.graph || {};
  const sec = g.client_secret || {};
  document.getElementById("sys-graph-form").graph_client_secret.placeholder = sec.gesetzt || sec.aus_umgebung
    ? "gespeichert – leer lassen = unverändert" : "Wert des geheimen Clientschlüssels";
  document.getElementById("sys-graph-secret-status").textContent = sysGeheimText(sec, "GRAPH_CLIENT_SECRET");
  document.getElementById("sys-graph-secret-entfernen").style.display = sec.gesetzt ? "" : "none";
  document.getElementById("sys-graph-status").innerHTML =
    sysStatusHtml(g.konfiguriert, "eingerichtet", "nicht vollständig eingerichtet");
}

function sysKiStatus(s) {
  const k = s.llm || {};
  const key = k.api_key || {};
  document.getElementById("sys-ki-form").llm_api_key.placeholder = key.gesetzt || key.aus_umgebung
    ? "gespeichert – leer lassen = unverändert" : "leer = ohne Schlüssel (z. B. lokaler Server)";
  document.getElementById("sys-ki-key-status").textContent = sysGeheimText(key, "LLM_API_KEY");
  document.getElementById("sys-ki-key-entfernen").style.display = key.gesetzt ? "" : "none";
  document.getElementById("sys-ki-status").innerHTML =
    sysStatusHtml(k.konfiguriert, "eingerichtet", "nicht eingerichtet (API-URL und Modell fehlen)");
}

// Formular eines Abschnitts mit den Serverwerten neu belegen
function sysGraphZeigen(s) {
  const gf = document.getElementById("sys-graph-form");
  const g = s.graph || {};
  gf.graph_tenant_id.value = g.tenant_id || "";
  gf.graph_client_id.value = g.client_id || "";
  gf.graph_client_secret.value = "";
  sysGraphStatus(s);
  sysGeaendert.graph = false;
}

function sysKiZeigen(s) {
  const kf = document.getElementById("sys-ki-form");
  const k = s.llm || {};
  kf.llm_url.value = k.url || "";
  kf.llm_modell.value = k.modell || "";
  kf.llm_vision.checked = !!k.vision;
  kf.llm_api_key.value = "";
  sysKiStatus(s);
  sysGeaendert.ki = false;
}

function sysAllgemeinZeigen(s) {
  const a = s.ablage || {};
  document.getElementById("sys-ablage-verzeichnis").textContent = a.verzeichnis || "–";
  document.getElementById("sys-ablage-max").textContent = a.max_mb ? `${a.max_mb} MB je Datei` : "–";

  document.getElementById("sys-schluessel-warnung").innerHTML = s.secret_key_unsicher
    ? `<div class="warnung"><strong>Unsicherer Schlüssel:</strong> SECRET_KEY ist nicht gesetzt (Entwicklungs-Standard).
       Gespeicherte Geheimnisse sind damit nur schwach geschützt. Bitte <code>SECRET_KEY</code> bzw.
       <code>GEHEIMNIS_SCHLUESSEL</code> in der Umgebung setzen und die Geheimnisse danach neu eingeben.</div>`
    : "";
}

function sysZeigen(s) {
  sysGraphZeigen(s);
  sysKiZeigen(s);
  sysAllgemeinZeigen(s);
}

async function sysLaden() {
  try { sysZeigen(await api("GET", "/api/system/einstellungen")); }
  catch (e) { toast(e.message); }
}

// Speichert und belegt nur den gespeicherten Abschnitt („graph“ | „ki“) neu; der
// andere (bzw. ohne Abschnitt: beide) bekommt nur den Status – ungespeicherte Eingaben bleiben
async function sysSpeichern(daten, art = null) {
  const s = await api("PUT", "/api/system/einstellungen", daten);
  if (art === "graph") sysGraphZeigen(s); else sysGraphStatus(s);
  if (art === "ki") sysKiZeigen(s); else sysKiStatus(s);
  sysAllgemeinZeigen(s);
}

function sysGraphDaten() {
  const f = document.getElementById("sys-graph-form");
  const daten = {
    graph_tenant_id: f.graph_tenant_id.value.trim(),
    graph_client_id: f.graph_client_id.value.trim(),
  };
  // Geheimnis nur senden, wenn eingegeben (sonst bleibt der gespeicherte Wert)
  if (f.graph_client_secret.value) daten.graph_client_secret = f.graph_client_secret.value.trim();
  return daten;
}

function sysKiDaten() {
  const f = document.getElementById("sys-ki-form");
  const daten = {
    llm_url: f.llm_url.value.trim(),
    llm_modell: f.llm_modell.value.trim(),
    llm_vision: f.llm_vision.checked,
  };
  if (f.llm_api_key.value) daten.llm_api_key = f.llm_api_key.value.trim();
  return daten;
}

function sysErgebnis(id, html) { document.getElementById(id).innerHTML = html; }

async function sysTesten(art) {
  const knopf = document.getElementById(`sys-${art}-test`);
  const ergebnisId = `sys-${art}-ergebnis`;
  const alt = knopf.textContent;
  knopf.disabled = true;
  knopf.textContent = "Wird geprüft …";
  try {
    // ungespeicherte Eingaben zuerst speichern – getestet wird der gespeicherte Stand
    if (sysGeaendert[art]) {
      await sysSpeichern(art === "graph" ? sysGraphDaten() : sysKiDaten(), art);
      toast("Einstellungen gespeichert.", false);
    }
    sysErgebnis(ergebnisId, `<p class="hinweis">Verbindung wird geprüft …</p>`);
    if (art === "graph") {
      const erg = await api("POST", "/api/system/graph/test");
      sysErgebnis(ergebnisId,
        `<div class="warnung gruen">✓ Anmeldung bei Microsoft Graph erfolgreich` +
        `${erg.sites_lesbar ? " – Websites sind lesbar (Suche verfügbar)." : "."}</div>` +
        (erg.hinweis ? `<div class="warnung">${esc(erg.hinweis)}</div>` : ""));
    } else {
      const erg = await api("POST", "/api/system/ki/test");
      sysErgebnis(ergebnisId,
        `<div class="warnung gruen">✓ Modell <strong>${esc(erg.modell || "")}</strong> antwortet` +
        `${erg.antwort ? `: „${esc(String(erg.antwort).slice(0, 200))}“` : "."}</div>`);
    }
  } catch (e) {
    sysErgebnis(ergebnisId, `<div class="warnung rot">✕ ${esc(e.message)}</div>`);
  } finally {
    knopf.disabled = false;
    knopf.textContent = alt;
  }
}

document.getElementById("sys-graph-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await sysSpeichern(sysGraphDaten(), "graph"); toast("Microsoft-Graph-Zugang gespeichert.", false); sysErgebnis("sys-graph-ergebnis", ""); }
  catch (err) { toast(err.message); }
});
document.getElementById("sys-ki-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await sysSpeichern(sysKiDaten(), "ki"); toast("KI-Zugang gespeichert.", false); sysErgebnis("sys-ki-ergebnis", ""); }
  catch (err) { toast(err.message); }
});
for (const art of ["graph", "ki"]) {
  const f = document.getElementById(`sys-${art}-form`);
  f.addEventListener("input", () => { sysGeaendert[art] = true; });
  f.addEventListener("change", () => { sysGeaendert[art] = true; });
  document.getElementById(`sys-${art}-test`).addEventListener("click", () => sysTesten(art));
}
document.getElementById("sys-graph-secret-entfernen").addEventListener("click", async () => {
  if (!confirm("Gespeichertes Client-Secret entfernen? Der SharePoint-Zugang funktioniert danach nicht mehr.")) return;
  try { await sysSpeichern({ graph_client_secret: "" }); toast("Client-Secret entfernt.", false); }
  catch (e) { toast(e.message); }
});
document.getElementById("sys-ki-key-entfernen").addEventListener("click", async () => {
  if (!confirm("Gespeicherten API-Key entfernen?")) return;
  try { await sysSpeichern({ llm_api_key: "" }); toast("API-Key entfernt.", false); }
  catch (e) { toast(e.message); }
});

sysLaden();
