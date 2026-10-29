"use strict";
// KPI-Seite: Kennzahlen-Kacheln und Diagramme nach dem Fact-Sheet-Aufbau
// (Liquidität · Ein-/Auszahlungen · Planvergleich · Umsatz und Kosten).

let kpi = null;
// Diagramme werden erst gezeichnet, wenn alle Karten im Raster stehen –
// sonst misst ein Diagramm eine vorläufige (zu große) Kartenbreite
let zeichenAuftraege = [];
function karte(box, einstellungen, zeichner) {
  const plot = diagrammKarte(box, einstellungen);
  zeichenAuftraege.push(() => zeichner(plot));
}
const VERGLEICH_SCHLUESSEL = `liqui.kpi.vergleich.${MANDANT_ID}`;
const VORWOCHE_SCHLUESSEL = `liqui.kpi.vorwoche.${MANDANT_ID}`;
const ANTRAG_SCHLUESSEL = `liqui.kpi.antrag.${MANDANT_ID}`;

function gemerkt(schluessel) {
  try { return localStorage.getItem(schluessel) || ""; } catch (e) { return ""; }
}
function merken(schluessel, wert) {
  try { localStorage.setItem(schluessel, wert); } catch (e) { /* privater Modus */ }
}

function planstandText(s) {
  return s ? `${s.kommentar ? s.kommentar + " · " : ""}Stichtag ${datumVollDe(s.stichtag)}` : "–";
}

async function ladeVergleichsauswahl() {
  const snaps = await api("GET", `/api/mandanten/${MANDANT_ID}/plan/snapshots`);
  const sel = document.getElementById("vergleich");
  sel.innerHTML = `<option value="">Soll-Referenz (automatisch)</option>` + snaps.map(s =>
    `<option value="${s.id}">${esc(planstandText(s))}${s.referenz ? " (Soll-Referenz)" : ""}</option>`).join("");
  const vgl = gemerkt(VERGLEICH_SCHLUESSEL);
  if (snaps.some(s => String(s.id) === vgl)) sel.value = vgl;
  const vw = document.getElementById("vorwoche");
  vw.innerHTML = `<option value="">automatisch (jüngster Planstand vor dieser Woche)</option>` + snaps.map(s =>
    `<option value="${s.id}">${esc(planstandText(s))}</option>`).join("");
  const vwWert = gemerkt(VORWOCHE_SCHLUESSEL);
  if (snaps.some(s => String(s.id) === vwWert)) vw.value = vwWert;
  const an = document.getElementById("antrag");
  an.innerHTML = `<option value="">automatisch (Planstand „Antrag“, sonst ältester)</option>` + snaps.map(s =>
    `<option value="${s.id}">${esc(planstandText(s))}</option>`).join("");
  const anWert = gemerkt(ANTRAG_SCHLUESSEL);
  if (snaps.some(s => String(s.id) === anWert)) an.value = anWert;
}

async function lade() {
  const start = document.getElementById("start").value;
  startMerken(MANDANT_ID, start);
  const vergleich = document.getElementById("vergleich").value;
  const vorwoche = document.getElementById("vorwoche").value;
  const antrag = document.getElementById("antrag").value;
  merken(VERGLEICH_SCHLUESSEL, vergleich);
  merken(VORWOCHE_SCHLUESSEL, vorwoche);
  merken(ANTRAG_SCHLUESSEL, antrag);
  let url = `/api/mandanten/${MANDANT_ID}/kpi?wochen=${document.getElementById("wochen").value || 13}`;
  if (start) url += `&start=${start}`;
  if (vergleich) url += `&vergleich_id=${vergleich}`;
  if (vorwoche) url += `&vorwoche_id=${vorwoche}`;
  if (antrag) url += `&antrag_id=${antrag}`;
  const seite = document.querySelector(".kpi-seite");
  seite.classList.add("laedt");           // vorherige Darstellung gedimmt halten
  try { kpi = await api("GET", url); }
  catch (e) { toast(e.message); return; }
  finally { seite.classList.remove("laedt"); }
  if (!start) document.getElementById("start").value = kpi.start;
  exportLink();
  document.getElementById("kpi-info").textContent =
    `Zeitraum ${datumVollDe(kpi.start)} – ${datumVollDe(kpi.ende)} · Ist bis ${datumVollDe(kpi.heute)}, danach Plan` +
    (kpi.referenz ? ` · Soll-Referenz: ${planstandText(kpi.referenz)}` : " · noch kein eingefrorener Planstand");
  zeichne();
}

