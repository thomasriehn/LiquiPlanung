"use strict";
// Durchsuchbare Kontoauswahl mit Kategorie-Überschriften (BWA-Gruppen) –
// gemeinsam für PDF-Import, Ist-Buchungen, offene Posten und
// Dauerschuldverhältnisse.

// Gruppen für die Auswahl aus der Antwort von /api/mandanten/{id}/konten
function kontoGruppenAus(daten, { nurSachkonten = true } = {}) {
  const konten = daten.konten.filter(k => k.aktiv
    && (!nurSachkonten || (k.typ !== "BANK" && k.typ !== "KASSE")));
  const gruppen = daten.gruppen
    .slice().sort((a, b) => (a.sortierung || 0) - (b.sortierung || 0))
    .map(g => ({
      name: g.name,
      konten: konten.filter(k => k.gruppe_id === g.id).sort((a, b) => a.nummer.localeCompare(b.nummer)),
    }))
    .filter(g => g.konten.length);
  const ohne = konten.filter(k => !k.gruppe_id).sort((a, b) => a.nummer.localeCompare(b.nummer));
  if (ohne.length) gruppen.push({ name: "Ohne Gruppe", konten: ohne });
  return gruppen;
}

// gruppen:      [{name, konten: [{id, nummer, bezeichnung}]}]
// beiAuswahl:   (konto | null, aufteilen: bool) => void
// name:         Formularmodus – verstecktes Feld mit der Konto-ID
// leerText:     Eintrag zum Leeren der Auswahl (z. B. „– kein Konto –“)
// konten:       zusätzliche Konten zum Anzeigen eines gesetzten Werts
function kontoAuswahl({ gruppen, beiAuswahl = null, mitAufteilen = false, name = null,
                        leerText = null, konten = [], platzhalter = "Konto suchen …" }) {
  const alle = new Map();
  for (const k of konten) alle.set(k.id, k);
  for (const g of gruppen) for (const k of g.konten) alle.set(k.id, k);
  const text = (k) => `${k.nummer} ${k.bezeichnung}`;

  const huelle = document.createElement("div");
  huelle.className = "kombo";
  const eingabe = document.createElement("input");
  eingabe.placeholder = platzhalter;
  eingabe.autocomplete = "off";
  const liste = document.createElement("div");
  liste.className = "kombo-liste";
  liste.hidden = true;
  huelle.append(eingabe, liste);
  let versteckt = null;
  if (name) {
    versteckt = document.createElement("input");
    versteckt.type = "hidden";
    versteckt.name = name;
    huelle.appendChild(versteckt);
  }
  let wert = null;
  let markiert = -1;

  function setzeWert(id) {
    wert = id === null || id === undefined || id === "" ? null : Number(id);
    const k = wert === null ? null : alle.get(wert);
    eingabe.value = k ? text(k) : (wert === null ? "" : `Konto #${wert}`);
    if (versteckt) versteckt.value = wert === null ? "" : String(wert);
  }

  function eintraege() { return [...liste.querySelectorAll(".kombo-eintrag")]; }

  function fuelle() {
    const f = eingabe.value.trim().toLowerCase();
    const gewaehlt = wert !== null && alle.get(wert);
    // steht noch der gewählte Kontoname im Feld, alle Konten zeigen
    const filter = gewaehlt && eingabe.value === text(gewaehlt) ? "" : f;
    liste.replaceChildren();
    const knopf = (klasse, inhalt, daten) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "kombo-eintrag " + klasse;
      Object.assign(b.dataset, daten);
      b.append(...inhalt);
      liste.appendChild(b);
    };
    if (mitAufteilen) {
      const s = document.createElement("strong");
      s.textContent = "Auf mehrere Konten aufteilen …";
      knopf("", [s], { aufteilen: "1" });
    }
    if (leerText) knopf("kombo-leer-eintrag", [leerText], { leeren: "1" });
    let gefunden = 0;
    for (const g of gruppen) {
      const treffer = g.konten.filter(k => !filter || text(k).toLowerCase().includes(filter));
      if (!treffer.length) continue;
      const kopf = document.createElement("div");
      kopf.className = "kombo-gruppe";
      kopf.textContent = g.name;
      liste.appendChild(kopf);
      for (const k of treffer) {
        const nr = document.createElement("span");
        nr.className = "konto-nr";
        nr.textContent = k.nummer;
        knopf(k.id === wert ? "gewaehlt" : "", [nr, k.bezeichnung], { id: String(k.id) });
        gefunden++;
      }
    }
    if (!gefunden) {
      const leer = document.createElement("div");
      leer.className = "kombo-leer";
      leer.textContent = "Kein Konto gefunden.";
      liste.appendChild(leer);
    }
    markiert = -1;
  }

  function markiere(i) {
    const alleEintraege = eintraege();
    if (!alleEintraege.length) return;
    markiert = (i + alleEintraege.length) % alleEintraege.length;
    alleEintraege.forEach((e, j) => e.classList.toggle("markiert", j === markiert));
    alleEintraege[markiert].scrollIntoView({ block: "nearest" });
  }

  function waehle(eintrag) {
    liste.hidden = true;
    if (eintrag.dataset.aufteilen) { if (beiAuswahl) beiAuswahl(null, true); return; }
    if (eintrag.dataset.leeren) { setzeWert(null); if (beiAuswahl) beiAuswahl(null, false); return; }
    setzeWert(eintrag.dataset.id);
    if (beiAuswahl) beiAuswahl(alle.get(wert), false);
  }

  function schliessen() {
    liste.hidden = true;
    setzeWert(wert);     // angefangene Suche verwerfen, Auswahl wieder anzeigen
  }

  eingabe.addEventListener("focus", () => { fuelle(); liste.hidden = false; eingabe.select(); });
  eingabe.addEventListener("input", () => { fuelle(); liste.hidden = false; });
  eingabe.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); liste.hidden = false; markiere(markiert + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); markiere(markiert - 1); }
    else if (e.key === "Enter") {
      const ziel = eintraege()[markiert >= 0 ? markiert : 0];
      if (!liste.hidden && ziel) { e.preventDefault(); waehle(ziel); }
    } else if (e.key === "Escape") { schliessen(); }
  });
  document.addEventListener("click", (e) => {
    if (huelle.isConnected && !huelle.contains(e.target) && !liste.hidden) schliessen();
  });
  liste.addEventListener("click", (e) => {
    const eintrag = e.target.closest(".kombo-eintrag");
    if (eintrag) waehle(eintrag);
  });

  Object.defineProperty(huelle, "wert", { get: () => wert });
  huelle.setzeWert = setzeWert;
  return huelle;
}
