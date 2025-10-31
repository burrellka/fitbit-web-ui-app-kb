# Fitbit Wellness API Documentation

## REST API Endpoints for MCP Server Integration

This document describes the REST API endpoints available for integrating with MCP (Model Context Protocol) servers and other applications.

---

## Base URL

When running locally or in Docker:
```
http://localhost:5032
```

When deployed with HTTPS:
```
https://your-domain.com
```

---

## Configuration

### Timezone Setup

To ensure accurate timestamps in logs and cache status, set the `TZ` environment variable in your `docker-compose.yml`:

```yaml
environment:
  - TZ=America/New_York  # or your timezone
```

**Common Timezones:**
- `America/New_York` (EST/EDT)
- `America/Chicago` (CST/CDT)
- `America/Los_Angeles` (PST/PDT)
- `Europe/London` (GMT/BST)
- `Asia/Tokyo` (JST)

Without this setting, the container defaults to UTC, which may cause confusion when viewing cache status timestamps.

### Authentication

All API endpoints (except `/api/health`) require:
1. **OAuth Session**: An active OAuth session with stored refresh tokens (created via web dashboard login)
2. **API Key** (Recommended): Set the `API_KEY` environment variable to protect endpoints

#### API Key Setup

To secure your API endpoints, set an API key in your environment:

```bash
# In .env file
API_KEY=your_secure_random_key_here
```

```yaml
# In docker-compose.yml
environment:
  - API_KEY=${API_KEY:-}
```

#### Making Authenticated Requests

Include the API key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: your_secure_random_key_here" \
     http://localhost:5032/api/data/range?start=2025-10-01&end=2025-10-31
```

```python
import requests

headers = {
    'X-API-Key': 'your_secure_random_key_here'
}

