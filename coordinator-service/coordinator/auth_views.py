import base64
import hashlib
import hmac
import json
import os
import secrets
import smtplib
import threading
import time
import redis
from django.conf import settings
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from rest_framework.decorators import api_view
from rest_framework.response import Response


def _get_redis_client():
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        return redis.Redis.from_url(
            broker_url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2
        )
    except Exception:
        return None


def _normalize_email(email_str):
    if not email_str:
        return ""
    return str(email_str).strip().lower()


def _generate_actor_id(email):
    norm = _normalize_email(email)
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"usr_{sha}"


def _create_jwt(actor_id, email, expires_in=1800):
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "actor_id": actor_id,
        "email": email,
        "iat": now,
        "exp": now + expires_in
    }

    def b64url(data_bytes):
        return base64.urlsafe_b64encode(data_bytes).rstrip(b"=").decode("utf-8")

    h_str = b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_str = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_str}.{p_str}".encode("utf-8")

    secret = settings.SECRET_KEY.encode("utf-8")
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    sig_str = b64url(sig)

    return f"{h_str}.{p_str}.{sig_str}"


def verify_jwt(token):
    if not token or "." not in token:
        return None, "Invalid token format"

    parts = token.split(".")
    if len(parts) != 3:
        return None, "Malformed JWT structure"

    h_str, p_str, sig_str = parts
    signing_input = f"{h_str}.{p_str}".encode("utf-8")
    secret = settings.SECRET_KEY.encode("utf-8")
    expected_sig = hmac.new(secret, signing_input, hashlib.sha256).digest()

    def b64url_decode(s):
        rem = len(s) % 4
        if rem > 0:
            s += "=" * (4 - rem)
        return base64.urlsafe_b64decode(s.encode("utf-8"))

    try:
        actual_sig = b64url_decode(sig_str)
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None, "Invalid token signature"

        payload_json = b64url_decode(p_str).decode("utf-8")
        payload = json.loads(payload_json)

        if "exp" in payload and payload["exp"] < int(time.time()):
            return None, "Token has expired"

        return payload, None
    except Exception as e:
        return None, str(e)


def get_actor_from_request(request):
    """
    Extracts authenticated actor identity from Authorization header or request payload.
    """
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1].strip()
    elif "token" in request.data:
        token = request.data.get("token")

    if token:
        payload, err = verify_jwt(token)
        if payload:
            return {
                "actor_id": payload.get("actor_id"),
                "email": payload.get("email"),
                "is_authenticated": True
            }

    # Fallback to direct X-Actor-ID header or payload actor_id
    actor_id = request.headers.get("X-Actor-ID") or request.data.get("actor_id")
    if actor_id:
        return {
            "actor_id": str(actor_id),
            "email": request.data.get("email") or str(actor_id),
            "is_authenticated": False
        }

    return None


from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


import resend

resend.api_key = os.getenv("RESEND_API_KEY")


