import jwt
import hashlib
import hmac
import logging
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
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.exceptions import APIException, AuthenticationFailed
from django.utils import timezone

logger = logging.getLogger(__name__)

_redis_last_check = 0.0
_redis_last_ping = 0.0
_redis_client_instance = None
_redis_lock = threading.Lock()

is_testing = bool(getattr(settings, "IS_TESTING", False))


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "STAFF_SERVICE_UNAVAILABLE"


# verified
def _get_redis_client():
    """Return a healthy cached Redis client with thread-safety and circuit-breaker self-healing."""
    global _redis_last_check, _redis_last_ping, _redis_client_instance

    now = time.monotonic()

    # Fast path: healthy client instance with periodic health verification (every 2.0s)
    if _redis_client_instance is not None:
        if now - _redis_last_ping < 2.0:
            return _redis_client_instance
        try:
            _redis_client_instance.ping()
            _redis_last_ping = now
            return _redis_client_instance
        except Exception as exc:
            logger.warning("Cached Redis client failed health ping (%s); invalidating instance.", exc)
            with _redis_lock:
                if _redis_client_instance is not None:
                    try:
                        _redis_client_instance.close()
                    except Exception:
                        pass
                    _redis_client_instance = None
                    _redis_last_check = now
            return None

    # Circuit breaker cooldown: avoid hammering an unreachable Redis server
    if now - _redis_last_check < 5.0:
        return None

    # Double-checked locking to prevent connection stampedes across worker threads
    with _redis_lock:
        if _redis_client_instance is not None:
            return _redis_client_instance

        now = time.monotonic()
        if now - _redis_last_check < 5.0:
            return None

        _redis_last_check = now
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
        testing_mode = bool(getattr(settings, "IS_TESTING", False))
        timeout = getattr(settings, "REDIS_SOCKET_TIMEOUT", 0.02 if testing_mode else 0.5)

        client = None
        try:
            client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
            )
            client.ping()
            _redis_client_instance = client
            _redis_last_ping = now
            return client
        except Exception:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            _redis_client_instance = None
            return None

import difflib
# verified
def _is_dev_auth_mode():
    """Return True only if in local development mode (DEBUG=True and not in test suite)."""
    if getattr(settings, "IS_TESTING", False):
        return False
    return bool(getattr(settings, "DEBUG", False))

# verified
def _normalize_email(email_str):
    """Safely strip, sanitize zero-width unicode characters, and lowercase email string."""
    if not email_str or not isinstance(email_str, str):
        return ""
    # Strip invisible zero-width unicode characters (\u200b, \u200c, \u200d, \ufeff)
    cleaned = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", email_str)
    return cleaned.strip().lower()


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

_PROD_DOMAINS = frozenset({"gmail.com"})
_TEST_DOMAINS = frozenset({"example.com", "test.io", "local.io"})

# Common gmail typos worth catching explicitly (fast O(1) path)
_KNOWN_GMAIL_TYPOS = frozenset({
    "gmial.com", "gmal.com", "gmai.com", "gnail.com", "gmail.co",
    "gmail.cm", "gmail.con", "gamil.com", "gmail.comm", "gmaill.com",
    "gmail.vom", "gmali.com", "gmailc.om", "gmail",
})

# Real distinct external domains that must NEVER be suggested as gmail typos
_NON_TYPO_REAL_DOMAINS = frozenset({
    "gail.com", "mail.com", "email.com", "ymail.com", "yahoo.com",
    "yaho.com", "outlook.com", "hotmail.com", "icloud.com",
    "proton.me", "protonmail.com", "aol.com", "zoho.com", "g.com",
})

