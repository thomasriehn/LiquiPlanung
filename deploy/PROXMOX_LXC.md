# Betrieb im Proxmox-LXC-Container

Zielbild: Ein LXC-Container (Debian 12 / Ubuntu 24.04) auf dem Proxmox-Host, darin Docker
mit zwei Containern (`app` + `db`/PostgreSQL) per `docker compose`.

## 1. LXC-Container anlegen

Docker in LXC benötigt Nesting. Auf dem Proxmox-Host:

```bash
pct create 210 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname liquiplanung \
  --cores 2 --memory 4096 --swap 512 \
  --rootfs local-lvm:16 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --onboot 1
pct start 210
```

Bei bestehendem Container: `pct set 210 --features nesting=1,keyctl=1` und neu starten.

## 2. Docker im Container installieren

```bash
pct enter 210
apt update && apt install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

## 3. Anwendung installieren

```bash
cd /opt
git clone <REPO-URL> liquiplanung
cd liquiplanung
cp .env.example .env
nano .env        # POSTGRES_PASSWORD, SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD setzen
docker compose up -d --build
docker compose logs -f app   # "Admin angelegt" abwarten
```

Die Anwendung läuft dann auf `http://<LXC-IP>:8000`. Erstanmeldung mit `ADMIN_EMAIL` /
`ADMIN_PASSWORD` aus der `.env`; danach Passwort über die Benutzerverwaltung ändern und
`DEMO_DATEN=false` setzen, wenn der Demo-Mandant nicht mehr benötigt wird
(`docker compose up -d` zum Übernehmen).

## 4. TLS / Reverse Proxy (empfohlen)

Anwendung nicht direkt ins Internet stellen. Entweder nur im LAN/VPN betreiben oder einen
Reverse Proxy (z. B. Caddy oder nginx auf dem LXC bzw. ein zentraler Proxy) mit TLS
vorschalten:

```
# /etc/caddy/Caddyfile
liqui.example.de {
    reverse_proxy 127.0.0.1:8000
}
```

Bei **nginx** das Upload-Limit anheben – ZIP-Pakete mit Kontoauszug und
Belegen sind schnell mehrere MB groß (Standard: 1 MB):

```
client_max_body_size 50m;
```

Die Texterkennung für Kontoauszüge ohne Textebene (tesseract inkl. deutscher
Sprachdaten) ist im Docker-Image enthalten. Wer die Anwendung ohne Docker
betreibt: `apt install tesseract-ocr tesseract-ocr-deu`.

## 5. Datensicherung

Tägliche Sicherung per Cron im LXC (zusätzlich zur Proxmox-Container-Sicherung):

```bash
cat >/etc/cron.d/liqui-backup <<'EOF'
15 2 * * * root cd /opt/liquiplanung && docker compose exec -T db pg_dump -U liqui liqui | gzip > /var/backups/liqui-$(date +\%F).sql.gz
45 2 * * * root find /var/backups -name 'liqui-*.sql.gz' -mtime +30 -delete
EOF
```

Wiederherstellung:

```bash
gunzip -c /var/backups/liqui-JJJJ-MM-TT.sql.gz | docker compose exec -T db psql -U liqui liqui
```

**Belegablage (Bestelldateien).** Liegt die Ablage eines Mandanten lokal, landen
die Dateien im Docker-Volume `ablage` (bzw. unter `ABLAGE_HOST_PFAD`, falls in der
`.env` ein Host-Pfad wie eine eingebundene Netzfreigabe gesetzt ist). Weder die
integrierten Sicherungen noch `pg_dump` enthalten diese Dateien – also zusätzlich
sichern:

```bash
cat >>/etc/cron.d/liqui-backup <<'EOF'
30 2 * * * root docker run --rm -v liquiplanung_ablage:/a -v /var/backups:/b alpine tar czf /b/liqui-ablage-$(date +\%F).tgz -C /a .
50 2 * * * root find /var/backups -name 'liqui-ablage-*.tgz' -mtime +30 -delete
EOF
```

(Der Volume-Name setzt sich aus dem Verzeichnisnamen `liquiplanung` und `_ablage`
zusammen; `docker volume ls` zeigt ihn.) Dateien in SharePoint sichert SharePoint selbst.

Je Mandant liegt dort ein Unterordner `<Kurzname>-<ID>` (bzw. der unter
„Einstellungen → Belegablage“ gewählte Ordner). Beim Löschen eines Mandanten wird
sein Ordner nach `.geloescht/<Kurzname>-<ID>-<Zeitstempel>` verschoben, nicht
gelöscht; die Sicherung oben erfasst ihn weiter. Die verschobenen Ordner zeigt
`docker compose exec app ls /srv/liquiplanung/ablage/.geloescht`; nach Ablauf der
Aufbewahrungsfristen von Hand entfernen.

**Geheimnisse.** Client-Secret und KI-API-Key werden in der Datenbank mit einem aus
`SECRET_KEY` abgeleiteten Schlüssel verschlüsselt (alternativ `GEHEIMNIS_SCHLUESSEL`).
Wer `SECRET_KEY` ändert oder eine Sicherung auf einer anderen Installation einspielt,
muss die Geheimnisse unter „System“ neu eingeben – oder vorab `GEHEIMNIS_SCHLUESSEL`
setzen, dann bleibt `SECRET_KEY` frei änderbar.

## 6. Updates

```bash
cd /opt/liquiplanung
git pull
docker compose up -d --build
```

