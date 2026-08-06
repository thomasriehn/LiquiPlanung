import base64

from app.services.totp import code_fuer_schritt, neues_geheimnis, otpauth_uri, pruefe_code

# RFC-6238-Testvektor: Geheimnis "12345678901234567890" (SHA-1)
RFC_GEHEIMNIS = base64.b32encode(b"12345678901234567890").decode()


def test_rfc6238_testvektoren():
    # t=59s -> Schritt 1 -> 94287082 (letzte 6 Stellen: 287082)
    assert code_fuer_schritt(RFC_GEHEIMNIS, 1) == "287082"
    # t=1111111109s -> Schritt 37037036 -> 07081804 -> 081804
    assert code_fuer_schritt(RFC_GEHEIMNIS, 37037036) == "081804"


def test_pruefe_code_mit_toleranz_und_replay():
    jetzt = 1111111109.0
    schritt = int(jetzt // 30)
    code = code_fuer_schritt(RFC_GEHEIMNIS, schritt)
    assert pruefe_code(RFC_GEHEIMNIS, code, 0, jetzt=jetzt) == schritt
    # Nachbarschritt wird toleriert
    code_davor = code_fuer_schritt(RFC_GEHEIMNIS, schritt - 1)
    assert pruefe_code(RFC_GEHEIMNIS, code_davor, 0, jetzt=jetzt) == schritt - 1
    # Wiederverwendung wird abgelehnt
    assert pruefe_code(RFC_GEHEIMNIS, code, schritt, jetzt=jetzt) is None
    # falscher/unbrauchbarer Code
    assert pruefe_code(RFC_GEHEIMNIS, "000000", 0, jetzt=jetzt) is None
    assert pruefe_code(RFC_GEHEIMNIS, "12345", 0, jetzt=jetzt) is None


def test_geheimnis_und_uri():
    geheimnis = neues_geheimnis()
    assert len(base64.b32decode(geheimnis)) == 20
    uri = otpauth_uri(geheimnis, "test@example.com")
    assert uri.startswith("otpauth://totp/LiquiPlanung:test@example.com?")
    assert f"secret={geheimnis}" in uri