// PDF übernimmt Start, Wochen und Vergleichsstand der Seite
function exportLink() {
  const start = document.getElementById("start").value;
  const vergleich = document.getElementById("vergleich").value;
  const vorwoche = document.getElementById("vorwoche").value;
  const antrag = document.getElementById("antrag").value;
  let q = `?wochen=${document.getElementById("wochen").value || 13}`;
  if (start) q += `&start=${start}`;
  if (vergleich) q += `&vergleich_id=${vergleich}`;
  if (vorwoche) q += `&vorwoche_id=${vorwoche}`;
  if (antrag) q += `&antrag_id=${antrag}`;
  document.getElementById("export-pdf").href = `/api/mandanten/${MANDANT_ID}/export/kpi.pdf${q}`;
}

// ---------------------------------------------------------------- Kacheln

function deltaZeile(wert, text, gutWennPositiv = true) {
  if (wert === null || wert === undefined) return null;
  const gut = gutWennPositiv ? wert >= 0 : wert <= 0;
  return { text: `${wert >= 0 ? "▲ +" : "▼ "}${euroVoll(wert)} ${text}`, klasse: gut ? "gut" : "schlecht" };
}

function kachel(eltern, { label, wert, zeilen = [], status = null }) {
  const k = document.createElement("div");
  k.className = "kpi-kachel" + (status ? ` status-${status.art}` : "");
  const l = document.createElement("div");
  l.className = "kpi-kachel-label";
  l.textContent = label;
  const w = document.createElement("div");
  w.className = "kpi-kachel-wert";
  w.textContent = wert;
  k.append(l, w);
  if (status) {
    const s = document.createElement("div");
    s.className = "kpi-status";
    s.textContent = `${status.art === "kritisch" ? "⚠" : "✓"} ${status.text}`;
    k.appendChild(s);
  }
  for (const z of zeilen.filter(Boolean)) {
    const d = document.createElement("div");
    d.className = "kpi-kachel-zeile " + (z.klasse || "");
    d.textContent = z.text;
    k.appendChild(d);
  }
  eltern.appendChild(k);
}

function zeichneKacheln() {
  const box = document.getElementById("kacheln");
  box.replaceChildren();
  const z = kpi.kennzahlen;
  kachel(box, { label: "Liquidität heute", wert: euroVoll(z.liquiditaet_heute),
    zeilen: [{ text: `Bank + Kasse, Stand ${datumVollDe(kpi.heute)}` }] });
  kachel(box, { label: "Liquidität zum Planende", wert: euroVoll(z.liquiditaet_ende),
    zeilen: [
      kpi.vergleich && z.liquiditaet_ende_vergleich !== null
        ? deltaZeile(z.liquiditaet_ende - z.liquiditaet_ende_vergleich, "ggü. Vergleichsstand") : null,
      { text: `Netto-Cashflow im Zeitraum ${euroVoll(z.netto_fenster)}` },
    ] });
  kachel(box, { label: "Tiefster Stand", wert: euroVoll(z.tiefstand.wert),
    status: z.tiefstand.wert < 0 ? { art: "kritisch", text: "Unterdeckung" } : null,
    zeilen: [{ text: `am ${datumVollDe(z.tiefstand.datum)}` }] });
  kachel(box, z.unterdeckung_am
    ? { label: "Liquiditätsreichweite", wert: `bis ${datumVollDe(z.unterdeckung_am)}`,
        status: { art: "kritisch", text: "Unterdeckung im Planungszeitraum" },
        zeilen: [{ text: `${Math.round((new Date(z.unterdeckung_am) - new Date(kpi.heute)) / 864e5)} Tage ab heute` }] }
    : { label: "Liquiditätsreichweite", wert: `über ${datumVollDe(kpi.ende)}`,
        status: { art: "gut", text: "keine Unterdeckung" },
        zeilen: [{ text: "über das Ende des Planungszeitraums hinaus" }] });
  const s = z.kw_sollist;
  kachel(box, s
    ? { label: `Liquidität ${s.label}: Ist gegen Soll`, wert: euroVoll(s.ist),
        zeilen: [deltaZeile(s.abweichung, "ggü. Soll"), { text: `Soll ${euroVoll(s.soll)} (${datumVollDe(s.bis)})` }] }
    : { label: "Liquidität Ist gegen Soll", wert: "–",
        zeilen: [{ text: "keine Soll-Referenz für abgeschlossene Wochen im Zeitraum" }] });
  kachel(box, { label: "Cash-Marge (Umsatzrentabilität)", wert: prozent(z.umsatzrentabilitaet),
    zeilen: [{ text: `ohne Sondereffekte ${prozent(z.umsatzrentabilitaet_ohne_effekte)}` },
             { text: "Netto-Cashflow ÷ Einzahlungen im Zeitraum" }] });
  kachel(box, { label: "Offene Forderungen", wert: euroVoll(z.forderungen_offen),
    zeilen: [{ text: "Debitoren, Restbeträge" }] });
  kachel(box, { label: "Offene Verbindlichkeiten", wert: euroVoll(z.verbindlichkeiten_offen),
    zeilen: [{ text: `davon gesperrt (§ 38 InsO) ${euroVoll(z.gesperrte_insolvenzforderungen)}` }] });
}

