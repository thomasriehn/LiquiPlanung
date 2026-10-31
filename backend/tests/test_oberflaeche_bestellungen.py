"""Oberflächentests (Bestellungen, Plan-Zellendialog, Systemeinstellungen):
Die echten Skripte laufen in Node mit einer minimalen DOM-Attrappe und
API-Stubs (synthetische Daten). Ohne Node werden die Tests übersprungen."""
import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
NODE = shutil.which("node") or next(
    (p for p in ("/opt/node22/bin/node", "/usr/bin/node") if os.path.exists(p)), None)

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js nicht verfügbar")

# DOM-Attrappe: Elemente je id, innerHTML als Text, <option>-Werte aus innerHTML,
# Ereignis-Handler werden gesammelt und lassen sich auslösen.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
process.on("unhandledRejection", () => {});
const [, , skripteJson, testCode] = process.argv;

function neu(tag) {
  const ziel = new El(tag);
  return new Proxy(ziel, {
    get(t, p, r) {
      if (typeof p === "symbol" || p in t) return Reflect.get(t, p, r);
      if (p === "then" || p === "toJSON") return undefined;
      t[p] = neu(String(p));          // unbekanntes Feld (z. B. form.feldname) → Element
      return t[p];
    },
  });
}
class El {
  constructor(tag) {
    this.tagName = tag; this._html = ""; this.kinder = []; this._sel = new Map(); this.handler = {};
    this.textContent = ""; this._wert = null; this.style = {}; this.dataset = {}; this.disabled = false;
    this.checked = false; this.className = ""; this.files = []; this.placeholder = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.kinder = []; this._sel = new Map(); }
  get children() { return this.kinder; }
  get options() { return [...this._html.matchAll(/<option value="([^"]*)"/g)].map(m => ({ value: m[1] })); }
  get value() {
    if (this._wert !== null) return this._wert;
    const sel = /<option value="([^"]*)"[^>]*selected/.exec(this._html);
    if (sel) return sel[1];
    const o = this.options;
    return o.length ? o[0].value : "";
  }
  set value(v) { this._wert = String(v); }
  get selectedIndex() { return this.options.findIndex(o => o.value === this.value); }
  set selectedIndex(i) { this.value = this.options[i].value; }
  appendChild(c) { this.kinder.push(c); return c; }
  insertAdjacentHTML(pos, html) { this._html += html; }
  addEventListener(typ, fn) { (this.handler[typ] = this.handler[typ] || []).push(fn); }
  removeEventListener() {}
  querySelector(s) { if (!this._sel.has(s)) this._sel.set(s, neu(s)); return this._sel.get(s); }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {} click() {} remove() {} showModal() {} close() {} setAttribute() {} blur() {}
  reportValidity() { return true; }
}
const elemente = new Map();
const document = {
  getElementById(id) { if (!elemente.has(id)) elemente.set(id, neu("#" + id)); return elemente.get(id); },
  createElement: (tag) => neu(tag),
  querySelector: () => neu("q"), querySelectorAll: () => [], addEventListener() {},
  body: neu("body"),
};
const speicher = new Map();
const g = {
  document, console, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  confirm: () => true, alert() {}, prompt: () => "", URLSearchParams, Intl, FormData: class { append() {} },
  location: { search: "", href: "" }, navigator: {},
  localStorage: { getItem: (k) => (speicher.has(k) ? speicher.get(k) : null), setItem: (k, v) => speicher.set(k, String(v)), removeItem: (k) => speicher.delete(k) },
  fetch: async () => ({ status: 200, ok: true, json: async () => ({}) }),
  MANDANT_ID: 7, LESER: false, DARF_FREIGEBEN: true, KI_AKTIV: false, IST_ADMIN: true, ANSICHT: "wochen",
};
g.window = g;
const ctx = vm.createContext(g);
const ladefehler = [];
for (const datei of JSON.parse(skripteJson)) {
  try { vm.runInContext(fs.readFileSync(datei, "utf8"), ctx, { filename: datei }); }
  catch (e) { ladefehler.push(String(e)); }
}
ctx.__antworten = [];        // API-Stub: Liste [prüfFunktion(methode, url), antwort]
ctx.api = async (methode, url, daten) => {
  for (const [passt, antwort] of ctx.__antworten) {
    if (passt(methode, url, daten)) return typeof antwort === "function" ? antwort(methode, url, daten) : antwort;
  }
  return [];
};
ctx.__ausloesen = async (el, typ, ereignis) => {
  for (const fn of el.handler[typ] || []) await fn(ereignis || { preventDefault() {}, target: el });
};
(async () => {
  // Aufrufe beim Laden der Skripte (z. B. erstes Laden der Daten) erst ablaufen lassen
  for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
  const ergebnis = await vm.runInContext(`(async () => { ${testCode} })()`, ctx);
  process.stdout.write(JSON.stringify({ ladefehler, ergebnis }));
})().catch(e => { process.stdout.write(JSON.stringify({ ladefehler, fehler: String(e && e.stack || e) })); });
"""


def _lauf(tmp_path, skripte: list[str], test_code: str):
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS)
    pfade = [str(STATIC / s) for s in skripte]
    proc = subprocess.run([NODE, str(harness), json.dumps(pfade), test_code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    aus = json.loads(proc.stdout)
    assert "fehler" not in aus, aus["fehler"]
    assert not aus["ladefehler"], aus["ladefehler"]       # Skripte laden ohne Fehler
    return aus["ergebnis"]


def _iso(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------- plan.js


def test_plan_zellendialog_abgeloeste_bestellung_mit_planbetrag(tmp_path):
    """Abgelöste Bestellung: Betrag aus plan_betrag (nicht rest_geplant = 0), kein
    „überfällig, vorgetragen“; der Hinweis hängt nur an `gerollt`."""
    heute = date.today()
    liste = [
        {"id": 1, "kreditor": "Abgelöst GmbH", "status": "ABGELOEST", "status_text": "abgelöst",
         "betrag_brutto": 1190.0, "rest_geplant": 0.0, "rest_offen": 0.0, "plan_betrag": 1190.0,
         "plan_tag": _iso(heute), "zahldatum": _iso(heute + timedelta(days=5)), "gerollt": False,
         "abgeloest_am": _iso(heute), "abgeloest_betrag": 900.0},
        {"id": 2, "kreditor": "Überfällig AG", "status": "FREIGEGEBEN", "status_text": "freigegeben",
         "betrag_brutto": 595.0, "rest_geplant": 595.0, "rest_offen": 595.0, "plan_betrag": 595.0,
         "plan_tag": _iso(heute), "zahldatum": _iso(heute - timedelta(days=3)), "gerollt": True},
    ]
    code = f"""
      __antworten.push([(m, u) => u.includes("/plan/bestellungen"), {json.dumps(liste)}]);
      const box = await bestellAbschnitt({{ key: "42" }}, {{ tage: ["{_iso(heute)}"] }});
      const lis = box.querySelector("ul").children.map(li => li.innerHTML);
      return {{ kopf: box.innerHTML, lis }};
    """
    erg = _lauf(tmp_path, ["app.js", "plan.js"], code)
    assert "1.785,00 €" in erg["kopf"]                       # 1.190 + 595
    abgeloest, gerollt = erg["lis"]
    assert "1.190,00 €" in abgeloest
    assert "überfällig" not in abgeloest
    assert "abgelöst am" in abgeloest and "gezahlt 900,00 €" in abgeloest
    assert "überfällig, vorgetragen" in gerollt and "595,00 €" in gerollt


# ---------------------------------------------------------------- bestellungen.js


def test_freigabedialog_teilzahlung_offener_rest(tmp_path):
    """Mit Teilzahlung: Zeile „− diese Bestellung (offener Rest)“ mit Brutto und
    bereits Gezahltem; ohne die neuen Felder (alte Einträge) wie bisher „(brutto)“."""
    ebene = {"ebene": "gesamt", "name": "", "soll_kw": 10000, "soll_kw_rest": 10000, "verfuegbar": 8000,
             "nach_freigabe": 4050, "gebunden": {}, "abweichung": {}}
    kz = {"kw": {"label": "KW 46/2026", "von": "2026-11-09", "bis": "2026-11-15"}, "ab": "2026-11-09",
          "ist_bis": "2026-11-08", "massgeblich": "gesamt", "ampel": "gruen", "ebenen": [ebene],
          "betrag_brutto": 3950.0, "bestellung_brutto": 5950.0, "bereits_gezahlt": 2000.0}
    alt = {k: v for k, v in kz.items() if k not in ("bestellung_brutto", "bereits_gezahlt")}
    alt["betrag_brutto"] = 5950.0
    code = f"""
      return {{ neu: fdRechnungHtml({json.dumps(kz)}), alt: fdRechnungHtml({json.dumps(alt)}),
               kurz: bsKennzahlenKurz({json.dumps(kz)}), kurzAlt: bsKennzahlenKurz({json.dumps(alt)}) }};
    """
    erg = _lauf(tmp_path, ["app.js", "bestellungen.js"], code)
    assert "diese Bestellung (offener Rest)" in erg["neu"]
    assert "brutto 5.950,00 €, bereits gezahlt 2.000,00 €" in erg["neu"]
    assert "diese Bestellung (brutto)" not in erg["neu"]
    assert "diese Bestellung (brutto)" in erg["alt"] and "bereits gezahlt" not in erg["alt"]
    assert "offener Rest 3.950,00 €" in erg["kurz"] and "bereits gezahlt" not in erg["kurzAlt"]


def _uebersicht(heute: date, offen: list[dict]) -> dict:
    return {"kw": {"label": "KW", "von": _iso(heute), "bis": _iso(heute + timedelta(days=6))},
            "heute": _iso(heute), "basis": {"typ": "LIVE"}, "summe_offen": 3950.0,
            "gesamt": {"verfuegbar": 10000.0, "nach_freigabe": 6050.0, "gebunden": {}, "abweichung": {}},
            "offen": offen, "freigegeben": []}


def test_freigabe_tab_rest_mit_offenem_rest(tmp_path):
    """„Rest gesamt danach“ zieht den offenen Rest ab (Teilzahlung vor heute), nicht das Brutto."""
    heute = date.today()
    b = {"id": 5, "kreditor": "Teil GmbH", "status": "OFFEN", "zahldatum": _iso(heute + timedelta(days=2)),
         "betrag_brutto": 5950.0, "abgeloest_betrag": 2000.0, "rest_offen": 3950.0, "rest_geplant": 3950.0,
         "konto": None,
         "abloesungen": [{"betrag": 2000.0, "datum": _iso(heute - timedelta(days=1)), "abschliessend": False}]}
    code = f"""
      __antworten.push([(m, u) => u.includes("freigabe-uebersicht"), {json.dumps(_uebersicht(heute, [b]))}]);
      await fgLaden();
      return document.getElementById("fg-offen").children.map(tr => tr.innerHTML);
    """
    zeilen = _lauf(tmp_path, ["app.js", "bestellungen.js"], code)
    assert len(zeilen) == 1
    assert "6.050,00" in zeilen[0]                 # 10.000 − 3.950
    assert "4.050,00" not in zeilen[0]             # 10.000 − 5.950 (doppelt gezählt)
    assert "offen 3.950,00" in zeilen[0]


def test_liste_und_toast_rest_offen_statt_rest_geplant(tmp_path):
    """Teilbezahlte OFFEN-Bestellung, die nicht im Plan steht: Liste und Toast zeigen
    den echten Rest (rest_offen), nicht rest_geplant = 0."""
    heute = date.today()
    b = {"id": 9, "kreditor": "Rest KG", "status": "OFFEN", "status_text": "zur Freigabe",
         "bestelldatum": _iso(heute), "zahldatum": _iso(heute + timedelta(days=9)),
         "betrag_netto": 1000.0, "ust_satz": 19.0, "betrag_brutto": 1190.0, "konto": None,
         "abgeloest_betrag": 500.0, "rest_geplant": 0.0, "rest_offen": 690.0, "im_plan": False,
         "abloesungen": []}
    neu = dict(b, abgeloest_betrag=1000.0, rest_offen=190.0)
    code = f"""
      bs.liste = [{json.dumps(b)}];
      document.getElementById("filter-status").value = "";
      bsListeZeichnen();
      const zeile = document.getElementById("bestell-tabelle").children[0].innerHTML;
      __antworten.push([(m, u) => m === "POST" && u.includes("/abloesung"), {json.dumps(neu)}]);
      bs.ad = {{ b: {json.dumps(b)}, art: "MANUELL", gewaehlt: null }};
      document.getElementById("ad-betrag").value = "500";
      document.getElementById("ad-datum").value = "{_iso(heute)}";
      document.getElementById("ad-abschliessend").checked = false;
      await adSpeichern();
      const toasts = document.getElementById("toast").children.map(t => t.textContent);
      return {{ zeile, toasts }};
    """
    erg = _lauf(tmp_path, ["app.js", "bestellungen.js"], code)
    assert "Rest 690,00 €" in erg["zeile"]
    assert any("Rest 190,00 € offen" in t and "im Plan" not in t for t in erg["toasts"]), erg["toasts"]


def test_freigabe_tab_ohne_vergangene_kws(tmp_path):
    """KW-Auswahl beginnt mit der laufenden KW; ◀ ist dort gesperrt."""
    code = """
      const sel = document.getElementById("fg-kw");
      const w = isoWoche(bs.heute);
      return { werte: sel.options.map(o => o.value), gewaehlt: sel.value, laufend: `${w.jahr}-${w.kw}`,
               zurueck: document.getElementById("fg-zurueck").disabled,
               vor: document.getElementById("fg-vor").disabled };
    """
    erg = _lauf(tmp_path, ["app.js", "bestellungen.js"], code)
    assert erg["werte"][0] == erg["laufend"] == erg["gewaehlt"]
    assert len(erg["werte"]) == 14                     # laufende KW … +13
    assert erg["zurueck"] is True and erg["vor"] is False


def test_bearbeiten_freigegeben_warnung_nach_neuer_regel(tmp_path):
    """Rückfall auf „zur Freigabe“ auch bei Kreditor, Zahldatum in anderer KW (Hinweis + Rückfrage)."""
    montag = date.today() - timedelta(days=date.today().weekday()) + timedelta(days=14)
    alt = {"kreditor": "Alt", "zahldatum": _iso(montag), "betrag_netto": 100.0, "ust_satz": 19.0}
    code = f"""
      const alt = {json.dumps(alt)};
      return {{
        kreditor: efFreigabeRelevant(alt, {{ kreditor: "Neu" }}),
        gleicheKw: efFreigabeRelevant(alt, {{ zahldatum: "{_iso(montag + timedelta(days=4))}" }}),
        andereKw: efFreigabeRelevant(alt, {{ zahldatum: "{_iso(montag + timedelta(days=7))}" }}),
        notiz: efFreigabeRelevant(alt, {{ beschreibung: "x", bestellnummer: "1" }}),
      }};
    """
    erg = _lauf(tmp_path, ["app.js", "bestellungen.js"], code)
    assert erg == {"kreditor": True, "gleicheKw": False, "andereKw": True, "notiz": False}


# ---------------------------------------------------------------- system.js

_SYS = {
    "graph": {"tenant_id": "t-alt", "client_id": "c-alt", "client_secret": {"gesetzt": True},
              "konfiguriert": True},
    "llm": {"url": "", "modell": "", "vision": False, "api_key": {"gesetzt": False}, "konfiguriert": False},
    "ablage": {"verzeichnis": "/daten", "max_mb": 20},
}


def test_system_speichern_belegt_nur_eigenen_abschnitt_neu(tmp_path):
    """Graph speichern verwirft keine ungespeicherten KI-Eingaben (inkl. API-Key) und umgekehrt."""
    gespeichert = dict(_SYS, graph=dict(_SYS["graph"], tenant_id="t-neu"))
    code = f"""
      __antworten.push([(m, u) => m === "PUT", {json.dumps(gespeichert)}]);
      __antworten.push([(m, u) => m === "GET", {json.dumps(_SYS)}]);
      await sysLaden();
      const gf = document.getElementById("sys-graph-form"), kf = document.getElementById("sys-ki-form");
      kf.llm_url.value = "https://ki.example.invalid/v1";
      kf.llm_modell.value = "modell-x";
      kf.llm_api_key.value = "geheim-ki";
      await __ausloesen(kf, "input");
      gf.graph_tenant_id.value = "t-neu";
      gf.graph_client_secret.value = "geheim-graph";
      await __ausloesen(gf, "input");
      await __ausloesen(gf, "submit");
      return {{ url: kf.llm_url.value, modell: kf.llm_modell.value, key: kf.llm_api_key.value,
               kiGeaendert: sysGeaendert.ki, graphGeaendert: sysGeaendert.graph,
               tenant: gf.graph_tenant_id.value, secret: gf.graph_client_secret.value }};
    """
    erg = _lauf(tmp_path, ["app.js", "system.js"], code)
    assert erg["url"] == "https://ki.example.invalid/v1"
    assert erg["modell"] == "modell-x" and erg["key"] == "geheim-ki"
    assert erg["kiGeaendert"] is True
    assert erg["graphGeaendert"] is False and erg["tenant"] == "t-neu" and erg["secret"] == ""
