"""Microsoft-Graph-Client (SharePoint) gegen httpx.MockTransport – ohne Netzwerk."""

from urllib.parse import parse_qs

import httpx
import pytest

from app.services import sharepoint
from app.services.sharepoint import GraphClient, GraphFehler
from app.services.zugaenge import GraphZugang

ZUGANG = GraphZugang("contoso.onmicrosoft.com", "client-1", "secret-1")
G = "https://graph.microsoft.com/v1.0"


@pytest.fixture(autouse=True)
def _cache_leeren():
    sharepoint.token_cache_leeren()
    yield
    sharepoint.token_cache_leeren()


class FakeGraph:
    """Minimaler Graph-Nachbau; protokolliert alle Anfragen."""

    def __init__(self):
        self.anfragen: list[httpx.Request] = []
        self.token_nr = 0
        self.routen: dict[tuple[str, str], object] = {}
        self.token_antwort = None

    def route(self, methode, pfad, antwort):
        self.routen[(methode, pfad)] = antwort

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.anfragen.append(req)
        if req.url.host == "login.microsoftonline.com":
            if self.token_antwort is not None:
                return self.token_antwort
            self.token_nr += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_nr}", "expires_in": 3600})
        antwort = self.routen.get((req.method, req.url.path)) or self.routen.get((req.method, str(req.url)))
        if antwort is None:
            return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "nope"}})
        return antwort(req) if callable(antwort) else antwort

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def graph_anfragen(self):
        return [r for r in self.anfragen if r.url.host == "graph.microsoft.com"]


@pytest.fixture()
def fake(monkeypatch):
    f = FakeGraph()
    monkeypatch.setattr(sharepoint, "http_client_fabrik", f.client)
    return f


# --- Token ------------------------------------------------------------------------


def test_token_anfrage_und_cache(fake):
    assert GraphClient(ZUGANG).token() == "tok1"
    req = fake.anfragen[0]
    assert req.method == "POST"
    assert str(req.url) == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
    form = parse_qs(req.content.decode())
    assert form == {"grant_type": ["client_credentials"], "client_id": ["client-1"],
                    "client_secret": ["secret-1"], "scope": ["https://graph.microsoft.com/.default"]}
    # zweiter Client, gleicher Zugang → aus dem Modul-Cache
    assert GraphClient(ZUGANG).token() == "tok1"
    assert fake.token_nr == 1
    # anderes Secret → eigener Cache-Eintrag
    assert GraphClient(GraphZugang("contoso.onmicrosoft.com", "client-1", "secret-2")).token() == "tok2"
    # Schlüssel enthält das Secret nur als Hash
    assert all("secret-1" not in str(k) for k in sharepoint._token_cache)


def test_token_wird_vor_ablauf_erneuert(fake, monkeypatch):
    jetzt = [1000.0]
    monkeypatch.setattr(sharepoint, "_uhr", lambda: jetzt[0])
    gc = GraphClient(ZUGANG)
    assert gc.token() == "tok1"
    jetzt[0] += 3600 - 301  # noch > 5 min gültig
    assert gc.token() == "tok1"
    jetzt[0] += 2  # weniger als 5 min Restlaufzeit
    assert gc.token() == "tok2"


def test_token_fehler_aadsts(fake):
    fake.token_antwort = httpx.Response(401, json={
        "error": "invalid_client",
        "error_description": "AADSTS7000215: Invalid client secret provided. Ensure the secret being sent "
                             "in the request is the client secret value.\r\nTrace ID: abc\r\nCorrelation ID: def",
    })
    with pytest.raises(GraphFehler) as e:
        GraphClient(ZUGANG).token()
    text = str(e.value)
    assert "Anmeldung bei Microsoft fehlgeschlagen" in text
    assert "AADSTS7000215" in text and "Secret-ID" in text
    assert "Trace ID" not in text  # gekürzt auf die erste Zeile
    assert e.value.status == 401