// ---------------------------------------------------------------- Diagramme

function zeichneLiquiditaet() {
  const box = document.getElementById("bereich-liquiditaet");
  box.replaceChildren();
  const w = kpi.liquiditaet_wochen;
  const prognoseAb = w.findIndex(x => x.prognose);
  const hatSoll = w.some(x => x.soll !== null);
  const sollName = kpi.referenz ? `Soll (${planstandText(kpi.referenz)})` : "Soll";
  const reihen = [];
  if (hatSoll) reihen.push({ name: sollName, farbe: DG_FARBE.grau, werte: w.map(x => x.soll) });
  reihen.push({ name: "Ist bis heute, danach Prognose", farbe: DG_FARBE.blau, werte: w.map(x => x.wert),
                prognoseAb: prognoseAb < 0 ? null : prognoseAb });
  karte(box, {
    titel: "Liquidität je Wochenende: Soll / Ist",
    untertitel: "Bank + Kasse; gestrichelt: Prognose aus dem aktuellen Plan",
    legende: [...(hatSoll ? [{ name: sollName, farbe: DG_FARBE.grau, art: "linie" }] : []),
              { name: "Ist bis heute, danach Prognose", farbe: DG_FARBE.blau, art: "linie" }],
    tabelle: { kopf: ["Woche", "Ist / Prognose", "Soll", "Abweichung"],
      zeilen: w.map(x => [`${x.label} (bis ${datumVollDe(x.bis)})${x.prognose ? " – Prognose" : ""}`,
        euroVoll(x.wert), euroVoll(x.soll),
        x.soll === null || x.wert === null ? "–" : euroVoll(x.wert - x.soll)]) },
  }, plot => linienDiagramm(plot, { kategorien: w.map(x => x.label), reihen }));

  const m = kpi.monate;
  const mitVergleich = kpi.vergleich && m.some(x => x.vergleich_liquiditaet_ende !== null);
  const vglName = `Vergleich (${planstandText(kpi.vergleich)})`;
  const reihenM = [];
  if (mitVergleich) reihenM.push({ name: vglName, farbe: DG_FARBE.grau, werte: m.map(x => x.vergleich_liquiditaet_ende) });
  reihenM.push({ name: "Aktuell", farbe: DG_FARBE.blau, werte: m.map(x => x.liquiditaet_ende) });
  karte(box, {
    titel: "Liquidität zum Monatsende",
    untertitel: "Randmonate bis zum Fensterbeginn bzw. -ende",
    legende: mitVergleich ? [{ name: vglName, farbe: DG_FARBE.grau }, { name: "Aktuell", farbe: DG_FARBE.blau }] : null,
    tabelle: { kopf: ["Monat", "Aktuell", ...(mitVergleich ? ["Vergleich", "Veränderung"] : [])],
      zeilen: m.map(x => [`${x.label} (${x.status})`, euroVoll(x.liquiditaet_ende),
        ...(mitVergleich ? [euroVoll(x.vergleich_liquiditaet_ende),
          x.vergleich_liquiditaet_ende === null ? "–" : euroVoll(x.liquiditaet_ende - x.vergleich_liquiditaet_ende)] : [])]) },
  }, plot => saeulenDiagramm(plot, { kategorien: m.map(x => x.label), unterLabels: m.map(x => x.status), reihen: reihenM,
        beschriften: reihenM.length - 1 }));
}

function zeichneZahlungen() {
  const box = document.getElementById("bereich-zahlungen");
  box.replaceChildren();
  const m = kpi.monate;
  karte(box, {
    titel: "Netto-Cashflow je Monat",
    untertitel: "Einzahlungen − Auszahlungen; Ist bis heute, danach Plan",
    legende: [{ name: "Überschuss", farbe: DG_FARBE.blau }, { name: "Unterdeckung", farbe: DG_FARBE.rot }],
    tabelle: { kopf: ["Monat", "Einzahlungen", "Auszahlungen", "Netto"],
      zeilen: m.map(x => [`${x.label} (${x.status})`, euroVoll(x.einzahlungen), euroVoll(x.auszahlungen), euroVoll(x.netto)]) },
  }, plot => saeulenDiagramm(plot, { kategorien: m.map(x => x.label), unterLabels: m.map(x => x.status),
        reihen: [{ name: "Netto-Cashflow", farbe: DG_FARBE.blau, werte: m.map(x => x.netto) }],
        divergierend: true, beschriften: 0 }));

  karte(box, {
    titel: "Einzahlungen und Auszahlungen je Monat",
    untertitel: "Beträge; Ist bis heute, danach Plan",
    legende: [{ name: "Einzahlungen", farbe: DG_FARBE.blau }, { name: "Auszahlungen", farbe: DG_FARBE.orange }],
    tabelle: { kopf: ["Monat", "Einzahlungen", "Auszahlungen"],
      zeilen: m.map(x => [`${x.label} (${x.status})`, euroVoll(x.einzahlungen), euroVoll(-x.auszahlungen)]) },
  }, plot => saeulenDiagramm(plot, { kategorien: m.map(x => x.label), unterLabels: m.map(x => x.status),
        reihen: [{ name: "Einzahlungen", farbe: DG_FARBE.blau, werte: m.map(x => x.einzahlungen) },
                 { name: "Auszahlungen", farbe: DG_FARBE.orange, werte: m.map(x => -x.auszahlungen) }] }));
}

