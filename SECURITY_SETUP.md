# 🔐 Security Setup Guide - Dual Port Architecture

## Overview

This deployment uses a **defense-in-depth** security model with **two separate ports**:

- **Port 5032**: OAuth callback endpoint (public, minimal exposure)
- **Port 5033**: Full dashboard (password-protected, internal only)

## Architecture Diagram

```
Internet
   │
   ├─[Cloudflare]──> [Nginx]──> Port 5032 (OAuth Only - No Health Data)
   │                                │
   │                                └──> Redirects to Port 5033
   │
[Local Network Only]─────────────> Port 5033 (Full Dashboard + Password)
```

## Security Features

✅ **Port Separation**: Health data never exposed on public port  
✅ **Password Protection**: Dashboard requires authentication  
✅ **Session Management**: Secure Flask sessions  
✅ **OAuth Bypass**: Fitbit callbacks work without password  
✅ **Network Isolation**: Dashboard port not exposed to internet

---

## Step 1: Update Environment Variables

Add these new variables to your `.env` file:

```bash
# Existing variables
CLIENT_ID=your_fitbit_client_id
CLIENT_SECRET=your_fitbit_client_secret
REDIRECT_URL=https://fitbitkb.burrellstribedns.org/

# NEW: Dashboard security
DASHBOARD_PASSWORD=YourStrongPasswordHere123!
SECRET_KEY=your_random_secret_key_here_at_least_32_chars
DASHBOARD_URL=http://192.168.13.5:5033/
```

### Generate a Strong Secret Key

```bash
python3 -c "import os; print(os.urandom(32).hex())"
```

---

## Step 2: Update Nginx Configuration

### For the PUBLIC OAuth Endpoint (Port 5032)

This is the ONLY port that should be exposed to the internet:

```nginx
server {
    listen 443 ssl http2;
    server_name fitbitkb.burrellstribedns.org;
    
    # SSL configuration (Cloudflare)
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://192.168.13.5:5032;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### For the INTERNAL Dashboard (Port 5033)

**Option A: No Nginx Proxy (Most Secure)**
Access directly via `http://192.168.13.5:5033/` - Only accessible on your local network.

**Option B: Nginx with IP Whitelist (If You Need a Domain)**
```nginx
server {
    listen 443 ssl http2;
    server_name fitbit-dashboard.local;  # Internal domain
    
    # IP whitelist - ONLY your local network
    allow 192.168.13.0/24;
    deny all;
    
    location / {
        proxy_pass http://192.168.13.5:5033;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Step 3: Deploy with Docker Compose

1. **Pull the latest image:**
```bash
docker pull brain40/fitbit-wellness-enhanced:latest
```

2. **Update your docker-compose:**
Use the new `docker-compose-homelab.yml` which exposes both ports.

3. **Deploy:**
```bash
docker-compose -f docker-compose-homelab.yml up -d
```

4. **Verify both services are running:**
```bash
# Check OAuth callback (should return HTML)
curl http://192.168.13.5:5032/

# Check dashboard (should redirect to login)
curl -L http://192.168.13.5:5033/
```

---

## Step 4: Test the Security

### Test 1: OAuth Callback (Public Port)
1. Navigate to `https://fitbitkb.burrellstribedns.org/`
2. Should see: "Fitbit OAuth Callback Endpoint" page
3. **Verify**: No health data visible, no dashboard accessible

### Test 2: Dashboard Login (Internal Port)
1. Navigate to `http://192.168.13.5:5033/`
2. Should see: Password login page
3. Enter your `DASHBOARD_PASSWORD`
4. Should be redirected to full dashboard

### Test 3: Fitbit OAuth Flow
1. Click "Login to FitBit" on dashboard
2. Authorize on Fitbit
3. Redirected to `https://fitbitkb.burrellstribedns.org/?code=...`
4. Automatically forwarded to `http://192.168.13.5:5033/`
5. Dashboard loads with your data

### Test 4: External Access Prevention
From a device **outside** your network:
1. Try accessing `http://YOUR_PUBLIC_IP:5033/`
2. Should **timeout** or **connection refused** ✅

---

## Access Methods

### For You (Internal Network)
- **Dashboard**: `http://192.168.13.5:5033/`
- **OAuth Login**: Uses public URL, redirects to internal dashboard

### For Fitbit OAuth
- **Callback**: `https://fitbitkb.burrellstribedns.org/` (public)
- **No data exposed** on this endpoint

---

## Firewall Rules (Optional but Recommended)

### TrueNAS/Firewall Configuration

Block external access to port 5033:

```bash
# Allow port 5032 (OAuth) from anywhere
iptables -A INPUT -p tcp --dport 5032 -j ACCEPT

# Allow port 5033 (Dashboard) ONLY from local network
iptables -A INPUT -p tcp --dport 5033 -s 192.168.13.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 5033 -j DROP
```

---

## Password Management

### Change Dashboard Password
1. Update `DASHBOARD_PASSWORD` in `.env`
2. Restart container:
```bash
docker-compose -f docker-compose-homelab.yml restart
```

### Logout
Navigate to: `http://192.168.13.5:5033/logout`

---

## Setup for Your Wife (Kady-Ann)

Follow the same process with separate credentials:

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

**Her Access URLs:**
- OAuth: `https://fitbitkcsb.burrellstribedns.org/`
- Dashboard: `http://192.168.13.5:5035/`

---

## Troubleshooting

### Can't Access Dashboard
**Symptom**: "Connection refused" on port 5033  
**Solution**: Check if container is running on correct ports:
```bash
docker ps
netstat -tuln | grep 5033
```

### OAuth Redirect Not Working
**Symptom**: Stuck on OAuth callback page  
**Solution**: Check `DASHBOARD_URL` in environment variables matches your internal IP.

### Password Not Working
**Symptom**: "Incorrect password" even with correct password  
**Solution**: 
1. Check for extra spaces in `.env` file
2. Verify env variable loaded: `docker exec fitbit-report-app-enhanced env | grep DASHBOARD_PASSWORD`

### Forgot Password
**Solution**: Update `.env` and restart container.

---

## Logs

View logs for both services:

```bash
# OAuth callback logs
docker exec fitbit-report-app-enhanced tail -f /var/log/supervisor/oauth-callback.out.log

# Dashboard logs
docker exec fitbit-report-app-enhanced tail -f /var/log/supervisor/dashboard.out.log

# All supervisor logs
docker logs -f fitbit-report-app-enhanced
```

---

## Security Best Practices

✅ Use a strong, unique password (20+ characters)  
✅ Never expose port 5033 to the internet  
✅ Use HTTPS for the OAuth callback (port 5032)  
✅ Keep `SECRET_KEY` and passwords in `.env` (never commit to git)  
✅ Regularly update the Docker image  
✅ Enable Cloudflare DDoS protection  
✅ Monitor failed login attempts in logs

---

## Questions?

- GitHub Issues: https://github.com/burrellka/fitbit-web-ui-app-kb/issues
- Documentation: See README.md and other guides in the repo

