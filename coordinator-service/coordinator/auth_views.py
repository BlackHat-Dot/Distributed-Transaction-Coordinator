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
    now = time.time()
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
    if not email_str:
        return ""
    return str(email_str).strip().lower()


def _generate_actor_id(email):
    norm = _normalize_email(email)
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"usr_{sha}"


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

    # In production/testing: enforce account lockout, cooldown, and daily limit only
    if not is_dev_mode:
        lock_key        = f"dtc:otp_locked:{email}"
        daily_block_key = f"dtc:otp_daily_blocked:{email}"
        daily_key       = f"dtc:otp_daily:{email}"
        cooldown_key    = f"dtc:otp_cooldown:{email}"

        # --- Account lockout check (from failed OTP verifications) ---
        is_locked = False
        lock_ttl = 900
        if client:
            try:
                if client.exists(lock_key):
                    is_locked = True
                    lock_ttl = max(0, client.ttl(lock_key))
            except Exception:
                pass
        if not is_locked:
            try:
                if cache.get(lock_key):
                    is_locked = True
            except Exception:
                pass

        if is_locked:
            return Response(
                {
                    "error": "Account is locked due to too many failed OTP attempts. Try again later.",
                    "code": "ACCOUNT_LOCKED",
                    "lockout_remaining": lock_ttl,
                },
                status=429,
            )

        if client:
            try:
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

                # --- Increment today's counter (expires at midnight via TTL) ---
                daily_count = client.incr(daily_key)
                if daily_count == 1:
                    # Calculate seconds until midnight (start of next day)
                    import datetime
                    now_dt = datetime.datetime.now()
                    midnight = (now_dt + datetime.timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    seconds_until_midnight = int((midnight - now_dt).total_seconds())
                    client.expire(daily_key, seconds_until_midnight)

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
            # In local dev mode, clear any residual limit/fail/lock keys so Redis limits stay numb
            if is_dev_mode:
                for leftover_k in [
                    f"dtc:otp_locked:{email}",
                    f"dtc:otp_fail_count:{email}",
                    f"dtc:otp_cooldown:{email}",
                    f"dtc:otp_daily_blocked:{email}",
                    f"dtc:otp_daily:{email}",
                ]:
                    client.delete(leftover_k)
        except Exception:
            pass

    # Redundant memory backup in case Redis is rebooting
    try:
        cache.set(cache_key, otp_code, timeout=300)
        if is_dev_mode:
            cache.delete(f"dtc:otp_locked:{email}")
            cache.delete(f"dtc:otp_fail_count:{email}")
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
    and issues a 30-minute JWT token with user_id.
    """
    email_raw = request.data.get("email") or request.data.get("email_address")
    submitted_otp = str(request.data.get("otp", "")).strip()

    if not email_raw or not submitted_otp:
        return Response({"error": "email and otp are required"}, status=400)

    email = _normalize_email(email_raw)
    cache_key = f"dtc:otp:{email}"
    lock_key = f"dtc:otp_locked:{email}"
    fail_key = f"dtc:otp_fail_count:{email}"

    is_dev_mode = _is_dev_auth_mode()
    client = _get_redis_client()

    # Check if currently locked out (only enforced in production / test mode)
    if not is_dev_mode:
        is_locked = False
        lock_ttl = 900
        if client:
            try:
                if client.exists(lock_key):
                    is_locked = True
                    lock_ttl = max(0, client.ttl(lock_key))
            except Exception:
                pass
        if not is_locked:
            try:
                if cache.get(lock_key):
                    is_locked = True
            except Exception:
                pass

        if is_locked:
            return Response(
                {
                    "error": "Account is temporarily locked due to 5 failed attempts. Please try again in 15 minutes.",
                    "code": "ACCOUNT_LOCKED",
                    "lockout_remaining": lock_ttl,
                },
                status=429,
            )

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
        # In local dev mode: failure tracking in Redis is completely NUMB
        if is_dev_mode:
            return Response(
                {
                    "error": "Invalid or expired verification code. (Infinite attempts allowed in local dev)",
                    "code": "INVALID_OTP",
                    "attempts_remaining": "∞",
                },
                status=400,
            )

        # In production / test mode: track failure count and lock out after 5 failures
        fail_count = None
        if client:
            try:
                fail_count = client.incr(fail_key)
                if fail_count == 1:
                    client.expire(fail_key, 900)
            except Exception:
                fail_count = None

        if fail_count is None:
            try:
                fail_count = (cache.get(fail_key) or 0) + 1
                cache.set(fail_key, fail_count, timeout=900)
            except Exception:
                fail_count = 1

        if fail_count >= 5:
            # Lock out for 15 minutes (900 seconds) in production
            if client:
                try:
                    client.setex(lock_key, 900, "locked")
                    client.delete(cache_key)
                    client.delete(fail_key)
                except Exception:
                    pass
            try:
                cache.set(lock_key, "locked", timeout=900)
                cache.delete(cache_key)
                cache.delete(fail_key)
            except Exception:
                pass

            return Response(
                {
                    "error": "Too many failed attempts. Account is locked for 15 minutes.",
                    "code": "ACCOUNT_LOCKED",
                    "lockout_remaining": 900,
                },
                status=429,
            )

        attempts_remaining = max(0, 5 - fail_count)
        return Response(
            {
                "error": f"Invalid or expired verification code. {attempts_remaining} attempt(s) remaining.",
                "code": "INVALID_OTP",
                "attempts_remaining": attempts_remaining,
            },
            status=400,
        )

    # Delete key on success (single-use + reset failures)
    if client:
        try:
            client.delete(cache_key)
            client.delete(fail_key)
        except Exception:
            pass
    try:
        cache.delete(cache_key)
        cache.delete(fail_key)
    except Exception:
        pass

    user_id = _generate_actor_id(email)
    jwt_expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))
    jwt_token = _create_jwt(user_id=user_id, email=email, expires_in=jwt_expires_in)

    # Register active session in Redis (only in production)
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