def test_netzwerkfehler(monkeypatch):
    def kaputt(req):
        raise httpx.ConnectError("Verbindung abgelehnt", request=req)

    monkeypatch.setattr(sharepoint, "http_client_fabrik", lambda: httpx.Client(transport=httpx.MockTransport(kaputt)))
    with pytest.raises(GraphFehler, match="nicht erreichbar"):
        GraphClient(ZUGANG).token()
    sharepoint._token_cache[GraphClient(ZUGANG)._cache_schluessel()] = ("t", sharepoint._uhr() + 3600)
    with pytest.raises(GraphFehler, match="Microsoft Graph nicht erreichbar"):
        GraphClient(ZUGANG).bibliothek("b!x")


def test_401_meldet_neu_an(fake):
    aufrufe = []

    def drive(req):
        aufrufe.append(req.headers["authorization"])
        if len(aufrufe) == 1:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"id": "b!x", "name": "Dokumente", "webUrl": "https://w"})

    fake.route("GET", "/v1.0/drives/b!x", drive)
    assert GraphClient(ZUGANG).bibliothek("b!x") == {"id": "b!x", "name": "Dokumente", "web_url": "https://w"}
    assert aufrufe == ["Bearer tok1", "Bearer tok2"]


# --- Fehlertexte ------------------------------------------------------------------


def test_fehlertexte(fake):
    fake.route("GET", "/v1.0/drives/b!verboten", httpx.Response(403, json={"error": {"code": "accessDenied"}}))
    with pytest.raises(GraphFehler) as e:
        GraphClient(ZUGANG).bibliothek("b!verboten")
    assert e.value.status == 403
    assert "Berechtigung fehlt" in str(e.value) and "Sites.Selected" in str(e.value)
    assert "Files.ReadWrite.All" in str(e.value)

    with pytest.raises(GraphFehler) as e:
        GraphClient(ZUGANG).bibliothek("b!fehlt")
    assert e.value.status == 404 and "nicht gefunden" in str(e.value)


@pytest.mark.parametrize("boese", ["../me", "..", "a/b", "x?y=1", "", "a b", "%2e%2e"])
def test_ungueltige_ids_erreichen_graph_nicht(fake, boese):
    with pytest.raises(GraphFehler, match="Ungültige"):
        GraphClient(ZUGANG).element("b!x", boese) if boese else GraphClient(ZUGANG).bibliothek(boese)
    assert fake.graph_anfragen() == []


# --- Websites / Bibliotheken ----------------------------------------------------------


def test_websites_suche(fake):
    def sites(req):
        assert req.headers["authorization"] == "Bearer tok1"
        assert req.url.params["search"] == "*"
        return httpx.Response(200, json={"value": [
            {"id": "h,2,3", "displayName": "Zeta", "webUrl": "https://h/sites/z", "description": "Z"},
            {"id": "h,4,5", "name": "akte", "webUrl": "https://h/sites/a"},
        ]})

    fake.route("GET", "/v1.0/sites", sites)
    assert GraphClient(ZUGANG).websites("") == [
        {"id": "h,4,5", "name": "akte", "web_url": "https://h/sites/a", "beschreibung": ""},
        {"id": "h,2,3", "name": "Zeta", "web_url": "https://h/sites/z", "beschreibung": "Z"},
    ]
    fake.route("GET", "/v1.0/sites", lambda req: httpx.Response(200, json={"value": [], "x": req.url.params["search"]}))
    assert GraphClient(ZUGANG).websites("Insolvenz") == []
    assert fake.graph_anfragen()[-1].url.params["search"] == "Insolvenz"


