#!/usr/bin/env bash
# LiquiPlanung auf den Stand vor dem letzten Update zurücksetzen (Rücksprung).
#
# Aufruf als root:
#   /var/backups/liquiplanung/werkzeug/zuruecksetzen.sh [STANDDATEI] [--ja]
# Ohne STANDDATEI gilt der Rücksprungpunkt des letzten Updates
# ($LIQUI_SICHERUNG/letzter-stand.env, geschrieben von deploy/aktualisieren.sh).
# --ja überspringt die Rückfrage.
#
# Ablauf:
#   1. Datenbank sichern (pg_dump) – auch der Rücksprung ist umkehrbar.
#   2. Schema zurückstufen (alembic downgrade auf die Revision von vorher), solange noch
#      der neue Code ausgecheckt ist – nur er kennt die neuen Migrationen.
#   3. Den Commit von vorher auschecken (losgelöst, der Zweig bleibt unverändert).
#   4. Container neu bauen und starten, warten, Schema-Revision prüfen.
#
# Die Daten bleiben erhalten: Was seit dem Update erfasst wurde, bleibt in der Datenbank.
# Beim Zurückstufen entfallen nur Spalten, die die neuen Migrationen angelegt haben
# (z. B. erstellt_am an Posten, Terminen und Dauerbuchungen). Zur vollständigen
# Wiederherstellung des Datenbestands vor dem Update dient die Sicherung aus der
# Standdatei (SICHERUNG_DATEI) – sie verwirft alles seither Erfasste.
#
# Wieder vorwärts: $LIQUI_SICHERUNG/werkzeug/aktualisieren.sh ZWEIG
set -Eeuo pipefail

