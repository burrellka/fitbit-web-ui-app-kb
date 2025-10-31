# Fitbit Wellness Report Web UI - Enhanced Edition

> **🔐 SECURITY UPDATE**: This app now uses a **dual-port architecture** for maximum security:
> - **Port 5032**: Public OAuth callback (no health data exposed)
> - **Port 5033**: Password-protected dashboard (internal only)
> 
> **📖 Setup Guide**: See [QUICK_START_SECURITY.md](QUICK_START_SECURITY.md) for deployment instructions.

---

## 📊 Metrics Overview

### Standard Metrics (Always Included)
**API Cost: ~15 calls per report** - Well within Fitbit's 150 requests/hour limit

- ❤️ **Heart Rate & Resting Heart Rate** - Daily and historical trends
- 👟 **Steps** - Daily step count with heatmap visualization
- ⚖️ **Weight** - Body weight tracking (with kg → lbs conversion)
- 🫁 **SpO2 (Blood Oxygen)** - Oxygen saturation levels
- 😴 **Sleep Analysis** - Sleep stages, duration, and regularity
- 🏃 **Cardio Fitness Score (VO2 Max)** - Cardiovascular fitness
- ⚡ **Active Zone Minutes** - Heart rate zone activity tracking
- 🔥 **Calories Burned** - Daily energy expenditure
- 📏 **Distance Traveled** - Daily distance (with km → miles conversion)
- 🏢 **Floors Climbed** - Elevation gain tracking
- 💪 **Exercise Log** - Detailed workout history with HR zones

### Advanced Metrics (Optional - Checkbox Required)
**API Cost: ~3 calls per day of data** (e.g., 79-day report = 237 additional calls ⚠️)

- 💓 **Heart Rate Variability (HRV)** - Stress and recovery indicator
- 🌬️ **Breathing Rate** - Respiratory rate during sleep
- 🌡️ **Skin Temperature** - Temperature variation tracking

> **⚠️ Important:** Advanced metrics require one API call per day, which can quickly exhaust Fitbit's 150 requests/hour limit. Keep them **OFF** by default and only enable for shorter date ranges (≤30 days) when needed.

---

## 🎉 Enhanced Features

### Core Enhancements
- ✅ **OAuth 2.0 Authentication** - No more manual token entry!
- ✅ **Automatic Token Refresh** - Tokens refresh automatically before expiration
- ✅ **Seamless Login Experience** - One-click Fitbit login
- ✅ **Session Management** - Secure token storage during your session
- ✅ **API Rate Limit Protection** - Smart API usage with optional advanced metrics
- ✅ **Unit Conversions** - Automatic kg→lbs and km→miles conversions
- ✅ **Graceful Error Handling** - Clear rate limit warnings and error messages

### Advanced Analytics Features 🆕

#### 🏋️ Exercise Analysis
- **Exercise Log** - View all workouts with comprehensive metrics
- **Enhanced Exercise Metrics** - Duration, active duration, calories, average HR, steps, and distance for each workout
- **Activity Type Auto-Detection** - Automatically categorizes all logged exercises
- **CSV Export** - Download exercise history for external analysis

#### 😴 Sleep Quality Analysis - 3-Tier Scoring System 🆕
- **Reality Score (Primary)** - Honest, aggressive assessment of sleep quality that matches felt experience
- **Proxy Score** - Calibrated to approximate official Fitbit app scores for comparison
- **Efficiency %** - Raw API metric (time asleep / time in bed)
- **3-Line Visualization** - Compare all three metrics simultaneously on one chart
- **Validated Accuracy** - Reality Score matches user's Fitbit app (Oct 22: Reality=80, App=80 ✅)
- **Sleep Stage Distribution** - Beautiful pie chart showing Deep, Light, REM, and Wake time percentages
- **Sleep Consistency Tracking** - Monitor your sleep regularity over time