function zeichneVergleich() {
  const box = document.getElementById("bereich-vergleich");
  box.replaceChildren();
  document.getElementById("titel-vergleich").textContent = kpi.vergleich
    ? `Planvergleich: aktueller Stand gegenüber ${planstandText(kpi.vergleich)}` : "Planvergleich";
  if (!kpi.vergleich) {
    const p = document.createElement("p");
    p.className = "hinweis";
    p.textContent = "Noch kein eingefrorener Planstand – über „Plan einfrieren“ auf der Plan-Seite anlegen.";
    box.appendChild(p);
    return;
  }
  kategorieVeraenderungKarte(box, kpi.kategorien);

  const m = kpi.monate.filter(x => x.vergleich_liquiditaet_ende !== null);
  karte(box, {
    titel: "Veränderung der Monatsendliquidität",
    untertitel: "aktuell − Vergleich",
    legende: [{ name: "höher als geplant", farbe: DG_FARBE.blau }, { name: "niedriger als geplant", farbe: DG_FARBE.rot }],
    tabelle: { kopf: ["Monat", "Aktuell", "Vergleich", "Veränderung"],
      zeilen: m.map(x => [x.label, euroVoll(x.liquiditaet_ende), euroVoll(x.vergleich_liquiditaet_ende),
        euroVoll(x.liquiditaet_ende - x.vergleich_liquiditaet_ende)]) },
  }, plot => saeulenDiagramm(plot, { kategorien: m.map(x => x.label),
        reihen: [{ name: "Veränderung", farbe: DG_FARBE.blau, werte: m.map(x => x.liquiditaet_ende - x.vergleich_liquiditaet_ende) }],
        divergierend: true, beschriften: 0 }));

  kategorieMonatKarte(box, kpi.kategorien);
}

// Veränderung je Kategorie (Liquiditätswirkung aktuell − Planstand)
function kategorieVeraenderungKarte(box, kategorien, vergleichName = "Vergleich") {
  const kat = [...kategorien].filter(k => Math.abs(k.veraenderung) >= 0.005)
    .sort((a, b) => Math.abs(b.veraenderung) - Math.abs(a.veraenderung));
  karte(box, {
    titel: "Veränderung je Kategorie",
    untertitel: `Liquiditätswirkung im gemeinsamen Zeitraum (aktuell − ${vergleichName})`,
    legende: [{ name: "Liquidität besser", farbe: DG_FARBE.blau }, { name: "Liquidität schlechter", farbe: DG_FARBE.rot }],
    tabelle: { kopf: ["Kategorie", "Aktuell", vergleichName, "Veränderung"],
      zeilen: kategorien.map(k => [k.name, euroVoll(k.aktuell), euroVoll(k.vergleich), euroVoll(k.veraenderung)]) },
  }, plot => balkenDiagramm(plot, { kategorien: kat.map(k => k.name), werte: kat.map(k => k.veraenderung), divergierend: true,
        tooltipZeilen: i => [
          { farbe: kat[i].veraenderung >= 0 ? DG_FARBE.blau : DG_FARBE.rot, art: "flaeche", wert: euroVoll(kat[i].veraenderung), name: "Veränderung" },
          { farbe: DG_FARBE.blau, art: "linie", wert: euroVoll(kat[i].aktuell), name: "aktuell" },
          { farbe: DG_FARBE.grau, art: "linie", wert: euroVoll(kat[i].vergleich), name: vergleichName },
        ] }));
}

