import os
import socket
import sys
import threading
from pathlib import Path

import uvicorn
import api_server

HTTP_PORT = 8000
HTTPS_PORT = 8443
CERT_DIR = Path(__file__).resolve().parent / ".ssl"
CERT_PATH = CERT_DIR / "dashboard.crt"
KEY_PATH = CERT_DIR / "dashboard.key"


def _lan_ips():
    """Best-effort discovery of this machine's LAN IPv4 addresses for the cert SAN list."""
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _ensure_https_cert():
    """Generate a self-signed cert (covering localhost + LAN IPs) so phones can use
    a secure context, which the Web Speech / mic APIs require on Android Chrome."""
    if CERT_PATH.exists() and KEY_PATH.exists():
        return str(CERT_PATH), str(KEY_PATH)

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime as dt
    import ipaddress

    CERT_DIR.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Future"),
        x509.NameAttribute(NameOID.COMMON_NAME, u"future.local"),
    ])

    san_entries = [x509.DNSName(u"localhost")]
    ip_addresses = ["127.0.0.1"] + _lan_ips()
    for ip in ip_addresses:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow())
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(private_key, hashes.SHA256())
    )

    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_PATH.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return str(CERT_PATH), str(KEY_PATH)


def _run_https_server():
    try:
        cert_path, key_path = _ensure_https_cert()
    except Exception as exc:
        print(f"[future] HTTPS disabled (cert setup failed): {exc}")
        return
    try:
        uvicorn.run(
            api_server.app,
            host="0.0.0.0",
            port=HTTPS_PORT,
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
        )
    except Exception as exc:
        print(f"[future] HTTPS server on :{HTTPS_PORT} failed: {exc}")


if __name__ == "__main__":
    # Serve HTTPS on 8443 alongside the existing HTTP port so mobile browsers get a
    # secure context (required for Web Speech / mic capture on Android Chrome).
    if os.getenv("FUTURE_DISABLE_HTTPS", "").strip().lower() not in ("1", "true", "yes"):
        threading.Thread(target=_run_https_server, daemon=True).start()
    uvicorn.run(api_server.app, host="0.0.0.0", port=HTTP_PORT)

