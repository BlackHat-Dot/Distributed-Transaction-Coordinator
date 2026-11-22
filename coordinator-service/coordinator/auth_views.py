# Imports PyJWT library to encode, decode, and cryptographically verify authentication tokens and session payloads.
import jwt
# Imports Python's hashlib library to generate SHA-256 hashes of OTP tokens and magic link secrets for constant-length secure comparison.
import hashlib
# Imports HMAC cryptographic module to perform timing-safe digest comparisons and prevent side-channel timing attacks.
import hmac
# Imports Python standard logging facility to record authentication events, security warnings, and audit trails.
import logging
# Imports operating system module to read environment variables such as token expiration intervals and staff secret keys.
import os
# Imports regular expression engine to validate email syntax and sanitize incoming client input parameters.
import re
# Imports cryptographically strong pseudo-random number generator for generating high-entropy OTP codes and link tokens.
import secrets
# Imports threading module to provide synchronization primitives such as Lock for thread-safe Redis connection pool management.
import threading
# Imports time module to measure monotonic clock intervals for rate limiting and Redis circuit breaker health pings.
import time
# Imports uuid module to parse, validate, and generate universally unique user and session identifiers.
import uuid
# Imports datetime and timedelta utilities to compute and compare timestamp expirations for tokens and OTP challenges.
from datetime import datetime, timedelta
# Imports redis client library to connect to the in-memory cache and key-value store for session and rate-limiting storage.
import redis
# Imports Resend email delivery service SDK to transmit authentication emails containing OTP codes and magic links to users.
import resend
# Imports Django application configuration settings containing API keys, token secrets, and environment parameters.
from django.conf import settings
# Imports Django cache framework to interface with caching backends for quick key lookups and temporary state caching.
from django.core.cache import cache
# Imports HTTP status codes from Django REST Framework for standardized API response status reporting.
from rest_framework import status
# Imports api_view decorator to wrap functional views with DRF request handling, authentication, and permission checks.
from rest_framework.decorators import api_view
# Imports DRF Response object to serialize Python dictionaries into HTTP response payloads with proper content negotiation.
from rest_framework.response import Response
# Imports DRF exception classes to raise structured API errors and reject unauthenticated requests with appropriate HTTP codes.
from rest_framework.exceptions import APIException, AuthenticationFailed
# Imports Django timezone utilities to obtain timezone-aware datetime objects conforming to UTC and local settings.
from django.utils import timezone

# reates a namespaced logger instance tied to this module so error and debug messages trace back here.
logger = logging.getLogger(__name__)

# Timestamp tracker storing the last time Redis connection health was evaluated to prevent spamming status checks.
_redis_last_check = 0.0

# Timestamp tracker recording the last 'successful' ping response from the Redis server.
_redis_last_ping = 0.0

# Module-level singleton variable holding the reusable, active Redis connection pool instance.
_redis_client_instance = None

# A mutex lock ensuring only one thread initializes or rebuilds the Redis connection at a time to prevent race conditions.
_redis_lock = threading.Lock()

# Defines a custom Django REST Framework exception to signal that a critical dependency is down.
class ServiceUnavailable(APIException):
    # Sets HTTP status code to 503 Service Unavailable indicating an essential backend dependency is offline.
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    # Provides the machine-readable error code string returned in the error response payload.
    default_code = "STAFF_SERVICE_UNAVAILABLE"


# Defines an internal helper function that manages, health-checks, and safely returns an active Redis client connection.
def _get_redis_client():
    """Return a healthy cached Redis client with thread-safety and circuit-breaker self-healing."""
    # Declares module-level state variables as global so connection timestamps and client handles can be mutated across calls.
    global _redis_last_check, _redis_last_ping, _redis_client_instance

    # Captures a clock timestamp that always moves forward and never jumps backwards (even if system time changes), making it ideal for measuring elapsed intervals.
    now = time.monotonic()

    # Checks if an existing Redis connection instance is already cached in memory.
    if _redis_client_instance is not None:
        # Checks if a Redis health ping succeeded within the last 2 seconds to avoid overloading Redis with unnecessary ping calls on every request.
        if now - _redis_last_ping < 2.0:
            # Returns the cached client immediately without sending a ping if the last ping was recent.
            return _redis_client_instance
        # Wraps the connection health verification ping in an exception handler to catch socket drops or timeouts.
        try:
            # Sends a lightweight heartbeat ping to Redis to actively verify the connection is still alive.
            _redis_client_instance.ping()

            # Updates the last verified ping timestamp so subsequent calls within the 2-second grace period can skip pinging.
            _redis_last_ping = now

            # Returns the verified active Redis client after confirming successful ping response.
            return _redis_client_instance

        # Catches any ping failure (network drop, timeout, or closed connection) and binds the error object to exc.
        except Exception as exc:
            # Records a warning log containing the failure reason so operators can detect connection health issues.
            logger.warning("Cached Redis client failed health ping (%s); invalidating instance.", exc)

            # Acquires the thread lock to ensure only one thread mutates the shared Redis connection state at a time.
            with _redis_lock:
                # Verifies the cached instance has not already been reset by a concurrent thread.
                if _redis_client_instance is not None:
                    # Safely closes open socket connections without crashing if the socket is already corrupted.
                    try:
                        _redis_client_instance.close()
                    # Intercepts any exception raised during connection socket shutdown to prevent resource leak aborts.
                    except Exception:
                        # Ignores cleanup exceptions when closing an already broken socket.
                        pass

                    # Wipes the cached instance variable so subsequent requests know they must re-establish a fresh connection.
                    _redis_client_instance = None

                    # Records when the failure occurred to enforce a cooldown before attempting another connection.
                    _redis_last_check = now
            # Returns None to signal to the caller that the existing Redis instance failed health check and was cleared.
            return None

    # Circuit breaker cooldown: Redis failed within the last 5 seconds, it immediately returns None without wasting time attempting to reconnect.
    if now - _redis_last_check < 5.0:
        # Returns None immediately during the circuit breaker cooldown period without attempting an expensive reconnect.
        return None

    # Acquires the lock to guarantee only one thread attempts reconnection at any given moment.
    with _redis_lock:
        # Double-checked locking pattern; returns the client if another thread already reconnected while this thread was waiting.
        if _redis_client_instance is not None:
            # Returns the newly initialized Redis client created by a concurrent thread.
            return _redis_client_instance

        # Samples the monotonic timestamp inside the locked section to re-evaluate cooldown status.
        now = time.monotonic()
        # Checks whether another thread attempted connection and triggered a cooldown failure while lock was pending.
        if now - _redis_last_check < 5.0:
            # Aborts connection attempt if circuit breaker cooldown is active within locked context.
            return None

        # Updates the last connection attempt timestamp to mark start of current reconnect attempt.
        _redis_last_check = now
        # Resolves the Redis broker URL from Django application settings or defaults to localhost.
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
        # Determines if current execution is running inside automated test suite to set aggressive timeouts.
        testing_mode = bool(getattr(settings, "IS_TESTING", False))

        # Sets a tight network socket timeout (0.02s in tests, 0.5s in production) to fail fast on outages.
        timeout = getattr(settings, "REDIS_SOCKET_TIMEOUT", 0.02 if testing_mode else 0.5)

        # Initializes the client variable to ensure clean error handling in the except block if connection setup fails.
        client = None
        # Guards the Redis connection establishment and initial ping handshake against socket and DNS failures.
        try:
            # Creates a new Redis client with UTF-8 string decoding and strict connection/read timeouts.
            client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
            )

            # Immediately tests whether the newly created connection is actually responsive.
            client.ping()
            # Caches the verified active Redis client in the singleton module variable.
            _redis_client_instance = client
            # Updates the ping timestamp to record the time of the successful connection handshake.
            _redis_last_ping = now

            # Returns the freshly established and verified Redis client to the caller.
            return client

        # Catches any connection failure, DNS resolution error, or ping timeout during the connection attempt.
        except Exception:
            # Cleanly shuts down dangling network sockets if the client was partially constructed before failing.
            if client is not None:
                # Wraps socket cleanup in an exception handler to prevent shutdown errors from masking the original exception.
                try:
                    client.close()
                # Silently intercepts any network socket or descriptor exceptions occurring during close.
                except Exception:
                    # Suppresses cleanup exceptions to allow failure handling to proceed cleanly.
                    pass
            # Clears the global client singleton reference to prevent stale socket reuse after failed connect.
            _redis_client_instance = None

            # Returns None to indicate to caller services that Redis remains unavailable.
            return None

