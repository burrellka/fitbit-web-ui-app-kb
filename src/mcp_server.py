
from fastmcp import FastMCP
import os
import sys
import json
import statistics
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# Ensure we can import from src
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cache_manager import FitbitCache
from src.prompts import PERSONAS

# Initialize MCP Server
mcp = FastMCP("Fitbit Health Coach")

# Initialize Cache
cache = FitbitCache()

# --- Helpers ---

def _calculate_readiness(date: str) -> Dict[str, Any]:
    """
    Internal helper to calculate readiness score.
    Logic:
    - Sleep Factor (40%): Reality Score
    - HRV Factor (30%): vs 30-day baseline
    - RHR Factor (30%): vs 30-day baseline
    """
    # Get today's data (or "yesterday" relative to the morning)
    # Usually readiness is for "today" based on "last night's" sleep and "yesterday's" strain?
    # Actually, readiness for TODAY is based on LAST NIGHT's sleep and TODAY's morning RHR/HRV.
    # So if date="2023-10-27", we look at sleep for "2023-10-27" (which is the sleep ending on that morning).
    
    sleep = cache.get_sleep_data(date)
    advanced = cache.get_advanced_metrics(date)
    daily = cache.get_daily_metrics(date)
    
    if not sleep or not advanced or not daily:
        return {"score": None, "reason": "Insufficient data for readiness calculation"}
        
    # 1. Sleep Factor (0-100)
    sleep_score = sleep.get('reality_score') or sleep.get('fitbit_score') or 0
    sleep_factor = sleep_score
    
    # 2. Baselines (Last 30 days)
    end_date = datetime.strptime(date, "%Y-%m-%d")
    start_date = (end_date - timedelta(days=30)).strftime("%Y-%m-%d")
    prev_date = (end_date - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Fetch historical data for baselines
    # We need a way to get range of advanced metrics. 
    # cache.get_missing_dates checks existence, but we need values.
    # We'll use a loop for now (SQLite is fast) or add a range getter to cache later.
    # For now, let's just get last 7 days for speed to approximate baseline.
    baseline_days = 14
    rhr_values = []
    hrv_values = []
    
    for i in range(1, baseline_days + 1):
        d = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
        dm = cache.get_daily_metrics(d)
        am = cache.get_advanced_metrics(d)
        if dm and dm.get('resting_heart_rate'):
            rhr_values.append(dm['resting_heart_rate'])
        if am and am.get('hrv'):
            hrv_values.append(am['hrv'])
            
    # RHR Factor (Lower is better)
    current_rhr = daily.get('resting_heart_rate')
    rhr_factor = 50 # Default neutral
    if current_rhr and rhr_values:
        avg_rhr = statistics.mean(rhr_values)
        diff = current_rhr - avg_rhr
        # If RHR is 5 bpm higher than avg -> bad (-10 pts)
        # If RHR is 5 bpm lower than avg -> good (+10 pts)
        # Scale: 50 - (diff * 2)
        rhr_factor = max(0, min(100, 50 - (diff * 4)))
        
    # HRV Factor (Higher is better)
    current_hrv = advanced.get('hrv')
    hrv_factor = 50
    if current_hrv and hrv_values:
        avg_hrv = statistics.mean(hrv_values)
        diff = current_hrv - avg_hrv
        # If HRV is 10ms higher -> good
        # If HRV is 10ms lower -> bad
        hrv_factor = max(0, min(100, 50 + (diff * 2)))
        
    # Weighted Sum
    # Sleep 40%, RHR 30%, HRV 30%
    total_score = (sleep_factor * 0.4) + (rhr_factor * 0.3) + (hrv_factor * 0.3)
    
    return {
        "score": round(total_score),
        "breakdown": {
            "sleep_score": sleep_score,
            "rhr_score": round(rhr_factor),
            "hrv_score": round(hrv_factor),
            "current_rhr": current_rhr,
            "avg_rhr": round(avg_rhr) if rhr_values else None,
            "current_hrv": current_hrv,
            "avg_hrv": round(avg_hrv) if hrv_values else None
        }
    }

# --- Tools ---

@mcp.tool()
def get_daily_snapshot(date: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get a holistic 'Morning Briefing' view for a specific date.
    Includes Sleep, Activity, Biometrics, and a calculated Readiness Score.
    
    Args:
        date: Date in 'YYYY-MM-DD' format (e.g., '2023-10-27').
    """
    try:
        daily = cache.get_daily_metrics(date)
        sleep = cache.get_sleep_data(date)
        advanced = cache.get_advanced_metrics(date)
        readiness = _calculate_readiness(date)
        
        output = [f"📅 Daily Snapshot for {date}"]
        
        # Readiness
        if readiness['score'] is not None:
            score = readiness['score']
            status = "🟢 Prime" if score >= 80 else "🟡 Normal" if score >= 50 else "🔴 Recovery"
            output.append(f"⚡ Readiness: {score}/100 ({status})")
        
        # Sleep
        if sleep:
            output.append(f"😴 Sleep: {sleep.get('reality_score')} (Reality) | {sleep.get('fitbit_score')} (Fitbit)")
            output.append(f"   Duration: {sleep.get('total_sleep')} min | Deep: {sleep.get('deep')} min | REM: {sleep.get('rem')} min")
        
        # Biometrics
        if daily and advanced:
            output.append(f"❤️ Biometrics: RHR {daily.get('resting_heart_rate')} bpm | HRV {advanced.get('hrv')} ms | SpO2 {daily.get('spo2')}%")
            
        # Activity
        if daily:
            output.append(f"🔥 Activity: {daily.get('steps')} steps | {daily.get('active_zone_minutes')} AZM | {daily.get('calories')} kcal")
            
        return "\n".join(output)
    except Exception as e:
        return f"Error generating snapshot: {str(e)}"

@mcp.tool()
def get_readiness_breakdown(date: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Explain WHY the readiness score is what it is.
    Useful for 'Why am I tired?' or 'Should I train hard?'
    
    Args:
        date: Date in 'YYYY-MM-DD' format.
    """
    try:
        r = _calculate_readiness(date)
        if r['score'] is None:
            return "Insufficient data to calculate readiness."
            
        b = r['breakdown']
        output = [f"⚡ Readiness Breakdown for {date}: {r['score']}/100"]
        
        # Sleep Analysis
        output.append(f"1. Sleep Factor (40%): {b['sleep_score']}/100")
        
        # RHR Analysis
        rhr_diff = b['current_rhr'] - b['avg_rhr'] if b['avg_rhr'] else 0
        rhr_status = "Elevated (Stress/Illness?)" if rhr_diff > 3 else "Recovered" if rhr_diff < -2 else "Normal"
        output.append(f"2. RHR Factor (30%): {b['rhr_score']}/100")
        output.append(f"   - Current: {b['current_rhr']} bpm vs Baseline: {b['avg_rhr']} bpm")
        output.append(f"   - Status: {rhr_status} ({rhr_diff:+.1f} bpm)")
        
        # HRV Analysis
        hrv_diff = b['current_hrv'] - b['avg_hrv'] if b['avg_hrv'] else 0
        hrv_status = "Prime State" if hrv_diff > 5 else "Stressed/Fatigued" if hrv_diff < -5 else "Balanced"
        output.append(f"3. HRV Factor (30%): {b['hrv_score']}/100")
        output.append(f"   - Current: {b['current_hrv']} ms vs Baseline: {b['avg_hrv']} ms")
        output.append(f"   - Status: {hrv_status} ({hrv_diff:+.1f} ms)")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error getting readiness breakdown: {str(e)}"

@mcp.tool()
def get_sleep_consistency(days: int = 30, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Analyze sleep hygiene (bedtime/wake time consistency) over the last N days.
    """
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Fetch sleep data
        bedtimes = []
        waketimes = []
        
        current = start_date
        while current <= end_date:
            d_str = current.strftime("%Y-%m-%d")
            s = cache.get_sleep_data(d_str)
            if s and s.get('start_time'):
                # Parse start time
                # Format usually "2023-10-27T23:30:00.000"
                try:
                    start_dt = datetime.fromisoformat(s['start_time'])
                    # For consistency, we care about time of day.
                    # But bedtime can be 23:00 or 01:00.
                    # Convert to "minutes from midnight" (negative for previous day)
                    # e.g. 23:00 -> -60, 01:00 -> 60
                    minutes = start_dt.hour * 60 + start_dt.minute
                    if minutes > 12 * 60: # PM
                        minutes -= 24 * 60
                    bedtimes.append(minutes)
                    
                    # Calculate wake time
                    duration = s.get('total_sleep', 0) + s.get('wake', 0) # approx
                    # Better: use end time if available, but we only store start_time and duration?
                    # Actually app.py parses full log, cache stores summary.
                    # Let's approx wake time = start + total_minutes (including wake)
                    # Wait, total_sleep is sleep time. total_minutes usually implies duration?
                    # cache.get_sleep_data returns 'total_sleep' (minutes).
                    # Let's assume total_sleep + wake_minutes = duration
                    total_duration = s.get('total_sleep', 0) + s.get('wake', 0)
                    wake_dt = start_dt + timedelta(minutes=total_duration)
                    waketimes.append(wake_dt.hour * 60 + wake_dt.minute)
                except:
                    pass
            current += timedelta(days=1)
            
        if not bedtimes:
            return "No sleep data found for consistency analysis."
            
        # Calculate Stdev
        bed_std = statistics.stdev(bedtimes) if len(bedtimes) > 1 else 0
        wake_std = statistics.stdev(waketimes) if len(waketimes) > 1 else 0
        
        grade = "A" if bed_std < 30 else "B" if bed_std < 60 else "C" if bed_std < 90 else "D"
        
        return f"""
😴 Sleep Consistency Report ({days} days)
Grade: {grade}

Bedtime Consistency: +/- {round(bed_std)} min
Wake Time Consistency: +/- {round(wake_std)} min

Interpretation:
- < 30 min: Excellent circadian rhythm.
- 30-60 min: Good, minor social jetlag.
- > 60 min: Inconsistent, likely affecting recovery.
"""
    except Exception as e:
        return f"Error analyzing consistency: {str(e)}"

@mcp.tool()
def get_sleep_log(start_date: str, end_date: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get a daily log of sleep metrics for a date range.
    Useful for finding the 'best' or 'worst' night of sleep.
    
    Args:
        start_date: Start date in 'YYYY-MM-DD' format.
        end_date: End date in 'YYYY-MM-DD' format.
    """
    try:
        current = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        
        output = [f"😴 Sleep Log ({start_date} to {end_date})"]
        
        while current <= end:
            d_str = current.strftime("%Y-%m-%d")
            s = cache.get_sleep_data(d_str)
            
            if s:
                score = s.get('reality_score') or s.get('fitbit_score') or 0
                duration = s.get('total_sleep', 0)
                deep = s.get('deep', 0)
                rem = s.get('rem', 0)
                output.append(f"- {d_str}: Score {score} | {duration} min (Deep: {deep}, REM: {rem})")
            else:
                output.append(f"- {d_str}: No data")
                
            current += timedelta(days=1)
            
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching sleep log: {str(e)}"

@mcp.tool()
def get_workout_history(start_date: str, end_date: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get detailed workout logs with calculated Intensity Score.
    
    Args:
        start_date: Start date in 'YYYY-MM-DD' format.
        end_date: End date in 'YYYY-MM-DD' format.
    """
    try:
        activities = cache.get_activities_in_range(start_date, end_date)
        if not activities:
            return f"No workouts found between {start_date} and {end_date}."
            
        output = [f"🏋️ Workout History ({start_date} to {end_date})"]
        
        for act in activities:
            name = act.get('activityName', 'Unknown')
            duration = act.get('duration', 0) // 60000 # ms to min
            cals = act.get('calories', 0)
            hr = act.get('averageHeartRate', 'N/A')
            steps = act.get('steps', 0)
            
            # Intensity Score (Cal/Min)
            intensity = round(cals / duration, 1) if duration > 0 else 0
            intensity_label = "🔥 High" if intensity > 10 else "⚡ Moderate" if intensity > 5 else "💧 Low"
            
            output.append(f"- {act['startTime'][:10]} | {name}")
            output.append(f"  {duration} min | {cals} kcal | Avg HR: {hr}")
            output.append(f"  Intensity: {intensity} cal/min ({intensity_label})")
            
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching history: {str(e)}"

@mcp.tool()
def get_comparative_trends(metric: str, period_1_days: int = 7, period_2_days: int = 30, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Compare a metric between two timeframes (e.g., Last 7 days vs Previous 30 days).
    Metrics: sleep_score, resting_heart_rate, hrv, active_zone_minutes, steps
    """
    try:
        end_date = datetime.now()
        p1_start = end_date - timedelta(days=period_1_days)
        p2_start = p1_start - timedelta(days=period_2_days)
        
        # Helper to fetch and average
        def get_avg(start, end, metric_key):
            values = []
            curr = start
            while curr < end:
                d = curr.strftime("%Y-%m-%d")
                val = None
                if metric_key in ['sleep_score', 'reality_score']:
                    s = cache.get_sleep_data(d)
                    val = s.get(metric_key) if s else None
                elif metric_key in ['hrv', 'breathing_rate']:
                    a = cache.get_advanced_metrics(d)
                    val = a.get(metric_key) if a else None
                else:
                    dm = cache.get_daily_metrics(d)
                    val = dm.get(metric_key) if dm else None
                
                if val is not None:
                    values.append(val)
                curr += timedelta(days=1)
            return statistics.mean(values) if values else 0
            
        # Map friendly names to DB keys
        key_map = {
            'sleep_score': 'reality_score',
            'rhr': 'resting_heart_rate',
            'hrv': 'hrv',
            'azm': 'active_zone_minutes',
            'steps': 'steps'
        }
        
        db_key = key_map.get(metric, metric)
        
        avg_p1 = get_avg(p1_start, end_date, db_key)
        avg_p2 = get_avg(p2_start, p1_start, db_key)
        
        diff = avg_p1 - avg_p2
        pct = (diff / avg_p2 * 100) if avg_p2 > 0 else 0
        
        direction = "📈 Up" if diff > 0 else "📉 Down" if diff < 0 else "➡️ Flat"
        
        return f"""
📊 Trend Analysis: {metric.upper()}
Period 1 (Last {period_1_days} days): {round(avg_p1, 1)}
Period 2 (Prev {period_2_days} days): {round(avg_p2, 1)}

Change: {direction} {round(diff, 1)} ({round(pct, 1)}%)
"""
    except Exception as e:
        return f"Error calculating trends: {str(e)}"

@mcp.tool()
def analyze_correlation(metric_a: str, metric_b: str, days: int = 60, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Calculate Pearson correlation between two metrics over N days.
    Discover relationships like 'Does AZM affect Sleep Score?'
    """
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        vals_a = []
        vals_b = []
        
        # Map keys
        key_map = {
            'sleep_score': ('sleep', 'reality_score'),
            'deep_sleep': ('sleep', 'deep'),
            'rem_sleep': ('sleep', 'rem'),
            'rhr': ('daily', 'resting_heart_rate'),
            'hrv': ('advanced', 'hrv'),
            'azm': ('daily', 'active_zone_minutes'),
            'steps': ('daily', 'steps'),
            'calories': ('daily', 'calories'),
            'stress': ('daily', 'stress_score') # if available
        }
        
        def get_val(date_str, key_tuple):
            source, key = key_tuple
            if source == 'sleep':
                d = cache.get_sleep_data(date_str)
                return d.get(key) if d else None
            elif source == 'advanced':
                d = cache.get_advanced_metrics(date_str)
                return d.get(key) if d else None
            else:
                d = cache.get_daily_metrics(date_str)
                return d.get(key) if d else None

        current = start_date
        while current <= end_date:
            d_str = current.strftime("%Y-%m-%d")
            
            # For sleep, we usually compare "Day's Activity" vs "Next Night's Sleep"
            # OR "Last Night's Sleep" vs "Day's Performance"
            # Default assumption: Same calendar date. 
            # Note: Fitbit Sleep date "2023-10-27" is sleep ENDING on morning of 27th.
            # Activity "2023-10-27" is activity DURING the 27th.
            # So Activity(27th) -> Sleep(28th).
            # If metric_b is sleep, we might want to shift date?
            # For simplicity, let's correlate SAME DATE first (e.g. RHR vs HRV).
            # If user asks "Does activity affect sleep", they might need to specify lag.
            # Let's stick to simple same-date correlation for now.
            
            ka = key_map.get(metric_a)
            kb = key_map.get(metric_b)
            
            if ka and kb:
                va = get_val(d_str, ka)
                vb = get_val(d_str, kb)
                if va is not None and vb is not None:
                    vals_a.append(va)
                    vals_b.append(vb)
            
            current += timedelta(days=1)
            
        if len(vals_a) < 10:
            return "Insufficient data points for correlation (need at least 10)."
            
        # Pearson Correlation
        # r = Cov(X,Y) / (std(X) * std(Y))
        mean_a = statistics.mean(vals_a)
        mean_b = statistics.mean(vals_b)
        
        numerator = sum((a - mean_a) * (b - mean_b) for a, b in zip(vals_a, vals_b))
        denom = (sum((a - mean_a)**2 for a in vals_a) * sum((b - mean_b)**2 for b in vals_b)) ** 0.5
        
        if denom == 0:
            return "Correlation undefined (no variance in data)."
            
        r = numerator / denom
        
        strength = "Strong" if abs(r) > 0.7 else "Moderate" if abs(r) > 0.3 else "Weak"
        relationship = "Positive" if r > 0 else "Negative"
        
        return f"""
🔗 Correlation Analysis ({len(vals_a)} days)
{metric_a} vs {metric_b}

Coefficient (r): {round(r, 3)}
Interpretation: {strength} {relationship} Correlation.
"""
    except Exception as e:
        return f"Error calculating correlation: {str(e)}"

    except Exception as e:
        return f"Error calculating correlation: {str(e)}"

# --- Health Coach 2.0 Tools ---

@mcp.tool()
def get_lifetime_stats(sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get lifetime aggregated stats (Steps, Distance, Floors) from the local cache.
    Note: This represents 'Lifetime in Cache', not necessarily total Fitbit account lifetime.
    """
    try:
        # We need direct DB access for aggregation
        import sqlite3
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT SUM(steps), SUM(distance), SUM(floors), COUNT(date)
            FROM daily_metrics_cache
        ''')
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return "No data found in cache."
            
        steps = result[0] or 0
        dist = result[1] or 0
        floors = result[2] or 0
        days = result[3] or 0
        
        # Fun comparisons
        earth_circumference_km = 40075
        pct_earth = (dist / earth_circumference_km) * 100
        
        everest_floors = 2900 # Approx
        pct_everest = (floors / everest_floors) * 100
        
        return f"""
🏆 Lifetime Stats (Cached Data - {days} days)
----------------------------------------
👣 Steps: {steps:,}
🌍 Distance: {dist:,.1f} km ({pct_earth:.2f}% of Earth's circumference)
🧗 Floors: {floors:,} ({pct_everest:.2f}% of Mt. Everest)
"""
    except Exception as e:
        return f"Error calculating lifetime stats: {str(e)}"

@mcp.tool()
def get_badges(sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get a list of earned badges.
    (Currently a placeholder as badges are not yet cached)
    """
    return "🏆 Badges are not yet synchronized to the local cache. Stay tuned for updates!"

@mcp.tool()
def get_zone_analysis(start_date: str, end_date: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Analyze Heart Rate Zones (Fat Burn, Cardio, Peak) for a date range.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT date, fat_burn_minutes, cardio_minutes, peak_minutes, active_zone_minutes
            FROM daily_metrics_cache
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
        ''', (start_date, end_date))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return f"No zone data found between {start_date} and {end_date}."
            
        output = [f"🔥 Zone Analysis ({start_date} to {end_date})"]
        
        total_fat = 0
        total_cardio = 0
        total_peak = 0
        
        for row in rows:
            d, fat, cardio, peak, azm = row
            fat = fat or 0
            cardio = cardio or 0
            peak = peak or 0
            
            total_fat += fat
            total_cardio += cardio
            total_peak += peak
            
            output.append(f"- {d}: Fat Burn {fat}m | Cardio {cardio}m | Peak {peak}m ({azm} AZM)")
            
        output.append("-" * 30)
        output.append(f"Totals: Fat Burn {total_fat}m | Cardio {total_cardio}m | Peak {total_peak}m")
        
        return "\n".join(output)
    except Exception as e:
        return f"Error analyzing zones: {str(e)}"

@mcp.tool()
def get_activity_log(start_date: str, end_date: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get a raw list of activities for a date range.
    """
    try:
        activities = cache.get_activities_in_range(start_date, end_date)
        if not activities:
            return f"No activities found between {start_date} and {end_date}."
            
        output = [f"📝 Activity Log ({start_date} to {end_date})"]
        
        for act in activities:
            # Handle different formats (cached dict vs raw)
            name = act.get('activityName', 'Unknown')
            date = act.get('startTime', '')[:10]
            cals = act.get('calories', 0)
            duration = act.get('duration', 0) // 60000
            
            output.append(f"- {date}: {name} ({duration} min, {cals} kcal)")
            
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching activity log: {str(e)}"

# --- Dynamic Data Explorer ---

@mcp.tool()
def inspect_schema(sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Get the database schema (list of tables and their columns).
    Useful for understanding what data is available for SQL queries.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        # Get tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        output = ["🗄️ Database Schema"]
        
        for t in tables:
            table_name = t[0]
            output.append(f"\nTable: {table_name}")
            
            # Get columns
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = cursor.fetchall()
            for c in columns:
                # cid, name, type, notnull, dflt_value, pk
                output.append(f"  - {c[1]} ({c[2]})")
                
        conn.close()
        return "\n".join(output)
    except Exception as e:
        return f"Error inspecting schema: {str(e)}"

@mcp.tool()
def run_sql_query(query: str, sessionId: str = "", action: str = "", chatInput: str = "", toolCallId: str = "") -> str:
    """
    Execute a READ-ONLY SQL query against the local database.
    WARNING: Only SELECT statements are allowed.
    
    Args:
        query: The SQL query string (e.g., "SELECT date, steps FROM daily_metrics_cache LIMIT 5")
    """
    try:
        # Security Check
        normalized = query.strip().upper()
        if not normalized.startswith("SELECT"):
            return "⛔ Security Error: Only SELECT queries are allowed."
            
        forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "REPLACE"]
        for word in forbidden:
            if word in normalized:
                return f"⛔ Security Error: Keyword '{word}' is not allowed."
        
        import sqlite3
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        cursor.execute(query)
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description]
        
        conn.close()
        
        if not rows:
            return "Query returned no results."
            
        # Format as JSON for the LLM to parse easily
        results = []
        for row in rows:
            results.append(dict(zip(columns, row)))
            
        return json.dumps(results, indent=2)
        
    except Exception as e:
        return f"SQL Error: {str(e)}"

# --- Resources ---

@mcp.resource("fitbit://prompts/personas")
def get_personas() -> str:
    """Get the library of Health Coach personas"""
    return json.dumps(PERSONAS, indent=2)

if __name__ == "__main__":
    mcp.run()
