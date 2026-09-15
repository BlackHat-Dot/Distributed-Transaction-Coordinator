import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import redis
import resend
from django.conf import settings
from django.core.cache import cache
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from rest_framework.decorators import api_view
from rest_framework.response import Response


_redis_last_check = 0
_redis_is_available = False
_redis_client_instance = None


def _get_redis_client():
    global _redis_last_check, _redis_is_available, _redis_client_instance
    now = time.monotonic()
    if _redis_client_instance is not None and _redis_is_available:
        return _redis_client_instance
    if now - _redis_last_check < 5.0 and not _redis_is_available:
        return None
    _redis_last_check = now
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    try:
        client = redis.Redis.from_url(
            broker_url, decode_responses=True, socket_connect_timeout=0.05, socket_timeout=0.05
        )
        client.ping()
        _redis_is_available = True
        _redis_client_instance = client
        return client
    except Exception:
        _redis_is_available = False
        _redis_client_instance = None
        return None


def _is_dev_auth_mode():
    is_testing = bool(getattr(settings, "IS_TESTING", False)) or ("test" in sys.argv)
    if is_testing:
        return False
    return bool(getattr(settings, "DEBUG", False)) or os.getenv("DTC_DEV_AUTH_MODE", "").lower() in ("true", "1")


def _normalize_email(email_str):
    return str(email_str).strip().lower()


def _generate_actor_id(email):
    norm = _normalize_email(email)
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"usr_{sha}"


def _get_client_ip(request):
    """
    Extracts the real client IP address.
    Handles reverse proxies (Railway, Nginx, Cloudflare) via X-Forwarded-For.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # X-Forwarded-For: <client>, <proxy1>, <proxy2>
        # The leftmost entry is the actual client.
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _email_hash(email):
    """Short hash of email used in Redis keys (avoids storing raw emails in key names)."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]


def _ip_block_key(ip, email):
    return f"dtc:ip_block:{ip}:{_email_hash(email)}"


def _ip_fail_key(ip, email):
    """Counts wrong OTP submissions from this IP for this email."""
    return f"dtc:ip_fail:{ip}:{_email_hash(email)}"


def _ip_abandon_key(ip, email):
    """Counts OTP sends from this IP for this email with no successful verify."""
    return f"dtc:ip_abandon:{ip}:{_email_hash(email)}"


