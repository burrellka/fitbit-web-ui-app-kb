# MCP Server Design: The "AI Health Coach" Core

## Vision
To transform the Fitbit Wellness App into an intelligent, proactive Health Coach by exposing the full depth of user data to Gemini. The MCP server will not just fetch numbers; it will provide context, trends, and deep analytical capabilities, allowing the AI to "see" what the user sees in the detailed reports and offer actionable, personalized guidance.

## System Architecture
-   **Core**: `mcp_server.py` running `fastmcp`.
-   **Data Layer**: Direct access to `FitbitCache` (SQLite) for sub-millisecond query performance.
-   **Intelligence**: Custom Python logic within tools to perform pre-aggregation and statistical analysis before sending data to the LLM.

---

## 🛠️ Tool Suite: The Coach's Toolkit

### 1. The "Morning Briefing" & Daily Context
*Tools to answer: "How am I today?" "What's the plan?"*

*   **`get_daily_snapshot(date: str)`**
    *   **Purpose**: Holistic view of a single day.
    *   **Data**: Merges Sleep (Score, Stages, Restoration), Activity (Steps, AZM, Cals), and Biometrics (RHR, HRV, SpO2).
    *   **Enrichment**: Includes a calculated "Readiness Score" (0-100) based on recovery metrics.

*   **`get_readiness_breakdown(date: str)`**
    *   **Purpose**: Explain *why* the user feels a certain way.
    *   **Data**: Detailed comparison of RHR vs 30-day baseline, HRV vs baseline, and Sleep Debt.
    *   **Output**: "Readiness: 45/100 (Low). Driven by: RHR +5bpm above baseline, 2h Sleep Debt."

### 2. Deep Sleep Analysis
*Tools to answer: "Why am I tired?" "Is my sleep improving?"*

*   **`get_sleep_details(date: str)`**
    *   **Purpose**: Granular sleep stage analysis.
    *   **Data**: Hypnogram summary (time in Deep/REM/Light/Wake), Efficiency, Restlessness.
    *   **Context**: Compares tonight's percentages to 30-day averages (e.g., "Deep Sleep: 15% (Normal is 12-18%)").

*   **`get_sleep_consistency(days: int = 30)`**
    *   **Purpose**: Analyze sleep hygiene and regularity.
    *   **Data**: Standard deviation of Bedtime and Wake Time.
    *   **Output**: "Consistency Grade: B. Bedtime varies by +/- 45 mins. Wake time is consistent."

### 3. Workout & Performance Analytics
*Tools to answer: "Analyze my run." "Am I overtraining?"*

*   **`get_workout_history(start_date: str, end_date: str, type: str = None)`**
    *   **Purpose**: Logbook of all exercises.
    *   **Data**: Duration, Calories, Avg HR, Max HR, Steps.
    *   **Enrichment**: Calculated "Intensity Score" (Cal/Min) and "Training Load" (AZM * Intensity).

*   **`get_intraday_heart_rate(date: str, start_time: str, end_time: str)`**
    *   **Purpose**: Second-by-second analysis of a specific workout.
    *   **Data**: High-resolution HR data points.
    *   **Use Case**: "Did I hit my max HR during the intervals at 5:30 PM?"

### 4. Long-Term Trends & Correlations
*Tools to answer: "How is my month going?" "Does running help me sleep?"*

*   **`get_comparative_trends(metric: str, period_1: str, period_2: str)`**
    *   **Purpose**: A/B testing timeframes.
    *   **Example**: Compare "Sleep Score" for "This Week" vs "Last Week".
    *   **Output**: Absolute change, percentage change, and trend direction.

*   **`analyze_correlation(metric_a: str, metric_b: str, days: int = 60)`**
    *   **Purpose**: Discover relationships between behaviors.
    *   **Example**: "Correlate 'Active Zone Minutes' with 'Deep Sleep' over 60 days."
    *   **Output**: Pearson correlation coefficient (e.g., "0.45 - Moderate Positive Correlation").

*   **`get_weekly_report(end_date: str)`**
    *   **Purpose**: The "Sunday Review".
    *   **Data**: Aggregated totals for the week (Total Steps, Avg Sleep, Total AZM) vs Goals.

---

## 📚 Resources: The Knowledge Base
*Read-only context provided to the LLM.*

*   **`fitbit://user/profile`**: User's age, height, weight, and specific health goals (e.g., "Goal: Improve Deep Sleep to 90min").
*   **`fitbit://knowledge/glossary`**: Definitions of metrics (e.g., "What is HRV?", "What is AZM?").
*   **`fitbit://prompts/personas`**: System instructions for "The Drill Sergeant", "The Empathetic Coach", "The Data Scientist".

---

## 🤖 Persona Prompts (Examples)

**The Sleep Detective**
> "Analyze my sleep for the last 7 days using `get_sleep_consistency` and `get_comparative_trends`. Correlate my `wake_minutes` with my `active_zone_minutes`. Tell me if I'm physically restless or just have bad hygiene."

**The Performance Coach**
> "Look at my `get_workout_history` for the month. Am I progressing in intensity? Check my `get_readiness_breakdown` for today—should I push for a PR or do a recovery run?"
