from __future__ import annotations

import os
import socket
import ssl
import tempfile
from pathlib import Path

import httpx


class PolymarketUnreachable(RuntimeError):
    """Gamma/CLOB are blocked or intercepted on this network."""


def _peer_names(host: str) -> list[str]:
    ctx = ssl._create_unverified_context()
    with socket.create_connection((host, 443), timeout=8) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as sock:
            der = sock.getpeercert(binary_form=True)
            pem = ssl.DER_cert_to_PEM_cert(der)
    # Intercept ANJ / Gandi: CN *.anj.fr is often only in the DER, not as plaintext in PEM.
    if "YW5qLmZy" in pem or "qLmFuai5m" in pem or "R2FuZGkgU0FT" in pem:
        return ["*.anj.fr", "anj.fr"]
    fd, path = tempfile.mkstemp(suffix=".pem")
    os.close(fd)
    tmp = Path(path)
    try:
        tmp.write_text(pem, encoding="ascii")
        parsed = ssl._ssl._test_decode_cert(str(tmp))  # noqa: SLF001
    except Exception:
        return ["certificat inattendu"]
    finally:
        tmp.unlink(missing_ok=True)
    names: list[str] = []
    for typ, value in parsed.get("subjectAltName") or []:
        if typ == "DNS":
            names.append(str(value))
    if not names:
        for part in parsed.get("subject") or ():
            for key, value in part:
                if key == "commonName":
                    names.append(str(value))
    return names or ["inconnu"]


def wrap_http_error(exc: Exception, host: str) -> Exception:
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" not in text and "Hostname mismatch" not in text:
        return exc
    presented = "inconnu"
    try:
        presented = ", ".join(_peer_names(host))
    except Exception:
        pass
    return PolymarketUnreachable(
        "Polymarket n'est pas joignable en HTTPS depuis ce réseau. "
        f"Le certificat présenté pour {host} est : {presented}. "
        "En France, l'ANJ intercepte souvent polymarket.com. "
        "Il faut un réseau, un VPN ou un proxy qui atteint vraiment Gamma/CLOB "
        "(variables HTTPS_PROXY / HTTP_PROXY), puis `python -m polybot doctor`."
    )


def diagnose(timeout: float = 8.0) -> list[str]:
    lines: list[str] = []
    for host in (
        "gamma-api.polymarket.com",
        "clob.polymarket.com",
        "api.binance.com",
        "api.typesafe.ai",
    ):
        try:
            ip = socket.getaddrinfo(host, 443)[0][4][0]
        except Exception as exc:
            lines.append(f"{host}: DNS KO ({exc})")
            continue
        try:
            names = _peer_names(host)
        except Exception as exc:
            lines.append(f"{host} -> {ip} | TLS KO ({type(exc).__name__})")
            continue
        try:
            httpx.get(f"https://{host}/", timeout=timeout, follow_redirects=True)
            status = "OK"
        except httpx.ConnectError:
            status = "BLOCAGE TLS"
        except Exception:
            status = "OK (HTTP)"
        lines.append(f"{host} -> {ip} | cert={', '.join(names)} | {status}")
    return lines
