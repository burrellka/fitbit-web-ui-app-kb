# 🚀 FULL DEPLOYMENT CHECKLIST - Weight Fix

## Current Status
- ✅ All code committed to `main` branch
- ✅ Latest commit: `64f6b37` - Clarified weight variable definitions
- ✅ API endpoint fixed: `/body/log/weight/` (has `/log/`)
- ✅ Parsing logic: Extracts `weight` and `date` keys correctly
- ✅ Database schema: Has `body_fat` column

## 📦 Step 1: Build Fresh Docker Image (Local)

```bash
# Navigate to project directory
cd C:\dev\fitbit-web-ui-app-kb

# Pull latest code (just to be sure)
git pull origin main

# Build with no cache to ensure fresh build
docker-compose build --no-cache

# Tag for Docker Hub
docker tag fitbit-wellness-enhanced:latest brain40/fitbit-wellness-enhanced:latest

# Push to Docker Hub
docker push brain40/fitbit-wellness-enhanced:latest
```

## 🏠 Step 2: Deploy to Homelab (Dockge)

### Option A: Via Dockge UI
1. Open Dockge in browser
2. Find `fitbit-report-app-enhanced` stack
3. Click **Stop**
4. Click **Pull** (pulls `brain40/fitbit-wellness-enhanced:latest`)
5. Click **Start**

### Option B: Via SSH
```bash
# SSH into homelab
ssh user@homelab-ip

# Navigate to project directory
cd /path/to/fitbit-web-ui-app-kb

# Pull latest code
git pull origin main

# Pull latest Docker image
docker pull brain40/fitbit-wellness-enhanced:latest

# Stop and restart
docker-compose down
docker-compose up -d
```

## 🗑️ Step 3: CRITICAL - Delete Old Cache Database

**This is THE MOST IMPORTANT step!** The old cache has corrupt/empty weight data.

```bash
# SSH into homelab (if not already)
ssh user@homelab-ip

# Navigate to project directory
cd /path/to/fitbit-web-ui-app-kb

# Backup old cache (optional but recommended)
cp cache/fitbit_cache.db cache/fitbit_cache_backup_$(date +%Y%m%d_%H%M%S).db

# DELETE the old cache
rm cache/fitbit_cache.db

# Restart container to create fresh database
docker-compose restart
```

## ✅ Step 4: Verify Deployment

### Check Container is Running
```bash
docker ps | grep fitbit
```
Should show: `fitbit-report-app-enhanced   Up X minutes`

### Check Logs for Weight Fetching
```bash
# Follow logs in real-time
docker logs fitbit-report-app-enhanced -f

# Or search for weight-related logs
docker logs fitbit-report-app-enhanced --tail=500 | grep -i weight
```

### Expected Log Output:
```
🚀 Starting ROBUST background cache builder...
🔄 NEW HOURLY CYCLE STARTING - 2025-10-30 18:00:00
✅ Token refreshed! Valid for 8 hours.
📍 PHASE 1: Range Metrics (Steps, Calories, Distance, Floors, AZM, RHR, Weight, SpO2)
...
📥 'Weight' missing 365 days. Fetching entire range...
  [CACHE_DEBUG] Caching Weight for 2025-10-20: Weight=182.5, Body Fat=20.5%
  ✅ [CACHE_VERIFY] Weight/Fat cached successfully for 2025-10-20
  [CACHE_DEBUG] Caching Weight for 2025-10-21: Weight=183.0, Body Fat=None%
  ✅ [CACHE_VERIFY] Weight/Fat cached successfully for 2025-10-21
...
💾 Cached 14 days for 'Weight'
```

### Run Diagnostic Script (Optional)
```bash
cd /path/to/fitbit-web-ui-app-kb
python3 check_weight_cache.py
```

Expected output:
```
✅ 'body_fat' column EXISTS
Total records: 365
Records with weight: 14
✅ Found 14 days with weight data
```

## ⏰ Step 5: Wait for Background Builder

The background builder runs **every hour**. Here's the timeline:

- **T+0 min**: Container starts, builder begins first cycle
- **T+15 min**: Phase 1 completes (Steps, Calories, Weight, etc.)
- **T+45 min**: Phase 3 completes (Sleep, HRV, etc.)
- **T+60 min**: First cycle complete, waits for next hour
- **T+120 min**: Second cycle (fills any missed data)

## 🎉 Step 6: Verify Weight Data in Report

### Download Cache Log
1. Open browser: `http://your-homelab-ip:8050/cache-log`
2. Select date range (e.g., last 14 days)
3. Check "Daily Metrics"
4. Click "Generate Report"
5. Look for:
   ```
   📊 Daily Metrics:
     Steps: 11076
     Calories: 3162
     ...
     Weight: 182.8 lbs        ← SHOULD HAVE DATA!
     Body Fat: 20.5%          ← SHOULD HAVE DATA!
     SpO2: 93.9
   ```

### Generate Full Report
1. Open browser: `http://your-homelab-ip:8050/`
2. Select date range with weight data
3. Click "Submit"
4. Scroll to **Weight Log** chart - should show data with enhanced header
5. Scroll to **Body Fat %** chart - should show data (if you log body fat in Fitbit)

## 🐛 Troubleshooting

### Weight Still Shows "None" After 2 Hours
**Problem:** Cache database wasn't deleted or background builder failed

**Solution:**
```bash
# Check if old database still exists
ls -lh cache/fitbit_cache.db

# Delete it
rm cache/fitbit_cache.db

# Restart container
docker-compose restart

# Monitor logs
docker logs fitbit-report-app-enhanced -f
```

### "No Weight Data Logged" Message
**Problem:** Your Fitbit account has no weight entries

**Solution:** 
- Open Fitbit app on phone
- Go to "Log" → "Weight"
- Log your current weight
- Wait 1 hour for background builder to fetch it

### Container Won't Start
**Problem:** Docker image corruption or port conflict

**Solution:**
```bash
# Check logs
docker logs fitbit-report-app-enhanced

# Restart Docker daemon (if needed)
sudo systemctl restart docker

# Rebuild container
docker-compose down
docker-compose up -d
```

## 📝 Final Checklist

Before closing this task, verify:
- [ ] Docker image built with `--no-cache`
- [ ] Docker image pushed to Docker Hub
- [ ] Container restarted on homelab
- [ ] **Old cache database deleted**
- [ ] Container logs show weight fetching
- [ ] Cache log shows weight data
- [ ] Report displays weight chart with data

---

## 🎯 Expected Result

After completing these steps, your Fitbit report should show:

### Weight Log Chart
```
Weight

⚖️ Most Recent: 182.8 lbs (10/28) | Earliest: 180.5 lbs (10/15) | Change: 📈 +2.3 lbs
Overall avg: 181.5 lbs | Last 30d avg: 182.1 lbs

[Beautiful line chart with weight trend]
```

### Body Fat % Chart (if you log body fat)
```
Body Fat %

💪 Most Recent: 20.5% (10/28) | Earliest: 20.0% (10/15) | Change: 📈 +0.5%
Overall avg: 20.7% | Last 30d avg: 20.8%

[Beautiful line chart with body fat trend]
```

---

**Good luck with the deployment!** 🚀

