#!/usr/bin/env bash
# LiquiPlanung aktualisieren – mit Datenbanksicherung und Rücksprungpunkt.
#
# Aufruf im Installationsverzeichnis (z. B. /opt/liquiplanung), als root:
#   deploy/aktualisieren.sh [ZWEIG]
# Ohne ZWEIG wird der ausgecheckte Zweig aktualisiert (nach einem Rücksprung ist keiner
# ausgecheckt – dann den Zweig angeben, z. B. deploy/aktualisieren.sh claude/bestellfreigabe).
#
# Ablauf:
#   1. Stand festhalten: Commit, Zweig und Schema-Revision der Datenbank (Alembic).
#   2. Datenbank sichern (pg_dump, gzip).
#   3. Standdatei schreiben – sie ist der Rücksprungpunkt für deploy/zuruecksetzen.sh.
#   4. Neuen Stand holen (nur Fast-Forward, lokale Änderungen brechen ab).
#   5. Container neu bauen und starten; die Anwendung migriert beim Start selbst.
#   6. Warten, bis die Anwendung antwortet; Schema-Revision prüfen.
#
# Sicherungen, Standdateien und eine Kopie der beiden Skripte liegen unter
# $LIQUI_SICHERUNG (Standard /var/backups/liquiplanung). Die Kopien bleiben nutzbar,
# auch wenn ein Rücksprung einen Stand ohne diese Skripte auscheckt.
#
# Umgebungsvariablen:
#   LIQUI_SICHERUNG    Ablage für Sicherungen und Standdateien
#   LIQUI_VERZEICHNIS  Installationsverzeichnis (sonst aus dem Skriptpfad bzw. dem
#                      aktuellen Verzeichnis ermittelt)
#   LIQUI_WARTEN       Sekunden, die auf den Start der Anwendung gewartet wird (180)
set -Eeuo pipefail

info() { printf '[aktualisieren] %s\n' "$*"; }
fehler() { printf '[aktualisieren] FEHLER: %s\n' "$*" >&2; exit 1; }

verzeichnis_finden() {
  if [ -n "${LIQUI_VERZEICHNIS:-}" ]; then
    printf '%s\n' "$LIQUI_VERZEICHNIS"
    return
  fi
  local skriptdir
  skriptdir=$(cd "$(dirname "$0")" && pwd)
  if [ -f "$skriptdir/../docker-compose.yml" ] && git -C "$skriptdir/.." rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$skriptdir/.." rev-parse --show-toplevel
    return
  fi
  if [ -f docker-compose.yml ] && git rev-parse --git-dir >/dev/null 2>&1; then
    git rev-parse --show-toplevel
    return
  fi
  local stand=${LIQUI_SICHERUNG:-/var/backups/liquiplanung}/letzter-stand.env
  if [ -f "$stand" ]; then
    local v
    v=$(VERZEICHNIS=""; source "$stand"; printf '%s' "$VERZEICHNIS")
    if [ -n "$v" ] && [ -f "$v/docker-compose.yml" ]; then
      printf '%s\n' "$v"
      return
    fi
  fi
  fehler "Installationsverzeichnis nicht gefunden – im Installationsverzeichnis aufrufen oder LIQUI_VERZEICHNIS setzen"
}

db_bereit() {
  local i
  docker compose up -d db >/dev/null
  for i in $(seq 1 60); do
    if docker compose exec -T db sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' </dev/null; then
      return 0
    fi
    sleep 1
  done
  fehler "Datenbank-Container antwortet nicht (docker compose logs db)"
}

db_revision() {
  docker compose exec -T db sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null' \
    </dev/null | tr -d '[:space:]' || true
}

db_sichern() {
  local ziel=$1
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' </dev/null \
    | gzip >"$ziel.teil"
  gzip -t "$ziel.teil" || fehler "Sicherung $ziel ist beschädigt"
  [ "$(gzip -dc "$ziel.teil" | head -c 4096 | wc -c)" -gt 0 ] || fehler "Sicherung $ziel ist leer"
  mv "$ziel.teil" "$ziel"
}

app_antwortet() {
  docker compose exec -T app python -c \
    "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=5).status == 200 else 1)" \
    </dev/null >/dev/null 2>&1
}

auf_app_warten() {
  local bis=$((SECONDS + ${LIQUI_WARTEN:-180}))
  while [ "$SECONDS" -lt "$bis" ]; do
    if app_antwortet; then
      return 0
    fi
    sleep 3
  done
  return 1
}

