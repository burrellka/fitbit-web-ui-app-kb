# Fitbit Web UI Application - Project Transfer Document

**Date:** November 20, 2024  
**Project Status:** Active Development - Resolving Production Bug  
**Transfer Type:** Personal → Corporate Cursor Account

---

## 🎯 Project Overview

### Purpose
A self-hosted web application that provides comprehensive Fitbit health metrics visualization and data management. The application fetches data from Fitbit's OAuth2.0 API, caches it in SQLite, and presents it through an interactive Dash-based dashboard.

### Key Features
- **OAuth2.0 Authentication** with Fitbit API
- **Multi-phase Background Cache Builder** (optimized for API efficiency)
- **Interactive Dashboard** with date range reporting
- **Data Export** (CSV format)
- **MCP Server Integration** via JSON API endpoints
- **API Key Authentication** for programmatic access
- **Persistent Logging** with configurable log levels

### Technology Stack
- **Backend:** Python 3.10, Flask, Dash
- **Database:** SQLite (persistent cache)
- **Containerization:** Docker + Docker Compose
- **APIs:** Fitbit OAuth2.0 REST API
- **Visualization:** Plotly Express, Dash components
- **Data Processing:** Pandas

---

## 🏗️ Architecture

### Application Components

1. **Main Application (`src/app.py`)**
   - Dash web UI (port 5033)
   - Flask endpoints for API and OAuth
   - OAuth callback handler (port 5032)
   - Report generation with charts and tables
   - MCP JSON API endpoints

2. **Cache Manager (`src/cache_manager.py`)**
   - SQLite database wrapper
   - CRUD operations for all metrics
   - Schema: 12 tables (heart_rate, sleep, weight, hrv, spo2, cardio_fitness, steps, calories, distance, floors, azm, activities)

3. **Docker Configuration**
   - `Dockerfile`: Python 3.10-slim, installs dependencies, health checks
   - `docker-compose.yml`: Service definitions, volume mounts, environment variables

### Data Flow

```
Fitbit API → OAuth2.0 Token Exchange → Cache Builder (3 phases) → SQLite Database
                                                ↓
                                    Dashboard UI / MCP API
                                                ↓
                                    Charts, Tables, CSV Export
```

### Cache Builder Phases

**Phase 1: Range Endpoints (Most Efficient)**
- HRV: `/1/user/-/hrv/date/{start}/{end}.json` (30 days/call)

**Phase 2: Monthly Endpoints (Calendar-Aware)**
- Sleep: `/1.2/user/-/sleep/date/{start}/{end}.json` (100 days max, fetches by calendar month)
- Weight: `/1/user/-/body/log/weight/date/{start}/{period}.json` (fetches by calendar month)

**Phase 3: Daily Endpoints (Per-Metric)**
- Heart Rate, Steps, Calories, Distance, Floors, SpO2, Cardio Fitness, AZM
- Each metric: `/1/user/-/{metric}/date/{date}/1d.json`

**Activities:** Fetched separately via `/1/user/-/activities/date/{date}.json`

---

## 🖥️ Local Setup Details

### Homelab Environment
- **OS:** Windows 10 (Build 26200)
- **Shell:** PowerShell
- **Time Zone:** America/New_York
- **Workspace:** `C:\dev\fitbit-web-ui-app-kb`

### Docker Configuration
- **Docker Desktop:** Running on Windows
- **Compose Version:** v2.x (using `docker-compose.yml`)
- **Network:** Bridge network
- **Volumes:**
  - `./data:/app/data` (SQLite database: `data_cache.db`)
  - `./logs:/app/logs` (Application logs: `fitbit-app.log`)
  - `./data/tokens:/app/tokens` (OAuth refresh tokens)

### Port Mappings
- **5032:5032** → OAuth callback endpoint
- **5033:5033** → Main dashboard

### Domain & Reverse Proxy
- **Production URL:** `https://fitbitkbauth.burrellstribedns.org/`
- **OAuth Redirect URI:** `https://fitbitkbauth.burrellstribedns.org/`
- **Reverse Proxy:** Configured to forward to `localhost:5033`
- **SSL:** Handled by reverse proxy (not in Docker)