# Imports difflib module to provide text sequence matching and similarity scoring for typo-squatting detection.
import difflib
# verified
# Helper predicate function to check whether development authentication bypasses are permitted.
def _is_dev_auth_mode():
    """Return True only if in local development mode (DEBUG=True and not in test suite)."""
    # Distinguishes automated unit test execution from interactive development to ensure test fixtures always enforce strict production security gates.
    if getattr(settings, "IS_TESTING", False):
        # Disables development auth mode under test runners to ensure security suites validate production rules.
        return False
    # Returns True if Django DEBUG setting is active, enabling developer convenience bypasses in local dev.
    return bool(getattr(settings, "DEBUG", False))

# verified
# Helper function to sanitize, lowercase, and strip zero-width characters from email inputs.
def _normalize_email(email_str):
    """Safely strip, sanitize zero-width unicode characters, and lowercase email string."""
    # Validates input is a non-empty string instance to reject null, integer, or invalid types.
    if not email_str or not isinstance(email_str, str):
        # Returns an empty string fallback when input is None or non-string to prevent downstream crashes.
        return ""
    # Strip invisible zero-width unicode characters (\u200b, \u200c, \u200d, \ufeff)
    # Strips invisible zero-width unicode characters to prevent homograph obfuscation and cache key bypass attacks.
    cleaned = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", email_str)
    # Returns the trimmed, lowercased canonical email string for consistent comparison and storage.
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
# Precompiled regular expression enforcing RFC 5322-compliant Gmail username syntax and character length boundaries.
GMAIL_LOCAL_REGEX = re.compile(r"^[a-z0-9][a-z0-9._%+-]{1,62}[a-z0-9]$")

# Enforces immutable frozensets for permitted email domains to provide thread-safe, zero-allocation O(1) membership lookups.
_PROD_DOMAINS = frozenset({"gmail.com"})
# Defines immutable test domains permitted only during testing and local development.
_TEST_DOMAINS = frozenset({"example.com", "test.io", "local.io"})

# Common gmail typos worth catching explicitly (fast O(1) path)
# Precompiles high-frequency keyboard slip typo variants of gmail.com for instant O(1) domain suggestion dispatch.
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
# Suggests typo corrections for misspelled gmail domain parts based on dictionary lookups and edit distance.
def _suggest_domain(domain_part):
    """
    Returns a suggested correction for `domain_part` if it looks like a
    near-miss typo of gmail.com, else None.
    
    1. Rejects distinct legitimate domains and malformed TLDs.
    2. O(1) lookup against known Gmail typo catalog.
    3. Dynamic SequenceMatcher ratio (>= 0.85) for single-letter keyboard slips.
    """
    # Guard check ensuring domain input is a populated string prior to parsing.
    if not domain_part or not isinstance(domain_part, str):
        # Aborts domain suggestion and returns None if domain part is absent or invalid type.
        return None

    # Trims whitespace and normalizes domain part to lowercase for consistent dictionary and regex lookups.
    d = domain_part.strip().lower()

    # Guard: legitimate third-party services and malformed non-TLDs must never suggest gmail.com
    # Explicitly blacklists legitimate independent mail providers (Yahoo, Outlook, Proton) from ever triggering a Gmail typo recommendation.
    if d in _NON_TYPO_REAL_DOMAINS:
        # Returns None immediately if domain is an explicit non-typo legitimate third-party provider.
        return None
    # Rejects malformed domain strings lacking dot delimiters unless equal to bare 'gmail'.
    if "." not in d and d != "gmail":
        # Discards domain strings that cannot represent a valid domain or known abbreviation.
        return None

    # Fast path: known high-frequency typos
    if d in _KNOWN_GMAIL_TYPOS:
        # Returns canonical 'gmail.com' immediately if domain matches high-frequency catalog.
        return "gmail.com"

    # Dynamic path: smart similarity check for keyboard slips (ratio >= 0.85)
    # Computes Gestalt pattern matching similarity ratio to catch non-standard keyboard misspellings while discarding unrelated domains.
    matcher = difflib.SequenceMatcher(None, d, "gmail.com")
    # Checks if character sequence similarity exceeds the 85% threshold for close keyboard slips.
    if matcher.ratio() >= 0.85:
        # Suggests 'gmail.com' correction when dynamic similarity threshold is met.
        return "gmail.com"

    # Returns None when domain does not sufficiently resemble gmail.com.
    return None

# verified
# Main validation utility ensuring incoming email addresses strictly conform to security and provider constraints.
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
    # Verifies email parameter is a non-empty string instance to reject null, integer, or complex payloads.
    if not email or not isinstance(email, str):
        return False, "Email address is required."

    # Trims extraneous whitespace and normalizes email casing to lowercase.
    email = email.strip().lower()

    # Enforces RFC 5321 maximum path length limit to protect downstream SMTP transport agents and database columns from buffer overflows.
    if len(email) > 254:
        return False, "Email address exceeds maximum length of 254 characters."

    # Splits email into local username and domain segments on '@' delimiter.
    parts = email.split("@")
    # Verifies the email contains exactly one '@' character separating local-part and domain.
    if len(parts) != 2:
        # Rejects addresses with zero or multiple '@' characters.
        return False, "Email address must contain exactly one '@' symbol."

    # Unpacks the two verified segments into distinct local_part and domain_part variables.
    local_part, domain_part = parts

    # Validates local username length is between 3 and 64 characters per Gmail specifications.
    if not local_part or len(local_part) < 3 or len(local_part) > 64:
        # Rejects username strings shorter than 3 or longer than 64 characters.
        return False, "Email username (before '@') must be between 3 and 64 characters."

    # Rejects consecutive dots in the email local-part in strict accordance with RFC 5322 section 3.4.1.
    if ".." in local_part:
        # Rejects local username strings containing consecutive period characters.
        return False, "Email username cannot contain consecutive dots ('..')."

    # Enforces RFC 5322 rule prohibiting leading or trailing period characters in email usernames.
    if local_part.startswith(".") or local_part.endswith("."):
        # Rejects usernames beginning or concluding with a period.
        return False, "Email username cannot begin or end with a dot."

    # Matches local part against regex to ensure only permitted alphanumeric characters and dots are present.
    if not GMAIL_LOCAL_REGEX.match(local_part):
        # Returns failure if username contains disallowed special characters or invalid patterns.
        return False, "Email username contains invalid characters."

    # Sets default allowed domain set strictly to production domains.
    allowed_domains = _PROD_DOMAINS
    # Expands permitted email domain set to include mock test domains during testing or development.
    if getattr(settings, "IS_TESTING", False) or getattr(settings, "DEBUG", False):
        # Combines production and test domains into active allowed set.
        allowed_domains = _PROD_DOMAINS | _TEST_DOMAINS

    # Validates whether extracted domain part belongs to permitted email domains.
    if domain_part not in allowed_domains:
        # Checks if unrecognized domain matches known Gmail typo patterns.
        suggestion = _suggest_domain(domain_part)
        # Evaluates whether a valid typo suggestion was returned for user guidance.
        if suggestion:
            # Returns rejection response enriched with friendly typo correction recommendation.
            return False, f"Only @gmail.com addresses are supported. Did you mean {local_part}@{suggestion}?"
        # Rejects unpermitted domain with standardized unsupported domain error message.
        return False, "Only @gmail.com email addresses are supported."

    # Returns true tuple indicating email successfully passed syntax and domain policy checks.
    return True, None