def test_websites_per_url(fake):
    fake.route("GET", "/v1.0/sites/contoso.sharepoint.com:/sites/Akte X",
               httpx.Response(200, json={"id": "contoso.sharepoint.com,1,2", "displayName": "Akte X",
                                         "webUrl": "https://contoso.sharepoint.com/sites/Akte%20X"}))
    erg = GraphClient(ZUGANG).websites(
        "https://Contoso.sharepoint.com/sites/Akte%20X/Freigegebene%20Dokumente/Forms/AllItems.aspx")
    assert erg == [{"id": "contoso.sharepoint.com,1,2", "name": "Akte X",
                    "web_url": "https://contoso.sharepoint.com/sites/Akte%20X", "beschreibung": ""}]
    assert fake.graph_anfragen()[-1].url.raw_path.startswith(b"/v1.0/sites/contoso.sharepoint.com:/sites/Akte%20X")
    # Stammwebsite
    fake.route("GET", "/v1.0/sites/contoso.sharepoint.com",
               httpx.Response(200, json={"id": "root", "displayName": "Start"}))
    assert GraphClient(ZUGANG).websites("https://contoso.sharepoint.com/")[0]["name"] == "Start"


def test_bibliotheken(fake):
    fake.route("GET", "/v1.0/sites/h,2,3/drives", httpx.Response(200, json={"value": [
        {"id": "b!2", "name": "Rechnungen", "webUrl": "https://h/r"},
        {"id": "b!1", "name": "Dokumente", "webUrl": "https://h/d"},
    ]}))
    assert GraphClient(ZUGANG).bibliotheken("h,2,3") == [
        {"id": "b!1", "name": "Dokumente", "web_url": "https://h/d"},
        {"id": "b!2", "name": "Rechnungen", "web_url": "https://h/r"},
    ]


def test_verbindung_testen(fake):
    fake.route("GET", "/v1.0/sites", httpx.Response(200, json={"value": [{"id": "s"}]}))
    assert GraphClient(ZUGANG).verbindung_testen() == {"ok": True, "sites_lesbar": True, "hinweis": None}
    fake.route("GET", "/v1.0/sites", httpx.Response(403, json={"error": {"code": "accessDenied"}}))
    erg = GraphClient(ZUGANG).verbindung_testen()
    assert erg["ok"] is True and erg["sites_lesbar"] is False and "Sites.Selected" in erg["hinweis"]
    fake.token_antwort = httpx.Response(400, json={"error_description": "AADSTS90002: Tenant not found."})
    sharepoint.token_cache_leeren()
    with pytest.raises(GraphFehler, match="Tenant nicht gefunden"):
        GraphClient(ZUGANG).verbindung_testen()


# --- Ordner / Elemente -----------------------------------------------------------------


def test_ordner_wurzel_mit_paging(fake):
    fake.route("GET", "/v1.0/drives/b!d/root", httpx.Response(200, json={
        "id": "ROOT", "name": "root", "root": {}, "folder": {"childCount": 3}, "webUrl": "https://h/d"}))
    seite2 = f"{G}/drives/b!d/root/children?$skiptoken=abc"

    def kinder(req):
        if "$skiptoken" in str(req.url):
            return httpx.Response(200, json={"value": [
                {"id": "F1", "name": "angebot.pdf", "file": {"mimeType": "application/pdf"}, "size": 12,
                 "lastModifiedDateTime": "2026-10-01T10:00:00Z", "webUrl": "https://h/d/angebot.pdf"}]})
        assert req.url.params["$top"] == "200"
        assert "parentReference" in req.url.params["$select"]
        return httpx.Response(200, json={"value": [
            {"id": "O2", "name": "Zulieferer", "folder": {"childCount": 0}},
            {"id": "O1", "name": "bestellungen", "folder": {"childCount": 7}, "webUrl": "https://h/d/b"},
        ], "@odata.nextLink": seite2})

    fake.route("GET", "/v1.0/drives/b!d/root/children", kinder)
    erg = GraphClient(ZUGANG).ordner("b!d")
    assert erg["ordner"]["pfad"] == "/" and erg["ordner"]["ist_wurzel"] is True
    assert erg["ordner"]["id"] == "ROOT"
    assert [o["name"] for o in erg["unterordner"]] == ["bestellungen", "Zulieferer"]
    assert erg["unterordner"][0] == {"id": "O1", "name": "bestellungen", "elemente": 7, "web_url": "https://h/d/b"}
    assert erg["dateien"] == [{"id": "F1", "name": "angebot.pdf", "groesse": 12, "mime": "application/pdf",
                               "geaendert": "2026-10-01T10:00:00Z", "web_url": "https://h/d/angebot.pdf"}]
    # alle Graph-Anfragen mit Token
    assert all(r.headers["authorization"] == "Bearer tok1" for r in fake.graph_anfragen())