### Environment Variables (`.env` file)
```env
# Fitbit OAuth2.0
CLIENT_ID=your_fitbit_client_id
CLIENT_SECRET=your_fitbit_client_secret
REDIRECT_URI=https://fitbitkbauth.burrellstribedns.org/

# Security
DASHBOARD_PASSWORD=your_secure_password
API_KEY=your_api_key_for_mcp_server

# Configuration
TZ=America/New_York
LOG_LEVEL=INFO  # Options: CRITICAL, ERROR, WARN, INFO, DEBUG, TRACE
```

### Docker Compose Structure
```yaml
version: '3.8'
services:
  fitbit-report-app-enhanced:
    build: .
    container_name: fitbit-report-app-enhanced
    ports:
      - "5032:5032"  # OAuth callback
      - "5033:5033"  # Dashboard
    environment:
      - CLIENT_ID=${CLIENT_ID}
      - CLIENT_SECRET=${CLIENT_SECRET}
      - REDIRECT_URI=${REDIRECT_URI}
      - DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}
      - API_KEY=${API_KEY}
      - TZ=${TZ:-America/New_York}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./data/tokens:/app/tokens
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5033/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
```

### File Structure
```
fitbit-web-ui-app-kb/
├── src/
│   ├── app.py                    # Main application (5900+ lines)
│   └── cache_manager.py          # SQLite cache wrapper
├── data/                         # Created by user
│   ├── data_cache.db            # SQLite database (gitignored)
│   └── tokens/                  # OAuth tokens (gitignored)
│       └── refresh_token.json
├── logs/                         # Created by user
│   └── fitbit-app.log           # Application logs (gitignored)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                          # User-created (gitignored)
├── env.example                   # Template
├── README.md
├── API_DOCUMENTATION.md
└── .gitignore
```

---

## 🎯 Project Goals & Roadmap

### Completed ✅
1. **API Optimization:** Switched from daily calls to range/monthly endpoints (saved ~3000 API calls/year per user)
2. **Calendar-Aware Fetching:** Sleep and weight fetch by calendar month (28-31 days)
3. **UI Enhancements:**
   - Renamed "Cardio Load" → "Active Duration"
   - Added Active Duration to exercise log table and CSV export
   - Removed unused activity filter dropdown
4. **MCP Server Ready:** `/api/data/range` endpoint with API key authentication
5. **Real-Time Today Stats:** When generating reports that include today's date, app fetches fresh data for today before proceeding
6. **Persistent Logging:** File-based logging with rotation (10MB, 5 backups)
7. **Configurable Log Levels:** Environment variable control (TRACE, DEBUG, INFO, WARN, ERROR, CRITICAL)

### In Progress 🚧
- **Bug Fix:** Resolving `ValueError: All arrays must be of the same length` when generating reports with cached data

### Future Goals 📋
1. **MCP Server Integration:** Build separate MCP server to consume `/api/data/range` endpoint
2. **Cardio Load Extraction:** Currently renamed to "Active Duration", may need to extract true Cardio Load metric
3. **Enhanced Error Handling:** More graceful handling of partial cache misses
4. **Performance Optimization:** Reduce report generation time for large date ranges
5. **Testing Suite:** Unit and integration tests

---

## 🐛 Current Issue - CRITICAL

### Problem Description
**Error:** `ValueError: All arrays must be of the same length` when generating reports

**Location:** `src/app.py`, line 4127 in `update_output` callback

**Trigger:** User clicks "Generate Report" with:
- Date range: 2025-11-01 to 2025-11-03 (3 days)
- All data already in cache
- Did NOT click "Start Cache" button
- Successfully re-authenticated (fresh OAuth token)

