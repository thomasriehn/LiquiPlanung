"""Eigenschaftsbasierte Prüfung der manuellen Plan-Verschiebungen (Orakel) und ihrer
Wirkung auf Plan, Liquidität, Planstand und Freigabe-Kennzahlen – synthetische Daten.

1. Feste Seeds: je Invariante (szenario_bausteine.INVARIANTEN) werden die Szenarien
   fester Seeds geprüft. Erwartete Fehlschläge (offene Befunde) stünden in BEKANNT –
   seit der sichtunabhängigen Zuordnung der Verschiebungen (liquiditaet.Zuordnung)
   keine. Ein neuer Fehlschlag lässt den Test mit dem verkleinerten Szenario
   fehlschlagen, ein bekannter, der nicht mehr auftritt, ebenfalls (Befund behoben →
   Eintrag entfernen). Ändert sich der Generator, BEKANNT neu bestimmen (Orakel:
   scratchpad/r3/orakel.py).
2. Benannte Tests: die minimalen Szenarien der Befunde V1–V6 (Runde 2) und der im
   Orakellauf gefundenen (N…, V2b, V5b) – Regressionstests (bis 32e4237 fehlgeschlagen).
3. Gegenproben: fachlich korrekte Fälle (u. a. die in Runde 1/2 behobenen), an denen
   die Invarianten gelten müssen – schützen vor zu scharfen Invarianten.

Die Datenbanken sind SQLite-Kopien einer Vorlage (auch mit TEST_DATABASE_URL), weil je
Szenario mehrere Varianten gleichzeitig offen sind."""

from datetime import date
from decimal import Decimal

import pytest

from szenario_bausteine import (
    INVARIANTEN,
    Auswertung,
    Budget,
    Dauer,
    Posten,
    Szenario,
    Verschiebung,
    beschreibe,
    erzeuge,
    pruefen,
    punkt_verletzt,
    verkleinere,
)

D = Decimal

# ---------------------------------------------------------------------------
# 1. Feste Seeds
# ---------------------------------------------------------------------------

SEEDS = tuple(range(40))
# seltene Vorbedingungen: zusätzlich die ersten anwendbaren Seeds ab 40
SEEDS_ZUSATZ: dict[str, tuple[int, ...]] = {
    "I1b": (84, 145, 170, 289, 356, 404),
    "I2b": (70, 128, 136, 147, 172, 216),
    "I2": (46, 48, 54, 57, 63, 71),
    "I5": (42, 52, 63, 68, 83, 84),
}

# Invariante → Seed → vermuteter Befund (szenario_bausteine.URSACHEN): derzeit erwartete
# Fehlschläge; bestimmt mit scratchpad/r3/bekannt_fuellen.py. Stand 32e4237 waren es 31
# (V2–V6, N2–N5, N8, V5b), seit der Zuordnung der Verschiebungen keine.
BEKANNT: dict[str, dict[int, str]] = {}


@pytest.fixture(scope="module")
def auswertungen():
    """Eine Auswertung je Seed für alle Invarianten (Datenbanken und Pläne gepuffert)."""
    vorrat: dict[int, Auswertung] = {}
    yield vorrat
    for aw in vorrat.values():
        aw.schliessen()


@pytest.mark.parametrize("name", list(INVARIANTEN))
def test_eigenschaft_feste_seeds(name, auswertungen):
    fehlschlaege = {}
    for seed in sorted(set(SEEDS) | set(SEEDS_ZUSATZ.get(name, ()))):
        aw = auswertungen.get(seed) or auswertungen.setdefault(seed, Auswertung(erzeuge(seed)))
        e = pruefen(aw.sz, [name], aw=aw, erster_fehler=True)[name]
        if e.verletzungen:
            fehlschlaege[seed] = e.verletzungen[0]
    bekannt = BEKANNT.get(name, {})
    unerwartet = sorted(set(fehlschlaege) - set(bekannt))
    meldungen = []
    for seed in unerwartet[:2]:
        punkt, _ = fehlschlaege[seed]
        klein, text = verkleinere(erzeuge(seed), name, punkt)
        meldungen.append(f"{beschreibe(klein, punkt)}\n  → {text}")
    assert not unerwartet, (f"{name}: neue Fehlschläge bei Seeds {unerwartet}\n"
                            + "\n".join(meldungen))
    behoben = sorted(set(bekannt) - set(fehlschlaege))
    assert not behoben, (f"{name}: bekannte Fehlschläge treten nicht mehr auf (Befund behoben?) – "
                         f"aus BEKANNT entfernen: { {s: bekannt[s] for s in behoben} }")