# verified
def _suggest_domain(domain_part):
    """
    Returns a suggested correction for `domain_part` if it looks like a
    near-miss typo of gmail.com, else None.
    
    1. Rejects distinct legitimate domains and malformed TLDs.
    2. O(1) lookup against known Gmail typo catalog.
    3. Dynamic SequenceMatcher ratio (>= 0.85) for single-letter keyboard slips.
    """
    if not domain_part or not isinstance(domain_part, str):
        return None

    d = domain_part.strip().lower()

    # Guard: legitimate third-party services and malformed non-TLDs must never suggest gmail.com
    if d in _NON_TYPO_REAL_DOMAINS:
        return None
    if "." not in d and d != "gmail":
        return None

    # Fast path: known high-frequency typos
    if d in _KNOWN_GMAIL_TYPOS:
        return "gmail.com"

    # Dynamic path: smart similarity check for keyboard slips (ratio >= 0.85)
    matcher = difflib.SequenceMatcher(None, d, "gmail.com")
    if matcher.ratio() >= 0.85:
        return "gmail.com"

    return None

# verified
def validate_email(email):
    """
    Validates that the email address is a valid @gmail.com address
    (plus test domains when IS_TESTING/DEBUG is on).

    Enforces:
      1. Type safety and non-empty string check.
      2. Maximum length of 254 characters (RFC 5321).
      3. Exactly one '@' delimiter.
      4. Only @gmail.com domain (or test domains in test/debug mode).
      5. Local-part syntax: no consecutive dots, no leading/trailing dot.
      6. Local-part length between 3 and 64 characters.

    On domain failure, attempts a typo suggestion (e.g. 'gmial.com' ->
    'gmail.com') and includes it in the error message when found.
    """
    if not email or not isinstance(email, str):
        return False, "Email address is required."

    email = email.strip().lower()

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
    """Generate deterministic user ID prefixed with usr_ from normalized email SHA256."""
    norm = _normalize_email(email)
    if not norm:
        return ""
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"usr_{sha}"


def _get_client_ip(request):
    """
    Safely resolves client IP address (SEC-05).
    X-Forwarded-For is only trusted if the immediate peer (REMOTE_ADDR)
    is in the configured TRUSTED_PROXIES set. Otherwise REMOTE_ADDR is used.
    """
    if not request or not hasattr(request, "META"):
        return "unknown"

    # Safely extract REMOTE_ADDR without crashing if value is None
    raw_remote = request.META.get("REMOTE_ADDR")
    remote_addr = (raw_remote.strip() if isinstance(raw_remote, str) else "") or "unknown"

    # Normalize whitelist of trusted proxy IPs from settings or environment
    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", None)
    if trusted_proxies is None:
        raw = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1")
        trusted_proxies = {ip.strip() for ip in raw.split(",") if ip.strip()}
    elif isinstance(trusted_proxies, (list, tuple, set)):
        trusted_proxies = {str(p).strip() for p in trusted_proxies if str(p).strip()}
    elif isinstance(trusted_proxies, str):
        trusted_proxies = {p.strip() for p in trusted_proxies.split(",") if p.strip()}
    else:
        trusted_proxies = {"127.0.0.1", "::1"}

    # If the direct peer is not a trusted proxy, it might be an untrusted client attempting to spoof headers; ignore XFF
    if remote_addr not in trusted_proxies:
        return remote_addr

    # Fetch X-Forwarded-For header containing proxy chain: "client_ip, proxy1_ip, proxy2_ip"
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if not xff or not isinstance(xff, str):
        return remote_addr

    # Parse comma-separated XFF IPs in order, sanitizing valid IP characters to prevent Redis key injection
    raw_ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    clean_ips = []
    for ip in raw_ips:
        sanitized = re.sub(r"[^a-zA-Z0-9.:%-]", "", ip)
        if sanitized:
            clean_ips.append(sanitized)

    if not clean_ips:
        return remote_addr

    # Iterate backwards (right-to-left) from the most recent proxy toward the original caller.
    # The first IP from the right that is NOT in our trusted proxy list is the authentic client IP.
    for ip in reversed(clean_ips):
        if ip not in trusted_proxies:
            return ip

    # Fallback: if every IP in the chain was trusted, return the earliest one in the list
    return clean_ips[0]