### Error Stack Trace
```python
File "/app/src/app.py", line 4127, in update_output
  df_merged = pd.DataFrame({
File "/usr/local/lib/python3.10/site-packages/pandas/core/frame.py", line 709, in __init__
  mgr = dict_to_mgr(data, index, columns, dtype=dtype, copy=copy, typ=manager)
File "/usr/local/lib/python3.10/site-packages/pandas/core/internals/construction.py", line 481, in dict_to_mgr
  return arrays_to_mgr(arrays, columns, index, dtype=dtype, typ=typ, consolidate=copy)
File "/usr/local/lib/python3.10/site-packages/pandas/core/internals/construction.py", line 115, in arrays_to_mgr
  index = _extract_index(arrays)
File "/usr/local/lib/python3.10/site-packages/pandas/core/internals/construction.py", line 655, in _extract_index
  raise ValueError("All arrays must be of the same length")
```

### Root Cause Analysis
The error occurs when creating `df_merged` DataFrame. This DataFrame requires all lists (arrays) passed to it to have the same length:
- `dates_str_list`
- `deep_sleep_list`
- `light_sleep_list`
- `rem_sleep_list`
- `awake_list`
- `heartrate_list`
- `vo2_max_list`
- `weight_list`
- `spo2_list`
- `calories_list`
- `distance_list`
- `floors_list`
- `steps_list`
- `azm_list`

**The Issue:** When data is loaded from cache AND "today" is fetched for real-time stats, the logic has an inconsistency:
1. "Today" real-time stats are fetched via `fetch_todays_stats()` (lines 4076-4081)
2. Data is successfully cached (log shows: "✅ Fetched heart_rate", "✅ Fetched steps", etc.)
3. But the cached data is NOT being added to the respective lists
4. Result: Lists have mismatched lengths (some 3, some 0)

### Logs Analysis
```
2025-11-03 15:56:42 [INFO] 🔄 TODAY (2025-11-03) in range - fetching real-time stats...
2025-11-03 15:56:42 [INFO] 🔄 Fetching TODAY's real-time stats (2025-11-03)...
[... API calls succeed ...]
2025-11-03 15:56:43 [INFO] ✅ Fetched floors
2025-11-03 16:17:41 [INFO] 📊 Processing data for 3 dates...
2025-11-03 16:17:41 [INFO] ✅ Cached 0 days of heart rate data  ← PROBLEM!
2025-11-03 16:17:41 [INFO] ✅ Cached 0 days of steps data       ← PROBLEM!
2025-11-03 16:17:41 [INFO] ✅ Cached 0 days of weight data
[...]
2025-11-03 16:17:41 [INFO] 📊 Fetching sleep data for 3 dates...
2025-11-03 16:17:41 [INFO] 📊 Using CACHED sleep scores for 2025-11-01: Reality=82, Proxy=79, Efficiency=94
2025-11-03 16:17:41 [INFO] 📊 Using CACHED sleep scores for 2025-11-02: Reality=84, Proxy=82, Efficiency=90
2025-11-03 16:17:41 [INFO] 📊 Using CACHED sleep scores for 2025-11-03: Reality=89, Proxy=86, Efficiency=93
2025-11-03 16:17:41 [INFO] ✅ Loaded 3 dates from cache
2025-11-03 16:17:41 [ERROR] Exception on /_dash-update-component [POST]
[... ValueError ...]
```

**Key Observation:** Sleep data is correctly loaded from cache (3 dates), but heart rate, steps, etc., show "Cached 0 days" despite being fetched!

### Recent Changes That May Have Caused This

**Last Working State:** Code worked before the latest indentation fixes

**Recent Changes:**
1. Fixed indentation error in activities loading loop (line 3549)
2. Previous fixes related to `fetch_todays_stats()` integration
3. Logic changes around `all_cached` flag and list initialization

### Code Section Requiring Investigation

**File:** `src/app.py`  
**Function:** `update_output` (starts ~line 3990)  
**Critical Section:** Lines 4070-4130 (around "TODAY in range" logic and DataFrame creation)

