# Fitbit Wellness Report Web UI - Enhanced Edition

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

This fork includes the following enhancements:
- ✅ **OAuth 2.0 Authentication** - No more manual token entry!
- ✅ **Automatic Token Refresh** - Tokens refresh automatically before expiration
- ✅ **Seamless Login Experience** - One-click Fitbit login
- ✅ **Session Management** - Secure token storage during your session
- ✅ **API Rate Limit Protection** - Smart API usage with optional advanced metrics
- ✅ **Unit Conversions** - Automatic kg→lbs and km→miles conversions
- ✅ **Graceful Error Handling** - Clear rate limit warnings and error messages

## Try it out

[Demo website on Render](https://fitbit-api-web-ui.onrender.com/) or [Self Hosted on my Server](https://fitbit-report.arpan.app/) (Use this if the Render page is down)

## Preview of Data

![screenshot](https://github.com/arpanghosh8453/fitbit-web-ui-app/blob/main/help/Fitbit_Wellness_Report_Final_v2.jpg)

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
   ```
   > **Note**: Replace `192.168.x.x:5032` with your actual server IP and port

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

## Contributions

Special thanks to [@dipanghosh](https://github.com/dipanghosh) for his help and contribution towards the sleep schedule analysis part of the script and overall aesthetics suggestions. 

## Support me 
If you love visualizing your long term data with this web app, please consider supporting me with a coffee ❤ if you can! You can view more detailed health statistics with this setup than paying a subscription fee to Fitbit, thanks to their REST API services. 

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/A0A84F3DP)