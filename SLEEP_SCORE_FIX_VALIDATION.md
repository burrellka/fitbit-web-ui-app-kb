# 🎯 Sleep Score Fix - Validation Guide

## 🐛 Root Cause Identified

The diagnostic script confirmed: **Fitbit API is NOT returning the `sleepScore` field** because the OAuth `settings` scope was missing from the authorization request.

### What Was Wrong:
```python
# OLD (Line 1262) - MISSING 'settings' scope
scope = 'profile activity cardio_fitness heartrate sleep weight oxygen_saturation...'
auth_url = f'...&prompt=none...'  # Did NOT force consent screen
```

### What's Fixed:
```python
# NEW (Line 1264) - INCLUDES 'settings' scope + forces consent
scope = 'profile activity settings heartrate sleep cardio_fitness weight...'
auth_url = f'...&prompt=consent...'  # FORCES consent screen to re-grant scopes
```

---

## 📋 Step-by-Step Deployment & Validation

### Step 1: Pull & Deploy the Fix

On your homelab:

```bash
# Stop the current container
docker stop fitbit-report-app-enhanced
docker rm fitbit-report-app-enhanced

# Pull the latest code
cd /path/to/fitbit-web-ui-app-kb
git pull origin main

# Build the new image
docker build -t brain40/fitbit-wellness-enhanced:latest .

# Push to Docker Hub (optional)
docker push brain40/fitbit-wellness-enhanced:latest

# Start the container
docker compose up -d
```

---

### Step 2: Re-Authorize with New Scopes

**CRITICAL: You MUST log out and re-authorize to grant the `settings` scope!**

1. **Logout**: Visit `http://192.168.13.5:5033/` and click the **Logout** button

2. **Clear Session**: Optional but recommended:
   ```bash
   docker restart fitbit-report-app-enhanced
   ```

3. **Login Again**: Click the **Login** button

4. **Grant Permissions**: You should see the **Fitbit consent screen** asking for permissions. 
   - Verify it shows: ✅ **Sleep**, ✅ **Settings**, ✅ **Heart Rate**, ✅ **Profile**
   - Click **Allow** to grant all permissions

5. **Confirm New Token**: Check logs to see the new scopes:
   ```bash
   docker logs fitbit-report-app-enhanced | grep "scope"
   ```
   
   You should see `"scope":"...settings...sleep..."`

---

### Step 3: Validate Sleep Score is Now Present

#### Option A: Quick Test (UI)

1. Generate a report for **Oct 20-23**
2. Check the **Sleep Score Chart**
3. **Expected Result**: 
   - Should show **actual sleep scores** (e.g., 80, 84, 79)
   - Should **NOT** show efficiency values (e.g., 89, 93)
4. Check logs for:
   ```
   ✅ PHASE 3 - Found REAL sleep score for 2025-10-22: 80
   💾 Cached sleep score for 2025-10-22: 80
   ```

#### Option B: Diagnostic Script Validation

Re-run the diagnostic script with the **new access token**:

```bash
# Get new token
docker logs fitbit-report-app-enhanced | grep "access_token" | tail -1

# Edit test script with new token
nano test_fitbit_sleep_api.py

# Run validation
python3 test_fitbit_sleep_api.py
```

**Expected Output:**
```
🎯 SLEEP SCORE OBJECT FOUND:
   Type: <class 'dict'>
   Content: {
      "overall": 80,
      "composition": {...},
      "revitalization": {...},
      "duration": {...}
   }

✅ OVERALL SLEEP SCORE: 80
```

---

### Step 4: Flush Cache & Rebuild

The old cache may still have incorrect efficiency-based "scores". Flush it:

1. Visit the app: `http://192.168.13.5:5033/`
2. Click **Flush Cache** button
3. Click **Start Cache** button
4. Wait for the hourly cycle to complete
5. Generate a report

**Expected Logs:**
```
✅ PHASE 3 - Found REAL sleep score for 2025-10-22: 80
✅ PHASE 3 - Found REAL sleep score for 2025-10-21: 79
💾 Cached sleep score for 2025-10-22: 80
💾 Cached sleep score for 2025-10-21: 79
```

---

### Step 5: Verify Sleep Score vs Efficiency

Open the SQLite cache database and verify:

```bash
docker exec fitbit-report-app-enhanced sqlite3 /app/data_cache.db "SELECT date, sleep_score, efficiency FROM sleep_data WHERE date BETWEEN '2025-10-20' AND '2025-10-23';"
```

**Expected:**
```
2025-10-20|84|93
2025-10-21|80|89
2025-10-22|80|89
2025-10-23|79|91
```

Notice: `sleep_score ≠ efficiency` (this is correct!)

---

## ✅ Success Checklist

- [ ] Code deployed with `settings` scope
- [ ] Logged out and re-authorized with consent screen
- [ ] Diagnostic script shows `sleepScore` object
- [ ] UI displays correct sleep scores (80, 84, 79) not efficiency (89, 93)
- [ ] Cache database shows `sleep_score != efficiency`
- [ ] Logs show: `✅ PHASE 3 - Found REAL sleep score`
- [ ] No more warnings: `⚠️ No sleep score found`

---

## 🚨 If Sleep Score is STILL Missing

If after re-authorization the sleep score is still not present:

### Verify Fitbit App Settings

1. Go to https://dev.fitbit.com/apps
2. Select your app
3. Verify OAuth 2.0 Application Type: **Personal**
4. Under **Intraday Time Series**, check:
   - ✅ Intraday Heart Rate
   - ✅ (Any other available options)
5. Save settings

### Check Token Scopes

```bash
# Extract and decode the access token
docker logs fitbit-report-app-enhanced | grep "access_token" | tail -1

# Use JWT decoder (jwt.io) to decode the token
# Look for the "scopes" field - should include "wsle" (write sleep) and other settings-related scopes
```

### Contact Fitbit Support

If all else fails, the issue may be account-specific:
- Some Fitbit devices (e.g., very old models) may not support Sleep Score
- Some accounts may have restricted API access

---

## 📊 Final Note on Efficiency vs Sleep Score

**These are TWO DIFFERENT metrics:**

| Metric | Description | Range | Example |
|--------|-------------|-------|---------|
| **Efficiency** | % of time asleep while in bed | 0-100% | 89% |
| **Sleep Score** | Holistic sleep quality score | 0-100 | 80 |

The app was incorrectly using **efficiency** as a fallback for **sleep score**. With the `settings` scope, we now get the **official Sleep Score** from the Fitbit API.

---

**Questions?** Check the logs, run the diagnostic script, or review the API response structure.

