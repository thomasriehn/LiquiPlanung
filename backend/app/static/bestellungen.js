"use strict";
// Bestellungen & Bestellfreigabe: Liste, Erfassung (Upload → KI-Auslesen →
// Prüfen und Speichern), Freigabe je KW mit offen gelegtem Rechenweg,
// Zahlungsabgleich (Ablösung durch Zahlung/Rechnung/manuell) und Journal.
// Erwartet MANDANT_ID, LESER, DARF_FREIGEBEN, KI_AKTIV, IST_ADMIN aus der Vorlage.

const BS_STATUS_TEXT = {
  OFFEN: "zur Freigabe", FREIGEGEBEN: "freigegeben", ABGELEHNT: "abgelehnt",
  ABGELOEST: "abgelöst", STORNIERT: "storniert",
};
const BS_STATUS_KLASSE = {
  OFFEN: "offen", FREIGEGEBEN: "freigegeben", ABGELEHNT: "abgelehnt",
  ABGELOEST: "abgeloest", STORNIERT: "storniert",
};
const BS_AKTION_TEXT = {
  ERFASST: "erfasst", GEAENDERT: "geändert", FREIGEGEBEN: "freigegeben", ABGELEHNT: "abgelehnt",
  ZURUECKGESETZT: "Entscheidung zurückgenommen", ABGELOEST: "abgelöst", TEILZAHLUNG: "Teilzahlung zugeordnet",
  ABLOESUNG_AUFGEHOBEN: "Ablösung aufgehoben", STORNIERT: "storniert", GELOESCHT: "gelöscht", DATEI: "Datei geändert",
};
const BS_AKTION_KLASSE = {
  ERFASST: "neutral", GEAENDERT: "neutral", DATEI: "neutral", FREIGEGEBEN: "freigegeben",
  ABGELEHNT: "abgelehnt", ZURUECKGESETZT: "offen", ABGELOEST: "abgeloest", TEILZAHLUNG: "abgeloest",
  ABLOESUNG_AUFGEHOBEN: "offen", STORNIERT: "storniert", GELOESCHT: "storniert",
};
const BS_EBENE_TEXT = { konto: "Konto", gruppe: "BWA-Gruppe", gesamt: "Gesamt" };
const BS_BELEG_TEXT = { BUCHUNG: "Ist-Buchung", TRANSAKTION: "Bankumsatz", POSTEN: "Eingangsrechnung" };
const BS_INLINE_MIME = ["application/pdf", "image/png", "image/jpeg", "image/webp", "image/tiff"];
const BS_FILTER_SCHLUESSEL = `liqui.bestellungen.status.${MANDANT_ID}`;

// Seitenzustand (ein Objekt statt vieler globaler Namen)
const bs = {
  rechte: { freigeben: DARF_FREIGEBEN && !LESER, ki: KI_AKTIV },
  heute: isoTag(new Date()),
  liste: [], byId: {}, kreditoren: [], ablage: null,
  konten: [], kontenById: {}, kontoFeld: null,
  snapshots: null,
  ef: null,            // Erfassungsdialog {modus, bestellung, auslesen, token, ablageElement, geaendert}
  fd: null,            // Freigabedialog {id, b, kz, jahr, kw}
  ad: null,            // Ablösedialog {b, kandidaten}
  journal: [],
  tab: "liste",
};

// ------------------------------------------------------------ Hilfen
function bsDialogAuf(dlg) {
  if (dlg.open) return;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");               // Browser ohne <dialog>-Unterstützung
}
function bsDialogZu(dlg) {
  if (typeof dlg.close === "function") { if (dlg.open) dlg.close(); }
  else { dlg.removeAttribute("open"); dlg.dispatchEvent(new Event("close")); }
}
function bsStatusBadge(status, text) {
  return `<span class="status-badge ${BS_STATUS_KLASSE[status] || "neutral"}">${esc(text || BS_STATUS_TEXT[status] || status || "–")}</span>`;
}
function bsAktionBadge(aktion, text) {
  return `<span class="status-badge ${BS_AKTION_KLASSE[aktion] || "neutral"}">${esc(text || BS_AKTION_TEXT[aktion] || aktion)}</span>`;
}
function bsDatum(iso) { return iso ? datumVollDe(String(iso).slice(0, 10)) : "–"; }
function bsZeit(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}
function bsEur(v) { return v === null || v === undefined ? "–" : `${eur(v)} €`; }
function bsSatzText(s) { return s === null || s === undefined ? "ohne USt" : `${String(Number(s)).replace(".", ",")} %`; }
function bsKontoText(k) { return k ? `${k.nummer} ${k.bezeichnung}` : "–"; }
function bsKwLabel(iso) { const w = isoWoche(iso); return `KW ${w.kw}/${w.jahr}`; }
function bsNegKlasse(v) { return v !== null && v !== undefined && v < -0.005 ? "negativ" : ""; }
function bsOffen(b) { return b.status === "OFFEN" || b.status === "FREIGEGEBEN"; }
function bsAenderbar(b) { return ["OFFEN", "FREIGEGEBEN", "ABGELEHNT"].includes(b.status); }

async function bsMitBusy(knopf, text, fn) {
  if (!knopf) return fn();
  const alt = knopf.textContent;
  knopf.disabled = true;
  knopf.textContent = text;
  try { return await fn(); }
  finally { knopf.disabled = false; knopf.textContent = alt; }
}

function bsGroesse(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1).replace(".", ",") + " MB";
  return Math.max(1, Math.round(bytes / 1024)) + " kB";
}

// ------------------------------------------------------------ Konten
async function bsKontenLaden() {
  const d = await api("GET", `/api/mandanten/${MANDANT_ID}/konten`);
  bs.konten = d.konten;
  bs.kontenById = Object.fromEntries(d.konten.map(k => [k.id, k]));
  // EINE Kontoauswahl für das Erfassungsformular (wird per setzeWert wiederverwendet)
  bs.kontoFeld = kontoAuswahl({
    gruppen: kontoGruppenAus(d), name: "konto_id", konten: d.konten,
    platzhalter: "Konto suchen (Nummer oder Bezeichnung) …",
    beiAuswahl: (k) => efKontoGewaehlt(k),
  });
  document.getElementById("ef-konto").replaceChildren(bs.kontoFeld);
  const eingabe = bs.kontoFeld.querySelector("input");
  eingabe.required = false;
  eingabe.addEventListener("input", () => eingabe.classList.remove("ki-vorschlag"));
}

// ------------------------------------------------------------ Tabs
function tabWechsel(tab) {
  bs.tab = tab;
  for (const name of ["liste", "freigabe", "abgleich", "journal"]) {
    document.getElementById("bereich-" + name).style.display = tab === name ? "" : "none";
    document.getElementById("tab-" + name).classList.toggle("aktiv", tab === name);
  }
  if (tab === "freigabe") fgLaden();
  if (tab === "abgleich") abLaden();
  if (tab === "journal") jnLaden();
}

// ------------------------------------------------------------ Liste
function bsFilterUrl() {
  const status = document.getElementById("filter-status").value;
  const suche = document.getElementById("filter-suche").value.trim();
  const q = [];
  if (status) q.push(`status=${encodeURIComponent(status)}`);
  if (suche) q.push(`suche=${encodeURIComponent(suche)}`);
  return `/api/mandanten/${MANDANT_ID}/bestellungen${q.length ? "?" + q.join("&") : ""}`;
}

async function bsListeLaden() {
  let d;
  try { d = await api("GET", bsFilterUrl()); }
  catch (e) {
    document.getElementById("bestell-tabelle").innerHTML =
      `<tr><td colspan="9" class="fehler">${esc(e.message)}</td></tr>`;
    return;
  }
  bs.liste = d.bestellungen || [];
  bs.byId = Object.fromEntries(bs.liste.map(b => [b.id, b]));
  bs.kreditoren = d.kreditoren || [];
  bs.ablage = d.ablage || null;
  if (typeof d.darf_freigeben === "boolean") bs.rechte.freigeben = d.darf_freigeben && !LESER;
  if (typeof d.ki_aktiv === "boolean") bs.rechte.ki = d.ki_aktiv;
  document.getElementById("ef-kreditoren").innerHTML =
    bs.kreditoren.map(k => `<option value="${esc(k)}"></option>`).join("");
  const ab = document.getElementById("liste-ablage");
  if (bs.ablage) {
    ab.innerHTML = `Belegablage: ${esc(bs.ablage.beschreibung || "–")}` +
      (bs.ablage.konfiguriert ? "" : ` (Standard${IST_ADMIN ? ` – festlegen unter <a href="/mandanten/${MANDANT_ID}/einstellungen">Einstellungen</a>` : ""})`) +
      (bs.rechte.ki ? "" : ` · KI-Auslesen nicht eingerichtet${IST_ADMIN ? ` (<a href="/system">System</a>)` : ""} – Angaben bitte manuell erfassen`);
  }
  bsListeZeichnen();
}

function bsListeZeichnen() {
  const tb = document.getElementById("bestell-tabelle");
  tb.innerHTML = "";
  const alleStatus = !document.getElementById("filter-status").value;
  let netto = 0, brutto = 0, anzahl = 0;
  for (const b of bs.liste) {
    if (!alleStatus || bsOffen(b)) {
      netto += b.betrag_netto || 0;
      brutto += b.betrag_brutto || 0;
      anzahl++;
    }
    const tr = document.createElement("tr");
    tr.dataset.id = b.id;
    if (["ABGELOEST", "STORNIERT", "ABGELEHNT"].includes(b.status)) tr.classList.add("bs-zu");
    const ueberfaellig = bsOffen(b) && b.zahldatum && b.zahldatum < bs.heute;
    let zusatz = "";
    if (b.status === "ABGELOEST" && b.abgeloest_betrag !== null && b.abgeloest_betrag !== undefined) {
      zusatz = `<br><span class="klein">gezahlt ${eur(b.abgeloest_betrag)} €` +
        (b.abweichung !== null && b.abweichung !== undefined && Math.abs(b.abweichung) > 0.004
          ? `, Δ ${b.abweichung > 0 ? "+" : ""}${eur(b.abweichung)} €` : "") + `</span>`;
    } else if (bsOffen(b) && b.abgeloest_betrag > 0) {
      // rest_offen statt rest_geplant: der ist 0, wenn die Bestellung nicht im Plan steht
      zusatz = `<br><span class="klein">teilbezahlt ${eur(b.abgeloest_betrag)} €, Rest ${eur(b.rest_offen)} €</span>`;
    }
    const aktionen = [];
    if (b.status === "OFFEN" && bs.rechte.freigeben) {
      aktionen.push(`<button class="klein-knopf" data-aktion="pruefen" title="Rechenweg ansehen und freigeben bzw. ablehnen">prüfen …</button>`);
    }
    if (!LESER && bsAenderbar(b)) {
      aktionen.push(`<button class="sekundaer klein-knopf" data-aktion="bearbeiten" title="Bestellung bearbeiten">✎</button>`);
    }
    if (!LESER && bsOffen(b)) {
      aktionen.push(`<button class="sekundaer klein-knopf" data-aktion="abloesen" title="Durch Zahlung, Eingangsrechnung oder manuell ablösen">Ablösen</button>`);
    }
    aktionen.push(`<button class="sekundaer klein-knopf" data-aktion="details" title="Details, Rechenweg und Journal">⋯</button>`);
    tr.innerHTML = `
      <td>${bsStatusBadge(b.status, b.status_text)}${b.ki ? ` <span class="klein" title="Angaben von der KI ausgelesen">KI</span>` : ""}</td>
      <td>${bsDatum(b.bestelldatum)}</td>
      <td><strong>${esc(b.kreditor)}</strong>${b.beschreibung ? ` <span title="${esc(b.beschreibung)}">📝</span>` : ""}
        ${b.bestellnummer ? `<br><span class="klein">Nr. ${esc(b.bestellnummer)}</span>` : ""}</td>
      <td class="klein">${esc(bsKontoText(b.konto))}</td>
      <td class="zahl">${eur(b.betrag_netto)}</td>
      <td class="zahl">${eur(b.betrag_brutto)}<br><span class="klein">${esc(bsSatzText(b.ust_satz))}</span>${zusatz}</td>
      <td>${bsDatum(b.zahldatum)} <span class="klein">${esc(b.kw ? b.kw.label : bsKwLabel(b.zahldatum))}</span>
        ${ueberfaellig ? `<br><span class="badge-gesperrt" title="Zahldatum überschritten – im Plan auf den nächsten Bankarbeitstag vorgetragen">überfällig</span>` : ""}</td>
      <td>${b.datei ? `<a href="${esc(b.datei.url)}" target="_blank" rel="noopener" title="${esc(b.datei.name)}">📄</a>` : ""}</td>
      <td class="bs-aktionen">${aktionen.join(" ")}</td>`;
    tb.appendChild(tr);
  }
  if (!bs.liste.length) {
    const status = document.getElementById("filter-status").value;
    tb.innerHTML = `<tr><td colspan="9" class="hinweis">Keine Bestellungen${status ? ` mit Status „${esc(BS_STATUS_TEXT[status])}“` : ""}` +
      `${document.getElementById("filter-suche").value.trim() ? " zur Suche" : ""}.</td></tr>`;
  }
  document.getElementById("bestell-summe").innerHTML = anzahl > 1
    ? `<tr><td colspan="4"><strong>Summe</strong> <span class="klein">(${anzahl} Bestellungen${alleStatus ? " zur Freigabe bzw. freigegeben" : ""})</span></td>
        <td class="zahl"><strong>${eur(netto)}</strong></td><td class="zahl"><strong>${eur(brutto)}</strong></td>
        <td colspan="3"></td></tr>` : "";
  document.getElementById("liste-info").textContent = `${bs.liste.length} Bestellung${bs.liste.length === 1 ? "" : "en"}`;
}