```python
# Around line 4070-4081
if today_str in dates_str_list:
    print(f"🔄 TODAY ({today_str}) in range - fetching real-time stats...")
    todays_data = fetch_todays_stats(today_str, oauth_token)
    if todays_data:
        print(f"✅ Today's stats fetched and cached: {list(todays_data.keys())}")
        all_cached = False  # ← May be causing issues?
    else:
        print(f"⚠️ Failed to fetch today's stats")

# Around line 4085-4120
# Logic for loading from cache when all_cached=True
# BUT: Lists may not be populated correctly after fetch_todays_stats()

# Around line 4127
df_merged = pd.DataFrame({
    'Date': dates_str_list,
    'Deep Sleep (min)': deep_sleep_list,
    'Light Sleep (min)': light_sleep_list,
    # ... etc - ALL MUST BE SAME LENGTH!
})
```

### Hypothesis
The `fetch_todays_stats()` function successfully:
1. Fetches today's data from Fitbit API ✅
2. Caches it in SQLite ✅

But the `update_output` callback FAILS to:
1. Read the freshly cached "today" data back into the lists ❌
2. Merge it with the other cached dates (2025-11-01, 2025-11-02) ❌

**Result:** Sleep lists have 3 items, but heart rate/steps/etc. lists remain empty → ValueError

### Debugging Steps Taken
1. ✅ Fixed indentation errors
2. ✅ Verified cache write operations work
3. ✅ Confirmed OAuth token is valid
4. ✅ Verified API calls succeed
5. ❌ Need to verify cache READ operations populate lists correctly
6. ❌ Need to ensure `all_cached` flag logic doesn't skip list population

### Next Steps for Resolution
1. **Add more detailed logging** around line 4085-4120 to trace list population
2. **Verify cache read logic** after `fetch_todays_stats()` completes
3. **Check `all_cached` flag usage** - it may incorrectly skip list population
4. **Ensure lists are initialized once** and populated from cache for ALL dates (including today)
5. **Test with date range that doesn't include today** to isolate the issue

### Temporary Workaround
- User can click "Start Cache" to rebuild cache, which may populate lists correctly
- But this defeats the purpose of having cached data

---

## 🔧 Development Commands

### Docker Operations
```bash
# Build and start
docker-compose up -d --build

# Stop
docker-compose down

# View logs
docker-compose logs -f

# Rebuild after code changes
docker-compose down && docker-compose up -d --build

# Check container status
docker ps

# Enter container shell
docker exec -it fitbit-report-app-enhanced /bin/bash
```

### Git Operations
```bash
# Status
git status --short

# Add and commit
git add .
git commit -m "Your message"

# Push
git push origin main
```

### Local Testing (Without Docker)
```bash
# Install dependencies
pip install -r requirements.txt

# Run app
python src/app.py

# Access at http://localhost:5033
```

---

## 🔐 Security Considerations

### Credentials Storage
- **OAuth Tokens:** Stored in `./data/tokens/refresh_token.json` (gitignored)
- **Environment Variables:** Stored in `.env` (gitignored)
- **API Key:** Used for MCP endpoints, set in `.env`

### Authentication Layers
1. **Dashboard:** Password-protected (DASHBOARD_PASSWORD env var)
2. **MCP API:** API key authentication via `X-API-Key` header
3. **Fitbit API:** OAuth2.0 with refresh token

### Network Security
- **Ports 5032/5033:** Exposed only through reverse proxy
- **HTTPS:** Terminated at reverse proxy level
- **Docker Network:** Bridge mode (isolated)

---

## 📊 API Endpoints

### Dashboard Endpoints
- `GET /` - Main dashboard UI (password-protected)
- `GET /oauth/callback` - OAuth2.0 callback (port 5032)
- `POST /_dash-update-component` - Dash callbacks

### MCP API Endpoints (API Key Required)
- `GET /api/data/range?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` - Get cached data for date range
- `GET /api/health` - Health check

### API Response Format
```json
{
  "start_date": "2025-11-01",
  "end_date": "2025-11-03",
  "total_days": 3,
  "data": [
    {
      "date": "2025-11-01",
      "heart_rate": {"resting": 58, "zones": [...]},
      "sleep": {"efficiency": 94, "minutes_asleep": 450, ...},
      "activities": {"steps": 8500, "calories": 2400, ...},
      // ... all metrics
    }
  ]
}
```