# Generates deterministic user identifier prefixed with usr_ derived from SHA256 of canonical email.
def _generate_user_id(email):
    """Generate deterministic user ID prefixed with usr_ from normalized email SHA256."""
    # Sanitizes input email to ensure user ID hashing is consistent across casing variants.
    # Canonicalizes email address to prevent cache key divergence across mixed cases.
    # Canonicalizes email string to lowercase prior to domain comparison.
    norm = _normalize_email(email)
    # Returns empty string fallback if email parameter is empty or non-string.
    if not norm:
        return ""
    # Computes a deterministic 16-character hexadecimal hash of the canonical email to generate persistent, PII-free internal user identifiers.
    sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    # Prefixes truncated hexadecimal digest with 'usr_' namespace to format application user identifier.
    return f"usr_{sha}"


# Resolves client IP address safely while preventing header spoofing via trusted proxy validation.
def _get_client_ip(request):
    """
    Safely resolves client IP address (SEC-05).
    X-Forwarded-For is only trusted if the immediate peer (REMOTE_ADDR)
    is in the configured TRUSTED_PROXIES set. Otherwise REMOTE_ADDR is used.
    """
    # Returns 'unknown' if request context or META header mapping is absent.
    if not request or not hasattr(request, "META"):
        return "unknown"

    # Safely extract REMOTE_ADDR without crashing if value is None
    # Extracts direct peer TCP socket address from WSGI request metadata dictionary.
    raw_remote = request.META.get("REMOTE_ADDR")
    # Sanitizes remote address string, defaulting to 'unknown' if empty or invalid type.
    remote_addr = (raw_remote.strip() if isinstance(raw_remote, str) else "") or "unknown"

    # Normalize whitelist of trusted proxy IPs from settings or environment
    # Retrieves configured trusted reverse proxy addresses from application settings.
    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", None)
    # Falls back to environment configuration when settings variable is unconfigured.
    if trusted_proxies is None:
        # Reads comma-separated TRUSTED_PROXIES environment variable with default loopback addresses.
        raw = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1")
        # Converts environment variable string into a trimmed, non-empty set of IP strings.
        trusted_proxies = {ip.strip() for ip in raw.split(",") if ip.strip()}
    # Handles collection types (list, tuple, set) by trimming non-empty string representations.
    elif isinstance(trusted_proxies, (list, tuple, set)): 
        trusted_proxies = {str(p).strip() for p in trusted_proxies if str(p).strip()}
    # Parses string formatted settings into a set of trimmed IP addresses.
    elif isinstance(trusted_proxies, str):
        trusted_proxies = {p.strip() for p in trusted_proxies.split(",") if p.strip()}
    # Defaults to loopback addresses if trusted_proxies is configured with an unrecognized type.
    else:
        trusted_proxies = {"127.0.0.1", "::1"}

    # If the direct peer is not a trusted proxy, it might be an untrusted client attempting to spoof headers; ignore XFF
    # Anti-spoofing firewall: ignores X-Forwarded-For headers entirely unless the direct upstream TCP peer is explicitly verified as a trusted reverse proxy.
    if remote_addr not in trusted_proxies:
        # Returns direct remote address without trusting forwarded headers from untrusted proxy peer.
        return remote_addr

    # Fetch X-Forwarded-For header containing proxy chain: "client_ip, proxy1_ip, proxy2_ip"
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    # Verifies presence and string type of HTTP_X_FORWARDED_FOR header before parsing.
    if not xff or not isinstance(xff, str):
        # Returns direct socket address if forwarded header is absent or not a string.
        return remote_addr

    # Parse comma-separated XFF IPs in order, sanitizing valid IP characters to prevent Redis key injection
    raw_ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    # Initializes accumulator list to collect sanitized IP strings.
    clean_ips = []
    # Iterates over each extracted raw IP address in the forwarded proxy chain.
    for ip in raw_ips:
        # Strips any characters outside valid IPv4/IPv6 sets to eliminate injection risks.
        sanitized = re.sub(r"[^a-zA-Z0-9.:%-]", "", ip)
        # Appends non-empty sanitized IP address to clean list.
        if sanitized:
            clean_ips.append(sanitized)

    # Falls back to direct peer IP if sanitization stripped all entries from the forwarded header.
    if not clean_ips:
        return remote_addr

    # Iterate backwards (right-to-left) from the most recent proxy toward the original caller.
    # The first IP from the right that is NOT in our trusted proxy list is the authentic client IP.
    # Traverses the proxy chain from right to left (most recent to oldest) to extract the first untrusted public IP address as the authentic client origin.
    for ip in reversed(clean_ips):
        # Returns the first untrusted upstream IP identified in the proxy chain.
        if ip not in trusted_proxies:
            return ip

    # Fallback: if every IP in the chain was trusted, return the earliest one in the list
    return clean_ips[0]


# Generates a fixed-length SHA-256 digest of normalized email addresses for cache key hashing.
def _email_hash(email):
    """
    Short hash of normalized email used in Redis keys.
    Guarantees canonical lowercase hashing and prevents lockout bypass.
    """
    norm = _normalize_email(email)
    # Derives a short, collision-resistant 20-character digest for Redis cache keys to prevent leaking raw user emails in memory dumps.
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]




# verfied
# Issues a signed JSON Web Token carrying user identity, session identifier, and expiration timestamps.
def _create_jwt(user_id, user_email, session_id=None, expires_in=None):
    """Create a signed HS256 JWT."""
    # Captures current epoch time in integer seconds for JWT issue timestamp.
    now = int(time.time())
    # Falls back to configured environment expiration seconds or 1800 seconds (30 minutes) default.
    if expires_in is None:
        expires_in = int(os.getenv("JWT_EXPIRES_IN_SECONDS", "1800"))

    # Constructs standard JWT claims payload with user identifiers and time boundaries.
    payload = {
        "user_id": user_id,
        "user_email": user_email,
        "iat": now,
        "exp": now + expires_in,
    }
    # Injects optional session ID claim when explicit session tracking is requested.
    if session_id:
        payload["sid"] = session_id

    # Signs the authentication payload using HMAC-SHA256 with the coordinator's secret key, embedding session ID and expiry claims.
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

# verified
# Validates and cryptographically decodes a JSON Web Token using server secret key.
def verify_jwt(token):
    """Verify and decode a JWT. Returns (payload, None) on success, or (None, err_str)."""
    # Validates token is a string containing exactly two dot separators (header.payload.signature).
    if not token or not isinstance(token, str) or token.count(".") != 2:
        return None, "Invalid token format"

    # Guard check ensuring server SECRET_KEY is configured before attempting verification.
    if not settings.SECRET_KEY:
        # Logs critical error if cryptographic signing key is missing from environment.
        logger.error("SECRET_KEY is not configured on the server")
        # Raises DRF AuthenticationFailed exception with generic error for security.
        raise AuthenticationFailed({
            "error": "Server error, try again later ...",
            "code": "SERVER_KEY_ERROR",
        })

    # Executes cryptographic signature verification and payload decoding in protected block.
    try:
        # Enforces mandatory token expiration validation during decode to reject tokens lacking an exp timestamp claim.
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
        # Returns decoded claims dictionary and None error upon successful signature verification.
        return payload, None
    # Handles expired token exception and returns expired error string.
    except jwt.ExpiredSignatureError:
        return None, "Token has expired"
    # Catches signature mismatch and returns invalid signature message.
    except jwt.InvalidSignatureError:
        # Returns failure tuple with signature error detail.
        return None, "Invalid token signature"
    # Catches general PyJWT decode errors and returns string representation.
    except jwt.PyJWTError as e:
        return None, str(e)
    # Intercepts unexpected exceptions during JWT verification.
    except Exception as e:
        return None, str(e)