def test_ordner_paging_grenze(fake, monkeypatch):
    monkeypatch.setattr(sharepoint, "MAX_ELEMENTE", 3)
    fake.route("GET", "/v1.0/drives/b!d/items/O1", httpx.Response(200, json={
        "id": "O1", "name": "A", "folder": {}, "parentReference": {"path": "/drives/b!d/root:"}}))
    zaehler = [0]

    def kinder(req):
        zaehler[0] += 1
        return httpx.Response(200, json={
            "value": [{"id": f"X{zaehler[0]}{i}", "name": f"n{zaehler[0]}{i}", "folder": {}} for i in range(2)],
            "@odata.nextLink": f"{G}/drives/b!d/items/O1/children?$skiptoken={zaehler[0]}"})

    fake.route("GET", "/v1.0/drives/b!d/items/O1/children", kinder)
    erg = GraphClient(ZUGANG).ordner("b!d", "O1")
    assert len(erg["unterordner"]) == 3 and erg["abgeschnitten"] is True
    assert zaehler[0] == 2
    assert erg["ordner"]["pfad"] == "/A"


def test_next_link_fremder_host_wird_nicht_gefolgt(fake):
    fake.route("GET", "/v1.0/drives/b!d/root", httpx.Response(200, json={"id": "R", "root": {}, "folder": {}}))
    fake.route("GET", "/v1.0/drives/b!d/root/children", httpx.Response(200, json={
        "value": [], "@odata.nextLink": "https://boese.example.com/klau"}))
    with pytest.raises(GraphFehler, match="Unerwartete Adresse"):
        GraphClient(ZUGANG).ordner("b!d")
    assert all(r.url.host != "boese.example.com" for r in fake.anfragen)


def test_ordner_kein_ordner(fake):
    fake.route("GET", "/v1.0/drives/b!d/items/F1", httpx.Response(200, json={"id": "F1", "name": "a.pdf", "file": {}}))
    with pytest.raises(GraphFehler, match="kein Ordner"):
        GraphClient(ZUGANG).ordner("b!d", "F1")


def test_element_pfad(fake):
    fake.route("GET", "/v1.0/drives/b!d/items/F1", httpx.Response(200, json={
        "id": "F1", "name": "Bestellung Müller.pdf", "file": {"mimeType": "application/pdf"}, "size": 99,
        "webUrl": "https://h/x",
        "parentReference": {"driveId": "b!d", "id": "O2", "path": "/drives/b!d/root:/Belege%20Nord/2026"}}))
    e = GraphClient(ZUGANG).element("b!d", "F1")
    assert e["pfad"] == "/Belege Nord/2026/Bestellung Müller.pdf"
    assert e["ist_ordner"] is False and e["mime"] == "application/pdf" and e["groesse"] == 99
    assert e["eltern_id"] == "O2" and e["drive_id"] == "b!d"
    assert sharepoint.pfad_unter(e["pfad"], "/belege nord")
    assert sharepoint.pfad_unter(e["pfad"], "/")
    assert not sharepoint.pfad_unter(e["pfad"], "/Belege")
    assert not sharepoint.pfad_unter("/Belege Nordwest/x", "/Belege Nord")
    assert not sharepoint.pfad_unter(None, "/")


# --- Hochladen / Herunterladen -------------------------------------------------------