info() { printf '[zuruecksetzen] %s\n' "$*"; }
fehler() { printf '[zuruecksetzen] FEHLER: %s\n' "$*" >&2; exit 1; }

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
  local stand_datei="" ja=0 arg
  for arg in "$@"; do
    case "$arg" in
      -h|--help) sed -n '2,25p' "$0"; return 0 ;;
      -y|--ja) ja=1 ;;
      *) stand_datei=$arg ;;
    esac
  done
  command -v docker >/dev/null || fehler "docker fehlt"
  docker compose version >/dev/null 2>&1 || fehler "docker compose fehlt"

  local sicherung
  sicherung=${LIQUI_SICHERUNG:-/var/backups/liquiplanung}
  stand_datei=${stand_datei:-$sicherung/letzter-stand.env}
  [ -f "$stand_datei" ] || fehler "Standdatei $stand_datei fehlt – Rücksprungpunkte liegen unter $sicherung/stand-*.env"

  local ZEITPUNKT="" VERZEICHNIS="" VORHER_COMMIT="" VORHER_ZWEIG="" VORHER_REVISION=""
  local NACHHER_COMMIT="" NACHHER_ZWEIG="" SICHERUNG_DATEI=""
  # shellcheck disable=SC1090
  source "$stand_datei"
  [ -n "$VORHER_COMMIT" ] || fehler "Standdatei $stand_datei enthält keinen Commit"

  local verz=${LIQUI_VERZEICHNIS:-$VERZEICHNIS}
  [ -n "$verz" ] || verz=$(git rev-parse --show-toplevel 2>/dev/null) || fehler "Installationsverzeichnis unbekannt – LIQUI_VERZEICHNIS setzen"
  cd "$verz"
  [ -f docker-compose.yml ] || fehler "$verz enthält keine docker-compose.yml"
  git cat-file -e "$VORHER_COMMIT^{commit}" 2>/dev/null || fehler "Commit $VORHER_COMMIT ist im Repository nicht vorhanden"

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no
    fehler "lokale Änderungen im Installationsverzeichnis – erst sichern oder verwerfen"
  fi

  local jetzt_commit
  jetzt_commit=$(git rev-parse HEAD)
  db_bereit
  local jetzt_revision
  jetzt_revision=$(db_revision)

  printf '\nRücksprung auf den Stand vor dem Update vom %s\n' "$ZEITPUNKT"
  printf '  jetzt:   %s  Schema %s\n' "$(git log -1 --format='%h %s' "$jetzt_commit")" "${jetzt_revision:-?}"
  printf '  zurück:  %s  Schema %s\n' "$(git log -1 --format='%h %s' "$VORHER_COMMIT")" "${VORHER_REVISION:-?}"
  printf '  Daten seit dem Update bleiben erhalten; vorher wird die Datenbank gesichert.\n\n'
  if [ "$jetzt_commit" = "$VORHER_COMMIT" ] && [ "$jetzt_revision" = "$VORHER_REVISION" ]; then
    info "Der Stand von vorher ist bereits aktiv. Nichts zu tun."
    return 0
  fi
  if [ -n "$NACHHER_COMMIT" ] && [ "$jetzt_commit" != "$NACHHER_COMMIT" ]; then
    info "Hinweis: ausgecheckt ist nicht der Stand dieses Updates ($(git rev-parse --short "$NACHHER_COMMIT")) – seither wurde erneut aktualisiert oder zurückgesetzt."
  fi
  if [ "$ja" -ne 1 ]; then
    local antwort
    read -r -p "Fortfahren? [j/N] " antwort
    case "$antwort" in j|J|ja|Ja|JA|y|Y) ;; *) info "Abgebrochen."; return 1 ;; esac
  fi

  local ts sicherung_datei
  ts=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$sicherung"
  sicherung_datei="$sicherung/vor-ruecksprung-$ts.sql.gz"
  info "Sichere die Datenbank nach $sicherung_datei …"
  db_sichern "$sicherung_datei"

  if [ -n "$VORHER_REVISION" ] && [ -n "$jetzt_revision" ] && [ "$jetzt_revision" != "$VORHER_REVISION" ]; then
    info "Stufe das Schema von $jetzt_revision auf $VORHER_REVISION zurück (mit dem ausgecheckten Code) …"
    docker compose stop app >/dev/null 2>&1 || true
    docker compose build app
    docker compose run --rm --no-deps -T app python -m alembic downgrade "$VORHER_REVISION" </dev/null \
      || fehler "Zurückstufen fehlgeschlagen – Datenbank unverändert lassen, Anwendung mit 'docker compose up -d' wieder starten und Ausgabe prüfen"
    local rev_nach
    rev_nach=$(db_revision)
    [ "$rev_nach" = "$VORHER_REVISION" ] || fehler "Schema steht nach dem Zurückstufen auf '$rev_nach' statt '$VORHER_REVISION'"
  else
    info "Schema ist bereits auf ${VORHER_REVISION:-?} – kein Zurückstufen nötig."
  fi

  info "Checke $(git log -1 --format='%h %s' "$VORHER_COMMIT") aus …"
  git checkout -q --detach "$VORHER_COMMIT"

  info "Baue und starte die Container …"
  docker compose up -d --build

  info "Warte auf die Anwendung …"
  if ! auf_app_warten; then
    docker compose logs --tail 40 app || true
    fehler "die Anwendung antwortet nicht – siehe 'docker compose logs app'"
  fi
  local rev_ende
  rev_ende=$(db_revision)
  [ -z "$VORHER_REVISION" ] || [ "$rev_ende" = "$VORHER_REVISION" ] \
    || info "Hinweis: Schema steht auf '$rev_ende', erwartet '$VORHER_REVISION'."

  info "Fertig. Aktiv ist wieder $(git log -1 --format='%h %s' HEAD) (Schema ${rev_ende:-?})."
  printf '  Sicherung vor dem Rücksprung: %s\n' "$sicherung_datei"
  printf '  Wieder auf den neuen Stand:   %s %s\n\n' "$sicherung/werkzeug/aktualisieren.sh" "${NACHHER_ZWEIG:-ZWEIG}"
}

main "$@"; exit $?
