#!/usr/bin/env python3
"""
Script to check what data is cached for a specific date range
"""
import sqlite3
import json
from datetime import datetime, timedelta

# Path to the cache database (adjust if needed)
DB_PATH = "data_cache.db"

def check_cache_for_date_range(start_date, end_date):
    """Check all cached data for a date range"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print(f"\n{'='*80}")
    print(f"CACHE REPORT: {start_date} to {end_date}")
    print(f"{'='*80}\n")
    
    # Get date range
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    dates = [(start + timedelta(days=x)).strftime('%Y-%m-%d') 
             for x in range((end - start).days + 1)]
    
    for date in dates:
        print(f"\n📅 {date}")
        print("-" * 80)
        
        # Check daily_metrics_cache
        cursor.execute('''
            SELECT steps, calories, distance, floors, active_zone_minutes,
                   resting_heart_rate, fat_burn_minutes, cardio_minutes, peak_minutes,
                   weight, spo2, eov
            FROM daily_metrics_cache WHERE date = ?
        ''', (date,))
        daily_metrics = cursor.fetchone()
        
        if daily_metrics:
            print("  📊 Daily Metrics:")
            print(f"    Steps: {daily_metrics[0]}")
            print(f"    Calories: {daily_metrics[1]}")
            print(f"    Distance: {daily_metrics[2]}")
            print(f"    Floors: {daily_metrics[3]}")
            print(f"    Active Zone Minutes: {daily_metrics[4]}")
            print(f"    Resting Heart Rate: {daily_metrics[5]}")
            print(f"    Fat Burn Minutes: {daily_metrics[6]}")
            print(f"    Cardio Minutes: {daily_metrics[7]}")
            print(f"    Peak Minutes: {daily_metrics[8]}")
            print(f"    Weight: {daily_metrics[9]}")
            print(f"    SpO2: {daily_metrics[10]}")
            print(f"    EOV: {daily_metrics[11]}")
        else:
            print("  ❌ No daily metrics found")
        
        # Check sleep_cache
        cursor.execute('''
            SELECT reality_score, proxy_score, efficiency, 
                   deep, light, rem, wake, total_minutes
            FROM sleep_cache WHERE date = ?
        ''', (date,))
        sleep = cursor.fetchone()
        
        if sleep:
            print("  😴 Sleep Data:")
            print(f"    Reality Score: {sleep[0]}")
            print(f"    Proxy Score: {sleep[1]}")
            print(f"    Efficiency: {sleep[2]}%")
            print(f"    Deep: {sleep[3]} min")
            print(f"    Light: {sleep[4]} min")
            print(f"    REM: {sleep[5]} min")
            print(f"    Wake: {sleep[6]} min")
            print(f"    Total: {sleep[7]} min")
        else:
            print("  ❌ No sleep data found")
        
        # Check advanced_metrics_cache
        cursor.execute('''
            SELECT hrv, breathing_rate, temperature
            FROM advanced_metrics_cache WHERE date = ?
        ''', (date,))
        advanced = cursor.fetchone()
        
        if advanced:
            print("  💚 Advanced Metrics:")
            print(f"    HRV: {advanced[0]} ms")
            print(f"    Breathing Rate: {advanced[1]} bpm")
            print(f"    Temperature: {advanced[2]}°F")
        else:
            print("  ❌ No advanced metrics found")
        
        # Check cardio_fitness_cache
        cursor.execute('''
            SELECT vo2_max FROM cardio_fitness_cache WHERE date = ?
        ''', (date,))
        cardio = cursor.fetchone()
        
        if cardio and cardio[0]:
            print("  🏃 Cardio Fitness:")
            print(f"    VO2 Max: {cardio[0]}")
        else:
            print("  ❌ No cardio fitness data found")
        
        # Check activities_cache
        cursor.execute('''
            SELECT activity_name, duration_ms, calories, avg_heart_rate
            FROM activities_cache WHERE date = ?
        ''', (date,))
        activities = cursor.fetchall()
        
        if activities:
            print(f"  🏋️ Activities ({len(activities)}):")
            for act in activities:
                duration_min = act[1] // 60000 if act[1] else 0
                print(f"    - {act[0]}: {duration_min} min, {act[2]} cal, HR: {act[3]}")
        else:
            print("  ❌ No activities found")
    
    conn.close()
    
    print(f"\n{'='*80}")
    print("Cache check complete!")
    print(f"{'='*80}\n")

if __name__ == "__main__":
    # Check Oct 20-25, 2025
    check_cache_for_date_range("2025-10-20", "2025-10-25")

