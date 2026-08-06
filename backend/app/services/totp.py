"""Zeitbasierte Einmalcodes (TOTP, RFC 6238) für die Zwei-Faktor-Anmeldung.

Kompatibel mit gängigen Authenticator-Apps (SHA-1, 6 Stellen, 30-Sekunden-
Schritte). Bewusst ohne Fremdbibliothek – HOTP/TOTP sind wenige Zeilen Stdlib.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

SCHRITT_SEKUNDEN = 30
STELLEN = 6


def neues_geheimnis() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def code_fuer_schritt(geheimnis: str, schritt: int) -> str:
    schluessel = base64.b32decode(geheimnis, casefold=True)
    digest = hmac.new(schluessel, struct.pack(">Q", schritt), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    zahl = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{zahl % 10 ** STELLEN:0{STELLEN}d}"


def pruefe_code(
    geheimnis: str,
    code: str,
    letzter_schritt: int = 0,
    jetzt: float | None = None,
) -> int | None:
    """Prüft einen Code (±1 Zeitschritt Toleranz).

    Liefert den akzeptierten Zeitschritt (für den Wiederverwendungsschutz) oder
    None. Schritte kleiner/gleich `letzter_schritt` werden abgelehnt, damit ein
    abgefangener Code nicht erneut verwendet werden kann.
    """
    code = (code or "").strip().replace(" ", "")
    if len(code) != STELLEN or not code.isdigit():
        return None
    aktuell = int((jetzt if jetzt is not None else time.time()) // SCHRITT_SEKUNDEN)
    for schritt in (aktuell, aktuell - 1, aktuell + 1):
        if schritt <= letzter_schritt:
            continue
        if hmac.compare_digest(code_fuer_schritt(geheimnis, schritt), code):
            return schritt
    return None


def otpauth_uri(geheimnis: str, email: str, aussteller: str = "LiquiPlanung") -> str:
    return (
        f"otpauth://totp/{aussteller}:{email}"
        f"?secret={geheimnis}&issuer={aussteller}&digits={STELLEN}&period={SCHRITT_SEKUNDEN}"
    )