document.getElementById("bestell-tabelle").addEventListener("click", (e) => {
  const knopf = e.target.closest("button[data-aktion]");
  if (!knopf) return;
  const id = parseInt(knopf.closest("tr").dataset.id);
  const b = bs.byId[id];
  const aktion = knopf.dataset.aktion;
  if (aktion === "pruefen" || aktion === "details") fdOeffnen(id);
  else if (aktion === "bearbeiten" && b) efOeffnen({ modus: "bearbeiten", bestellung: b });
  else if (aktion === "abloesen") adOeffnen(id);
});

// ------------------------------------------------------------ Neu: Upload, Ablage, manuell
async function bsUploadStarten(datei) {
  if (!datei) return;
  const knopf = document.getElementById("neu-upload");
  const info = document.getElementById("liste-info");
  info.textContent = `„${datei.name}“ wird ${bs.rechte.ki ? "hochgeladen und ausgelesen" : "hochgeladen"} …`;
  try {
    await bsMitBusy(knopf, bs.rechte.ki ? "Wird ausgelesen …" : "Wird hochgeladen …", async () => {
      const fd = new FormData();
      fd.append("datei", datei);
      const erg = await apiDatei(`/api/mandanten/${MANDANT_ID}/bestellungen/auslesen`, fd);
      efOeffnen({ modus: "neu", auslesen: erg });
    });
  } catch (e) { toast(e.message); }
  finally { info.textContent = `${bs.liste.length} Bestellung${bs.liste.length === 1 ? "" : "en"}`; }
}

