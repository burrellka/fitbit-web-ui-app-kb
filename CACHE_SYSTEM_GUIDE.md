# 🗄️ Intelligent Caching System - Complete Guide

## Overview

Your Fitbit Wellness app now has an **intelligent caching system** that:
- ✅ **Accurately stores** Fitbit's actual sleep scores (not approximations)
- ✅ **Auto-populates** cache in the background after login
- ✅ **Always refreshes** today's data for real-time accuracy
- ✅ **Shows cache status** in the UI with live updates
- ✅ **Persists data** across container restarts
- ✅ **Exposes REST APIs** for MCP server integration
- ✅ **Avoids rate limits** by fetching only missing data

---

## 🎯 Key Features Implemented

### 1. **Cache Status Display** 
Visual indicator in the UI showing:
- ⏳ **Empty Cache**: "Cache Empty - Will auto-populate on first report"
- 🔄 **Building**: "Building Cache: 15 days cached"
- ✅ **Ready**: "Cache Ready: 83 days | 2025-08-01 to 2025-10-22"

Updates every 5 seconds automatically!

### 2. **Always Refresh Today**
Every time you hit "Submit":
- 🔄 **Today's date** is always re-fetched from Fitbit
- 📊 Ensures you see the **most current data**
- 🗄️ Historical dates come from cache (instant!)