def _dispatch_otp_email(to_email, otp_code):
    subject = f"Your DTC Arena Verification Code: {otp_code}"
    text_content = f"""Hello,

Your verification code for the Distributed Transaction Coordinator Arena is:

    {otp_code}

This code will expire in 5 minutes.

If you did not request this verification code, please ignore this message.

— Distributed Transaction Coordinator Security"""

    html_content = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px; background: #0f172a; border-radius: 12px; color: #f8fafc;">
      <h2 style="color: #38bdf8; margin: 0 0 12px;">DTC Arena Verification</h2>
      <p style="color: #94a3b8; font-size: 14px; margin: 0 0 20px;">Use the 6-digit verification code below to verify your fan identity and access live seat holds:</p>
      <div style="background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 18px; text-align: center; margin: 20px 0;">
        <span style="font-size: 32px; font-weight: 800; letter-spacing: 8px; color: #38bdf8; font-family: monospace;">{otp_code}</span>
      </div>
      <p style="color: #64748b; font-size: 12px; margin: 0;">This code will expire in 5 minutes. Never share this code with anyone.</p>
    </div>"""

    try:
        params: resend.Emails.SendParams = {
            "from": "DTC Security <otp@relaydtc.online>",
            "to": [to_email],
            "subject": subject,
            "html": html_content,
            "text": text_content,
        }
        result = resend.Emails.send(params)
        print(f"[Email Dispatch Success] Verification code {otp_code} delivered to {to_email} via Resend. id={result.get('id')}")
    except Exception as e:
        print(f"[Email Dispatch Error] {type(e).__name__}: {e}")


@api_view(["POST"])
def send_otp(request):
    """
    Phase 1: Validates email, generates random 6-digit OTP, stores in Redis (5-min TTL),
    and sends via Gmail SMTP with console fallback.
    """
    email_raw = request.data.get("email") or request.data.get("email_address")
    if not email_raw:
        return Response({"error": "email is required"}, status=400)

    email = _normalize_email(email_raw)
    try:
        validate_email(email)
    except ValidationError:
        return Response({"error": "Invalid email address format"}, status=400)

    client = _get_redis_client()
    session_key = f"dtc:session:{email}"
    if client:
        try:
            if client.exists(session_key):
                return Response({"error": "Account already in use on another device."}, status=403)
        except Exception:
            pass

    otp_code = f"{secrets.randbelow(900000) + 100000}"
    cache_key = f"dtc:otp:{email}"

    client = _get_redis_client()
    if client:
        try:
            client.setex(cache_key, 300, otp_code)
        except Exception:
            pass

    # Redundant memory backup in case Redis is rebooting
    try:
        from django.core.cache import cache
        cache.set(cache_key, otp_code, timeout=300)
    except Exception:
        pass

    # Dispatch email asynchronously in background thread so HTTP response never hangs
    threading.Thread(target=_dispatch_otp_email, args=(email, otp_code), daemon=True).start()

    actor_id = _generate_actor_id(email)

    return Response({
        "success": True,
        "message": f"Verification code sent to {email}",
        "email": email,
        "actor_id": actor_id,
        "expires_in_seconds": 300
    }, status=200)


@api_view(["POST"])
def verify_otp(request):
    """
    Phase 2: Compares submitted OTP against Redis value, deletes Redis key on match (single-use),
    and issues a 30-minute JWT token with actor_id.
    """
    email_raw = request.data.get("email") or request.data.get("email_address")
    submitted_otp = str(request.data.get("otp", "")).strip()

    if not email_raw or not submitted_otp:
        return Response({"error": "email and otp are required"}, status=400)

    email = _normalize_email(email_raw)
    cache_key = f"dtc:otp:{email}"
    stored_otp = None

    client = _get_redis_client()
    if client:
        try:
            stored_otp = client.get(cache_key)
        except Exception:
            pass

    if not stored_otp:
        try:
            from django.core.cache import cache
            stored_otp = cache.get(cache_key)
        except Exception:
            pass

    if not stored_otp or str(stored_otp).strip() != submitted_otp:
        return Response({"error": "Invalid or expired verification code. Please request a new one."}, status=400)

    # Delete key on success (single-use)
    if client:
        try:
            client.delete(cache_key)
        except Exception:
            pass
    try:
        from django.core.cache import cache
        cache.delete(cache_key)
    except Exception:
        pass

    actor_id = _generate_actor_id(email)
    jwt_token = _create_jwt(actor_id=actor_id, email=email, expires_in=1800)

    # Register active session
    if client:
        try:
            client.setex(f"dtc:session:{email}", 30, "active")
        except Exception:
            pass

    return Response({
        "success": True,
        "message": "Authenticated successfully!",
        "token": jwt_token,
        "actor_id": actor_id,
        "email": email,
        "expires_in": 1800
    }, status=200)


@api_view(["GET"])
def get_me(request):
    """
    Phase 3: Returns current authenticated actor identity from JWT footprint.
    """
    actor_info = get_actor_from_request(request)
    if not actor_info:
        return Response({"authenticated": False, "actor": None}, status=401)

    return Response({
        "authenticated": True,
        "actor": actor_info
    }, status=200)


@api_view(["POST"])
def heartbeat(request):
    """
    Extends the TTL of the user's active session in Redis.
    """
    actor_info = get_actor_from_request(request)
    if not actor_info or not actor_info.get("is_authenticated"):
        return Response({"error": "Unauthorized"}, status=401)
    
    email = actor_info.get("email")
    if email:
        client = _get_redis_client()
        if client:
            try:
                client.setex(f"dtc:session:{email}", 30, "active")
            except Exception:
                pass
                
    return Response({"status": "alive"}, status=200)


@api_view(["POST"])
def logout(request):
    """
    Deletes the user's active session in Redis.
    """
    actor_info = get_actor_from_request(request)
    if actor_info and actor_info.get("email"):
        email = actor_info.get("email")
        client = _get_redis_client()
        if client:
            try:
                client.delete(f"dtc:session:{email}")
            except Exception:
                pass
                
    return Response({"status": "logged_out"}, status=200)
