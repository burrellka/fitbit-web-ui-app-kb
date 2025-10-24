# 🚀 Quick Start - Secure Deployment

## What Changed?

Your dashboard is now split into **TWO separate ports** for maximum security:

| Port | Purpose | Access |
|------|---------|--------|
| **5032** | OAuth callback ONLY | Public (via nginx) |
| **5033** | Full dashboard + data | Internal + Password protected |

---

## ⚡ Quick Setup (5 Minutes)

### Step 1: Add New Environment Variables

Add these to your `.env` file:

```bash
# Existing (keep these)
CLIENT_ID=23TG7K
CLIENT_SECRET=your_secret
REDIRECT_URL=https://fitbitkb.burrellstribedns.org/

# NEW: Add these 3 lines
DASHBOARD_PASSWORD=YourStrongPassword123!
SECRET_KEY=run_command_below_to_generate
DASHBOARD_URL=http://192.168.13.5:5033/
```

**Generate SECRET_KEY:**
```bash
python3 -c "import os; print(os.urandom(32).hex())"
```

---

### Step 2: Update Docker Compose in Dockge

Replace your current compose file with:

```yaml
# Production Docker Compose for Homelab - Dual Port Security Setup

services:
  fitbit-ui-enhanced:
    image: brain40/fitbit-wellness-enhanced:latest
    container_name: fitbit-report-app-enhanced
    ports:
      - "5032:5032"  # OAuth callback (public)
      - "5033:5033"  # Dashboard (internal only)
    restart: unless-stopped
    environment:
      - CLIENT_ID=${CLIENT_ID}
      - CLIENT_SECRET=${CLIENT_SECRET}
      - REDIRECT_URL=${REDIRECT_URL}
      - DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}
      - SECRET_KEY=${SECRET_KEY}
      - DASHBOARD_URL=${DASHBOARD_URL:-http://192.168.13.5:5033/}
    volumes:
      - ./data_cache.db:/app/data_cache.db
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5032/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

---

### Step 3: Update Nginx Config

Your nginx config for `fitbitkb.burrellstribedns.org` should now proxy to **port 5032** (instead of 5032):

```nginx
server {
    listen 443 ssl http2;
    server_name fitbitkb.burrellstribedns.org;
    
    location / {
        proxy_pass http://192.168.13.5:5032;  # Changed from :5032
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Reload nginx:**
```bash
sudo nginx -t && sudo nginx -s reload
```

---

### Step 4: Pull & Restart in Dockge

1. Click **"Pull"** to get the latest image
2. Click **"Restart"** to apply changes
3. Wait 30 seconds for services to start

---

### Step 5: Test Access

#### Test Public OAuth (Should Work)
Navigate to: `https://fitbitkb.burrellstribedns.org/`

**Expected**: Minimal "OAuth Callback Endpoint" page (no health data)

#### Test Dashboard (Password Required)
Navigate to: `http://192.168.13.5:5033/`

**Expected**: Login page asking for password

Enter your `DASHBOARD_PASSWORD` and you should see your full dashboard!

---

## 🔐 How It Works

### The OAuth Flow:

1. You click "Login to FitBit" → Goes to Fitbit
2. Fitbit redirects back to `https://fitbitkb.burrellstribedns.org/?code=...` (port 5032)
3. Port 5032 automatically forwards you to `http://192.168.13.5:5033/` (your dashboard)
4. Dashboard is already logged in from your session!

### Security Benefits:

✅ **No health data** ever visible on public port  
✅ **Password protection** on dashboard  
✅ **Port 5033** never exposed to internet  
✅ **Session-based auth** with secure cookies  

---

## 📱 Access URLs

| What | URL | Notes |
|------|-----|-------|
| **Your Dashboard** | `http://192.168.13.5:5033/` | Internal only, password required |
| **OAuth Callback** | `https://fitbitkb.burrellstribedns.org/` | Public, no data exposed |
| **Logout** | `http://192.168.13.5:5033/logout` | Clear session |

---

## ⚙️ Kady-Ann's Setup (Your Wife)

Same process, but use different ports and credentials:

**docker-compose-wife.yml**
```yaml
services:
  fitbit-ui-enhanced-wife:
    image: brain40/fitbit-wellness-enhanced:latest
    container_name: fitbit-report-app-kady-ann
    ports:
      - "5034:5032"  # OAuth callback (public)
      - "5035:5033"  # Dashboard (internal)
    restart: unless-stopped
    environment:
      - CLIENT_ID=${WIFE_CLIENT_ID}
      - CLIENT_SECRET=${WIFE_CLIENT_SECRET}
      - REDIRECT_URL=https://fitbitkcsb.burrellstribedns.org/
      - DASHBOARD_PASSWORD=${WIFE_DASHBOARD_PASSWORD}
      - SECRET_KEY=${WIFE_SECRET_KEY}
      - DASHBOARD_URL=http://192.168.13.5:5035/
    volumes:
      - ./data_cache_wife.db:/app/data_cache.db
```

**Her URLs:**
- Dashboard: `http://192.168.13.5:5035/`
- OAuth: `https://fitbitkcsb.burrellstribedns.org/`

---

## 🆘 Troubleshooting

### Can't Access Port 5033
**Check if container is running:**
```bash
docker ps | grep fitbit
netstat -tuln | grep 5033
```

### OAuth Redirect Loops
**Check environment variable:**
```bash
docker exec fitbit-report-app-enhanced env | grep DASHBOARD_URL
```
Should be: `http://192.168.13.5:5033/`

### Wrong Password
**Update `.env` and restart:**
```bash
# Edit .env file with new password
docker restart fitbit-report-app-enhanced
```

### View Logs
```bash
docker logs -f fitbit-report-app-enhanced
```

---

## 🎉 You're Done!

- ✅ OAuth works via public URL
- ✅ Dashboard is password-protected
- ✅ Health data never exposed to internet
- ✅ Cache status display at bottom
- ✅ Defense in depth security

**Access your dashboard:** `http://192.168.13.5:5033/`

For detailed security documentation, see `SECURITY_SETUP.md`.

