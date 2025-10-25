# Fitbit API Technical Documentation

## 📚 Overview

This document provides a comprehensive reference of all Fitbit Web API endpoints used in the Fitbit Wellness Report application, including implementation details, rate limit considerations, and future enhancement opportunities.

**Last Updated**: October 25, 2025  
**Fitbit API Version**: v1 and v1.2 (for sleep endpoints)

---

## ⚠️ Rate Limits

**Fitbit API Limit**: **150 requests per hour per user**

**Our Mitigation Strategy**:
- **SQLite caching** for all metrics
- **Cache-first approach** for sleep and advanced metrics
- **Cache-on-write** for daily metrics (RHR, steps, weight, etc.)
- **Background builder** for incremental historical data population
- **Manual "Start Cache" trigger** to control when background jobs run

---

## 🔐 OAuth & Authentication Endpoints

### 1. Token Refresh
**Endpoint**: `POST https://api.fitbit.com/oauth2/token`

**Purpose**: Refresh expired access tokens using refresh token

**Request Type**: POST with form data
```http
POST /oauth2/token
Content-Type: application/x-www-form-urlencoded
Authorization: Basic {base64(client_id:client_secret)}

grant_type=refresh_token&refresh_token={refresh_token}
```

**Response**:
```json
{
  "access_token": "eyJhbGc...",
  "refresh_token": "84563b31...",
  "expires_in": 28800,
  "token_type": "Bearer"
}
```

**Implementation Locations**:
- `automatic_daily_sync()` - Auto-refresh for daily sync
- `refresh_access_token()` - Manual token refresh
- `update_login_button()` - OAuth callback handling
- `/api/data/exercise/<date>` - API endpoint token refresh

**Rate Limit Impact**: ✅ Minimal (only on token expiry ~every 8 hours)

---

## 👤 User Profile Endpoints

### 2. User Profile
**Endpoint**: `GET https://api.fitbit.com/1/user/-/profile.json`

**Purpose**: Validate access token and retrieve user information

**Response**:
```json
{
  "user": {
    "firstName": "John",
    "lastName": "Doe",
    "displayName": "John D.",
    "age": 30,
    "height": 180,
    "weight": 75.5,
    ...
  }
}
```

**Usage**:
- Token validation before report generation
- Display user name in report title

**Implementation**: 
- `disable_button_and_calculate()` - Token validation check
- `update_output()` - Fetch user details for report

**Rate Limit Impact**: ⚠️ 2 calls per report (validation + profile fetch)

**Optimization Opportunity**: Cache user profile data (changes rarely)

---

## 📊 Efficient Range-Based Endpoints

These endpoints support date ranges (`start_date/end_date`), making them **very efficient** for historical data retrieval.

### 3. Heart Rate & Zones (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/heart/date/{start_date}/{end_date}.json`

**Purpose**: Retrieve resting heart rate (RHR) and heart rate zones for a date range

**Example**: `GET /1/user/-/activities/heart/date/2024-10-01/2024-10-24.json`

**Response**:
```json
{
  "activities-heart": [
    {
      "dateTime": "2024-10-01",
      "value": {
        "restingHeartRate": 62,
        "heartRateZones": [
          { "name": "Out of Range", "minutes": 1200 },
          { "name": "Fat Burn", "minutes": 45, "caloriesOut": 250 },
          { "name": "Cardio", "minutes": 20, "caloriesOut": 180 },
          { "name": "Peak", "minutes": 5, "caloriesOut": 75 }
        ]
      }
    }
  ]
}
```

**Metrics Extracted**:
- Resting Heart Rate (RHR)
- Fat Burn Zone Minutes
- Cardio Zone Minutes
- Peak Zone Minutes

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report** (regardless of date range size!)

---

### 4. Steps Count (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/steps/date/{start_date}/{end_date}.json`

**Purpose**: Daily step counts for date range

