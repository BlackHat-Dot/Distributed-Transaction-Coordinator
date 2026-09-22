import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
import redis
import resend
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from rest_framework.decorators import api_view
from rest_framework.response import Response


_redis_last_check = 0
_redis_is_available = False
_redis_client_instance = None

is_testing = bool(getattr(settings, "IS_TESTING", False))

def _get_redis_client():
    global _redis_last_check, _redis_is_available, _redis_client_instance

    # Use time.monotonic() instead of time.time() for interval/cooldown calculations.
    # Monotonic is immune to system wall-clock shifts (e.g. NTP syncs, leap seconds, 
    # or manual clock adjustments), guaranteeing the timer never jumps backward.
    now = time.monotonic()

    if _redis_client_instance and _redis_is_available:
        return _redis_client_instance
    
    if now - _redis_last_check < 5.0 and not _redis_is_available:
        return None
    
    _redis_last_check = now
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")

    try:
        # socket_connect_timeout = 0.5(500ms); max time allowed to establish the TCP network connection.
        # socket_timeout = 0.5(500ms); max time allowed to wait for redis to reply to a command
        client = redis.Redis.from_url(
            broker_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5
        )

        # If connection is successful then redis will reply 'PONG'
        client.ping()
        
        _redis_is_available = True
        _redis_client_instance = client
        return client

    except Exception:           
        _redis_is_available = False
        _redis_client_instance = None
        return None


def _is_dev_auth_mode():
    if is_testing:
        return False
    
    return bool(getattr(settings, "DEBUG", False)) or os.getenv("DTC_DEV_AUTH_MODE", "").lower() in ("true", "1")

# remove spaces and lower the email
def _normalize_email(email_str):
    # email_str can be of other types , so guard it by converting it to string before performing string operations.
    return str(email_str).strip().lower()


import re

"""
1. '^' -> Start of string.
2. [a-z0-9] -> a character class matching exactly one character, which must be a lowercase letter or digit.
3. [a-z0-9._%+-] -> Middle characters: letters, numbers, or . _ % + -
4. {1, 62} -> a quantifier applied to the class right before it. It means "repeat the previous character class between 1 and 62 times." So the middle section must have at least 1 character and at most 62.
5. [a-z0-9] -> same class as the first one: exactly one alphanumeric character, no symbols. This is the last character of the local part.
6. $ -> end of string.

=> Together ^...$ means the entire string must match this pattern top to bottom, nothing before, nothing after.
"""
GMAIL_LOCAL_REGEX = re.compile(r"^[a-z0-9][a-z0-9._%+-]{1,62}[a-z0-9]$")

# A frozenset is an immutable (read-only) set. Once created, its elements cannot be changed, added, or deleted.
_PROD_DOMAINS = frozenset({"gmail.com"})
_TEST_DOMAINS = frozenset({"example.com", "test.io", "local.io"})

# Common gmail typos worth catching explicitly (fast path, no distance calc needed)
_KNOWN_GMAIL_TYPOS = frozenset({
    "gmial.com", "gmal.com", "gmai.com", "gnail.com", "gmail.co",
    "gmail.cm", "gmail.con", "gamil.com", "gmail.comm", "gmaill.com",
    "gmail.vom", "gmali.com", "gmailc.om", "gmail",
})



def _suggest_domain(domain_part):
    """
    Returns a suggested correction for `domain_part` if it looks like a
    near-miss typo of gmail.com, else None.
      1. Exact match against a known-typo list (cheap, catches 90% of cases).
    """
    if domain_part in _KNOWN_GMAIL_TYPOS:
        return "gmail.com"

    return None


def validate_email(email):
    """
    Validates that the email address is a valid @gmail.com address
    (plus test domains when IS_TESTING/DEBUG is on).

    Enforces:
      1. Only @gmail.com domain (or test domains in test/debug mode).
      2. Local-part syntax: no consecutive dots, no leading/trailing dot.
      3. Local-part length between 3 and 64 characters.

    On domain failure, attempts a typo suggestion (e.g. 'gmial.com' ->
    'gmail.com') and includes it in the error message when found.
    """

    if len(email) > 254:
        return False, "Email address exceeds maximum length of 254 characters."

    parts = email.split("@")
    if len(parts) != 2:
        return False, "Email address must contain exactly one '@' symbol."
    
    local_part, domain_part = parts

    if not local_part or len(local_part) < 3 or len(local_part) > 64:
        return False, "Email username (before '@') must be between 3 and 64 characters."

    if ".." in local_part:
        return False, "Email username cannot contain consecutive dots ('..')."

    if local_part.startswith(".") or local_part.endswith("."):
        return False, "Email username cannot begin or end with a dot."

    if not GMAIL_LOCAL_REGEX.match(local_part):
        return False, "Email username contains invalid characters."

    allowed_domains = _PROD_DOMAINS
    if getattr(settings, "IS_TESTING", False) or getattr(settings, "DEBUG", False):
        allowed_domains = _PROD_DOMAINS | _TEST_DOMAINS

    if domain_part not in allowed_domains:
        suggestion = _suggest_domain(domain_part)
        if suggestion:
            return False, f"Only @gmail.com addresses are supported. Did you mean {local_part}@{suggestion}?"
        return False, "Only @gmail.com email addresses are supported."

    return True, None


