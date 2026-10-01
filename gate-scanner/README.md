# DTC Staff Gate Scanner

A dedicated, isolated web application for venue gate staff and turnstiles to validate and redeem fan e-tickets in real-time.

## Directory Structure

```text
gate-scanner/
├── index.html               # Staff scanner entry point (hosted at /)
├── gate.html                # Alias entry point (hosted at /gate.html)
├── gate.js                  # Scanner logic, camera control & redemption engine
├── gate.css                 # Staff scanner UI styles
├── theme.css                # DTC design tokens and themes
├── favicon.svg              # DTC badge icon
├── vendor/
│   └── html5-qrcode.min.js  # Vendored offline camera & QR decoder (v2.3.8)
└── README.md                # Deployment documentation
```

---

## Deploying as a Separate Service on Vercel

To keep the staff turnstile scanner strictly separated from the public client booking app (`visualizer/`):

1. **Create New Vercel Project:**
   - In your [Vercel Dashboard](https://vercel.com/dashboard), click **"Add New..."** -> **"Project"**.
   - Select the `Distributed-Transaction-Coordinator` GitHub repository.

2. **Configure Root Directory:**
   - Under **Project Settings** -> **Root Directory**, click **Edit**.
   - Select or type: `gate-scanner`.
   - Leave build command blank (it is static HTML/JS/CSS).

3. **Deploy:**
   - Click **Deploy**.
   - Vercel will assign a dedicated URL (e.g., `https://dtc-gate-scanner.vercel.app/`).

4. **Update CORS in Coordinator:**
   - In Railway -> **Coordinator Service** -> **Variables**, add your new Vercel URL to `CORS_ALLOWED_ORIGINS`:
     ```env
     CORS_ALLOWED_ORIGINS="https://distributed-transaction-coordinator.vercel.app,https://dtc-gate-scanner.vercel.app,https://reverse-proxy.nexoflow.workers.dev"
     ```

---

## Local Development

To run the gate scanner locally on a separate port from the public visualizer (port 3000):

```bash
python -m http.server 3001 --directory gate-scanner
```

Open: `http://localhost:3001/` or `http://localhost:3001/gate.html`
Gate Key: `gate-secret-key-change-in-production`