# verified
# Extracts, validates, and memoizes authenticated user profile from request Authorization headers.
def get_user_from_request(request):
    """
    Extracts authenticated user identity from Authorization header or request payload.
    Memoizes the result on the request object for zero-overhead repeated lookups.
    """
    # Memoizes the decoded user identity on the Django request object to eliminate redundant cryptographic decoding across multiple middleware and view checks.
    if hasattr(request, "_cached_user_info"):
        # Returns pre-computed cached user information without re-verifying token.
        return request._cached_user_info

    # Extracts Authorization header value, defaulting to empty string.
    auth_header = request.headers.get("Authorization", "") # fallback "" given for grazeful stop
    # Initializes token variable to None prior to bearer extraction.
    token = None
    
    # Checks whether authorization header specifies the standard Bearer authentication scheme.
    if auth_header.startswith("Bearer "):
        # Splits header string by space and extracts bearer token payload string.
        token = auth_header.split(" ")[1].strip() # index 1 holds the JWT Token

    # Initializes user information dictionary variable to None.
    user_info = None
    # Proceed with cryptographic verification only if bearer token string was resolved.
    if token:
        # Invokes verify_jwt to decode token and validate expiration and signature integrity.
        payload, err = verify_jwt(token)
        # error is consumed
        # Checks whether payload was successfully decoded without error.
        if payload:
            uid = payload.get("user_id")
            uemail = payload.get("user_email")
            sid = payload.get("sid")

            # Assembles authenticated user metadata dictionary from decoded claims.
            user_info = {
                "user_id": uid,
                "user_email": uemail,
                "email": uemail,
                "sid": sid,
                "is_authenticated": True
            }

    # Attempts to attach memoized user info to request instance, catching immutability errors.
    try:
        request._cached_user_info = user_info
    # Suppresses AttributeError if request object is mock or does not allow dynamic attributes.
    except AttributeError:
        pass

    # Returns extracted user info dictionary or None if unauthenticated.
    return user_info

# verified
# Validates privileged administrative staff API key using timing-safe comparison.
def _verify_staff_key(header_val):
    """Verify staff key from request header with constant-time comparison."""
    # Rejects missing, empty, or non-string header values with AuthenticationFailed exception.
    if not isinstance(header_val, str) or not header_val.strip():
        # Raises AuthenticationFailed exception with INVALID_STAFF_KEY error details.
        raise AuthenticationFailed({
            "error": "Invalid staff key provided.",
            "code": "INVALID_STAFF_KEY"
        })

    # Fetches active staff secret key from operating system environment.
    staff_key = os.getenv("STAFF_API_KEY")
    # Validates the privileged staff API key using constant-time string comparison to prevent timing side-channel attacks.
    if not staff_key:
        logger.error("Staff verification failed: STAFF_API_KEY environment variable is not configured on the server.")
        raise ServiceUnavailable({
            "error": "Staff key service is not available currently.",
            "code": "STAFF_SERVICE_UNAVAILABLE"
        })

    # Performs constant-time digest comparison to prevent timing side-channel leaks.
    return hmac.compare_digest(header_val.strip(), staff_key.strip())

# verified
# Validates whether a normalized email address belongs to authorized ADMIN_DOMAIN domains.
def _is_admin_email(email):
    """
    Check if email belongs to configured ADMIN_DOMAIN(s).
    Supports Django settings, env vars, comma-separated domains, and leading '@' stripping.
    """
    norm = _normalize_email(email)
    # Rejects invalid or empty email inputs as non-admin.
    if not norm:
        return False

    # Resolves configured admin domains from Django settings or environment fallback.
    raw_domain = getattr(settings, "ADMIN_DOMAIN", None) or os.getenv("ADMIN_DOMAIN", "")
    # Returns False if no valid admin domain configuration string is present.
    if not raw_domain or not isinstance(raw_domain, str):
        return False

    # Normalize comma-separated domains into a clean set
    # Parses configured internal admin email domains into a normalized set for instantaneous role escalation lookups.
    admin_domains = {
        d.strip().removeprefix("@").lower()
        for d in raw_domain.split(",")
        if d.strip() and d.strip() != "@"
    }
    # Disallows administrative rights if the parsed admin domain set is empty.
    if not admin_domains:
        return False

    # Splits normalized email into local-part and domain-part components.
    parts = norm.split("@")
    # Validates email has exactly two non-empty components before and after '@'.
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False

    # Returns True if domain matches one of the configured admin domains.
    return parts[1] in admin_domains

# verified
# Custom immutable tuple representing caller authentication state, user ID, staff role, and email.
class RequesterIdentity(tuple):
    """
    Caller identity 3-tuple (is_authenticated, user_id, is_staff) with .user_email.
    Fully compatible with 3-tuple unpacking, pickle serialization, and caching.
    """

    # Constructs new immutable RequesterIdentity tuple instance with convenience attributes.
    def __new__(cls, is_authenticated, user_id=None, is_staff=False, user_email=None):
        # Support reconstitution from single sequence tuple (pickle/cache unpacking)
        # Unpacks nested tuple during deserialization or pickle reconstitution.
        if isinstance(is_authenticated, tuple) and user_id is None:
            seq = is_authenticated
            is_authenticated = seq[0] if len(seq) > 0 else False
            user_id = seq[1] if len(seq) > 1 else None
            is_staff = seq[2] if len(seq) > 2 else False

        # Enforces boolean type casting for authentication status.
        auth_bool = bool(is_authenticated)
        # Enforces boolean type casting for staff privilege flag.
        staff_bool = bool(is_staff)

        # Instantiates underlying 3-element tuple for backward-compatible tuple unpacking.
        instance = super().__new__(cls, (auth_bool, user_id, staff_bool))
        instance.is_authenticated = auth_bool
        instance.user_id = user_id
        instance.is_staff = staff_bool
        instance.user_email = user_email
        # Returns initialized RequesterIdentity instance with dynamic attributes.
        return instance

    # Defines custom pickle protocol reduction logic so RequesterIdentity retains user_email and custom attributes across Redis cache serialization.
    def __reduce__(self):
        """Preserve all tuple elements and user_email across pickle / cache round-trips."""
        return (self.__class__, (self.is_authenticated, self.user_id, self.is_staff, self.user_email))

    # Formats developer-friendly string representation of requester identity attributes.
    def __repr__(self):
        # Returns formatted debug string detailing internal tuple components.
        return (
            f"RequesterIdentity(is_authenticated={self.is_authenticated!r}, "
            f"user_id={self.user_id!r}, is_staff={self.is_staff!r}, user_email={self.user_email!r})"
        )