# ---------------------------------------------------------------------------
# 2. Benannte Minimal-Szenarien der Befunde (Regressionstests; bis 32e4237 rot)
# ---------------------------------------------------------------------------

def _gilt(sz: Szenario, name: str, punkt: tuple) -> None:
    inv = INVARIANTEN[name]
    assert inv.vorbedingung(sz) is None and inv.punkt_gueltig(sz, punkt), \
        "Szenario erfüllt die Vorbedingung nicht mehr"
    text = punkt_verletzt(sz, name, punkt)
    assert text is None, f"{name} verletzt: {text}\n{beschreibe(sz, punkt)}"


def _op(nr, faellig, betrag="1500", konto="4920", zahlungen=()):
    return Posten(nr, konto, D(betrag), faellig, tuple((t, D(b)) for t, b in zahlungen))


def _v(nr, von, nach, betrag, konto="4920"):
    return Verschiebung(nr, konto, von, nach, D(betrag))


def test_v1_teilbezahlter_ueberfaelliger_posten_gibt_kein_vergangenes_budget_frei():
    """OP 1.500 fällig 04.08., 500 am 05.08. bezahlt (Rest 1.000 gerollt), Verschiebung
    04.08.→18.08. über −1.500, Budget nur KW 32: die Verschiebung nimmt den Rest 1.000 und
    die Teilzahlung 500 (in keiner Sicht geführt), kein Budget des 04.08. → KW 34 verfügbar
    0 (bis 32e4237: 500)."""
    sz = Szenario(1, date(2026, 8, 12), posten=(
        _op(1, date(2026, 8, 4), zahlungen=((date(2026, 8, 5), "500"),)),),
        budgets=(Budget("4920", date(2026, 8, 3), D("10000")),),
        verschiebungen=(_v(1, date(2026, 8, 4), date(2026, 8, 18), "-1500"),))
    _gilt(sz, "I1b", ("KZ", date(2026, 8, 17), "LIVE", "4920"))


def _v2(planstaende=()):
    """OP 1.500 fällig 04.08., verschoben auf 18.08. und dort bezahlt; heute Mi 19.08."""
    return Szenario(2, date(2026, 8, 19), posten=(
        _op(1, date(2026, 8, 4), zahlungen=((date(2026, 8, 18), "1500"),)),),
        verschiebungen=(_v(1, date(2026, 8, 4), date(2026, 8, 18), "-1500"),),
        planstaende=planstaende)


@pytest.mark.parametrize("basis", ["LIVE", ("PLANSTAND", 0)])
def test_v2_am_zieltag_bezahlter_posten_gibt_keinen_spielraum(basis):
    """Der bezahlte Posten steht im Soll nur am Zieltag 18.08. (bis 32e4237 auch am 17.08.:
    KW 35/38 verfügbar 1.500) – auch auf Basis eines Planstands zum 17.08."""
    _gilt(_v2((0,)), "I1", ("KZ", date(2026, 8, 24), basis, "4920"))


def test_v2_am_zieltag_bezahlter_posten_steht_einmal_im_plan():
    _gilt(_v2(), "I3", ("SUMME", 0, 2, "4920"))


