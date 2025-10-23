# Fitbit Dashboard Enhancement Roadmap

## 🎯 Overview
This document tracks the holistic dashboard enhancement plan, integrating all requested features into phased implementation.

---

## ✅ Phase 1: Technical Fixes (COMPLETED)

### Unit Conversions ✅
- [x] **Weight**: Convert kg → lbs (multiply by 2.20462)
- [x] **Distance**: Convert km → miles (multiply by 0.621371)
- [x] Updated all chart labels, titles, and annotations

### Debug Empty Data 🔍
- [x] Added debug logging for:
  - Heart Rate Variability (HRV)
  - Breathing Rate
  - Skin Temperature Variation  
  - Cardio Fitness Score (VO2 Max)

**Status**: Ready for testing. After deploying, check Docker logs to see API responses for these metrics.

**Likely Causes of Empty Data**:
1. Device doesn't support metric (e.g., Temperature needs Sense/Versa 3/Charge 5+)
2. Not wearing device long enough (HRV needs overnight wear)
3. API endpoint mismatch or requires different permissions
4. Fitbit hasn't calculated metric yet (some require 3+ days of data)

---

## 📊 Phase 2: Exercise Analysis (PLANNED)

### 2A. Interactive Exercise Log
- [ ] Add "Filter by Activity Type" dropdown
- [ ] Add "Filter by Date Range" selector
- [ ] Add column: "Active Zone Minutes" per workout
- [ ] Add column: "Cardio Load" (if available in API)

### 2B. Workout Drill-Down (Modal/Popout)
- [ ] Make exercise log rows clickable
- [ ] Create workout detail modal with:
  - [ ] **Intraday Heart Rate Graph**: Line chart with HR zones color-coded background
  - [ ] **Time in Zones Bar Chart**: Minutes in Light/Moderate/Vigorous/Peak
- [ ] Fetch intraday HR data from API: `/1/user/-/activities/heart/date/[date]/1d/1sec.json`

### 2C. New Activity Summary Charts
- [ ] **Activity Type Pie Chart**: "Total Active Minutes by Activity Type"
  - Shows % breakdown (Walking, Running, Spinning, etc.)
- [ ] **Performance Over Time**: Line chart for selected activity
  - Track Average Pace, Average HR over time
  - Dropdown to select activity type
- [ ] **Fitness Consistency Heatmap**: Calendar-style grid
  - Color any day with logged exercise
  - Similar to Weekly Steps Heatmap

**API Endpoints Needed**:
- `/1/user/-/activities/heart/date/[date]/1d/1sec.json` (intraday HR)
- `/1/user/-/activities/[resource]/date/[date]/[period].json` (activity details)

---

## 😴 Phase 3: Sleep Analysis (PLANNED)

### 3A. Sleep SpO2 Drill-Down
- [ ] Create "Sleep SpO2" section with summary table:
  - Columns: Date, Sleep Start, Sleep End, Avg SpO2
- [ ] Make rows clickable
- [ ] Display "Estimated Oxygen Variation Chart" for selected night
  - Highlight periods of high/low variation
- [ ] Fetch detailed SpO2: `/1.2/user/-/spo2/date/[date].json`

### 3B. New Sleep Quality Charts
- [ ] **Sleep Score Trend**: Line chart of daily sleep score (85, 78, 91, etc.)
  - API: `/1.2/user/-/sleep/date/[date].json` → `efficiency` field
- [ ] **Sleep Stage Percentages**: Stacked bar chart
  - Show % of time in Deep, Light, REM (normalized for total sleep time)
- [ ] **Bedtime Consistency Chart**: Variance from 30-day average
  - Plot "Time to Bed" and "Time to Wake"
  - Show deviation ("+15 min early", "-30 min late")

**API Endpoints Needed**:
- `/1.2/user/-/spo2/date/[date].json` (detailed SpO2)
- `/1.2/user/-/sleep/date/[date].json` (sleep score/efficiency)

---

## 🔬 Phase 4: Advanced Correlational Insights (PLANNED)

**Prerequisites**: Requires HRV, Breathing Rate, and Temperature data to be working.

### 4A. Exercise Impact on Sleep
- [ ] **Scatter Plot**: Active Zone Minutes (X) vs HRV/Deep Sleep (Y)
  - X-axis: Total Active Zone Minutes from day N
  - Y-axis: HRV or Deep Sleep Minutes from night N
  - Color code by activity type
  - Add trend line

**Question Answered**: "How much intense exercise is 'too much' for my recovery?"

### 4B. Sleep Impact on Recovery
- [ ] **Scatter Plot**: Sleep Score (X) vs Resting Heart Rate (Y)
  - X-axis: Sleep Score or Total Sleep Time from night N
  - Y-axis: RHR from morning N+1
  - Add trend line
  - Highlight "optimal sleep zone"