# verified
# Determines caller identity and staff privileges from staff key or bearer JWT.
def _get_caller(request):
    """
    Resolve caller identity strictly from verified sources:
    1. Staff Key (Admin privilege)
    2. Bearer JWT (Authenticated User)
    No unverified headers or environment fallbacks allowed.
    Memoized on the request object for zero-overhead repeated lookups.
    """
    # Returns cached caller identity from request context if already resolved.
    if hasattr(request, "_cached_caller_identity"):
        return request._cached_caller_identity

    # Initializes caller identity reference to None prior to credential evaluation.
    identity = None

    # Staff verification
    # Priority 1: Evaluates administrative staff API key credentials before falling back to standard customer JWT verification.
    # Extracts privileged X-Staff-Key authentication header from request.
    req_staff = request.headers.get("X-Staff-Key")
    # Evaluates staff key authenticity using timing-safe comparison if header is present.
    if req_staff :
        if not _verify_staff_key(req_staff):
            # Rejects unauthorized staff key attempts with AuthenticationFailed exception.
            raise AuthenticationFailed({
                "error": "Invalid staff key provided",
                "code": "INVALID_STAFF_KEY "
            })
        
        # both user_id and user_email are shared values.
        # Resolves administrative staff user identifier from environment or default string.
        user_id = os.getenv("STAFF_USER_ID") or "system_staff"
        # Resolves administrative staff email address from environment or internal domain default.
        user_email = os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"
        # Constructs authenticated staff RequesterIdentity instance with full privileges.
        identity = RequesterIdentity(True, user_id, True, user_email=user_email)

    # Normal JWT authentication, if not staff
    # Priority 2: Resolves customer authentication identity from the verified Bearer JWT footprint and sets staff flags if using an admin domain.
    if not identity:
        # Resolves authenticated user metadata from Bearer token in request headers.
        user_info = get_user_from_request(request)
        # Evaluates whether user info exists and carries verified authentication status.
        if user_info and user_info.get("is_authenticated"): 
            user_id = user_info.get("user_id")
            user_email = user_info.get("user_email")
            is_staff = _is_admin_email(user_email)

            # Assembles authenticated user RequesterIdentity with user ID and admin role flag.
            identity = RequesterIdentity(True, user_id, is_staff, user_email=user_email)

    # Unauthenticated
    # Falls back to anonymous, unauthenticated RequesterIdentity when no credentials matched.
    if not identity:
        identity = RequesterIdentity(False, None, False, user_email=None)

    # Caches resolved caller identity on request context for zero-cost subsequent reads.
    try:
        request._cached_caller_identity = identity
    except AttributeError:
        pass

    # Returns final resolved RequesterIdentity tuple instance.
    return identity