def test_v3_verschobene_vergangene_rate_bleibt_gebunden():
    """Miete 2.000 am 11., nicht bezahlt, verschoben 11.08.→19.08.; heute 12.08.: KW 34
    gebunden 2.000, verfügbar 0 (bis 32e4237: gebunden 0, verfügbar 2.000)."""
    sz = Szenario(3, date(2026, 8, 12), dauer=(Dauer(1, "4920", D("2000"), 11),),
                  verschiebungen=(_v(1, date(2026, 8, 11), date(2026, 8, 19), "-2000"),))
    _gilt(sz, "I1", ("KZ", date(2026, 8, 17), "LIVE", "4920"))


def test_v4_kette_eines_bezahlten_postens_erhaelt_liquiditaet():
    """OP 3.000 fällig 12.08., vorab am 10.08. bezahlt, verschoben 12.08.→19.08.→26.08.;
    heute 21.08.: Liquidität 97.000 (bis 32e4237: 94.000)."""
    sz = Szenario(4, date(2026, 8, 21), posten=(
        _op(1, date(2026, 8, 12), "3000", zahlungen=((date(2026, 8, 10), "3000"),)),),
        verschiebungen=(_v(1, date(2026, 8, 12), date(2026, 8, 19), "-3000"),
                        _v(2, date(2026, 8, 19), date(2026, 8, 26), "-3000")))
    _gilt(sz, "I2b", ("LIQ", sz.horizont_ende))


def test_v5_zukunftsfenster_zaehlt_verschobenen_posten_einmal():
    """Fenster ab 31.08., heute 05.08., Posten fällig 25.08. → 08.09.: Plan zeigt ihn nur am
    08.09. (bis 32e4237 zusätzlich am 31.08.)."""
    sz = Szenario(5, date(2026, 8, 5), konten=("4210",),
                  posten=(_op(1, date(2026, 8, 25), "3000", konto="4210"),),
                  verschiebungen=(_v(1, date(2026, 8, 25), date(2026, 9, 8), "-3000", "4210"),),
                  fenster=((4, 2),))
    _gilt(sz, "I3", ("SUMME", 4, 2, "4210"))


def test_v6_verschobene_rate_nimmt_keinen_anderen_posten_mit():
    """Rate 2.000 am 11.08. verschoben auf 19.08., OP 1.500 (05.08.) überfällig: bewegt wird
    die Rate, der OP bleibt überfällig; KW 34 verfügbar 0 (bis 32e4237: OP bewegt, 2.000)."""
    sz = Szenario(6, date(2026, 8, 12), posten=(_op(1, date(2026, 8, 5)),),
                  dauer=(Dauer(1, "4920", D("2000"), 11),),
                  verschiebungen=(_v(1, date(2026, 8, 11), date(2026, 8, 19), "-2000"),))
    _gilt(sz, "I1", ("KZ", date(2026, 8, 17), "LIVE", "4920"))


def test_v2b_verschiebung_vom_wochenbeginn_nimmt_bezahlten_posten_der_vorwoche_nicht_mit():
    """OP 500 fällig Sa 18.04., bezahlt Mi 22.04.; Verschiebung vom Mo 20.04. (dort ist
    nichts geplant) auf 15.05.: die Verschiebung findet am 20.04. nichts (bis 32e4237 trug
    sie den Posten aus der Wochensicht in KW 20 → verfügbar 500)."""
    sz = Szenario(12, date(2026, 5, 9), konten=("3400",), posten=(
        _op(1, date(2026, 4, 18), "500", "3400", ((date(2026, 4, 22), "500"),)),),
        verschiebungen=(_v(1, date(2026, 4, 20), date(2026, 5, 15), "-1250", "3400"),))
    _gilt(sz, "I1", ("KZ", date(2026, 5, 11), "LIVE", "3400"))


