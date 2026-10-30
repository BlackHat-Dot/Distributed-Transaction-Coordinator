# Security Review

This report records the confirmed findings from an authorized, read-only
security assessment of the repository. No exploitation or data exfiltration
was performed.

## Findings

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | ⚪ LOW | [docker-compose.yml](./docker-compose.yml) | 25-38, 63-66 | The local compose setup publishes PostgreSQL and Redis and uses development-friendly defaults. This is a deployment-hardening concern, not proof of internet exposure. | 10/10 |
| 2 | 🟠 HIGH (conditional) | [coordinator-service/coordinator/auth_views.py](./coordinator-service/coordinator/auth_views.py) | 912-916, 1003, 1067-1082 | If an attacker can write the Redis instance used by the coordinator, OTP verification accepts the attacker-written value and can issue a victim JWT. The repository does not show that an external attacker can obtain Redis access in a normal local deployment. | 10/10 |
| 3 | 🟠 HIGH | [coordinator-service/coordinator_service/settings.py](./coordinator-service/coordinator_service/settings.py), [coordinator-service/coordinator/ticket_views.py](./coordinator-service/coordinator/ticket_views.py) | 337; 390 | Gate redemption falls back to the public fixed key `gate-secret-key-change-in-production` when `GATE_API_KEY` is unset, including with `DEBUG=False`. | 9/10 |

## Detailed Findings

### 1. Development-oriented database service exposure

**Affected locations:** `docker-compose.yml:25-38, 63-66`

**Configuration evidence:**

The compose file publishes PostgreSQL and Redis ports and supplies development
defaults. Docker's host binding can make those ports reachable from networks
allowed by the host firewall, but this file alone does not establish internet
reachability or identify an attacker who can access them.

**Impact if deployed on an exposed or shared network:** An attacker with
network access could tamper with payment, inventory, coordinator, OTP, session,
and Celery state.

**Remediation:** For local use, document the intended trust boundary and rely
on host firewall controls. For shared or production deployments, remove public
host port publishing where unnecessary, bind services to private networks,
require strong PostgreSQL credentials, and configure Redis authentication, TLS,
and network ACLs.

### 2. Redis compromise enables OTP account takeover (conditional)

**Affected locations:** `coordinator-service/coordinator/auth_views.py:912-916,
1003, 1067-1082`

**Reproduction if Redis access is already obtained:**

1. Set `dtc:otp:<normalized-victim-email>` in Redis to an attacker-chosen
   six-digit value.
2. Submit that value and the victim's email to `/auth/verify-otp/`.
3. Receive the signed JWT issued by the verification endpoint.
4. Use the token to access the victim's authenticated operations.

**Impact:** In a deployment where an attacker can write the coordinator's Redis
instance, account takeover and unauthorized access to the victim's
transactions, tickets, and other authenticated operations are possible. This
is not an independent remote authentication bypass: the prerequisite Redis
write access is not established by `auth_views.py`.

**Remediation:** Isolate Redis and require authentication and TLS in shared
deployments. Treat Redis as trusted server-side state and enforce strict cache
access controls. Additional OTP challenge binding is defense in depth, not a
replacement for securing Redis.

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

The OTP data flow and compose defaults are confirmed from repository code and
configuration. External exploitability depends on deployment network exposure,
host firewall rules, and controls outside this repository; those were not
independently verified. The report therefore distinguishes the conditional OTP
impact from the local compose configuration.
