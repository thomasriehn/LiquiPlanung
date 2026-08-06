"""Kompletter Durchlauf des generierten Testdatensatzes „Nordlicht Möbelwerk“.

Erzeugt den Datensatz mit dem heutigen Datum als Stichtag und spielt ihn wie in
der ANLEITUNG beschrieben über die API ein: Mandant, IBAN, DATEV-Stapel, BWA,
OP-Liste, MT940, CAMT.053 – und prüft die erwarteten Ergebnisse (automatische
§ 38-Einstufung, Bestandsanker, Abgleichvorschläge aller vier Arten).
"""

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from test_api import _login

GENERATOR = Path(__file__).resolve().parents[2] / "testdaten" / "erzeuge_testdaten.py"


def test_testdatensatz_kompletter_durchlauf(client, tmp_path):
    heute = date.today()
    subprocess.run(
        [sys.executable, str(GENERATOR), "--stichtag", heute.isoformat(),
         "--ziel", str(tmp_path)],
        check=True, capture_output=True,
    )

    _login(client)
    mandant_id = client.post(
        "/api/mandanten",
        json={
            "name": "Nordlicht Möbelwerk GmbH", "kurzname": "nordlicht",
            "kontenrahmen": "SKR03", "bundesland": "SH",
            "ust_zeitraum": "MONAT", "dauerfrist": True,
            "verfahrensstatus": "VORLAEUFIG",
            "insolvenz_stichtag": (heute - timedelta(days=21)).isoformat(),
        },
    ).json()["id"]

    konten = client.get(f"/api/mandanten/{mandant_id}/konten").json()["konten"]
    k1200 = next(k for k in konten if k["nummer"] == "1200")
    client.patch(f"/api/konten/{k1200['id']}",
                 json={"iban": "DE02 1203 0000 0000 2020 51"})

    # DATEV-Stapel (drei Monate) – alle Konten der Vorlage, keine unbekannten
    for datei in sorted(tmp_path.glob("0[1-3]_buchungen_*.csv")):
        erg = client.post(
            f"/api/mandanten/{mandant_id}/import",
            files={"datei": (datei.name, datei.read_bytes(), "text/csv")},
        ).json()
        assert erg["format"] == "DATEV", erg
        assert erg["anzahl"] > 40
        assert erg["unbekannte_konten"] == []

    erg = client.post(
        f"/api/mandanten/{mandant_id}/import?format=bwa",
        files={"datei": ("bwa.csv", (tmp_path / "04_bwa_saldenliste_12monate.csv").read_bytes(), "text/csv")},
    ).json()
    assert erg["anzahl"] == 144

    # OP-Liste: 20 Posten, zwei Altrechnungen automatisch als § 38 gesperrt
    erg = client.post(
        f"/api/mandanten/{mandant_id}/posten/import",
        files={"datei": ("op.csv", (tmp_path / "05_op_liste.csv").read_bytes(), "text/csv")},
    ).json()
    assert erg["angelegt"] == 20
    assert erg["insolvenzforderungen"] == 2

    # Kontoauszüge: Umsätze + Bestandsanker über die IBAN
    erg = client.post(
        f"/api/mandanten/{mandant_id}/bank-import",
        files={"datei": ("auszug.sta", (tmp_path / "06_kontoauszug_mt940.sta").read_bytes(), "text/plain")},
    ).json()
    assert erg["format"] == "MT940"
    assert erg["anzahl"] == 7
    assert len(erg["bestandsanker"]) == 1
    erg = client.post(
        f"/api/mandanten/{mandant_id}/bank-import",
        files={"datei": ("auszug.xml", (tmp_path / "07_kontoauszug_camt053.xml").read_bytes(), "text/xml")},
    ).json()
    assert erg["format"] == "CAMT"
    assert erg["anzahl"] == 2

    # Abgleichvorschläge: alle vier Arten mit erwarteter Konfidenz
    vorschlaege = client.get(
        f"/api/mandanten/{mandant_id}/op-ausgleich/vorschlaege"
    ).json()
    nach_betrag = {round(v["transaktion"]["betrag"], 2): v for v in vorschlaege}
    assert len(vorschlaege) == 6, sorted(nach_betrag)

    assert (nach_betrag[11900.00]["art"], nach_betrag[11900.00]["konfidenz"]) == ("VOLL", "SICHER")
    assert nach_betrag[11900.00]["posten"]["belegnr"] == "RE-2026-1041"
    sammel = nach_betrag[-7735.00]
    assert (sammel["art"], sammel["konfidenz"]) == ("SAMMEL", "SICHER")
    assert {p["belegnr"] for p in sammel["posten_liste"]} == {"RE-P-77812", "RE-P-77903"}
    teil = nach_betrag[5000.00]
    assert (teil["art"], teil["konfidenz"]) == ("TEIL", "MOEGLICH")
    assert teil["posten"]["rest_nach_zahlung"] == 7495.00
    assert (nach_betrag[-2910.00]["art"], nach_betrag[-2910.00]["konfidenz"]) == ("VOLL", "SICHER")
    assert (nach_betrag[8330.00]["art"], nach_betrag[8330.00]["konfidenz"]) == ("VOLL", "SICHER")
    assert (nach_betrag[-1350.00]["art"], nach_betrag[-1350.00]["konfidenz"]) == ("VOLL", "SICHER")
    # Miete, Kartenumsätze und Entgelt bleiben bewusst ohne Vorschlag
    assert not {-8900.00, 1250.00, -42.50} & set(nach_betrag)

    # alle Vorschläge übernehmen (inkl. Teilzahlung)
    paare = [
        {"transaktion_id": v["transaktion"]["id"],
         "posten_ids": [p["id"] for p in v.get("posten_liste", [])] or [v["posten"]["id"]]}
        for v in vorschlaege
    ]
    erg = client.post(f"/api/mandanten/{mandant_id}/op-ausgleich", json={"paare": paare}).json()
    assert erg["ausgeglichen"] == 6

    posten = client.get(f"/api/mandanten/{mandant_id}/posten").json()
    nach_beleg = {p["belegnr"]: p for p in posten}
    assert nach_beleg["RE-2026-1041"]["status"] == "BEZAHLT"
    assert nach_beleg["RE-P-77812"]["status"] == "BEZAHLT"
    assert nach_beleg["RE-P-77903"]["status"] == "BEZAHLT"
    assert nach_beleg["RE-IT-2201"]["status"] == "BEZAHLT"     # Skonto-Toleranz
    teilzahlung = nach_beleg["RE-2026-1042"]
    assert teilzahlung["status"] == "OFFEN"
    assert teilzahlung["bezahlt_betrag"] == 5000.00
    assert teilzahlung["restbetrag"] == 7495.00

    # Plan: gesperrte Altforderungen ausgewiesen, Bankbestand aus den Auszügen
    plan = client.get(f"/api/mandanten/{mandant_id}/plan").json()
    assert plan["gesperrte_insolvenzforderungen"] == 12680.00 + 7140.00