> **📘 Why Custom Scores?** The official Fitbit Sleep Score is NOT available via Personal OAuth apps. Our calculated scores use the same raw data (sleep stages, duration, fragmentation) with validated formulas. See [Technical Documentation](FITBIT_API_TECHNICAL_DOCUMENTATION.md#-custom-sleep-score-calculation-system) for details.

#### 🔗 Exercise-Sleep Correlations
- **AI-Powered Insights** - Discover how your workouts impact your sleep quality
- **Correlation Analysis** - Statistical analysis showing relationship between exercise and next-day sleep
- **Personalized Recommendations** - Smart suggestions for optimal workout timing
- **Interactive Scatter Plots** - Visualize exercise intensity vs. sleep duration with trendlines
- **Comparative Metrics** - See average sleep on workout days vs. rest days

### 🚀 Intelligent Caching System 🆕
- **Hourly Background Builder** - Automatically fetches and caches historical data every hour
- **3-Phase Strategy** - Efficiently fills 365 days of data using ~125 API calls per hour
- **Real-Time Today Refresh** - Always fetches fresh data for today when generating reports or via API
- **Zero API Calls for Historical Data** - Serves past dates from cache instantly
- **Smart Cache Management** - Manual "Start Cache" button, "Flush Cache" for troubleshooting
- **Persistent Storage** - SQLite database survives container restarts
- **Cache Status Display** - Real-time visibility into what's cached and what's building
- **Cache Log Viewer** 🆕 - Interactive page (`/cache-log`) to inspect cached data
  - View cached data for any date range
  - Filter by metric type (Daily, Sleep, Advanced, Activities, Cardio)
  - Download as text file or **CSV export for Excel** 📊
  - Perfect for data analysis, backups, and sharing with healthcare providers

### 🔌 MCP Server Ready 🆕
- **RESTful API Endpoints** - Full API for LLM integration via Model Context Protocol (MCP)
- **Smart Data Retrieval** - GET endpoints for sleep, metrics, exercise, and activities
- **Cache Management API** - Endpoints for cache status, flush, and manual refresh
- **Today Auto-Refresh** - API automatically refreshes today's data for real-time stats
- **Authentication Preserved** - Uses stored OAuth tokens for secure API access

## Try it out

[Demo website on Render](https://fitbit-api-web-ui.onrender.com/) or [Self Hosted on my Server](https://fitbit-report.arpan.app/) (Use this if the Render page is down)

## 📸 Screenshots - Enhanced Edition

### Dashboard Overview
![Dashboard Header and Core Metrics](help/enhanced-screenshots/Fitbit%20Wellness%20Report-images-0.jpg)

### Sleep Analysis & Exercise Tracking
![Sleep Quality and Exercise Log](help/enhanced-screenshots/Fitbit%20Wellness%20Report-images-1.jpg)

### Advanced Metrics (HRV, Breathing Rate, Temperature)
![Advanced Health Metrics](help/enhanced-screenshots/Fitbit%20Wellness%20Report-images-2.jpg)

### Correlation Analysis & Insights
![Exercise-Sleep Correlations](help/enhanced-screenshots/Fitbit%20Wellness%20Report-images-3.jpg)

### Detailed Visualizations
![Charts and Trends](help/enhanced-screenshots/Fitbit%20Wellness%20Report-images-4.jpg)

> **Note**: These screenshots showcase the enhanced edition with OAuth, caching, and MCP API integration. Your actual dashboard may vary based on your data and configuration.

## Self-Hosting with Docker Compose (OAuth Enabled)

### Prerequisites

1. **Create a Fitbit Application** at [https://dev.fitbit.com/apps](https://dev.fitbit.com/apps)
   - Application Type: `Personal`
   - OAuth 2.0 Application Type: `Server`
   - Callback URL: Your server URL (e.g., `https://fitbitkb.yourdomain.com/` for production with HTTPS)
   - Note your `Client ID` and `Client Secret`
   - **Note:** The app automatically requests all necessary permissions (Profile, Activity, Heart Rate, Sleep, Weight, SpO2, Breathing Rate, Temperature, Location)

### Setup Instructions

1. **Clone this repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/fitbit-web-ui-app-kb.git
   cd fitbit-web-ui-app-kb
   ```

2. **Create environment file**
   ```bash
   cp env.example .env
   ```

3. **Edit `.env` file** with your Fitbit app credentials:
   ```bash
   CLIENT_ID=your_client_id_here
   CLIENT_SECRET=your_client_secret_here
   REDIRECT_URL=http://192.168.x.x:5032/
   DASHBOARD_PASSWORD=your_secure_password_here
   API_KEY=your_secure_api_key_here
   ```
   > **Note**: Replace `192.168.x.x:5032` with your actual server IP and port
   > 
   > **Security**: 
   > - `DASHBOARD_PASSWORD`: Protects the web dashboard (optional but recommended)
   > - `API_KEY`: Secures API endpoints for MCP/external access (optional - if not set, API is unprotected)

4. **Start the application**
   ```bash
   docker-compose up -d
   ```

5. **Access the app** at `http://192.168.13.5:5032/` (or your configured URL)

6. **Login** - Click "Login to FitBit" button and authorize the app

### Docker Compose Configuration

```yaml
services:
  fitbit-ui:
    build: .
    container_name: fitbit-report-app
    ports:
      - "5032:80"
    restart: unless-stopped
    environment:
      - CLIENT_ID=${CLIENT_ID}
      - CLIENT_SECRET=${CLIENT_SECRET}
      - REDIRECT_URL=${REDIRECT_URL:-http://192.168.13.5:5032/}
      - DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD:-}
      - API_KEY=${API_KEY:-}
      - TZ=America/New_York  # Set your timezone (e.g., America/Chicago, America/Los_Angeles, Europe/London)
```

## Legacy Self-Hosting (Manual Token Entry)

If you prefer the original manual token entry method:

```yaml
services:
    fitbit-ui:
        image: 'thisisarpanghosh/fitbit-report-app:latest'
        container_name: 'fitbit-report-app'
        ports:
            - "5000:80"
        restart: unless-stopped
```

[How to get ACCESS TOKEN](https://github.com/arpanghosh8453/fitbit-web-ui-app/blob/main/help/GET_ACCESS_TOKEN.md)

---

## 🙏 Credits & Attribution

### Original Project
This enhanced edition is built upon the excellent foundation created by **[@arpanghosh8453](https://github.com/arpanghosh8453/fitbit-web-ui-app)**. 

**Huge thanks to Arpan Ghosh** for:
- Creating the original Fitbit Web UI application
- Implementing the core visualization framework
- Establishing the Fitbit API integration patterns
- Open-sourcing the project for the community

Special thanks to [@dipanghosh](https://github.com/dipanghosh) for contributions to sleep schedule analysis and aesthetics in the original project.

---

## 🔧 Recent Updates & Bug Fixes

### 🎉 New Features (October 30, 2025)
**📊 CSV Export for Cache Data**
- Added CSV export button to Cache Log Viewer (`/cache-log`)
- Excel-compatible format with dynamic columns based on selected metrics
- Perfect for creating custom charts, long-term trend analysis, and data sharing
- Exports all metrics: Daily (Steps, Weight, HR, etc.), Sleep, HRV, Activities, and more

**🔇 Reduced Log Verbosity**
- Disabled per-metric caching debug logs (CACHE_DEBUG and CACHE_VERIFY)
- Reduces log spam by ~90% while keeping critical error messages
- Makes troubleshooting easier by highlighting actual issues

### 🐛 Weight & Body Fat Fix (October 30, 2025)
**Symptom**: Weight and Body Fat % showed as `None` in all reports and cache logs, despite having data in Fitbit app.

**Root Cause**: App was calling the wrong Fitbit API endpoint (`/body/weight/` instead of `/body/log/weight/`), causing the API to return a different JSON structure that the parsing logic couldn't handle.

**Fix**: Updated weight endpoint to `/body/log/weight/` in both Phase 1 fetch and retry logic. Also added enhanced headers showing most recent vs. earliest weight/body fat with change indicators.

**Impact**: Weight and Body Fat data now cache and display correctly with beautiful trend charts.

---

### 🐛 Comprehensive Per-Metric Caching Fix (October 28, 2025)
**Symptom**: Even after previous fixes, some metrics remained fragmented. For example, Steps might be 100% cached but Calories only 80% cached, despite both being Phase 1 metrics.

**Root Cause**: Gemini identified that the fragmentation bug affected **ALL 12+ metrics**, not just the 4 Phase 3 metrics (Sleep, HRV, BR, Temp). If any Phase 1 metric (Steps, Calories, Distance, Floors, AZM, RHR, Weight, SpO2) failed during initial fetch, the builder would never retry it.

**The Fix - Three Parts:**

1. **`cache_manager.py` - Universal Per-Metric Checking**: Added individual SQL checks for ALL metrics (steps, calories, distance, floors, azm, heartrate, weight, spo2, cardio_fitness, activities, hrv, breathing_rate, temperature, sleep). Each uses `WHERE metric_column IS NOT NULL` to accurately detect missing dates.

2. **`app.py` - Phase 1 Retry Loop**: Added intelligent retry logic after Phase 1 completes. The builder now:
   - Checks EACH Phase 1 metric individually for missing dates
   - Re-fetches only the metrics that have gaps
   - Runs every hourly cycle until ALL metrics are 100% cached

3. **`app.py` - Phase 3 Per-Metric (Already Implemented)**: Each Phase 3 metric (Sleep, HRV, Breathing Rate, Temperature) is fetched independently with its own missing date check.

**Impact**: Eliminates ALL cache fragmentation. Every metric is now tracked and filled independently, ensuring eventual 100% cache completion for all metrics.

---

### 🐛 NULL Overwrites Fix (October 28, 2025)
**Symptom**: Fragmented metrics - Oct 5-10 had some data, Oct 20-26 had different data, Active Zone Minutes stopped on Oct 19, exercise log empty.

**Root Cause**: Phase 1 cache builder iterated over ALL 365 days, but Fitbit API only returns dates with data. When a metric had no data for a date, the function skipped caching, but subsequent metric UPSERTs would overwrite previously cached values with NULL.

**Fix**: Modified `process_and_cache_daily_metrics()` to extract dates directly from API response instead of iterating over a fixed 365-day list. Now only caches dates that the API actually returned.

**Impact**: Eliminates fragmented data across date ranges.

---

### 🐛 Heart Rate Zones Fix (October 27, 2025)
**Symptom**: Cache logs showed "✅ Cached 366 days of heart rate data" but HR zones were empty in reports.

**Root Cause**: Phase 1 cache builder only cached Resting Heart Rate, not the Fat Burn/Cardio/Peak zone minutes.

**Fix**: Expanded `heartrate` handler to extract and cache all HR zone data (Fat Burn at index 1, Cardio at index 2, Peak at index 3).

**Impact**: HR zones now populate correctly from cache.

---

### Enhanced Edition Philosophy

This fork represents a **fundamental architectural shift** from the original project:

**Original Approach**: Manual token entry, on-demand API fetching, no persistence  
**Enhanced Approach**: OAuth 2.0 authentication, intelligent caching, background data population, MCP API integration

#### Key Enhancements:
- 🔐 **OAuth 2.0 & Automatic Token Refresh** - Seamless authentication without manual token management
- 💾 **SQLite Caching System** - Persistent data storage with 3-phase hourly background builder
- 🚀 **Real-Time Today Refresh** - Always fetch fresh data for today while serving history from cache
- 🔌 **MCP Server Integration** - RESTful API for LLM-powered insights via Model Context Protocol
- 🔒 **Dual-Port Security** - Separate OAuth callback and password-protected dashboard
- 📊 **Advanced Analytics** - Exercise-sleep correlations, sleep timeline visualizations, drill-down features
- ⏰ **Smart Rate Limit Management** - 3-phase caching strategy respects Fitbit's 150 calls/hour limit

---

## ⚠️ Important Disclaimer

### Code Authorship
While maintained by **[@burrellka](https://github.com/burrellka)** (a business transformation and process architect), **all code in this enhanced edition was written by Cursor AI** (Claude Sonnet 4.5) to meet specific personal homelab requirements.

**What this means:**
- ✅ This is a **production-grade personal project** running on my homelab
- ✅ Code is **thoroughly tested** for my use case (TrueNAS/Dockge deployment)
- ⚠️ **No professional software engineering review** has been performed
- ⚠️ **Use at your own risk** - this is hobby-grade code, not enterprise software
- ⚠️ **Security considerations** are implemented but not audited by security professionals

### Support & Maintenance
**This is a personal project with no warranty or guaranteed support.**

**If you encounter issues:**
1. Check the comprehensive documentation (README, DEPLOYMENT_GUIDE.md, API_DOCUMENTATION.md)
2. Review troubleshooting sections in the deployment guide
3. Open a GitHub issue with detailed logs and reproduction steps
4. **Best effort support** - I'll help when time permits, but this is a hobby project

**Contributions Welcome!**
- 🍴 **Fork freely** - Modify, enhance, and adapt to your needs
- 🐛 **Bug reports** - Issues with clear reproduction steps are appreciated
- 💡 **Feature requests** - Share your ideas, but no guarantees on implementation
- 🔧 **Pull requests** - Contributions are welcome, but review may be slow

### Open Source License
This project inherits its license from the original work. See [LICENSE](LICENSE) for details.

**You are free to:**
- ✅ Use this software for personal or commercial purposes
- ✅ Modify and distribute your own versions
- ✅ Use it as a learning resource or starting point for your projects

**With the understanding that:**
- ❌ No warranty or support is provided
- ❌ The authors are not liable for any damages or issues
- ❌ You use this software at your own risk

---

## 📚 Additional Documentation

- **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)** - Comprehensive deployment and troubleshooting guide
- **[API_DOCUMENTATION.md](API_DOCUMENTATION.md)** - REST API reference for MCP integration
- **[FITBIT_API_TECHNICAL_DOCUMENTATION.md](FITBIT_API_TECHNICAL_DOCUMENTATION.md)** - Detailed Fitbit API usage patterns
- **[SECURITY_SETUP.md](SECURITY_SETUP.md)** - Dual-port security architecture guide
- **[GET_ACCESS_TOKEN.md](help/GET_ACCESS_TOKEN.md)** - Fitbit Developer App setup

---

## 🎯 Use Cases

**Perfect for:**
- 📊 Personal health data visualization without Fitbit Premium subscription
- 🏠 Homelab enthusiasts running Docker/TrueNAS/Dockge
- 🤖 LLM integration for AI-powered health insights via MCP
- 📈 Long-term health trend analysis with 365 days of cached data
- 🔬 Developers learning Fitbit API integration patterns

**Not suitable for:**
- 🏥 Medical or clinical use (this is hobby-grade software)
- 🏢 Enterprise deployments requiring professional support
- 👥 Multi-user scenarios (designed for single-user personal use)
- 🔐 High-security environments requiring professional security audits

---

## 🌟 Philosophy

This project embodies the spirit of **personal data ownership** and **open-source collaboration**. Rather than paying for Fitbit Premium, this tool empowers you to:
- Own your health data
- Visualize it however you want
- Integrate it with AI for personalized insights
- Learn from the code and adapt it to your needs

If this project helps you, **pay it forward**:
- ⭐ Star the repo to boost visibility
- 🍴 Fork and share your enhancements
- 📖 Improve the documentation
- 🤝 Help others in GitHub issues

---

**Built with curiosity, powered by AI, maintained with hobby-grade enthusiasm.** 🚀