def test_v5b_verschiebung_aus_kuenftiger_woche_nimmt_ueberfaelliges_nicht_mit():
    """OP 1.200 seit 05.04. überfällig; Verschiebung 01.06.→04.05. (am 01.06. ist nichts
    geplant): die Verschiebung findet dort nichts – LIVE-Soll 1.200 (bis 32e4237: 2.400, die
    Wochensicht ab 01.06. legte den OP auf den 01.06.)."""
    sz = Szenario(13, date(2026, 4, 16), konten=("3400",),
                  posten=(_op(1, date(2026, 4, 5), "1200", "3400"),),
                  verschiebungen=(_v(1, date(2026, 6, 1), date(2026, 5, 4), "-1200", "3400"),))
    _gilt(sz, "I4", ("SOLL",))


def test_n2_mehrere_verschiebungen_vom_selben_tag_ausserhalb_des_fensters():
    """OP 1.000 fällig 14.09. (außerhalb des Fensters 10.–23.08.), zwei Verschiebungen je
    −600 auf 18.08. und 20.08.: zusammen höchstens 1.000 (bis 32e4237 nahmen beide 600 vom
    unveränderten Wochenwert – 1.200 im Fenster)."""
    sz = Szenario(22, date(2026, 8, 12), posten=(_op(1, date(2026, 9, 14), "1000"),),
                  verschiebungen=(_v(1, date(2026, 9, 14), date(2026, 8, 18), "-600"),
                                  _v(2, date(2026, 9, 14), date(2026, 8, 20), "-600")),
                  fenster=((0, 2),))
    _gilt(sz, "I3", ("OBERGRENZE", 0, 2, "4920"))


def test_n3_kette_ueber_tag_ausserhalb_des_fensters():
    """OP 2.500 fällig 30.11., verschoben 30.11.→20.12.→06.12.; heute 18.11.: LIVE und
    Planstand zum Wochenbeginn führen ihn am 06.12. (bis 32e4237 fehlte er im LIVE-Soll der
    KW 49, der Planstand führte ihn ohne Bindung)."""
    sz = Szenario(23, date(2026, 11, 18), konten=("4930",),
                  posten=(_op(1, date(2026, 11, 30), "2500", "4930"),),
                  verschiebungen=(_v(1, date(2026, 11, 30), date(2026, 12, 20), "-2500", "4930"),
                                  _v(2, date(2026, 12, 20), date(2026, 12, 6), "-2500", "4930")))
    _gilt(sz, "I6", ("KZ", date(2026, 11, 30), "4930"))


def _n4():
    """OP 1.000 fällig 10.09., auf 02.09. vorgezogen und nicht bezahlt; heute Sa 05.09."""
    return Szenario(24, date(2026, 9, 5), posten=(_op(1, date(2026, 9, 10), "1000"),),
                    verschiebungen=(_v(1, date(2026, 9, 10), date(2026, 9, 2), "-1000"),))


def test_n4_verstrichenes_ziel_gibt_keinen_spielraum():
    """KW 36: Soll 1.000 am 02.09., Ist 0 (C1 −1.000), der offene Posten ist überfällig
    (C3 1.000) → verfügbar 0 (bis 32e4237: weder gebunden noch C3, verfügbar 1.000)."""
    _gilt(_n4(), "I1", ("KZ", date(2026, 8, 31), "LIVE", "4920"))


def test_n4_verstrichenes_ziel_bleibt_in_der_liquiditaet():
    """Der offene Posten rollt – Liquidität am Horizontende 99.000 (bis 32e4237: 100.000)."""
    _gilt(_n4(), "I2b", ("LIQ", _n4().horizont_ende))


def test_n5_planstand_zum_wochenbeginn_rechnet_ueberfaelliges_wie_live():
    """Mo 20.07.: OP 3.000 seit 09.07. überfällig. Er steht im Soll am ersten Bankarbeitstag
    der laufenden Woche und zählt für KW 31 nicht als C3 – bei LIVE wie beim Planstand zum
    20.07. (bis 32e4237 dort verfügbar −3.000)."""
    sz = Szenario(25, date(2026, 7, 20), posten=(_op(1, date(2026, 7, 9), "3000"),))
    _gilt(sz, "I6", ("KZ", date(2026, 7, 27), "4920"))


