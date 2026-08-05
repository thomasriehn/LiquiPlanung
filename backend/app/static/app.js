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