---

## 📝 Important Notes

### Known Limitations
1. **Fitbit API Rate Limits:** 150 calls/hour (app respects this)
2. **Sleep Range Endpoint:** Max 100 days per call
3. **Date Range:** App tested with ranges up to 365 days
4. **Timezone:** Hardcoded to America/New_York

### Data Freshness
- **Cache Builder:** Runs on-demand (button click) or automatically on app start (if configured)
- **Today's Data:** Always fetched fresh when included in report date range
- **Historical Data:** Only fetched if not in cache

### Performance
- **Report Generation:** ~2-5 seconds for 30-day range (cached)
- **Cache Build:** ~5-10 minutes for 365 days (initial)
- **Database Size:** ~5-10MB per year of data

---

## 🆘 Troubleshooting

### Common Issues

**Issue 1: OAuth callback fails**
- Check `REDIRECT_URI` matches Fitbit app settings EXACTLY (including trailing slash)
- Verify reverse proxy forwards `https://` to `http://localhost:5032`

**Issue 2: Database locked**
- Docker volume mount issue - ensure `./data` directory exists before starting
- Restart container: `docker-compose restart`

**Issue 3: Container won't start**
- Check healthcheck: `docker inspect fitbit-report-app-enhanced`
- View logs: `docker-compose logs`
- Verify ports 5032/5033 aren't in use: `netstat -ano | findstr 5033`

**Issue 4: Cache not persisting**
- Verify volume mount: `docker inspect fitbit-report-app-enhanced | findstr Mounts`
- Check file permissions on `./data` directory
- Ensure `data_cache.db` exists at `./data/data_cache.db`

---

## 📞 Support & Resources

### Documentation
- `README.md` - User guide and setup instructions
- `API_DOCUMENTATION.md` - API endpoint reference
- `env.example` - Environment variable template

### Fitbit API Documentation
- OAuth2.0: https://dev.fitbit.com/build/reference/web-api/oauth2/
- API Reference: https://dev.fitbit.com/build/reference/web-api/

### Dependencies (requirements.txt)
```
dash==2.14.2
dash-bootstrap-components==1.5.0
pandas==2.1.3
plotly==5.18.0
requests==2.31.0
flask==3.0.0
python-dotenv==1.0.0
```

---

## ✅ Pre-Transfer Checklist

- [ ] Verify `.env` file is properly configured
- [ ] Confirm `./data` and `./logs` directories exist
- [ ] Test OAuth flow end-to-end
- [ ] Generate a test report to verify functionality
- [ ] Resolve current `ValueError` bug (in progress)
- [ ] Document any additional customizations
- [ ] Export current database for backup: `cp ./data/data_cache.db ./data/data_cache.db.backup`
- [ ] Push all code changes to Git repository

---

## 🎓 Onboarding for New Cursor Session

### Quick Start Steps
1. **Read this document fully**
2. **Review current issue section** (critical bug needs fixing)
3. **Examine `src/app.py`** lines 4070-4130 (problem area)
4. **Check recent git history** for context on changes
5. **Test locally** to reproduce the issue
6. **Fix the bug** (list length mismatch in DataFrame creation)
7. **Verify fix** by generating a 3-day report including today
8. **Document solution** and update this transfer doc if needed

### Key Files to Understand
1. `src/app.py` - Main application (focus on `update_output` callback)
2. `src/cache_manager.py` - Database operations
3. `docker-compose.yml` - Deployment configuration

### Questions to Ask User
- What is the exact date range being tested?
- Has the cache builder been run at least once?
- Are there any other error messages in the logs?
- What is the size of `data_cache.db`?

---

**Document Version:** 1.0  
**Last Updated:** November 20, 2024  
**Prepared By:** AI Assistant (Cursor - Personal Account)  
**Transfer To:** AI Assistant (Cursor - Corporate Account)

---

*Good luck with the bug fix! The issue is isolated to list population logic around line 4070-4130. Focus on ensuring all lists are populated from cache after `fetch_todays_stats()` completes.*

