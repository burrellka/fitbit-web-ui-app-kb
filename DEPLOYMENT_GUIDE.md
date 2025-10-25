# Fitbit Wellness App - Deployment Guide

## 🚀 Quick Deploy (Docker Compose)

### 1. **Prerequisites**
- Docker and Docker Compose installed
- Fitbit Developer App configured (see [GET_ACCESS_TOKEN.md](help/GET_ACCESS_TOKEN.md))
- Static IP or domain name for your server

### 2. **Clone Repository**
```bash
git clone https://github.com/burrellka/fitbit-web-ui-app-kb.git
cd fitbit-web-ui-app-kb
```

### 3. **Configure Environment**

Create or edit `docker-compose.yml`:

```yaml
services:
  fitbit-ui-enhanced:
    image: brain40/fitbit-wellness-enhanced:latest
    container_name: fitbit-report-app-enhanced
    ports:
      - 5032:5032  # OAuth callback (public)
      - 5033:5033  # Dashboard (internal only)
    restart: unless-stopped
    environment:
      - CLIENT_ID=YOUR_FITBIT_CLIENT_ID
      - CLIENT_SECRET=YOUR_FITBIT_CLIENT_SECRET
      - REDIRECT_URL=https://your-domain.com/
      - DASHBOARD_PASSWORD=your_secure_password
      - SECRET_KEY=your_random_secret_key_here
      - DASHBOARD_URL=http://192.168.x.x:5033/
      - TZ=America/New_York  # ← IMPORTANT: Set your timezone!
    volumes:
      - ./fitbit_cache:/app  # Persist cache across restarts
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5032/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

### 4. **Timezone Configuration** ⏰

**CRITICAL**: Set the `TZ` environment variable to your local timezone to ensure accurate timestamps in logs and cache status.

**Common Timezones:**
| Region | Timezone String |
|--------|----------------|
| US Eastern | `America/New_York` |
| US Central | `America/Chicago` |
| US Mountain | `America/Denver` |
| US Pacific | `America/Los_Angeles` |
| UK | `Europe/London` |
| Central Europe | `Europe/Paris` |
| Japan | `Asia/Tokyo` |
| Australia (Sydney) | `Australia/Sydney` |

**Without this setting**, the container defaults to UTC, causing:
- Log timestamps 4-5 hours off from your local time
- Cache status showing incorrect "Last Run" times
- Confusion when debugging hourly cycles

### 5. **Start the Application**
```bash
docker-compose up -d
```

### 6. **Verify Deployment**
```bash
# Check container status
docker ps

# Watch logs
docker logs -f fitbit-report-app-enhanced

# Check health
curl http://localhost:5032/api/health
```

---

## 🔐 Security Setup (Dual-Port Architecture)

This app uses **two ports** for maximum security:

### **Port 5032: OAuth Callback (Public)**
- **Purpose**: Handles Fitbit OAuth redirects only
- **Exposure**: Should be publicly accessible (via reverse proxy)
- **Security**: No health data exposed, only authentication flow
- **Health Check**: `http://localhost:5032/health`

### **Port 5033: Dashboard (Internal Only)**
- **Purpose**: Full dashboard with health data
- **Exposure**: Should remain internal (localhost/LAN only)
- **Security**: Password-protected, requires login
- **Access**: `http://192.168.x.x:5033/` (internal IP only)

### **Nginx Reverse Proxy (Recommended)**

```nginx
# /etc/nginx/sites-available/fitbit
server {
    listen 443 ssl http2;
    server_name fitbitkb.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # ONLY expose port 5032 (OAuth callback)
    location / {
        proxy_pass http://localhost:5032;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Port 5033 (Dashboard) should NOT be exposed publicly
# Access it directly via internal IP: http://192.168.x.x:5033/
```

---

## 💾 Cache Management

### **How the Cache Works**

1. **Auto-Launch on Login**: Cache builder starts automatically when you log in
2. **Hourly Cycles**: Runs every hour, using ~125 API calls per cycle
3. **3-Phase Strategy**:
   - **Phase 1**: Fetches 365 days of range-based metrics (9 calls)
   - **Phase 2**: Fetches Cardio Fitness in 30-day blocks
   - **Phase 3**: Fetches Sleep/HRV/BR/Temp in 7-day blocks (28 calls per block)
4. **Loops** between Phase 2 & 3 until API budget exhausted or all data cached

### **Cache Status Display**

The dashboard shows real-time cache status:
- **Empty**: No data cached yet
- **Building**: Background builder actively fetching data
- **Data between X and Y**: Cache has data for that date range
- **Last Cache Run**: Timestamp of most recent hourly cycle
- **Next Run**: When the next cycle will start

### **Manual Controls**

**🚀 Start Cache Button**:
- Manually triggers cache builder (useful if auto-launch fails)
- Shows "already running" if builder is active

**🗑️ Flush Cache Button**:
- Clears all cached data (preserves OAuth tokens)
- Use when troubleshooting incorrect data
- Forces fresh fetch from Fitbit API on next report

### **Persistent Storage**

**Recommended**: Mount a volume to persist cache across container restarts:
```yaml
volumes:
  - ./fitbit_cache:/app  # Cache survives restarts
```

**Without volume mount**: Cache is lost on container restart, requiring full rebuild.

---

## 🔄 Today Refresh Behavior

### **Dashboard (Submit Button)**
When you click "Submit" with a date range that **includes today**:
- ✅ Fetches fresh data for **today** from Fitbit API
- ✅ Serves all historical dates from cache (0 API calls)
- ✅ Updates cache with new today data

Example:
- Select: Oct 20-25 (today is Oct 25)
- Result: Fetches Oct 25, serves Oct 20-24 from cache