def _email_hash(email):
    """
    Short hash of normalized email used in Redis keys.
    Guarantees canonical lowercase hashing and prevents lockout bypass.
    """
    norm = _normalize_email(email)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]




# verfied
def _create_jwt(user_id, user_email, session_id=None, expires_in=None):
    """Create a signed HS256 JWT."""
    now = int(time.time())
    if expires_in is None:
        expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))

    payload = {
        "user_id": user_id,
        "user_email": user_email,
        "iat": now,
        "exp": now + expires_in,
    }
    if session_id:
        payload["sid"] = session_id

    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

# verified
def verify_jwt(token):
    """Verify and decode a JWT. Returns (payload, None) on success, or (None, err_str)."""
    if not token or not isinstance(token, str) or token.count(".") != 2:
        return None, "Invalid token format"

    if not settings.SECRET_KEY:
        logger.error("SECRET_KEY is not configured on the server")
        raise AuthenticationFailed({
            "error": "Server error, try again later ...",
            "code": "SERVER_KEY_ERROR",
        })

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, "Token has expired"
    except jwt.InvalidSignatureError:
        return None, "Invalid token signature"
    except jwt.PyJWTError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)

# verified
def get_user_from_request(request):
    """
    Extracts authenticated user identity from Authorization header or request payload.
    Memoizes the result on the request object for zero-overhead repeated lookups.
    """
    if hasattr(request, "_cached_user_info"):
        return request._cached_user_info

    auth_header = request.headers.get("Authorization", "") # fallback "" given for grazeful stop
    token = None
    
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1].strip() # index 1 holds the JWT Token

    user_info = None
    if token:
        payload, err = verify_jwt(token)
        # error is consumed
        if payload:
            uid = payload.get("user_id")
            uemail = payload.get("user_email")
            sid = payload.get("sid")

            user_info = {
                "user_id": uid,
                "user_email": uemail,
                "email": uemail,
                "sid": sid,
                "is_authenticated": True
            }

    try:
        request._cached_user_info = user_info
    except AttributeError:
        pass

    return user_info

# verified
def _verify_staff_key(header_val):
    """Verify staff key from request header with constant-time comparison."""
    if not isinstance(header_val, str) or not header_val.strip():
        raise AuthenticationFailed({
            "error": "Invalid staff key provided.",
            "code": "INVALID_STAFF_KEY"
        })

    staff_key = os.getenv("STAFF_API_KEY")
    if not staff_key:
        logger.error("Staff verification failed: STAFF_API_KEY environment variable is not configured on the server.")
        raise ServiceUnavailable({
            "error": "Staff key service is not available currently.",
            "code": "STAFF_SERVICE_UNAVAILABLE"
        })

    return hmac.compare_digest(header_val.strip(), staff_key.strip())

# verified
def _is_admin_email(email):
    """
    Check if email belongs to configured ADMIN_DOMAIN(s).
    Supports Django settings, env vars, comma-separated domains, and leading '@' stripping.
    """
    norm = _normalize_email(email)
    if not norm:
        return False

    raw_domain = getattr(settings, "ADMIN_DOMAIN", None) or os.getenv("ADMIN_DOMAIN", "")
    if not raw_domain or not isinstance(raw_domain, str):
        return False

    # Normalize comma-separated domains into a clean set
    admin_domains = {
        d.strip().removeprefix("@").lower()
        for d in raw_domain.split(",")
        if d.strip() and d.strip() != "@"
    }
    if not admin_domains:
        return False

    parts = norm.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False

    return parts[1] in admin_domains

