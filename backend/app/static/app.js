"use strict";

async function api(methode, url, daten) {
  const optionen = { method: methode, headers: {} };
  if (daten !== undefined) {
    optionen.headers["Content-Type"] = "application/json";
    optionen.body = JSON.stringify(daten);
  }
  const antwort = await fetch(url, optionen);
  if (antwort.status === 401) { location.href = "/login"; throw new Error("Nicht angemeldet"); }
  let json = null;
  try { json = await antwort.json(); } catch (e) { /* leere Antwort */ }
  if (!antwort.ok) {
    const detail = json && json.detail ? (typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail)) : antwort.statusText;
    throw new Error(detail);
  }
  return json;
}

async function apiDatei(url, formData) {
  const antwort = await fetch(url, { method: "POST", body: formData });
  if (antwort.status === 401) { location.href = "/login"; throw new Error("Nicht angemeldet"); }
  const json = await antwort.json().catch(() => null);
  if (!antwort.ok) {
    throw new Error(json && json.detail ? JSON.stringify(json.detail) : antwort.statusText);
  }
  return json;
}

// HTML-Escaping für alle nutzerkontrollierten Werte in Template-Literalen (XSS-Schutz)
function esc(text) {
  if (text === null || text === undefined) return "";
  return String(text).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const eurFormat = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function eur(wert) {
  if (wert === null || wert === undefined) return "";
  return eurFormat.format(wert);
}
function eur0(wert) {
  if (wert === null || wert === undefined || wert === 0) return "";
  return eurFormat.format(wert);
}

function toast(text, fehler = true) {
  const container = document.getElementById("toast");
  const div = document.createElement("div");
  div.className = "meldung" + (fehler ? " fehler" : "");
  div.textContent = text;
  container.appendChild(div);
  setTimeout(() => div.remove(), 6000);
}

function datumDe(iso) {
  const [j, m, t] = iso.split("-");
  return `${t}.${m}.`;
}

function wochentagKurz(iso) {
  return ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"][new Date(iso + "T12:00:00").getDay()];
}

// Gewählter Planungsstart (Stichtag) je Mandant: bleibt beim Seitenwechsel
// innerhalb des Mandanten erhalten (Plan, Szenarienvergleich)
function startMerken(mandantId, wert) {
  try {
    if (wert) localStorage.setItem(`liqui.start.${mandantId}`, wert);
    else localStorage.removeItem(`liqui.start.${mandantId}`);
  } catch (e) { /* z. B. privater Modus – dann eben ohne Persistenz */ }
}

function startGemerkt(mandantId) {
  try { return localStorage.getItem(`liqui.start.${mandantId}`) || ""; }
  catch (e) { return ""; }
}

// Soll-Referenz (eingefrorener Plan): serverseitig je Mandant gespeichert und
// damit für Plan-Seite, Soll/Ist und Excel-Export identisch
function datumVollDe(iso) {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;
}

async function referenzAuswahlFuellen(mandantId, select) {
  const snaps = await api("GET", `/api/mandanten/${mandantId}/plan/snapshots`);
  select.innerHTML = `<option value="">automatisch (jüngster eingefrorener Plan)</option>` +
    snaps.map(s => `<option value="${s.id}"${s.referenz ? " selected" : ""}>` +
      `Stichtag ${datumVollDe(s.stichtag)} · ${s.wochen} Wo. · erstellt ` +
      `${new Date(s.erstellt_am).toLocaleDateString("de-DE")}` +
      `${s.kommentar ? " · " + esc(s.kommentar) : ""}</option>`).join("");
  return snaps;
}

async function referenzSetzen(mandantId, snapshotId) {
  await api("PUT", `/api/mandanten/${mandantId}/plan/referenz`,
    { snapshot_id: snapshotId ? parseInt(snapshotId) : null });
}

// Spaltenansicht (Tage/Wochen/Monate) je Benutzer serverseitig speichern –
// gemeinsam für 13-Wochen-Plan und Budget, geräteübergreifend
async function ansichtMerken(wert) {
  try { await api("PUT", "/api/profil/einstellungen", { ansicht: wert }); }
  catch (e) { /* Anzeige funktioniert auch ohne gespeicherte Einstellung */ }
}
