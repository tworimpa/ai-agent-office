"""Create temporary localhost TLS and App credentials for the desktop fixture."""
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


root = Path(sys.argv[1])


def create_key(name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = root / name
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return key


create_key("app.pem")
tls = create_key("tls.pem")
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(tls.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(tls, hashes.SHA256()))
(root / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
