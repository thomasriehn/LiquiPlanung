"use strict";

let daten = null;
const zu = new Set(); // eingeklappte Gruppen

async function ladeSzenarien() {
  try {
    const szenarien = await api("GET", `/api/mandanten/${MANDANT_ID}/szenarien`);
    const sel = document.getElementById("szenario");
    sel.innerHTML = `<option value="">Basisplan</option>` + szenarien.map(s =>
      `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  } catch (e) { /* Szenarien sind optional */ }
}

async function lade() {
  const start = document.getElementById("start").value;
  startMerken(MANDANT_ID, start);
  const wochen = document.getElementById("wochen").value || 13;
  const szenarioId = document.getElementById("szenario").value;
  let url = `/api/mandanten/${MANDANT_ID}/plan?wochen=${wochen}`;
  if (start) url += `&start=${start}`;
  if (szenarioId) url += `&szenario_id=${szenarioId}`;
  try {
    daten = await api("GET", url);
  } catch (e) { toast(e.message); return; }
  if (!document.getElementById("start").value) document.getElementById("start").value = daten.start;
  const info = document.getElementById("basis-info");
  let infoText = daten.vergleichsbasis === "SNAPSHOT"
    ? `Soll-Basis: eingefrorener Plan vom ${datumVoll(daten.snapshot.stichtag)}`
    : "Soll-Basis: aktueller Plan (noch kein Snapshot eingefroren)";
  if (daten.vergleichsbasis === "SNAPSHOT" && daten.snapshot.referenz) {
    infoText = `Soll-Referenz: eingefrorener Plan vom ${datumVoll(daten.snapshot.stichtag)}`;
  }
  if (daten.szenario) {
    const s = daten.szenario;
    const teile = [`Einzahlungen ${s.ein_faktor} %`, `variable Auszahlungen ${s.aus_faktor} %`];
    if (s.debitoren_verzoegerung_tage) teile.push(`Debitoren +${s.debitoren_verzoegerung_tage} Tage`);
    infoText = `Szenario „${s.name}“ (${teile.join(", ")}) · ` + infoText;
  }
  info.textContent = infoText;
  exportLinks();
  const warnBox = document.getElementById("warnungen");
  warnBox.innerHTML = "";
  if (daten.unbekannte_konten.length) {
    const div = document.createElement("div");
    div.className = "warnung";
    div.textContent = "Buchungen auf nicht angelegten Konten: " +
      daten.unbekannte_konten.join(", ") + " – bitte unter „Konten“ anlegen.";
    warnBox.appendChild(div);
  }
  if (daten.gesperrte_insolvenzforderungen > 0) {
    const div = document.createElement("div");
    div.className = "warnung";
    div.textContent = "Zahlungsgesperrte Insolvenzforderungen (§ 38 InsO), nicht im Plan enthalten: " +
      eur(daten.gesperrte_insolvenzforderungen) + " €";
    warnBox.appendChild(div);
  }
  if (daten.insolvenzgeld) {
    const ig = daten.insolvenzgeld;
    const div = document.createElement("div");
    div.className = "warnung gruen";
    div.textContent = `Insolvenzgeldzeitraum ${datumVoll(ig.von)} – ${datumVoll(ig.bis)}: ` +
      `Personal-, SV- und LSt-Zahlungen entlastet (im Fenster: ${eur(ig.entlastung_fenster)} €).`;
    warnBox.appendChild(div);
  }
  zeichne();
}

// Export übernimmt Fenster, Szenario, Spaltenansicht und Werteauswahl
function exportLinks() {
  const start = document.getElementById("start").value;
  const szenarioId = document.getElementById("szenario").value;
  let q = `?wochen=${document.getElementById("wochen").value || 13}` + (start ? `&start=${start}` : "");
  if (szenarioId) q += `&szenario_id=${szenarioId}`;
  q += `&metrik=${document.getElementById("metrik").value}`;
  document.getElementById("export-xlsx").href = `/api/mandanten/${MANDANT_ID}/export/plan.xlsx${q}`;
  document.getElementById("export-pdf").href =
    `/api/mandanten/${MANDANT_ID}/export/plan.pdf${q}&ansicht=${document.getElementById("ansicht").value}`;
}

function vortragsHinweis() {
  const box = document.getElementById("vortrag-hinweis");
  const vt = daten.vortraege || [];
  if (document.getElementById("metrik").value !== "vortrag") { box.innerHTML = ""; return; }
  if (!vt.length) {
    box.innerHTML = `<div class="warnung gruen">Keine offenen Zahlungen aus Dauerschuldverhältnissen
      vorzutragen – alle fälligen Raten im Fenster sind gebucht.</div>`;
    return;
  }
  box.innerHTML = `<div class="warnung">Auf den ${datumVoll(vt[0].ziel)} vorgetragen
    (nicht oder nur teilweise gebuchte Dauerschuldverhältnisse):
    <ul class="vortrag-liste">${vt.map(v => `<li>${esc(v.name)} (${esc(v.konto)}), fällig
      ${datumVoll(v.faellig)}: ${eur(Math.abs(v.betrag))} € offen` +
      (v.gezahlt ? ` – ${eur(v.gezahlt)} € von ${eur(Math.abs(v.soll))} € bereits gezahlt` : "") +
      `</li>`).join("")}</ul>` +
    ((daten.vortrag_verschoben || []).length ? `Davon manuell verschoben:
    <ul class="vortrag-liste">${daten.vortrag_verschoben.map(v => `<li>${eur(Math.abs(v.betrag))} €
      (${esc(v.konto)}) auf den ${datumVoll(v.nach)}</li>`).join("")}</ul>` : "") + `</div>`;
}

function datumVoll(iso) { const [j, m, t] = iso.split("-"); return `${t}.${m}.${j}`; }

function wert(zeile, tag) {
  const metrik = document.getElementById("metrik").value;
  const plan = zeile.plan[tag] || 0, ist = zeile.ist[tag] || 0, basis = zeile.basis[tag] || 0;
  const vergangen = tag <= daten.heute;
  if (metrik === "plan") return { v: plan, ist: false };
  if (metrik === "ist") return { v: vergangen ? ist : null, ist: true };
  if (metrik === "delta") return { v: vergangen ? ist - basis : null, ist: false, delta: true };
  if (metrik === "vortrag" && !vergangen) {
    const vortrag = (zeile.vortrag && zeile.vortrag[tag]) || 0;
    return { v: plan + vortrag, ist: false, vortrag: vortrag !== 0, gemischt: vortrag !== 0 && plan !== 0 };
  }
  return vergangen ? { v: ist, ist: true } : { v: plan, ist: false };
}

function summiere(zeilen) {
  // Aggregat über Kinder: plan/ist/basis je Tag addieren
  const agg = { plan: {}, ist: {}, basis: {}, vortrag: {} };
  for (const z of zeilen) {
    for (const feld of ["plan", "ist", "basis", "vortrag"]) {
      for (const [tag, v] of Object.entries(z[feld] || {})) {
        agg[feld][tag] = (agg[feld][tag] || 0) + v;
      }
    }
  }
  return agg;
}

const MONATSNAMEN = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
                     "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];

function spalten() {
  const ansicht = document.getElementById("ansicht").value;
  if (ansicht === "tage") {
    return daten.tage.map(t => ({ id: t, tage: [t] }));
  }
  if (ansicht === "monate") {
    const monate = [];
    let aktuell = null;
    for (const t of daten.tage) {
      const schluessel = t.slice(0, 7);                     // JJJJ-MM
      if (!aktuell || aktuell.id !== schluessel) {
        aktuell = {
          id: schluessel,
          label: `${MONATSNAMEN[parseInt(t.slice(5, 7)) - 1]} ${t.slice(0, 4)}`,
          tage: [],
        };
        monate.push(aktuell);
      }
      aktuell.tage.push(t);
    }
    return monate;
  }
  return daten.wochen.map(w => ({ id: w.label + w.jahr, label: w.label, tage: w.tage.filter(t => daten.tage.includes(t)) }));
}

function zellWert(zeile, sp) {
  let summe = 0, istTeil = false, alleNull = true, deltaMod = false, vortrag = false, gemischt = false;
  for (const t of sp.tage) {
    const w = wert(zeile, t);
    if (w.delta) deltaMod = true;
    if (w.v !== null && w.v !== 0) { summe += w.v; alleNull = false; }
    if (w.ist && (zeile.ist[t] || 0) !== 0) istTeil = true;
    if (w.vortrag) vortrag = true;
    if (w.gemischt) gemischt = true;
  }
  return { summe, istTeil, leer: alleNull, delta: deltaMod, vortrag, gemischt };
}

function td(inhalt, klassen = []) {
  const zelle = document.createElement("td");
  zelle.innerHTML = inhalt;
  for (const k of klassen) if (k) zelle.classList.add(k);
  return zelle;
}

function tagKlassen(sp) {
  const kl = [];
  if (sp.tage.length === 1) {
    const d = new Date(sp.tage[0] + "T12:00:00").getDay();
    if (d === 0 || d === 6) kl.push("we");
    if (sp.tage[0] === daten.heute) kl.push("heute-spalte");
  } else if (sp.tage.includes(daten.heute)) {
    kl.push("heute-spalte");
  }
  return kl;
}

// Tage einer Spalte mit manuell verschobenen Beträgen (nur dort, wo die
// gewählte Werteansicht Planwerte zeigt)
function verschobeneTage(zeile, sp) {
  const metrik = document.getElementById("metrik").value;
  if (metrik === "ist" || metrik === "delta") return [];
  const plan = zeile.verschoben || {};
  const vortrag = metrik === "vortrag" ? (zeile.verschoben_vortrag || {}) : {};
  const ergebnis = [];
  for (const t of sp.tage) {
    if (metrik !== "plan" && t <= daten.heute) continue;
    const v = (plan[t] || 0) + (vortrag[t] || 0);
    if (Math.abs(v) > 0.004) ergebnis.push({ tag: t, betrag: v });
  }
  return ergebnis;
}

function datenZeile(zeile, sps, cssKlasse, einrueckung = "") {
  const tr = document.createElement("tr");
  if (cssKlasse) tr.className = cssKlasse;
  const kopfText = zeile.nummer
    ? `${einrueckung}<span class="konto-nr">${esc(zeile.nummer)}</span>${esc(zeile.name)}`
    : `${einrueckung}${esc(zeile.name)}`;
  tr.appendChild(td(kopfText, ["zeilkopf"]));
  const kontoZeile = !cssKlasse && zeile.key;
  for (const sp of sps) {
    const z = zellWert(zeile, sp);
    const klassen = tagKlassen(sp);
    if (!z.leer) {
      if (z.delta) klassen.push(z.summe < -0.005 ? "negativ" : (z.summe > 0.005 ? "positiv" : ""));
      else if (z.summe < 0 && cssKlasse === "summe") klassen.push("negativ");
      if (z.istTeil) klassen.push("ist-wert");
      // Schraffur nur auf Kontozeilen: Vortrag trifft auf Planbuchung desselben Kontos
      if (z.vortrag) klassen.push(z.gemischt && !cssKlasse ? "vortrag-schraffiert" : "vortrag");
    }
    const zelle = td(z.leer ? "" : eur(z.summe), klassen);
    if (kontoZeile) {
      zelle.classList.add("klickbar");
      const verschoben = verschobeneTage(zeile, sp);
      if (verschoben.length) {
        zelle.classList.add("verschoben");
        zelle.title = "Manuell verschoben: " + verschoben.map(v =>
          `${datumVoll(v.tag)} ${v.betrag > 0 ? "+" : ""}${eur(v.betrag)} €`).join(", ");
      }
      zelle.addEventListener("click", () => zellenDialog(zeile.key, sp));
    }
    tr.appendChild(zelle);
  }
  return tr;
}

function bestandsZeile(name, werteMap, sps, extraKlasse = "", linie = null) {
  const tr = document.createElement("tr");
  tr.className = "bestand " + extraKlasse;
  tr.appendChild(td(name + (linie ? ` <span class="klein">(Linie ${eur(linie)})</span>` : ""), ["zeilkopf"]));
  for (const sp of sps) {
    const letzterTag = sp.tage[sp.tage.length - 1];
    const wert = werteMap[letzterTag];
    const klassen = tagKlassen(sp);
    if (wert !== null && wert !== undefined && wert < 0) klassen.push("negativ");
    tr.appendChild(td(wert === null || wert === undefined ? "" : eur(wert), klassen));
  }
  return tr;
}

function zeichne() {
  const tabelle = document.getElementById("matrix");
  tabelle.innerHTML = "";
  const sps = spalten();
  const ansicht = document.getElementById("ansicht").value;

  // Kopf
  const th = (inhalt, klassen = []) => {
    const el = document.createElement("th");
    el.innerHTML = inhalt;
    for (const k of klassen) if (k) el.classList.add(k);
    return el;
  };
  const kopf = document.createElement("thead");
  if (ansicht === "tage") {
    const zeile1 = document.createElement("tr");
    zeile1.appendChild(th("Konto / BWA-Zeile", ["zeilkopf"]));
    for (const w of daten.wochen) {
      const el = th(`${w.label} (${datumDe(w.von)}–${datumDe(w.bis)})`);
      el.colSpan = w.tage.filter(t => daten.tage.includes(t)).length;
      zeile1.appendChild(el);
    }
    const zeile2 = document.createElement("tr");
    zeile2.appendChild(th("", ["zeilkopf"]));
    for (const sp of sps) {
      zeile2.appendChild(th(`${wochentagKurz(sp.tage[0])}<br>${datumDe(sp.tage[0])}`, tagKlassen(sp)));
    }
    kopf.appendChild(zeile1);
    kopf.appendChild(zeile2);
  } else {
    const zeile = document.createElement("tr");
    zeile.appendChild(th("Konto / BWA-Zeile", ["zeilkopf"]));
    for (const sp of sps) {
      zeile.appendChild(th(
        `${sp.label}<br><span class="klein">${datumDe(sp.tage[0])}–${datumDe(sp.tage[sp.tage.length-1])}</span>`,
        tagKlassen(sp)));
    }
    kopf.appendChild(zeile);
  }
  tabelle.appendChild(kopf);

  const koerper = document.createElement("tbody");

  // Gruppen mit Konten
  for (const gruppe of daten.zeilen) {
    const agg = summiere(gruppe.kinder);
    const aggZeile = { name: gruppe.name, plan: agg.plan, ist: agg.ist, basis: agg.basis, vortrag: agg.vortrag };
    const tr = datenZeile(aggZeile, sps, "gruppe");
    if (zu.has(gruppe.key)) tr.classList.add("zu");
    tr.addEventListener("click", () => {
      if (zu.has(gruppe.key)) zu.delete(gruppe.key); else zu.add(gruppe.key);
      zeichne();
    });
    koerper.appendChild(tr);
    if (!zu.has(gruppe.key)) {
      for (const kind of gruppe.kinder) {
        koerper.appendChild(datenZeile(kind, sps, "", "&nbsp;&nbsp;&nbsp;"));
      }
    }
  }

  // Summen
  const s = daten.summen;
  koerper.appendChild(datenZeile({ name: "Einzahlungen gesamt", ...s.einzahlungen }, sps, "summe"));
  koerper.appendChild(datenZeile({ name: "Auszahlungen gesamt", ...s.auszahlungen }, sps, "summe"));
  koerper.appendChild(datenZeile({ name: "Netto-Cashflow", ...s.netto }, sps, "summe"));

  // Bestände
  const b = daten.bestaende;
  const mitVortrag = document.getElementById("metrik").value === "vortrag" && b.liquiditaet_vortrag;
  const liquiditaet = mitVortrag ? b.liquiditaet_vortrag : b.liquiditaet;
  const verfuegbar = mitVortrag ? b.verfuegbar_vortrag : b.verfuegbar;
  for (const fk of b.finanzkonten) {
    koerper.appendChild(bestandsZeile(
      `<span class="konto-nr">${esc(fk.nummer)}</span>${esc(fk.name)}`,
      fk.bestand, sps, "", fk.kreditlinie > 0 ? fk.kreditlinie : null));
  }
  koerper.appendChild(bestandsZeile("Liquidität (Bank + Kasse)", liquiditaet, sps, "gesamt"));
  if (b.kreditlinien > 0) {
    koerper.appendChild(bestandsZeile("Verfügbar (inkl. Kreditlinien)", verfuegbar, sps, "gesamt"));
  }
  koerper.appendChild(bestandsZeile("Warenbestand (nachrichtlich)", b.waren, sps));

  tabelle.appendChild(koerper);
  vortragsHinweis();
}

// ------------------------------------------------------------ Zellendialog
// Klick auf eine Kontozelle: Ist-Buchungen (Tage bis heute) mit Umbuchen des
// Sachkontos sowie Verschieben von geplanten Werten/Vorträgen (Tage ab morgen)

let kontoGruppenCache = null;
let zdZustand = null;          // { key, sp, geaendert }

async function kontoGruppenLaden() {
  if (!kontoGruppenCache) {
    kontoGruppenCache = kontoGruppenAus(await api("GET", `/api/mandanten/${MANDANT_ID}/konten`));
  }
  return kontoGruppenCache;
}

function zeileZumKey(key) {
  for (const g of daten.zeilen) for (const k of g.kinder) if (k.key === key) return k;
  return null;
}

// isoTag, tagPlus, montagVon, kalenderwoche: gemeinsam in app.js

function zeitraumText(sp) {
  const ansicht = document.getElementById("ansicht").value;
  const von = sp.tage[0], bis = sp.tage[sp.tage.length - 1];
  if (sp.tage.length === 1) return `${wochentagKurz(von)}, ${datumVoll(von)}`;
  const label = ansicht === "monate" ? sp.label : `${sp.label}`;
  return `${label} (${datumVoll(von)} – ${datumVoll(bis)})`;
}

function zdOeffnen() {
  const dlg = document.getElementById("zellen-dialog");
  if (dlg.open) return;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");          // Browser ohne <dialog>-Unterstützung
}

function zdSchliessen() {
  const dlg = document.getElementById("zellen-dialog");
  if (typeof dlg.close === "function") dlg.close(); else dlg.removeAttribute("open");
}

document.getElementById("zd-schliessen").addEventListener("click", zdSchliessen);
document.getElementById("zellen-dialog").addEventListener("close", () => {
  if (zdZustand && zdZustand.geaendert) lade();
  zdZustand = null;
});

async function zellenDialog(key, sp) {
  zdZustand = { key, sp, geaendert: false };
  await zdZeichnen();
  zdOeffnen();
}

async function zdZeichnen() {
  const { key, sp } = zdZustand;
  const zeile = zeileZumKey(key);
  const inhalt = document.getElementById("zd-inhalt");
  document.getElementById("zd-titel").textContent =
    zeile ? `${zeile.nummer ? zeile.nummer + " " : ""}${zeile.name}` : "Planzeile";
  document.getElementById("zd-zeitraum").textContent = zeitraumText(sp);
  inhalt.innerHTML = "";
  if (!zeile) { inhalt.innerHTML = `<p class="hinweis">Die Zeile ist im aktuellen Plan nicht mehr enthalten.</p>`; return; }
  const istTage = sp.tage.filter(t => t <= daten.heute);
  const zukunft = sp.tage.filter(t => t > daten.heute);
  if (istTage.length) inhalt.appendChild(await istAbschnitt(zeile, istTage));
  if (zukunft.length) inhalt.appendChild(verschiebeAbschnitt(zeile, sp, zukunft));
  if (/^\d+$/.test(key) || key.startsWith("TERMIN:")) {
    inhalt.appendChild(await verschiebungsListe(zeile));
  }
  if (/^\d+$/.test(key)) inhalt.appendChild(await bestellAbschnitt(zeile, sp));
}

// ---- Bestellungen, die im Zeitraum auf diesem Konto im Plan stehen ----
async function bestellAbschnitt(zeile, sp) {
  const box = document.createElement("div");
  box.className = "zd-abschnitt";
  let liste = [];
  try {
    liste = await api("GET", `/api/mandanten/${MANDANT_ID}/plan/bestellungen?konto_id=${encodeURIComponent(zeile.key)}` +
      `&von=${sp.tage[0]}&bis=${sp.tage[sp.tage.length - 1]}`);
  } catch (e) { return box; }                 // Abschnitt ist optional
  if (!liste || !liste.length) return box;
  const STATUS = { OFFEN: ["offen", "zur Freigabe"], FREIGEGEBEN: ["freigegeben", "freigegeben"],
                   ABGELOEST: ["abgeloest", "abgelöst"] };
  // Betrag wie im Plan (plan_betrag); abgelöste stehen mit vollem Brutto im Plan, rest_geplant ist dort 0
  const planBetrag = (b) => Math.abs(b.plan_betrag
    ?? (b.status === "ABGELOEST" ? b.betrag_brutto : b.rest_geplant) ?? b.betrag_brutto ?? 0);
  const summe = liste.reduce((s, b) => s + planBetrag(b), 0);
  box.innerHTML = `<h4>Bestellungen <span class="hinweis">(brutto, ${eur(summe)} €)</span></h4><ul class="zd-liste"></ul>`;
  const ul = box.querySelector("ul");
  for (const b of liste) {
    const [klasse, text] = STATUS[b.status] || ["neutral", b.status_text || b.status || ""];
    const tag = b.plan_tag || b.zahldatum;
    let zusatz = "";
    if (b.gerollt && b.zahldatum) {
      zusatz = `fällig ${datumVoll(b.zahldatum)} (überfällig, vorgetragen)`;
    } else if (b.status === "ABGELOEST" && b.abgeloest_am) {
      zusatz = `abgelöst am ${datumVoll(b.abgeloest_am)}` +
        (b.abgeloest_betrag !== null && b.abgeloest_betrag !== undefined ? `, gezahlt ${eur(b.abgeloest_betrag)} €` : "");
    }
    const li = document.createElement("li");
    li.innerHTML = `<span>${tag ? datumVoll(tag) : ""}</span>
      <span><strong>${esc(b.kreditor || "")}</strong>${b.bestellnummer ? ` <span class="klein">Nr. ${esc(b.bestellnummer)}</span>` : ""}</span>
      <span class="zahl">${eur(planBetrag(b))} €</span>
      <span class="status-badge ${klasse}">${esc(b.status_text || text)}</span>
      ${zusatz ? `<span class="hinweis">${zusatz}</span>` : ""}
      <a href="/mandanten/${MANDANT_ID}/bestellungen?bestellung=${encodeURIComponent(b.id)}" style="margin-left:auto">öffnen</a>`;
    ul.appendChild(li);
  }
  return box;
}

// ---- Ist-Buchungen ----
async function istAbschnitt(zeile, istTage) {
  const box = document.createElement("div");
  box.className = "zd-abschnitt";
  const von = istTage[0], bis = istTage[istTage.length - 1];
  box.innerHTML = `<h4>Ist-Buchungen ${von === bis ? "am " + datumVoll(von) : datumVoll(von) + " – " + datumVoll(bis)}</h4>`;
  if (zeile.key.startsWith("TERMIN:")) {
    box.insertAdjacentHTML("beforeend", `<p class="hinweis">Termin-/Postenzeile ohne Sachkonto –
      Ist-Buchungen erscheinen auf den jeweiligen Konten.</p>`);
    return box;
  }
  let buchungen;
  try {
    buchungen = await api("GET", `/api/mandanten/${MANDANT_ID}/plan/ist-buchungen?key=${encodeURIComponent(zeile.key)}&von=${von}&bis=${bis}`);
  } catch (e) { box.insertAdjacentHTML("beforeend", `<p class="hinweis">${esc(e.message)}</p>`); return box; }
  if (!buchungen.length) {
    box.insertAdjacentHTML("beforeend", `<p class="hinweis">Keine Ist-Buchungen in diesem Zeitraum.</p>`);
    return box;
  }
  const summe = buchungen.reduce((s, b) => s + b.fluss, 0);
  const tabelle = document.createElement("table");
  tabelle.className = "liste";
  tabelle.innerHTML = `<thead><tr><th>Datum</th><th>Buchungstext</th><th>Beleg</th>
    <th>Zahlungsmittel</th><th class="zahl">Betrag</th><th>Sachkonto</th></tr></thead><tbody></tbody>
    <tfoot><tr><td colspan="4"><strong>Summe</strong></td>
    <td class="zahl"><strong>${eur(summe)}</strong></td><td></td></tr></tfoot>`;
  const tb = tabelle.querySelector("tbody");
  for (const b of buchungen) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${datumVoll(b.datum)}</td>
      <td class="klein">${esc(b.text || "")}${b.posten_id ? ` <span class="sammel-vermerk">Posten-Zahlung</span>` : ""}
        ${b.import ? `<div class="hinweis">${esc(b.import)}</div>` : ""}</td>
      <td class="klein">${esc(b.belegfeld || "")}</td>
      <td class="klein">${esc(b.finanzkonto)}</td>
      <td class="zahl ${b.fluss < 0 ? "negativ" : ""}">${eur(b.fluss)}</td><td></td>`;
    kontoZelle(tr.lastElementChild, b);
    tb.appendChild(tr);
  }
  box.appendChild(tabelle);
  box.insertAdjacentHTML("beforeend", LESER ? "" : `<p class="hinweis">✕ löst die Kontoverknüpfung
    einer Buchung; danach das neue Sachkonto auswählen – die Buchung wird umgebucht
    (Bank-/Kassenseite bleibt unverändert).</p>`);
  return box;
}

// Badge mit dem aktuellen Sachkonto; ✕ öffnet die Kontoauswahl
function kontoZelle(td, buchung) {
  td.innerHTML = `<span class="zd-konto"><span class="badge-konto">${esc(buchung.sachkonto)}</span>
    ${LESER ? "" : `<button type="button" class="loesen" title="Kontoverknüpfung lösen und neu festlegen">✕</button>`}</span>`;
  const knopf = td.querySelector(".loesen");
  if (!knopf) return;
  knopf.addEventListener("click", async () => {
    let gruppen;
    try { gruppen = await kontoGruppenLaden(); } catch (e) { toast(e.message); return; }
    td.innerHTML = "";
    const auswahl = kontoAuswahl({
      gruppen,
      beiAuswahl: async (konto) => {
        if (!konto) return;
        try {
          const erg = await api("POST", `/api/buchungen/${buchung.id}/sachkonto`, { konto_id: konto.id });
          buchung.sachkonto = erg.konto;
          zdZustand.geaendert = true;
          kontoZelle(td, buchung);
          td.querySelector(".badge-konto").style.background = "#fdf3d7";
          toast(`Buchung auf ${erg.konto} umgebucht.`, false);
        } catch (e) { toast(e.message); }
      },
    });
    const abbrechen = document.createElement("button");
    abbrechen.type = "button";
    abbrechen.className = "sekundaer klein-knopf";
    abbrechen.textContent = "Abbrechen";
    abbrechen.style.marginLeft = "6px";
    abbrechen.addEventListener("click", () => kontoZelle(td, buchung));
    td.append(auswahl, abbrechen);
    const eingabe = auswahl.querySelector("input");
    if (eingabe) eingabe.focus();
  });
}

// ---- Verschieben ----
function zielOptionen(sp) {
  const ansicht = document.getElementById("ansicht").value;
  const morgen = tagPlus(daten.heute, 1);
  const ende = tagPlus(daten.ende, 7 * 13);
  const optionen = [];
  if (ansicht === "wochen") {
    for (let m = montagVon(morgen); m <= ende; m = tagPlus(m, 7)) {
      if (sp.tage.includes(m)) continue;
      optionen.push({ wert: m, text: `KW ${kalenderwoche(m)} (${datumDe(m)}–${datumVoll(tagPlus(m, 6))})` });
    }
  } else if (ansicht === "monate") {
    let [j, mo] = morgen.split("-").map(Number);
    for (let i = 0; i < 12; i++) {
      const wert = `${j}-${String(mo).padStart(2, "0")}-01`;
      if (!sp.tage.some(t => t.slice(0, 7) === wert.slice(0, 7))) {
        optionen.push({ wert, text: `${MONATSNAMEN[mo - 1]} ${j}` });
      }
      mo += 1; if (mo > 12) { mo = 1; j += 1; }
    }
  }
  return optionen;
}

function summeTage(werte, tage) {
  let netto = 0;
  for (const t of tage) netto += (werte && werte[t]) || 0;
  return netto;
}
function verfuegbarTage(werte, tage, netto) {
  let s = 0;
  for (const t of tage) { const w = (werte && werte[t]) || 0; if (w * netto > 0) s += Math.abs(w); }
  return Math.round(s * 100) / 100;
}

function verschiebeAbschnitt(zeile, sp, zukunft) {
  const box = document.createElement("div");
  box.className = "zd-abschnitt";
  const ansicht = document.getElementById("ansicht").value;
  const mitVortrag = document.getElementById("metrik").value === "vortrag";
  const planNetto = summeTage(zeile.plan, zukunft);
  const vortragNetto = mitVortrag ? summeTage(zeile.vortrag, zukunft) : 0;
  const quellen = [];
  if (Math.abs(planNetto) > 0.004) quellen.push({ wert: "PLAN", text: "Geplanter Wert", netto: planNetto,
    max: verfuegbarTage(zeile.plan, zukunft, planNetto) });
  if (Math.abs(vortragNetto) > 0.004) quellen.push({ wert: "VORTRAG", text: "Vortrag", netto: vortragNetto,
    max: verfuegbarTage(zeile.vortrag, zukunft, vortragNetto) });
  const teil = zukunft.length < sp.tage.length ? ` ab ${datumVoll(zukunft[0])}` : "";
  box.innerHTML = `<h4>Verschieben${teil}</h4>`;
  if (!zeile.key.match(/^\d+$/) && !zeile.key.startsWith("TERMIN:")) {
    box.insertAdjacentHTML("beforeend", `<p class="hinweis">Diese Zeile hat kein angelegtes Konto.</p>`);
    return box;
  }
  if (!quellen.length) {
    box.insertAdjacentHTML("beforeend", `<p class="hinweis">Kein geplanter Wert${mitVortrag ? " oder Vortrag" : ""}
      in diesem Zeitraum.${!mitVortrag && Math.abs(summeTage(zeile.vortrag, zukunft)) > 0.004
        ? " Vorträge lassen sich in der Ansicht „mit Vortrag“ verschieben." : ""}</p>`);
    return box;
  }
  if (LESER) {
    box.insertAdjacentHTML("beforeend", `<p class="hinweis">${quellen.map(q =>
      `${q.text}: ${eur(q.netto)} €`).join(" · ")} – Verschieben nur mit Schreibrecht.</p>`);
    return box;
  }
  const morgen = tagPlus(daten.heute, 1);
  let zielFeld;
  if (ansicht === "tage") {
    const vorschlag = tagPlus(sp.tage[0], 7) > morgen ? tagPlus(sp.tage[0], 7) : tagPlus(morgen, 7);
    zielFeld = `<label>Zieltag<input type="date" id="zd-nach" min="${morgen}" value="${vorschlag}" required></label>`;
  } else {
    const optionen = zielOptionen(sp);
    const vorschlag = optionen.find(o => o.wert > sp.tage[0]) || optionen[0];
    zielFeld = `<label>${ansicht === "wochen" ? "Zielwoche" : "Zielmonat"}
      <select id="zd-nach">${optionen.map(o => `<option value="${o.wert}"${o === vorschlag ? " selected" : ""}>${esc(o.text)}</option>`).join("")}</select></label>`;
  }
  const quellText = quellen.length > 1
    ? `<div class="quellen">${quellen.map((q, i) => `<label><input type="radio" name="zd-quelle" value="${q.wert}"${i === 0 ? " checked" : ""}>
        ${q.text} (${eur(q.netto)} €)</label>`).join("")}</div>`
    : `<div class="hinweis">${quellen[0].text}: <strong>${eur(quellen[0].netto)} €</strong></div>`;
  box.insertAdjacentHTML("beforeend", `
    ${quellText}
    <form class="zd-formular" id="zd-verschieben" style="margin-top:8px">
      <label>Betrag (€)<input type="number" id="zd-betrag" step="0.01" min="0.01" style="width:130px" required></label>
      ${zielFeld}
      <label>Kommentar<input type="text" id="zd-kommentar" maxlength="255" placeholder="z. B. Stundung vereinbart" style="width:220px"></label>
      <button type="submit">Verschieben</button>
    </form>
    <p class="hinweis" id="zd-rest"></p>`);
  const form = box.querySelector("#zd-verschieben");
  const betragFeld = box.querySelector("#zd-betrag");
  const restText = box.querySelector("#zd-rest");
  const quelle = () => {
    const r = box.querySelector('input[name="zd-quelle"]:checked');
    return quellen.find(q => q.wert === (r ? r.value : quellen[0].wert));
  };
  const zielHinweis = ansicht === "tage" ? "auf den gewählten Tag (bzw. nächsten Bankarbeitstag)"
    : `auf den ersten Bankarbeitstag der ${ansicht === "wochen" ? "Woche" : "Monats"}`;
  const restZeigen = () => {
    const q = quelle();
    const b = parseFloat(betragFeld.value) || 0;
    const rest = Math.max(0, q.max - b);
    restText.textContent = `Verschoben wird ${zielHinweis}. ` +
      (rest > 0.004 ? `In der Zelle verbleiben ${eur(rest)} €.` : "Der gesamte Betrag wird verschoben.");
  };
  const quelleSetzen = () => {
    const q = quelle();
    betragFeld.max = q.max;
    betragFeld.value = q.max.toFixed(2);       // Standard: Gesamtbetrag
    restZeigen();
  };
  box.querySelectorAll('input[name="zd-quelle"]').forEach(r => r.addEventListener("change", quelleSetzen));
  betragFeld.addEventListener("input", restZeigen);
  quelleSetzen();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = quelle();
    const nach = box.querySelector("#zd-nach").value;
    if (!nach) { toast("Bitte ein Ziel wählen."); return; }
    try {
      const erg = await api("POST", `/api/mandanten/${MANDANT_ID}/plan/verschiebungen`, {
        key: zeile.key, quelle: q.wert, von: sp.tage[0], bis: sp.tage[sp.tage.length - 1],
        nach, ansicht, betrag: parseFloat(betragFeld.value),
        start: daten.start, wochen: parseInt(document.getElementById("wochen").value) || 13,
        szenario_id: document.getElementById("szenario").value ? parseInt(document.getElementById("szenario").value) : null,
        kommentar: box.querySelector("#zd-kommentar").value || null,
      });
      toast(`${eur(erg.betrag)} € auf den ${datumVoll(erg.ziel)} verschoben.`, false);
      zdZustand.geaendert = false;
      await lade();
      await zdZeichnen();
    } catch (err) { toast(err.message); }
  });
  return box;
}