response = requests.get(
    'http://localhost:5032/api/data/range',
    params={'start': '2025-10-01', 'end': '2025-10-31'},
    headers=headers
)
```

**Security Notes:**
- If `API_KEY` is not set, endpoints are **unprotected** (backward compatibility)
- A warning will appear in logs if API_KEY is not configured
- For production/public deployments, **always set API_KEY**
- Use a strong, random key (e.g., generated via `openssl rand -hex 32`)

---

## Endpoints

### 1. Health Check

**GET** `/api/health`

Check if the API is running and healthy.

**Response:**
```json
{
  "success": true,
  "status": "healthy",
  "app": "Fitbit Wellness Enhanced",
  "version": "2.0.0-cache"
}
```

**Example:**
```bash
curl http://localhost:5032/api/health
```

---

### 2. Cache Status

**GET** `/api/cache/status`

Get statistics about the current cache state.

**Response:**
```json
{
  "success": true,
  "cache_stats": {
    "sleep_records": 83,
    "sleep_date_range": "2025-08-01 to 2025-10-22",
    "advanced_records": 0,
    "advanced_date_range": "No data"
  },
  "builder_running": false
}
```

**Example:**
```bash
curl http://localhost:5032/api/cache/status
```

---

### 3. Get Sleep Data

**GET** `/api/data/sleep/<date>`

Retrieve sleep data for a specific date.

**🔄 Real-Time Behavior:** If `date` is today, automatically refreshes data from Fitbit API before returning, ensuring real-time stats.

**Parameters:**
- `date` (path): Date in YYYY-MM-DD format (e.g., `2025-10-22` or `today`)

**Response (Success):**
```json
{
  "success": true,
  "date": "2025-10-22",
  "data": {
    "sleep_score": 79,
    "efficiency": 92,
    "total_sleep": 445,
    "deep": 120,
    "light": 230,
    "rem": 95,
    "wake": 25,
    "start_time": "2025-10-22T23:30:00",
    "sleep_data_json": "..."
  }
}
```

**Response (Not Found):**
```json
{
  "success": false,
  "message": "No sleep data found for 2025-10-22"
}
```

**Example:**
```bash
curl http://localhost:5032/api/data/sleep/2025-10-22
```

---

### 4. Get Exercise Data

**GET** `/api/data/exercise/<date>`

Retrieve exercise/activity data for a specific date.

**Parameters:**
- `date` (path): Date in YYYY-MM-DD format

**Response (Success):**
```json
{
  "success": true,
  "date": "2025-10-22",
  "activities": [
    {
      "activity_name": "Run",
      "duration_ms": 1800000,
      "duration_min": 30,
      "calories": 250,
      "avg_heart_rate": 145,
      "steps": 3500,
      "distance": 5.2,
      "distance_mi": 3.23,
      "start_time": "2025-10-22T07:00:00.000",
      "active_duration": 1750000
    }
  ],
  "count": 1
}
```

**Response (No Activities):**
```json
{
  "success": true,
  "date": "2025-10-22",
  "activities": [],
  "count": 0
}
```

**Example:**
```bash
curl http://localhost:5032/api/data/exercise/2025-10-22
```

---

### 5. Get All Metrics

**GET** `/api/data/metrics/<date>`

Retrieve all metrics (sleep, HRV, breathing rate, temperature, daily metrics) for a specific date.

**🔄 Real-Time Behavior:** If `date` is today, automatically refreshes sleep data from Fitbit API before returning, ensuring real-time stats.

**Parameters:**
- `date` (path): Date in YYYY-MM-DD format (e.g., `2025-10-22` or `today`)

**Response:**
```json
{
  "success": true,
  "date": "2025-10-22",
  "sleep": {
    "sleep_score": 79,
    "efficiency": 92,
    "total_sleep": 445,
    ...
  },
  "advanced_metrics": {
    "hrv": 45.2,
    "breathing_rate": 14.5,
    "temperature": 36.7
  }
}
```

**Example:**
```bash
curl http://localhost:5032/api/data/metrics/2025-10-22
```

---

### 5. Refresh Cache for Date

**POST** `/api/cache/refresh/<date>`

Force refresh cached data for a specific date by fetching from Fitbit API.

**Parameters:**
- `date` (path): Date in YYYY-MM-DD format

**Headers:**
- `Authorization: Bearer <your_fitbit_access_token>`

**Response:**
```json
{
  "success": true,
  "message": "Refreshed cache for 2025-10-22",
  "records_updated": 1
}
```

**Example:**
```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  http://localhost:5032/api/cache/refresh/2025-10-22
```

---

### 6. Cache Log Viewer (JSON)

**GET** `/api/cache-log?start=<start_date>&end=<end_date>&metrics=<metric_list>`

Generate a human-readable cache report for the specified date range and metrics.

**Parameters:**
- `start` (query): Start date in YYYY-MM-DD format
- `end` (query): End date in YYYY-MM-DD format  
- `metrics` (query): Comma-separated list of metrics to include
  - Options: `daily`, `sleep`, `advanced`, `cardio`, `activities`
  - Default: `daily,sleep,advanced,cardio,activities`

**Response:**
```json
{
  "success": true,
  "report": "================================================================================\nFITBIT CACHE REPORT: 2025-10-20 to 2025-10-25\n...\n"
}
```

**Example:**
```bash
curl "http://localhost:5032/api/cache-log?start=2025-10-20&end=2025-10-25&metrics=daily,sleep"
```

---

### 7. Export Cache Data (CSV) 🆕

**GET** `/api/cache-csv?start=<start_date>&end=<end_date>&metrics=<metric_list>`

Export cached data as CSV file for Excel/spreadsheet analysis.

**Parameters:**
- `start` (query): Start date in YYYY-MM-DD format
- `end` (query): End date in YYYY-MM-DD format
- `metrics` (query): Comma-separated list of metrics to include
  - Options: `daily`, `sleep`, `advanced`, `cardio`, `activities`
  - Default: `daily,sleep,advanced,cardio,activities`

**Response:**
- Content-Type: `text/csv`
- Filename: `fitbit-cache-export-YYYY-MM-DD.csv`
- Dynamic columns based on selected metrics

**CSV Column Headers:**
- **Daily Metrics**: Date, Steps, Calories, Distance (mi), Floors, Active Zone Min, Resting HR, Fat Burn Min, Cardio Min, Peak Min, Weight (lbs), Body Fat %, SpO2, EOV
- **Sleep Metrics**: Sleep Reality Score, Sleep Proxy Score, Sleep Efficiency %, Deep Sleep (min), Light Sleep (min), REM Sleep (min), Wake Time (min), Total Sleep (min)
- **Advanced Metrics**: HRV (ms), Breathing Rate (bpm), Temperature (°F)
- **Cardio Metrics**: VO2 Max
- **Activities**: Activities Count, Activities Summary

**Example:**
```bash
# Download all metrics for date range
curl "http://localhost:5032/api/cache-csv?start=2025-10-01&end=2025-10-31" -o fitbit-export.csv

