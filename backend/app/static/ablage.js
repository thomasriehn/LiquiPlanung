"use strict";
// Auswahldialoge der Belegablage – gemeinsam für Einstellungen und Bestellungen:
//   sharepointOrdnerWaehlen({vorauswahl}) → {site_id, site_name, drive_id, drive_name, item_id, pfad, web_url} | null
//   lokalOrdnerWaehlen({vorauswahl})      → {pfad} | null
//   ablageDateiWaehlen({mandantId})       → {element, name} | null
// Jeder Aufruf baut einen eigenen <dialog> (wird beim Schließen entfernt);
// Abbrechen, ✕ und Esc liefern null. Nur eindeutig benannte Funktions-
// deklarationen (Präfix ablage…/sp…/lokal…), da alle Skripte einen globalen
// Scope teilen.

function ablageDlgNeu(titel, untertitel, knopfText = "Auswahl übernehmen") {
  const dlg = document.createElement("dialog");
  dlg.className = "zellen-dialog sp-dialog";
  dlg.innerHTML = `
    <div class="zd-kopf">
      <div><h3></h3><div class="hinweis" data-rolle="untertitel"></div></div>
      <button type="button" class="sekundaer klein-knopf" data-rolle="schliessen" title="Schließen">✕</button>
    </div>
    <div data-rolle="inhalt"></div>
    <div class="sp-fuss">
      <span class="sp-gewaehlt" data-rolle="gewaehlt">Gewählt: –</span>
      <button type="button" class="sekundaer" data-rolle="abbrechen">Abbrechen</button>
      <button type="button" data-rolle="uebernehmen" disabled></button>
    </div>`;
  dlg.querySelector("h3").textContent = titel;
  dlg.querySelector('[data-rolle="untertitel"]').textContent = untertitel || "";
  dlg.querySelector('[data-rolle="uebernehmen"]').textContent = knopfText;
  document.body.appendChild(dlg);
  return dlg;
}

// Öffnet den Dialog und löst mit dem Ergebnis von ergebnis() (Übernehmen) bzw.
// null (Abbrechen/✕/Esc) auf; der Dialog wird danach aus dem DOM entfernt.
function ablageDlgAblauf(dlg, ergebnis) {
  return new Promise((resolve) => {
    let erledigt = false;
    let wert = null;
    const fertig = () => {
      if (erledigt) return;
      erledigt = true;
      dlg.remove();
      resolve(wert);
    };
    const schliessen = () => {
      if (typeof dlg.close === "function" && dlg.open) dlg.close();
      else { dlg.removeAttribute("open"); fertig(); }
    };
    dlg.addEventListener("close", fertig);
    dlg.querySelector('[data-rolle="schliessen"]').addEventListener("click", schliessen);
    dlg.querySelector('[data-rolle="abbrechen"]').addEventListener("click", schliessen);
    dlg.querySelector('[data-rolle="uebernehmen"]').addEventListener("click", () => {
      const w = ergebnis();
      if (!w) return;
      wert = w;
      schliessen();
    });
    dlg._ablageUebernehmen = () => dlg.querySelector('[data-rolle="uebernehmen"]').click();
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");               // Browser ohne <dialog>-Unterstützung
  });
}

function ablageLiMeldung(ul, text, fehler = false) {
  ul.innerHTML = `<li class="sp-leer${fehler ? " fehler" : ""}">${esc(text)}</li>`;
}

function ablageGroesseText(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1).replace(".", ",") + " MB";
  return Math.max(1, Math.round(bytes / 1024)) + " kB";
}

function ablageElementeText(n) {
  if (n === null || n === undefined) return "";
  return n === 1 ? "1 Element" : `${n} Elemente`;
}

// Brotkrumen: teile = [{name, ziel}] – ziel === undefined: nicht anklickbar
function ablagePfadZeichnen(nav, teile, beiKlick) {
  nav.innerHTML = "";
  teile.forEach((t, i) => {
    if (i > 0) {
      const tr = document.createElement("span");
      tr.className = "sp-trenner";
      tr.textContent = "›";
      nav.appendChild(tr);
    }
    const letzter = i === teile.length - 1;
    if (letzter || t.ziel === undefined) {
      const s = document.createElement("span");
      s.className = letzter ? "sp-hier" : "";
      s.textContent = t.name;
      nav.appendChild(s);
    } else {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = t.name;
      b.addEventListener("click", () => beiKlick(t.ziel, i));
      nav.appendChild(b);
    }
  });
}