**Response**:
```json
{
  "activities-steps": [
    { "dateTime": "2024-10-01", "value": "8542" },
    { "dateTime": "2024-10-02", "value": "12308" }
  ]
}
```

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 5. Weight (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/body/weight/date/{start_date}/{end_date}.json`

**Purpose**: Body weight measurements (manual or from Aria scale)

**Response**:
```json
{
  "body-weight": [
    {
      "dateTime": "2024-10-01",
      "logId": 1234567890,
      "weight": 75.5,
      "bmi": 23.2,
      "source": "Aria"
    }
  ]
}
```

**Note**: Returns kg, app converts to lbs (`kg * 2.20462`)

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 6. SpO2 (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/spo2/date/{start_date}/{end_date}.json`

**Purpose**: Blood oxygen saturation percentage

**Response**:
```json
[
  {
    "dateTime": "2024-10-01",
    "value": {
      "avg": 96.5,
      "min": 94.0,
      "max": 98.0
    }
  }
]
```

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 7. Calories Burned (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/calories/date/{start_date}/{end_date}.json`

**Purpose**: Total daily calories burned (BMR + activity)

**Response**:
```json
{
  "activities-calories": [
    { "dateTime": "2024-10-01", "value": "2450" }
  ]
}
```

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 8. Distance Traveled (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/distance/date/{start_date}/{end_date}.json`

**Purpose**: Daily distance traveled (walking, running, cycling)

**Response**:
```json
{
  "activities-distance": [
    { "dateTime": "2024-10-01", "value": "5.83" }
  ]
}
```

**Note**: Returns km, app converts to miles (`km * 0.621371`)

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 9. Floors Climbed (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/floors/date/{start_date}/{end_date}.json`

**Purpose**: Number of floors (equivalent of 10 feet elevation) climbed

**Response**:
```json
{
  "activities-floors": [
    { "dateTime": "2024-10-01", "value": "12" }
  ]
}
```

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 10. Active Zone Minutes (Range)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/active-zone-minutes/date/{start_date}/{end_date}.json`

**Purpose**: Time spent in fat burn, cardio, or peak heart rate zones

**Response**:
```json
{
  "activities-active-zone-minutes": [
    {
      "dateTime": "2024-10-01",
      "value": {
        "activeZoneMinutes": 45,
        "fatBurnActiveZoneMinutes": 30,
        "cardioActiveZoneMinutes": 12,
        "peakActiveZoneMinutes": 3
      }
    }
  ]
}
```

**Caching**: ✅ Cached in `daily_metrics_cache` table

**Rate Limit Impact**: ✅ **1 call per report**

---

### 11. Cardio Fitness Score / VO2 Max (Range - Limited)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/cardioscore/date/{start_date}/{end_date}.json`

**Purpose**: Estimated VO2 Max (cardio fitness level)

**⚠️ API Limitation**: Maximum 30-day range per request

**Response**:
```json
{
  "cardioScore": [
    {
      "dateTime": "2024-10-01",
      "value": {
        "vo2Max": "42-46"
      }
    }
  ]
}
```

**Special Handling**: 
- API returns ranges (e.g., "42-46")
- App calculates midpoint: `(42 + 46) / 2 = 44`
- Fetches in 30-day chunks for longer date ranges

**Caching**: ✅ Cached in `cardio_fitness_cache` table

**Rate Limit Impact**: ⚠️ **1 call per 30 days** (e.g., 90-day report = 3 calls)

---

### 12. Activities/Exercise Log (List)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/list.json?afterDate={start_date}&sort=asc&offset=0&limit=100`

**Purpose**: Retrieve logged activities/exercises (manual or auto-tracked)

**Response**:
```json
{
  "activities": [
    {
      "activityId": 90009,
      "activityName": "Running",
      "logId": 1234567890,
      "startTime": "2024-10-01T06:30:00.000",
      "duration": 1800000,
      "calories": 250,
      "averageHeartRate": 145,
      "steps": 3500,
      "distance": 5.5,
      "pace": 327.27,
      "speed": 11.0,
      "activeDuration": 1750000,
      "heartRateZones": [...],
      "tcxLink": "https://..."
    }
  ],
  "pagination": {
    "next": "https://..."
  }
}
```

**Metrics Extracted**:
- Activity name, date, duration
- Calories burned
- Average heart rate
- Steps, distance
- Heart rate zones (for drill-down visualization)

**Caching**: ✅ Cached in `activities_cache` table

**Rate Limit Impact**: ✅ **1 call per report** (limit=100 covers most use cases)

**Note**: For users with >100 activities in range, pagination required (not yet implemented)

---

## 🔥 Day-by-Day Endpoints (Rate Limit Heavy)

These endpoints **require one API call per day**, making them "noisy" and rate-limit intensive. Caching is **critical** for these.

### 13. HRV - Heart Rate Variability (Daily)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/hrv/date/{date}.json`

**Purpose**: Heart rate variability (variation in time between heartbeats)

**⚠️ Rate Limit Impact**: **HIGH** - 1 call per day!

**Example**: For 30-day report = **30 API calls**

**Response**:
```json
{
  "hrv": [
    {
      "value": {
        "dailyRmssd": 45.2,
        "deepRmssd": 48.5
      },
      "dateTime": "2024-10-01"
    }
  ]
}
```

**Metric**: `dailyRmssd` (root mean square of successive differences in milliseconds)

**Caching**: ✅ **Cache-first logic** - checks cache before API call

**Implementation**: Parallel fetching with `ThreadPoolExecutor` (max 20 concurrent)

**Why Daily?**: No range endpoint available for HRV

**Future Enhancement**: Intraday HRV endpoint available for per-minute data:
```
GET /1/user/-/hrv/date/{date}/all.json
```

---

### 14. Breathing Rate (Daily)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/br/date/{date}.json`

**Purpose**: Average breathing rate during sleep (breaths per minute)

**⚠️ Rate Limit Impact**: **HIGH** - 1 call per day!

**Response**:
```json
{
  "br": [
    {
      "value": {
        "breathingRate": 15.5
      },
      "dateTime": "2024-10-01"
    }
  ]
}
```

**Caching**: ✅ **Cache-first logic**

**Why Daily?**: No range endpoint available

---

### 15. Skin Temperature (Daily)
**Endpoint**: `GET https://api.fitbit.com/1/user/-/temp/skin/date/{date}.json`

**Purpose**: Skin temperature variation from personal baseline

**⚠️ Rate Limit Impact**: **HIGH** - 1 call per day!

**Response**:
```json
{
  "tempSkin": [
    {
      "value": {
        "nightlyRelative": 0.5
      },
      "logType": "auto_detected",
      "dateTime": "2024-10-01"
    }
  ]
}
```

**Metric**: Relative temperature change (°F or °C from baseline)

**Caching**: ✅ **Cache-first logic**

**Why Daily?**: No range endpoint available

**Devices**: Fitbit Sense, Versa 3, Charge 5+

---

### 16. Sleep Data with Score (Daily)
**Endpoint**: `GET https://api.fitbit.com/1.2/user/-/sleep/date/{date}.json`

**Purpose**: Detailed sleep data including **official Fitbit Sleep Score**

**⚠️ Rate Limit Impact**: **HIGH** - 1 call per day!

**Why Daily?**: 
- Range endpoint (`/sleep/date/{start}/{end}.json`) doesn't include `sleepScore`
- Only daily endpoint provides `sleepScore.overall`

**Response**:
```json
{
  "sleep": [
    {
      "dateOfSleep": "2024-10-01",
      "isMainSleep": true,
      "startTime": "2024-10-01T23:15:00.000",
      "endTime": "2024-10-02T07:30:00.000",
      "duration": 29700000,
      "minutesAsleep": 450,
      "minutesAwake": 45,
      "efficiency": 90,
      "sleepScore": {
        "overall": 80,
        "composition": 25,
        "revitalization": 28,
        "duration": 27
      },
      "levels": {
        "summary": {
          "deep": { "count": 3, "minutes": 105 },
          "light": { "count": 12, "minutes": 250 },
          "rem": { "count": 5, "minutes": 95 },
          "wake": { "count": 18, "minutes": 45 }
        },
        "data": [
          {
            "dateTime": "2024-10-01T23:15:00.000",
            "level": "light",
            "seconds": 1800
          }
        ]
      }
    }
  ]
}
```

**Metrics Extracted**:
- **Sleep Score** (0-100, Fitbit's proprietary metric)
- Sleep stages: Deep, Light, REM, Wake (minutes)
- Sleep efficiency (%)
- Start/end times
- Total sleep duration

**Caching**: ✅ **Cache-first logic** - Core of our caching strategy

**Background Builder**: Prioritizes sleep data for 90-day historical backfill

---

## 🚀 Future Enhancement: Intraday Endpoints

These endpoints provide **minute-by-minute** or **second-by-second** data. Not currently implemented but documented for future drill-down features.

### 17. Intraday Heart Rate
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/heart/date/{date}/1d/1min.json`

**Purpose**: Heart rate every minute for 24 hours

**Use Case**: Detailed workout drill-down showing HR progression over time

**Response Sample**:
```json
{
  "activities-heart": [
    {
      "dateTime": "2024-10-01",
      "value": {
        "restingHeartRate": 62
      }
    }
  ],
  "activities-heart-intraday": {
    "dataset": [
      { "time": "00:00:00", "value": 58 },
      { "time": "00:01:00", "value": 59 },
      { "time": "06:30:00", "value": 145 }
    ],
    "datasetInterval": 1,
    "datasetType": "minute"
  }
}
```

**⚠️ Rate Limit**: 1 call per day (returns full 24 hours)

**Implementation Status**: 🔜 **Planned** for workout drill-down feature

---

### 18. Intraday Steps
**Endpoint**: `GET https://api.fitbit.com/1/user/-/activities/steps/date/{date}/1d/1min.json`

**Purpose**: Step count per minute

**Use Case**: Activity timeline visualization

**Implementation Status**: 🔜 **Planned**

---

### 19. Intraday Sleep Levels
**Already Available** in Sleep Data (Daily) response under `levels.data` array

**Data**: Second-by-second sleep stage transitions

**Use Case**: Sleep timeline visualization (currently partially implemented)

**Example**:
```json
{
  "levels": {
    "data": [
      { "dateTime": "2024-10-01T23:15:00.000", "level": "light", "seconds": 1800 },
      { "dateTime": "2024-10-01T23:45:00.000", "level": "deep", "seconds": 3600 },
      { "dateTime": "2024-10-02T00:45:00.000", "level": "rem", "seconds": 2400 }
    ]
  }
}
```

**Implementation Status**: ⚠️ **Partially implemented** - data available but visualization is simplified

**Future Enhancement**: Full timeline chart like Fitbit app

---

## 📊 API Call Summary by Report Type

### Standard Report (e.g., 7-day range, cached):

| Endpoint | Calls | Notes |
|----------|-------|-------|
| User Profile | 2 | Validation + details |
| Heart Rate (Range) | 1 | All 7 days in one call |
| Steps (Range) | 1 | All 7 days |
| Weight (Range) | 1 | All 7 days |
| SpO2 (Range) | 1 | All 7 days |
| Calories (Range) | 1 | All 7 days |
| Distance (Range) | 1 | All 7 days |
| Floors (Range) | 1 | All 7 days |
| AZM (Range) | 1 | All 7 days |
| Cardio Fitness (Range) | 1 | All 7 days |
| Activities (List) | 1 | All activities |
| **HRV (Daily)** | 0 | ✅ Served from cache |
| **Breathing (Daily)** | 0 | ✅ Served from cache |
| **Temperature (Daily)** | 0 | ✅ Served from cache |
| **Sleep (Daily)** | 0 | ✅ Served from cache |
| **TOTAL** | **~12 calls** | **Mostly efficient!** |

### First-Time Report (30 days, no cache):

| Endpoint | Calls | Notes |
|----------|-------|-------|
| Range-based metrics | 11 | One call each |
| **HRV (Daily)** | 30 | ⚠️ Day-by-day |
| **Breathing (Daily)** | 30 | ⚠️ Day-by-day |
| **Temperature (Daily)** | 30 | ⚠️ Day-by-day |
| **Sleep (Daily)** | 30 | ⚠️ Day-by-day |
| **TOTAL** | **~131 calls** | **Cache critical!** |

### Second Report (Same 30 days, with cache):

| Endpoint | Calls | Notes |
|----------|-------|-------|
| Range-based metrics | 11 | Still need fresh data |
| **Advanced metrics** | 0 | ✅ All from cache |
| **TOTAL** | **~11 calls** | **92% reduction!** |

---

## 💾 Caching Strategy

### Cache Tables:

1. **`sleep_cache`**: Sleep scores, stages, efficiency, times
2. **`advanced_metrics_cache`**: HRV, breathing rate, temperature
3. **`daily_metrics_cache`**: RHR, steps, weight, SpO2, calories, distance, floors, AZM, zones
4. **`cardio_fitness_cache`**: VO2 Max scores
5. **`activities_cache`**: Exercise/workout logs

### Cache Logic:

**Sleep & Advanced Metrics**: **Cache-First**
```python
# Check cache first
cached_data = cache.get_sleep_data(date)
if cached_data:
    # Use cached data, 0 API calls
else:
    # Fetch from API, then cache
```

**Daily Metrics**: **Cache-on-Write**
```python
# Fetch from API (using efficient range endpoint)
response = api.get_heart_rate(start_date, end_date)

# Cache immediately after fetching
for entry in response:
    cache.set_daily_metrics(date=entry['date'], rhr=entry['rhr'])
```

**Background Builder**: Manual trigger via "Start Cache" button
- Backfills last 90 days of sleep data
- Respects rate limits (pauses on 429 errors)
- Runs continuously until container restart

---

## 🔍 Rate Limit Error Handling

**Error Response** (429 Too Many Requests):
```json
{
  "error": {
    "code": 429,
    "message": "Resource has been exhausted (e.g. check quota).",
    "status": "RESOURCE_EXHAUSTED"
  }
}
```

**Handling**:
1. **Report Generation**: Show user-friendly error, suggest waiting 1 hour
2. **Background Builder**: Pause for 1 hour, then resume
3. **Cache Population**: Return -1 to signal rate limit

**Prevention**:
- Cache-first for noisy endpoints (HRV, BR, Temp, Sleep)
- Parallel fetching with reasonable concurrency (max 20 threads)
- Background builder with 30-second delays between batches

---

## 📋 Endpoint Quick Reference

### Efficient (Range-Based):
✅ Heart Rate & Zones  
✅ Steps  
✅ Weight  
✅ SpO2  
✅ Calories  
✅ Distance  
✅ Floors  
✅ Active Zone Minutes  
⚠️ Cardio Fitness (30-day max)  
✅ Activities List  

### Rate-Limit Heavy (Daily):
🔥 HRV  
🔥 Breathing Rate  
🔥 Temperature  
🔥 Sleep with Score  

### Not Yet Implemented:
🔜 Intraday Heart Rate (1-minute resolution)  
🔜 Intraday Steps (1-minute resolution)  
⚠️ Intraday Sleep (data available but not fully visualized)  

---

## 🎯 Recommendations

1. **Always use cache** for historical data
2. **Only refresh "today"** to get latest data
3. **Background builder** should run once after deploy, then rely on cache
4. **Future enhancements** should use intraday endpoints for detailed drill-downs
5. **Monitor rate limits** via API response headers (not currently tracked)
6. **Consider pagination** for Activities endpoint if users have >100 activities

---

## 📚 Additional Resources

- [Fitbit Web API Documentation](https://dev.fitbit.com/build/reference/web-api/)
- [OAuth 2.0 Flow](https://dev.fitbit.com/build/reference/web-api/oauth2/)
- [Rate Limits](https://dev.fitbit.com/build/reference/web-api/developer-guide/application-design/#Rate-Limits)
- [Intraday Time Series](https://dev.fitbit.com/build/reference/web-api/intraday/)

---

**Document Version**: 1.0  
**App Version**: 2.0.0-cache  
**Last Reviewed**: October 25, 2025