// Kategorie × Monat als Tabelle (zu viele Klassen für ein Diagramm)
function kategorieMonatKarte(box, kategorien, vergleichName = "Vergleich") {
  const monate = kpi.monate.filter(x => kategorien.some(k => k.je_monat[x.schluessel] !== undefined));
  const tabKarte = document.createElement("figure");
  tabKarte.className = "kpi-karte breit";
  const h = document.createElement("h3");
  h.textContent = "Veränderung je Kategorie und Monat";
  const u = document.createElement("div");
  u.className = "kpi-untertitel";
  u.textContent = `Liquiditätswirkung aktuell − ${vergleichName} in €; positiv = Liquidität besser`;
  const huelle = document.createElement("div");
  huelle.className = "kpi-tabelle-huelle";
  const t = document.createElement("table");
  t.className = "liste";
  const kopf = document.createElement("tr");
  for (const text of ["Kategorie", ...monate.map(x => x.label), "Summe"]) {
    const th = document.createElement("th");
    th.textContent = text;
    if (text !== "Kategorie") th.className = "zahl";
    kopf.appendChild(th);
  }
  const thead = document.createElement("thead");
  thead.appendChild(kopf);
  const tbody = document.createElement("tbody");
  const geaendert = kategorien.filter(k => Math.abs(k.veraenderung) >= 0.005
    || Object.values(k.je_monat).some(v => Math.abs(v || 0) >= 0.005));
  const unveraendert = kategorien.length - geaendert.length;
  if (unveraendert) u.textContent += ` · ${unveraendert} Kategorien unverändert (ausgeblendet)`;
  for (const k of geaendert) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = k.name;
    tr.appendChild(name);
    for (const x of monate) wertZelle(tr, k.je_monat[x.schluessel]);
    wertZelle(tr, k.veraenderung, true);
    tbody.appendChild(tr);
  }
  if (!geaendert.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = monate.length + 2;
    td.className = "hinweis";
    td.textContent = "Keine Veränderungen.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  t.append(thead, tbody);
  huelle.appendChild(t);
  tabKarte.append(h, u, huelle);
  box.appendChild(tabKarte);
}

function wertZelle(tr, wert, fett = false) {
  const td = document.createElement("td");
  td.className = "zahl" + (wert < -0.005 ? " negativ" : wert > 0.005 ? " positiv" : "");
  td.textContent = wert === null || wert === undefined || Math.abs(wert) < 0.005 ? "" : eur(wert);
  if (fett) td.style.fontWeight = "600";
  tr.appendChild(td);
}

// ------------------------------------- Vergleich zur Vorwoche / zum Antrag
// Fact-Sheet-Blöcke „Entwicklung KW“ und „Entwicklung Antrag“: letzte Woche
// Ist gegen den Planstand, Ergebnisveränderung, Abweichung Woche → Ergebnis,
// Anpassungen je Kategorie und Monat

const PLANSTAND_BLOECKE = {
  vorwoche: {
    titel: "Vergleich zur Vorwoche", kurz: "Vorwoche", plan: "Vorwochenplan",
    leer: "Kein Planstand aus einer früheren Woche – den Plan wöchentlich über „Plan einfrieren“ " +
      "festhalten; der jüngste vor der laufenden Woche eingefrorene Stand dient dann als Vorwoche.",
  },
  antrag: {
    titel: "Vergleich zum Antrag", kurz: "Antrag", plan: "Antragsplan",
    leer: "Noch kein eingefrorener Planstand – den Plan zum Antrag über „Plan einfrieren“ festhalten " +
      "(Kommentar mit „Antrag“; sonst gilt der älteste Planstand).",
  },
};

