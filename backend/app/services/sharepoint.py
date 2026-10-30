"""Microsoft Graph (app-only, Client-Credentials) für die SharePoint-Belegablage.

Websites suchen, Dokumentbibliotheken und Ordner durchsuchen, Dateien hoch- und
herunterladen. Bewusst ohne MSAL/SDK – nur httpx. Tests ersetzen
`http_client_fabrik` (httpx.MockTransport); nie `httpx.Client` global patchen.

Sicherheit:
- Der Bearer-Token geht nur an graph.microsoft.com (auch bei @odata.nextLink).
- Vorab signierte URLs (Download-Weiterleitung, Upload-Session) werden OHNE
  Authorization-Header aufgerufen; der Browser bekommt sie nie zu sehen.
- IDs aus der Oberfläche werden geprüft, bevor sie in einen URL-Pfad gelangen.
"""

import hashlib
import re
import time
from collections.abc import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from ..config import get_settings
from .zugaenge import GraphZugang

GRAPH_BASIS = "https://graph.microsoft.com/v1.0"
LOGIN_BASIS = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"

TOKEN_VORLAUF_SEKUNDEN = 300          # Token 5 Minuten vor Ablauf erneuern
EINFACHER_UPLOAD_MAX = 4 * 1024 * 1024  # darüber Upload-Session
UPLOAD_STUECK = 16 * 320 * 1024       # 5 MiB; Vielfaches von 320 KiB (Graph-Vorgabe)
MAX_ELEMENTE = 2000                   # Paging-Grenze je Ordner
MAX_WEBSITES = 200
SEITE = 200                           # $top
MAX_UMLEITUNGEN = 5

_UMLEITUNG = (301, 302, 303, 307, 308)
_ID_MUSTER = re.compile(r"^[A-Za-z0-9!_.,=~-]{1,512}$")
_KIND_SELECT = "id,name,folder,file,size,webUrl,lastModifiedDateTime,parentReference"
_ELEMENT_SELECT = "id,name,folder,file,size,webUrl,lastModifiedDateTime,parentReference,root"

BERECHTIGUNG_HINWEIS = (
    "Der App-Registrierung fehlt die Berechtigung – in Entra ID die "
    "Anwendungsberechtigung Sites.Selected (mit Freigabe der Website für die App) "
    "oder Sites.ReadWrite.All bzw. Files.ReadWrite.All erteilen und die "
    "Administratorzustimmung erteilen."
)
_AADSTS_HINWEISE = {
    "7000215": "Client-Secret ungültig – bitte den Wert des Secrets eintragen, nicht die Secret-ID.",
    "7000222": "Client-Secret abgelaufen – bitte in Entra ID ein neues Secret erstellen.",
    "700016": "Anwendungs-(Client-)ID im Tenant nicht gefunden – bitte die Anwendungs-ID "
              "(nicht die Objekt-ID) prüfen.",
    "90002": "Tenant nicht gefunden – bitte die Verzeichnis-(Tenant-)ID prüfen.",
    "900023": "Tenant-ID ungültig – bitte die Verzeichnis-(Tenant-)ID prüfen.",
}


class GraphFehler(Exception):
    """Fehler bei Microsoft Graph; `status` = HTTP-Status der Gegenstelle (oder None)."""

    def __init__(self, nachricht: str, status: int | None = None):
        super().__init__(nachricht)
        self.nachricht = nachricht
        self.status = status


def _standard_client() -> httpx.Client:
    t = get_settings().graph_timeout_sekunden
    return httpx.Client(timeout=httpx.Timeout(t), follow_redirects=False)


# Tests ersetzen die Fabrik (z. B. lambda: httpx.Client(transport=httpx.MockTransport(h)))
http_client_fabrik: Callable[[], httpx.Client] = _standard_client

# (tenant, client_id, sha256(secret)) → (access_token, gültig_bis)
_token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}
_uhr: Callable[[], float] = time.monotonic


def token_cache_leeren() -> None:
    _token_cache.clear()


def _id(wert: str | None, was: str = "ID") -> str:
    """ID aus Graph bzw. der Oberfläche für den URL-Pfad prüfen."""
    if not isinstance(wert, str) or not _ID_MUSTER.match(wert) or ".." in wert:
        raise GraphFehler(f"Ungültige SharePoint-{was}", status=404)
    return quote(wert, safe="!,=~")


def _kuerzen(text: str, laenge: int = 240) -> str:
    text = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return text if len(text) <= laenge else text[: laenge - 1] + "…"


