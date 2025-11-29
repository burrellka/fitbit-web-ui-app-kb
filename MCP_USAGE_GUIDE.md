# 🎓 Fitbit Health Coach MCP Server - Usage Guide

## Overview
This MCP server transforms your Fitbit data into an intelligent "Health Coach" for AI agents (like Claude, Cursor, or Windsurf). It doesn't just fetch numbers; it calculates readiness, analyzes sleep consistency, and finds hidden trends in your health data.

## 🚀 Getting Started

### 1. Prerequisites
-   **Python 3.10+**
-   **Fitbit Web UI App** running (to populate the cache).
-   **`fastmcp`** library installed (`pip install fastmcp`).

### 2. Running the Server

**Option A: Local Python**
```bash
fastmcp dev src/mcp_server.py
```

**Option B: Docker (Recommended)**
The server is included in the main application container.
1.  Build and start the container:
    ```bash
    docker-compose up -d --build
    ```
2.  The MCP server will be available at `http://localhost:8000/sse`.

### 3. Connecting to AI Clients

**Local Python Connection:**
-   **Command**: `python`
-   **Arguments**: `src/mcp_server.py`

**Docker Connection (SSE):**
-   **Type**: SSE (Server-Sent Events)
-   **URL**: `http://localhost:8000/sse`


---

## 🛠️ Available Tools

### 🌅 Morning Briefing
*   **`get_daily_snapshot(date)`**: The "One-Stop Shop" for your day. Returns a calculated **Readiness Score** (0-100), Sleep Score, RHR, HRV, and Activity summary.
*   **`get_readiness_breakdown(date)`**: Explains *why* your readiness is high or low (e.g., "RHR is +5bpm above baseline").

### 😴 Sleep Analysis
*   **`get_sleep_consistency(days)`**: Grades your sleep hygiene (bedtime/wake time variance) over the last N days.
*   **`get_sleep_details(date)`**: Granular analysis of sleep stages and efficiency.

### 🏋️ Performance & Activity
*   **`get_workout_history(start, end)`**: Detailed log of workouts with calculated **Intensity Score** (Cal/Min).
*   **`get_intraday_heart_rate(date, start, end)`**: Second-by-second HR analysis for deep dives into specific workouts.

### 📈 Trends & Insights
*   **`get_comparative_trends(metric, p1, p2)`**: A/B test your health. Compare "This Week" vs "Last Month" for any metric (steps, rhr, sleep_score, etc.).
*   **`analyze_correlation(metric_a, metric_b)`**: Find relationships. "Does `active_zone_minutes` correlate with `deep_sleep`?"

---

## 🤖 Example Prompts (Copy & Paste)

### The "Morning Check-In"
> "Call `get_daily_snapshot` for today. Based on my Readiness Score and Sleep data, what kind of workout should I do? If readiness is low, give me a specific recovery plan."

### The "Sleep Detective"
> "I feel tired despite sleeping 8 hours. Call `get_sleep_details` for last night and `get_sleep_consistency` for the last 30 days. Is my sleep quality poor, or is my schedule inconsistent?"

### The "Performance Review"
> "Analyze my training for the last month using `get_workout_history`. Have I been increasing my intensity? Also check `get_comparative_trends` for my Resting Heart Rate—is it trending down (indicating fitness) or up (indicating fatigue)?"

### The "Data Scientist"
> "Run an analysis: Does my stress (RHR) correlate with my activity levels? Call `analyze_correlation` for 'resting_heart_rate' vs 'active_zone_minutes' over the last 60 days."

---

## 🧠 Personas
The server includes a prompt library resource: `fitbit://prompts/personas`.
You can ask your AI:
> "Load the 'Performance Coach' persona from the prompt library and analyze my last week."