async function bsAusAblage() {
  const wahl = await ablageDateiWaehlen({ mandantId: MANDANT_ID });
  if (!wahl) return;
  const knopf = document.getElementById("neu-ablage");
  try {
    await bsMitBusy(knopf, "Wird ausgelesen …", async () => {
      const erg = await api("POST", `/api/mandanten/${MANDANT_ID}/bestellungen/auslesen-ablage`, { element: wahl.element });
      if (!erg.ablage_element) erg.ablage_element = wahl.element;
      if (!erg.datei) erg.datei = { name: wahl.name };
      efOeffnen({ modus: "neu", auslesen: erg });
    });
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------ Erfassungsdialog
const EF_KI_FELD = {          // KI-Feldname → Formularfeld
  kreditor: "kreditor", bestellnummer: "bestellnummer", beschreibung: "beschreibung",
  bestelldatum: "bestelldatum", lieferdatum: "lieferdatum", zahldatum: "zahldatum",
  zahlungsziel_tage: "zahldatum", betrag_netto: "betrag_netto", betrag_brutto: "betrag_netto",
  ust_satz: "ust_satz", konto_id: "konto", konto: "konto", kontonummer: "konto",
};

function efForm() { return document.getElementById("ef-form"); }

function efUstSetzen(satz) {
  const sel = efForm().ust_satz;
  if (satz === null || satz === undefined || Number(satz) === 0) { sel.value = "0"; return; }
  const wert = String(Number(satz));
  if (![...sel.options].some(o => o.value === wert)) {
    const o = document.createElement("option");
    o.value = wert;
    o.textContent = `${wert.replace(".", ",")} %`;
    sel.appendChild(o);
  }
  sel.value = wert;
}

function efGewaehltesKonto() { return bs.kontoFeld && bs.kontoFeld.wert !== null ? bs.kontenById[bs.kontoFeld.wert] : null; }

// wirksamer USt-Satz (Zahl) oder null = ohne USt
function efSatz() {
  const v = efForm().ust_satz.value;
  if (v === "") {
    const k = efGewaehltesKonto();
    return k && k.ust_satz !== null && k.ust_satz !== undefined ? Number(k.ust_satz) : null;
  }
  return Number(v) === 0 ? null : Number(v);
}

function efBerechnen() {
  const f = efForm();
  const k = efGewaehltesKonto();
  f.ust_satz.querySelector('option[value=""]').textContent = k
    ? `laut Konto (${bsSatzText(k.ust_satz)})` : "laut Konto";
  const netto = parseFloat(f.betrag_netto.value);
  const satz = efSatz();
  const el = document.getElementById("ef-brutto");
  el.textContent = isNaN(netto) ? "–" : `${eur(Math.round(netto * (1 + (satz || 0) / 100) * 100) / 100)} €`;
  el.title = satz ? `inkl. ${bsSatzText(satz)} USt – so fließt der Betrag in den Plan` : "ohne USt";
  // Zahldatum: KW und Warnung bei Vergangenheit
  const z = f.zahldatum.value;
  document.getElementById("ef-kw").textContent = z ? `${wochentagKurz(z)}, ${bsKwLabel(z)}` : "";
  const warn = document.getElementById("ef-zahldatum-warnung");
  if (z && z < bs.heute) {
    warn.style.display = "";
    warn.textContent = "Das Zahldatum liegt in der Vergangenheit – im Plan wird die Zahlung als überfällig auf den nächsten Bankarbeitstag gelegt.";
  } else if (z && f.bestelldatum.value && z < f.bestelldatum.value) {
    warn.style.display = "";
    warn.textContent = "Das Zahldatum liegt vor dem Bestelldatum – bitte prüfen.";
  } else {
    warn.style.display = "none";
  }
}

function efKontoGewaehlt(k) {
  const eingabe = bs.kontoFeld.querySelector("input");
  eingabe.classList.remove("ki-vorschlag");
  if (bs.ef) bs.ef.geaendert.add("konto_id");
  document.getElementById("ef-konto-hinweis").textContent = k && k.ust_satz !== null && k.ust_satz !== undefined
    ? `Standard-USt des Kontos: ${bsSatzText(k.ust_satz)}` : "";
  efBerechnen();
}

function efFeldElement(name) {
  if (name === "konto") return bs.kontoFeld ? bs.kontoFeld.querySelector("input") : null;
  return efForm().elements[name] || null;
}

function efKiMarkieren(feld, titel) {
  const el = efFeldElement(feld);
  if (!el) return;
  el.classList.add("ki-vorschlag");
  el.title = titel || "von der KI ausgelesen – bitte prüfen";
}

function efVorschau({ url, name, mime, groesse, hinweis, textAuszug }) {
  const box = document.getElementById("ef-vorschau");
  if (!url && !name) {
    box.innerHTML = `<div class="bs-vorschau-leer">Ohne Datei erfasst.<br>Die Bestelldatei lässt sich nach dem
      Speichern über ✎ anhängen.</div>`;
    return;
  }
  const m = (mime || "").toLowerCase();
  let koerper;
  if (url && m.startsWith("image/")) {
    koerper = `<div style="overflow:auto;max-height:clamp(320px, calc(100vh - 330px), 760px);background:#fff">
      <img src="${esc(url)}" alt="${esc(name || "Vorschau")}" style="max-width:100%;display:block;margin:0 auto"></div>`;
  } else if (url && BS_INLINE_MIME.includes(m)) {
    koerper = `<iframe src="${esc(url)}" title="Vorschau ${esc(name || "")}"></iframe>`;
  } else {
    koerper = `<div class="bs-vorschau-leer">${esc(hinweis || (url
      ? "Für diesen Dateityp gibt es keine Vorschau – Datei über den Link öffnen."
      : "Vorschau nach dem Speichern über 📄 in der Liste."))}</div>`;
  }
  box.innerHTML = `<div class="bs-vorschau-kopf"><span>📄</span>
      <span class="sp-name"><strong>${esc(name || "Datei")}</strong>${groesse ? ` <span class="klein">${bsGroesse(groesse)}</span>` : ""}</span>
      ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">in neuem Tab öffnen ↗</a>` : ""}</div>
    ${koerper}
    ${textAuszug ? `<details><summary class="klein" style="padding:6px 10px;cursor:pointer">Ausgelesener Text</summary>
      <pre class="bs-textauszug">${esc(textAuszug)}</pre></details>` : ""}`;
}

function efKiStatus(erg) {
  const box = document.getElementById("ef-ki-status");
  const meld = document.getElementById("ef-meldungen");
  box.innerHTML = "";
  meld.innerHTML = "";
  if (!erg) return;
  if (erg.duplikat) {
    const d = erg.duplikat;
    meld.innerHTML = `<div class="warnung">Mögliches Duplikat: Eine Datei mit gleichem Namen und gleicher Größe
      gehört bereits zur Bestellung <strong>${esc(d.kreditor || "")}</strong> (#${esc(d.id)}${d.erstellt_am ? `, erfasst ${bsZeit(d.erstellt_am)}` : ""}).
      <button type="button" class="sekundaer klein-knopf" id="ef-duplikat-oeffnen">ansehen</button></div>`;
    document.getElementById("ef-duplikat-oeffnen").addEventListener("click", () => fdOeffnen(d.id));
  }
  const ki = erg.ki || {};
  const hinweise = (ki.hinweise || []).map(h => `<li>${esc(h)}</li>`).join("");
  let html;
  if (ki.fehler) {
    html = `<div class="warnung rot bs-ki-box"><strong>KI-Auslesen fehlgeschlagen:</strong> ${esc(ki.fehler)}
      <br>Bitte die Angaben manuell erfassen – die Datei bleibt angehängt.${hinweise ? `<ul>${hinweise}</ul>` : ""}</div>`;
  } else if (ki.aktiv) {
    const quelle = { TEXT: "Text der Datei", OCR: "Texterkennung (OCR)", VISION: "Seitenbilder" }[ki.quelle] || ki.quelle || "";
    html = `<div class="warnung bs-ki-box"><span class="ki-legende"></span><strong>Von der KI ausgelesen</strong>
      ${ki.modell ? `(${esc(ki.modell)}${quelle ? ", " + esc(quelle) : ""})` : ""} – markierte Felder bitte prüfen;
      die Markierung verschwindet beim Bearbeiten.${hinweise ? `<ul>${hinweise}</ul>` : ""}</div>`;
  } else {
    html = `<div class="warnung bs-ki-box">KI-Auslesen nicht eingerichtet${IST_ADMIN ? ` (<a href="/system">System</a>)` : ""}
      – bitte die Angaben manuell erfassen.${hinweise ? `<ul>${hinweise}</ul>` : ""}</div>`;
  }
  if (erg.text_quelle === "LEER") {
    html += `<div class="warnung bs-ki-box">Aus der Datei konnte kein Text gelesen werden (Scan ohne Texterkennung?).</div>`;
  }
  box.innerHTML = html;
}

function efZuruecksetzen() {
  const f = efForm();
  f.reset();
  f.querySelectorAll(".ki-vorschlag").forEach(el => { el.classList.remove("ki-vorschlag"); el.title = ""; });
  [...f.ust_satz.options].forEach(o => { if (!["", "19", "7", "0"].includes(o.value)) o.remove(); });
  if (bs.kontoFeld) {
    bs.kontoFeld.setzeWert(null);
    const e = bs.kontoFeld.querySelector("input");
    e.classList.remove("ki-vorschlag");
    e.title = "";
  }
  document.getElementById("ef-konto-hinweis").textContent = "";
  document.getElementById("ef-ki-status").innerHTML = "";
  document.getElementById("ef-meldungen").innerHTML = "";
  document.getElementById("ef-datei-bereich").innerHTML = "";
  document.getElementById("ef-ablage-ziel").innerHTML = "";
  document.getElementById("ef-fuss-hinweis").textContent = "";
}

function efOeffnen({ modus, auslesen = null, bestellung = null }) {
  if (!bs.kontoFeld) { toast("Konten werden noch geladen – bitte erneut versuchen."); return; }
  efZuruecksetzen();
  const f = efForm();
  bs.ef = { modus, auslesen, bestellung, token: null, ablageElement: null, geaendert: new Set() };
  document.getElementById("ef-speichern-pruefen").style.display =
    bs.rechte.freigeben && (modus === "neu" || (bestellung && bestellung.status === "OFFEN")) ? "" : "none";
  if (modus === "bearbeiten") {
    const b = bestellung;
    document.getElementById("ef-titel").textContent = `Bestellung bearbeiten – ${b.kreditor}`;
    document.getElementById("ef-untertitel").innerHTML =
      `${bsStatusBadge(b.status, b.status_text)} · erfasst ${bsZeit(b.erstellt_am)}${b.erstellt_von ? " von " + esc(b.erstellt_von) : ""}`;
    f.kreditor.value = b.kreditor || "";
    f.bestellnummer.value = b.bestellnummer || "";
    f.bestelldatum.value = b.bestelldatum || "";
    f.lieferdatum.value = b.lieferdatum || "";
    f.zahldatum.value = b.zahldatum || "";
    f.betrag_netto.value = b.betrag_netto !== null && b.betrag_netto !== undefined ? Number(b.betrag_netto).toFixed(2) : "";
    efUstSetzen(b.ust_satz);
    f.beschreibung.value = b.beschreibung || "";
    bs.kontoFeld.setzeWert(b.konto ? b.konto.id : null);
    if (b.status === "FREIGEGEBEN") {
      document.getElementById("ef-meldungen").innerHTML = `<div class="warnung">Die Bestellung ist freigegeben. Änderungen an
        Kreditor, Betrag, USt-Satz oder Konto, ein Zahldatum in einer anderen KW oder eine ersetzte Datei setzen sie
        wieder auf „zur Freigabe“ (erneute Freigabe erforderlich).</div>`;
    } else if (b.status === "ABGELEHNT") {
      document.getElementById("ef-meldungen").innerHTML = `<div class="warnung">Die Bestellung wurde abgelehnt. Jede Änderung
        legt sie erneut zur Freigabe vor.</div>`;
    }
    const d = b.datei;
    efVorschau(d ? { url: d.url, name: d.name, mime: d.mime, groesse: d.groesse } : {});
    document.getElementById("ef-ablage-ziel").innerHTML = d
      ? `Abgelegt (${d.ablage === "SHAREPOINT" ? "SharePoint" : "lokal"}): ${esc(d.pfad || d.name)}` +
        (d.web_url ? ` · <a href="${esc(d.web_url)}" target="_blank" rel="noopener">in SharePoint öffnen ↗</a>` : "")
      : "";
    if (!LESER) efDateiBereich(b);
  } else {
    document.getElementById("ef-titel").textContent = auslesen ? "Bestellung prüfen und erfassen" : "Bestellung manuell erfassen";
    document.getElementById("ef-untertitel").textContent = auslesen
      ? "Angaben mit der Datei abgleichen, ergänzen und speichern – danach steht die Bestellung zur Freigabe."
      : "Nach dem Speichern steht die Bestellung zur Freigabe.";
    f.bestelldatum.value = bs.heute;
    if (auslesen) efVorbelegen(auslesen);
    else efVorschau({});
  }
  efBerechnen();
  bsDialogAuf(document.getElementById("erfassung-dialog"));
  f.kreditor.focus();
}

function efVorbelegen(erg) {
  const f = efForm();
  const v = erg.vorschlag || {};
  const ki = erg.ki || {};
  bs.ef.token = erg.token || null;
  bs.ef.ablageElement = erg.token ? null : (erg.ablage_element || null);
  const setze = (name, wert) => { if (wert !== null && wert !== undefined && wert !== "") f.elements[name].value = wert; };
  setze("kreditor", v.kreditor);
  setze("bestellnummer", v.bestellnummer);
  setze("beschreibung", v.beschreibung);
  setze("bestelldatum", v.bestelldatum);
  setze("lieferdatum", v.lieferdatum);
  setze("zahldatum", v.zahldatum);
  if (v.betrag_netto !== null && v.betrag_netto !== undefined) f.betrag_netto.value = Number(v.betrag_netto).toFixed(2);
  if (v.ust_satz !== null && v.ust_satz !== undefined) efUstSetzen(v.ust_satz);
  if (v.konto_id) bs.kontoFeld.setzeWert(v.konto_id);
  // KI-Felder markieren (nur wenn ein Wert gesetzt wurde)
  for (const feld of ki.felder || []) {
    const ziel = EF_KI_FELD[feld] || feld;
    if (ziel === "konto") { if (v.konto_id && v.konto_quelle !== "HISTORIE") efKiMarkieren("konto"); continue; }
    const el = efFeldElement(ziel);
    if (el && el.value !== "") efKiMarkieren(ziel);
  }
  if (v.konto_id && v.konto_quelle === "HISTORIE") {
    document.getElementById("ef-konto-hinweis").textContent = "Konto aus der letzten Bestellung dieses Kreditors übernommen.";
  } else if (v.konto_id && v.konto_quelle === "KI") {
    efKiMarkieren("konto");
  }
  const zusatz = [];
  if (v.zahlungsziel_tage) zusatz.push(`Zahlungsziel ${v.zahlungsziel_tage} Tage`);
  if (v.iban) zusatz.push(`IBAN ${v.iban}`);
  if (v.betrag_brutto !== null && v.betrag_brutto !== undefined) zusatz.push(`Brutto laut Beleg ${eur(v.betrag_brutto)} €`);
  if (zusatz.length) document.getElementById("ef-fuss-hinweis").textContent = zusatz.join(" · ");
  efKiStatus(erg);
  const d = erg.datei || {};
  if (bs.ef.token) {
    efVorschau({ url: `/api/mandanten/${MANDANT_ID}/bestellungen/zwischenablage/${encodeURIComponent(bs.ef.token)}`,
      name: d.name, mime: d.mime, groesse: d.groesse, textAuszug: erg.text_auszug });
    document.getElementById("ef-ablage-ziel").innerHTML = bs.ablage
      ? `Wird gespeichert in: <strong>${esc(bs.ablage.beschreibung || "–")}</strong>`
      : "Wird in der Belegablage des Mandanten gespeichert.";
  } else {
    efVorschau({ url: null, name: d.name, mime: d.mime, groesse: d.groesse, textAuszug: erg.text_auszug,
      hinweis: "Die Datei bleibt in der Ablage; Vorschau nach dem Speichern über 📄 in der Liste." });
    document.getElementById("ef-ablage-ziel").innerHTML =
      `Datei bleibt in der Ablage: <strong>${esc(d.name || bs.ef.ablageElement || "")}</strong>`;
  }
}

// Bearbeiten: Datei anhängen bzw. ersetzen
function efDateiBereich(b) {
  const bereich = document.getElementById("ef-datei-bereich");
  bereich.innerHTML = `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px">
    <button type="button" class="sekundaer klein-knopf" id="ef-datei-knopf">${b.datei ? "Datei ersetzen …" : "Datei anhängen …"}</button>
    <input type="file" id="ef-datei-eingabe" class="bs-datei-eingabe" tabindex="-1" aria-hidden="true" accept="${esc(document.getElementById("upload-datei")
      ? document.getElementById("upload-datei").accept : ".pdf,image/*")}">
    <span class="hinweis">wird in der Belegablage gespeichert${bs.ablage ? ": " + esc(bs.ablage.beschreibung || "") : ""}</span></div>`;
  const eingabe = document.getElementById("ef-datei-eingabe");
  const knopf = document.getElementById("ef-datei-knopf");
  knopf.addEventListener("click", () => eingabe.click());
  eingabe.addEventListener("change", async () => {
    const datei = eingabe.files[0];
    if (!datei) return;
    // Ersetzen der Datei einer freigegebenen Bestellung erfordert eine erneute Freigabe
    const aktuell = (bs.ef && bs.ef.bestellung) || b;
    if (aktuell.status === "FREIGEGEBEN" && aktuell.datei
        && !confirm("Die Bestellung ist freigegeben. Wird die Datei ersetzt, steht sie wieder zur Freigabe (erneute Freigabe erforderlich). Fortfahren?")) {
      eingabe.value = "";
      return;
    }
    try {
      await bsMitBusy(knopf, "Wird hochgeladen …", async () => {
        const fd = new FormData();
        fd.append("datei", datei);
        const neu = await apiDatei(`/api/bestellungen/${b.id}/datei`, fd);
        bs.ef.bestellung = neu;
        bs.byId[neu.id] = neu;
        const d = neu.datei;
        if (d) efVorschau({ url: d.url, name: d.name, mime: d.mime, groesse: d.groesse });
        if (neu.status !== aktuell.status) {
          document.getElementById("ef-untertitel").innerHTML =
            `${bsStatusBadge(neu.status, neu.status_text)} · erfasst ${bsZeit(neu.erstellt_am)}${neu.erstellt_von ? " von " + esc(neu.erstellt_von) : ""}`;
          document.getElementById("ef-meldungen").innerHTML = `<div class="warnung">Die Datei wurde ersetzt – die Bestellung
            steht wieder „${esc(neu.status_text || BS_STATUS_TEXT[neu.status])}“ (erneute Freigabe erforderlich).</div>`;
          document.getElementById("ef-speichern-pruefen").style.display =
            bs.rechte.freigeben && neu.status === "OFFEN" ? "" : "none";
          toast(`Datei gespeichert – Status jetzt „${neu.status_text || BS_STATUS_TEXT[neu.status]}“.`, false);
        } else {
          toast("Datei gespeichert.", false);
        }
        bsListeLaden();
      });
    } catch (e) { toast(e.message); }
    eingabe.value = "";
  });
}

function efDaten() {
  const f = efForm();
  const d = {
    kreditor: f.kreditor.value.trim(),
    bestellnummer: f.bestellnummer.value.trim() || null,
    beschreibung: f.beschreibung.value.trim() || null,
    bestelldatum: f.bestelldatum.value,
    lieferdatum: f.lieferdatum.value || null,
    zahldatum: f.zahldatum.value,
    betrag_netto: Math.round(parseFloat(f.betrag_netto.value) * 100) / 100,
    konto_id: bs.kontoFeld.wert,
  };
  const u = f.ust_satz.value;
  if (u !== "") d.ust_satz = Number(u) === 0 ? null : Number(u);
  else if (bs.ef.modus === "bearbeiten") d.ust_satz = efSatz();   // beim Ändern explizit
  // Neu + „laut Konto“: Schlüssel weglassen → Standard-USt des Kontos (serverseitig)
  return d;
}

function efPruefen() {
  const f = efForm();
  if (!f.reportValidity()) return false;
  if (bs.kontoFeld.wert === null) {
    toast("Bitte ein Konto wählen.");
    bs.kontoFeld.querySelector("input").focus();
    return false;
  }
  const netto = parseFloat(f.betrag_netto.value);
  if (!(netto > 0)) { toast("Bitte einen Nettobetrag größer 0 angeben."); f.betrag_netto.focus(); return false; }
  return true;
}

function efGleich(a, b) {
  if ((a === null || a === undefined || a === "") && (b === null || b === undefined || b === "")) return true;
  if (typeof a === "number" || typeof b === "number") return Math.abs(Number(a) - Number(b)) < 0.005;
  return String(a) === String(b);
}

// Änderungen, nach denen eine freigegebene Bestellung erneut freigegeben werden muss
// (wie im Backend: Kreditor, Betrag, USt, Konto, Zahldatum in anderer KW)
function efFreigabeRelevant(alt, aenderung) {
  if (["kreditor", "betrag_netto", "ust_satz", "konto_id"].some(k => k in aenderung)) return true;
  if (!("zahldatum" in aenderung) || !aenderung.zahldatum || !alt.zahldatum) return "zahldatum" in aenderung;
  const vorher = isoWoche(alt.zahldatum), nachher = isoWoche(aenderung.zahldatum);
  return vorher.jahr !== nachher.jahr || vorher.kw !== nachher.kw;
}

async function efSpeichern(pruefen) {
  if (!bs.ef || !efPruefen()) return;
  const daten = efDaten();
  const knopf = document.getElementById(pruefen ? "ef-speichern-pruefen" : "ef-speichern");
  try {
    await bsMitBusy(knopf, "Wird gespeichert …", async () => {
      let b;
      if (bs.ef.modus === "bearbeiten") {
        const alt = bs.ef.bestellung;
        const vorher = {
          kreditor: alt.kreditor, bestellnummer: alt.bestellnummer, beschreibung: alt.beschreibung,
          bestelldatum: alt.bestelldatum, lieferdatum: alt.lieferdatum, zahldatum: alt.zahldatum,
          betrag_netto: alt.betrag_netto, ust_satz: alt.ust_satz, konto_id: alt.konto ? alt.konto.id : null,
        };
        // nur geänderte Felder senden (sonst würde eine Freigabe unnötig zurückgesetzt)
        const aenderung = {};
        for (const [k, v] of Object.entries(daten)) if (!efGleich(v, vorher[k])) aenderung[k] = v;
        if (!Object.keys(aenderung).length) { toast("Keine Änderungen.", false); efSchliessen(true); if (pruefen) fdOeffnen(alt.id); return; }
        if (alt.status === "FREIGEGEBEN" && efFreigabeRelevant(alt, aenderung)
            && !confirm("Die Bestellung ist freigegeben. Die Änderung an Kreditor, Betrag, USt-Satz, Konto bzw. ein Zahldatum in einer anderen KW erfordert eine erneute Freigabe. Fortfahren?")) return;
        b = await api("PATCH", `/api/bestellungen/${alt.id}`, aenderung);
        toast(b.status !== alt.status ? `Bestellung gespeichert – Status jetzt „${b.status_text || BS_STATUS_TEXT[b.status]}“.` : "Bestellung gespeichert.", false);
      } else {
        if (bs.ef.token) daten.token = bs.ef.token;
        if (bs.ef.ablageElement) daten.ablage_element = bs.ef.ablageElement;
        const a = bs.ef.auslesen;
        if (a && a.ki && a.ki.aktiv && !a.ki.fehler) {
          // KI-Vorschlag zur Nachvollziehbarkeit mitspeichern (inkl. vom Benutzer geänderter Felder)
          daten.ki_daten = { vorschlag: a.vorschlag || null, ki: a.ki, text_quelle: a.text_quelle || null,
            datei: a.datei || null, geaendert: [...bs.ef.geaendert] };
        }
        b = await api("POST", `/api/mandanten/${MANDANT_ID}/bestellungen`, daten);
        toast(`Bestellung „${b.kreditor}“ erfasst – Status „zur Freigabe“.`, false);
      }
      efSchliessen(true);
      await bsListeLaden();
      if (bs.tab === "freigabe") fgLaden();
      if (pruefen && b) fdOeffnen(b.id);
    });
  } catch (e) { toast(e.message); }
}

// ungespeicherte Eingaben bzw. ausgelesene Datei vorhanden?
function efHatEingaben() {
  return !!(bs.ef && (bs.ef.geaendert.size || bs.ef.auslesen));
}

function efSchliessen(ohneRueckfrage = false) {
  const dlg = document.getElementById("erfassung-dialog");
  if (!ohneRueckfrage && efHatEingaben() && !confirm("Eingaben verwerfen?")) return;
  bs.ef = null;
  bsDialogZu(dlg);
}

(function efEreignisse() {
  const f = efForm();
  const merken = (e) => {
    const name = e.target.name;
    if (!name || !bs.ef) return;
    e.target.classList.remove("ki-vorschlag");
    e.target.title = "";
    bs.ef.geaendert.add(name);
    efBerechnen();
  };
  f.addEventListener("input", merken);
  f.addEventListener("change", merken);
  f.addEventListener("submit", (e) => { e.preventDefault(); efSpeichern(false); });
  document.getElementById("ef-speichern").addEventListener("click", () => efSpeichern(false));
  document.getElementById("ef-speichern-pruefen").addEventListener("click", () => efSpeichern(true));
  document.getElementById("ef-abbrechen").addEventListener("click", () => efSchliessen());
  document.getElementById("ef-schliessen").addEventListener("click", () => efSchliessen());
  const dlg = document.getElementById("erfassung-dialog");
  dlg.addEventListener("cancel", (e) => {                       // Esc
    if (efHatEingaben() && !confirm("Eingaben verwerfen?")) e.preventDefault();
  });
  dlg.addEventListener("close", () => {
    bs.ef = null;
    document.getElementById("ef-vorschau").innerHTML = "";      // Vorschau entladen
  });
})();

// ------------------------------------------------------------ KW-/Basis-Auswahl
function bsKwOptionen(select, { vonWochen, bisWochen, gewaehlt = null, extra = [] }) {
  const montag = montagVon(bs.heute);
  const werte = [];
  for (let i = vonWochen; i <= bisWochen; i++) werte.push(tagPlus(montag, 7 * i));
  for (const m of extra) if (m && !werte.includes(m)) werte.push(m);
  werte.sort();
  select.innerHTML = werte.map(m => {
    const w = isoWoche(m);
    const wert = `${w.jahr}-${w.kw}`;
    const laufend = m === montag ? " · laufende KW" : "";
    return `<option value="${wert}"${wert === gewaehlt ? " selected" : ""}>KW ${w.kw}/${w.jahr} ` +
      `(${datumDe(m)}–${datumVollDe(tagPlus(m, 6))})${laufend}</option>`;
  }).join("");
  if (!gewaehlt) {
    const w = isoWoche(montag);
    select.value = `${w.jahr}-${w.kw}`;
  }
}

function bsKwWert(select) {
  const [jahr, kw] = (select.value || "").split("-").map(Number);
  return jahr && kw ? { jahr, kw } : null;
}

async function bsSnapshotsLaden() {
  if (bs.snapshots) return bs.snapshots;
  try { bs.snapshots = await api("GET", `/api/mandanten/${MANDANT_ID}/plan/snapshots`); }
  catch (e) { bs.snapshots = []; }
  return bs.snapshots;
}

function bsBasisOptionen(select, gewaehlt = "") {
  const snaps = bs.snapshots || [];
  select.innerHTML = `<option value="">Soll-Referenz / automatisch</option>` +
    snaps.map(s => `<option value="${s.id}">Planstand ${datumVollDe(s.stichtag)} · ${s.wochen} Wo.` +
      `${s.referenz ? " · Soll-Referenz" : ""}${s.kommentar ? " · " + esc(s.kommentar) : ""}</option>`).join("") +
    `<option value="LIVE">aktueller Plan (live)</option>`;
  select.value = gewaehlt || "";
  if (select.value !== (gewaehlt || "")) select.value = "";
}

function bsBasisText(basis) {
  if (!basis) return "";
  if (basis.typ === "SNAPSHOT" && basis.snapshot) {
    return `Basis: Planstand vom ${datumVollDe(basis.snapshot.stichtag)}${basis.snapshot.referenz ? " (Soll-Referenz)" : ""}` +
      `${basis.snapshot.kommentar ? " – " + esc(basis.snapshot.kommentar) : ""}`;
  }
  return "Basis: aktueller Plan (live)";
}

// ------------------------------------------------------------ Tab „Freigabe (KW)“
async function fgLaden() {
  const sel = document.getElementById("fg-kw");
  const kw = bsKwWert(sel);
  const basis = document.getElementById("fg-basis").value;
  const box = document.getElementById("fg-rahmen");
  fgKnoepfeSetzen();
  if (!kw) return;
  box.innerHTML = `<p class="hinweis">Wird berechnet …</p>`;
  let d;
  try {
    d = await api("GET", `/api/mandanten/${MANDANT_ID}/bestellungen/freigabe-uebersicht?jahr=${kw.jahr}&kw=${kw.kw}` +
      (basis ? `&basis=${encodeURIComponent(basis)}` : ""));
  } catch (e) {
    box.innerHTML = `<div class="warnung rot">${esc(e.message)}</div>`;
    document.getElementById("fg-offen").innerHTML = "";
    document.getElementById("fg-freigegeben").innerHTML = "";
    document.getElementById("fg-info").textContent = "";
    return;
  }
  const g = d.gesamt || {};
  const gb = g.gebunden || {}, ab = g.abweichung || {};
  const summeOffen = d.summe_offen || 0;
  const zeile = (text, wert, klasse = "", erl = "", rot = false) =>
    `<tr class="${klasse}"><td>${text}${erl ? `<span class="bf-erl">${erl}</span>` : ""}</td>
      <td class="zahl ${rot ? bsNegKlasse(wert) : ""}">${bsEur(wert)}</td></tr>`;
  const kwText = d.kw ? d.kw.label : "";
  const laufend = d.kw && d.kw.von && d.kw.von <= bs.heute;
  box.innerHTML = `
    <table class="liste bf-tabelle bf-rahmen">
      <thead><tr><th>Rahmen ${esc(kwText)} – alle Auszahlungen</th><th class="zahl">€</th></tr></thead>
      <tbody>
        ${zeile(`Plan-Auszahlungen ${esc(kwText)} (ganze Woche)`, g.soll_kw)}
        ${laufend ? zeile("davon bereits vergangen (steckt im Ist bis gestern)", g.soll_vergangen, "bf-davon") : ""}
        ${zeile("Plan-Auszahlungen ab heute", g.soll_kw_rest, "bf-zwischen")}
        ${zeile("− gebunden: freigegebene Bestellungen", gb.bestellungen)}
        ${zeile("− gebunden: Rechnungen, Daueraufträge, Termine", gb.sonstige)}
        ${zeile("− bisherige Abweichung bis gestern inkl. Vorträge", ab.summe, "",
          `Ist/Plan ${bsEur(ab.ist_minus_plan)} · Vorträge ${bsEur(ab.vortraege)} · überfällig ${bsEur(ab.ueberfaellig)}`)}
        ${zeile("= verfügbar", g.verfuegbar, "bf-summe", "", true)}
        ${zeile(`− offene Bestellungen zur Freigabe (${(d.offen || []).length})`, summeOffen)}
        ${zeile("= Rest nach Freigabe aller offenen", g.nach_freigabe, "bf-ergebnis", "", true)}
      </tbody>
    </table>
    <p class="hinweis" style="margin:6px 0 0">${[bsBasisText(d.basis), d.basis && d.basis.hinweis ? esc(d.basis.hinweis) : ""].filter(Boolean).join(" · ")}<br>
      Abweichung: positiv = bisher mehr ausgegeben als geplant (mindert den Rahmen), negativ = weniger (erhöht ihn).</p>`;
  document.getElementById("fg-info").textContent = d.kw ? `${datumVollDe(d.kw.von)} – ${datumVollDe(d.kw.bis)}` : "";

  // offene Bestellungen mit laufendem Rest (Ebene gesamt) – geprüft wird der offene Rest
  const tbO = document.getElementById("fg-offen");
  tbO.innerHTML = "";
  let rest = g.verfuegbar || 0;
  for (const b of d.offen || []) {
    const pruef = fgPruefbetrag(b, d.heute || bs.heute);
    rest = Math.round((rest - pruef) * 100) / 100;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><strong>${esc(b.kreditor)}</strong>${b.bestellnummer ? ` <span class="klein">Nr. ${esc(b.bestellnummer)}</span>` : ""}</td>
      <td class="klein">${esc(bsKontoText(b.konto))}</td>
      <td>${bsDatum(b.zahldatum)}${b.zahldatum < bs.heute ? ` <span class="badge-gesperrt">überfällig</span>` : ""}</td>
      <td class="zahl">${eur(b.betrag_brutto)}${Math.abs(pruef - (b.betrag_brutto || 0)) > 0.004
        ? `<br><span class="klein" title="Brutto abzüglich bereits geleisteter Teilzahlungen">offen ${eur(pruef)}</span>` : ""}</td>
      <td class="zahl ${bsNegKlasse(rest)}">${eur(rest)}</td>
      <td><button class="${bs.rechte.freigeben ? "" : "sekundaer "}klein-knopf" data-id="${b.id}">${bs.rechte.freigeben ? "prüfen …" : "ansehen"}</button></td>`;
    tbO.appendChild(tr);
  }
  if (!tbO.children.length) tbO.innerHTML = `<tr><td colspan="6" class="hinweis">Keine Bestellungen zur Freigabe in dieser KW.</td></tr>`;
  const tbF = document.getElementById("fg-freigegeben");
  tbF.innerHTML = "";
  for (const b of d.freigegeben || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><strong>${esc(b.kreditor)}</strong>${b.bestellnummer ? ` <span class="klein">Nr. ${esc(b.bestellnummer)}</span>` : ""}</td>
      <td class="klein">${esc(bsKontoText(b.konto))}</td>
      <td>${bsDatum(b.zahldatum)}</td>
      <td class="zahl">${eur(b.betrag_brutto)}${b.abgeloest_betrag > 0 ? `<br><span class="klein">offen ${eur(b.rest_offen)}</span>` : ""}</td>
      <td class="klein">${b.entschieden_am ? bsZeit(b.entschieden_am) : ""}${b.entschieden_von ? " · " + esc(b.entschieden_von) : ""}</td>
      <td><button class="sekundaer klein-knopf" data-id="${b.id}">⋯</button></td>`;
    tbF.appendChild(tr);
  }
  if (!tbF.children.length) tbF.innerHTML = `<tr><td colspan="6" class="hinweis">Keine freigegebenen Bestellungen in dieser KW.</td></tr>`;
}

for (const id of ["fg-offen", "fg-freigegeben"]) {
  document.getElementById(id).addEventListener("click", (e) => {
    const knopf = e.target.closest("button[data-id]");
    if (knopf) fdOeffnen(parseInt(knopf.dataset.id));
  });
}

// Prüfbetrag einer offenen Bestellung wie im Backend (Summe „offene Bestellungen“):
// Brutto abzüglich der Teilzahlungen vor heute – heutige stehen noch nicht im Ist bis gestern
function fgPruefbetrag(b, heute) {
  if (!Array.isArray(b.abloesungen)) return b.rest_offen ?? b.betrag_brutto ?? 0;
  const gezahlt = b.abloesungen.filter(a => !a.abschliessend && a.datum && a.datum < heute)
    .reduce((s, a) => s + (a.betrag || 0), 0);
  return Math.max(Math.round(((b.betrag_brutto || 0) - gezahlt) * 100) / 100, 0);
}

// ◀/▶ an den Enden der Auswahl sperren (vergangene KWs gibt es nicht)
function fgKnoepfeSetzen() {
  const sel = document.getElementById("fg-kw");
  document.getElementById("fg-zurueck").disabled = sel.selectedIndex <= 0;
  document.getElementById("fg-vor").disabled = sel.selectedIndex >= sel.options.length - 1;
}

function fgKwSchritt(richtung) {
  const sel = document.getElementById("fg-kw");
  const i = sel.selectedIndex + richtung;
  if (i >= 0 && i < sel.options.length) { sel.selectedIndex = i; fgKnoepfeSetzen(); fgLaden(); }
}

// ------------------------------------------------------------ Freigabe-/Detaildialog
async function fdOeffnen(id, { jahr = null, kw = null, basis = "" } = {}) {
  let b;
  try { b = await api("GET", `/api/bestellungen/${id}`); }
  catch (e) { toast(e.message); return; }
  bs.fd = { id, b, kz: null, jahr, kw, basis };
  await bsSnapshotsLaden();
  fdZeichnen();
  bsDialogAuf(document.getElementById("freigabe-dialog"));
  if (bsAenderbar(b)) fdBerechnen();
}

function fdKopf(b) {
  const d = b.datei;
  const feld = (label, wert) => `<div><span class="bf-label">${label}</span><span class="bf-wert">${wert}</span></div>`;
  return `<div class="bf-kopf">
    ${feld("Kreditor", esc(b.kreditor))}
    ${feld("Bestellnummer", esc(b.bestellnummer || "–"))}
    ${feld("Konto", esc(bsKontoText(b.konto)))}
    ${feld("Netto / Brutto", `${eur(b.betrag_netto)} € / ${eur(b.betrag_brutto)} €<span class="klein" style="font-weight:400"> (${esc(bsSatzText(b.ust_satz))})</span>`)}
    ${feld("Zahlung erwartet", `${bsDatum(b.zahldatum)} <span class="klein" style="font-weight:400">${esc(b.kw ? b.kw.label : bsKwLabel(b.zahldatum))}</span>`)}
    ${feld("Bestellt / geliefert", `${bsDatum(b.bestelldatum)}${b.lieferdatum ? " / " + bsDatum(b.lieferdatum) : ""}`)}
    ${feld("Status", bsStatusBadge(b.status, b.status_text) +
      (b.entschieden_am ? `<span class="klein" style="font-weight:400"> ${bsZeit(b.entschieden_am)}${b.entschieden_von ? " · " + esc(b.entschieden_von) : ""}</span>` : ""))}
    ${feld("Datei", d ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">📄 ${esc(d.name)}</a>` : "–")}
  </div>
  ${b.beschreibung ? `<p style="margin:8px 0 0;white-space:pre-wrap">${esc(b.beschreibung)}</p>` : ""}`;
}

function fdZeichnen() {
  const { b } = bs.fd;
  document.getElementById("fd-titel").textContent =
    `${b.status === "OFFEN" && bs.rechte.freigeben ? "Bestellfreigabe" : "Bestellung"} – ${b.kreditor}`;
  document.getElementById("fd-untertitel").textContent =
    `erfasst ${bsZeit(b.erstellt_am)}${b.erstellt_von ? " von " + b.erstellt_von : ""}${b.ki ? " · mit KI ausgelesen" : ""}`;
  const inhalt = document.getElementById("fd-inhalt");
  let html = fdKopf(b);
  if (bsAenderbar(b)) {
    html += `<div class="zd-abschnitt">
      <h4>Freigabeprüfung</h4>
      <div class="bf-steuer">
        <label>KW <select id="fd-kw"></select></label>
        <label>Basis <select id="fd-basis"></select></label>
        <span class="hinweis" id="fd-basis-text"></span>
      </div>
      <div id="fd-rechnung"><p class="hinweis">Wird berechnet …</p></div>
    </div>`;
    html += fdEntscheidungHtml(b);
  }
  if ((b.abloesungen || []).length || b.status === "ABGELOEST") html += fdAbloesungenHtml(b);
  html += `<div class="zd-abschnitt"><h4>Journal dieser Bestellung</h4>${fdJournalHtml(b.journal || [])}</div>`;
  const aktionen = [];
  if (!LESER && bsAenderbar(b)) aktionen.push(`<button type="button" class="sekundaer" data-fd="bearbeiten">✎ Bearbeiten</button>`);
  if (!LESER && bsOffen(b)) aktionen.push(`<button type="button" class="sekundaer" data-fd="abloesen">Ablösen …</button>`);
  if (!LESER && bsAenderbar(b)) aktionen.push(`<button type="button" class="sekundaer gefahr-text" data-fd="stornieren">Stornieren …</button>`);
  const entschieden = (b.journal || []).some(j => j.aktion === "FREIGEGEBEN" || j.aktion === "ABGELEHNT");
  if (!LESER && !entschieden && !(b.abloesungen || []).length && b.status !== "ABGELOEST") {
    aktionen.push(`<button type="button" class="sekundaer gefahr-text" data-fd="loeschen" title="Nur solange nie über die Bestellung entschieden wurde">Löschen</button>`);
  }
  html += `<div class="werkzeuge bs-fuss">${aktionen.join("")}
    <button type="button" class="sekundaer" data-fd="schliessen" style="margin-left:auto">Schließen</button></div>`;
  inhalt.innerHTML = html;

  if (bsAenderbar(b)) {
    const zd = isoWoche(b.zahldatum && b.zahldatum > bs.heute ? b.zahldatum : bs.heute);
    const gewaehlt = bs.fd.jahr ? `${bs.fd.jahr}-${bs.fd.kw}` : `${zd.jahr}-${zd.kw}`;
    bsKwOptionen(document.getElementById("fd-kw"), { vonWochen: 0, bisWochen: 13, gewaehlt,
      extra: [b.zahldatum ? montagVon(b.zahldatum) : null, bs.fd.jahr ? kwMontag(bs.fd.jahr, bs.fd.kw) : null]
        .filter(m => m && m >= montagVon(bs.heute)) });
    bsBasisOptionen(document.getElementById("fd-basis"), bs.fd.basis);
    document.getElementById("fd-kw").addEventListener("change", () => {
      const w = bsKwWert(document.getElementById("fd-kw"));
      bs.fd.jahr = w.jahr; bs.fd.kw = w.kw;
      fdBerechnen();
    });
    document.getElementById("fd-basis").addEventListener("change", (e) => { bs.fd.basis = e.target.value; fdBerechnen(); });
    fdEntscheidungVerdrahten(b);
  }
  inhalt.querySelectorAll("[data-fd]").forEach(k => k.addEventListener("click", () => fdAktion(k.dataset.fd)));
  inhalt.querySelectorAll("[data-abloesung-aufheben]").forEach(k => k.addEventListener("click",
    () => bsAbloesungAufheben(parseInt(k.dataset.abloesungAufheben), () => fdOeffnen(b.id))));
}

async function fdBerechnen() {
  const fd = bs.fd;
  if (!fd) return;
  const box = document.getElementById("fd-rechnung");
  const q = [];
  if (fd.jahr && fd.kw) q.push(`jahr=${fd.jahr}`, `kw=${fd.kw}`);
  if (fd.basis) q.push(`basis=${encodeURIComponent(fd.basis)}`);
  box.innerHTML = `<p class="hinweis">Wird berechnet …</p>`;
  let kz;
  try { kz = await api("GET", `/api/bestellungen/${fd.id}/freigabe${q.length ? "?" + q.join("&") : ""}`); }
  catch (e) {
    if (bs.fd !== fd) return;
    fd.kz = null;
    box.innerHTML = `<div class="warnung rot">Rahmen nicht berechenbar: ${esc(e.message)}</div>`;
    fdEntscheidungAktualisieren();
    return;
  }
  if (bs.fd !== fd) return;           // Dialog inzwischen gewechselt
  fd.kz = kz;
  if (kz.kw) {
    fd.jahr = kz.kw.jahr; fd.kw = kz.kw.kw;
    const sel = document.getElementById("fd-kw");
    const wert = `${kz.kw.jahr}-${kz.kw.kw}`;
    if (![...sel.options].some(o => o.value === wert)) {
      bsKwOptionen(sel, { vonWochen: 0, bisWochen: 13, gewaehlt: wert, extra: [kz.kw.von] });
    }
    sel.value = wert;
  }
  document.getElementById("fd-basis-text").textContent = bsBasisText(kz.basis);
  box.innerHTML = fdRechnungHtml(kz);
  box.querySelectorAll("button[data-id]").forEach(k => k.addEventListener("click", () => fdOeffnen(parseInt(k.dataset.id))));
  fdEntscheidungAktualisieren();
}

// Rechenweg: Zeilen × Ebenen (Konto | Gruppe | Gesamt), maßgebliche Spalte hervorgehoben
function fdRechnungHtml(kz) {
  const reihenfolge = ["konto", "gruppe", "gesamt"];
  const ebenen = reihenfolge.map(e => (kz.ebenen || []).find(x => x.ebene === e)).filter(Boolean);
  const m = kz.massgeblich;
  const kwText = kz.kw ? kz.kw.label : "";
  const laufend = kz.ab && kz.kw && kz.ab > kz.kw.von;
  const zeilen = [
    { text: `Plan-Auszahlungen ${esc(kwText)} (gesamte Woche)`, wert: e => e.soll_kw },
    laufend ? { text: `davon bereits vergangen (bis ${datumDe(tagPlus(kz.ab, -1))}, steckt im Ist bis gestern)`,
      wert: e => e.soll_vergangen, klasse: "bf-davon" } : null,
    { text: laufend ? `Plan-Auszahlungen ${esc(kwText)} ab heute` : `Plan-Auszahlungen ${esc(kwText)}`,
      wert: e => e.soll_kw_rest, klasse: "bf-zwischen" },
    { text: "− freigegebene Bestellungen", wert: e => (e.gebunden || {}).bestellungen,
      erl: "bereits freigegeben, Zahlung in der KW" },
    { text: "− Rechnungen / Daueraufträge / Termine", wert: e => (e.gebunden || {}).sonstige,
      erl: "offene Posten, Dauerschuldverhältnisse, Steuer-/SV-Termine" },
    { text: "− Abweichung Ist/Plan bis gestern", wert: e => (e.abweichung || {}).ist_minus_plan,
      erl: `seit ${kz.basis && kz.basis.s0 ? datumVollDe(kz.basis.s0) : "Planbeginn"}; positiv = Mehrausgabe` },
    { text: "− Vorträge", wert: e => (e.abweichung || {}).vortraege, erl: "nicht gebuchte Raten aus Dauerschuldverhältnissen" },
    { text: "− überfällige Posten", wert: e => (e.abweichung || {}).ueberfaellig, erl: "überfällig, auf heute/morgen vorgetragen" },
    { text: "= verfügbar", wert: e => e.verfuegbar, klasse: "bf-summe", rot: true },
    // betrag_brutto = Prüfbetrag (offener Rest); Teilzahlungen stecken schon im Ist
    kz.bereits_gezahlt > 0
      ? { text: "− diese Bestellung (offener Rest)", wert: () => kz.betrag_brutto,
          erl: `brutto ${eur(kz.bestellung_brutto)} €, bereits gezahlt ${eur(kz.bereits_gezahlt)} €` }
      : { text: "− diese Bestellung (brutto)", wert: () => kz.betrag_brutto },
    { text: "= Rest nach Freigabe", wert: e => e.nach_freigabe, klasse: "bf-ergebnis", rot: true },
  ].filter(Boolean);
  const kopf = ebenen.map(e => `<th class="zahl${e.ebene === m ? " bf-massgeblich" : ""}">${BS_EBENE_TEXT[e.ebene] || e.ebene}
      <span class="bf-erl">${esc(e.name || "")}</span>${e.ebene === m ? `<span class="bf-erl"><strong>maßgeblich</strong></span>` : ""}</th>`).join("");
  const koerper = zeilen.map(z => `<tr class="${z.klasse || ""}"><td>${z.text}${z.erl ? `<span class="bf-erl">${esc(z.erl)}</span>` : ""}</td>` +
    ebenen.map(e => {
      const v = z.wert(e);
      return `<td class="zahl${e.ebene === m ? " bf-massgeblich" : ""} ${z.rot ? bsNegKlasse(v) : ""}">${bsEur(v)}</td>`;
    }).join("") + `</tr>`).join("");
  const me = ebenen.find(e => e.ebene === m) || ebenen[ebenen.length - 1];
  let ampel = "";
  if (me) {
    const ebeneText = `${BS_EBENE_TEXT[me.ebene] || me.ebene}${me.name ? " " + esc(me.name) : ""}`;
    const grund = m === "gruppe" ? " – auf dem Konto ist in der KW nichts geplant, daher zählt der Rahmen der BWA-Gruppe" : "";
    ampel = kz.ampel === "gruen"
      ? `<div class="bf-ampel gruen"><span class="bf-licht"></span><div><strong>Im Rahmen.</strong>
          Nach Freigabe verbleiben ${bsEur(me.nach_freigabe)} (maßgeblich: ${ebeneText}${grund}).</div></div>`
      : `<div class="bf-ampel rot"><span class="bf-licht"></span><div><strong>Überschreitet den Rahmen um ${bsEur(-me.nach_freigabe)}</strong>
          (maßgeblich: ${ebeneText}${grund}). Eine Freigabe sollte begründet werden.</div></div>`;
  }
  const l = kz.liquiditaet || {};
  const liq = l.ende_kw !== undefined && l.ende_kw !== null ? `<div class="bf-liquiditaet">
      <span>Liquidität Ende ${esc(kwText)}: <strong style="${l.ende_kw < 0 ? "color:var(--rot)" : ""}">${bsEur(l.ende_kw)}</strong></span>
      <span>nach Freigabe: <strong style="${l.nach_freigabe < 0 ? "color:var(--rot)" : ""}">${bsEur(l.nach_freigabe)}</strong>
        ${l.bestellung_im_plan ? `<span class="klein">(Bestellung steht bereits im Plan)</span>` : ""}</span>
      ${l.verfuegbar_ende_kw !== null && l.verfuegbar_ende_kw !== undefined && Math.abs(l.verfuegbar_ende_kw - l.ende_kw) > 0.004
        ? `<span>verfügbar inkl. Kreditlinien: <strong style="${l.verfuegbar_ende_kw < 0 ? "color:var(--rot)" : ""}">${bsEur(l.verfuegbar_ende_kw)}</strong></span>` : ""}
    </div>` : "";
  const hinweise = [];
  if (kz.basis && kz.basis.hinweis) hinweise.push(kz.basis.hinweis);
  for (const h of kz.hinweise || []) if (!hinweise.includes(h)) hinweise.push(h);
  const kwListe = (titel, liste) => liste && liste.length ? `<div style="margin-top:8px"><strong style="font-size:12.5px">${titel}</strong>
      <ul class="zd-liste">${liste.map(x => `<li><span>${bsDatum(x.zahldatum)}</span><span>${esc(x.kreditor)}</span>
        <span class="zahl">${eur(x.betrag_brutto)} €</span>
        <button type="button" class="sekundaer klein-knopf" data-id="${x.id}">öffnen</button></li>`).join("")}</ul></div>` : "";
  return `<table class="liste bf-tabelle">
      <thead><tr><th>Rechenweg ${esc(kwText)}${kz.kw ? ` <span class="bf-erl">${datumVollDe(kz.kw.von)} – ${datumVollDe(kz.kw.bis)} · Ist bis ${datumVollDe(kz.ist_bis)}</span>` : ""}</th>${kopf}</tr></thead>
      <tbody>${koerper}</tbody></table>
    <p class="hinweis" style="margin:4px 0 0">Alle Beträge brutto als Auszahlungen. Abweichung: positiv = bisher mehr
      ausgegeben als geplant (mindert den Rahmen), negativ = weniger (erhöht ihn).</p>
    ${ampel}${liq}
    ${hinweise.length ? `<ul class="bf-hinweise">${hinweise.map(h => `<li>${esc(h)}</li>`).join("")}</ul>` : ""}
    ${kwListe(`Bereits freigegeben in ${esc(kwText)}`, kz.freigegebene_bestellungen)}
    ${kwListe(`Weitere Bestellungen zur Freigabe in ${esc(kwText)}`, kz.offene_bestellungen)}`;
}

function fdEntscheidungHtml(b) {
  if (!bs.rechte.freigeben) {
    return `<div class="zd-abschnitt"><p class="hinweis" style="margin:0">Freigabe bzw. Ablehnung nur durch
      berechtigte Benutzer (Sachwalter, Administratoren).</p></div>`;
  }
  if (b.status === "OFFEN") {
    return `<div class="zd-abschnitt bf-entscheidung"><h4>Entscheidung</h4>
      <label>Begründung <span class="hinweis">(Pflicht bei Ablehnung; empfohlen bei Überschreitung)</span>
        <textarea id="fd-begruendung" rows="3" maxlength="2000"></textarea></label>
      <div class="werkzeuge bs-fuss">
        <span class="hinweis bs-fuss-links" id="fd-entscheidung-hinweis">Die Kennzahlen werden beim Entscheiden neu berechnet und im Journal gespeichert.</span>
        <button type="button" class="sekundaer" data-fd="schliessen">Abbrechen</button>
        <button type="button" class="gefahr" id="fd-ablehnen">Ablehnen</button>
        <button type="button" id="fd-freigeben">Freigeben</button>
      </div></div>`;
  }
  return `<div class="zd-abschnitt bf-entscheidung"><h4>Entscheidung zurücknehmen</h4>
    <p class="hinweis" style="margin:0 0 6px">Die Bestellung ist ${esc(b.status_text || BS_STATUS_TEXT[b.status])}${b.entschieden_von ? " (" + esc(b.entschieden_von) + ")" : ""}.
      Beim Zurücknehmen steht sie wieder zur Freigabe.</p>
    <label>Begründung <span class="hinweis">(Pflicht)</span><textarea id="fd-begruendung" rows="2" maxlength="2000"></textarea></label>
    <div class="werkzeuge bs-fuss"><button type="button" class="sekundaer gefahr-text" id="fd-zuruecksetzen">Entscheidung zurücknehmen</button></div></div>`;
}

function fdEntscheidungAktualisieren() {
  const fd = bs.fd;
  const knopf = document.getElementById("fd-freigeben");
  if (!knopf || !fd) return;
  knopf.textContent = fd.kz && fd.kz.ampel === "rot" ? "Trotzdem freigeben" : "Freigeben";
  const hinweis = document.getElementById("fd-entscheidung-hinweis");
  if (hinweis) hinweis.textContent = fd.kz && fd.kz.kw
    ? `Entscheidung für ${fd.kz.kw.label}; die Kennzahlen werden dabei neu berechnet und im Journal gespeichert.`
    : "Die Kennzahlen werden beim Entscheiden neu berechnet und im Journal gespeichert.";
}

function fdEntscheidungVerdrahten(b) {
  const fr = document.getElementById("fd-freigeben");
  const ab = document.getElementById("fd-ablehnen");
  const zu = document.getElementById("fd-zuruecksetzen");
  if (fr) fr.addEventListener("click", () => fdEntscheiden("FREIGEBEN", fr));
  if (ab) ab.addEventListener("click", () => fdEntscheiden("ABLEHNEN", ab));
  if (zu) zu.addEventListener("click", async () => {
    const text = document.getElementById("fd-begruendung").value.trim();
    if (!text) { toast("Bitte begründen, warum die Entscheidung zurückgenommen wird."); document.getElementById("fd-begruendung").focus(); return; }
    try {
      await bsMitBusy(zu, "Wird gespeichert …", async () => {
        await api("POST", `/api/bestellungen/${b.id}/zuruecksetzen`, { begruendung: text });
        toast("Entscheidung zurückgenommen – die Bestellung steht wieder zur Freigabe.", false);
        await bsNachAenderung();
        fdOeffnen(b.id);
      });
    } catch (e) { toast(e.message); }
  });
}

async function fdEntscheiden(entscheidung, knopf) {
  const fd = bs.fd;
  const feld = document.getElementById("fd-begruendung");
  const text = feld.value.trim();
  if (entscheidung === "ABLEHNEN" && !text) { toast("Bitte die Ablehnung begründen."); feld.focus(); return; }
  if (entscheidung === "FREIGEBEN" && fd.kz && fd.kz.ampel === "rot" && !text
      && !confirm("Die Bestellung überschreitet den Rahmen. Ohne Begründung freigeben?")) { feld.focus(); return; }
  const w = fd.jahr && fd.kw ? { jahr: fd.jahr, kw: fd.kw } : bsKwWert(document.getElementById("fd-kw"));
  try {
    await bsMitBusy(knopf, "Wird gespeichert …", async () => {
      const b = await api("POST", `/api/bestellungen/${fd.id}/entscheidung`, {
        entscheidung, begruendung: text || null,
        jahr: w ? w.jahr : null, kw: w ? w.kw : null,
        basis: fd.basis ? String(fd.basis) : null,
      });
      toast(entscheidung === "FREIGEBEN" ? `Bestellung „${b.kreditor}“ freigegeben.` : `Bestellung „${b.kreditor}“ abgelehnt.`, false);
      bsDialogZu(document.getElementById("freigabe-dialog"));
      await bsNachAenderung();
    });
  } catch (e) { toast(e.message); }
}

// Zusatz je Ablösung: die erledigende (Server: `erledigt`, auch ohne ausdrückliches
// „abschließend“, wenn die Summe das Brutto deckt) ist keine Teilzahlung
function fdAbloesungZusatz(a, anzahl) {
  if (a.erledigt) return anzahl > 1 ? " (abschließend)" : "";
  return a.abschliessend ? "" : " (Teilzahlung)";
}

function fdAbloesungenHtml(b) {
  const liste = b.abloesungen || [];
  const ART = { ZAHLUNG: "Zahlung", RECHNUNG: "Eingangsrechnung", MANUELL: "manuell" };
  return `<div class="zd-abschnitt"><h4>Ablösung</h4>
    ${b.status === "ABGELOEST" ? `<p style="margin:0 0 6px">Abgelöst am ${bsDatum(b.abgeloest_am)} durch
      ${esc(ART[b.abgeloest_durch] || b.abgeloest_durch || "–")}: ${bsEur(b.abgeloest_betrag)}
      ${b.abweichung !== null && b.abweichung !== undefined ? ` · Abweichung zum Bestellbrutto ${b.abweichung > 0 ? "+" : ""}${eur(b.abweichung)} €` : ""}</p>` : ""}
    ${liste.length ? `<ul class="zd-liste">${liste.map(a => `<li><span>${bsDatum(a.datum)}</span>
        <span>${esc(ART[a.art] || a.art)}${fdAbloesungZusatz(a, liste.length)}</span>
        <span class="zahl">${eur(a.betrag)} €</span>
        <span class="hinweis">${esc(a.beleg || "")}${a.notiz ? " · " + esc(a.notiz) : ""}</span>
        ${LESER ? "" : `<button type="button" class="sekundaer klein-knopf" data-abloesung-aufheben="${a.id}" title="Ablösung aufheben">aufheben</button>`}</li>`).join("")}</ul>`
      : `<p class="hinweis">Keine Ablösungen.</p>`}</div>`;
}

function bsKennzahlenKurz(k) {
  if (!k || typeof k !== "object") return "";
  const teile = [];
  if (k.kw && k.kw.label) teile.push(k.kw.label);
  const e = (k.ebenen || []).find(x => x.ebene === k.massgeblich);
  if (e) {
    teile.push(`${BS_EBENE_TEXT[e.ebene] || e.ebene}: verfügbar ${bsEur(e.verfuegbar)}, nach Freigabe ${bsEur(e.nach_freigabe)}`);
  }
  if (k.ampel) teile.push(k.ampel === "gruen" ? "im Rahmen" : "über Rahmen");
  if (k.bereits_gezahlt > 0) {
    teile.push(`geprüft offener Rest ${eur(k.betrag_brutto)} € (brutto ${eur(k.bestellung_brutto)} €, bereits gezahlt ${eur(k.bereits_gezahlt)} €)`);
  }
  if (k.basis) teile.push(k.basis.typ === "SNAPSHOT" && k.basis.snapshot ? `Planstand ${datumVollDe(k.basis.snapshot.stichtag)}` : "aktueller Plan");
  return teile.join(" · ");
}

function bsStatusWechsel(j) {
  if (!j.status_vorher && !j.status_nachher) return "";
  const text = (st) => esc(BS_STATUS_TEXT[st] || st || "–");
  if (j.status_vorher === j.status_nachher) return text(j.status_nachher);
  if (!j.status_vorher) return `→ ${text(j.status_nachher)}`;
  if (!j.status_nachher) return `${text(j.status_vorher)} →`;
  return `${text(j.status_vorher)} → ${text(j.status_nachher)}`;
}

function fdJournalHtml(journal) {
  if (!journal.length) return `<p class="hinweis">Noch keine Einträge.</p>`;
  return `<ul class="zd-liste">${journal.map(j => {
    const kz = bsKennzahlenKurz(j.kennzahlen);
    return `<li style="flex-wrap:wrap"><span style="white-space:nowrap">${bsZeit(j.zeit)}</span>
      ${bsAktionBadge(j.aktion, j.aktion_text)}
      <span>${esc(j.benutzer || "–")}</span>
      <span class="hinweis">${bsStatusWechsel(j)}</span>
      ${j.begruendung ? `<span style="flex-basis:100%;white-space:pre-wrap">„${esc(j.begruendung)}“</span>` : ""}
      ${kz ? `<span class="hinweis" style="flex-basis:100%">${esc(kz)}</span>` : ""}</li>`;
  }).join("")}</ul>`;
}

async function fdAktion(aktion) {
  const fd = bs.fd;
  const dlg = document.getElementById("freigabe-dialog");
  if (aktion === "schliessen") { bsDialogZu(dlg); return; }
  if (!fd) return;
  const b = fd.b;
  if (aktion === "bearbeiten") { bsDialogZu(dlg); efOeffnen({ modus: "bearbeiten", bestellung: b }); return; }
  if (aktion === "abloesen") { bsDialogZu(dlg); adOeffnen(b.id); return; }
  try {
    if (aktion === "stornieren") {
      const grund = prompt(`Bestellung „${b.kreditor}“ stornieren? Sie fällt damit aus dem Plan.\nBegründung (optional):`, "");
      if (grund === null) return;
      await api("POST", `/api/bestellungen/${b.id}/stornieren`, { begruendung: grund.trim() || null });
      toast("Bestellung storniert.", false);
    } else if (aktion === "loeschen") {
      if (!confirm(`Bestellung „${b.kreditor}“ endgültig löschen? Der Journaleintrag bleibt erhalten.`)) return;
      await api("DELETE", `/api/bestellungen/${b.id}`);
      toast("Bestellung gelöscht.", false);
    }
    bsDialogZu(dlg);
    await bsNachAenderung();
  } catch (e) { toast(e.message); }
}

document.getElementById("fd-schliessen").addEventListener("click", () => bsDialogZu(document.getElementById("freigabe-dialog")));
document.getElementById("freigabe-dialog").addEventListener("close", () => { bs.fd = null; });

// nach jeder Statusänderung: Liste und ggf. aktiven Tab neu laden
async function bsNachAenderung() {
  await bsListeLaden();
  if (bs.tab === "freigabe") fgLaden();
  if (bs.tab === "abgleich") abLaden();
  if (bs.tab === "journal") jnLaden();
}

// ------------------------------------------------------------ Ablösedialog
async function adOeffnen(id) {
  let b, kandidaten = [], fehler = null;
  try { b = await api("GET", `/api/bestellungen/${id}`); }
  catch (e) { toast(e.message); return; }
  try { kandidaten = await api("GET", `/api/bestellungen/${id}/abloesung/kandidaten`); }
  catch (e) { fehler = e.message; }
  bs.ad = { b, kandidaten: kandidaten || [], fehler, art: null, gewaehlt: null };
  const zahlungen = bs.ad.kandidaten.filter(k => k.typ !== "POSTEN");
  const rechnungen = bs.ad.kandidaten.filter(k => k.typ === "POSTEN");
  bs.ad.art = zahlungen.length ? "ZAHLUNG" : (rechnungen.length ? "RECHNUNG" : "MANUELL");
  adZeichnen();
  bsDialogAuf(document.getElementById("abloese-dialog"));
}

function adOffenerRest(b) {
  return Math.round(((b.betrag_brutto || 0) - (b.abgeloest_betrag || 0)) * 100) / 100;
}

function adZeichnen() {
  const { b, kandidaten, fehler, art } = bs.ad;
  document.getElementById("ad-titel").textContent = `Bestellung ablösen – ${b.kreditor}`;
  document.getElementById("ad-untertitel").textContent =
    `Brutto ${eur(b.betrag_brutto)} €${b.abgeloest_betrag > 0 ? ` · bereits abgelöst ${eur(b.abgeloest_betrag)} € · offen ${eur(adOffenerRest(b))} €` : ""}` +
    ` · Zahlung erwartet ${bsDatum(b.zahldatum)}${b.bestellnummer ? ` · Nr. ${b.bestellnummer}` : ""}`;
  const zahlungen = kandidaten.filter(k => k.typ !== "POSTEN");
  const rechnungen = kandidaten.filter(k => k.typ === "POSTEN");
  const inhalt = document.getElementById("ad-inhalt");
  inhalt.innerHTML = `
    ${(b.abloesungen || []).length ? fdAbloesungenHtml(b) : ""}
    <div class="zd-abschnitt">
      <h4>Ablösen durch</h4>
      <div class="bd-optionen" style="flex-direction:row;flex-wrap:wrap;gap:6px 18px" id="ad-arten">
        <label><input type="radio" name="ad-art" value="ZAHLUNG"${art === "ZAHLUNG" ? " checked" : ""}> Zahlung
          <span class="hinweis">(Ist-Buchung oder Bankumsatz, ${zahlungen.length} Vorschläge)</span></label>
        <label><input type="radio" name="ad-art" value="RECHNUNG"${art === "RECHNUNG" ? " checked" : ""}> Rechnung (offener Posten)
          <span class="hinweis">(${rechnungen.length})</span></label>
        <label><input type="radio" name="ad-art" value="MANUELL"${art === "MANUELL" ? " checked" : ""}> manuell</label>
      </div>
      ${fehler ? `<div class="warnung rot">${esc(fehler)}</div>` : ""}
      <div id="ad-kandidaten" style="margin-top:8px"></div>
    </div>
    <div class="zd-abschnitt">
      <div class="zd-formular">
        <label>Betrag brutto (€)<input type="number" id="ad-betrag" step="0.01" min="0.01" style="width:150px"></label>
        <label id="ad-datum-feld">Datum<input type="date" id="ad-datum" style="width:160px"></label>
        <label style="flex:1;min-width:220px">Notiz<input id="ad-notiz" maxlength="255" placeholder="z. B. Minderlieferung, Skonto"></label>
      </div>
      <div class="bd-optionen" style="margin-top:8px">
        <label><input type="checkbox" id="ad-abschliessend" checked> Bestellung damit vollständig abgelöst
          <span class="hinweis">(z. B. Minderlieferung – sonst Teilzahlung, der Rest bleibt im Plan)</span></label>
      </div>
      <p id="ad-abweichung" style="margin:8px 0 0;font-size:13px"></p>
      <div class="werkzeuge bs-fuss">
        <button type="button" class="sekundaer" id="ad-abbrechen">Abbrechen</button>
        <button type="button" id="ad-speichern">Ablösen</button>
      </div>
    </div>`;
  inhalt.querySelectorAll('input[name="ad-art"]').forEach(r => r.addEventListener("change", () => {
    bs.ad.art = r.value;
    bs.ad.gewaehlt = null;
    adKandidatenZeichnen();
  }));
  inhalt.querySelectorAll("[data-abloesung-aufheben]").forEach(k => k.addEventListener("click",
    () => bsAbloesungAufheben(parseInt(k.dataset.abloesungAufheben), () => adOeffnen(b.id))));
  document.getElementById("ad-betrag").addEventListener("input", adAbweichungZeigen);
  document.getElementById("ad-abschliessend").addEventListener("change", adAbweichungZeigen);
  document.getElementById("ad-abbrechen").addEventListener("click", () => bsDialogZu(document.getElementById("abloese-dialog")));
  document.getElementById("ad-speichern").addEventListener("click", adSpeichern);
  adKandidatenZeichnen();
}

function adKandidatenZeichnen() {
  const { b, kandidaten, art } = bs.ad;
  const box = document.getElementById("ad-kandidaten");
  const betrag = document.getElementById("ad-betrag");
  const datum = document.getElementById("ad-datum");
  const abschl = document.getElementById("ad-abschliessend");
  document.getElementById("ad-datum-feld").style.display = art === "MANUELL" ? "" : "none";
  abschl.disabled = art === "RECHNUNG";
  if (art === "RECHNUNG") abschl.checked = true;
  if (art === "MANUELL") {
    box.innerHTML = `<p class="hinweis" style="margin:0">Manuelle Ablösung, z. B. bei Barzahlung, Verrechnung oder wenn die
      Zahlung nicht importiert wird. Betrag und Datum sind Pflicht.</p>`;
    betrag.value = adOffenerRest(b).toFixed(2);
    datum.value = bs.heute;
    adAbweichungZeigen();
    return;
  }
  const liste = kandidaten.filter(k => (art === "RECHNUNG") === (k.typ === "POSTEN"));
  if (!liste.length) {
    box.innerHTML = `<p class="hinweis" style="margin:0">${art === "RECHNUNG"
      ? "Keine offenen Eingangsrechnungen als Kandidaten gefunden."
      : "Keine passenden Ausgangszahlungen gefunden (Ist-Buchungen ab 7 Tage vor dem Bestelldatum, nicht zugeordnete Bankumsätze)."}</p>`;
    betrag.value = "";
    adAbweichungZeigen();
    return;
  }
  box.innerHTML = `<table class="liste"><thead><tr><th></th><th>Datum</th><th>${art === "RECHNUNG" ? "Rechnung" : "Zahlung"}</th>
      <th class="zahl">Betrag</th><th>Treffer</th><th>Begründung</th></tr></thead><tbody>${liste.map((k, i) => `
      <tr data-i="${i}" style="cursor:pointer">
        <td><input type="radio" name="ad-kandidat" value="${i}" style="width:auto"></td>
        <td>${bsDatum(k.datum)}</td>
        <td><span class="klein">${esc(BS_BELEG_TEXT[k.typ] || k.typ)}</span><br>${esc(k.text || "")}
          ${k.konto ? `<br><span class="klein">${esc(k.konto)}</span>` : ""}</td>
        <td class="zahl">${eur(k.betrag)}</td>
        <td>${bsTrefferBadge(k.score)}</td>
        <td class="klein">${(k.gruende || []).map(esc).join(", ")}</td></tr>`).join("")}</tbody></table>`;
  const waehle = (i) => {
    bs.ad.gewaehlt = liste[i];
    box.querySelectorAll('input[name="ad-kandidat"]').forEach(r => { r.checked = parseInt(r.value) === i; });
    betrag.value = Number(liste[i].betrag).toFixed(2);
    adAbweichungZeigen();
  };
  box.querySelectorAll("tr[data-i]").forEach(tr => tr.addEventListener("click", () => waehle(parseInt(tr.dataset.i))));
  // bester Vorschlag vorausgewählt, wenn eindeutig genug
  if (liste[0].score >= 60) waehle(0);
  else { bs.ad.gewaehlt = null; betrag.value = ""; adAbweichungZeigen(); }
}

function bsTrefferBadge(score) {
  if (score === null || score === undefined) return "";
  if (score >= 85) return `<span class="status-badge freigegeben">sicher (${score})</span>`;
  if (score >= 60) return `<span class="status-badge offen">möglich (${score})</span>`;
  return `<span class="status-badge neutral">schwach (${score})</span>`;
}

function adAbweichungZeigen() {
  const { b } = bs.ad;
  const el = document.getElementById("ad-abweichung");
  const betrag = parseFloat(document.getElementById("ad-betrag").value);
  if (isNaN(betrag)) { el.textContent = ""; return; }
  const gesamt = Math.round(((b.abgeloest_betrag || 0) + betrag) * 100) / 100;
  const abw = Math.round((gesamt - (b.betrag_brutto || 0)) * 100) / 100;
  const abschl = document.getElementById("ad-abschliessend").checked;
  if (!abschl) {
    const rest = Math.round(((b.betrag_brutto || 0) - gesamt) * 100) / 100;
    el.innerHTML = rest > 0.004
      ? `Teilzahlung: <strong>${eur(rest)} €</strong> bleiben offen${b.im_plan ? " und zum Zahldatum im Plan" : ""}.`
      : `Der Betrag deckt die Bestellung vollständig – sie gilt damit als abgelöst.`;
    return;
  }
  el.innerHTML = Math.abs(abw) < 0.005
    ? `Betrag entspricht dem Bestellbrutto.`
    : `Abweichung zum Bestellbrutto: <strong style="color:${abw < 0 ? "var(--gruen)" : "var(--rot)"}">${abw > 0 ? "+" : ""}${eur(abw)} €</strong>
       ${abw < 0 ? "(weniger gezahlt, z. B. Minderlieferung – der Rest entfällt im Plan)" : "(mehr als bestellt)"}`;
}

async function adSpeichern() {
  const { b, art, gewaehlt } = bs.ad;
  const betragWert = parseFloat(document.getElementById("ad-betrag").value);
  const daten = {
    art,
    abschliessend: document.getElementById("ad-abschliessend").checked,
    notiz: document.getElementById("ad-notiz").value.trim() || null,
  };
  if (art === "MANUELL") {
    if (!(betragWert > 0)) { toast("Bitte einen Betrag größer 0 angeben."); return; }
    const datum = document.getElementById("ad-datum").value;
    if (!datum) { toast("Bitte ein Datum angeben."); return; }
    daten.betrag = Math.round(betragWert * 100) / 100;
    daten.datum = datum;
  } else {
    if (!gewaehlt) { toast(`Bitte ${art === "RECHNUNG" ? "eine Rechnung" : "eine Zahlung"} auswählen.`); return; }
    if (gewaehlt.typ === "BUCHUNG") daten.buchung_id = gewaehlt.id;
    else if (gewaehlt.typ === "TRANSAKTION") daten.transaktion_id = gewaehlt.id;
    else daten.posten_id = gewaehlt.id;
    if (betragWert > 0) daten.betrag = Math.round(betragWert * 100) / 100;
  }
  const knopf = document.getElementById("ad-speichern");
  try {
    await bsMitBusy(knopf, "Wird gespeichert …", async () => {
      const neu = await api("POST", `/api/bestellungen/${b.id}/abloesung`, daten);
      toast(neu.status === "ABGELOEST" ? `Bestellung „${neu.kreditor}“ abgelöst.`
        : `Teilzahlung zugeordnet – Rest ${eur(neu.rest_offen)} € offen${neu.im_plan ? " (im Plan)" : ""}.`, false);
      bsDialogZu(document.getElementById("abloese-dialog"));
      await bsNachAenderung();
    });
  } catch (e) { toast(e.message); }
}

async function bsAbloesungAufheben(abloesungId, danach) {
  if (!confirm("Ablösung aufheben? Die Bestellung steht danach wieder (bzw. mit höherem Rest) im Plan.")) return;
  try {
    await api("DELETE", `/api/bestell-abloesungen/${abloesungId}`);
    toast("Ablösung aufgehoben.", false);
    await bsNachAenderung();
    if (danach) danach();
  } catch (e) { toast(e.message); }
}

document.getElementById("ad-schliessen").addEventListener("click", () => bsDialogZu(document.getElementById("abloese-dialog")));
document.getElementById("abloese-dialog").addEventListener("close", () => { bs.ad = null; });

// ------------------------------------------------------------ Tab „Zahlungsabgleich“
async function abLaden() {
  const tb = document.getElementById("ab-vorschlaege");
  const info = document.getElementById("ab-info");
  tb.innerHTML = `<tr><td colspan="8" class="hinweis">Wird geladen …</td></tr>`;
  let liste;
  try { liste = await api("GET", `/api/mandanten/${MANDANT_ID}/bestellungen/abgleich`); }
  catch (e) { tb.innerHTML = `<tr><td colspan="8" class="fehler">${esc(e.message)}</td></tr>`; info.textContent = ""; return; }
  tb.innerHTML = "";
  liste.forEach((v, i) => {
    const b = v.bestellung || {}, k = v.kandidat || {};
    const abw = Math.round(((k.betrag || 0) - adOffenerRest(b)) * 100) / 100;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${bsStatusBadge(b.status, b.status_text)} <strong>${esc(b.kreditor)}</strong>
        ${b.bestellnummer ? `<span class="klein">Nr. ${esc(b.bestellnummer)}</span>` : ""}
        <br><span class="klein">${esc(bsKontoText(b.konto))} · Zahlung erwartet ${bsDatum(b.zahldatum)}</span></td>
      <td class="zahl">${eur(b.betrag_brutto)}${b.abgeloest_betrag > 0 ? `<br><span class="klein">offen ${eur(adOffenerRest(b))}</span>` : ""}</td>
      <td><span class="klein">${esc(BS_BELEG_TEXT[k.typ] || k.typ)} · ${bsDatum(k.datum)}</span><br>${esc(k.text || "")}
        ${k.konto ? `<br><span class="klein">${esc(k.konto)}</span>` : ""}</td>
      <td class="zahl">${eur(k.betrag)}</td>
      <td class="zahl ${Math.abs(abw) > 0.004 ? (abw < 0 ? "positiv" : "negativ") : ""}">${Math.abs(abw) > 0.004 ? (abw > 0 ? "+" : "") + eur(abw) : "–"}</td>
      <td>${v.konfidenz === "SICHER" ? `<span class="status-badge freigegeben">sicher (${v.score})</span>`
        : `<span class="status-badge offen">möglich (${v.score})</span>`}</td>
      <td class="klein">${(k.gruende || []).map(esc).join(", ")}</td>
      <td class="bs-aktionen">${LESER ? "" : `<button class="klein-knopf" data-i="${i}" data-aktion="uebernehmen"
          title="${k.typ === "POSTEN" ? "Bestellung durch diese Eingangsrechnung ablösen" : "Bestellung durch diese Zahlung ablösen"}">übernehmen</button>
        <button class="sekundaer klein-knopf" data-i="${i}" data-aktion="dialog" title="Andere Zahlung, Teilzahlung oder abweichenden Betrag wählen">Ablösen …</button>`}</td>`;
    tb.appendChild(tr);
  });
  if (!liste.length) {
    tb.innerHTML = `<tr><td colspan="8" class="hinweis">Keine Vorschläge – keine passenden Zahlungen oder Rechnungen zu offenen bzw. freigegebenen Bestellungen.</td></tr>`;
  }
  info.textContent = liste.length
    ? `${liste.length} Vorschläge (${liste.filter(v => v.konfidenz === "SICHER").length} sicher).` : "";
  tb.querySelectorAll("button[data-i]").forEach(knopf => knopf.addEventListener("click", async () => {
    const v = liste[parseInt(knopf.dataset.i)];
    if (knopf.dataset.aktion === "dialog") { adOeffnen(v.bestellung.id); return; }
    const k = v.kandidat;
    const abw = Math.round(((k.betrag || 0) - adOffenerRest(v.bestellung)) * 100) / 100;
    if (Math.abs(abw) > 0.004 && !confirm(`Der Betrag weicht um ${abw > 0 ? "+" : ""}${eur(abw)} € vom offenen Bestellbetrag ab. ` +
        `Bestellung trotzdem vollständig ablösen? (Teilzahlung: „Ablösen …“)`)) return;
    const daten = { art: k.typ === "POSTEN" ? "RECHNUNG" : "ZAHLUNG", abschliessend: true };
    if (k.typ === "BUCHUNG") daten.buchung_id = k.id;
    else if (k.typ === "TRANSAKTION") daten.transaktion_id = k.id;
    else daten.posten_id = k.id;
    try {
      await bsMitBusy(knopf, "…", async () => {
        await api("POST", `/api/bestellungen/${v.bestellung.id}/abloesung`, daten);
        toast(`Bestellung „${v.bestellung.kreditor}“ abgelöst.`, false);
        await bsNachAenderung();
      });
    } catch (e) { toast(e.message); }
  }));
}