def _pfad(item: dict) -> str | None:
    """Pfad relativ zur Bibliothekswurzel („/A/B/name“, Wurzel „/“) aus
    parentReference.path („/drives/{id}/root:/A/B“, prozentkodiert)."""
    if "root" in item:
        return "/"
    pfad = (item.get("parentReference") or {}).get("path")
    if not isinstance(pfad, str):
        return None
    _, trenner, rest = pfad.partition("root:")
    if not trenner:
        return None
    eltern = unquote(rest).rstrip("/")
    return f"{eltern}/{item.get('name') or ''}"


def pfad_unter(pfad: str | None, wurzel: str | None) -> bool:
    """Liegt `pfad` in `wurzel` oder darunter? (SharePoint: ohne Groß/Klein)"""
    if not pfad or not wurzel:
        return False
    p, w = pfad.casefold(), wurzel.casefold().rstrip("/")
    if not w:  # Bibliothekswurzel
        return p.startswith("/")
    return p == w or p.startswith(w + "/")


def _site(d: dict) -> dict:
    return {
        "id": d.get("id"),
        "name": d.get("displayName") or d.get("name") or d.get("webUrl") or d.get("id"),
        "web_url": d.get("webUrl"),
        "beschreibung": d.get("description") or "",
    }


def _drive(d: dict) -> dict:
    return {"id": d.get("id"), "name": d.get("name"), "web_url": d.get("webUrl")}


def _element(d: dict) -> dict:
    ref = d.get("parentReference") or {}
    return {
        "id": d.get("id"),
        "name": "/" if "root" in d else d.get("name"),
        "pfad": _pfad(d),
        "ist_ordner": "folder" in d or "root" in d,
        "ist_wurzel": "root" in d,
        "mime": (d.get("file") or {}).get("mimeType"),
        "groesse": d.get("size"),
        "web_url": d.get("webUrl"),
        "geaendert": d.get("lastModifiedDateTime"),
        "drive_id": ref.get("driveId"),
        "eltern_id": None if "root" in d else ref.get("id"),
    }