### 3. **Background Cache Builder**
Automatically starts after you login:
- 🚀 Runs in a separate thread (doesn't block UI)
- 📥 Fetches last **90 days** of sleep data
- ⏸️ Batches of 10 dates with 30-second pauses (rate limit friendly)
- 🎉 Completes silently in the background

**You don't need to do anything - it just works!**

### 4. **Cache Log Viewer** 🆕
Interactive web page to inspect and export cached data:
- 📍 Access at `/cache-log` route
- 📅 Select any date range to view
- ✅ Filter by metric type (Daily, Sleep, Advanced, Activities, Cardio)
- 💾 **Download as Text** - Human-readable cache report
- 📊 **Export CSV** - Excel-compatible format for data analysis
- 🔍 Perfect for troubleshooting, backups, and sharing with healthcare providers

**CSV Export Features:**
- Dynamic columns based on selected metrics
- All metrics included: Steps, Weight, Body Fat %, Sleep, HRV, Activities, etc.
- Empty cells for missing data (Excel-friendly)
- Activities summarized in single cell

### 5. **REST API Endpoints**
For MCP server and LLM integration:
- `GET /api/health` - Health check
- `GET /api/cache/status` - Cache statistics
- `GET /api/cache-log` - Generate cache report (JSON)
- `GET /api/cache-csv` - Export cache data as CSV 🆕
- `GET /api/data/sleep/<date>` - Sleep data for specific date
- `GET /api/data/metrics/<date>` - All metrics for specific date
- `POST /api/cache/refresh/<date>` - Force refresh specific date

See [API_DOCUMENTATION.md](API_DOCUMENTATION.md) for full details.

### 5. **Persistent Storage**
Cache survives container restarts:
- 💾 Stored in `data_cache.db` SQLite database
- 🔗 Mounted as Docker volume
- ♾️ Grows over time as you use the app

---

## 📦 Deployment Steps

### Step 1: Update Your Docker Compose

Update your `docker-compose-homelab.yml` (or whatever file you use in Dockge):

```yaml
services:
  fitbit-ui-enhanced:
    image: brain40/fitbit-wellness-enhanced:latest
    container_name: fitbit-report-app-enhanced
    ports:
      - "5032:80"
    restart: unless-stopped
    environment:
      - CLIENT_ID=${CLIENT_ID}
      - CLIENT_SECRET=${CLIENT_SECRET}
      - REDIRECT_URL=${REDIRECT_URL}
    volumes:
      # IMPORTANT: Persist cache database
      - ./data_cache.db:/app/data_cache.db
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

### Step 2: Pull Latest Image

In Dockge or terminal:
```bash
docker pull brain40/fitbit-wellness-enhanced:latest
```

### Step 3: Recreate Container

In Dockge:
1. Click "Down" (stop container)
2. Click "Up" (start container)

Or in terminal:
```bash
docker-compose down
docker-compose up -d
```

### Step 4: Check Logs

Watch the cache builder start:
```bash
docker logs -f fitbit-report-app-enhanced
```

You should see:
```
🗄️ Initializing Fitbit data cache...
✅ Cache database initialized
```

Then after login:
```
🚀 Launching background cache builder...
🚀 Starting background cache builder...
📋 Background cache builder: Found 83 dates to cache
📥 Background cache builder: Fetching batch 1 (10 dates)...
✅ Background cache builder: Cached 10 dates
⏸️ Background cache builder: Waiting 30 seconds before next batch...
```

---

## 🧪 Testing the System

### Test 1: Check UI Cache Status

1. Open your app: `https://fitbitkb.burrellstribedns.org/`
2. Login to FitBit
3. **Look for the cache status indicator** above the date range selector:
   - Should show: ⏳ "Cache Empty - Will auto-populate on first report"

### Test 2: Generate First Report

1. Select a date range (e.g., last 30 days)
2. Click "Submit"
3. **Check logs** - should see:
   ```
   🔄 Refreshing today's data (2025-10-23)...
   📥 Fetching 30 missing sleep scores from API...
   ✅ Successfully cached 30 new sleep scores
   ```

4. **Check UI** - cache status should update:
   - 🔄 "Building Cache: 30 days cached"

### Test 3: Verify Sleep Scores

1. Look at the **Sleep Quality Score** chart
2. **Compare with Fitbit app** - should match EXACTLY now!
3. Example: Oct 22 should show **79** (not 89 like before)

### Test 4: Generate Second Report

1. Select same or different date range
2. Click "Submit"
3. **Should be much faster** - data comes from cache!
4. Logs should show:
   ```
   ✅ All historical sleep scores already cached!
   ```

### Test 5: Check Background Builder

1. Wait a few minutes after login
2. Check cache status - should increase:
   - 🔄 "Building Cache: 40 days cached"
   - 🔄 "Building Cache: 50 days cached"
   - Eventually: ✅ "Cache Ready: 83 days | 2025-08-01 to 2025-10-23"

### Test 6: Test API Endpoints

```bash
# Health check
curl https://fitbitkb.burrellstribedns.org/api/health

# Cache status
curl https://fitbitkb.burrellstribedns.org/api/cache/status

# Get sleep data for a date
curl https://fitbitkb.burrellstribedns.org/api/data/sleep/2025-10-22
```

---

## 📊 How Cache Population Works

### Initial State (First Login)
```
Day 1: Login → Background builder starts
       ├─ Fetches dates 1-10 (wait 30s)
       ├─ Fetches dates 11-20 (wait 30s)
       ├─ Fetches dates 21-30 (wait 30s)
       └─ ... continues for 90 days
       
You: Generate report → Fetches remaining dates for your range
     ├─ Always refreshes TODAY
     └─ Fills in any gaps (up to 30 at a time)
```

### Steady State (After a Week)
```
Day 7: Cache has 90 days of history
       
You: Generate report → INSTANT!
     ├─ Only refreshes TODAY (1 API call)
     └─ Everything else from cache (0 API calls)
```

### API Usage Comparison

**Before (No Cache):**
- 83-day report = **83 API calls** (one per day)
- Fitbit limit: **150 calls/hour**
- Could easily hit rate limit with multiple reports or advanced metrics

**After (With Cache):**
- First 83-day report = **30 calls** (batch limit)
- Second report = **1 call** (just today)
- Third report = **1 call** (just today)
- You could generate **150 reports per hour** after cache is built!

---

## 🎉 Benefits Summary

### For You
- ✅ **Accurate sleep scores** matching Fitbit app
- ✅ **Fast reports** after initial cache build
- ✅ **No rate limit errors** with normal usage
- ✅ **Always fresh** data for today
- ✅ **Visual feedback** on cache status

### For Your Wife's Instance
Same setup works independently:
- Separate cache database (`data_cache.db` in her compose directory)
- Separate background builder
- No interference between instances

### For Future MCP Server
- ✅ REST APIs ready for LLM integration
- ✅ Historical data instantly available
- ✅ No Fitbit API quota wasted on queries
- ✅ Can build intelligent insights engine

---

## 🔧 Troubleshooting

### Cache Not Building
**Symptom:** Status stays "Cache Empty"
**Solution:**
1. Check logs: `docker logs fitbit-report-app-enhanced`
2. Look for: "🚀 Starting background cache builder..."
3. If missing, try logging out and back in

### Sleep Scores Still Off
**Symptom:** Scores don't match Fitbit app
**Solution:**
1. Check logs for "Cached sleep score for..."
2. Verify the score printed matches Fitbit app
3. If still using `efficiency`, the cache needs that date
4. Generate a report including that date to cache it

### Cache Database Growing Too Large
**Symptom:** `data_cache.db` file is huge
**Solution:**
- Normal: ~1 MB per 100 days of data
- If > 100 MB, something's wrong (report this!)
- To reset: Stop container, delete `data_cache.db`, restart

### Background Builder Taking Forever
**Symptom:** Still building after 30+ minutes
**Solution:**
- Normal: 90 days = 9 batches × 30 seconds = ~5 minutes
- If stuck, check logs for errors
- Fitbit may have throttled you - wait an hour

### Container Won't Start
**Symptom:** Container crashes on startup
**Solution:**
1. Check logs: `docker logs fitbit-report-app-enhanced`
2. Look for SQLite or cache_manager errors
3. If corrupted cache: Delete `data_cache.db` and restart

---

## 📈 Next Steps

### Immediate (You)
1. ✅ Deploy and test
2. ✅ Verify sleep scores match app
3. ✅ Watch cache populate
4. ✅ Enjoy fast, accurate reports!

### Soon (Lady-Ann)
1. Set up her instance with separate compose file
2. Separate cache database
3. Independent background builder

### Future (MCP Server)
1. Build MCP server to expose APIs
2. Create LLM prompts for fitness insights
3. Enable voice queries ("How was my sleep last night?")
4. Trend analysis and recommendations

---

## 📞 Support

**Questions?**
- Check logs first: `docker logs -f fitbit-report-app-enhanced`
- Review [API_DOCUMENTATION.md](API_DOCUMENTATION.md)
- Review [ENHANCEMENT_ROADMAP.md](ENHANCEMENT_ROADMAP.md)

**Found a bug?**
- Check if cache database is corrupted
- Try deleting `data_cache.db` and rebuilding
- Report with logs and cache status screenshot

---

## 🎯 Success Criteria

You'll know it's working when:
- ✅ Cache status shows in UI
- ✅ Sleep scores match Fitbit app exactly
- ✅ Reports generate in < 5 seconds after cache builds
- ✅ Logs show "All historical sleep scores already cached!"
- ✅ API endpoints return data: `/api/cache/status`

---

**Enjoy your intelligent, self-maintaining fitness dashboard!** 🎉