def _create_jwt(user_id, email, expires_in=None, actor_id=None):
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    uid = user_id or actor_id
    if expires_in is None:
        expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))
    payload = {
        "user_id": uid,
        "user_email": email,
        "actor_id": uid,
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
    Extracts authenticated user/actor identity from Authorization header or request payload.
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
            uid = payload.get("user_id") or payload.get("actor_id")
            uemail = payload.get("user_email") or payload.get("email")
            return {
                "user_id": uid,
                "user_email": uemail,
                "actor_id": uid,
                "email": uemail,
                "is_authenticated": True
            }

    # Fallback to direct X-User-ID / X-Actor-ID header or payload
    user_id = (
        request.headers.get("X-User-ID")
        or request.headers.get("X-Actor-ID")
        or request.data.get("user_id")
        or request.data.get("actor_id")
    )
    user_email = (
        request.headers.get("X-User-Email")
        or request.data.get("user_email")
        or request.data.get("email")
    )
    if user_id:
        return {
            "user_id": str(user_id),
            "user_email": user_email or str(user_id),
            "actor_id": str(user_id),
            "email": user_email or str(user_id),
            "is_authenticated": False
        }

    return None


resend.api_key = os.getenv("RESEND_API_KEY")


def _dispatch_otp_email(to_email, otp_code):
    subject = f"Your DTC Verification Code: {otp_code}"
    text_content = f"""Hello,

Your verification code for the Distributed Transaction Coordinator is:

    {otp_code}

This code will expire in 5 minutes.

If you did not request this verification code, please ignore this message.

— DTC Security"""

    html_content = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f5; padding: 40px 20px;">
      <div style="max-width: 460px; margin: 0 auto; background-color: #ffffff; padding: 32px; border-radius: 8px; border: 1px solid #e4e4e7; text-align: center;">
        <h2 style="margin: 0 0 16px; font-size: 22px; font-weight: 600; color: #18181b;">DTC Verification</h2>
        <p style="color: #52525b; font-size: 15px; margin: 0 0 24px; line-height: 1.6;">Use the 6-digit verification code below to verify your user identity and access live seat holds.</p>
        <div style="background-color: #f4f4f5; border-radius: 6px; padding: 16px; display: inline-block; margin-bottom: 24px;">
          <span style="font-size: 36px; font-weight: 700; color: #18181b; letter-spacing: 2px; user-select: all;">{otp_code}</span>
        </div>
        <p style="color: #71717a; font-size: 13px; margin: 0; line-height: 1.5;">This code will expire in 5 minutes.<br>If you didn't request this, you can safely ignore this email.</p>
      </div>
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
        print(f"[Email Dispatch Success] Verification code delivered to {to_email} via Resend. id={result.get('id')}")
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

    is_dev_mode = _is_dev_auth_mode()
    client = _get_redis_client()
    chances_remaining = 10

    # In production/testing: enforce IP+email block, cooldown, abandonment tracking, and daily limit
    if not is_dev_mode:
        client_ip       = _get_client_ip(request)
        daily_block_key = f"dtc:otp_daily_blocked:{email}"
        daily_key       = f"dtc:otp_daily:{email}"
        cooldown_key    = f"dtc:otp_cooldown:{email}"
        block_key       = _ip_block_key(client_ip, email)
        abandon_key     = _ip_abandon_key(client_ip, email)

        if client:
            try:
                # --- IP+email block check (wrong OTPs or abandonment abuse) ---
                if client.exists(block_key):
                    ttl = max(0, client.ttl(block_key))
                    return Response(
                        {
                            "error": "Your network has been temporarily blocked from requesting codes for this email due to repeated abuse. Try again in 24 hours.",
                            "code": "IP_EMAIL_BLOCKED",
                            "retry_after": ttl,
                        },
                        status=429,
                    )

                # --- Cooldown: prevent re-send within 30 seconds ---
                if client.exists(cooldown_key):
                    ttl = client.ttl(cooldown_key)
                    if ttl > 0:
                        return Response({
                            "success": True,
                            "message": "Verification code was recently sent.",
                            "email": email,
                            "actor_id": _generate_actor_id(email),
                            "cooldown_remaining": ttl,
                            "chances_remaining": max(0, 10 - int(client.get(daily_key) or 0)),
                        }, status=200)

                # --- Hard daily block (set after limit is crossed) ---
                if client.exists(daily_block_key):
                    return Response(
                        {"error": "Daily OTP limit reached. You can request a new code tomorrow.", "code": "DAILY_LIMIT_REACHED"},
                        status=429,
                    )

                # --- Abandonment tracking: send without verify ---
                # Each OTP send increments the abandon counter.
                # A successful verify resets it. 5+ unverified sends = 24h IP+email block.
                abandon_count = client.incr(abandon_key)
                if abandon_count == 1:
                    client.expire(abandon_key, 7200)  # 2-hour rolling window
                if abandon_count > 5:
                    client.setex(block_key, 86400, "abandon")
                    client.delete(abandon_key)
                    return Response(
                        {
                            "error": "Too many OTP requests without completing verification. Your network is blocked for 24 hours.",
                            "code": "IP_EMAIL_BLOCKED",
                            "retry_after": 86400,
                        },
                        status=429,
                    )

                # --- Increment today's counter (expires at midnight via TTL) ---
                daily_count = client.incr(daily_key)
                if daily_count == 1:
                    import datetime
                    now_dt = datetime.datetime.now()
                    midnight = (now_dt + datetime.timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    client.expire(daily_key, int((midnight - now_dt).total_seconds()))

                if daily_count > 10:
                    client.setex(daily_block_key, 86400, "blocked")
                    client.delete(daily_key)
                    return Response(
                        {"error": "Daily OTP limit reached. You can request a new code tomorrow.", "code": "DAILY_LIMIT_REACHED"},
                        status=429,
                    )

                chances_remaining = max(0, 10 - int(daily_count))
            except Exception:
                pass

    otp_code = f"{secrets.randbelow(900000) + 100000}"
    cache_key = f"dtc:otp:{email}"

    if client:
        try:
            client.setex(cache_key, 300, otp_code)
            if is_dev_mode:
                for leftover_k in [
                    f"dtc:otp_daily_blocked:{email}",
                    f"dtc:otp_daily:{email}",
                    f"dtc:otp_cooldown:{email}",
                ]:
                    client.delete(leftover_k)
        except Exception:
            pass

    # Redundant memory backup in case Redis is rebooting
    try:
        cache.set(cache_key, otp_code, timeout=300)
    except Exception:
        pass

    # Only dispatch real email if NOT running in development or test mode
    is_testing = bool(getattr(settings, "IS_TESTING", False)) or ('test' in sys.argv)
    if not is_dev_mode and not is_testing:
        threading.Thread(target=_dispatch_otp_email, args=(email, otp_code), daemon=True).start()
    else:
        print(f"[Local Auth] Simulated OTP generated for {email}: {otp_code} (Infinite dev resends enabled)")

    if client and not is_dev_mode:
        try:
            client.setex(f"dtc:otp_cooldown:{email}", 30, "1")
        except Exception:
            pass

    user_id = _generate_actor_id(email)

    resp_data = {
        "success": True,
        "message": f"[LOCAL DEV] Verification code: {otp_code} (Infinite resends enabled)" if is_dev_mode else f"Verification code sent to {email}",
        "email": email,
        "user_email": email,
        "user_id": user_id,
        "actor_id": user_id,
        "expires_in_seconds": 300,
        "cooldown_remaining": 0 if is_dev_mode else 30,
        "chances_remaining": "∞" if is_dev_mode else chances_remaining,
    }
    if is_dev_mode:
        resp_data["otp"] = otp_code
        resp_data["is_local"] = True

    return Response(resp_data, status=200)


@api_view(["POST"])
def verify_otp(request):
    """
    Phase 2: Compares submitted OTP against Redis value, deletes Redis key on match (single-use),
    and issues a JWT token. Wrong OTPs are tracked per IP+email — 5 failures triggers a 24h block.
    """
    email_raw = request.data.get("email") or request.data.get("email_address")
    submitted_otp = str(request.data.get("otp", "")).strip()

    if not email_raw or not submitted_otp:
        return Response({"error": "email and otp are required"}, status=400)

    email = _normalize_email(email_raw)
    cache_key = f"dtc:otp:{email}"

    is_dev_mode = _is_dev_auth_mode()
    client = _get_redis_client()

    # In production: check IP+email block before allowing verify attempt
    if not is_dev_mode and client:
        client_ip = _get_client_ip(request)
        block_key = _ip_block_key(client_ip, email)
        try:
            if client.exists(block_key):
                ttl = max(0, client.ttl(block_key))
                return Response(
                    {
                        "error": "Your network has been temporarily blocked from accessing this email. Try again in 24 hours.",
                        "code": "IP_EMAIL_BLOCKED",
                        "retry_after": ttl,
                    },
                    status=429,
                )
        except Exception:
            pass

    stored_otp = None
    if client:
        try:
            stored_otp = client.get(cache_key)
        except Exception:
            pass

    if not stored_otp:
        try:
            stored_otp = cache.get(cache_key)
        except Exception:
            pass

    if not stored_otp or str(stored_otp).strip() != submitted_otp:
        # Dev mode: unlimited attempts, no tracking
        if is_dev_mode:
            return Response(
                {
                    "error": "Invalid or expired verification code. (Infinite attempts allowed in local dev)",
                    "code": "INVALID_OTP",
                    "attempts_remaining": "∞",
                },
                status=400,
            )

        # Production: track wrong attempts per IP+email — 5 strikes = 24h block
        if client:
            try:
                client_ip = _get_client_ip(request)
                fail_key = _ip_fail_key(client_ip, email)
                fail_count = client.incr(fail_key)
                if fail_count == 1:
                    client.expire(fail_key, 86400)  # 24-hour window
                if fail_count >= 5:
                    block_key = _ip_block_key(client_ip, email)
                    client.setex(block_key, 86400, "otp_fail")
                    client.delete(fail_key)
                    client.delete(cache_key)
                    return Response(
                        {
                            "error": "Your network has been blocked from accessing this email for 24 hours due to too many failed attempts.",
                            "code": "IP_EMAIL_BLOCKED",
                            "retry_after": 86400,
                        },
                        status=429,
                    )
                strikes_left = max(0, 5 - int(fail_count))
                return Response(
                    {
                        "error": f"Invalid or expired verification code. {strikes_left} attempt(s) remaining before your network is blocked.",
                        "code": "INVALID_OTP",
                        "attempts_remaining": strikes_left,
                    },
                    status=400,
                )
            except Exception:
                pass

        return Response({"error": "Invalid or expired verification code.", "code": "INVALID_OTP"}, status=400)

    # ── OTP matched: success path ─────────────────────────────────────────────
    if client:
        try:
            client_ip = _get_client_ip(request)
            # Clear OTP, fail counter, and abandon counter — clean slate on success
            client.delete(cache_key)
            client.delete(_ip_fail_key(client_ip, email))
            client.delete(_ip_abandon_key(client_ip, email))
        except Exception:
            pass
    try:
        cache.delete(cache_key)
    except Exception:
        pass

    user_id = _generate_actor_id(email)
    jwt_expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))
    jwt_token = _create_jwt(user_id=user_id, email=email, expires_in=jwt_expires_in)

    if client and not is_dev_mode:
        try:
            client.setex(f"dtc:session:{email}", 30, "active")
        except Exception:
            pass

    return Response({
        "success": True,
        "message": "Authenticated successfully!",
        "token": jwt_token,
        "user_id": user_id,
        "user_email": email,
        "actor_id": user_id,
        "email": email,
        "expires_in": jwt_expires_in
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
    if email and not _is_dev_auth_mode():
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
