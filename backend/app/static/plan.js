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
  if (daten.szenario) {
    const s = daten.szenario;
    const teile = [`Einzahlungen ${s.ein_faktor} %`, `variable Auszahlungen ${s.aus_faktor} %`];
    if (s.debitoren_verzoegerung_tage) teile.push(`Debitoren +${s.debitoren_verzoegerung_tage} Tage`);
    infoText = `Szenario „${s.name}“ (${teile.join(", ")}) · ` + infoText;
  }
  info.textContent = infoText;
  let q = `?wochen=${wochen}` + (document.getElementById("start").value ? `&start=${document.getElementById("start").value}` : "");
  if (szenarioId) q += `&szenario_id=${szenarioId}`;
  document.getElementById("export-xlsx").href = `/api/mandanten/${MANDANT_ID}/export/plan.xlsx${q}`;
  document.getElementById("export-pdf").href = `/api/mandanten/${MANDANT_ID}/export/plan.pdf${q}`;
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

function datumVoll(iso) { const [j, m, t] = iso.split("-"); return `${t}.${m}.${j}`; }

function wert(zeile, tag) {
  const metrik = document.getElementById("metrik").value;
  const plan = zeile.plan[tag] || 0, ist = zeile.ist[tag] || 0, basis = zeile.basis[tag] || 0;
  const vergangen = tag <= daten.heute;
  if (metrik === "plan") return { v: plan, ist: false };
  if (metrik === "ist") return { v: vergangen ? ist : null, ist: true };
  if (metrik === "delta") return { v: vergangen ? ist - basis : null, ist: false, delta: true };
  return vergangen ? { v: ist, ist: true } : { v: plan, ist: false };
}

function summiere(zeilen) {
  // Aggregat über Kinder: plan/ist/basis je Tag addieren
  const agg = { plan: {}, ist: {}, basis: {} };
  for (const z of zeilen) {
    for (const feld of ["plan", "ist", "basis"]) {
      for (const [tag, v] of Object.entries(z[feld])) {
        agg[feld][tag] = (agg[feld][tag] || 0) + v;
      }
    }
  }
  return agg;
}

function spalten() {
  const ansicht = document.getElementById("ansicht").value;
  if (ansicht === "tage") {
    return daten.tage.map(t => ({ id: t, tage: [t] }));
  }
  return daten.wochen.map(w => ({ id: w.label + w.jahr, label: w.label, tage: w.tage.filter(t => daten.tage.includes(t)) }));
}

function zellWert(zeile, sp) {
  let summe = 0, istTeil = false, alleNull = true, deltaMod = false;
  for (const t of sp.tage) {
    const w = wert(zeile, t);
    if (w.delta) deltaMod = true;
    if (w.v !== null && w.v !== 0) { summe += w.v; alleNull = false; }
    if (w.ist && (zeile.ist[t] || 0) !== 0) istTeil = true;
  }
  return { summe, istTeil, leer: alleNull, delta: deltaMod };
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

function datenZeile(zeile, sps, cssKlasse, einrueckung = "") {
  const tr = document.createElement("tr");
  if (cssKlasse) tr.className = cssKlasse;
  const kopfText = zeile.nummer
    ? `${einrueckung}<span class="konto-nr">${esc(zeile.nummer)}</span>${esc(zeile.name)}`
    : `${einrueckung}${esc(zeile.name)}`;
  tr.appendChild(td(kopfText, ["zeilkopf"]));
  for (const sp of sps) {
    const z = zellWert(zeile, sp);
    const klassen = tagKlassen(sp);
    if (!z.leer) {
      if (z.delta) klassen.push(z.summe < -0.005 ? "negativ" : (z.summe > 0.005 ? "positiv" : ""));
      else if (z.summe < 0 && cssKlasse === "summe") klassen.push("negativ");
      if (z.istTeil) klassen.push("ist-wert");
    }
    tr.appendChild(td(z.leer ? "" : eur(z.summe), klassen));
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
    const aggZeile = { name: gruppe.name, plan: agg.plan, ist: agg.ist, basis: agg.basis };
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
  for (const fk of b.finanzkonten) {
    koerper.appendChild(bestandsZeile(
      `<span class="konto-nr">${esc(fk.nummer)}</span>${esc(fk.name)}`,
      fk.bestand, sps, "", fk.kreditlinie > 0 ? fk.kreditlinie : null));
  }
  koerper.appendChild(bestandsZeile("Liquidität (Bank + Kasse)", b.liquiditaet, sps, "gesamt"));
  if (b.kreditlinien > 0) {
    koerper.appendChild(bestandsZeile("Verfügbar (inkl. Kreditlinien)", b.verfuegbar, sps, "gesamt"));
  }
  koerper.appendChild(bestandsZeile("Warenbestand (nachrichtlich)", b.waren, sps));

  tabelle.appendChild(koerper);
}

document.getElementById("laden").addEventListener("click", lade);
document.getElementById("ansicht").addEventListener("change", zeichne);
document.getElementById("metrik").addEventListener("change", zeichne);
document.getElementById("szenario").addEventListener("change", lade);
const einfrierenKnopf = document.getElementById("einfrieren");
if (einfrierenKnopf) {
  einfrierenKnopf.addEventListener("click", async () => {
    if (!confirm("Aktuellen Plan als Soll-Vorgabe einfrieren?")) return;
    try {
      const erg = await api("POST", `/api/mandanten/${MANDANT_ID}/plan/snapshot`, { kommentar: null });
      toast(`Plan eingefroren (Stichtag ${datumVoll(erg.stichtag)}).`, false);
      lade();
    } catch (e) { toast(e.message); }
  });
}
ladeSzenarien().then(lade);