function zeichnePlanstandVergleich(art) {
  const cfg = PLANSTAND_BLOECKE[art];
  const kacheln = document.getElementById(`kacheln-${art}`);
  const box = document.getElementById(`bereich-${art}`);
  kacheln.replaceChildren();
  box.replaceChildren();
  const vg = kpi[art];
  const titel = document.getElementById(`titel-${art}`);
  if (!vg) {
    titel.textContent = cfg.titel;
    const p = document.createElement("p");
    p.className = "hinweis";
    p.textContent = cfg.leer;
    box.appendChild(p);
    return;
  }
  const planName = `${cfg.plan} (Stichtag ${datumVollDe(vg.planstand.stichtag)})`;
  titel.textContent = `${cfg.titel}: aktueller Stand gegenüber ${planstandText(vg.planstand)}`;
  const w = vg.woche, e = vg.ergebnis, b = vg.bruecke;

  kachel(kacheln, w
    ? { label: `${w.label}: Ist gegen ${cfg.plan}`, wert: euroVoll(w.ist),
        zeilen: [deltaZeile(w.abweichung, `ggü. ${cfg.plan}`), { text: `Soll ${euroVoll(w.soll)} (${datumVollDe(w.bis)})` }] }
    : { label: `Letzte Woche: Ist gegen ${cfg.plan}`, wert: "–",
        zeilen: [{ text: "der Planstand deckt die letzte Woche nicht ab" }] });
  kachel(kacheln, w
    ? { label: `Über-/Unterdeckung ${w.label}`, wert: euroVoll(w.netto.ist),
        zeilen: [deltaZeile(w.netto.differenz, `ggü. ${cfg.plan}`), { text: `Soll ${euroVoll(w.netto.soll)} (Ein- − Auszahlungen)` }] }
    : { label: "Über-/Unterdeckung letzte Woche", wert: "–", zeilen: [] });
  kachel(kacheln, e
    ? { label: `Ergebnis: Liquidität zum ${datumVollDe(e.datum)}`, wert: euroVoll(e.aktuell),
        zeilen: [deltaZeile(e.differenz, `ggü. ${cfg.kurz}`), { text: `${cfg.kurz} ${euroVoll(e.planstand)}` }] }
    : { label: "Ergebnis", wert: "–", zeilen: [{ text: "kein gemeinsamer Zeitraum mit dem Planstand" }] });
  kachel(kacheln, b
    ? { label: "Veränderung nach der Woche", wert: euroVoll(b.differenz),
        zeilen: [{ text: `Ergebnis ${euroVoll(b.ergebnis)} − Woche ${euroVoll(b.woche)}` },
                 { text: "Zeitverschiebungen und Planänderungen bis zum Ergebnistag" }] }
    : { label: "Veränderung nach der Woche", wert: "–", zeilen: [] });

  // Liquidität je Wochenende; beim Antrag als Gegenüberstellung mit dem Vorwochenplan
  const lw = vg.liquiditaet_wochen;
  const prognoseAb = lw.findIndex(x => x.prognose);
  const reihen = [{ name: planName, farbe: DG_FARBE.grau, werte: lw.map(x => x.planstand) }];
  const legende = [{ name: planName, farbe: DG_FARBE.grau, art: "linie" }];
  const kopf = ["Woche", "Ist / Prognose", cfg.plan, "Abweichung"];
  const vw = art === "antrag" && kpi.vorwoche && kpi.vorwoche.planstand.id !== vg.planstand.id ? kpi.vorwoche : null;
  if (vw) {
    const vwName = `Vorwochenplan (Stichtag ${datumVollDe(vw.planstand.stichtag)})`;
    reihen.push({ name: vwName, farbe: DG_FARBE.orange, werte: vw.liquiditaet_wochen.map(x => x.planstand) });
    legende.push({ name: vwName, farbe: DG_FARBE.orange, art: "linie" });
  }
  reihen.push({ name: "Ist bis heute, danach Prognose", farbe: DG_FARBE.blau, werte: lw.map(x => x.wert),
                prognoseAb: prognoseAb < 0 ? null : prognoseAb });
  legende.push({ name: "Ist bis heute, danach Prognose", farbe: DG_FARBE.blau, art: "linie" });
  karte(box, {
    titel: vw ? `Gegenüberstellung: ${cfg.plan}, Vorwochenplan und Ist` : `Liquidität je Wochenende: ${cfg.plan} / Ist`,
    untertitel: "Bank + Kasse je Wochenende; gestrichelt: Prognose aus dem aktuellen Plan",
    legende,
    tabelle: { kopf: vw ? [...kopf, "Vorwochenplan"] : kopf,
      zeilen: lw.map((x, i) => [`${x.label} (bis ${datumVollDe(x.bis)})${x.prognose ? " – Prognose" : ""}`,
        euroVoll(x.wert), euroVoll(x.planstand), x.planstand === null || x.wert === null ? "–" : euroVoll(x.wert - x.planstand),
        ...(vw ? [euroVoll(vw.liquiditaet_wochen[i].planstand)] : [])]) },
  }, plot => linienDiagramm(plot, { kategorien: lw.map(x => x.label), reihen }));

  // Abweichung Woche → Ergebnis („Woche / Ergebnis / Differenz“ im Fact Sheet)
  if (b) {
    karte(box, {
      titel: "Abweichung Woche und Ergebnis",
      untertitel: `Ist − ${cfg.plan} zum Wochenende, Veränderung des Ergebnisses und Differenz`,
      legende: [{ name: "Liquidität besser", farbe: DG_FARBE.blau }, { name: "Liquidität schlechter", farbe: DG_FARBE.rot }],
      tabelle: { kopf: ["", "Betrag"], zeilen: [
        [`Abweichung ${w.label} (Ist ${euroVoll(w.ist)} − Soll ${euroVoll(w.soll)})`, euroVoll(b.woche)],
        [`Veränderung Ergebnis zum ${datumVollDe(e.datum)} (aktuell − ${cfg.kurz})`, euroVoll(b.ergebnis)],
        ["Differenz (Zeitverschiebungen und Planänderungen nach der Woche)", euroVoll(b.differenz)]] },
    }, plot => saeulenDiagramm(plot, { kategorien: [`Woche ${w.label}`, `Ergebnis ${datumDe(e.datum)}`, "Differenz"],
          reihen: [{ name: "Betrag", farbe: DG_FARBE.blau, werte: [b.woche, b.ergebnis, b.differenz] }],
          divergierend: true, beschriften: 0 }));
  }

  // letzte Woche je Kategorie: Ist gegen Planstand
  if (w) {
    const kat = w.kategorien.filter(k => Math.abs(k.differenz) >= 0.005)
      .sort((x, y) => Math.abs(y.differenz) - Math.abs(x.differenz));
    karte(box, {
      titel: `${w.label} je Kategorie: Ist gegen ${cfg.plan}`,
      untertitel: `${datumVollDe(w.von)} – ${datumVollDe(w.bis)}; Liquiditätswirkung Ist − Soll`,
      legende: [{ name: "Liquidität besser", farbe: DG_FARBE.blau }, { name: "Liquidität schlechter", farbe: DG_FARBE.rot }],
      tabelle: { kopf: ["Kategorie", `Soll (${cfg.plan})`, "Ist", "Differenz"],
        zeilen: [...w.kategorien.map(k => [k.name, euroVoll(k.soll), euroVoll(k.ist), euroVoll(k.differenz)]),
          ["Über-/Unterdeckung", euroVoll(w.netto.soll), euroVoll(w.netto.ist), euroVoll(w.netto.differenz)]] },
    }, plot => kat.length
      ? balkenDiagramm(plot, { kategorien: kat.map(k => k.name), werte: kat.map(k => k.differenz), divergierend: true,
          tooltipZeilen: i => [
            { farbe: kat[i].differenz >= 0 ? DG_FARBE.blau : DG_FARBE.rot, art: "flaeche", wert: euroVoll(kat[i].differenz), name: "Differenz" },
            { farbe: DG_FARBE.blau, art: "linie", wert: euroVoll(kat[i].ist), name: "Ist" },
            { farbe: DG_FARBE.grau, art: "linie", wert: euroVoll(kat[i].soll), name: `Soll (${cfg.plan})` },
          ] })
      : dgLeer(plot, `Keine Abweichungen gegenüber dem ${cfg.plan}.`));
  }

  const m = vg.monate.filter(x => x.planstand_liquiditaet_ende !== null);
  if (art === "vorwoche") {
    // Ein-/Auszahlungen der kommenden Wochen („Forecast“ im Fact Sheet)
    const fw = vg.forecast_wochen;
    karte(box, {
      titel: "Ein- und Auszahlungen der kommenden Wochen",
      untertitel: "Aktueller Plan (laufende Woche: Ist bis heute); Tabelle mit Vorwochenplan",
      legende: [{ name: "Einzahlungen", farbe: DG_FARBE.blau }, { name: "Auszahlungen", farbe: DG_FARBE.orange }],
      tabelle: { kopf: ["Woche", "Einzahlungen", "Vorwoche", "Auszahlungen", "Vorwoche"],
        zeilen: fw.map(x => [`${x.label} (${datumDe(x.von)}–${datumVollDe(x.bis)})`, euroVoll(x.einzahlungen),
          euroVoll(x.planstand_einzahlungen), euroVoll(-x.auszahlungen),
          x.planstand_auszahlungen === null ? "–" : euroVoll(-x.planstand_auszahlungen)]) },
    }, plot => saeulenDiagramm(plot, { kategorien: fw.map(x => x.label),
          reihen: [{ name: "Einzahlungen", farbe: DG_FARBE.blau, werte: fw.map(x => x.einzahlungen) },
                   { name: "Auszahlungen", farbe: DG_FARBE.orange, werte: fw.map(x => -x.auszahlungen) }] }));
  } else {
    // Monatsübersicht: Antragsplan gegen aktuellen Stand
    karte(box, {
      titel: `Liquidität zum Monatsende: ${cfg.plan} / aktuell`,
      untertitel: "Ist bis heute, danach Plan; Antragsplan als Vergleich",
      legende: [{ name: planName, farbe: DG_FARBE.grau }, { name: "Aktuell", farbe: DG_FARBE.blau }],
      tabelle: { kopf: ["Monat", "Aktuell", cfg.plan],
        zeilen: m.map(x => [`${x.label} (${x.status})`, euroVoll(x.liquiditaet_ende), euroVoll(x.planstand_liquiditaet_ende)]) },
    }, plot => saeulenDiagramm(plot, { kategorien: m.map(x => x.label), unterLabels: m.map(x => x.status),
          reihen: [{ name: planName, farbe: DG_FARBE.grau, werte: m.map(x => x.planstand_liquiditaet_ende) },
                   { name: "Aktuell", farbe: DG_FARBE.blau, werte: m.map(x => x.liquiditaet_ende) }] }));
  }

  // Anpassungen gegenüber dem Planstand
  kategorieVeraenderungKarte(box, vg.kategorien, cfg.kurz);
  karte(box, {
    titel: "Veränderung der Monatsendliquidität",
    untertitel: `aktuell − ${cfg.kurz}`,
    legende: [{ name: `höher als im ${cfg.plan}`, farbe: DG_FARBE.blau },
              { name: `niedriger als im ${cfg.plan}`, farbe: DG_FARBE.rot }],
    tabelle: { kopf: ["Monat", "Aktuell", cfg.kurz, "Veränderung"],
      zeilen: m.map(x => [x.label, euroVoll(x.liquiditaet_ende), euroVoll(x.planstand_liquiditaet_ende),
        euroVoll(x.liquiditaet_ende - x.planstand_liquiditaet_ende)]) },
  }, plot => saeulenDiagramm(plot, { kategorien: m.map(x => x.label),
        reihen: [{ name: "Veränderung", farbe: DG_FARBE.blau, werte: m.map(x => x.liquiditaet_ende - x.planstand_liquiditaet_ende) }],
        divergierend: true, beschriften: 0 }));
  kategorieMonatKarte(box, vg.kategorien, cfg.kurz);
}