**Question Answered**: "How much sleep do I need to feel recovered?"

### 4C. Additional Correlations (Optional)
- [ ] **HRV vs Training Load**: Is recovery keeping up with training?
- [ ] **Temperature vs Sleep Quality**: Correlate temp variation with deep sleep
- [ ] **Breathing Rate vs Sleep Apnea Risk**: Identify concerning patterns

---

## 📦 Implementation Details

### Technology Stack
- **Frontend**: Dash (Plotly)
- **Charts**: Plotly Express (px.scatter, px.pie, px.bar)
- **Interactions**: Dash callbacks with `dcc.Store` for state management
- **Modals**: `dbc.Modal` from dash-bootstrap-components

### New Dependencies Needed
```txt
dash-bootstrap-components
```

### File Structure
```
src/
├── app.py (main dashboard)
├── components/
│   ├── exercise_modal.py (workout drill-down)
│   ├── sleep_modal.py (SpO2 drill-down)
│   └── filters.py (date/activity filters)
└── utils/
    ├── correlations.py (scatter plot calculations)
    └── api_helpers.py (intraday data fetching)
```

---

## 🚦 Implementation Order (Recommended)

1. **Phase 1: Technical Fixes** ✅ DONE - Deploy and test
2. **Debug Empty Data** 🔍 IN PROGRESS - Check Docker logs after deployment
3. **Phase 2A: Exercise Log Filters** - Quick win, high user value
4. **Phase 3A: Sleep SpO2 Drill-Down** - Requested feature
5. **Phase 2B: Workout Drill-Down** - Complex but high value
6. **Phase 3B: Sleep Quality Charts** - Medium complexity
7. **Phase 2C: Activity Summary Charts** - Nice to have
8. **Phase 4: Correlations** - Advanced analytics, requires working HRV data

---

## 🐛 Known Issues to Address

1. **Empty HRV/Breathing/Temperature**: Investigate API responses
2. **API Rate Limiting**: May need to throttle requests for large date ranges
3. **Intraday Data**: Requires additional OAuth scope `heartrate` (already included)
4. **Modal Performance**: May need lazy loading for workout drill-downs

---

## 📝 Testing Checklist

### Phase 1 (Current)
- [ ] Deploy updated image to homelab
- [ ] Verify weight displays in lbs
- [ ] Verify distance displays in miles
- [ ] Check Docker logs for API debug output
- [ ] Test with different Fitbit devices (Sense, Versa, Charge)

### Phase 2 (Future)
- [ ] Exercise log filters work correctly
- [ ] Workout drill-down modal opens with HR graph
- [ ] Activity pie chart shows correct percentages
- [ ] Performance over time tracks selected activity

### Phase 3 (Future)
- [ ] SpO2 drill-down displays variation chart
- [ ] Sleep score trend matches Fitbit app
- [ ] Bedtime consistency shows accurate deviations

### Phase 4 (Future)
- [ ] Scatter plots render correctly with enough data points
- [ ] Trend lines show meaningful correlations
- [ ] Tooltips display relevant information on hover

---

## 💡 Future Ideas (Backlog)

- **Export Data**: CSV/Excel export for all charts
- **Comparison Mode**: Compare two date ranges side-by-side
- **Goal Tracking**: Set and track custom goals
- **Weekly/Monthly Reports**: Automated email summaries
- **Mobile Optimization**: Responsive design improvements
- **Dark Mode**: Theme toggle
- **Multi-User Dashboard**: Compare metrics with family/friends (privacy-respecting)

---

## 📚 Resources

### Fitbit API Documentation
- **Base URL**: https://dev.fitbit.com/build/reference/web-api/
- **Intraday Data**: https://dev.fitbit.com/build/reference/web-api/intraday/
- **Sleep Data**: https://dev.fitbit.com/build/reference/web-api/sleep/
- **Activity Data**: https://dev.fitbit.com/build/reference/web-api/activity/

### Dash Documentation
- **Callbacks**: https://dash.plotly.com/basic-callbacks
- **Modals**: https://dash-bootstrap-components.opensource.faculty.ai/docs/components/modal/
- **State Management**: https://dash.plotly.com/sharing-data-between-callbacks

---

## 🎉 Current Status

**Phase 1 Complete!** Ready to:
1. Rebuild Docker image
2. Deploy to homelab
3. Test unit conversions
4. Check logs for empty data debugging

**Next Action**: Deploy and gather debug info for empty metrics, then proceed to Phase 2A (Exercise Log Filters).

---

*Last Updated: October 22, 2025*

