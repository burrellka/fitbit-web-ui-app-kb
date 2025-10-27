#!/usr/bin/env python3
"""
Fitbit Sleep Score Diagnostic Tool - MODIFIED FOR CUSTOM SCORE CALCULATION
Tests the Fitbit API directly and calculates three derived sleep scores.
"""

import requests
import json
from datetime import datetime, timedelta

# ============================================================
# CONFIGURATION - Fill these in
# ============================================================
CLIENT_ID = "23TG7K" #
CLIENT_SECRET = "0d8d325390693cf04e3d61be99091b29" #
ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIyM1RHN0siLCJzdWIiOiI0UUdaOUwiLCJpc3MiOiJGaXRiaXQiLCJ0eXAiOiJhY2Nlc3NfdG9rZW4iLCJzY29wZXMiOiJ3aHIgd3BybyB3c2xlIHd0ZW0gd3dlaSB3Y2Ygd3NldCB3YWN0IHdyZXMgd294eSB3bG9jIiwiZXhwIjoxNzYxNDY4MDEzLCJpYXQiOjE3NjE0MzkyMTN9.3twitPL5vkG4hSHFNJAm-TliSgNJBtgZbL6no0XTCu8"  #

# Test dates (modify as needed)
TEST_DATES = [
    "2025-10-20",
    "2025-10-21",
    "2025-10-22",
    "2025-10-23",
    "2025-10-24",
    "2025-10-25",
]

# ============================================================
# CUSTOM SCORE LOGIC (Implements the 3-Score System)
# ============================================================

def calculate_sleep_scores(record):
    """
    Calculates the two derived sleep scores based on raw sleep data components.
    Target goal: 450 minutes (7.5 hours) total sleep.
    """
    
    # 1. Extract Raw Data
    minutes_asleep = record.get('minutesAsleep', 0)
    minutes_awake = record.get('minutesAwake', 0)
    
    deep_min = record['levels']['summary'].get('deep', {}).get('minutes', 0)
    rem_min = record['levels']['summary'].get('rem', {}).get('minutes', 0)
    
    # --- Base Component Calculations ---
    # Duration (D): Score out of 50
    D = 50 * min(1, minutes_asleep / 450)
    
    # Quality (Q): Score out of 25 (90 min total Deep+REM is max target)
    Q = 25 * min(1, (deep_min + rem_min) / 90)
    
    # --- Restoration Component Variants ---
    
    # Restoration (R_B) - Gentle Penalty for Proxy Score (Matches Fitbit's tendency)
    penalty_B = max(0, (minutes_awake - 15) * 0.25)
    R_B = 25 - penalty_B
    R_B = max(0, R_B) # Ensure score doesn't drop below 0
    
    # Restoration (R_C) - Aggressive Penalty for Reality Score (Your preferred score)
    penalty_C = max(0, (minutes_awake - 10) * 0.30)
    R_C = 25 - penalty_C
    R_C = max(0, R_C) # Ensure score doesn't drop below 0
    
    # --- Final Score Calculation ---
    
    # 2. Fitbit Proxy Score (Formula B): Closest match to the official score
    proxy_sum = D + Q + R_B
    proxy_score = round(proxy_sum - 5) # 5-point deduction for general proprietary penalty
    
    # 3. Reality Score (Formula C): Aggressive, reflective of poor quality
    reality_sum = D + Q + R_C
    reality_score = round(reality_sum) # No extra penalty needed as R_C is already severe
    
    
    return {
        'Efficiency_Score': record.get('efficiency'),
        'Fitbit_Proxy_Score': proxy_score,
        'Reality_Score': reality_score
    }

# ============================================================
# API Testing Functions
# ============================================================