### **MCP API Endpoints**
When an LLM requests data via API:
- `GET /api/data/sleep/2025-10-25` (today) → Auto-refreshes before returning
- `GET /api/data/metrics/2025-10-25` (today) → Auto-refreshes before returning
- `GET /api/data/sleep/2025-10-20` (historical) → Serves from cache (0 calls)

This ensures LLMs always get real-time stats for today!

---

## 🐛 Troubleshooting

### **"Cache Empty" Despite Builder Running**

**Symptoms**: Logs show "✅ Phase 3 Block Complete: 28 metric-days cached" but UI shows "Cache Empty"

**Diagnosis**: Check logs for:
```bash
docker logs fitbit-report-app-enhanced | grep "💾 Cached"
```

**If you see**: `💾 Cached sleep score for 2025-10-25: 80`
- Cache IS working, UI just needs a refresh
- Click "Submit" to update the display

**If you DON'T see** any "💾 Cached..." messages:
- Cache writes are failing silently
- Check logs for errors: `docker logs fitbit-report-app-enhanced | grep "❌"`
- Try "Flush Cache" and restart container

### **Sleep Scores Incorrect (showing efficiency instead)**

**Symptoms**: Dashboard shows sleep score 89, but Fitbit app shows 80

**Cause**: Old cache data from before bug fix

**Fix**:
1. Click "🗑️ Flush Cache" button
2. Wait for hourly cycle to rebuild cache
3. Generate new report
4. Verify logs show: `✅ Found REAL sleep score for 2025-10-22: 80`

### **Rate Limit Exceeded**

**Symptoms**: "⚠️ RATE LIMIT EXCEEDED!" message, Fitbit returns 429 errors

**Cause**: Exceeded 150 API calls/hour limit

**Fix**:
- Wait 1 hour for limit to reset
- Cache builder automatically pauses and resumes
- Historical dates served from cache require 0 API calls

**Prevention**:
- Let cache builder run overnight to backfill data
- Avoid generating many reports in quick succession
- Use date ranges that are fully cached when possible

### **Timezone Shows Wrong Time**

**Symptoms**: "Last Cache Run" shows 6:57 PM but it's only 2:57 PM

**Cause**: Container is using UTC instead of your local timezone

**Fix**: Add `TZ` environment variable to `docker-compose.yml`:
```yaml
environment:
  - TZ=America/New_York  # Your timezone here
```

Then restart: `docker-compose down && docker-compose up -d`

### **Container Keeps Restarting**

**Check logs**:
```bash
docker logs fitbit-report-app-enhanced --tail 100
```

**Common causes**:
1. **Database permission error**: Ensure volume mount is writable
2. **Missing environment variables**: Check all required vars are set
3. **Port conflict**: Ports 5032/5033 already in use

**Fix permission issues**:
```bash
chmod 777 ./fitbit_cache
docker-compose down && docker-compose up -d
```

---

## 🔧 Advanced Configuration

### **Custom Database Location**

```yaml
environment:
  - DB_PATH=/custom/path/data_cache.db
volumes:
  - /host/custom/path:/custom/path
```

### **Disable Background Cache Builder**

To control when caching happens (useful for testing):
- Don't click "Start Cache" button
- Cache builder won't auto-launch
- Data fetched on-demand when you click "Submit"

### **API Call Budget Adjustment**

Edit `src/app.py` line 180:
```python
MAX_CALLS_PER_HOUR = 145  # Default (leaves 5 call buffer)
MAX_CALLS_PER_HOUR = 100  # Conservative (more buffer)
MAX_CALLS_PER_HOUR = 148  # Aggressive (risky)
```

---

## 📊 Monitoring

### **Health Check Endpoint**
```bash
curl http://localhost:5032/api/health
```

**Response**:
```json
{
  "success": true,
  "status": "healthy",
  "app": "Fitbit Wellness Enhanced",
  "version": "2.0.0-cache"
}
```

### **Cache Status API**
```bash
curl http://localhost:5032/api/cache/status
```

**Response**:
```json
{
  "success": true,
  "sleep_records": 365,
  "advanced_metrics_records": 30,
  "daily_metrics_records": 365,
  "cardio_fitness_records": 83,
  "activities_records": 42,
  "last_sync": "2025-10-25"
}
```

### **Grafana/Prometheus Integration** (Future)

Future enhancement: Export cache metrics to Prometheus for monitoring:
- Cache hit rate
- API call count per hour
- Cache build progress
- Rate limit incidents

---

## 🚢 Deployment Platforms

### **Docker (Self-Hosted)**
- ✅ Full control
- ✅ Persistent cache
- ✅ Dual-port security
- ✅ Custom timezone

### **TrueNAS/Dockge**
- ✅ Easy management via UI
- ✅ Health monitoring
- ✅ Log aggregation
- ⚠️ Ensure volume permissions

### **Kubernetes** (Advanced)
Use provided manifests (coming soon) for:
- StatefulSet for cache persistence
- Service for OAuth callback
- Service for dashboard (internal)
- ConfigMap for environment variables

---

## 📚 Additional Resources

- [API Documentation](API_DOCUMENTATION.md)
- [Fitbit API Technical Docs](FITBIT_API_TECHNICAL_DOCUMENTATION.md)
- [Security Setup Guide](SECURITY_SETUP.md)
- [Quick Start Security](QUICK_START_SECURITY.md)
- [Get Access Token Guide](help/GET_ACCESS_TOKEN.md)

---

## 🆘 Support

**Issues?**
1. Check logs: `docker logs -f fitbit-report-app-enhanced`
2. Review this troubleshooting guide
3. Open an issue on GitHub with:
   - Log excerpts (redact tokens!)
   - Docker Compose config (redact credentials!)
   - Steps to reproduce

**Community:**
- GitHub Issues
- Discord (coming soon)
- Email: support@example.com