def test_hochladen_einfach(fake):
    def put(req):
        assert req.headers["authorization"] == "Bearer tok1"
        assert req.headers["content-type"] == "application/pdf"
        assert b"@microsoft.graph.conflictBehavior=rename" in req.url.raw_path
        assert req.content == b"%PDF-1.4 klein"
        return httpx.Response(201, json={"id": "NEU", "name": "2026-10-30_Müller (1).pdf", "size": 14,
                                         "webUrl": "https://h/neu",
                                         "parentReference": {"path": "/drives/b!d/root:/Belege"}})

    fake.route("PUT", "/v1.0/drives/b!d/items/O1:/2026-10-30_Müller.pdf:/content", put)
    erg = GraphClient(ZUGANG).hochladen("b!d", "O1", "2026-10-30_Müller.pdf", b"%PDF-1.4 klein", "application/pdf")
    assert erg == {"id": "NEU", "name": "2026-10-30_Müller (1).pdf", "web_url": "https://h/neu", "groesse": 14,
                   "pfad": "/Belege/2026-10-30_Müller (1).pdf"}
    assert b"/items/O1:/2026-10-30_M%C3%BCller.pdf:/content" in fake.graph_anfragen()[-1].url.raw_path

    # Bibliothekswurzel; ohne parentReference.path → Pfad per Elementabfrage
    fake.route("PUT", "/v1.0/drives/b!d/root:/a.pdf:/content",
               httpx.Response(200, json={"id": "A", "name": "a.pdf"}))
    fake.route("GET", "/v1.0/drives/b!d/items/A", httpx.Response(200, json={
        "id": "A", "name": "a.pdf", "file": {}, "parentReference": {"path": "/drives/b!d/root:"}}))
    erg = GraphClient(ZUGANG).hochladen("b!d", None, "a.pdf", b"x", "application/pdf")
    assert erg["pfad"] == "/a.pdf" and erg["groesse"] == 1


