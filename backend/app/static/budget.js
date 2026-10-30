"use strict";
// Budgetplanung im Aufbau des 13-Wochen-Plans: BWA-Gruppen mit Konten,
// Tages-/Wochen-/Monatsspalten, Summenzeilen; Klick auf eine Kontozelle
// öffnet den Eingabedialog (Gültigkeit, Betrag, Aufteilung, Fortschreibung).

let raster = null;
const zu = new Set();                    // eingeklappte Gruppen
const NUR_BUDGET_SCHLUESSEL = `liqui.budget.nurBudget.${MANDANT_ID}`;
const WOCHENTAG_NAME = { WTAG_MO: "montags", WTAG_DI: "dienstags", WTAG_MI: "mittwochs",
                         WTAG_DO: "donnerstags", WTAG_FR: "freitags" };
const PROFIL_TEXT = {
  GLEICH: "gleichmäßig auf die Bankarbeitstage", MONATSANFANG: "zum Monatsanfang",
  MONATSMITTE: "zur Monatsmitte", MONATSENDE: "zum Monatsende",
  WTAG_MO: "wöchentlich montags", WTAG_DI: "wöchentlich dienstags", WTAG_MI: "wöchentlich mittwochs",
  WTAG_DO: "wöchentlich donnerstags", WTAG_FR: "wöchentlich freitags",
};
const AUFTEILUNG_TEXT = { KW: "je KW", TAG: "je Tag", KONTO: "laut Konto", "": "laut Konto" };

const ansichtFeld = document.getElementById("ansicht");
ansichtFeld.value = ANSICHT;
const gemerkterStart = startGemerkt(MANDANT_ID);
if (gemerkterStart) document.getElementById("start").value = gemerkterStart;
try { document.getElementById("nur-budget").checked = localStorage.getItem(NUR_BUDGET_SCHLUESSEL) === "1"; }
catch (e) { /* ohne Speicher: Standard */ }

async function lade() {
  const start = document.getElementById("start").value;
  startMerken(MANDANT_ID, start);
  let url = `/api/mandanten/${MANDANT_ID}/budget-raster?ansicht=${ansichtFeld.value}` +
    `&wochen=${parseInt(document.getElementById("wochen").value) || 13}`;
  if (start) url += `&start=${start}`;
  try { raster = await api("GET", url); } catch (e) { toast(e.message); return; }
  if (!start) document.getElementById("start").value = wochenMontag(raster.heute);
  zeichne();
}