function zeichneVerteilung() {
  const box = document.getElementById("bereich-verteilung");
  box.replaceChildren();
  const ein = kpi.einzahlungen_konten;
  karte(box, {
    titel: "Einzahlungen nach Konto",
    untertitel: "Summe im Zeitraum, Anteil an allen Einzahlungen",
    tabelle: { kopf: ["Konto", "Summe", "Anteil"], zeilen: ein.map(x => [x.name, euroVoll(x.summe), prozent(x.anteil)]) },
  }, plot => balkenDiagramm(plot, { kategorien: ein.map(x => x.name), werte: ein.map(x => x.summe), farbe: DG_FARBE.blau,
        zusatz: ein.map(x => ` · ${prozent(x.anteil, 0)}`) }));

  const aus = kpi.auszahlungen_gruppen;
  karte(box, {
    titel: "Auszahlungen nach Kategorie",
    untertitel: "Summe im Zeitraum, Anteil an allen Auszahlungen",
    tabelle: { kopf: ["Kategorie", "Summe", "Anteil", "Anteil ohne Sondereffekte"],
      zeilen: aus.map(x => [x.name + (x.sondereffekt ? " (Sondereffekt)" : ""), euroVoll(x.summe),
        prozent(x.anteil), x.sondereffekt ? "–" : prozent(x.anteil_ohne)]) },
  }, plot => balkenDiagramm(plot, { kategorien: aus.map(x => x.name + (x.sondereffekt ? " *" : "")), werte: aus.map(x => x.summe),
        farbe: DG_FARBE.orange, zusatz: aus.map(x => ` · ${prozent(x.anteil, 0)}`),
        tooltipZeilen: i => [
          { farbe: DG_FARBE.orange, art: "flaeche", wert: euroVoll(aus[i].summe), name: "Summe" },
          { farbe: DG_FARBE.orange, art: "linie", wert: prozent(aus[i].anteil), name: "Anteil" },
          { farbe: DG_FARBE.grau, art: "linie", wert: aus[i].sondereffekt ? "Sondereffekt" : prozent(aus[i].anteil_ohne),
            name: "Anteil ohne Sondereffekte" },
        ] }));

  const gruppen = kpi.kennzahlen.sondereffekt_gruppen;
  document.getElementById("sondereffekt-hinweis").textContent = gruppen.length
    ? `Sondereffekte (* – in den Kennzahlen „ohne Sondereffekte“ herausgerechnet): ${gruppen.join(", ")}.`
    : "Für die Kennzahlen „ohne Sondereffekte“ ist noch keine Kontogruppe als Sondereffekt gekennzeichnet (Konten → Gruppen, Spalte „Sondereffekt“).";
}

function zeichne() {
  if (!kpi) return;
  zeichenAuftraege = [];
  zeichneKacheln();
  zeichneLiquiditaet();
  zeichneZahlungen();
  zeichneVergleich();
  zeichnePlanstandVergleich("vorwoche");
  zeichnePlanstandVergleich("antrag");
  zeichneVerteilung();
  for (const auftrag of zeichenAuftraege) auftrag();
}

let groesseTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(groesseTimer);
  groesseTimer = setTimeout(zeichne, 150);
});
document.getElementById("laden").addEventListener("click", lade);
document.getElementById("vergleich").addEventListener("change", lade);
document.getElementById("vorwoche").addEventListener("change", lade);
document.getElementById("antrag").addEventListener("change", lade);
const gemerkterStart = startGemerkt(MANDANT_ID);
if (gemerkterStart) document.getElementById("start").value = gemerkterStart;
ladeVergleichsauswahl().catch(() => {}).then(lade);