class GraphClient:
    """Sitzung gegen Microsoft Graph für einen Zugang (kurzlebig, je Anfrage).

    Ein übergebener `client` gehört dem Aufrufer; einen selbst über die
    Standardfabrik erzeugten schließt `schliessen()` bzw. der with-Block.
    """

    def __init__(self, zugang: GraphZugang, client: httpx.Client | None = None):
        self.zugang = zugang
        self._client = client
        self._eigener = False

    # --- Verbindung ---------------------------------------------------------

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._eigener = http_client_fabrik is _standard_client
            self._client = http_client_fabrik()
        return self._client

    def schliessen(self) -> None:
        if self._client is not None and self._eigener:
            self._client.close()
            self._client = None

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc) -> None:
        self.schliessen()

    def _cache_schluessel(self) -> tuple[str, str, str]:
        z = self.zugang
        return (z.tenant_id, z.client_id, hashlib.sha256(z.client_secret.encode("utf-8")).hexdigest())

    def token(self) -> str:
        """Zugriffstoken (Client-Credentials); zwischengespeichert bis kurz vor Ablauf."""
        schluessel = self._cache_schluessel()
        eintrag = _token_cache.get(schluessel)
        jetzt = _uhr()
        if eintrag and eintrag[1] - TOKEN_VORLAUF_SEKUNDEN > jetzt:
            return eintrag[0]
        z = self.zugang
        url = f"{LOGIN_BASIS}/{quote(z.tenant_id, safe='')}/oauth2/v2.0/token"
        try:
            r = self._http.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": z.client_id,
                    "client_secret": z.client_secret,
                    "scope": SCOPE,
                },
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            raise GraphFehler(f"Microsoft-Anmeldung nicht erreichbar: {e}") from e
        try:
            daten = r.json()
        except ValueError:
            daten = {}
        if r.status_code != 200 or not isinstance(daten, dict) or not daten.get("access_token"):
            beschreibung = ""
            if isinstance(daten, dict):
                beschreibung = str(daten.get("error_description") or daten.get("error") or "")
            code = re.search(r"AADSTS(\d+)", beschreibung)
            hinweis = _AADSTS_HINWEISE.get(code.group(1), "") if code else ""
            text = " ".join(t for t in (hinweis, _kuerzen(beschreibung)) if t) or f"HTTP {r.status_code}"
            raise GraphFehler(f"Anmeldung bei Microsoft fehlgeschlagen: {text}", status=r.status_code)
        try:
            dauer = float(daten.get("expires_in") or 3600)
        except (TypeError, ValueError):
            dauer = 3600.0
        _token_cache[schluessel] = (daten["access_token"], jetzt + dauer)
        return daten["access_token"]

    def _token_verwerfen(self) -> None:
        _token_cache.pop(self._cache_schluessel(), None)

    def _fehler(self, r: httpx.Response, was: str = "Element") -> GraphFehler:
        meldung = ""
        try:
            info = r.json().get("error") or {}
            if isinstance(info, dict):
                meldung = str(info.get("message") or info.get("code") or "")
        except (ValueError, AttributeError):
            pass
        s = r.status_code
        if s in (401, 403):
            text = f"Microsoft Graph: Berechtigung fehlt. {BERECHTIGUNG_HINWEIS}"
        elif s == 404:
            text = f"{was} nicht gefunden (Microsoft Graph)"
        elif s in (429, 503):
            text = "Microsoft Graph ist ausgelastet (Drosselung) – bitte später erneut versuchen"
        elif s == 409:
            text = f"Microsoft Graph meldet einen Konflikt: {_kuerzen(meldung) or was}"
        else:
            text = f"Microsoft Graph meldet Fehler {s}" + (f": {_kuerzen(meldung)}" if meldung else "")
        return GraphFehler(text, status=s)

    def _anfrage(self, methode: str, pfad: str, *, was: str = "Element", erwartet=(200,), **kw) -> httpx.Response:
        """Anfrage an Graph mit Bearer-Token; einmal neu anmelden bei 401."""
        url = pfad if pfad.startswith("https://") else GRAPH_BASIS + pfad
        if not url.startswith(GRAPH_BASIS + "/"):
            raise GraphFehler("Unerwartete Adresse von Microsoft Graph – Anfrage abgebrochen")
        extra = kw.pop("headers", None) or {}
        for versuch in (1, 2):
            kopf = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json", **extra}
            try:
                r = self._http.request(methode, url, headers=kopf, follow_redirects=False, **kw)
            except httpx.HTTPError as e:
                raise GraphFehler(f"Microsoft Graph nicht erreichbar: {e}") from e
            if r.status_code == 401 and versuch == 1:
                self._token_verwerfen()
                continue
            break
        if r.status_code not in erwartet:
            raise self._fehler(r, was)
        return r

    def _json(self, methode: str, pfad: str, **kw) -> dict:
        r = self._anfrage(methode, pfad, **kw)
        try:
            daten = r.json()
        except ValueError as e:
            raise GraphFehler("Ungültige Antwort von Microsoft Graph") from e
        if not isinstance(daten, dict):
            raise GraphFehler("Ungültige Antwort von Microsoft Graph")
        return daten

    def _alle(self, pfad: str, params: dict | None, grenze: int, was: str) -> tuple[list[dict], bool]:
        """Sammelt `value` über @odata.nextLink; (Einträge, abgeschnitten)."""
        eintraege: list[dict] = []
        daten = self._json("GET", pfad, params=params, was=was)
        while True:
            eintraege.extend(d for d in daten.get("value") or [] if isinstance(d, dict))
            weiter = daten.get("@odata.nextLink")
            if len(eintraege) >= grenze:
                return eintraege[:grenze], bool(weiter) or len(eintraege) > grenze
            if not weiter:
                return eintraege, False
            daten = self._json("GET", weiter, was=was)

    # --- Abfragen -----------------------------------------------------------

    def verbindung_testen(self) -> dict:
        """Anmeldung + Lesetest auf Websites; Anmeldefehler werfen GraphFehler."""
        self.token()
        try:
            daten = self._json("GET", "/sites", params={"search": "*", "$select": "id,displayName"}, was="Website")
        except GraphFehler as e:
            if e.status in (401, 403):
                return {
                    "ok": True,
                    "sites_lesbar": False,
                    "hinweis": "Anmeldung erfolgreich. Websites können nicht durchsucht werden "
                               "(z. B. bei Sites.Selected) – im Auswahldialog die Adresse der "
                               "Website eingeben (https://…/sites/…).",
                }
            raise
        if not daten.get("value"):
            return {
                "ok": True,
                "sites_lesbar": False,
                "hinweis": "Anmeldung erfolgreich, aber keine Websites sichtbar – bei Sites.Selected "
                           "im Auswahldialog die Adresse der Website eingeben (https://…/sites/…).",
            }
        return {"ok": True, "sites_lesbar": True, "hinweis": None}

    def websites(self, suche: str = "") -> list[dict]:
        """Websites suchen (leer = alle sichtbaren); eine SharePoint-URL wird direkt
        aufgelöst (funktioniert auch mit Sites.Selected)."""
        s = (suche or "").strip()
        if re.match(r"^https?://", s, re.IGNORECASE):
            teile = urlsplit(s)
            host = (teile.hostname or "").lower()
            if not re.match(r"^[a-z0-9.-]+$", host):
                raise GraphFehler("Ungültige SharePoint-Adresse", status=404)
            pfad = unquote(teile.path or "").rstrip("/")
            treffer = re.match(r"^(/(?:sites|teams|personal)/[^/]+)", pfad, re.IGNORECASE)
            site_pfad = treffer.group(1) if treffer else ""
            ziel = f"/sites/{host}:{quote(site_pfad)}" if site_pfad else f"/sites/{host}"
            return [_site(self._json("GET", ziel, was="Website"))]
        params = {"search": s or "*", "$select": "id,displayName,name,webUrl,description"}
        eintraege, _ = self._alle("/sites", params, MAX_WEBSITES, "Website")
        return sorted((_site(d) for d in eintraege), key=lambda x: str(x["name"] or "").casefold())

    def bibliotheken(self, site_id: str) -> list[dict]:
        eintraege, _ = self._alle(
            f"/sites/{_id(site_id, 'Website-ID')}/drives",
            {"$select": "id,name,webUrl,driveType"},
            MAX_WEBSITES,
            "Website",
        )
        return sorted((_drive(d) for d in eintraege), key=lambda x: str(x["name"] or "").casefold())

    def bibliothek(self, drive_id: str) -> dict:
        return _drive(self._json("GET", f"/drives/{_id(drive_id, 'Bibliotheks-ID')}",
                                 params={"$select": "id,name,webUrl"}, was="Bibliothek"))

    def _item_pfad(self, drive_id: str, item_id: str | None) -> str:
        d = _id(drive_id, "Bibliotheks-ID")
        if item_id is None or item_id == "":
            return f"/drives/{d}/root"
        return f"/drives/{d}/items/{_id(item_id, 'Element-ID')}"

    def element(self, drive_id: str, item_id: str | None) -> dict:
        """Metadaten eines Elements (item_id None = Bibliothekswurzel).
        {"id", "name", "pfad", "ist_ordner", "mime", "groesse", "web_url", …}"""
        return _element(self._json("GET", self._item_pfad(drive_id, item_id),
                                   params={"$select": _ELEMENT_SELECT}, was="Element"))

    def ordner(self, drive_id: str, item_id: str | None = None) -> dict:
        """Inhalt eines Ordners (item_id None = Bibliothekswurzel)."""
        basis = self._item_pfad(drive_id, item_id)
        meta = self._json("GET", basis, params={"$select": _ELEMENT_SELECT}, was="Ordner")
        if "folder" not in meta and "root" not in meta:
            raise GraphFehler("Das gewählte Element ist kein Ordner", status=422)
        kinder, abgeschnitten = self._alle(
            f"{basis}/children", {"$select": _KIND_SELECT, "$top": SEITE}, MAX_ELEMENTE, "Ordner"
        )
        unterordner, dateien = [], []
        for k in kinder:
            if "folder" in k:
                unterordner.append({
                    "id": k.get("id"),
                    "name": k.get("name"),
                    "elemente": int((k.get("folder") or {}).get("childCount") or 0),
                    "web_url": k.get("webUrl"),
                })
            elif "file" in k:
                dateien.append({
                    "id": k.get("id"),
                    "name": k.get("name"),
                    "groesse": k.get("size"),
                    "mime": (k.get("file") or {}).get("mimeType"),
                    "geaendert": k.get("lastModifiedDateTime"),
                    "web_url": k.get("webUrl"),
                })
        unterordner.sort(key=lambda x: str(x["name"] or "").casefold())
        dateien.sort(key=lambda x: str(x["name"] or "").casefold())
        info = _element(meta)
        return {
            "ordner": {
                "id": info["id"],
                "name": info["name"],
                "pfad": info["pfad"],
                "web_url": info["web_url"],
                "ist_wurzel": info["ist_wurzel"],
                "eltern_id": info["eltern_id"],
            },
            "unterordner": unterordner,
            "dateien": dateien,
            "abgeschnitten": abgeschnitten,
        }

    # --- Dateien ------------------------------------------------------------

    def _ziel(self, drive_id: str, ordner_id: str | None, dateiname: str) -> str:
        name = (dateiname or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise GraphFehler("Ungültiger Dateiname", status=422)
        return f"{self._item_pfad(drive_id, ordner_id)}:/{quote(name, safe='')}:"

    def _ergebnis(self, drive_id: str, item: dict, groesse: int) -> dict:
        pfad = _pfad(item)
        if pfad is None and item.get("id"):
            try:
                pfad = self.element(drive_id, item["id"])["pfad"]
            except GraphFehler:
                pfad = None
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "web_url": item.get("webUrl"),
            "groesse": item.get("size") if item.get("size") is not None else groesse,
            "pfad": pfad,
        }

    def hochladen(self, drive_id: str, ordner_id: str | None, dateiname: str, daten: bytes, mime: str) -> dict:
        """Datei in den Ordner hochladen (Namenskonflikt → SharePoint benennt um).
        → {"id", "name", "web_url", "groesse", "pfad"}"""
        ziel = self._ziel(drive_id, ordner_id, dateiname)
        if len(daten) <= EINFACHER_UPLOAD_MAX:
            r = self._anfrage(
                "PUT", f"{ziel}/content?@microsoft.graph.conflictBehavior=rename",
                content=daten, headers={"Content-Type": mime or "application/octet-stream"},
                erwartet=(200, 201), was="Ordner",
            )
            try:
                return self._ergebnis(drive_id, r.json(), len(daten))
            except ValueError as e:
                raise GraphFehler("Ungültige Antwort von Microsoft Graph") from e
        sitzung = self._json(
            "POST", f"{ziel}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "rename"}}, was="Ordner",
        )
        upload_url = sitzung.get("uploadUrl")
        if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
            raise GraphFehler("Microsoft Graph hat keine gültige Upload-Adresse geliefert")
        return self._ergebnis(drive_id, self._stueckweise(upload_url, daten), len(daten))

    def _stueckweise(self, upload_url: str, daten: bytes) -> dict:
        """Upload-Session: Stücke mit Content-Range – vorab signierte URL, OHNE Token."""
        gesamt = len(daten)
        stueck = max(320 * 1024, (UPLOAD_STUECK // (320 * 1024)) * 320 * 1024)
        start = 0
        try:
            while start < gesamt:
                ende = min(start + stueck, gesamt) - 1
                r = self._http.put(
                    upload_url,
                    content=daten[start:ende + 1],
                    headers={"Content-Range": f"bytes {start}-{ende}/{gesamt}"},
                    follow_redirects=False,
                )
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code != 202:
                    fehler = self._fehler(r, "Upload")
                    self._abbrechen(upload_url)
                    raise fehler
                start = ende + 1
        except httpx.HTTPError as e:
            self._abbrechen(upload_url)
            raise GraphFehler(f"Microsoft Graph nicht erreichbar: {e}") from e
        except ValueError as e:
            raise GraphFehler("Ungültige Antwort von Microsoft Graph") from e
        raise GraphFehler("Upload unvollständig – Microsoft Graph hat das Ende nicht bestätigt")

    def _abbrechen(self, upload_url: str) -> None:
        try:
            self._http.delete(upload_url, follow_redirects=False)
        except httpx.HTTPError:
            pass

    def herunterladen(self, drive_id: str, item_id: str, *, max_bytes: int | None = None) -> bytes:
        """Dateiinhalt; Graph leitet (302) auf eine vorab signierte URL um, der
        OHNE Authorization-Header gefolgt wird."""
        url = f"{self._item_pfad(drive_id, item_id)}/content"
        url = GRAPH_BASIS + url
        mit_token = True
        neu_angemeldet = False
        for _ in range(MAX_UMLEITUNGEN + 1):
            kopf = {"Authorization": f"Bearer {self.token()}"} if mit_token else {}
            try:
                with self._http.stream("GET", url, headers=kopf, follow_redirects=False) as r:
                    if r.status_code in _UMLEITUNG:
                        ort = r.headers.get("location")
                        if not ort:
                            raise GraphFehler("Ungültige Weiterleitung von Microsoft Graph")
                        url = urljoin(url, ort)
                        if not url.startswith("https://"):
                            raise GraphFehler("Unsichere Weiterleitung von Microsoft Graph abgelehnt")
                        mit_token = url.startswith(GRAPH_BASIS + "/")
                        continue
                    if r.status_code == 401 and mit_token and not neu_angemeldet:
                        self._token_verwerfen()
                        neu_angemeldet = True
                        continue
                    if r.status_code != 200:
                        r.read()
                        raise self._fehler(r, "Datei")
                    teile, n = [], 0
                    for teil in r.iter_bytes():
                        n += len(teil)
                        if max_bytes is not None and n > max_bytes:
                            raise GraphFehler("Die Datei ist zu groß", status=413)
                        teile.append(teil)
                    return b"".join(teile)
            except httpx.HTTPError as e:
                raise GraphFehler(f"Microsoft Graph nicht erreichbar: {e}") from e
        raise GraphFehler("Zu viele Weiterleitungen beim Herunterladen")
