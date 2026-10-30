# Security Review

This report records the confirmed findings from an authorized, read-only
security assessment of the repository. No exploitation or data exfiltration
was performed.

## Findings

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🟠 HIGH | [docker-compose.yml](./docker-compose.yml) | 25-38, 63-66 | PostgreSQL and Redis are published on all host interfaces; PostgreSQL defaults to `postgres`/`postgres`, and Redis has no authentication. | 10/10 |
| 2 | 🔴 CRITICAL | [coordinator-service/coordinator/auth_views.py](./coordinator-service/coordinator/auth_views.py) | 912-916, 1003, 1067-1082 | OTP verification trusts Redis state that is unauthenticated and externally reachable in the supplied deployment, enabling account takeover. | 10/10 |
| 3 | 🟠 HIGH | [coordinator-service/coordinator_service/settings.py](./coordinator-service/coordinator_service/settings.py), [coordinator-service/coordinator/ticket_views.py](./coordinator-service/coordinator/ticket_views.py) | 337; 390 | Gate redemption falls back to the public fixed key `gate-secret-key-change-in-production` when `GATE_API_KEY` is unset, including with `DEBUG=False`. | 9/10 |

## Detailed Findings

### 1. Exposed unauthenticated Redis and weakly credentialed PostgreSQL

**Affected locations:** `docker-compose.yml:25-38, 63-66`

**Reproduction:**

1. Deploy the supplied compose configuration.
2. Reach the host's published PostgreSQL or Redis ports from a network that
   can access the host.
3. Connect to Redis without credentials, or authenticate to PostgreSQL using
   the default `postgres`/`postgres` credentials.
4. Read or modify application state.

**Impact:** An attacker can tamper with payment, inventory, coordinator, OTP,
session, and Celery state, compromising data confidentiality and integrity and
potentially affecting availability.

**Remediation:** Remove public host port publishing where it is unnecessary,
bind administrative services to loopback or private networks, require strong
PostgreSQL credentials, and configure Redis authentication, TLS, and network
ACLs.

### 2. Redis compromise enables OTP account takeover

**Affected locations:** `coordinator-service/coordinator/auth_views.py:912-916,
1003, 1067-1082`

**Reproduction:**

1. Set `dtc:otp:<normalized-victim-email>` in Redis to an attacker-chosen
   six-digit value.
2. Submit that value and the victim's email to `/auth/verify-otp/`.
3. Receive the signed JWT issued by the verification endpoint.
4. Use the token to access the victim's authenticated operations.

**Impact:** Account takeover and unauthorized access to the victim's
transactions, tickets, and other authenticated operations.

**Remediation:** Isolate Redis and require authentication and TLS. Bind OTP
verification to server-issued challenge state, enforce strict cache access
controls, and ensure OTP verification cannot be satisfied by attacker-written
cache state.

### 3. Public fallback gate API key

**Affected locations:** `coordinator-service/coordinator_service/settings.py:337`
and `coordinator-service/coordinator/ticket_views.py:390`

**Reproduction:**

1. Run the coordinator with `DEBUG=False` and without `GATE_API_KEY`.
2. Obtain a valid ticket code.
3. Send `X-Gate-Api-Key:
   gate-secret-key-change-in-production` to `/gate/redeem/`.
4. The endpoint accepts the fallback key and redeems the ticket.

**Impact:** Anyone who obtains a ticket code can perform unauthorized gate
redemption, causing unauthorized admission and ticket state changes.

**Remediation:** Fail closed at startup when `GATE_API_KEY` is absent or weak,
never use a production fallback secret, rotate exposed deployments, and
restrict redemption to a private gate network.

## Scope and limitations

The findings above are confirmed from repository code and deployment
configuration. Runtime behavior, network exposure in a particular deployment,
and controls outside this repository were not independently verified.