def test_n7_vorab_bezahlte_rate_gibt_keinen_spielraum():
    """Rate 3.000 am Mo 20.04. bereits am 10.04. gezahlt; heute So 26.04.: LIVE-Soll der
    laufenden Woche enthält sie, die Zahlung vor s0 wird am Plantag ins Ist nachgetragen →
    C1 0, KW 21 verfügbar 0 (bis 32e4237: C1 −3.000, verfügbar 3.000) – ohne Verschiebung."""
    sz = Szenario(27, date(2026, 4, 26),
                  dauer=(Dauer(1, "4920", D("3000"), 18,
                               bezahlt=((date(2026, 4, 20), date(2026, 4, 10)),)),))
    _gilt(sz, "I1", ("KZ", date(2026, 5, 18), "LIVE", "4920"))


def test_n6_trotz_verschiebung_bezahlte_rate_zaehlt_einmal():
    """Rate 2.000 am Mo 08.06. auf den 23.07. verschoben, aber am 08.06. bezahlt (heute
    10.06.): sie gilt als bezahlt, die Restplan-Sicht bucht sie am 23.07. nicht erneut (bis
    32e4237: Liquidität 2.000 zu niedrig)."""
    sz = Szenario(26, date(2026, 6, 10), dauer=(
        Dauer(1, "4920", D("2000"), 7, bezahlt=((date(2026, 6, 8), date(2026, 6, 8)),)),),
        verschiebungen=(_v(1, date(2026, 6, 8), date(2026, 7, 23), "-2000"),))
    _gilt(sz, "I2", ("LIQ", sz.horizont_ende))


def _n8():
    """OP 1.000 seit Sa 11.04. überfällig, steht heute (Di 14.04.) im Plan und wird von dort
    zur Hälfte auf den 22.04. verschoben – der naheliegende Bedienweg im Zellendialog."""
    return Szenario(28, date(2026, 4, 14), konten=("4930",),
                    posten=(_op(1, date(2026, 4, 11), "1000", "4930"),),
                    verschiebungen=(_v(1, date(2026, 4, 14), date(2026, 4, 22), "-500", "4930"),))


def test_n8_verschiebung_vom_rolltag_gibt_keinen_spielraum():
    """KW 16: 500 am 22.04. gebunden, 500 überfällig (C3) → verfügbar 0 (bis 32e4237:
    gebunden −500, C3 1.000, verfügbar 500)."""
    _gilt(_n8(), "I1", ("KZ", date(2026, 4, 13), "LIVE", "4930"))


def test_n8_verschiebung_vom_rolltag_im_planstand_wie_im_plan():
    """Planstand zum 13.04. (Sicht Montag: OP am 13.04.) wendet die Verschiebung vom 14.04.
    an wie der Plan – KW 17 je −500 (bis 32e4237 im Planstand 0)."""
    _gilt(_n8(), "I5w", ("TAG", "4930"))


# ---------------------------------------------------------------------------
# 3. Gegenproben: fachlich korrekte Fälle (die Invarianten dürfen hier nicht anschlagen)
# ---------------------------------------------------------------------------

def test_gegenprobe_n1_ueberfaelliger_posten_mit_budget_der_ursprungswoche():
    """Runde 1 (N1, behoben): überfälliger OP aus KW 32 verschoben, Budget nur KW 32."""
    sz = Szenario(31, date(2026, 8, 12), posten=(_op(1, date(2026, 8, 4)),),
                  budgets=(Budget("4920", date(2026, 8, 3), D("10000")),),
                  verschiebungen=(_v(1, date(2026, 8, 4), date(2026, 8, 18), "-1500"),),
                  planstaende=(0,))
    for montag in (date(2026, 8, 17), date(2026, 8, 24)):
        for basis in ("LIVE", ("PLANSTAND", 0)):
            _gilt(sz, "I1b", ("KZ", montag, basis, "4920"))