function wochenMontag(iso) {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function zelle(inhalt, klassen = []) {
  const td = document.createElement("td");
  td.innerHTML = inhalt;
  for (const k of klassen) if (k) td.classList.add(k);
  return td;
}

function spaltenKlassen(p) {
  const kl = [];
  if (p.banktag === false) kl.push("we");
  if (p.von <= raster.heute && raster.heute <= p.bis) kl.push("heute-spalte");
  return kl;
}

function zeichne() {
  const tabelle = document.getElementById("matrix");
  tabelle.innerHTML = "";
  const nurBudget = document.getElementById("nur-budget").checked;
  const perioden = raster.perioden;

  // Kopf (Tagesansicht zweizeilig mit Kalenderwochen wie im Plan)
  const kopf = document.createElement("thead");
  const th = (inhalt, klassen = []) => {
    const el = document.createElement("th");
    el.innerHTML = inhalt;
    for (const k of klassen) if (k) el.classList.add(k);
    return el;
  };
  if (raster.ansicht === "tage") {
    const z1 = document.createElement("tr");
    z1.appendChild(th("Konto / BWA-Zeile", ["zeilkopf"]));
    let i = 0;
    while (i < perioden.length) {
      const woche = perioden[i].woche;
      let n = 0;
      while (i + n < perioden.length && perioden[i + n].woche === woche) n++;
      const el = th(`${woche} (${datumDe(perioden[i].von)}–${datumDe(perioden[i + n - 1].von)})`);
      el.colSpan = n;
      z1.appendChild(el);
      i += n;
    }
    const z2 = document.createElement("tr");
    z2.appendChild(th("", ["zeilkopf"]));
    for (const p of perioden) z2.appendChild(th(`${p.label}<br>${p.unter}`, spaltenKlassen(p)));
    kopf.append(z1, z2);
  } else {
    const z = document.createElement("tr");
    z.appendChild(th("Konto / BWA-Zeile", ["zeilkopf"]));
    for (const p of perioden) {
      z.appendChild(th(`${esc(p.label)}${p.unter ? `<br><span class="klein">${esc(p.unter)}</span>` : ""}`,
        spaltenKlassen(p)));
    }
    kopf.appendChild(z);
  }
  tabelle.appendChild(kopf);

  const koerper = document.createElement("tbody");
  let ausgeblendet = 0;
  for (const g of raster.gruppen) {
    const konten = g.konten.filter(k => !nurBudget
      || Object.keys(k.werte).length || Object.keys(k.erfasst).length);
    ausgeblendet += g.konten.length - konten.length;
    if (!konten.length) continue;
    const schluessel = `G:${g.id}`;
    const tr = document.createElement("tr");
    tr.className = "gruppe" + (zu.has(schluessel) ? " zu" : "");
    tr.appendChild(zelle(esc(g.name), ["zeilkopf"]));
    for (const p of perioden) {
      const summe = konten.reduce((s, k) => s + (k.werte[p.id] || 0), 0);
      tr.appendChild(zelle(Math.abs(summe) > 0.004 ? eur(summe) : "", spaltenKlassen(p)));
    }
    tr.addEventListener("click", () => {
      if (zu.has(schluessel)) zu.delete(schluessel); else zu.add(schluessel);
      zeichne();
    });
    koerper.appendChild(tr);
    if (zu.has(schluessel)) continue;
    for (const k of konten) koerper.appendChild(kontoZeile(k));
  }

  // Summen
  const summenZeile = (name, werte) => {
    const tr = document.createElement("tr");
    tr.className = "summe";
    tr.appendChild(zelle(name, ["zeilkopf"]));
    for (const p of perioden) {
      const v = werte(p.id);
      tr.appendChild(zelle(Math.abs(v) > 0.004 ? eur(v) : "", [...spaltenKlassen(p), v < -0.004 ? "negativ" : ""]));
    }
    koerper.appendChild(tr);
  };
  const ein = id => raster.summen.EIN[id] || 0, aus = id => raster.summen.AUS[id] || 0;
  summenZeile("Einzahlungen gesamt (netto)", ein);
  summenZeile("Auszahlungen gesamt (netto)", aus);
  summenZeile("Saldo Einzahlungen − Auszahlungen", id => ein(id) - aus(id));
  tabelle.appendChild(koerper);

  document.getElementById("info").textContent =
    `Zeitraum ${datumVollDe(raster.von)} – ${datumVollDe(raster.bis)}` +
    (ausgeblendet ? ` · ${ausgeblendet} Konten ohne Budget ausgeblendet` : "");
}

function kontoZeile(k) {
  const tr = document.createElement("tr");
  tr.appendChild(zelle(`&nbsp;&nbsp;&nbsp;<span class="konto-nr">${esc(k.nummer)}</span>${esc(k.bezeichnung)}`,
    ["zeilkopf"]));
  tr.firstChild.title = `Verteilungsprofil: ${PROFIL_TEXT[k.verteilung] || k.verteilung}` +
    (k.ust_satz ? ` · USt ${k.ust_satz} %` : "");
  for (const p of raster.perioden) {
    const wirksam = k.werte[p.id];
    const erfasst = k.erfasst[p.id];
    const herkunft = k.fortgeschrieben[p.id];
    const klassen = spaltenKlassen(p);
    let titel = wirksam !== undefined ? `${eur(wirksam)} € netto` : "kein Budget";
    if (erfasst !== undefined) {
      klassen.push("erfasst");
      titel += " · auf dieser Ebene erfasst";
      if (Math.abs((wirksam || 0) - erfasst) > 0.005) {
        klassen.push("abweichend");
        titel += ` (erfasst ${eur(erfasst)} €, durch Wochen-/Tageswerte übersteuert)`;
      }
    } else if (herkunft) {
      klassen.push("fortgeschrieben");
      titel += ` · fortgeschrieben aus ${herkunft}`;
    }
    if (!LESER && p.banktag !== false) klassen.push("klickbar");
    const td = zelle(wirksam !== undefined ? eur(wirksam) : (erfasst === 0 ? "0" : ""), klassen);
    td.title = titel;
    if (!LESER && p.banktag !== false) td.addEventListener("click", () => dialogOeffnen(k, p));
    tr.appendChild(td);
  }
  return tr;
}

// ------------------------------------------------------------ Eingabedialog

const dialog = document.getElementById("budget-dialog");
const form = document.getElementById("bd-form");
let dz = null;                           // { konto, periode, info, ebene }

function wochentagLang(iso) {
  return ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"][new Date(iso + "T12:00:00").getDay()];
}

async function dialogOeffnen(konto, periode) {
  let info;
  try {
    info = await api("GET", `/api/mandanten/${MANDANT_ID}/budget-zelle?konto_id=${konto.id}&tag=${periode.von}`);
  } catch (e) { toast(e.message); return; }
  const ebene = { monate: "monat", wochen: "woche", tage: "tag" }[raster.ansicht];
  dz = { konto, periode, info, ebene };
  document.getElementById("bd-titel").textContent = `${konto.nummer} ${konto.bezeichnung}`;
  const zeitraum = { monat: info.monat.label,
                     woche: `${info.woche.label} (${datumVollDe(info.woche.von)} – ${datumVollDe(info.woche.bis)})`,
                     tag: `${wochentagLang(info.tag.periode)}, ${datumVollDe(info.tag.periode)}` };
  document.getElementById("bd-zeitraum").textContent = zeitraum[ebene];
  const profil = info.konto.verteilung;
  document.getElementById("bd-kw-text").textContent = WOCHENTAG_NAME[profil]
    ? `eine Zahlung je Kalenderwoche, ${WOCHENTAG_NAME[profil]}`
    : "eine Zahlung je Kalenderwoche am ersten Bankarbeitstag";
  document.getElementById("bd-profil-text").textContent = `Verteilungsprofil: ${PROFIL_TEXT[profil] || profil}`;

  // Ebenen der Ansicht: Monat immer, KW in Wochen-/Tagesansicht, Tag in der Tagesansicht
  const ebenen = [["monat", `Monat ${info.monat.label}`]];
  if (raster.ansicht !== "monate") ebenen.push(["woche", `nur ${info.woche.label}`]);
  if (raster.ansicht === "tage") ebenen.push(["tag", `nur ${datumVollDe(info.tag.periode)}`]);
  document.getElementById("bd-ebenen").innerHTML = ebenen.map(([wert, text]) =>
    `<label><input type="radio" name="ebene" value="${wert}"${wert === ebene ? " checked" : ""}> ${esc(text)}</label>`).join("");
  form.querySelectorAll('input[name="ebene"]').forEach(r => r.addEventListener("change", () => {
    dz.ebene = r.value;
    ebeneZeigen();
  }));
  wirksamZeigen();
  ebeneZeigen();
  if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  document.getElementById("bd-betrag").focus();
}

function wirksamZeigen() {
  const i = dz.info;
  const zeilen = [];
  const m = i.monat;
  if (m.erfasst) {
    zeilen.push(`Monat ${esc(m.label)}: <strong>${eur(m.erfasst.betrag)} €</strong> erfasst, ` +
      `${AUFTEILUNG_TEXT[m.erfasst.aufteilung || ""]}${m.erfasst.fortschreiben ? ", fortgeschrieben" : ""}`);
  } else if (m.fortgeschrieben_aus) {
    zeilen.push(`Monat ${esc(m.label)}: <em>${eur(m.fortgeschrieben_aus.betrag)} € fortgeschrieben aus ` +
      `${esc(m.fortgeschrieben_aus.label)}</em> (${AUFTEILUNG_TEXT[m.fortgeschrieben_aus.aufteilung || ""]})`);
  } else {
    zeilen.push(`Monat ${esc(m.label)}: kein Monatsbudget`);
  }
  if (i.woche.erfasst) {
    zeilen.push(`${esc(i.woche.label)}: <strong>${eur(i.woche.erfasst.betrag)} €</strong> als Wochenwert erfasst ` +
      `(${AUFTEILUNG_TEXT[i.woche.erfasst.aufteilung || ""]})`);
  }
  if (raster.ansicht === "tage" && i.tag.erfasst) {
    zeilen.push(`${datumVollDe(i.tag.periode)}: <strong>${eur(i.tag.erfasst.betrag)} €</strong> als Tageswert erfasst`);
  }
  zeilen.push(`Wirksam: ${esc(m.label)} ${eur(m.wirksam)} € · ${esc(i.woche.label)} ${eur(i.woche.wirksam)} €` +
    (raster.ansicht === "tage" ? ` · Tag ${eur(i.tag.wirksam)} €` : "") + " (netto)");
  document.getElementById("bd-wirksam").innerHTML = zeilen.map(z => `<div>${z}</div>`).join("");
}

function ebeneZeigen() {
  const i = dz.info, ebene = dz.ebene;
  const erfasst = i[ebene].erfasst;
  const betrag = document.getElementById("bd-betrag");
  betrag.value = erfasst ? erfasst.betrag : "";
  betrag.placeholder = ebene === "monat" && i.monat.fortgeschrieben_aus
    ? `${eur(i.monat.fortgeschrieben_aus.betrag)} (fortgeschrieben)` : "";
  document.getElementById("bd-aufteilung-zeile").style.display = ebene === "tag" ? "none" : "";
  // Standard: je KW und Fortschreibung an – gespeicherte Einstellungen gelten,
  // sobald ein Wert einmal über diesen Dialog erfasst wurde (ältere Werte ohne
  // Aufteilung erhalten die Standardvorgaben)
  const eigeneWahl = erfasst && erfasst.aufteilung;
  const aufteilung = eigeneWahl ? erfasst.aufteilung : "KW";
  form.querySelectorAll('input[name="aufteilung"]').forEach(r => { r.checked = r.value === aufteilung; });
  document.getElementById("bd-fortschreiben-zeile").style.display = ebene === "monat" ? "" : "none";
  document.getElementById("bd-fortschreiben").checked = eigeneWahl ? !!erfasst.fortschreiben : true;
  const spaetere = i.monat.spaetere || [];
  document.getElementById("bd-spaetere-zeile").style.display = ebene === "monat" && spaetere.length ? "" : "none";
  document.getElementById("bd-spaetere").checked = false;
  document.getElementById("bd-spaetere-text").textContent = spaetere.length
    ? `(erfasst: ${spaetere.map(s => `${s.slice(5, 7)}/${s.slice(0, 4)}`).join(", ")})` : "";
  document.getElementById("bd-entfernen").style.display = erfasst ? "" : "none";
}

async function speichern(entfernen) {
  const ebene = dz.ebene;
  const wert = document.getElementById("bd-betrag").value;
  if (!entfernen && wert === "") { toast("Bitte einen Betrag eingeben (0 = kein Budget)."); return; }
  const aufteilungRadio = form.querySelector('input[name="aufteilung"]:checked');
  const daten = {
    konto_id: dz.konto.id, ebene, periode: dz.info[ebene].periode,
    betrag_netto: entfernen ? null : parseFloat(wert),
    aufteilung: ebene === "tag" || !aufteilungRadio ? null : aufteilungRadio.value,
    fortschreiben: ebene === "monat" && document.getElementById("bd-fortschreiben").checked,
    spaetere_entfernen: ebene === "monat" && document.getElementById("bd-spaetere").checked,
  };
  try {
    await api("PUT", `/api/mandanten/${MANDANT_ID}/budget-zelle`, daten);
    toast(entfernen ? "Eintrag entfernt." : "Budget gespeichert.", false);
    dialogSchliessen();
    await lade();
  } catch (e) { toast(e.message); }
}

function dialogSchliessen() {
  if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
}

form.addEventListener("submit", (e) => { e.preventDefault(); speichern(false); });
document.getElementById("bd-entfernen").addEventListener("click", () => {
  if (confirm("Diesen Eintrag entfernen?")) speichern(true);
});
document.getElementById("bd-abbrechen").addEventListener("click", dialogSchliessen);
document.getElementById("bd-schliessen").addEventListener("click", dialogSchliessen);

ansichtFeld.addEventListener("change", () => { ansichtMerken(ansichtFeld.value); lade(); });
document.getElementById("nur-budget").addEventListener("change", (e) => {
  try { localStorage.setItem(NUR_BUDGET_SCHLUESSEL, e.target.checked ? "1" : "0"); } catch (err) { /* egal */ }
  if (raster) zeichne();
});
document.getElementById("laden").addEventListener("click", lade);
lade();