# verified
class RequesterIdentity(tuple):
    """
    Caller identity 3-tuple (is_authenticated, user_id, is_staff) with .user_email.
    Fully compatible with 3-tuple unpacking, pickle serialization, and caching.
    """

    def __new__(cls, is_authenticated, user_id=None, is_staff=False, user_email=None):
        # Support reconstitution from single sequence tuple (pickle/cache unpacking)
        if isinstance(is_authenticated, tuple) and user_id is None:
            seq = is_authenticated
            is_authenticated = seq[0] if len(seq) > 0 else False
            user_id = seq[1] if len(seq) > 1 else None
            is_staff = seq[2] if len(seq) > 2 else False

        auth_bool = bool(is_authenticated)
        staff_bool = bool(is_staff)

        instance = super().__new__(cls, (auth_bool, user_id, staff_bool))
        instance.is_authenticated = auth_bool
        instance.user_id = user_id
        instance.is_staff = staff_bool
        instance.user_email = user_email
        return instance

    def __reduce__(self):
        """Preserve all tuple elements and user_email across pickle / cache round-trips."""
        return (self.__class__, (self.is_authenticated, self.user_id, self.is_staff, self.user_email))

    def __repr__(self):
        return (
            f"RequesterIdentity(is_authenticated={self.is_authenticated!r}, "
            f"user_id={self.user_id!r}, is_staff={self.is_staff!r}, user_email={self.user_email!r})"
        )

# verified
def _get_caller(request):
    """
    Resolve caller identity strictly from verified sources:
    1. Staff Key (Admin privilege)
    2. Bearer JWT (Authenticated User)
    No unverified headers or environment fallbacks allowed.
    Memoized on the request object for zero-overhead repeated lookups.
    """
    if hasattr(request, "_cached_caller_identity"):
        return request._cached_caller_identity

    identity = None

    # Staff verification
    req_staff = request.headers.get("X-Staff-Key")
    if req_staff :
        if not _verify_staff_key(req_staff):
            raise AuthenticationFailed({
                "error": "Invalid staff key provided",
                "code": "INVALID_STAFF_KEY "
            })
        
        # both user_id and user_email are shared values.
        user_id = os.getenv("STAFF_USER_ID") or "system_staff"
        user_email = os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"
        identity = RequesterIdentity(True, user_id, True, user_email=user_email)

    # Normal JWT authentication, if not staff
    if not identity:
        user_info = get_user_from_request(request)
        if user_info and user_info.get("is_authenticated"):
            user_id = user_info.get("user_id")
            user_email = user_info.get("user_email")
            is_staff = _is_admin_email(user_email)

            identity = RequesterIdentity(True, user_id, is_staff, user_email=user_email)

    # Unauthenticated
    if not identity:
        identity = RequesterIdentity(False, None, False, user_email=None)

    try:
        request._cached_caller_identity = identity
    except AttributeError:
        pass

    return identity