main() {
  local zweig_arg=${1:-}
  case "$zweig_arg" in
    -h|--help) sed -n '2,30p' "$0"; return 0 ;;
  esac
  command -v docker >/dev/null || fehler "docker fehlt"
  docker compose version >/dev/null 2>&1 || fehler "docker compose fehlt"

  local verz sicherung skript_quelle
  skript_quelle=$(cd "$(dirname "$0")" && pwd)
  verz=$(verzeichnis_finden)
  cd "$verz"
  [ -f docker-compose.yml ] || fehler "$verz enthält keine docker-compose.yml"
  [ -f .env ] || fehler "$verz/.env fehlt"
  sicherung=${LIQUI_SICHERUNG:-/var/backups/liquiplanung}
  mkdir -p "$sicherung/werkzeug"

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no
    fehler "lokale Änderungen im Installationsverzeichnis – erst sichern oder verwerfen"
  fi

  local zweig
  zweig=${zweig_arg:-$(git symbolic-ref --short -q HEAD || true)}
  [ -n "$zweig" ] || fehler "kein Zweig ausgecheckt (z. B. nach einem Rücksprung) – Zweig angeben: $0 claude/bestellfreigabe"

  info "Hole origin/$zweig …"
  git fetch origin "$zweig"
  local vorher_commit vorher_zweig neu_commit
  vorher_commit=$(git rev-parse HEAD)
  vorher_zweig=$(git symbolic-ref --short -q HEAD || true)
  neu_commit=$(git rev-parse FETCH_HEAD)
  if [ "$vorher_commit" = "$neu_commit" ] && [ "$vorher_zweig" = "$zweig" ]; then
    info "Bereits auf dem neuesten Stand ($(git log -1 --format='%h %s' HEAD)). Nichts zu tun."
    return 0
  fi
  git merge-base --is-ancestor "$vorher_commit" "$neu_commit" \
    || info "Hinweis: origin/$zweig ist kein Nachfolger des laufenden Stands (z. B. nach einem Rücksprung oder Zweigwechsel)."

  info "Datenbank prüfen …"
  db_bereit
  local vorher_revision
  vorher_revision=$(db_revision)
  [ -n "$vorher_revision" ] || info "Hinweis: keine Schema-Revision gefunden (Erstinstallation?) – ein Rücksprung kann das Schema dann nicht zurückstufen."

  local ts sicherung_datei stand_datei
  ts=$(date +%Y%m%d-%H%M%S)
  sicherung_datei="$sicherung/vor-update-$ts.sql.gz"
  info "Sichere die Datenbank nach $sicherung_datei …"
  db_sichern "$sicherung_datei"

  stand_datei="$sicherung/stand-$ts.env"
  {
    printf '# Rücksprungpunkt von deploy/aktualisieren.sh – gelesen von deploy/zuruecksetzen.sh\n'
    printf 'ZEITPUNKT=%q\n' "$ts"
    printf 'VERZEICHNIS=%q\n' "$verz"
    printf 'VORHER_COMMIT=%q\n' "$vorher_commit"
    printf 'VORHER_ZWEIG=%q\n' "$vorher_zweig"
    printf 'VORHER_REVISION=%q\n' "$vorher_revision"
    printf 'NACHHER_COMMIT=%q\n' "$neu_commit"
    printf 'NACHHER_ZWEIG=%q\n' "$zweig"
    printf 'SICHERUNG_DATEI=%q\n' "$sicherung_datei"
  } >"$stand_datei"
  cp "$stand_datei" "$sicherung/letzter-stand.env"
  info "Rücksprungpunkt: $stand_datei"

  info "Wechsle auf $zweig ($(git log -1 --format='%h %s' "$neu_commit")) …"
  if git show-ref --verify --quiet "refs/heads/$zweig"; then
    git checkout -q "$zweig"
    git merge -q --ff-only "$neu_commit" \
      || fehler "lokaler Zweig $zweig weicht von origin ab – bitte von Hand klären (Rücksprung: $sicherung/werkzeug/zuruecksetzen.sh)"
  else
    git checkout -q -b "$zweig" "$neu_commit"
    git branch -q --set-upstream-to="origin/$zweig" "$zweig" || true
  fi

  # Werkzeug-Kopien aus dem neuen Stand (überleben einen Rücksprung auf alte Stände)
  for f in aktualisieren.sh zuruecksetzen.sh; do
    if [ -f "deploy/$f" ]; then
      cp "deploy/$f" "$sicherung/werkzeug/$f"
    elif [ -f "$skript_quelle/$f" ]; then
      cp "$skript_quelle/$f" "$sicherung/werkzeug/$f"
    fi
  done
  chmod +x "$sicherung"/werkzeug/*.sh 2>/dev/null || true

  info "Baue und starte die Container (die Anwendung migriert beim Start) …"
  docker compose up -d --build

  info "Warte auf die Anwendung …"
  if ! auf_app_warten; then
    docker compose logs --tail 40 app || true
    printf '\n'
    fehler "die Anwendung antwortet nicht. Ursache steht oben bzw. in 'docker compose logs app'. Zurück zum vorigen Stand: $sicherung/werkzeug/zuruecksetzen.sh"
  fi

  local neu_revision
  neu_revision=$(db_revision)
  info "Fertig."
  printf '\n  vorher:  %s  Schema %s\n' "$(git log -1 --format='%h %s' "$vorher_commit")" "${vorher_revision:-?}"
  printf '  jetzt:   %s  Schema %s\n' "$(git log -1 --format='%h %s' HEAD)" "${neu_revision:-?}"
  printf '  Sicherung:      %s\n' "$sicherung_datei"
  printf '  Rücksprung mit: %s\n\n' "$sicherung/werkzeug/zuruecksetzen.sh"
}

main "$@"; exit $?
