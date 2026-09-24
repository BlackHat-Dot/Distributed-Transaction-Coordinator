# DTC System Threat Model (STRIDE & Architectural Analysis)

## 1. System Architecture & Trust Boundaries

```
[ Unauthenticated / Authenticated Public Clients ]
                    │
════════════════════╪═══════════════════════════════════════════════════════════
 TRUST BOUNDARY 1   │ Public Ingress (Port 8002: Coordinator, Port 8000: Inventory Seats)
════════════════════╪═══════════════════════════════════════════════════════════
                    ▼
       ┌─────────────────────────┐
       │   Coordinator Service   │
       └────────────┬────────────┘
                    │
════════════════════╪═══════════════════════════════════════════════════════════
 TRUST BOUNDARY 2   │ Internal Private Inter-Service Network (Shared Internal Secret)
════════════════════╪═══════════════════════════════════════════════════════════
                    ├─────────────────────────┐
                    ▼                         ▼
       ┌─────────────────────────┐ ┌─────────────────────────┐
       │    Inventory Service    │ │     Payment Service     │
       └────────────┬────────────┘ └────────────┬────────────┘
                    │                           │
════════════════════╪═══════════════════════════╪═══════════════════════════════
 TRUST BOUNDARY 3   │ Storage & Asynchronous Infrastructure
════════════════════╪═══════════════════════════╪═══════════════════════════════
                    ▼                           ▼
        PostgreSQL (3 Databases)        Redis 7 (Broker & Lock)
```

---

## 2. Threat Actors & Capabilities

| Threat Actor | Initial Access / Capabilities | Primary Objectives |
| :--- | :--- | :--- |
| **A1: Anonymous Attacker** | Network access to public endpoints (port 8002, 8000, 8001). No credentials. | Bypass authentication, exhaust OTP quotas, access exposed internal APIs, cause denial of service. |
| **A2: Authenticated Malicious User** | Possesses a valid JWT token obtained via standard Gmail OTP verification. | Exploit IDOR/BOLA to cancel other users' bookings, trigger unauthorized compensation, bypass hold quotas. |
| **A3: Network Insider / Lateral Movement** | Compromised adjacent container or unauthenticated access to Docker network. | Directly manipulate payment records or seat availability without going through Coordinator Saga checks. |
| **A4: Stalled Worker / Network Partition** | Celery worker crash or dropped socket between services mid-saga. | Creates stranded state: customer charged without seats or hold held indefinitely without payment. |

---

## 3. STRIDE Threat Mapping

| STRIDE Category | Component Affected | Threat Description | Severity | Finding Ref |
| :--- | :--- | :--- | :---: | :--- |
| **Spoofing** | Coordinator OTP | Client IP spoofing via `X-Forwarded-For` header allows bypassing rate-limiting and IP blocklists. | High | `SEC-05` |
| **Tampering** | Coordinator Saga | `_compensate()` skips refund when seat release fails, leaving payment in charged state while releasing inventory. | Critical | `REL-01` |
| **Repudiation** | Payments API | Payment service lacks internal key validation; direct host calls bypass Coordinator audit log. | High | `SEC-03` |
| **Information Disclosure** | Secret Checking | Non-constant time string comparisons (`==`, `!=`) on `STAFF_API_KEY` and `INTERNAL_SERVICE_KEY` leak secrets via timing. | Medium | `SEC-04` |
| **Denial of Service** | Recovery Endpoint | `POST /recovery/sweep/` is exposed to all authenticated non-staff users, allowing DB and network exhaustion. | High | `SEC-02` |
| **Elevation of Privilege** | Retry Compensation | `POST /transactions/<id>/retry-compensation/` lacks object-level ownership check (BOLA/IDOR). | Critical | `SEC-01` |

---

## 4. Critical Assets & High-Value Targets

1. **Financial Integrity**: Ensuring money is never charged without seats, and never stranded when transactions abort.
2. **Seat Lease Consistency**: Guaranteeing atomic hold allocations without overselling or exceeding maximum hold caps (`MAX_USER_HOLDS`).
3. **Internal Key & Token Privacy**: Protecting `INTERNAL_SERVICE_KEY`, `STAFF_API_KEY`, and `SECRET_KEY` from unauthorized callers.
