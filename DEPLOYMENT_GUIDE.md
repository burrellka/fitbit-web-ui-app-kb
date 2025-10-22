# Deployment Guide - Enhanced Fitbit Wellness Dashboard

## 🎉 What's Been Added

All requested features have been successfully implemented:

### ✅ OAuth & Auto-Refresh
- One-click Fitbit login (no more manual token entry)
- Automatic token refresh before expiration
- Secure session-based token storage

### ✅ New Data Visualizations
All available Fitbit API data has been added:

1. **Heart Rate Variability (HRV)** 💗
   - Daily HRV measurements in milliseconds
   - Stress and recovery indicator
   - 30-day and overall averages

2. **Breathing Rate** 🫁
   - Breaths per minute during sleep
   - Respiratory health tracking

3. **Cardio Fitness Score (VO2 Max)** 🏃
   - Cardiovascular fitness estimate
   - Age and sex-adjusted scoring

4. **Temperature** 🌡️
   - Skin temperature variation from baseline
   - Available on Sense, Versa 3, Charge 5+

5. **Active Zone Minutes** ⚡
   - Time in fat burn, cardio, and peak zones
   - Weekly health goal tracking

6. **Calories & Distance** 🔥
   - Daily calories burned (BMR + activity)
   - Distance traveled in kilometers

7. **Floors Climbed** 🪜
   - Daily elevation gain
   - Altimeter-based tracking

8. **Exercise Log** 🏋️
   - Detailed workout history
   - Duration, calories, heart rate per activity

---

## 🚀 Deployment Instructions

### For Your Current Instance (Already Working!)
Your instance is already running with OAuth at `https://fitbitkb.yourdomain.com/`

**To update with new features:**
```bash
cd /path/to/fitbit-web-ui-app-kb
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### For Your Wife's Instance

#### 1. Register a New Fitbit App (Under Her Account)
- Go to https://dev.fitbit.com/apps (logged in as her)
- Click "Register a New App"
- Fill in:
  - **Application Name**: "Wife's Fitbit Wellness Report"
  - **Description**: "Personal wellness dashboard"
  - **Application Website**: `https://fitbitkcsb.yourdomain.com/`
  - **Organization**: Her name
  - **OAuth 2.0 Application Type**: **Server**
  - **Callback URL**: `https://fitbitkcsb.yourdomain.com/`
  - **Default Access Type**: Read-Only
- Save and note the `CLIENT_ID` and `CLIENT_SECRET`

#### 2. Create a Separate Docker Compose Stack

Create a new directory:
```bash
mkdir -p ~/fitbit-wife
cd ~/fitbit-wife
```

Create `.env` file:
```bash
CLIENT_ID=her_client_id_here
CLIENT_SECRET=her_client_secret_here
REDIRECT_URL=https://fitbitkcsb.yourdomain.com/
```

Create `docker-compose.yml`:
```yaml
services:
  fitbit-ui-wife:
    build: /path/to/fitbit-web-ui-app-kb
    container_name: fitbit-report-app-wife
    ports:
      - "5033:80"  # Different port
    restart: unless-stopped
    environment:
      - CLIENT_ID=${CLIENT_ID}
      - CLIENT_SECRET=${CLIENT_SECRET}
      - REDIRECT_URL=${REDIRECT_URL}
```

#### 3. Configure nginx Proxy for Wife's Instance

Add to your nginx config:
```nginx
server {
    listen 443 ssl http2;
    server_name fitbitkcsb.yourdomain.com;
    
    location / {
        proxy_pass http://192.168.13.5:5033;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

#### 4. Deploy
```bash
cd ~/fitbit-wife
docker-compose up -d
```

#### 5. Test
- Visit `https://fitbitkcsb.yourdomain.com/`
- Click "Login to FitBit"
- Authorize with her Fitbit account
- Select date range and view wellness data

---

## 📊 What Your Wife Will See

The dashboard now includes all the original metrics PLUS:
- Resting Heart Rate
- Steps Count with heatmap
- Activity Minutes (Fat Burn, Cardio, Peak)
- Weight Log
- SpO2 Levels
- Sleep Stages & Regularity
- **NEW: HRV (stress tracking)**
- **NEW: Breathing Rate**
- **NEW: Cardio Fitness Score**
- **NEW: Temperature Tracking**
- **NEW: Active Zone Minutes**
- **NEW: Calories & Distance**
- **NEW: Floors Climbed**
- **NEW: Exercise Log with workout details**

All with:
- Interactive graphs
- 30-day, 3-month, 6-month, and 1-year summaries
- Min/Max/Average statistics
- Beautiful visualizations

---

## 🔧 Troubleshooting

### "Missing environment variable" error
- Ensure `.env` file exists in the same directory as `docker-compose.yml`
- Check that all three variables are set: `CLIENT_ID`, `CLIENT_SECRET`, `REDIRECT_URL`

### OAuth redirect fails
- Verify the Callback URL in Fitbit app settings EXACTLY matches your `REDIRECT_URL`
- Must use HTTPS for production (HTTP only works for localhost)
- Include trailing slash: `https://domain.com/` not `https://domain.com`

### Some metrics show "No data"
- Not all Fitbit devices support all metrics (e.g., Temperature requires Sense/Charge 5+)
- Some metrics only appear after several days of wear
- HRV and Breathing Rate require overnight wear

### Token expired
- This should never happen now with auto-refresh!
- If it does, just click "Login to FitBit" again

---

## 🎨 Customization Ideas

If you want to customize further, you can:
- Change colors in `src/assets/custom_styling.css`
- Modify date ranges in the date picker
- Add custom annotations to graphs
- Export data to CSV for external analysis

---

## 📝 Notes

- Each user needs their own Fitbit App registration (can't share CLIENT_ID between users)
- Sessions are independent - each container maintains its own user session
- Data is fetched directly from Fitbit API (nothing is stored on your server)
- Token auto-refreshes 5 minutes before expiration
- Report generation can take 10-30 seconds depending on date range

---

## ✅ Success Checklist

- [ ] Original instance updated with new features
- [ ] Wife's Fitbit app registered
- [ ] Wife's docker container deployed
- [ ] Wife's nginx proxy configured
- [ ] Both instances accessible via HTTPS
- [ ] Both can log in successfully
- [ ] All metrics displaying correctly

---

## 🆘 Need Help?

Common commands:
```bash
# View logs
docker logs fitbit-report-app
docker logs fitbit-report-app-wife

# Rebuild container
docker-compose build --no-cache

# Restart container
docker-compose restart

# Stop container
docker-compose down
```

Enjoy your enhanced Fitbit dashboard! 🎉