def test_gegenprobe_p1_vorab_bezahlter_posten_verschoben():
    """Runde 2 (P1, behoben): vorab bezahlter Posten aus vergangenem Tag verschoben –
    Liquidität unverändert, kein Spielraum."""
    sz = Szenario(32, date(2026, 8, 14), posten=(
        _op(1, date(2026, 8, 12), "3000", zahlungen=((date(2026, 8, 10), "3000"),)),),
        verschiebungen=(_v(1, date(2026, 8, 12), date(2026, 8, 19), "-3000"),))
    _gilt(sz, "I2", ("LIQ", sz.horizont_ende))
    _gilt(sz, "I1", ("KZ", date(2026, 8, 17), "LIVE", "4920"))


def test_gegenprobe_offener_posten_verschoben_und_teilbezahlt():
    """Künftiger OP verschoben, vorher teilbezahlt – Plan, Liquidität, Soll konsistent."""
    sz = Szenario(33, date(2026, 8, 12), posten=(
        _op(1, date(2026, 8, 20), "2000", zahlungen=((date(2026, 8, 11), "500"),)),),
        verschiebungen=(_v(1, date(2026, 8, 20), date(2026, 9, 3), "-1500"),),
        fenster=((0, 13), (1, 4)))
    _gilt(sz, "I2", ("LIQ", sz.horizont_ende))
    _gilt(sz, "I3", ("SUMME", 1, 4, "4920"))
    _gilt(sz, "I4", ("SOLL",))
    for montag in (date(2026, 8, 17), date(2026, 8, 31)):
        _gilt(sz, "I1", ("KZ", montag, "LIVE", "4920"))
        _gilt(sz, "I6", ("KZ", montag, "4920"))


def test_gegenprobe_unbezahlte_rate_im_vortrag_und_planstand_montags():
    """Unbezahlte Rate der laufenden Woche (Vortrag), Montag: Planstand = Plan je Tag."""
    sz = Szenario(34, date(2026, 8, 17), dauer=(Dauer(1, "4210", D("2000"), 11),),
                  konten=("4210",))
    _gilt(sz, "I5", ("TAG", "4210"))
    _gilt(sz, "I1", ("KZ", date(2026, 8, 24), "LIVE", "4210"))


def test_verkleinerung_findet_minimales_szenario(monkeypatch):
    """Die Verkleinerung entfernt alles, was für den Fehlschlag nicht nötig ist – geprüft an
    einem künstlichen Defekt (seit V2 behoben ist): die Freigabe-Kennzahlen rechnen ohne
    Verschiebungen, der Planstand mit."""
    from app.services import bestellungen as svc
    from app.services import liquiditaet as liq

    monkeypatch.setattr(svc, "Zuordnung", lambda db, m, heute, **kw:
                        liq.Zuordnung(db, m, heute, [], **kw))
    gross = Szenario(35, date(2026, 8, 19), konten=("3400", "4920"), posten=(
        _op(1, date(2026, 8, 27)),
        _op(2, date(2026, 8, 27), "700", konto="3400"),
        _op(3, date(2026, 9, 2), "300")),
        dauer=(Dauer(1, "3400", D("1000"), 5, bezahlt=((date(2026, 8, 5), date(2026, 8, 5)),)),),
        verschiebungen=(_v(1, date(2026, 8, 27), date(2026, 9, 8), "-1500"),
                        _v(2, date(2026, 8, 27), date(2026, 9, 1), "-700", "3400")))
    punkt = ("KZ", date(2026, 8, 24), "4920")
    assert punkt_verletzt(gross, "I6", punkt) is not None      # künstlicher Defekt
    klein, _ = verkleinere(gross, "I6", punkt)
    assert len(klein.posten) == 1 and not klein.dauer and len(klein.verschiebungen) == 1