// ------------------------------------------------------------ SharePoint
// Nach OnLumis-Vorbild: 1. Website · 2. Dokumentbibliothek · 3. Ordner
function sharepointOrdnerWaehlen({ vorauswahl = null } = {}) {
  const dlg = ablageDlgNeu("SharePoint-Ablage wählen",
    "Website → Dokumentbibliothek → Ordner (über Microsoft Graph)");
  const inhalt = dlg.querySelector('[data-rolle="inhalt"]');
  inhalt.innerHTML = `
    <div class="zd-abschnitt sp-abschnitt" data-abschnitt="site">
      <h4><span class="sp-nr">1</span>SharePoint-Website</h4>
      <div class="sp-zeile">
        <input type="text" data-rolle="suche" autocomplete="off" spellcheck="false"
               placeholder="Website suchen, leer = verfügbare Websites">
        <button type="button" class="sekundaer" data-rolle="laden">Websites laden</button>
      </div>
      <p class="hinweis" style="margin:0 0 6px">Die Suche benötigt die Anwendungsberechtigung
        Sites.Read.All (bzw. Sites.ReadWrite.All). Ist nur Sites.Selected erteilt, findet die Suche
        nichts – dann die Website-URL eingeben, z. B. https://firma.sharepoint.com/sites/Insolvenz.</p>
      <ul class="sp-liste" data-rolle="sites"><li class="sp-leer">„Websites laden“ zeigt die verfügbaren Websites.</li></ul>
    </div>
    <div class="zd-abschnitt sp-abschnitt inaktiv" data-abschnitt="bib">
      <h4><span class="sp-nr">2</span>Dokumentbibliothek</h4>
      <ul class="sp-liste" data-rolle="bibs"><li class="sp-leer">Zuerst eine Website wählen.</li></ul>
      <div class="sp-id" data-rolle="drive-id" style="margin-top:4px"></div>
    </div>
    <div class="zd-abschnitt sp-abschnitt inaktiv" data-abschnitt="ordner">
      <h4><span class="sp-nr">3</span>Ordner</h4>
      <nav class="sp-pfad" data-rolle="pfad"></nav>
      <ul class="sp-liste" data-rolle="ordner"><li class="sp-leer">Zuerst eine Dokumentbibliothek wählen.</li></ul>
      <p class="hinweis" style="margin:6px 0 0">Zeile anklicken = Ordner wählen · „Öffnen“ (oder Doppelklick)
        zeigt die Unterordner. Auch die Bibliothekswurzel ist wählbar.</p>
    </div>`;
  const teil = (r) => inhalt.querySelector(`[data-rolle="${r}"]`);
  const abschnitt = (a) => inhalt.querySelector(`[data-abschnitt="${a}"]`);
  const knopf = dlg.querySelector('[data-rolle="uebernehmen"]');
  const gewaehltText = dlg.querySelector('[data-rolle="gewaehlt"]');
  // stapel: Brotkrumen [{id, name}] (id null = Wurzel, undefined = unbekannt)
  const z = { site: null, bib: null, stapel: [], ordner: null, auswahl: null };
  const nr = { sites: 0, bibs: 0, ordner: 0 };     // veraltete Antworten verwerfen

  function zeigeAuswahl() {
    const teile = [];
    if (z.site) teile.push(z.site.name);
    if (z.bib) teile.push(z.bib.name);
    if (z.bib && z.auswahl) teile.push(z.auswahl.pfad || "/");
    gewaehltText.innerHTML = z.bib && z.auswahl
      ? `Gewählt: <strong>${esc(teile.join(" › "))}</strong>`
      : "Gewählt: –";
    knopf.disabled = !(z.bib && z.auswahl);
    abschnitt("bib").classList.toggle("inaktiv", !z.site && !z.bib);
    abschnitt("ordner").classList.toggle("inaktiv", !z.bib);
  }

  function markiere(ul, li) {
    ul.querySelectorAll("li.gewaehlt").forEach(e => e.classList.remove("gewaehlt"));
    if (li) li.classList.add("gewaehlt");
  }

  function siteZeile(ul, s) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="sp-symbol">🌐</span>
      <span class="sp-name">${esc(s.name)}<span class="klein">${esc(s.web_url || s.beschreibung || "")}</span></span>`;
    if (z.site && z.site.id === s.id) li.classList.add("gewaehlt");
    li.addEventListener("click", () => { markiere(ul, li); siteWaehlen(s); });
    ul.appendChild(li);
  }

  async function sitesLaden() {
    const ul = teil("sites");
    const suche = teil("suche").value.trim();
    const meine = ++nr.sites;
    ablageLiMeldung(ul, "Lade Websites …");
    let liste;
    try { liste = await api("GET", `/api/sharepoint/websites?suche=${encodeURIComponent(suche)}`); }
    catch (e) { if (meine === nr.sites) ablageLiMeldung(ul, e.message, true); return; }
    if (meine !== nr.sites) return;
    ul.innerHTML = "";
    if (!liste.length) {
      ablageLiMeldung(ul, suche ? "Keine Website gefunden – ggf. die vollständige Website-URL eingeben."
        : "Keine Websites sichtbar – bei Sites.Selected die Website-URL eingeben.");
      return;
    }
    for (const s of liste) siteZeile(ul, s);
    if (liste.length === 1 && (!z.site || z.site.id !== liste[0].id)) {
      markiere(ul, ul.firstElementChild);
      siteWaehlen(liste[0]);
    }
  }

  async function siteWaehlen(s, driveVorauswahl = null) {
    z.site = s;
    z.bib = null; z.ordner = null; z.auswahl = null; z.stapel = [];
    teil("drive-id").textContent = "";
    teil("pfad").innerHTML = "";
    ablageLiMeldung(teil("ordner"), "Zuerst eine Dokumentbibliothek wählen.");
    zeigeAuswahl();
    const ul = teil("bibs");
    const meine = ++nr.bibs;
    ablageLiMeldung(ul, "Lade Dokumentbibliotheken …");
    let liste;
    try { liste = await api("GET", `/api/sharepoint/bibliotheken?site_id=${encodeURIComponent(s.id)}`); }
    catch (e) { if (meine === nr.bibs) ablageLiMeldung(ul, e.message, true); return; }
    if (meine !== nr.bibs) return;
    ul.innerHTML = "";
    if (!liste.length) { ablageLiMeldung(ul, "Keine Dokumentbibliothek auf dieser Website."); return; }
    let vor = null;
    for (const b of liste) {
      const li = bibZeile(ul, b);
      if (driveVorauswahl && b.id === driveVorauswahl.drive_id) vor = { b, li };
    }
    if (!vor && driveVorauswahl && driveVorauswahl.drive_id) {
      // gespeicherte Bibliothek nicht (mehr) in der Liste: trotzdem anbieten
      const b = { id: driveVorauswahl.drive_id, name: driveVorauswahl.drive_name || "Dokumentbibliothek" };
      vor = { b, li: bibZeile(ul, b) };
    }
    if (vor) { markiere(ul, vor.li); bibWaehlen(vor.b, driveVorauswahl); }
    else if (liste.length === 1) { markiere(ul, ul.firstElementChild); bibWaehlen(liste[0]); }
  }

  function bibZeile(ul, b) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="sp-symbol">📚</span>
      <span class="sp-name">${esc(b.name)}${b.web_url ? `<span class="klein">${esc(b.web_url)}</span>` : ""}</span>`;
    li.addEventListener("click", () => { markiere(ul, li); bibWaehlen(b); });
    ul.appendChild(li);
    return li;
  }

  function bibWaehlen(b, ordnerVorauswahl = null) {
    z.bib = b;
    teil("drive-id").textContent = `Drive-ID: ${b.id}`;
    z.stapel = [{ id: null, name: b.name }];
    let ziel = null;
    if (ordnerVorauswahl && ordnerVorauswahl.item_id) {
      // Zwischenordner der Vorauswahl sind nur dem Namen nach bekannt
      const teile = (ordnerVorauswahl.pfad || "").split("/").filter(Boolean);
      teile.forEach((name, i) => z.stapel.push({
        id: i === teile.length - 1 ? ordnerVorauswahl.item_id : undefined, name }));
      if (!teile.length) z.stapel.push({ id: ordnerVorauswahl.item_id, name: "gewählter Ordner" });
      ziel = ordnerVorauswahl.item_id;
    }
    ordnerLaden(ziel);
  }

  function ordnerPfad(name) {
    const basis = z.ordner && z.ordner.pfad && z.ordner.pfad !== "/" ? z.ordner.pfad : "";
    return `${basis}/${name}`;
  }

  function aktuellerOrdnerAuswahl() {
    const o = z.ordner;
    return { item_id: o.ist_wurzel ? null : o.id, pfad: o.ist_wurzel ? "/" : (o.pfad || "/"), web_url: o.web_url || null };
  }

  async function ordnerLaden(itemId) {
    const ul = teil("ordner");
    const meine = ++nr.ordner;
    z.auswahl = null;
    zeigeAuswahl();
    ablagePfadZeichnen(teil("pfad"), z.stapel.map((s, i) => ({ name: s.name, ziel: s.id === undefined ? undefined : i })),
      (i) => { z.stapel = z.stapel.slice(0, i + 1); ordnerLaden(z.stapel[i].id); });
    ablageLiMeldung(ul, "Lade Ordner …");
    let erg;
    try {
      erg = await api("GET", `/api/sharepoint/ordner?drive_id=${encodeURIComponent(z.bib.id)}` +
        (itemId ? `&item_id=${encodeURIComponent(itemId)}` : ""));
    } catch (e) { if (meine === nr.ordner) ablageLiMeldung(ul, e.message, true); return; }
    if (meine !== nr.ordner) return;
    z.ordner = erg.ordner;
    ul.innerHTML = "";
    // „Dieser Ordner“ – auch die Bibliothekswurzel ist wählbar
    const hier = document.createElement("li");
    hier.className = "sp-aktuell gewaehlt";
    const dateien = (erg.dateien || []).length;
    hier.innerHTML = `<span class="sp-symbol">📂</span>
      <span class="sp-name">Dieser Ordner: ${esc(erg.ordner.ist_wurzel ? "Bibliothekswurzel" : erg.ordner.name)}
        <span class="klein">${esc(erg.ordner.pfad || "/")}</span></span>
      <span class="sp-info">${(erg.unterordner || []).length} Unterordner · ${dateien} ${dateien === 1 ? "Datei" : "Dateien"}</span>`;
    hier.addEventListener("click", () => { markiere(ul, hier); z.auswahl = aktuellerOrdnerAuswahl(); zeigeAuswahl(); });
    ul.appendChild(hier);
    for (const o of erg.unterordner || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="sp-symbol">📁</span><span class="sp-name">${esc(o.name)}</span>
        <span class="sp-info">${ablageElementeText(o.elemente)}</span>
        <button type="button" class="sekundaer klein-knopf">Öffnen</button>`;
      const oeffnen = () => {
        z.stapel.push({ id: o.id, name: o.name });
        ordnerLaden(o.id);
      };
      li.querySelector("button").addEventListener("click", (e) => { e.stopPropagation(); oeffnen(); });
      li.addEventListener("dblclick", oeffnen);
      li.addEventListener("click", () => {
        markiere(ul, li);
        z.auswahl = { item_id: o.id, pfad: ordnerPfad(o.name), web_url: o.web_url || null };
        zeigeAuswahl();
      });
      ul.appendChild(li);
    }
    if (!(erg.unterordner || []).length) {
      const li = document.createElement("li");
      li.className = "sp-leer";
      li.textContent = "Keine Unterordner.";
      ul.appendChild(li);
    }
    // Brotkrume des aktuellen Ordners mit dem echten Namen
    const letzter = z.stapel[z.stapel.length - 1];
    if (letzter && !erg.ordner.ist_wurzel && erg.ordner.name) letzter.name = erg.ordner.name;
    ablagePfadZeichnen(teil("pfad"), z.stapel.map((s, i) => ({ name: s.name, ziel: s.id === undefined ? undefined : i })),
      (i) => { z.stapel = z.stapel.slice(0, i + 1); ordnerLaden(z.stapel[i].id); });
    z.auswahl = aktuellerOrdnerAuswahl();
    zeigeAuswahl();
  }

  teil("laden").addEventListener("click", sitesLaden);
  teil("suche").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sitesLaden(); }
  });

  // Vorauswahl (aktuelle Ablage des Mandanten): Website und Bibliothek
  // direkt laden, Ordner öffnen
  if (vorauswahl && vorauswahl.drive_id) {
    const bibVor = { id: vorauswahl.drive_id, name: vorauswahl.drive_name || "Dokumentbibliothek" };
    if (vorauswahl.site_id) {
      const s = { id: vorauswahl.site_id, name: vorauswahl.site_name || "Website", web_url: "" };
      const ul = teil("sites");
      ul.innerHTML = "";
      z.site = s;
      siteZeile(ul, s);
      const li = document.createElement("li");
      li.className = "sp-leer";
      li.textContent = "„Websites laden“ zeigt weitere Websites.";
      ul.appendChild(li);
      siteWaehlen(s, vorauswahl);
    } else {
      // nur Drive-ID bekannt (manuell eingetragen)
      const ul = teil("bibs");
      ul.innerHTML = "";
      const li = bibZeile(ul, bibVor);
      markiere(ul, li);
      bibWaehlen(bibVor, vorauswahl);
    }
  }
  zeigeAuswahl();

  return ablageDlgAblauf(dlg, () => {
    if (!z.bib || !z.auswahl) return null;
    return {
      site_id: z.site ? z.site.id : null,
      site_name: z.site ? z.site.name : null,
      drive_id: z.bib.id,
      drive_name: z.bib.name,
      item_id: z.auswahl.item_id,
      pfad: z.auswahl.pfad,
      web_url: z.auswahl.web_url,
    };
  });
}

// ------------------------------------------------------------ lokaler Ordner
function lokalOrdnerWaehlen({ vorauswahl = null } = {}) {
  const dlg = ablageDlgNeu("Lokalen Ablageordner wählen",
    "Ordner unterhalb des Ablage-Verzeichnisses auf dem Server");
  const inhalt = dlg.querySelector('[data-rolle="inhalt"]');
  inhalt.innerHTML = `
    <div class="zd-abschnitt sp-abschnitt">
      <nav class="sp-pfad" data-rolle="pfad"></nav>
      <ul class="sp-liste" data-rolle="ordner" style="max-height:340px"></ul>
      <div class="sp-zeile" style="margin-top:8px">
        <input data-rolle="neu-name" maxlength="120" placeholder="Name des neuen Ordners" autocomplete="off">
        <button type="button" class="sekundaer" data-rolle="neu">Neuer Ordner</button>
      </div>
      <p class="hinweis" style="margin:4px 0 0">Zeile anklicken = Ordner wählen · „Öffnen“ (oder Doppelklick)
        zeigt die Unterordner. Ein neuer Ordner entsteht im angezeigten Ordner.</p>
    </div>`;
  const teil = (r) => inhalt.querySelector(`[data-rolle="${r}"]`);
  const knopf = dlg.querySelector('[data-rolle="uebernehmen"]');
  const gewaehltText = dlg.querySelector('[data-rolle="gewaehlt"]');
  const z = { pfad: "", auswahl: null };
  let nrLaden = 0;

  function zeigeAuswahl() {
    if (z.auswahl) {
      gewaehltText.innerHTML = `Gewählt: <strong>${esc(z.auswahl)}</strong>`;
    } else {
      gewaehltText.textContent = z.pfad === "" ? "Gewählt: – (bitte einen Unterordner wählen oder anlegen)" : "Gewählt: –";
    }
    knopf.disabled = !z.auswahl;
  }

  function markiere(li) {
    teil("ordner").querySelectorAll("li.gewaehlt").forEach(e => e.classList.remove("gewaehlt"));
    if (li) li.classList.add("gewaehlt");
  }

  async function laden(pfad, auswahl = null) {
    const ul = teil("ordner");
    const meine = ++nrLaden;
    ablageLiMeldung(ul, "Lade Ordner …");
    let erg;
    try { erg = await api("GET", `/api/ablage/lokal?pfad=${encodeURIComponent(pfad || "")}`); }
    catch (e) {
      if (meine !== nrLaden) return;
      if (pfad) { toast(e.message); laden(""); return; }      // Vorauswahl existiert nicht mehr
      ablageLiMeldung(ul, e.message, true);
      return;
    }
    if (meine !== nrLaden) return;
    z.pfad = erg.pfad || "";
    const teile = z.pfad ? z.pfad.split("/").filter(Boolean) : [];
    ablagePfadZeichnen(teil("pfad"),
      [{ name: "Ablage-Verzeichnis", ziel: "" }, ...teile.map((t, i) => ({ name: t, ziel: teile.slice(0, i + 1).join("/") }))],
      (ziel) => laden(ziel));
    ul.innerHTML = "";
    z.auswahl = null;
    if (z.pfad) {
      const hier = document.createElement("li");
      hier.className = "sp-aktuell";
      hier.innerHTML = `<span class="sp-symbol">📂</span><span class="sp-name">Dieser Ordner: ${esc(teile[teile.length - 1])}
        <span class="klein">${esc(z.pfad)}</span></span>
        <span class="sp-info">${(erg.dateien || []).length} Dateien</span>`;
      hier.addEventListener("click", () => { markiere(hier); z.auswahl = z.pfad; zeigeAuswahl(); });
      ul.appendChild(hier);
      if (!auswahl || auswahl === z.pfad) { markiere(hier); z.auswahl = z.pfad; }
    }
    for (const o of erg.unterordner || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="sp-symbol">📁</span><span class="sp-name">${esc(o.name)}</span>
        <span class="sp-info">${ablageElementeText(o.elemente)}</span>
        <button type="button" class="sekundaer klein-knopf">Öffnen</button>`;
      li.querySelector("button").addEventListener("click", (e) => { e.stopPropagation(); laden(o.pfad); });
      li.addEventListener("dblclick", () => laden(o.pfad));
      li.addEventListener("click", () => { markiere(li); z.auswahl = o.pfad; zeigeAuswahl(); });
      ul.appendChild(li);
      if (auswahl && auswahl === o.pfad) { markiere(li); z.auswahl = o.pfad; }
    }
    if (!(erg.unterordner || []).length) {
      const li = document.createElement("li");
      li.className = "sp-leer";
      li.textContent = "Keine Unterordner.";
      ul.appendChild(li);
    }
    zeigeAuswahl();
  }

  async function neuerOrdner() {
    const feld = teil("neu-name");
    const name = feld.value.trim();
    if (!name) { toast("Bitte einen Namen für den neuen Ordner eingeben."); feld.focus(); return; }
    try {
      const erg = await api("POST", "/api/ablage/lokal", { eltern: z.pfad, name });
      feld.value = "";
      toast(`Ordner „${name}“ angelegt.`, false);
      await laden(erg.pfad);                 // in den neuen Ordner wechseln – er ist damit gewählt
    } catch (e) { toast(e.message); }
  }
  teil("neu").addEventListener("click", neuerOrdner);
  teil("neu-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); neuerOrdner(); }
  });

  laden(vorauswahl && vorauswahl.pfad ? vorauswahl.pfad : "");
  zeigeAuswahl();
  return ablageDlgAblauf(dlg, () => (z.auswahl ? { pfad: z.auswahl } : null));
}

