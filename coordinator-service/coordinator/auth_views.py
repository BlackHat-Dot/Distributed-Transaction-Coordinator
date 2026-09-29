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
from datetime import datetime, timedelta
import redis
import resend
from django.conf import settings
from django.core.cache import cache
from rest_framework.decorators import api_view
from rest_framework.response import Response


_redis_last_check = 0
_redis_client_instance = None

is_testing = bool(getattr(settings, "IS_TESTING", False))

def _get_redis_client():
    global _redis_last_check, _redis_client_instance

    # Use time.monotonic() instead of time.time() for interval/cooldown calculations.
    # Monotonic is immune to system wall-clock shifts (e.g. NTP syncs, leap seconds,
    # or manual clock adjustments), guaranteeing the timer never jumps backward.
    now = time.monotonic()

    if _redis_client_instance:
        return _redis_client_instance

    if now - _redis_last_check < 5.0:
        return None

    _redis_last_check = now
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")

    try:
        # socket_connect_timeout = 0.5(500ms); max time allowed to establish the TCP network connection.
        # socket_timeout = 0.5(500ms); max time allowed to wait for redis to reply to a command
        # decode_responses=True tells the Redis client to automatically decode raw bytes into Python str (strings) using UTF-8.
        client = redis.Redis.from_url(
            broker_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5
        )

        # If connection is successful then redis will reply 'PONG'
        # If redis OFF then client.ping() does not return False or None instead, it raises an exception
        client.ping()

        _redis_client_instance = client

        return client

    except Exception:
        _redis_client_instance = None

        return None


def _is_dev_auth_mode():
    if is_testing:
        return False

    return bool(getattr(settings, "DEBUG", False))

# remove spaces and lower the email
def _normalize_email(email_str):
    # email_str can be of other types , so guard it by converting it to string before performing string operations.
    return str(email_str).strip().lower()



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


validate_custom_email = validate_email


def _generate_user_id(email):
    norm = _normalize_email(email)
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"usr_{sha}"


def _get_client_ip(request):
    """
    Safely resolves client IP address (SEC-05).
    X-Forwarded-For is only trusted if the immediate peer (REMOTE_ADDR)
    is in the configured TRUSTED_PROXIES set. Otherwise REMOTE_ADDR is used.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "unknown").strip()

    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", None)
    if trusted_proxies is None:
        raw = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1")
        trusted_proxies = {ip.strip() for ip in raw.split(",") if ip.strip()}

    if remote_addr not in trusted_proxies:
        return remote_addr

    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if not xff:
        return remote_addr

    # Parse X-Forwarded-For IPs in order
    ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    if not ips:
        return remote_addr

    # Rightmost untrusted IP is the genuine client
    for ip in reversed(ips):
        if ip not in trusted_proxies:
            return ip

    return ips[0]


def _email_hash(email):
    """Short hash of email used in Redis keys (avoids storing raw emails in key names)."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]


