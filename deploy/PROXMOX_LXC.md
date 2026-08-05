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

## 6. Updates

```bash
cd /opt/liquiplanung
git pull
docker compose up -d --build
```

Das Schema wird beim Start automatisch angelegt (Erstversion ohne Migrationstool; vor
Updates mit Schemaänderungen Sicherung erstellen – siehe KONZEPT.md, Roadmap Alembic).