def test_hochladen_upload_session(fake, monkeypatch):
    monkeypatch.setattr(sharepoint, "UPLOAD_STUECK", 2 * 320 * 1024)
    daten = bytes(range(256)) * (4 * 1024 * 4 + 100)  # > 4 MB
    gesamt = len(daten)
    assert gesamt > sharepoint.EINFACHER_UPLOAD_MAX
    upload_url = "https://contoso.sharepoint.com/_api/v2.0/uploadSession?guid=abc&sig=xyz"
    empfangen = bytearray()
    bereiche = []

    def session(req):
        assert req.headers["authorization"] == "Bearer tok1"
        import json as _json
        assert _json.loads(req.content) == {"item": {"@microsoft.graph.conflictBehavior": "rename"}}
        return httpx.Response(200, json={"uploadUrl": upload_url, "expirationDateTime": "2026-10-31T00:00:00Z"})

    def stueck(req):
        assert "authorization" not in req.headers  # vorab signierte URL: kein Token
        bereich = req.headers["content-range"]
        bereiche.append(bereich)
        start, ende = (int(x) for x in bereich.split(" ")[1].split("/")[0].split("-"))
        assert bereich.endswith(f"/{gesamt}")
        assert start == len(empfangen) and ende - start + 1 == len(req.content)
        empfangen.extend(req.content)
        if len(empfangen) < gesamt:
            return httpx.Response(202, json={"nextExpectedRanges": [f"{len(empfangen)}-"]})
        return httpx.Response(201, json={"id": "GROSS", "name": "gross.pdf", "size": gesamt,
                                         "parentReference": {"path": "/drives/b!d/root:/Belege"}})

    fake.route("POST", "/v1.0/drives/b!d/items/O1:/gross.pdf:/createUploadSession", session)
    fake.route("PUT", upload_url, stueck)
    erg = GraphClient(ZUGANG).hochladen("b!d", "O1", "gross.pdf", daten, "application/pdf")
    assert bytes(empfangen) == daten
    assert erg == {"id": "GROSS", "name": "gross.pdf", "web_url": None, "groesse": gesamt, "pfad": "/Belege/gross.pdf"}
    assert len(bereiche) == -(-gesamt // (2 * 320 * 1024))
    # alle Stücke außer dem letzten sind Vielfache von 320 KiB
    for b in bereiche[:-1]:
        start, ende = (int(x) for x in b.split(" ")[1].split("/")[0].split("-"))
        assert (ende - start + 1) % (320 * 1024) == 0


def test_hochladen_session_fehler_bricht_ab(fake, monkeypatch):
    monkeypatch.setattr(sharepoint, "EINFACHER_UPLOAD_MAX", 10)
    upload_url = "https://contoso.sharepoint.com/up/1"
    fake.route("POST", "/v1.0/drives/b!d/root:/x.pdf:/createUploadSession",
               httpx.Response(200, json={"uploadUrl": upload_url}))
    fake.route("PUT", upload_url, httpx.Response(500, json={"error": {"message": "kaputt"}}))
    fake.route("DELETE", upload_url, httpx.Response(204))
    with pytest.raises(GraphFehler, match="Fehler 500"):
        GraphClient(ZUGANG).hochladen("b!d", None, "x.pdf", b"0123456789abc", "application/pdf")
    assert fake.anfragen[-1].method == "DELETE" and "authorization" not in fake.anfragen[-1].headers


@pytest.mark.parametrize("name", ["", "a/b.pdf", "..", "a\\b"])
def test_hochladen_ungueltiger_name(fake, name):
    with pytest.raises(GraphFehler, match="Dateiname"):
        GraphClient(ZUGANG).hochladen("b!d", None, name, b"x", "text/plain")


def test_herunterladen_folgt_umleitung_ohne_token(fake):
    signiert = "https://contoso-my.sharepoint.com/download.aspx?tempauth=XYZ"
    fake.route("GET", "/v1.0/drives/b!d/items/F1/content",
               httpx.Response(302, headers={"location": signiert}))

    def download(req):
        assert "authorization" not in req.headers
        return httpx.Response(200, content=b"%PDF-1.7 inhalt")

    fake.route("GET", signiert, download)
    assert GraphClient(ZUGANG).herunterladen("b!d", "F1") == b"%PDF-1.7 inhalt"
    erste = [r for r in fake.anfragen if r.url.path.endswith("/content")][0]
    assert erste.headers["authorization"] == "Bearer tok1"
    with pytest.raises(GraphFehler, match="zu groß") as e:
        GraphClient(ZUGANG).herunterladen("b!d", "F1", max_bytes=5)
    assert e.value.status == 413


def test_herunterladen_mit_client_der_umleitungen_folgt(fake):
    """Auch ein übergebener Client mit follow_redirects=True gibt den Token nicht weiter."""
    signiert = "https://contoso.sharepoint.com/dl?x=1"
    fake.route("GET", "/v1.0/drives/b!d/items/F1/content", httpx.Response(302, headers={"location": signiert}))
    fake.route("GET", signiert, lambda req: httpx.Response(200, content=req.headers.get("authorization", "ohne").encode()))
    client = httpx.Client(transport=httpx.MockTransport(fake), follow_redirects=True)
    assert GraphClient(ZUGANG, client=client).herunterladen("b!d", "F1") == b"ohne"


def test_herunterladen_unsichere_umleitung(fake):
    fake.route("GET", "/v1.0/drives/b!d/items/F1/content",
               httpx.Response(302, headers={"location": "http://unsicher.example.com/x"}))
    with pytest.raises(GraphFehler, match="Unsichere Weiterleitung"):
        GraphClient(ZUGANG).herunterladen("b!d", "F1")


def test_herunterladen_404(fake):
    with pytest.raises(GraphFehler) as e:
        GraphClient(ZUGANG).herunterladen("b!d", "WEG")
    assert e.value.status == 404 and "Datei nicht gefunden" in str(e.value)


def test_eigener_client_wird_geschlossen(monkeypatch):
    erzeugt = []

    def fabrik():
        c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "t"})))
        erzeugt.append(c)
        return c

    # Standardfabrik → eigener Client, wird geschlossen
    monkeypatch.setattr(sharepoint, "_standard_client", fabrik)
    monkeypatch.setattr(sharepoint, "http_client_fabrik", fabrik)
    with GraphClient(ZUGANG) as gc:
        gc.token()
    assert erzeugt[0].is_closed
    # übergebener Client gehört dem Aufrufer
    eigener = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "t"})))
    with GraphClient(ZUGANG, client=eigener) as gc:
        sharepoint.token_cache_leeren()
        gc.token()
    assert not eigener.is_closed