// ------------------------------------------------------------ Datei aus der Mandantenablage
function ablageDateiWaehlen({ mandantId }) {
  const dlg = ablageDlgNeu("Datei aus der Ablage wählen", "", "Datei übernehmen");
  const inhalt = dlg.querySelector('[data-rolle="inhalt"]');
  inhalt.innerHTML = `
    <div class="zd-abschnitt sp-abschnitt">
      <nav class="sp-pfad" data-rolle="pfad"></nav>
      <ul class="sp-liste" data-rolle="liste" style="max-height:380px"></ul>
      <p class="hinweis" style="margin:6px 0 0">Ordner anklicken = öffnen · Datei anklicken = wählen
        (Doppelklick übernimmt). Die Datei bleibt, wo sie ist.</p>
    </div>`;
  const teil = (r) => inhalt.querySelector(`[data-rolle="${r}"]`);
  const knopf = dlg.querySelector('[data-rolle="uebernehmen"]');
  const gewaehltText = dlg.querySelector('[data-rolle="gewaehlt"]');
  const untertitel = dlg.querySelector('[data-rolle="untertitel"]');
  let auswahl = null;
  let nrLaden = 0;

  function zeigeAuswahl() {
    gewaehltText.innerHTML = auswahl ? `Gewählt: <strong>${esc(auswahl.name)}</strong>` : "Gewählt: –";
    knopf.disabled = !auswahl;
  }

  function symbol(mime, name) {
    const m = (mime || "").toLowerCase(), n = (name || "").toLowerCase();
    if (m === "application/pdf" || n.endsWith(".pdf")) return "📄";
    if (m.startsWith("image/")) return "🖼";
    if (n.endsWith(".eml") || m === "message/rfc822") return "✉️";
    return "📎";
  }

  async function laden(element) {
    const ul = teil("liste");
    const meine = ++nrLaden;
    ablageLiMeldung(ul, "Lade …");
    let erg;
    try {
      erg = await api("GET", `/api/mandanten/${mandantId}/ablage/durchsuchen` +
        (element ? `?element=${encodeURIComponent(element)}` : ""));
    } catch (e) { if (meine === nrLaden) ablageLiMeldung(ul, e.message, true); return; }
    if (meine !== nrLaden) return;
    untertitel.textContent = `${erg.typ === "SHAREPOINT" ? "SharePoint" : "Lokale Ablage"}: ${erg.wurzel_name || ""}`;
    const teile = (erg.pfad_teile && erg.pfad_teile.length)
      ? erg.pfad_teile : [{ element: "", name: erg.wurzel_name || "Ablage" }];
    ablagePfadZeichnen(teil("pfad"), teile.map(t => ({ name: t.name, ziel: t.element || "" })), (ziel) => laden(ziel));
    ul.innerHTML = "";
    auswahl = null;
    for (const o of erg.unterordner || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="sp-symbol">📁</span><span class="sp-name">${esc(o.name)}</span>
        <span class="sp-info">${ablageElementeText(o.elemente)}</span>`;
      li.addEventListener("click", () => laden(o.element));
      ul.appendChild(li);
    }
    for (const d of erg.dateien || []) {
      const li = document.createElement("li");
      const zeit = d.geaendert ? new Date(d.geaendert).toLocaleDateString("de-DE") : "";
      li.innerHTML = `<span class="sp-symbol">${symbol(d.mime, d.name)}</span><span class="sp-name">${esc(d.name)}</span>
        <span class="sp-info">${ablageGroesseText(d.groesse)}${zeit ? " · " + esc(zeit) : ""}</span>`;
      li.addEventListener("click", () => {
        ul.querySelectorAll("li.gewaehlt").forEach(e => e.classList.remove("gewaehlt"));
        li.classList.add("gewaehlt");
        auswahl = { element: d.element, name: d.name };
        zeigeAuswahl();
      });
      li.addEventListener("dblclick", () => {
        auswahl = { element: d.element, name: d.name };
        zeigeAuswahl();
        dlg._ablageUebernehmen();
      });
      ul.appendChild(li);
    }
    if (!(erg.unterordner || []).length && !(erg.dateien || []).length) {
      ablageLiMeldung(ul, "Dieser Ordner ist leer.");
    }
    zeigeAuswahl();
  }

  laden("");
  zeigeAuswahl();
  return ablageDlgAblauf(dlg, () => auswahl);
}