# Download only daily and sleep metrics
curl "http://localhost:5032/api/cache-csv?start=2025-10-20&end=2025-10-25&metrics=daily,sleep" -o fitbit-sleep.csv
```

**Use Cases:**
- 📊 Create custom charts in Excel/Google Sheets
- 📈 Long-term trend analysis
- 💾 External data backups
- 🏥 Share with healthcare providers
- 🔬 Statistical analysis and correlations

---

### 8. Flush Cache (Not Yet Implemented)

**POST** `/api/cache/flush`

Clear the entire cache database.

**Response:**
```json
{
  "success": false,
  "message": "Cache flush not yet implemented. Delete data_cache.db file to manually clear cache."
}
```

**Manual Workaround:**
```bash
# Stop the container
docker stop fitbit-report-app-enhanced

# Delete the cache database
rm ./data_cache.db

# Restart the container
docker start fitbit-report-app-enhanced
```

---

## MCP Server Integration

These endpoints are designed to be used with MCP (Model Context Protocol) servers, allowing LLMs to:

1. **Query your fitness data** - Retrieve sleep, HRV, and other metrics
2. **Monitor cache status** - Check what data is available
3. **Refresh data** - Trigger updates for specific dates
4. **Provide insights** - Analyze trends and patterns in your data

### Example MCP Server Use Cases

1. **Daily Health Summary:**
   ```
   GET /api/data/metrics/2025-10-22
   ```
   LLM analyzes sleep quality, HRV, and provides personalized recommendations

2. **Trend Analysis:**
   ```
   GET /api/data/sleep/2025-10-15
   GET /api/data/sleep/2025-10-16
   GET /api/data/sleep/2025-10-17
   ...
   ```
   LLM identifies patterns in sleep scores over time

3. **Cache Management:**
   ```
   GET /api/cache/status
   ```
   LLM checks if data is available before querying

---

## Error Responses

All endpoints return consistent error responses:

```json
{
  "success": false,
  "error": "Error message here"
}
```

**Common HTTP Status Codes:**
- `200` - Success
- `401` - Unauthorized (missing/invalid token)
- `404` - Resource not found
- `500` - Internal server error
- `501` - Not implemented

---

## Authentication

Most read endpoints (GET) do not require authentication.

Write/modify endpoints (POST) require a valid Fitbit OAuth access token:
```
Authorization: Bearer YOUR_ACCESS_TOKEN
```

You can obtain this token through the web UI login flow.

---

## Rate Limiting

The API itself has no rate limits, but remember that:
- The background cache builder respects Fitbit's API limits
- Forced refresh operations consume Fitbit API quota
- Cache hits are instant and don't consume API calls

---

## Future Enhancements

Planned API additions:
- ✅ GET `/api/data/sleep/<date>` - Implemented (with today auto-refresh)
- ✅ GET `/api/data/metrics/<date>` - Implemented (with today auto-refresh)
- ✅ GET `/api/data/exercise/<date>` - Implemented
- ✅ GET `/api/cache/status` - Implemented
- ✅ POST `/api/cache/flush` - Implemented
- ✅ POST `/api/cache/refresh/<date>` - Implemented
- ⏳ GET `/api/insights/summary` - Planned (AI-generated insights)
- ⏳ GET `/api/trends/sleep` - Planned (Sleep trend analysis)
- ⏳ GET `/api/trends/fitness` - Planned (Fitness trend analysis)

---

## Support

For issues or questions:
- Check the main [README.md](README.md)
- Review [ENHANCEMENT_ROADMAP.md](ENHANCEMENT_ROADMAP.md)
- Open an issue on GitHub