def _generate_user_id(email):
    norm = _normalize_email(email)
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"usr_{sha}"


def _get_client_ip(request):
    # Django prefixes all HTTP headers with "HTTP_" and replaces dashes with underscores.
    # So "X-Forwarded-For" becomes "HTTP_X_FORWARDED_FOR".
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    
    # If the request came through a proxy (e.g., Railway / Cloudflare):
    if xff:
        # Example: "203.0.113.19, 10.0.0.5"
        # .split(",")[0] -> grabs the first element: "203.0.113.19"
        # .strip()       -> cleans any extra whitespace
        return xff.split(",")[0].strip()
    
    # Fallback (Localhost / Direct Connection):
    # If you are running locally without a proxy, X-Forwarded-For won't exist.
    # Fall back to standard REMOTE_ADDR (e.g. "127.0.0.1"), or "0.0.0.0" if missing.
    # REMOTE_ADDR holds the IP of the immediate machine that directly connected to this server
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _email_hash(email):
    """Short hash of email used in Redis keys (avoids storing raw emails in key names)."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]


def _create_jwt(user_id, email, session_id=None, expires_in=None):
    """A JSON Web Token (JWT) is just a single string made of 3 parts separated by dots (.)"""

    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    uid = user_id
    if expires_in is None:
        expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))
    payload = {
        "user_id": uid,
        "user_email": email,
        "iat": now,
        "exp": now + expires_in
    }
    if session_id:
        payload["sid"] = session_id


    def b64url(data_bytes):
        # 1. base64: Translates any messy sequence of bytes into a clean, universally safe alphabet of 64 printable ASCII characters.
        # 2. urlsafe_b64encode: Replaces '+' and '/' with '-' and '_' for URL safety
        # 3. rstrip(b"="): Strips trailing '=' padding required by JWT standard
        # 4. decode("utf-8"): Converts resulting bytes to a standard Python string
        return base64.urlsafe_b64encode(data_bytes).rstrip(b"=").decode("utf-8")

    # json.dumps() does: Converts the Python dictionary into a JSON string.
    # By default, json.dumps() inserts spaces after commas and colons: '{"alg": "HS256", "typ": "JWT"}' (31 characters)
    # By specifying separators=(",", ":"), all unnecessary whitespace is stripped: '{"alg":"HS256","typ":"JWT"}' (27 characters)
    # Serialize header/payload dict into compact JSON bytes and encode as unpadded URL-safe Base64
    h_str = b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_str = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    # Join the Base64-encoded header and payload strings with a dot, then convert to bytes for signing
    signing_input = f"{h_str}.{p_str}".encode("utf-8")

    # Convert the server's private secret key to bytes
    secret = settings.SECRET_KEY.encode("utf-8")

    # hmac.new(secret, signing_input, hashlib.sha256) -> Creates a HMAC object
    # .digest() -> Actually extracts 32 raw bytes from the object
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    sig_str = b64url(sig) # Convert signature bytes to a URL-safe Base64 string


    # JWT token
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
        # Base64 strings must have a length divisible by 4; restore any stripped '=' padding
        rem = len(s) % 4
        if rem > 0:
            s += "=" * (4 - rem)

        # .encode("utf-8") turns the Base64 scrambled text into bytes.
        return base64.urlsafe_b64decode(s.encode("utf-8")) # Base64 String (b'eyJ1c2Vy...') -> Raw bytes (b'{"user_id":"123"}')

    try:
        actual_sig = b64url_decode(sig_str)

        # avoided using '!=' because it exits on the first mismatched byte,
        # leaking timing information. hmac.compare_digest compares in constant time.
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None, "Invalid token signature"

        payload_json = b64url_decode(p_str).decode("utf-8") # .decode("utf-8") -> JSON string '{"user_id":"123"}'
        payload = json.loads(payload_json) # json.loads(): '{"user_id":"123"}' -> {"user_id": "123"}

        if "exp" in payload and payload["exp"] < int(time.time()):
            return None, "Token has expired"

        return payload, None
    except Exception as e:
        return None, str(e)


def get_user_from_request(request):
    """
    Extracts authenticated user identity from Authorization header or request payload.
    """
    auth_header = request.headers.get("Authorization", "") # fallback "" given for grazeful stop
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1].strip() # index 1 holds the JWT Token

    if token:
        payload, err = verify_jwt(token)
        if payload:
            uid = payload.get("user_id")
            uemail = payload.get("user_email")
            sid = payload.get("sid")
            return {
                "user_id": uid,
                "user_email": uemail,
                "email": uemail,
                "sid": sid,
                "is_authenticated": True
            }

    # Fallback to direct X-User-ID / X-User-Email headers (used for service-to-service / test calls)
    user_id = request.headers.get("X-User-ID")
    user_email = request.headers.get("X-User-Email")

    if user_id:
        return {
            "user_id": str(user_id),
            "user_email": user_email or str(user_id),
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
    generates random 6-digit OTP, stores in Redis (5-min TTL),
    and sends via Gmail SMTP with console fallback.
    """

    email_raw = request.data.get("email")

    # If email is not sent reject the request
    if not email_raw:
        return Response({"error": "email is required"}, status=400)

    email = _normalize_email(email_raw)
    is_valid, err_msg = validate_email(email)
    if not is_valid:
        return Response({"error": err_msg, "code": "INVALID_EMAIL"}, status=400)

    is_dev_mode = _is_dev_auth_mode()
    client = _get_redis_client()
    chances_remaining = 10

    # Single-Device Guard: Reject new login attempts if an active session already exists for this email
    session_key = f"dtc:session:{email}"
    session_active = False
    if client:
        try:
            if client.exists(session_key):
                session_active = True
        except Exception:
            pass
    if not session_active:
        try:
            if cache.get(session_key):
                session_active = True
        except Exception:
            pass

    if session_active:
        return Response(
            {
                "error": "Another device is currently logged in with this email.",
                "code": "CONCURRENT_LOGIN_BLOCKED",
                "message": "Another device is currently logged in with this email. Please sign out from that device first or wait for the session to expire.",
            },
            status=409,
        )

    # In production/testing: enforce IP+email block, cooldown, abandonment tracking, and daily limit
    if not is_dev_mode:
        client_ip       = _get_client_ip(request)
        daily_block_key = f"dtc:otp_daily_blocked:{email}" # counts total otp sent
        daily_key       = f"dtc:otp_daily:{email}"
        cooldown_key    = f"dtc:otp_cooldown:{email}"
        block_key       = f"dtc:ip_block:{client_ip}:{_email_hash(email)}"
        abandon_key     = f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}"

        # IP+email block check (wrong OTPs or abandonment abuse)
        is_blocked = False
        ttl = 86400
        if client:
            try:
                if client.exists(block_key):
                    is_blocked = True
                    ttl = max(0, client.ttl(block_key))
            except Exception:
                pass
        if not is_blocked:
            try:
                if cache.get(block_key):
                    is_blocked = True
            except Exception:
                pass

        if is_blocked:
            return Response(
                {
                    "error": "Your network has been temporarily blocked from requesting codes for this email due to repeated abuse. Try again in 24 hours.",
                    "code": "IP_EMAIL_BLOCKED",
                    "retry_after": ttl,
                },
                status=429,
            )

        if client:
            try:

                # Cooldown: prevent re-send within 30 seconds
                if client.exists(cooldown_key):
                    ttl = client.ttl(cooldown_key)
                    if ttl > 0:
                        return Response({
                            "success": True,
                            "message": "Verification code was recently sent.",
                            "email": email,
                            "user_id": _generate_user_id(email),
                            "cooldown_remaining": ttl,
                            "chances_remaining": max(0, 10 - int(client.get(daily_key) or 0)),
                        }, status=200)

                # Hard daily block (set after limit is crossed)
                if client.exists(daily_block_key):
                    return Response(
                        {"error": "Daily OTP limit reached. You can request a new code tomorrow.", "code": "DAILY_LIMIT_REACHED"},
                        status=429,
                    )

                # Abandonment tracking: send without verify
                # Each OTP send increments the abandon counter.
                # A successful verify resets it. 5+ unverified sends = 24h IP+email block.
                abandon_count = client.incr(abandon_key) # atomic

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

                # Increment today's counter (expires at midnight via TTL)
                daily_count = client.incr(daily_key) # "key created"

                if daily_count == 1:
                    from datetime import datetime

                    now_dt = datetime.now() # 2026-09-15 17:32:16

                    # Adds 1 day (24 hours) to move to tomorrow (eg., 2026-09-16 17:32:16)
                    # .replace(...) wipes out the hours, minutes, and seconds, setting them to zero (eg., 2026-09-16 00:00:00 "Midnight")
                    midnight = (now_dt + datetime.timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )

                    # .total_seconds() : convert the calculated interval time to seconds
                    client.expire(daily_key, int((midnight - now_dt).total_seconds())) # "expiry added"

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

    # sometimes randbelow produce less than 6 digits, so adding those kind of value with '100000' will make it 6 digit
    # eg., 45 + 100000 = 100045
    # randbelow (1 - 899999) not 900000 itself.
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    cache_key = f"dtc:otp:{email}"

    if client:
        try:
            client.setex(cache_key, 300, otp_code)
            if is_dev_mode:
                for leftover_k in [
                    daily_block_key,
                    daily_key ,
                    cooldown_key,
                ]:
                    client.delete(leftover_k)
        except Exception:
            pass

    # Redundant memory backup in case Redis is rebooting
    try:
        # stored inside Django Process RAM
        cache.set(cache_key, otp_code, timeout=300)
    except Exception:
        pass

    # Only dispatch real email if NOT running in development or test mode

    if not is_dev_mode and not is_testing:
        threading.Thread(target=_dispatch_otp_email, args=(email, otp_code), daemon=True).start()
    else:
        print(f"[Local Auth] Simulated OTP generated for {email}: {otp_code} (Infinite dev resends enabled)")

    if client and not is_dev_mode:
        try:
            client.setex(cooldown_key, 30, "1")
        except Exception:
            pass

    user_id = _generate_user_id(email)

    resp_data = {
        "success": True,
        "message": f"[LOCAL DEV] Verification code: {otp_code} (Infinite resends enabled)" if is_dev_mode else f"Verification code sent to {email}",
        "email": email,
        "user_email": email,
        "user_id": user_id,
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
    email_raw = request.data.get("email")
    submitted_otp = str(request.data.get("otp", "")).strip()

    if not email_raw or not submitted_otp:
        return Response({"error": "email and otp are required"}, status=400)

    email = _normalize_email(email_raw)
    cache_key = f"dtc:otp:{email}"

    is_dev_mode = _is_dev_auth_mode()
    client = _get_redis_client()

    # Single-Device Guard: Reject verification if an active session already exists for this email
    session_key = f"dtc:session:{email}"
    session_active = False
    if client:
        try:
            if client.exists(session_key):
                session_active = True
        except Exception:
            pass
    if not session_active:
        try:
            if cache.get(session_key):
                session_active = True
        except Exception:
            pass

    if session_active:
        return Response(
            {
                "error": "Another device is currently logged in with this email.",
                "code": "CONCURRENT_LOGIN_BLOCKED",
                "message": "Another device is currently logged in with this email. Please sign out from that device first or wait for the session to expire.",
            },
            status=409,
        )

    client_ip = _get_client_ip(request)
    block_key = f"dtc:ip_block:{client_ip}:{_email_hash(email)}"
    fail_key = f"dtc:ip_fail:{client_ip}:{_email_hash(email)}"

    # In production: check IP+email block before allowing verify attempt
    if not is_dev_mode:
        is_blocked = False
        ttl = 86400
        if client:
            try:
                if client.exists(block_key):
                    is_blocked = True
                    ttl = max(0, client.ttl(block_key))
            except Exception:
                pass
        if not is_blocked:
            try:
                if cache.get(block_key):
                    is_blocked = True
            except Exception:
                pass

        if is_blocked:
            return Response(
                {
                    "error": "Your network has been temporarily blocked from accessing this email. Try again in 24 hours.",
                    "code": "IP_EMAIL_BLOCKED",
                    "retry_after": ttl,
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
        fail_count = None
        if client:
            try:
                fail_count = client.incr(fail_key)
                if fail_count == 1:
                    client.expire(fail_key, 86400)  # 24-hour window
            except Exception:
                fail_count = None

        if fail_count is None:
            try:
                fail_count = (cache.get(fail_key) or 0) + 1
                cache.set(fail_key, fail_count, timeout=86400)
            except Exception:
                fail_count = 1

        if fail_count >= 5:
            if client:
                try:
                    client.setex(block_key, 86400, "otp_fail")
                    client.delete(fail_key)
                    client.delete(cache_key)
                except Exception:
                    pass
            try:
                cache.set(block_key, "otp_fail", timeout=86400)
                cache.delete(fail_key)
                cache.delete(cache_key)
            except Exception:
                pass

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

    # OTP matched: success path
    if client:
        try:
            # Clear OTP, fail counter, and abandon counter; clean slate on success
            client.delete(cache_key)
            client.delete(fail_key)
            client.delete(f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}")
        except Exception:
            pass
    try:
        cache.delete(cache_key)
        cache.delete(fail_key)
        cache.delete(f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}")
    except Exception:
        pass

    session_id = uuid.uuid4().hex
    if client:
        try:
            client.setex(session_key, 30, session_id)
        except Exception:
            pass
    try:
        cache.set(session_key, session_id, timeout=30)
    except Exception:
        pass

    user_id = _generate_user_id(email)
    jwt_expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))
    jwt_token = _create_jwt(user_id=user_id, email=email, session_id=session_id, expires_in=jwt_expires_in)

    return Response({
        "success": True,
        "message": "Authenticated successfully!",
        "token": jwt_token,
        "user_id": user_id,
        "user_email": email,
        "email": email,
        "expires_in": jwt_expires_in
    }, status=200)



@api_view(["GET"])
def get_me(request):
    """
    Phase 3: Returns current authenticated user identity from JWT footprint.
    """
    user_info = get_user_from_request(request)
    if not user_info:
        # Returned None
        return Response({"authenticated": False, "user": None}, status=401)

    return Response({
        "authenticated": True,
        "user": user_info
    }, status=200)


@api_view(["POST"])
def heartbeat(request):
    """
    Extends the TTL of the user's active session in Redis and cache.

    WHY SESSION_SUPERSEDED EXISTS (The "Sleeping Laptop / Zombie Token" Scenario):
    Although send_otp and verify_otp block concurrent logins when an active session
    key exists (CONCURRENT_LOGIN_BLOCKED), two valid JWTs can still exist concurrently:
      1. Device A logs in, gets a JWT valid for 30 minutes, and sets a 30s session TTL.
      2. Device A sleeps (closes laptop lid / loses Wi-Fi). Its heartbeats stop.
      3. After 30 seconds, dtc:session:{email} in Redis expires and vanishes.
         (Note: Device A's JWT is still mathematically valid for ~29 more minutes).
      4. Device B now attempts login. Because the Redis key has expired, Device B passes
         the single-device guard at send_otp/verify_otp, gets a new JWT (sid_B), and sets
         dtc:session:{email} = sid_B.
      5. Device A wakes up / reconnects to Wi-Fi. Its browser immediately fires heartbeat
         with its still-valid JWT (sid_A).
      6. Without this check, Device A would blindly overwrite dtc:session:{email} = sid_A,
         stealing the session back or causing the two devices to fight.
      7. With this check, Device A detects active_sid (sid_B) != token_sid (sid_A), gets
         401 SESSION_SUPERSEDED, and is logged out, protecting Device B's active session.
    """
    user_info = get_user_from_request(request)
    if not user_info or not user_info.get("is_authenticated"):
        return Response({"error": "Unauthorized"}, status=401)
    
    email = user_info.get("email")
    if not email:
        return Response({"error": "Missing email"}, status=400)

    token_sid = user_info.get("sid")
    session_key = f"dtc:session:{email}"
    client = _get_redis_client()

    if client:
        try:
            # Fetch from Redis the session ID of whoever is supposed to be the active device
            raw_val = client.get(session_key)
            if raw_val:
                active_sid = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else raw_val
                
                if token_sid and active_sid and active_sid != token_sid:
                    return Response({
                        "error": "Session superseded by another login.",
                        "code": "SESSION_SUPERSEDED"
                    }, status=401)
            client.setex(session_key, 30, token_sid or "active")
        except Exception:
            pass

    try:
        cache_sid = cache.get(session_key)
        if cache_sid and token_sid and cache_sid != token_sid:
            return Response({
                "error": "Session superseded by another login.",
                "code": "SESSION_SUPERSEDED"
            }, status=401)
        cache.set(session_key, token_sid or cache_sid or "active", timeout=30)
    except Exception:
        pass
                
    return Response({"status": "alive"}, status=200)


@api_view(["POST"])
def logout(request):
    """
    Deletes the user's active session in Redis and cache.
    """
    user_info = get_user_from_request(request)
    if user_info and user_info.get("email"):
        email = user_info.get("email")
        session_key = f"dtc:session:{email}"
        client = _get_redis_client()
        if client:
            try:
                client.delete(session_key)
            except Exception:
                pass
        try:
            cache.delete(session_key)
        except Exception:
            pass
                
    return Response({"status": "logged_out"}, status=200)
