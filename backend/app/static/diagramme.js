"use strict";
// Kleine SVG-Diagrammbibliothek (ohne Fremdabhängigkeit) für die KPI-Seite:
// Linien-, Säulen- und Balkendiagramm, jeweils mit Tooltip, Legende und
// Tabellenansicht. Farben siehe DG_FARBE (validiert gegen die weiße Karte).

const DG_FARBE = {
  blau: "#2a78d6", orange: "#eb6834", grau: "#898781", rot: "#e34948",
  gitter: "#e1e0d9", achse: "#c3c2b7", text: "#1c2530", text2: "#52514e", gedaempft: "#898781",
};
const DG_NS = "http://www.w3.org/2000/svg";
const DG_SCHRIFT = '12px "Segoe UI", system-ui, -apple-system, sans-serif';
const dgMessung = document.createElement("canvas").getContext("2d");

function dgEl(tag, attrs, eltern) {
  const el = document.createElementNS(DG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (eltern) eltern.appendChild(el);
  return el;
}

function dgTextbreite(text, schrift = DG_SCHRIFT) {
  dgMessung.font = schrift;
  return dgMessung.measureText(text).width;
}

function dgText(eltern, x, y, text, attrs = {}) {
  const el = dgEl("text", { x, y, "font-size": 12, fill: DG_FARBE.gedaempft, ...attrs }, eltern);
  el.textContent = text;
  return el;
}

// Kompakte Euro-Achsenwerte: 950 · 12,5 T · 1,2 Mio.
function euroKompakt(v) {
  if (v === null || v === undefined) return "–";
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " Mio.";
  if (a >= 1e3) return (v / 1e3).toLocaleString("de-DE", { maximumFractionDigits: a >= 1e5 ? 0 : 1 }) + " T";
  return Math.round(v).toLocaleString("de-DE");
}
function euroVoll(v) { return v === null || v === undefined ? "–" : eur(v) + " €"; }
function prozent(v, stellen = 1) {
  return v === null || v === undefined ? "–"
    : (v * 100).toLocaleString("de-DE", { minimumFractionDigits: stellen, maximumFractionDigits: stellen }) + " %";
}

// „Schöne“ Achsenteilung inkl. Nulllinie
function dgTicks(min, max, ziel = 5) {
  if (min === max) { min -= 1; max += 1; }
  const roh = (max - min) / ziel;
  const basis = Math.pow(10, Math.floor(Math.log10(roh)));
  const schritt = [1, 2, 2.5, 5, 10].map(f => f * basis).find(s => s >= roh) || 10 * basis;
  const von = Math.floor(min / schritt) * schritt, bis = Math.ceil(max / schritt) * schritt;
  const ticks = [];
  for (let t = von; t <= bis + schritt / 2; t += schritt) ticks.push(Math.round(t * 1e6) / 1e6);
  return ticks;
}

// Säule/Balken mit 4px gerundetem Datenende, eckig an der Nulllinie
function dgBalkenPfad(x, y, b, h, ende) {
  const r = Math.min(4, b / 2, h);
  if (h <= 0.5) return `M${x},${y}h${b}v${Math.max(h, 0.5)}h${-b}z`;
  switch (ende) {
    case "oben": return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + b - r}Q${x + b},${y} ${x + b},${y + r}V${y + h}Z`;
    case "unten": return `M${x},${y}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + b - r}Q${x + b},${y + h} ${x + b},${y + h - r}V${y}Z`;
    case "rechts": return `M${x},${y}H${x + b - r}Q${x + b},${y} ${x + b},${y + r}V${y + h - r}Q${x + b},${y + h} ${x + b - r},${y + h}H${x}Z`;
    default: return `M${x + b},${y}H${x + r}Q${x},${y} ${x},${y + r}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + b}Z`;
  }
}

// ---- Tooltip (eine Instanz für die Seite; Inhalte nur über textContent) ----
let dgTip = null;
function dgTooltip(ereignis, titel, zeilen) {
  if (!dgTip) {
    dgTip = document.createElement("div");
    dgTip.className = "dg-tooltip";
    document.body.appendChild(dgTip);
  }
  dgTip.replaceChildren();
  const kopf = document.createElement("div");
  kopf.className = "dg-tooltip-titel";
  kopf.textContent = titel;
  dgTip.appendChild(kopf);
  for (const z of zeilen) {
    const zeile = document.createElement("div");
    zeile.className = "dg-tooltip-zeile";
    const schluessel = document.createElement("span");
    schluessel.className = "dg-schluessel " + (z.art || "linie");
    schluessel.style.background = z.farbe;
    const wert = document.createElement("strong");
    wert.textContent = z.wert;
    const name = document.createElement("span");
    name.className = "dg-tooltip-name";
    name.textContent = z.name;
    zeile.append(schluessel, wert, name);
    dgTip.appendChild(zeile);
  }
  dgTip.style.display = "block";
  const breite = dgTip.offsetWidth, hoehe = dgTip.offsetHeight;
  let x = ereignis.clientX + 14, y = ereignis.clientY - hoehe - 10;
  if (x + breite > window.innerWidth - 8) x = ereignis.clientX - breite - 14;
  if (y < 8) y = ereignis.clientY + 16;
  dgTip.style.left = `${x}px`;
  dgTip.style.top = `${y}px`;
}
function dgTooltipWeg() { if (dgTip) dgTip.style.display = "none"; }

// ---- Karte: Titel, Legende, Diagramm, umschaltbare Tabellenansicht ----
function diagrammKarte(eltern, { titel, untertitel, legende, tabelle, breit }) {
  const karte = document.createElement("figure");
  karte.className = "kpi-karte" + (breit ? " breit" : "");
  const kopf = document.createElement("div");
  kopf.className = "kpi-karte-kopf";
  const texte = document.createElement("div");
  const h = document.createElement("h3");
  h.textContent = titel;
  texte.appendChild(h);
  if (untertitel) {
    const u = document.createElement("div");
    u.className = "kpi-untertitel";
    u.textContent = untertitel;
    texte.appendChild(u);
  }
  kopf.appendChild(texte);
  const umschalter = document.createElement("button");
  umschalter.type = "button";
  umschalter.className = "sekundaer klein-knopf";
  umschalter.textContent = "Tabelle";
  kopf.appendChild(umschalter);
  karte.appendChild(kopf);

  if (legende && legende.length > 1) {
    const leg = document.createElement("div");
    leg.className = "dg-legende";
    for (const l of legende) {
      const eintrag = document.createElement("span");
      const muster = document.createElement("span");
      muster.className = "dg-schluessel " + (l.art || "flaeche") + (l.gestrichelt ? " gestrichelt" : "");
      muster.style.background = l.farbe;
      if (l.gestrichelt) muster.style.color = l.farbe;
      const name = document.createElement("span");
      name.textContent = l.name;
      eintrag.append(muster, name);
      leg.appendChild(eintrag);
    }
    karte.appendChild(leg);
  }
  const plot = document.createElement("div");
  plot.className = "dg-plot";
  karte.appendChild(plot);

  const tab = document.createElement("div");
  tab.className = "dg-tabelle";
  tab.hidden = true;
  if (tabelle) {
    const t = document.createElement("table");
    t.className = "liste";
    const thead = document.createElement("thead");
    const kr = document.createElement("tr");
    tabelle.kopf.forEach((k, i) => {
      const th = document.createElement("th");
      th.textContent = k;
      if (i > 0) th.className = "zahl";
      kr.appendChild(th);
    });
    thead.appendChild(kr);
    const tbody = document.createElement("tbody");
    for (const zeile of tabelle.zeilen) {
      const tr = document.createElement("tr");
      zeile.forEach((z, i) => {
        const td = document.createElement("td");
        td.textContent = z;
        if (i > 0) td.className = "zahl";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    t.append(thead, tbody);
    tab.appendChild(t);
  }
  karte.appendChild(tab);
  umschalter.addEventListener("click", () => {
    const zeigeTabelle = tab.hidden;
    tab.hidden = !zeigeTabelle;
    plot.hidden = zeigeTabelle;
    umschalter.textContent = zeigeTabelle ? "Diagramm" : "Tabelle";
  });
  eltern.appendChild(karte);
  return plot;
}

function dgLeer(plot, text) {
  const div = document.createElement("div");
  div.className = "dg-leer";
  div.textContent = text;
  plot.replaceChildren(div);
}

// Gemeinsame y-Achse mit Haarlinien-Raster
function dgYAchse(svg, ticks, y, links, rechts, format) {
  for (const t of ticks) {
    dgEl("line", { x1: links, x2: rechts, y1: y(t), y2: y(t),
      stroke: t === 0 ? DG_FARBE.achse : DG_FARBE.gitter, "stroke-width": 1, "shape-rendering": "crispEdges" }, svg);
    dgText(svg, links - 8, y(t) + 4, format(t), { "text-anchor": "end", class: "dg-ziffern" });
  }
}

function dgXBeschriftung(svg, kategorien, xMitte, yPos, platz, unter) {
  const breiteste = Math.max(...kategorien.map(k => dgTextbreite(k)));
  const jede = Math.max(1, Math.ceil((breiteste + 10) / Math.max(platz, 1)));
  kategorien.forEach((k, i) => {
    if (i % jede !== 0 && i !== kategorien.length - 1) return;
    if (i === kategorien.length - 1 && i % jede !== 0 && (i % jede) < jede / 2) return;
    dgText(svg, xMitte(i), yPos, k, { "text-anchor": "middle" });
    if (unter && unter[i]) dgText(svg, xMitte(i), yPos + 14, unter[i], { "text-anchor": "middle", "font-size": 10.5 });
  });
}

// ---- Liniendiagramm (Fadenkreuz-Tooltip über alle Reihen) ----
// reihen: [{name, farbe, werte: [zahl|null], prognoseAb: index|null}]
function linienDiagramm(plot, { kategorien, reihen, hoehe = 250, format = euroKompakt, tooltipFormat = euroVoll }) {
  plot.replaceChildren();
  const alle = reihen.flatMap(r => r.werte).filter(v => v !== null && v !== undefined);
  if (!alle.length) { dgLeer(plot, "Keine Werte im Zeitraum."); return; }
  const breite = Math.max(plot.clientWidth, 320);
  const ticks = dgTicks(Math.min(0, ...alle), Math.max(0, ...alle));
  const links = Math.max(...ticks.map(t => dgTextbreite(format(t)))) + 14;
  const rechts = breite - 64, oben = 10, unten = hoehe - 28;
  const svg = dgEl("svg", { width: breite, height: hoehe, role: "img" }, plot);
  const y = v => unten - (v - ticks[0]) / (ticks[ticks.length - 1] - ticks[0]) * (unten - oben);
  const schritt = kategorien.length > 1 ? (rechts - links) / (kategorien.length - 1) : 0;
  const x = i => links + i * schritt;
  dgYAchse(svg, ticks, y, links, rechts, format);
  dgXBeschriftung(svg, kategorien, x, hoehe - 8, schritt);

  const pfad = (werte, von, bis) => {
    let d = "";
    for (let i = von; i <= bis; i++) {
      if (werte[i] === null || werte[i] === undefined) continue;
      d += (d ? "L" : "M") + x(i) + "," + y(werte[i]);
    }
    return d;
  };
  const endPunkte = [];
  for (const r of reihen) {
    const letzter = r.werte.reduce((acc, v, i) => (v === null || v === undefined ? acc : i), -1);
    if (letzter < 0) continue;
    const grenze = r.prognoseAb === null || r.prognoseAb === undefined ? letzter : Math.min(r.prognoseAb - 1, letzter);
    const gemeinsam = { fill: "none", stroke: r.farbe, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" };
    if (grenze >= 0) dgEl("path", { d: pfad(r.werte, 0, grenze), ...gemeinsam }, svg);
    if (grenze < letzter) {
      dgEl("path", { d: pfad(r.werte, Math.max(grenze, 0), letzter), ...gemeinsam, "stroke-dasharray": "5 4" }, svg);
    }
    dgEl("circle", { cx: x(letzter), cy: y(r.werte[letzter]), r: 4, fill: r.farbe, stroke: "#fff", "stroke-width": 2 }, svg);
    endPunkte.push({ y: y(r.werte[letzter]), text: format(r.werte[letzter]), x: x(letzter) });
  }
  // Endwerte direkt beschriften, solange sie sich nicht überlappen
  const sortiert = [...endPunkte].sort((a, b) => a.y - b.y);
  const kollision = sortiert.some((p, i) => i > 0 && p.y - sortiert[i - 1].y < 14);
  if (!kollision) {
    for (const p of endPunkte) dgText(svg, p.x + 9, p.y + 4, p.text, { fill: DG_FARBE.text2, class: "dg-ziffern" });
  }

  // Fadenkreuz
  const linie = dgEl("line", { y1: oben, y2: unten, stroke: DG_FARBE.achse, "stroke-width": 1, visibility: "hidden" }, svg);
  const marker = reihen.map(r => dgEl("circle", { r: 4, fill: r.farbe, stroke: "#fff", "stroke-width": 2, visibility: "hidden" }, svg));
  const flaeche = dgEl("rect", { x: links - schritt / 2, y: oben, width: rechts - links + schritt, height: unten - oben, fill: "transparent", tabindex: 0 }, svg);
  const zeige = (i, ereignis) => {
    linie.setAttribute("x1", x(i)); linie.setAttribute("x2", x(i));
    linie.setAttribute("visibility", "visible");
    reihen.forEach((r, j) => {
      const v = r.werte[i];
      if (v === null || v === undefined) { marker[j].setAttribute("visibility", "hidden"); return; }
      marker[j].setAttribute("cx", x(i)); marker[j].setAttribute("cy", y(v));
      marker[j].setAttribute("visibility", "visible");
    });
    dgTooltip(ereignis, kategorien[i], reihen.map(r => ({
      farbe: r.farbe, art: "linie",
      wert: tooltipFormat(r.werte[i]),
      name: r.name + (r.prognoseAb !== null && r.prognoseAb !== undefined && i >= r.prognoseAb ? " (Prognose)" : ""),
    })));
  };
  flaeche.addEventListener("pointermove", (e) => {
    const box = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(kategorien.length - 1, Math.round((e.clientX - box.left - links) / (schritt || 1))));
    zeige(i, e);
  });
  flaeche.addEventListener("pointerleave", () => {
    linie.setAttribute("visibility", "hidden");
    marker.forEach(m => m.setAttribute("visibility", "hidden"));
    dgTooltipWeg();
  });
}

// ---- Säulendiagramm (gruppiert; divergierend = Farbe nach Vorzeichen) ----
// reihen: [{name, farbe, werte}] · beschriften: Index der direkt beschrifteten Reihe
function saeulenDiagramm(plot, { kategorien, unterLabels, reihen, divergierend = false, beschriften = null,
                                  hoehe = 250, format = euroKompakt, tooltipFormat = euroVoll }) {
  plot.replaceChildren();
  const alle = reihen.flatMap(r => r.werte).filter(v => v !== null && v !== undefined);
  if (!alle.length) { dgLeer(plot, "Keine Werte im Zeitraum."); return; }
  const breite = Math.max(plot.clientWidth, 320);
  // Platz für Wertbeschriftungen an den Säulenenden einplanen
  const luft = beschriften === null ? 1 : 1.14;
  const ticks = dgTicks(Math.min(0, ...alle) * luft, Math.max(0, ...alle) * luft);
  const links = Math.max(...ticks.map(t => dgTextbreite(format(t)))) + 14;
  const unterHoehe = unterLabels ? 14 : 0;
  const rechts = breite - 10, oben = 16, unten = hoehe - 28 - unterHoehe;
  const svg = dgEl("svg", { width: breite, height: hoehe, role: "img" }, plot);
  const y = v => unten - (v - ticks[0]) / (ticks[ticks.length - 1] - ticks[0]) * (unten - oben);
  const band = (rechts - links) / kategorien.length;
  const xMitte = i => links + band * (i + 0.5);
  dgYAchse(svg, ticks, y, links, rechts, format);
  dgXBeschriftung(svg, kategorien, xMitte, unten + 18, band, unterLabels);

  const k = reihen.length;
  const b = Math.max(4, Math.min(24, (band * 0.72 - (k - 1) * 2) / k));
  const gruppe = k * b + (k - 1) * 2;
  reihen.forEach((r, j) => {
    r.werte.forEach((v, i) => {
      if (v === null || v === undefined) return;
      const x0 = xMitte(i) - gruppe / 2 + j * (b + 2);
      const y0 = y(Math.max(v, 0)), h = Math.abs(y(v) - y(0));
      const farbe = divergierend ? (v >= 0 ? DG_FARBE.blau : DG_FARBE.rot) : r.farbe;
      const saeule = dgEl("path", { d: dgBalkenPfad(x0, y0, b, h, v >= 0 ? "oben" : "unten"), fill: farbe, tabindex: 0, class: "dg-marke" }, svg);
      const treffer = dgEl("rect", { x: x0 - 1, y: oben, width: b + 2, height: unten - oben, fill: "transparent" }, svg);
      const zeige = (e) => dgTooltip(e, kategorien[i] + (unterLabels && unterLabels[i] ? ` (${unterLabels[i]})` : ""),
        reihen.map((rr, jj) => ({
          farbe: divergierend ? (rr.werte[i] >= 0 ? DG_FARBE.blau : DG_FARBE.rot) : rr.farbe,
          art: "flaeche", wert: tooltipFormat(rr.werte[i]), name: rr.name, aktiv: jj === j,
        })));
      for (const el of [saeule, treffer]) {
        el.addEventListener("pointermove", (e) => { saeule.classList.add("aktiv"); zeige(e); });
        el.addEventListener("pointerleave", () => { saeule.classList.remove("aktiv"); dgTooltipWeg(); });
      }
      if (beschriften === j && band >= dgTextbreite(format(v)) + 4) {
        dgText(svg, x0 + b / 2, v >= 0 ? y0 - 5 : y0 + h + 13, format(v),
          { "text-anchor": "middle", fill: DG_FARBE.text2, class: "dg-ziffern" });
      }
    });
  });
}

// ---- Balkendiagramm (horizontal, eine Reihe; divergierend um die Null) ----
function balkenDiagramm(plot, { kategorien, werte, farbe = DG_FARBE.blau, divergierend = false, zusatz,
                                 tooltipZeilen, format = euroKompakt, tooltipFormat = euroVoll }) {
  plot.replaceChildren();
  if (!werte.length) { dgLeer(plot, "Keine Werte im Zeitraum."); return; }
  const breite = Math.max(plot.clientWidth, 320);
  const zeile = 28, dicke = 16, oben = 6;
  const hoehe = oben + zeile * kategorien.length + 6;
  const labelBreite = Math.min(Math.max(...kategorien.map(k => dgTextbreite(k))) + 12, breite * 0.4);
  const beschriftung = werte.map((v, i) => format(v) + (zusatz ? zusatz[i] : ""));
  const reserve = Math.max(...beschriftung.map(t => dgTextbreite(t))) + 10;
  const min = Math.min(0, ...werte), max = Math.max(0, ...werte);
  const links = labelBreite + (min < 0 ? reserve : 0), rechts = breite - (max > 0 ? reserve : 6);
  const skala = (rechts - links) / ((max - min) || 1);
  const x = v => links + (v - min) * skala;
  const svg = dgEl("svg", { width: breite, height: hoehe, role: "img" }, plot);
  dgEl("line", { x1: x(0), x2: x(0), y1: oben - 2, y2: hoehe - 4, stroke: DG_FARBE.achse, "stroke-width": 1, "shape-rendering": "crispEdges" }, svg);

  kategorien.forEach((k, i) => {
    const v = werte[i];
    const yMitte = oben + zeile * i + zeile / 2;
    let name = k;
    while (dgTextbreite(name) > labelBreite - 12 && name.length > 4) name = name.slice(0, -2);
    const nameEl = dgText(svg, 0, yMitte + 4, name === k ? k : name + "…", { fill: DG_FARBE.text2 });
    if (name !== k) dgEl("title", {}, nameEl).textContent = k;
    const x0 = Math.min(x(0), x(v)), w = Math.abs(x(v) - x(0));
    const f = divergierend ? (v >= 0 ? DG_FARBE.blau : DG_FARBE.rot) : farbe;
    const balken = dgEl("path", { d: dgBalkenPfad(x0, yMitte - dicke / 2, w, dicke, v >= 0 ? "rechts" : "links"), fill: f, tabindex: 0, class: "dg-marke" }, svg);
    const textX = v >= 0 ? x(v) + 6 : x(v) - 6;
    dgText(svg, textX, yMitte + 4, beschriftung[i], { "text-anchor": v >= 0 ? "start" : "end", fill: DG_FARBE.text2, class: "dg-ziffern" });
    const treffer = dgEl("rect", { x: 0, y: yMitte - zeile / 2, width: breite, height: zeile, fill: "transparent" }, svg);
    const zeige = (e) => dgTooltip(e, k, tooltipZeilen ? tooltipZeilen(i) : [{ farbe: f, art: "flaeche", wert: tooltipFormat(v), name: "" }]);
    for (const el of [balken, treffer]) {
      el.addEventListener("pointermove", (e) => { balken.classList.add("aktiv"); zeige(e); });
      el.addEventListener("pointerleave", () => { balken.classList.remove("aktiv"); dgTooltipWeg(); });
    }
  });
}
