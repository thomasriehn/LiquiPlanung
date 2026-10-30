"""Verschlüsselung gespeicherter Geheimnisse (Graph-Client-Secret, KI-API-Key).

Fernet (AES-128-CBC + HMAC-SHA256); der Schlüssel wird per HKDF-SHA256 aus
GEHEIMNIS_SCHLUESSEL bzw. – ohne Angabe – aus SECRET_KEY abgeleitet. Ändert sich
der Schlüssel (oder wird eine Sicherung einer anderen Installation eingespielt),
sind gespeicherte Token nicht mehr lesbar: `entschluesseln` liefert dann None,
nie eine Ausnahme – die Oberfläche zeigt „bitte neu eingeben“.
"""

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import get_settings

HKDF_INFO = b"liquiplanung/geheimnisse/v1"


@lru_cache(maxsize=8)
def _fernet(schluessel: str) -> Fernet:
    roh = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(
        schluessel.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(roh))


def _aktiv() -> Fernet:
    s = get_settings()
    return _fernet(s.geheimnis_schluessel or s.secret_key)


def verschluesseln(klartext: str) -> str:
    """Klartext → Fernet-Token (ASCII-String)."""
    return _aktiv().encrypt(klartext.encode("utf-8")).decode("ascii")


def entschluesseln(token: str) -> str | None:
    """Fernet-Token → Klartext; None, wenn nicht entschlüsselbar (anderer
    Schlüssel, beschädigter Wert)."""
    if not token or not isinstance(token, str):
        return None
    try:
        return _aktiv().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return None


def cache_leeren() -> None:
    """Abgeleitete Schlüssel verwerfen (Tests nach Schlüsselwechsel)."""
    _fernet.cache_clear()