def _create_jwt(user_id, user_email, session_id=None, expires_in=None):
    """A JSON Web Token (JWT) is created"""

    header = {"alg": "HS256", "typ": "JWT"}

    now = int(time.time())

    if expires_in is None:
        expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))

    payload = {
        "user_id": user_id,
        "user_email": user_email,
        "iat": now,
        "exp": now + expires_in
    }

    if session_id:
        payload["sid"] = session_id


    def b64url(data_bytes):
        # eg: data_bytes = b'{"alg":"HS256","typ":"JWT"}'
        # 1. base64: Translates any messy sequence of bytes into a clean, universally safe alphabet of 64 printable ASCII characters.
        # 2. urlsafe_b64encode: Replaces '+' and '/' with '-' and '_' for URL safety
        # 3. rstrip(b"="): Strips trailing '=' padding required by JWT standard
        # 4. base64.urlsafe_b64encode(data_bytes) => gives still bytes (b'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9') -> decode("utf-8"): Converts resulting bytes to a standard Python string ('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9')
        return base64.urlsafe_b64encode(data_bytes).rstrip(b"=").decode("utf-8")

    # json.dumps(): Converts the Python dictionary into a JSON string.
    # By default, json.dumps() inserts spaces after commas and colons: '{"alg": "HS256", "typ": "JWT"}' (31 characters)
    # By specifying separators=(",", ":"), all unnecessary whitespace is stripped: '{"alg":"HS256","typ":"JWT"}' (27 characters)
    # .encode("utd-8") : converts JSON string to bytes.

    # json_str = '{"user_id": "usr_9a8b", "email": "vijay@gmail.com"}'
    # json_bytes = json_str.encode("utf-8")
    # print(json_bytes)
    # Output: b'{"user_id": "usr_9a8b", "email": "vijay@gmail.com"}'
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


    # A JSON Web Token (JWT) is just a single string made of 3 parts separated by dots (.)
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
    elif hasattr(request, "GET") and request.GET.get("token"):
        token = request.GET.get("token").strip()

    if token:
        payload, err = verify_jwt(token)
        # error is consumed
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

    If you did not request this verification code, please ignore this message
    — no further action is needed.

    — DTC Security
    Distributed Transaction Coordinator"""

    html_content = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DTC Verification Code</title>
    </head>
    <body style="margin:0; padding:0; background-color:#f4f4f5; -webkit-text-size-adjust:100%;">
      <!-- Preheader (hidden, shows next to subject line in inbox) -->
      <div style="display:none; max-height:0; overflow:hidden; opacity:0;">
        Your DTC verification code is {otp_code}. It expires in 5 minutes.
      </div>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f4f5; padding:40px 20px;">
        <tr>
          <td align="center">
            <table role="presentation" width="460" cellpadding="0" cellspacing="0" border="0"
                   style="max-width:460px; width:100%; background-color:#ffffff; border-radius:10px; border:1px solid #e4e4e7; overflow:hidden;">

              <!-- Brand bar -->
              <tr>
                <td style="background-color:#111827; padding:20px 32px;">
                  <span style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; font-size:16px; font-weight:700; color:#ffffff; letter-spacing:0.5px;">
                    DTC
                  </span>
                  <span style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; font-size:13px; color:#9ca3af; margin-left:8px;">
                    Distributed Transaction Coordinator
                  </span>
                </td>
              </tr>

              <!-- Body -->
              <tr>
                <td style="padding:36px 32px 28px; text-align:center; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                  <h1 style="margin:0 0 12px; font-size:20px; font-weight:600; color:#18181b;">
                    Verify your identity
                  </h1>
                  <p style="margin:0 0 24px; font-size:14px; line-height:1.6; color:#52525b;">
                    Use the code below to verify your identity and access live seat holds.
                  </p>

                  <!-- OTP block -->
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto 20px;">
                    <tr>
                      <td style="background-color:#f4f4f5; border:1px solid #e4e4e7; border-radius:8px; padding:18px 28px;">
                        <span style="font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace; font-size:32px; font-weight:700; color:#111827; letter-spacing:8px;">
                          {otp_code}
                        </span>
                      </td>
                    </tr>
                  </table>

                  <p style="margin:0; font-size:13px; color:#a1a1aa;">
                    This code expires in <strong style="color:#52525b;">5 minutes</strong>.
                  </p>
                </td>
              </tr>

              <!-- Security notice -->
              <tr>
                <td style="padding:0 32px 32px;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                         style="background-color:#fafafa; border-radius:6px; border:1px solid #f0f0f1;">
                    <tr>
                      <td style="padding:14px 18px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; font-size:12.5px; line-height:1.6; color:#71717a; text-align:left;">
                        Didn't request this code? You can safely ignore this email — your account is still secure. Never share this code with anyone, including anyone claiming to be from DTC support.
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="padding:20px 32px 28px; border-top:1px solid #f0f0f1; text-align:center; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                  <p style="margin:0 0 4px; font-size:12px; color:#a1a1aa;">
                    This is an automated message from DTC Security.
                  </p>
                  <p style="margin:0; font-size:12px; color:#d4d4d8;">
                    Distributed Transaction Coordinator
                  </p>
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>"""


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
                            "user_email": email,
                            "user_id": _generate_user_id(email),
                            "cooldown_remaining": ttl,
                            "is_cooldown": True,
                            "chances_remaining": max(0, 10 - int(client.get(daily_key) or 0)),
                        }, status=200)

                # Hard daily block (set after limit is crossed)
                if client.exists(daily_block_key):
                    return Response(
                        {"error": "Daily OTP limit reached. You can request a new code tomorrow.",
                         "code": "DAILY_LIMIT_REACHED"},
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

                now_dt = datetime.now()
                midnight = (now_dt + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                ttl_until_midnight = max(1, int((midnight - now_dt).total_seconds()))

                # Increment today's counter (expires at midnight via TTL)
                daily_count = client.incr(daily_key) # "key created"

                if daily_count == 1:
                    client.expire(daily_key, ttl_until_midnight) # "expiry added"

                if daily_count > 10:
                    # Block expires at midnight so user can request OTP again next day morning
                    client.setex(daily_block_key, ttl_until_midnight, "blocked")
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
        # Non blocking mechanism is used : Threads
        # daemon=True: Make is as a daemon thread, If servers shutsdown , python will not freeze/hang waiting for this thread to finish,
        # non-daemon block process shutdown , daemon threads exit gracefully with the process.
        # .start(): Immediately creates and starts the OS level thread

        # target=_dispatch_otp_email, args=(email, otp_code) together form `_dispatch_otp_email(email, otp_code)`
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
        "is_cooldown": False,
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
    abandon_key = f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}"

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
            client.delete(abandon_key)
        except Exception:
            pass

    try:
        cache.delete(cache_key)
        cache.delete(fail_key)
        cache.delete(abandon_key)
    except Exception:
        pass

    session_id = uuid.uuid4().hex
    user_id = _generate_user_id(email)
    session_keys = [session_key, f"dtc:session:{user_id}"]
    if client:
        try:
            for k in session_keys:
                client.setex(k, 30, session_id)
        except Exception:
            pass
    try:
        for k in session_keys:
            cache.set(k, session_id, timeout=30)
    except Exception:
        pass

    jwt_expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))
    jwt_token = _create_jwt(user_id=user_id, user_email=email, session_id=session_id, expires_in=jwt_expires_in)

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
    user_id = user_info.get("user_id")
    if not email and not user_id:
        return Response({"error": "Missing email"}, status=400)

    token_sid = user_info.get("sid")
    client = _get_redis_client()
    session_keys = []
    if email:
        session_keys.append(f"dtc:session:{email}")
    if user_id:
        session_keys.append(f"dtc:session:{user_id}")

    if client:
        try:
            if email:
                raw_val = client.get(f"dtc:session:{email}")
                if raw_val:
                    active_sid = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else raw_val
                    if token_sid and active_sid and active_sid != token_sid:
                        return Response({
                            "error": "Session superseded by another login.",
                            "code": "SESSION_SUPERSEDED"
                        }, status=401)
            for k in session_keys:
                client.setex(k, 30, token_sid or "active")
        except Exception:
            pass

    try:
        if email:
            cache_sid = cache.get(f"dtc:session:{email}")
            if cache_sid and token_sid and cache_sid != token_sid:
                return Response({
                    "error": "Session superseded by another login.",
                    "code": "SESSION_SUPERSEDED"
                }, status=401)
        for k in session_keys:
            cache.set(k, token_sid or "active", timeout=30)
    except Exception:
        pass

    return Response({"status": "alive"}, status=200)


@api_view(["POST"])
def logout(request):
    """
    Deletes the user's active session in Redis and cache.
    """
    user_info = get_user_from_request(request)
    if user_info:
        keys_to_del = []
        if user_info.get("email"):
            keys_to_del.append(f"dtc:session:{user_info.get('email')}")
        if user_info.get("user_id"):
            keys_to_del.append(f"dtc:session:{user_info.get('user_id')}")
        client = _get_redis_client()
        for k in keys_to_del:
            if client:
                try:
                    client.delete(k)
                except Exception:
                    pass
            try:
                cache.delete(k)
            except Exception:
                pass

    return Response({"status": "logged_out"}, status=200)