resend.api_key = os.getenv("RESEND_API_KEY")
# verified
def _dispatch_otp_email(to_email, otp_code):
    """
    Dispatch 6-digit verification OTP email via Resend API.
    Designed for safe execution inside daemon background threads with structured logging.
    """
    clean_to = _normalize_email(to_email)
    if not clean_to:
        logger.error("Failed to dispatch OTP email: Recipient email address is missing or invalid.")
        return False

    # Defensively enforce strictly 6 numeric digits to prevent template injection
    clean_otp = re.sub(r"[^0-9]", "", str(otp_code or ""))
    if len(clean_otp) != 6:
        logger.error("Failed to dispatch OTP email to %s: Invalid OTP code format.", clean_to)
        return False

    api_key = getattr(settings, "RESEND_API_KEY", None) or os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.error("Failed to dispatch OTP email: RESEND_API_KEY is not configured on the server.")
        return False

    resend.api_key = api_key

    from_email = (
        getattr(settings, "RESEND_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or os.getenv("RESEND_FROM_EMAIL")
        or "DTC Security <otp@relaydtc.online>"
    )

    subject = f"Your DTC Verification Code: {clean_otp}"

    text_content = f"""Hello,

    Your verification code for the Distributed Transaction Coordinator is:

        {clean_otp}

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
        Your DTC verification code is {clean_otp}. It expires in 5 minutes.
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
                          {clean_otp}
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
            "from": from_email,
            "to": [clean_to],
            "subject": subject,
            "html": html_content,
            "text": text_content,
        }
        result = resend.Emails.send(params)
        msg_id = result.get("id") if isinstance(result, dict) else None
        logger.info("Verification code delivered to %s via Resend (id=%s).", clean_to, msg_id)
        return True
    except Exception as exc:
        logger.error(
            "Failed to deliver verification code to %s via Resend: %s",
            clean_to,
            exc,
            exc_info=True,
        )
        return False

# verified
@api_view(["POST"])
def send_otp(request):
    """
    Phase 1: Generates random 6-digit numeric OTP, stores in Redis (5-min TTL) with in-memory fallback,
    enforces rate limits (cooldown, daily limit, abandonment), and dispatches via Resend.
    """
    if not isinstance(request.data, dict):
        return Response(
            {"error": "Invalid request payload, expected JSON object.", "code": "INVALID_PAYLOAD"},
            status=400,
        )

    email_raw = request.data.get("email")
    if not email_raw:
        return Response({"error": "email is required"}, status=400)

    email = _normalize_email(email_raw)
    is_valid, err_msg = validate_email(email)
    if not is_valid:
        return Response({"error": err_msg, "code": "INVALID_EMAIL"}, status=400)

    is_dev_mode = _is_dev_auth_mode()
    is_testing = bool(getattr(settings, "IS_TESTING", False))
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

    daily_block_key = f"dtc:otp_daily_blocked:{email}"
    daily_key       = f"dtc:otp_daily:{email}"
    cooldown_key    = f"dtc:otp_cooldown:{email}"

    # In production/testing: enforce IP+email block, cooldown, abandonment tracking, and daily limit
    if not is_dev_mode:
        client_ip   = _get_client_ip(request)
        block_key   = f"dtc:ip_block:{client_ip}:{_email_hash(email)}"
        abandon_key = f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}"

        # 1. IP+email block check (wrong OTPs or abandonment abuse)
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

        # 2. Cooldown check: prevent re-send within 30 seconds
        cooldown_active = False
        cooldown_ttl = 30
        if client:
            try:
                if client.exists(cooldown_key):
                    t = client.ttl(cooldown_key)
                    if t > 0:
                        cooldown_active = True
                        cooldown_ttl = t
            except Exception:
                pass
        if not cooldown_active:
            try:
                if cache.get(cooldown_key):
                    cooldown_active = True
            except Exception:
                pass

        if cooldown_active:
            daily_used = 0
            if client:
                try:
                    daily_used = int(client.get(daily_key) or 0)
                except Exception:
                    pass
            return Response(
                {
                    "success": True,
                    "message": "Verification code was recently sent.",
                    "user_email": email,
                    "user_id": _generate_user_id(email),
                    "cooldown_remaining": cooldown_ttl,
                    "is_cooldown": True,
                    "chances_remaining": max(0, 10 - daily_used),
                },
                status=200,
            )

        # 3. Hard daily block (set after limit is crossed)
        daily_blocked = False
        if client:
            try:
                if client.exists(daily_block_key):
                    daily_blocked = True
            except Exception:
                pass
        if not daily_blocked:
            try:
                if cache.get(daily_block_key):
                    daily_blocked = True
            except Exception:
                pass

        if daily_blocked:
            return Response(
                {
                    "error": "Daily OTP limit reached. You can request a new code tomorrow.",
                    "code": "DAILY_LIMIT_REACHED",
                },
                status=429,
            )

        # 4. Abandonment tracking: send without verify (5+ unverified sends = 24h block)
        if client:
            try:
                abandon_count = client.incr(abandon_key)
                if abandon_count == 1 or client.ttl(abandon_key) == -1:
                    client.expire(abandon_key, 7200)  # 2-hour rolling window

                if abandon_count > 5:
                    client.setex(block_key, 86400, "abandon")
                    client.delete(abandon_key)
                    try:
                        cache.set(block_key, "abandon", timeout=86400)
                    except Exception:
                        pass
                    return Response(
                        {
                            "error": "Too many OTP requests without completing verification. Your network is blocked for 24 hours.",
                            "code": "IP_EMAIL_BLOCKED",
                            "retry_after": 86400,
                        },
                        status=429,
                    )
            except Exception:
                pass

        # 5. Daily limit tracking (expires at midnight)
        if client:
            try:
                now_dt = timezone.now()
                midnight = (now_dt + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                ttl_until_midnight = max(1, int((midnight - now_dt).total_seconds()))

                daily_count = client.incr(daily_key)
                if daily_count == 1 or client.ttl(daily_key) == -1:
                    client.expire(daily_key, ttl_until_midnight)

                if daily_count > 10:
                    client.setex(daily_block_key, ttl_until_midnight, "blocked")
                    client.delete(daily_key)
                    try:
                        cache.set(daily_block_key, "blocked", timeout=ttl_until_midnight)
                    except Exception:
                        pass
                    return Response(
                        {
                            "error": "Daily OTP limit reached. You can request a new code tomorrow.",
                            "code": "DAILY_LIMIT_REACHED",
                        },
                        status=429,
                    )

                chances_remaining = max(0, 10 - int(daily_count))
            except Exception:
                pass

    # Generate cryptographically secure 6-digit numeric OTP [100000, 999999]
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    cache_key = f"dtc:otp:{email}"

    if client:
        try:
            client.setex(cache_key, 300, otp_code)
            if is_dev_mode:
                for leftover_k in [
                    daily_block_key,
                    daily_key,
                    cooldown_key,
                    f"dtc:otp_session_blocked:{email}",
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
    if not is_dev_mode and not is_testing:
        threading.Thread(target=_dispatch_otp_email, args=(email, otp_code), daemon=True).start()
    else:
        logger.info(
            "[Local Auth] Simulated OTP generated for %s: %s (Infinite dev resends enabled)",
            email,
            otp_code,
        )

    # Set cooldown (30 seconds) in non-dev mode
    if not is_dev_mode:
        if client:
            try:
                client.setex(cooldown_key, 30, "1")
            except Exception:
                pass
        try:
            cache.set(cooldown_key, "1", timeout=30)
        except Exception:
            pass

    user_id = _generate_user_id(email)

    resp_data = {
        "success": True,
        "message": (
            f"[LOCAL DEV] Verification code: {otp_code} (Infinite resends enabled)"
            if is_dev_mode
            else f"Verification code sent to {email}"
        ),
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

# verified
@api_view(["POST"])
def verify_otp(request):
    """
    Phase 2: Compares submitted OTP using constant-time comparison, deletes OTP on match (single-use),
    and issues a signed JWT token. Failed OTPs are tracked per IP+email (5 strikes = 24h block).
    Records a 30s single-device active session on success.
    """
    if not isinstance(request.data, dict):
        return Response(
            {"error": "Invalid request payload, expected JSON object.", "code": "INVALID_PAYLOAD"},
            status=400,
        )

    email_raw = request.data.get("email")
    submitted_otp = str(request.data.get("otp", "")).strip()

    if not email_raw or not submitted_otp:
        return Response({"error": "email and otp are required"}, status=400)

    email = _normalize_email(email_raw)
    if not email:
        return Response({"error": "email and otp are required"}, status=400)

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

    # Constant-time comparison to prevent timing attacks
    stored_str = stored_otp.decode("utf-8") if isinstance(stored_otp, bytes) else str(stored_otp or "")
    otp_matches = bool(stored_otp) and hmac.compare_digest(stored_str.strip(), submitted_otp)

    if not otp_matches:
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
                if fail_count == 1 or client.ttl(fail_key) == -1:
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

    # OTP matched: clean slate on success (single-use)
    if client:
        try:
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

    jwt_expires_in = int(
        getattr(settings, "JWT_EXPIRES_IN_SECONDS", None)
        or os.getenv("JWT_EXPIRES_IN_SECONDS", "1800")
    )
    jwt_token = _create_jwt(user_id=user_id, user_email=email, session_id=session_id, expires_in=jwt_expires_in)

    return Response({
        "success": True,
        "message": "Authenticated successfully!",
        "token": jwt_token,
        "user_id": user_id,
        "user_email": email,
        "email": email,
        "expires_in": jwt_expires_in,
    }, status=200)


# verified
@api_view(["GET"])
def get_me(request):
    """
    Phase 3: Returns current authenticated user identity from verified JWT footprint.
    Includes staff role determination and validates against superseded sessions.
    """
    user_info = get_user_from_request(request)
    if not user_info or not user_info.get("is_authenticated"):
        return Response({"authenticated": False, "user": None}, status=401)

    email = _normalize_email(user_info.get("email") or user_info.get("user_email"))
    token_sid = user_info.get("sid")

    # Guard: Detect if session was already superseded on another device
    if token_sid and email:
        client = _get_redis_client()
        active_sid = None
        if client:
            try:
                raw_val = client.get(f"dtc:session:{email}")
                if raw_val:
                    active_sid = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else raw_val
            except Exception:
                pass
        if not active_sid:
            try:
                active_sid = cache.get(f"dtc:session:{email}")
            except Exception:
                pass

        if active_sid and not hmac.compare_digest(str(active_sid), str(token_sid)):
            return Response(
                {
                    "authenticated": False,
                    "error": "Session superseded by another login.",
                    "code": "SESSION_SUPERSEDED",
                },
                status=401,
            )

    user_data = dict(user_info)
    user_data["is_staff"] = _is_admin_email(email)

    return Response({
        "authenticated": True,
        "user": user_data,
    }, status=200)

# verified
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

    email = _normalize_email(user_info.get("email") or user_info.get("user_email"))
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

    check_key = f"dtc:session:{email}" if email else f"dtc:session:{user_id}"

    # 1. Redis supersession check and refresh
    if client:
        try:
            raw_val = client.get(check_key)
            if raw_val:
                active_sid = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else raw_val
                if token_sid and active_sid and not hmac.compare_digest(str(active_sid), str(token_sid)):
                    return Response(
                        {
                            "error": "Session superseded by another login.",
                            "code": "SESSION_SUPERSEDED",
                        },
                        status=401,
                    )
            for k in session_keys:
                client.setex(k, 30, token_sid or "active")
        except Exception:
            pass

    # 2. Cache fallback supersession check and refresh
    try:
        cache_sid = cache.get(check_key)
        if cache_sid and token_sid and not hmac.compare_digest(str(cache_sid), str(token_sid)):
            return Response(
                {
                    "error": "Session superseded by another login.",
                    "code": "SESSION_SUPERSEDED",
                },
                status=401,
            )
        for k in session_keys:
            cache.set(k, token_sid or "active", timeout=30)
    except Exception:
        pass

    return Response({"status": "alive"}, status=200)

# verified
@api_view(["POST"])
def logout(request):
    """
    Deletes the authenticated user's active session in Redis and cache.
    Guaranteed idempotent (returns 200 OK regardless).
    """
    user_info = get_user_from_request(request)
    if user_info:
        email = _normalize_email(user_info.get("email") or user_info.get("user_email"))
        user_id = user_info.get("user_id")

        keys_to_del = []
        if email:
            keys_to_del.append(f"dtc:session:{email}")
        if user_id:
            keys_to_del.append(f"dtc:session:{user_id}")

        if keys_to_del:
            client = _get_redis_client()
            if client:
                try:
                    client.delete(*keys_to_del)
                except Exception:
                    pass
            for k in keys_to_del:
                try:
                    cache.delete(k)
                except Exception:
                    pass

    return Response({"status": "logged_out"}, status=200)