Schemaänderungen laufen über Alembic-Migrationen und werden beim Anwendungsstart
automatisch angewendet (`alembic upgrade head`); ältere Installationen aus der Zeit
vor Alembic werden beim ersten Start übernommen. Vor Updates empfiehlt sich dennoch
eine Sicherung (Abschnitt 5).

Bricht eine Migration ab, startet die Anwendung nicht, und die Datenbank bleibt auf
dem bisherigen Stand; die Ursache steht in `docker compose logs app`. Beispiel:
Migration `799a2322545e` (eine Zahlung löst höchstens eine Bestellung ab) bricht ab,
wenn eine Buchung oder ein Bankumsatz bereits mehreren Bestellungen zugeordnet ist.
Dann den vorherigen Stand wieder starten (`git checkout <vorheriger Stand>` und
`docker compose up -d --build`), die doppelten Ablösungen in der Anwendung aufheben
und das Update wiederholen.

## 7. SharePoint-Anbindung (Microsoft Graph, optional)

Für die Belegablage in SharePoint braucht LiquiPlanung eine App-Registrierung in
Microsoft Entra ID (app-only, ohne Benutzeranmeldung):

1. Entra Admin Center → **App-Registrierungen** → „Neue Registrierung“, Name z. B.
   `LiquiPlanung`, Kontotyp „nur dieses Organisationsverzeichnis“, keine Umleitungs-URI.
2. Auf der Übersichtsseite **Verzeichnis-(Mandanten-)ID** (= Tenant-ID) und
   **Anwendungs-(Client-)ID** notieren – nicht die Objekt-ID.
3. **Zertifikate & Geheimnisse** → „Neuer geheimer Clientschlüssel“ → den **Wert**
   (nicht die Geheimnis-ID) kopieren; er wird nur einmal angezeigt. Ablaufdatum notieren.
4. **API-Berechtigungen** → Microsoft Graph → *Anwendungsberechtigungen*, dann
   „Administratorzustimmung erteilen“. Zwei Varianten:
   - einfach: `Sites.ReadWrite.All` – alle Websites durchsuchbar und beschreibbar;
   - restriktiv: `Sites.Selected` – Zugriff nur auf ausdrücklich freigegebene
     Websites, z. B. per PnP-PowerShell:
     `Grant-PnPAzureADAppSitePermission -AppId <Client-ID> -DisplayName LiquiPlanung -Site https://<tenant>.sharepoint.com/sites/<Website> -Permissions Write`.
     Die Website-Suche funktioniert damit nicht. Im Auswahldialog stattdessen
     die Website-URL (auch ein Freigabelink aus „Link kopieren“) eingeben oder
     Drive-ID und Ordner-ID manuell eintragen.
5. In LiquiPlanung unter **System** Tenant-ID, Client-ID und Secret eintragen und
   „Verbindung testen“. Der Test meldet sich immer neu an und zeigt damit auch
   gerade erst erteilte Berechtigungen. Danach je Mandant unter
   **Einstellungen → Belegablage** den Ordner wählen. Ordner zweier Mandanten dürfen
   weder gleich sein noch ineinander liegen (auch nicht die Bibliothekswurzel).

Statt in der Oberfläche kann das Secret auch in der `.env` stehen
(`GRAPH_CLIENT_SECRET=…`); es hat dann Vorrang und wird nicht in der Datenbank gespeichert.

Der Container braucht ausgehend HTTPS zu `login.microsoftonline.com`,
`graph.microsoft.com` und `*.sharepoint.com` (Downloads werden dorthin umgeleitet).

## 8. KI-Zugang zum Auslesen von Bestellungen (optional)

Unter **System → KI-Zugang** eine OpenAI-kompatible Schnittstelle eintragen:
API-URL (z. B. `https://api.openai.com/v1`, Azure OpenAI/OpenRouter oder ein lokaler
Server wie Ollama `http://<host>:11434/v1` bzw. vLLM), Modell und API-Key (bei lokalen
Servern ggf. leer). „Seitenbilder mitsenden“ nur aktivieren, wenn das Modell Bilder
verarbeitet (Vision); dann werden Scans und Fotos zusätzlich als Bild übertragen.

Alternativ API-URL und Key in der `.env` setzen: `LLM_URL=…` und `LLM_API_KEY=…`.
Der Key aus der Umgebung gilt nur zusammen mit `LLM_URL`; die API-URL ist dann unter
„System“ schreibgeschützt, Modell und Vision bleiben dort einstellbar. Ohne `LLM_URL`
wird `LLM_API_KEY` nicht verwendet (Hinweis unter „System“). Wer nur `LLM_URL` setzt,
sollte den unter „System“ gespeicherten Key prüfen – er geht dann an diese URL.

Ändert man unter „System“ den Server der API-URL (Schema, Host oder Port), wird ein
gespeicherter Key verworfen und ist neu einzugeben. Über `http://` geht ein Key nur
an localhost bzw. Adressen im eigenen Netz (private IPs, Namen ohne Punkt wie ein
Docker-Dienst `ollama`, `.local`, `.lan`, `.internal`); für Cloud-Dienste `https://`
verwenden.

**Datenschutz:** Zum Auslesen wird der Inhalt der Bestelldatei an den KI-Dienst
übertragen. Bei Cloud-Anbietern ist ein Auftragsverarbeitungsvertrag nötig; wer das
vermeiden will, betreibt ein lokales Modell. Ohne KI-Zugang bleibt die manuelle
Erfassung vollständig nutzbar.