# verified
# Transmits 6-digit one-time password email to user via Resend transactional delivery API.
def _dispatch_otp_email(to_email, otp_code):
    """
    Dispatch 6-digit verification OTP email via Resend API.
    Designed for safe execution inside daemon background threads with structured logging.
    """
    # Normalizes recipient email address to standard lowercase representation.
    clean_to = _normalize_email(to_email)
    # Rejects transmission and logs error if recipient address is missing or malformed.
    if not clean_to:
        logger.error("Failed to dispatch OTP email: Recipient email address is missing or invalid.")
        return False

    # Defensively enforce strictly 6 numeric digits to prevent template injection
    # Defensively strips non-numeric characters to ensure exactly 6 digits are injected into the transactional email template, preventing injection attacks.
    clean_otp = re.sub(r"[^0-9]", "", str(otp_code or ""))
    # Validates stripped OTP code is exactly six numeric digits before template injection.
    if len(clean_otp) != 6:
        logger.error("Failed to dispatch OTP email to %s: Invalid OTP code format.", clean_to)
        return False

    # Retrieves the Resend API authentication key from Django application settings or host environment variables.
    api_key = getattr(settings, "RESEND_API_KEY", None) or os.getenv("RESEND_API_KEY")
    # Aborts OTP email dispatch if no Resend API key is configured in the application environment.
    if not api_key:
        # Logs server configuration error when Resend API key is missing.
        logger.error("Failed to dispatch OTP email: RESEND_API_KEY is not configured on the server.")
        # Returns False indicating failure to dispatch verification email due to missing credentials.
        return False

    # Configures global Resend client SDK credentials with the retrieved API key.
    resend.api_key = api_key

    # Resolves sender display address and mailbox from settings, environment, or default fallback.
    from_email = (
        getattr(settings, "RESEND_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or os.getenv("RESEND_FROM_EMAIL")
        or "DTC Security <otp@relaydtc.online>"
    )

    # Constructs email subject line containing the 6-digit one-time password code.
    subject = f"Your DTC Verification Code: {clean_otp}"

    # Composes plain-text version of verification email for email clients without HTML rendering support.
    text_content = f"""Hello,

    Your verification code for the Distributed Transaction Coordinator is:

        {clean_otp}

    This code will expire in 5 minutes.

    If you did not request this verification code, please ignore this message
    — no further action is needed.

    — DTC Security
    Distributed Transaction Coordinator"""

    # Constructs responsive branded HTML email body template with stylized OTP code display.
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

    # Attempts to deliver verification email through Resend API while capturing potential network exceptions.
    try:
        # Prepares structured email payload dictionary containing sender, recipient, subject, and content.
        params: resend.Emails.SendParams = {
            "from": from_email,
            "to": [clean_to],
            "subject": subject,
            "html": html_content,
            "text": text_content,
        }
        # Dispatches transactional verification email via Resend's REST API with preheader preview text and branded HTML layout.
        result = resend.Emails.send(params)
        # Extracts external message delivery ID from the Resend API response for tracing.
        msg_id = result.get("id") if isinstance(result, dict) else None
        # Logs successful OTP email dispatch with recipient and delivery identifier.
        logger.info("Verification code delivered to %s via Resend (id=%s).", clean_to, msg_id)
        # Returns True signaling successful email dispatch to the caller.
        return True
    # Intercepts communication errors, network failures, or API rejections during email dispatch.
    except Exception as exc:
        # Logs structured error diagnostic details with full exception stack trace.
        logger.error(
            "Failed to deliver verification code to %s via Resend: %s",
            clean_to,
            exc,
            exc_info=True,
        )
        # Returns False indicating failure to deliver verification code via email.
        return False

# verified
# Restricts endpoint to handle HTTP POST requests only for OTP generation.
@api_view(["POST"])
# Handles client request to generate and dispatch a 6-digit one-time password.
def send_otp(request):
    """
    Phase 1: Generates random 6-digit numeric OTP, stores in Redis (5-min TTL) with in-memory fallback,
    enforces rate limits (cooldown, daily limit, abandonment), and dispatches via Resend.
    """
    # Validates that incoming request body contains a JSON object dictionary.
    # Validates that incoming request body contains a JSON object dictionary.
    if not isinstance(request.data, dict):
        # Returns HTTP 400 Bad Request error if payload is not a valid JSON dictionary.
        # Returns HTTP 400 Bad Request error if payload is not a valid JSON dictionary.
        return Response(
            {"error": "Invalid request payload, expected JSON object.", "code": "INVALID_PAYLOAD"},
            status=400,
        )

    # Extracts raw target recipient email address parameter from parsed request data.
    # Extracts raw target email address parameter from request payload.
    email_raw = request.data.get("email")
    # Guards against missing or empty email field in the request payload.
    if not email_raw:
        # Returns HTTP 400 Bad Request when email address parameter is omitted.
        return Response({"error": "email is required"}, status=400)

    # Normalizes email casing and trims surrounding whitespace for consistent matching.
    # Normalizes email casing and formatting for consistent cache and database matching.
    email = _normalize_email(email_raw)
    # Validates normalized email format against RFC specifications and domain checks.
    is_valid, err_msg = validate_email(email)
    # Rejects request early if email syntax validation checks fail.
    if not is_valid:
        # Returns HTTP 400 Bad Request with specific validation failure message.
        return Response({"error": err_msg, "code": "INVALID_EMAIL"}, status=400)

    # Evaluates whether server is running in dev authentication bypass mode.
    # Determines whether development authentication bypass mode is currently enabled.
    is_dev_mode = _is_dev_auth_mode()
    # Determines if current runtime environment is executing unit or integration tests.
    is_testing = bool(getattr(settings, "IS_TESTING", False))
    # Obtains shared Redis connection client or in-memory fallback adapter.
    # Obtains shared Redis connection client or in-memory fallback adapter.
    client = _get_redis_client()

    # Initializes maximum allowed verification attempts counter for daily quota tracking.
    chances_remaining = 10

    # Single-Device Guard: Reject new login attempts if an active session already exists for this email
    # Formats Redis session lookup cache key scoped to the normalized email address.
    # Formats Redis session lookup cache key scoped to the normalized email address.
    session_key = f"dtc:session:{email}"
    # Initializes flag tracking whether an active concurrent session was detected.
    session_active = False

    # Queries primary Redis cache store for active session key if client is available.
    if client:
        # Wraps primary Redis session presence lookup in error-resilient exception handler.
        try:
            # Checks whether active session record exists in Redis for this user account.
            if client.exists(session_key):
                # Marks session as active when concurrent login record is found in Redis.
                session_active = True
        # Catches Redis communication failures during session presence lookup.
        except Exception:
            # Silently continues to secondary cache check if Redis query encounters errors.
            pass

    # Falls back to local Django cache if Redis did not report an active session.
    if not session_active:
        # Safely attempts in-memory cache lookup for active session key.
        try:
            # Inspects in-memory cache to detect active login session for this email.
            if cache.get(session_key):
                # Flags session as active when session key is found in fallback cache.
                session_active = True
        # Catches cache retrieval exceptions during fallback session lookup.
        except Exception:
            # Ignores cache errors and proceeds with login flow.
            pass

    # Enforces single-device login policy: immediately blocks new login attempts if an active session is currently alive on another device.
    if session_active:
        # Returns HTTP 409 Conflict rejecting concurrent login attempts on multiple devices.
        return Response(
            {
                "error": "Another device is currently logged in with this email.",
                "code": "CONCURRENT_LOGIN_BLOCKED",
                "message": "Another device is currently logged in with this email. Please sign out from that device first or wait for the session to expire.",
            },
            status=409,
        )

    # Key identifying users who have been locked out for 24 hours after exceeding daily OTP requests.
    daily_block_key = f"dtc:otp_daily_blocked:{email}"
    # Key tracking cumulative number of OTP requests made by this email within the current calendar day.
    daily_key       = f"dtc:otp_daily:{email}"
    # Key enforcing 60-second cooldown period between successive OTP requests to prevent spamming.
    cooldown_key    = f"dtc:otp_cooldown:{email}"

    # In production/testing: enforce IP+email block, cooldown, abandonment tracking, and daily limit
    # Bypasses rate limiting and abuse controls only when development authentication mode is active.
    if not is_dev_mode:
        # Resolves remote client IP address from request headers or direct socket connection.
        client_ip   = _get_client_ip(request)
        # Key identifying whether this IP and email pair has been blocked due to failed attempts or abuse.
        block_key   = f"dtc:ip_block:{client_ip}:{_email_hash(email)}"
        # Key tracking unverified OTP generation requests to detect OTP request abandonment abuse.
        abandon_key = f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}"

        # 1. IP+email block check (wrong OTPs or abandonment abuse)
        # Initializes network block status flag for this IP address and email pair.
        is_blocked = False
        # Default block TTL in seconds corresponding to a full 24-hour lockout.
        ttl = 86400
        # Queries primary Redis database to check if IP+email block record exists.
        if client:
            # Wraps Redis block status lookup in an exception-handling block.
            try:
                # Checks whether an active block key is present in Redis for this IP and email.
                if client.exists(block_key):
                    # Sets blocked status flag to True upon finding active block key in Redis.
                    is_blocked = True
                    # Retrieves remaining lockout duration in seconds from Redis TTL.
                    ttl = max(0, client.ttl(block_key))
            # Handles errors encountered while reading block key from Redis.
            except Exception:
                pass
        # Falls back to local cache if Redis did not report an active block.
        # Evaluates fallback cache if Redis did not detect an active block.
        if not is_blocked:
            # Wraps fallback cache block check in an error-handling block.
            try:
                # Checks whether block record exists in local Django cache.
                # Checks whether local cache contains an active lockout record for this IP and email.
                if cache.get(block_key):
                    # Flags client as blocked if record is present in fallback cache.
                    is_blocked = True
            # Catches unexpected cache exceptions during fallback block verification.
            except Exception:
                pass

        # Rejects request immediately if client IP and email are under an active abuse block.
        if is_blocked:
            # Returns HTTP 429 Too Many Requests informing client of the 24-hour lockout.
            return Response(
                {
                    "error": "Your network has been temporarily blocked from requesting codes for this email due to repeated abuse. Try again in 24 hours.",
                    "code": "IP_EMAIL_BLOCKED",
                    "retry_after": ttl,
                },
                status=429,
            )

        # 2. Cooldown check: prevent re-send within 30 seconds
        # Initializes cooldown status flag to track whether client is requesting too soon.
        cooldown_active = False
        # Sets default cooldown duration to 30 seconds between successive code dispatches.
        cooldown_ttl = 30
        # Queries Redis to inspect remaining cooldown window for this email.
        if client:
            # Wraps Redis cooldown key lookup in an error-handling block.
            try:
                # Checks whether cooldown cache key currently exists in Redis.
                if client.exists(cooldown_key):
                    # Retrieves remaining TTL duration of the cooldown lock.
                    t = client.ttl(cooldown_key)
                    # Verifies that remaining cooldown time is strictly positive.
                    if t > 0:
                        # Marks cooldown flag active and updates TTL with live remaining seconds.
                        cooldown_active = True
                        cooldown_ttl = t
            # Catches Redis errors during cooldown status lookup.
            except Exception:
                # Continues to fallback cache check if Redis query encounters errors.
                pass
        # Evaluates fallback cache if Redis check did not indicate active cooldown.
        if not cooldown_active:
            # Protects local cache lookup with try-except block against memory errors.
            try:
                # Checks whether cooldown flag is present in local cache.
                if cache.get(cooldown_key):
                    # Marks cooldown active based on fallback cache entry.
                    cooldown_active = True
            # Intercepts any cache errors encountered during fallback cooldown lookup.
            except Exception:
                pass

        # Prevents OTP SMS/email flooding by returning a polite 30-second cooldown response while preserving the active OTP code.
        if cooldown_active:
            # Initializes daily attempt tally counter for chances remaining computation.
            daily_used = 0
            # Queries Redis to count total OTPs requested by this user today.
            if client:
                # Wraps Redis daily counter lookup in try-except block.
                try:
                    # Parses cumulative daily count integer from Redis cache key.
                    daily_used = int(client.get(daily_key) or 0)
                # Handles Redis read failures gracefully when fetching daily used counter.
                except Exception:
                    pass
            # Returns HTTP 200 response with remaining cooldown seconds and remaining chances.
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
        # Initializes daily quota lockout boolean status flag.
        daily_blocked = False
        # Checks primary Redis store to verify if daily quota block key is active.
        if client:
            # Protects daily block key query with exception-handling block.
            try:
                # Checks whether daily block record exists in Redis for this email.
                if client.exists(daily_block_key):
                    # Sets daily lockout status flag when Redis block key is found.
                    daily_blocked = True
            # Catches Redis communication failures during daily block status query.
            except Exception:
                pass
        # Inspects local Django cache if Redis did not report active daily lockout.
        if not daily_blocked:
            # Protects local cache daily block lookup with try-except block.
            try:
                # Detects whether daily block flag is recorded in local cache.
                if cache.get(daily_block_key):
                    # Flags user as daily blocked based on local cache entry.
                    daily_blocked = True
            except Exception:
                pass

        # Rejects OTP generation if user has crossed maximum daily request quota.
        if daily_blocked:
            # Returns HTTP 429 Too Many Requests informing client that daily quota has been exceeded.
            return Response(
                {
                    "error": "Daily OTP limit reached. You can request a new code tomorrow.",
                    "code": "DAILY_LIMIT_REACHED",
                },
                status=429,
            )

        # 4. Abandonment tracking: send without verify (5+ unverified sends = 24h block)
        # Checks primary Redis client to manage unverified request abandonment counters.
        if client:
            # Wraps abandonment counter increment and TTL management in try-except block.
            try:
                # Atomically increments consecutive unverified request counter for this IP and email.
                abandon_count = client.incr(abandon_key)
                # Sets rolling expiration window on initial request or if key is missing TTL.
                if abandon_count == 1 or client.ttl(abandon_key) == -1:
                    # Establishes a 2-hour rolling expiration TTL on the abandonment counter.
                    client.expire(abandon_key, 7200)  # 2-hour rolling window

                # Abandonment detector: flags and locks networks that repeatedly request OTPs without completing verification, defending against email quota exhaustion.
                if abandon_count > 5:
                    # Imposes a 24-hour lockout block key on this IP and email in Redis.
                    client.setex(block_key, 86400, "abandon")
                    # Clears the temporary abandonment counter once hard block has been set.
                    client.delete(abandon_key)
                    # Mirror-replicates the abandonment block to local cache for resilience.
                    try:
                        cache.set(block_key, "abandon", timeout=86400)
                    except Exception:
                        pass
                    # Returns HTTP 429 response informing client of the 24-hour abandonment block.
                    return Response(
                        {
                            "error": "Too many OTP requests without completing verification. Your network is blocked for 24 hours.",
                            "code": "IP_EMAIL_BLOCKED",
                            "retry_after": 86400,
                        },
                        status=429,
                    )
            # Catches Redis failures during unverified request abandonment tracking.
            except Exception:
                # Suppresses abandonment tracking exceptions to avoid disrupting user experience.
                pass

        # 5. Daily limit tracking (expires at midnight)
        # Checks Redis client availability to enforce midnight-expiring daily limit counters.
        if client:
            # Wraps daily quota computation and key expiration in try-except block.
            try:
                # Gets current UTC timestamp for accurate midnight cutoff calculation.
                now_dt = timezone.now()
                # Calculates the exact datetime boundary representing upcoming midnight.
                midnight = (now_dt + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                # Calculates remaining TTL in seconds until the midnight quota reset.
                ttl_until_midnight = max(1, int((midnight - now_dt).total_seconds()))

                # Atomically increments the daily request count for this email address.
                daily_count = client.incr(daily_key)
                # Ensures daily counter key expires at upcoming midnight.
                if daily_count == 1 or client.ttl(daily_key) == -1:
                    # Sets TTL on the daily counter to expire cleanly at midnight.
                    client.expire(daily_key, ttl_until_midnight)

                # Evaluates whether daily request volume has exceeded the limit of 10.
                if daily_count > 10:
                    # Establishes hard lockout in Redis lasting until midnight.
                    client.setex(daily_block_key, ttl_until_midnight, "blocked")
                    # Removes the daily counter key once lockout state is active.
                    client.delete(daily_key)
                    # Replicates daily lockout state into local fallback cache.
                    try:
                        cache.set(daily_block_key, "blocked", timeout=ttl_until_midnight)
                    except Exception:
                        pass
                    # Returns HTTP 429 response informing client that daily quota is exhausted.
                    return Response(
                        {
                            "error": "Daily OTP limit reached. You can request a new code tomorrow.",
                            "code": "DAILY_LIMIT_REACHED",
                        },
                        status=429,
                    )

                # Computes remaining allowed OTP requests for today.
                chances_remaining = max(0, 10 - int(daily_count))
            # Catches unexpected Redis errors during daily limit enforcement.
            except Exception:
                # Suppresses Redis errors to avoid blocking authentic user requests.
                pass

    # Generate cryptographically secure 6-digit numeric OTP [100000, 999999]
    # Generates a cryptographically strong 6-digit integer in range [100000, 999999] using OS entropy (secrets module) rather than pseudo-random numbers.
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    # Formats Redis cache key used to store active OTP code for this email.
    # Cache key identifying the pending OTP code stored for this email.
    cache_key = f"dtc:otp:{email}"

    # Stores generated OTP in Redis with 5-minute (300 seconds) expiration.
    if client:
        # Wraps Redis OTP storage and dev cache cleanup in an exception block.
        try:
            # Writes OTP code to Redis with 5-minute (300 seconds) time-to-live.
            client.setex(cache_key, 300, otp_code)
            # Purges residual rate limit and lockout keys in development environment.
            if is_dev_mode:
                # Iterates over leftover lockout and throttling keys to ensure smooth testing.
                for leftover_k in [
                    daily_block_key,
                    daily_key,
                    cooldown_key,
                    f"dtc:otp_session_blocked:{email}",
                ]:
                    # Deletes residual key from Redis for frictionless developer iterations.
                    client.delete(leftover_k)
        # Catches Redis errors during OTP storage.
        except Exception:
            # Continues gracefully to memory cache backup if Redis fails.
            pass

    # Redundant memory backup in case Redis is rebooting
    # Stores OTP in fallback in-memory cache to maintain availability if Redis restarts.
    try:
        # Writes OTP code to local Django cache with 300-second expiration.
        cache.set(cache_key, otp_code, timeout=300)
    # Catches unexpected local cache write failures.
    except Exception:
        # Suppresses memory cache failures so auth flow can still proceed.
        pass

    # Only dispatch real email if NOT running in development or test mode
    # Dispatches actual email delivery only in staging or production environments.
    if not is_dev_mode and not is_testing:
        # Spawns asynchronous daemon thread to transmit transactional OTP email via Resend.
        threading.Thread(target=_dispatch_otp_email, args=(email, otp_code), daemon=True).start()
    # Falls back to simulated local console logging when running in development or test modes.
    else:
        # Logs generated OTP code to console for instant developer convenience.
        logger.info(
            "[Local Auth] Simulated OTP generated for %s: %s (Infinite dev resends enabled)",
            email,
            otp_code,
        )

    # Set cooldown (30 seconds) in non-dev mode
    # Applies a 30-second request cooldown period for non-development environments.
    if not is_dev_mode:
        # Writes 30-second cooldown lock to Redis if connection is available.
        if client:
            # Wraps Redis cooldown key creation in try-except block.
            try:
                # Sets 30-second cooldown flag in Redis for this email.
                client.setex(cooldown_key, 30, "1")
            # Catches Redis errors during cooldown key write.
            except Exception:
                # Ignores Redis cooldown write errors and proceeds to fallback cache.
                pass
        # Mirrors 30-second cooldown into local memory cache as secondary defense.
        try:
            # Stores 30-second cooldown token in local Django cache.
            cache.set(cooldown_key, "1", timeout=30)
        # Catches local cache failures during cooldown write.
        except Exception:
            # Suppresses local cache errors to prevent blocking authentication response.
            pass

    # Derives deterministic UUID string identifier from normalized email address.
    user_id = _generate_user_id(email)

    # Constructs structured JSON response payload containing delivery status and cooldown info.
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
    # Injects plain-text OTP code directly into response when running in local development mode.
    if is_dev_mode:
        # Exposes generated OTP directly in development response for rapid testing.
        resp_data["otp"] = otp_code
        resp_data["is_local"] = True

    # Returns HTTP 200 OK with verification dispatch confirmation and metadata.
    return Response(resp_data, status=200)

# verified
# Restricts endpoint to handle HTTP POST requests only for OTP verification.
@api_view(["POST"])
# Validates submitted OTP, checks single-device login locks, and returns signed JWT token.
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
    # Extracts submitted OTP code from request payload and strips surrounding whitespace.
    submitted_otp = str(request.data.get("otp", "")).strip()

    # Validates that both email address and OTP code were provided in the request.
    if not email_raw or not submitted_otp:
        # Returns HTTP 400 Bad Request if email address or OTP code is missing.
        return Response({"error": "email and otp are required"}, status=400)

    email = _normalize_email(email_raw)
    # Guards against invalid email formatting after normalization.
    if not email:
        return Response({"error": "email and otp are required"}, status=400)

    cache_key = f"dtc:otp:{email}"

    is_dev_mode = _is_dev_auth_mode()
    client = _get_redis_client()

    # Single-Device Guard: Reject verification if an active session already exists for this email
    session_key = f"dtc:session:{email}"
    # Initializes flag tracking whether an active concurrent session was detected.
    session_active = False
    # Queries primary Redis database to check if user has an active session.
    if client:
        # Wraps Redis session lookup query in an error-handling block.
        try:
            # Checks whether an active session record exists in Redis for this email.
            if client.exists(session_key):
                # Marks session active flag to True when concurrent session record is found.
                session_active = True
        # Catches Redis communication failures during session presence lookup.
        except Exception:
            # Suppresses Redis errors during session lookup.
            pass
    # Falls back to local Django cache if Redis did not report an active session.
    if not session_active:
        # Wraps fallback cache session lookup in an error-handling block.
        try:
            # Inspects in-memory cache to detect active login session for this email.
            if cache.get(session_key):
                # Flags session as active when session key is found in fallback cache.
                session_active = True
        # Catches cache retrieval exceptions during fallback session lookup.
        except Exception:
            # Ignores cache errors and proceeds with login verification flow.
            pass

    # Enforces single-device login policy: immediately blocks new login attempts if an active session is currently alive on another device.
    if session_active:
        # Returns HTTP 409 Conflict rejecting concurrent login attempts on multiple devices.
        return Response(
            {
                "error": "Another device is currently logged in with this email.",
                "code": "CONCURRENT_LOGIN_BLOCKED",
                "message": "Another device is currently logged in with this email. Please sign out from that device first or wait for the session to expire.",
            },
            status=409,
        )

    # Resolves remote client IP address from request headers or direct socket connection.
    client_ip = _get_client_ip(request)
    # Key identifying whether this IP and email pair has been blocked due to failed attempts or abuse.
    block_key = f"dtc:ip_block:{client_ip}:{_email_hash(email)}"
    # Key tracking failed OTP attempts for this specific client IP and email pair.
    fail_key = f"dtc:ip_fail:{client_ip}:{_email_hash(email)}"
    # Key identifying unverified request abandonment records for this IP and email.
    abandon_key = f"dtc:ip_abandon:{client_ip}:{_email_hash(email)}"

    # In production: check IP+email block before allowing verify attempt
    # Enforces lockout verification check in production or non-dev environments.
    if not is_dev_mode:
        # Initializes boolean flag tracking whether client is under active lockout.
        is_blocked = False
        # Default lockout duration of 24 hours (86400 seconds) for IP+email blocks.
        ttl = 86400
        # Checks primary Redis database for active lockout block record.
        if client:
            # Wraps Redis lockout existence check in an exception handler.
            try:
                # Queries Redis to see if IP+email block key exists.
                if client.exists(block_key):
                    # Flags client as blocked upon finding active lockout key in Redis.
                    is_blocked = True
                    # Fetches remaining lockout TTL in seconds from Redis.
                    ttl = max(0, client.ttl(block_key))
            # Handles errors encountered during Redis block key inspection.
            except Exception:
                # Continues to fallback cache check if Redis query encounters errors.
                pass
        if not is_blocked:
            try:
                if cache.get(block_key):
                    # Sets blocked status flag to True based on local cache entry.
                    is_blocked = True
            # Catches local cache retrieval exceptions during block verification.
            except Exception:
                # Ignores cache errors and proceeds with verification.
                pass

        # Halts verification immediately if network or account is currently locked out.
        if is_blocked:
            # Returns HTTP 429 Too Many Requests informing user of active lockout and remaining duration.
            return Response(
                {
                    "error": "Your network has been temporarily blocked from accessing this email. Try again in 24 hours.",
                    "code": "IP_EMAIL_BLOCKED",
                    "retry_after": ttl,
                },
                status=429,
            )

    # Initializes variable holding retrieved cached OTP secret.
    stored_otp = None
    # Queries Redis to retrieve active OTP code for verification.
    if client:
        # Wraps primary Redis lookup in try-except block to safely fetch cached OTP.
        try:
            # Retrieves active OTP code from Redis using the email cache key.
            stored_otp = client.get(cache_key)
        # Handles Redis communication errors during OTP retrieval.
        except Exception:
            # Falls back to local Django cache on Redis connection failure.
            pass

    # Falls back to local in-memory cache if Redis returned no OTP record.
    if not stored_otp:
        # Wraps local cache OTP retrieval in an error-handling block.
        try:
            # Retrieves OTP secret from local memory cache.
            stored_otp = cache.get(cache_key)
        # Catches local cache read errors.
        except Exception:
            # Ignores local cache errors and continues to match evaluation.
            pass

    # Constant-time comparison to prevent timing attacks
    # Decodes binary Redis value to string or defaults to empty string.
    stored_str = stored_otp.decode("utf-8") if isinstance(stored_otp, bytes) else str(stored_otp or "")
    # Performs constant-time comparison (hmac.compare_digest) between submitted OTP and cached secret to eliminate timing attack vectors.
    otp_matches = bool(stored_otp) and hmac.compare_digest(stored_str.strip(), submitted_otp)

    # Handles invalid, expired, or non-matching OTP code submissions.
    if not otp_matches:
        # Dev mode: unlimited attempts, no tracking
        # Bypasses strike tracking when running under local development mode.
        if is_dev_mode:
            # Returns HTTP 400 Bad Request error indicating invalid OTP code in dev mode.
            return Response(
                {
                    "error": "Invalid or expired verification code. (Infinite attempts allowed in local dev)",
                    "code": "INVALID_OTP",
                    "attempts_remaining": "∞",
                },
                status=400,
            )

        # Production: track wrong attempts per IP+email — 5 strikes = 24h block
        # Initializes failed attempt strike counter.
        fail_count = None
        # Tracks failed verification attempt count in Redis.
        if client:
            # Wraps failed attempt increment in try-except block.
            try:
                # Atomically increments failed OTP submission strike counter.
                fail_count = client.incr(fail_key)
                # Establishes 24-hour expiration window on first failed attempt or missing TTL.
                if fail_count == 1 or client.ttl(fail_key) == -1:
                    # Sets 24-hour expiration on the failed attempts tracking key.
                    client.expire(fail_key, 86400)  # 24-hour window
            # Handles Redis increment errors gracefully.
            except Exception:
                fail_count = None

        # Falls back to local memory cache if Redis was unavailable or failed.
        if fail_count is None:
            # Protects local cache counter updates against transient cache errors.
            try:
                # Retrieves existing count from local cache and increments by one.
                fail_count = (cache.get(fail_key) or 0) + 1
                # Saves updated fail count in cache with a 24-hour expiration.
                cache.set(fail_key, fail_count, timeout=86400)
            # Catches unexpected local cache failures during count updates.
            except Exception:
                # Assumes at least one failed strike if cache write fails.
                fail_count = 1

        # Brute-force shield: locks the IP and email combination for 24 hours upon 5 consecutive incorrect OTP submissions.
        if fail_count >= 5:
            # Records lockout in Redis when available.
            if client:
                # Wraps Redis lockout writes in try-except block.
                try:
                    # Locks IP-email tuple in Redis for 24 hours with otp_fail tag.
                    client.setex(block_key, 86400, "otp_fail")
                    # Cleans up failed attempts tracking counter after enforcing lock.
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
    # Enforces single-use semantics: immediately evicts the verified OTP from Redis and fallback cache upon successful authentication.
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

    # Generates a unique 128-bit session token bound to both email and user ID in Redis with a rolling 30-second TTL.
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

        # Detects zombie tokens: rejects previously valid JWTs if another device has superseded the session, returning 401 SESSION_SUPERSEDED.
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
            # Extends active session lease in Redis by 30 seconds to maintain single-device continuity as long as the client tab remains open.
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
