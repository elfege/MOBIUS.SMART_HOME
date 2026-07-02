"""
Certificate Installation Routes (MOBIUS.HOME)
=============================================
FastAPI route group that serves the local CA certificate and provides a
guided installation page for all major platforms.

MOBIUS uses a single shared local CA (managed canonically by MOBIUS.NVR) to
sign every suite app's TLS certificate. A user installs the CA cert ONCE on a
device and every MOBIUS app (NVR, HOME, TILES) is trusted permanently — no more
"Your connection is not private" on each page load.

Ported from the NVR Flask blueprint (`0_MOBIUS.NVR/services/cert_routes.py`).
FastAPI has no blueprints, so instead of a module-level `Blueprint` we expose
`register_cert_routes(app, templates)` which the application factory calls once
at startup. This keeps app.py free of the cert-serving boilerplate while still
sharing app.py's single `Jinja2Templates` instance.

Endpoints (identical contract to NVR):
    GET /install-cert              → Guided installation page (HTML)
    GET /install-cert/download     → Raw CA cert download (.crt)
    GET /install-cert/mobileconfig → iOS/macOS configuration profile
    GET /api/cert/status           → JSON: cert info + whether CA exists

CA path: ``<project_root>/nginx/certs/ca.pem``. In the container this is the
read-only mount ``./nginx/certs:/app/nginx/certs:ro`` declared in
docker-compose.yml; the same PEM nginx serves as the TLS chain root, so the
fingerprint shown on the page is guaranteed to match the live certificate.
"""

import os
import uuid
import base64
import hashlib
import subprocess
import logging
from datetime import datetime

from fastapi import Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — relative to project root (this file lives in <root>/services/)
# ---------------------------------------------------------------------------
CERTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nginx", "certs"
)
CA_CERT_PATH = os.path.join(CERTS_DIR, "ca.pem")


def _ca_exists() -> bool:
    """Return True if the CA certificate file exists and is readable."""
    return os.path.isfile(CA_CERT_PATH) and os.access(CA_CERT_PATH, os.R_OK)


def _get_ca_fingerprint():
    """Return the SHA-256 fingerprint of the CA cert (for verification display),
    or None if the cert is missing or openssl is unavailable."""
    if not _ca_exists():
        return None
    try:
        result = subprocess.run(
            ["openssl", "x509", "-fingerprint", "-sha256", "-noout", "-in", CA_CERT_PATH],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Output: "sha256 Fingerprint=AA:BB:CC:..."
            return result.stdout.strip().split("=", 1)[-1]
    except Exception as e:  # openssl missing in the image, etc. — non-fatal
        logger.warning(f"Could not read CA fingerprint: {e}")
    return None


def _get_ca_expiry():
    """Return the CA certificate expiry as a human-readable string, or None."""
    if not _ca_exists():
        return None
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", CA_CERT_PATH],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Output: "notAfter=Feb 19 15:00:00 2036 GMT"
            return result.stdout.strip().split("=", 1)[-1]
    except Exception as e:
        logger.warning(f"Could not read CA expiry: {e}")
    return None


def register_cert_routes(app, templates) -> None:
    """Attach the certificate-installation routes to ``app``.

    Args:
        app: the FastAPI application instance.
        templates: the shared ``Jinja2Templates`` instance from app.py, so the
            install page renders with the same template environment as the rest
            of the UI.
    """

    @app.get("/install-cert", response_class=HTMLResponse, include_in_schema=False)
    async def cert_install_page(request: Request):
        """Render the guided certificate installation page. Platform detection
        happens client-side (JS). Works over both HTTP and HTTPS."""
        return templates.TemplateResponse(
            request,
            "cert_install.html",
            {
                "ca_available": _ca_exists(),
                "fingerprint": _get_ca_fingerprint(),
                "expiry": _get_ca_expiry(),
            },
        )

    @app.get("/install-cert/download", include_in_schema=False)
    async def cert_download():
        """Serve the CA certificate as a .crt download.

        Content-Type ``application/x-x509-ca-cert`` triggers the OS cert-install
        dialog on most platforms; the .crt download_name makes Windows/Android
        recognize it as a certificate rather than an opaque blob."""
        if not _ca_exists():
            raise HTTPException(
                status_code=404,
                detail="CA certificate not found on this host.",
            )
        return FileResponse(
            CA_CERT_PATH,
            media_type="application/x-x509-ca-cert",
            filename="MOBIUS_Local_CA.crt",
        )

    @app.get("/install-cert/mobileconfig", include_in_schema=False)
    async def cert_mobileconfig():
        """Serve an iOS/macOS configuration profile (.mobileconfig) that installs
        the CA. More guided on Apple devices than a raw .crt. The profile is an
        XML plist embedding the CA cert as base64 DER; opening it on iOS/macOS
        triggers the system profile installer."""
        if not _ca_exists():
            raise HTTPException(status_code=404, detail="CA certificate not found.")

        try:
            result = subprocess.run(
                ["openssl", "x509", "-in", CA_CERT_PATH, "-outform", "DER"],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                raise HTTPException(status_code=500, detail="Failed to convert certificate.")
            cert_der_b64 = base64.b64encode(result.stdout).decode("ascii")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to generate mobileconfig: {e}")
            raise HTTPException(status_code=500, detail="Certificate conversion failed.")

        # Stable UUIDs derived from cert content (same cert ⇒ same profile UUID,
        # so re-installing replaces rather than duplicates the profile).
        cert_hash = hashlib.sha256(result.stdout).hexdigest()
        profile_uuid = str(uuid.UUID(cert_hash[:32]))
        payload_uuid = str(uuid.UUID(cert_hash[32:64] if len(cert_hash) >= 64 else cert_hash[:32]))

        mobileconfig = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadCertificateFileName</key>
            <string>MOBIUS_Local_CA.cer</string>
            <key>PayloadContent</key>
            <data>{cert_der_b64}</data>
            <key>PayloadDescription</key>
            <string>Installs the MOBIUS Local CA certificate so your device trusts MOBIUS HTTPS connections.</string>
            <key>PayloadDisplayName</key>
            <string>MOBIUS Local CA</string>
            <key>PayloadIdentifier</key>
            <string>com.mobius-home.cert.{payload_uuid}</string>
            <key>PayloadType</key>
            <string>com.apple.security.root</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>Trust the MOBIUS HTTPS certificate. After installing, go to Settings -&gt; General -&gt; About -&gt; Certificate Trust Settings and enable full trust for "MOBIUS Local CA".</string>
    <key>PayloadDisplayName</key>
    <string>MOBIUS — Trust Certificate</string>
    <key>PayloadIdentifier</key>
    <string>com.mobius-home.profile.{profile_uuid}</string>
    <key>PayloadOrganization</key>
    <string>MOBIUS Home</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{profile_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>"""

        return Response(
            content=mobileconfig,
            media_type="application/x-apple-aspen-config",
            headers={
                "Content-Disposition": 'attachment; filename="MOBIUS_Trust_Certificate.mobileconfig"'
            },
        )

    @app.get("/api/cert/status", include_in_schema=False)
    async def cert_status():
        """JSON cert status. Used by the frontend banner to decide whether to
        show the 'install certificate' prompt."""
        return JSONResponse(
            {
                "ca_available": _ca_exists(),
                "fingerprint": _get_ca_fingerprint(),
                "expiry": _get_ca_expiry(),
                "download_url": "/install-cert/download",
                "mobileconfig_url": "/install-cert/mobileconfig",
                "install_page_url": "/install-cert",
            }
        )

    logger.info("Certificate installation routes registered (/install-cert, /api/cert/status)")