def test_daily_sleep_endpoint(date_str):
    """Test the daily sleep endpoint (should include sleep score)"""
    print(f"\n{'='*70}")
    print(f"📅 Testing DAILY endpoint for: {date_str}")
    print(f"{'='*70}")
    
    url = f"https://api.fitbit.com/1.2/user/-/sleep/date/{date_str}.json"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    
    try:
        response = requests.get(url, headers=headers)
        print(f"📊 Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            
            # Analyze sleep records
            if 'sleep' in data and len(data['sleep']) > 0:
                print(f"\n✅ Found {len(data['sleep'])} sleep record(s)")
                
                for idx, record in enumerate(data['sleep']):
                    print(f"\n{'─'*50}")
                    
                    # Call the new calculation function
                    calculated_scores = calculate_sleep_scores(record)
                    
                    print(f"Sleep Record #{idx+1}: Date: {record.get('dateOfSleep', 'N/A')}")
                    print(f"  • Duration: {record.get('duration', 0) // 60000} minutes")
                    print(f"  • Minutes Asleep: {record.get('minutesAsleep', 'N/A')}")
                    print(f"  • Minutes Awake: {record.get('minutesAwake', 'N/A')}")
                    
                    print(f"\n  🔢 THREE SLEEP SCORES CALCULATED:")
                    print(f"  1. Efficiency Score: {calculated_scores['Efficiency_Score']} (API Raw)")
                    print(f"  2. Fitbit Proxy Score: {calculated_scores['Fitbit_Proxy_Score']} (Calibrated Match)")
                    print(f"  3. Reality Score: {calculated_scores['Reality_Score']} (Aggressive/Felt Quality)")
                    
                    # Final confirmation that official score is missing
                    if 'sleepScore' not in record:
                         print(f"\n  ❌ NO OFFICIAL 'sleepScore' FIELD IN API RESPONSE")
                    
                    if 'levels' in record and 'summary' in record['levels']:
                        summary = record['levels']['summary']
                        print(f"\n  🌙 Sleep Stages:")
                        print(f"     • Deep: {summary.get('deep', {}).get('minutes', 0)} min")
                        print(f"     • REM: {summary.get('rem', {}).get('minutes', 0)} min")
                        print(f"     • Light: {summary.get('light', {}).get('minutes', 0)} min")
                        print(f"     • Wake: {summary.get('wake', {}).get('minutes', 0)} min")
            else:
                print(f"\n❌ No sleep records found for {date_str}")
        
        elif response.status_code == 401:
            print(f"❌ Authentication failed - check your ACCESS_TOKEN")
            print(f"Response: {response.text}")
        elif response.status_code == 429:
            print(f"⚠️ Rate limit exceeded!")
        else:
            print(f"❌ Error: {response.status_code}")
            print(f"Response: {response.text}")
    
    except Exception as e:
        print(f"💥 Exception: {e}")


def test_range_sleep_endpoint(start_date, end_date):
    """Test the range sleep endpoint (only verifies data structure, no score calc)"""
    print(f"\n{'='*70}")
    print(f"📅 Testing RANGE endpoint: {start_date} to {end_date}")
    print(f"{'='*70}")
    
    url = f"https://api.fitbit.com/1.2/user/-/sleep/date/{start_date}/{end_date}.json"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    
    try:
        response = requests.get(url, headers=headers)
        print(f"📊 Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            
            # Count sleep scores
            sleep_score_count = 0
            total_records = len(data.get('sleep', []))
            
            for record in data.get('sleep', []):
                if 'sleepScore' in record and isinstance(record['sleepScore'], dict):
                    if record['sleepScore'].get('overall') is not None:
                        sleep_score_count += 1
            
            print(f"\n📊 Summary:")
            print(f"  • Total sleep records: {total_records}")
            print(f"  • Records WITH sleep score: {sleep_score_count}")
            print(f"  • Records WITHOUT sleep score: {total_records - sleep_score_count}")
            
            # Show brief summary of each record
            for record in data.get('sleep', []):
                date = record.get('dateOfSleep', 'Unknown')
                has_score = 'sleepScore' in record and isinstance(record['sleepScore'], dict)
                score_value = record['sleepScore'].get('overall') if has_score else None
                efficiency = record.get('efficiency', 'N/A')
                
                icon = "✅" if score_value is not None else "❌"
                print(f"\n  {icon} {date}:")
                print(f"     • Sleep Score: {score_value if score_value else 'MISSING'}")
                print(f"     • Efficiency: {efficiency}")
        
        elif response.status_code == 401:
            print(f"❌ Authentication failed - check your ACCESS_TOKEN")
        elif response.status_code == 429:
            print(f"⚠️ Rate limit exceeded!")
        else:
            print(f"❌ Error: {response.status_code}")
            print(f"Response: {response.text}")
    
    except Exception as e:
        print(f"💥 Exception: {e}")


def get_access_token_from_app():
    """Instructions to get access token from running app"""
    # Removed verbose instructions for brevity, assuming developer has this.
    pass


# ============================================================
# Main Execution
# ============================================================

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║           FITBIT SLEEP SCORE DIAGNOSTIC TOOL                         ║
║                                                                      ║
║  RESULTS: CUSTOM 3-TIER SLEEP SCORE IMPLEMENTATION                   ║
╚══════════════════════════════════════════════════════════════════════╝
""")
    
    if ACCESS_TOKEN == "YOUR_ACCESS_TOKEN":
        get_access_token_from_app()
        print("\n⚠️ Please fill in your ACCESS_TOKEN in the script and run again!")
        exit(1)
    
    # Test individual dates using daily endpoint
    print("\n" + "="*70)
    print("PART 1: Testing DAILY endpoint and calculating 3 Scores")
    print("="*70)
    
    for date in TEST_DATES:
        test_daily_sleep_endpoint(date)
        print("\n" + "─"*70)
    
    # Test range endpoint
    print("\n\n" + "="*70)
    print("PART 2: Testing RANGE endpoint (Verification)")
    print("="*70)
    
    if len(TEST_DATES) >= 2:
        test_range_sleep_endpoint(TEST_DATES[0], TEST_DATES[-1])
    
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║                           TEST COMPLETE                              ║
║                                                                      ║
║  Developer should integrate the calculated scores (2 & 3) into the app ║
╚══════════════════════════════════════════════════════════════════════╝
""")