// ------------------------------------------------------------ Tab „Journal“
async function jnLaden() {
  const tb = document.getElementById("jn-tabelle");
  tb.innerHTML = `<tr><td colspan="7" class="hinweis">Wird geladen …</td></tr>`;
  try { bs.journal = await api("GET", `/api/mandanten/${MANDANT_ID}/bestellungen/journal?limit=200`); }
  catch (e) { tb.innerHTML = `<tr><td colspan="7" class="fehler">${esc(e.message)}</td></tr>`; return; }
  jnZeichnen();
}

function jnZeichnen() {
  const tb = document.getElementById("jn-tabelle");
  const aktion = document.getElementById("jn-aktion").value;
  const suche = document.getElementById("jn-suche").value.trim().toLowerCase();
  const liste = bs.journal.filter(j => (!aktion || j.aktion === aktion) && (!suche ||
    [j.bestellung_text, j.benutzer, j.begruendung, j.aktion_text].some(t => (t || "").toLowerCase().includes(suche))));
  tb.innerHTML = "";
  for (const j of liste) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="white-space:nowrap">${bsZeit(j.zeit)}</td>
      <td>${esc(j.benutzer || "–")}</td>
      <td>${esc(j.bestellung_text || "–")}${j.bestellung_id
        ? ` <button type="button" class="sekundaer klein-knopf" data-id="${j.bestellung_id}" title="Bestellung öffnen">⋯</button>`
        : (j.aktion === "GELOESCHT" ? "" : ` <span class="klein">(gelöscht)</span>`)}</td>
      <td>${bsAktionBadge(j.aktion, j.aktion_text)}</td>
      <td class="klein">${bsStatusWechsel(j)}</td>
      <td style="white-space:pre-wrap">${esc(j.begruendung || "")}</td>
      <td class="klein">${esc(bsKennzahlenKurz(j.kennzahlen))}</td>`;
    tb.appendChild(tr);
  }
  if (!liste.length) tb.innerHTML = `<tr><td colspan="7" class="hinweis">Keine Einträge.</td></tr>`;
  document.getElementById("jn-info").textContent = bs.journal.length >= 200
    ? `${liste.length} von den neuesten 200 Einträgen` : `${liste.length} Einträge`;
}

document.getElementById("jn-tabelle").addEventListener("click", (e) => {
  const knopf = e.target.closest("button[data-id]");
  if (knopf) fdOeffnen(parseInt(knopf.dataset.id));
});

// ------------------------------------------------------------ Start
(function bsStart() {
  // Status-Filter je Mandant merken (Standard „zur Freigabe“)
  const filter = document.getElementById("filter-status");
  try {
    const gemerkt = localStorage.getItem(BS_FILTER_SCHLUESSEL);
    if (gemerkt !== null && [...filter.options].some(o => o.value === gemerkt)) filter.value = gemerkt;
  } catch (e) { /* ohne Speicher: Standard */ }
  filter.addEventListener("change", () => {
    try { localStorage.setItem(BS_FILTER_SCHLUESSEL, filter.value); } catch (e) { /* egal */ }
    bsListeLaden();
  });
  let suchTimer = null;
  document.getElementById("filter-suche").addEventListener("input", () => {
    clearTimeout(suchTimer);
    suchTimer = setTimeout(bsListeLaden, 300);
  });

  for (const tab of ["liste", "freigabe", "abgleich", "journal"]) {
    document.getElementById("tab-" + tab).addEventListener("click", () => tabWechsel(tab));
  }

  if (!LESER) {
    const eingabe = document.getElementById("upload-datei");
    document.getElementById("neu-upload").addEventListener("click", () => eingabe.click());
    eingabe.addEventListener("change", () => {
      const datei = eingabe.files[0];
      eingabe.value = "";                 // dieselbe Datei erneut wählbar
      bsUploadStarten(datei);
    });
    document.getElementById("neu-ablage").addEventListener("click", bsAusAblage);
    document.getElementById("neu-manuell").addEventListener("click", () => efOeffnen({ modus: "neu" }));
    // Drag & Drop auf die Liste
    const bereich = document.getElementById("bereich-liste");
    bereich.addEventListener("dragover", (e) => {
      if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes("Files")) return;
      e.preventDefault();
      bereich.classList.add("ziehen-ueber");
    });
    bereich.addEventListener("dragleave", (e) => { if (e.target === bereich) bereich.classList.remove("ziehen-ueber"); });
    bereich.addEventListener("drop", (e) => {
      bereich.classList.remove("ziehen-ueber");
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      e.preventDefault();
      bsUploadStarten(e.dataTransfer.files[0]);
    });
  }

  // Freigabe-Tab: laufende KW … +13 (vergangene KWs lassen sich nicht mehr freigeben), Basis aus den Planständen
  bsKwOptionen(document.getElementById("fg-kw"), { vonWochen: 0, bisWochen: 13 });
  fgKnoepfeSetzen();
  document.getElementById("fg-kw").addEventListener("change", fgLaden);
  document.getElementById("fg-zurueck").addEventListener("click", () => fgKwSchritt(-1));
  document.getElementById("fg-vor").addEventListener("click", () => fgKwSchritt(1));
  document.getElementById("fg-basis").addEventListener("change", fgLaden);
  document.getElementById("fg-laden").addEventListener("click", fgLaden);
  bsSnapshotsLaden().then(() => bsBasisOptionen(document.getElementById("fg-basis")));

  document.getElementById("ab-laden").addEventListener("click", abLaden);
  document.getElementById("jn-aktion").innerHTML = `<option value="">alle</option>` +
    Object.entries(BS_AKTION_TEXT).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join("");
  document.getElementById("jn-aktion").addEventListener("change", jnZeichnen);
  document.getElementById("jn-suche").addEventListener("input", jnZeichnen);
  document.getElementById("jn-laden").addEventListener("click", jnLaden);

  // Direktaufruf: ?bestellung=<id> öffnet die Details, ?tab=freigabe|abgleich|journal
  const params = new URLSearchParams(location.search);
  bsKontenLaden().catch(e => toast(e.message)).then(bsListeLaden).then(() => {
    const tab = params.get("tab");
    if (tab && ["freigabe", "abgleich", "journal"].includes(tab)) tabWechsel(tab);
    const id = parseInt(params.get("bestellung"));
    if (id) fdOeffnen(id);
  });
})();