async function verschiebungsListe(zeile) {
  const box = document.createElement("div");
  box.className = "zd-abschnitt";
  let liste = [];
  try {
    liste = await api("GET", `/api/mandanten/${MANDANT_ID}/plan/verschiebungen?key=${encodeURIComponent(zeile.key)}`);
  } catch (e) { /* Liste ist optional */ }
  if (!liste.length) return box;
  box.innerHTML = `<h4>Verschiebungen dieser Zeile</h4><ul class="zd-liste"></ul>`;
  const ul = box.querySelector("ul");
  for (const v of liste) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${datumVoll(v.von)} → <strong>${datumVoll(v.nach)}</strong></span>
      <span class="zahl">${eur(v.betrag)} €</span>
      <span>${v.quelle === "VORTRAG" ? "Vortrag" : "Planwert"}</span>
      <span class="hinweis">${esc(v.kommentar || "")}${v.benutzer ? ` · ${esc(v.benutzer)}` : ""}</span>
      ${v.abgelaufen ? `<span class="abgelaufen">abgelaufen</span>` : ""}
      ${LESER ? "" : `<button type="button" class="sekundaer klein-knopf" title="Verschiebung zurücknehmen">✕</button>`}`;
    const knopf = li.querySelector("button");
    if (knopf) knopf.addEventListener("click", async () => {
      try {
        await api("DELETE", `/api/mandanten/${MANDANT_ID}/plan/verschiebungen/${v.id}`);
        toast("Verschiebung zurückgenommen.", false);
        zdZustand.geaendert = false;
        await lade();
        await zdZeichnen();
      } catch (e) { toast(e.message); }
    });
    ul.appendChild(li);
  }
  return box;
}

document.getElementById("laden").addEventListener("click", lade);
if (typeof ANSICHT !== "undefined") document.getElementById("ansicht").value = ANSICHT;
for (const id of ["ansicht", "metrik"]) {
  document.getElementById(id).addEventListener("change", () => { zeichne(); exportLinks(); });
}
document.getElementById("ansicht").addEventListener("change", (e) => ansichtMerken(e.target.value));
document.getElementById("referenz").addEventListener("change", async (e) => {
  try {
    await referenzSetzen(MANDANT_ID, e.target.value);
    toast("Soll-Referenz gespeichert (gilt auch für Soll/Ist und den Excel-Export).", false);
    lade();
  } catch (err) { toast(err.message); ladeReferenz(); }
});

function ladeReferenz() {
  return referenzAuswahlFuellen(MANDANT_ID, document.getElementById("referenz")).catch(() => {});
}
document.getElementById("szenario").addEventListener("change", lade);
const einfrierenKnopf = document.getElementById("einfrieren");
if (einfrierenKnopf) {
  einfrierenKnopf.addEventListener("click", async () => {
    const start = document.getElementById("start").value || (daten && daten.start) || null;
    if (!confirm(`Aktuellen Plan als Soll-Vorgabe einfrieren?` +
      (start ? `\nStichtag: Wochenbeginn zum ${datumVoll(start)}.` : ""))) return;
    try {
      const erg = await api("POST", `/api/mandanten/${MANDANT_ID}/plan/snapshot`, {
        kommentar: null,
        start,
        wochen: parseInt(document.getElementById("wochen").value) || 13,
      });
      toast(`Plan eingefroren (Stichtag ${datumVoll(erg.stichtag)}).`, false);
      ladeReferenz();
      lade();
    } catch (e) { toast(e.message); }
  });
}
const gemerkterStart = startGemerkt(MANDANT_ID);
if (gemerkterStart) document.getElementById("start").value = gemerkterStart;
Promise.all([ladeSzenarien(), ladeReferenz()]).then(lade);
