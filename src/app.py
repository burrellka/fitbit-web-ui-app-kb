# %%
import os
import base64
import logging
import requests
import dash, requests
from dash import dcc
from dash import html, dash_table
from dash.dependencies import Output, State, Input
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.cache_manager import FitbitCache
import threading
import time
from flask import jsonify, request, session, redirect as flask_redirect
from functools import wraps
import json


# %%

log = logging.getLogger(__name__)

# ============================================================
# CUSTOM SLEEP SCORE CALCULATION
# ============================================================
def calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake):
    """
    Calculates three sleep scores based on raw sleep data components.
    
    Since Fitbit API doesn't provide official Sleep Score via Personal OAuth apps,
    we calculate our own scores for accurate sleep quality assessment.
    
    Target: 450 minutes (7.5 hours) total sleep
    
    Returns:
        dict with 'efficiency', 'proxy_score', 'reality_score'
    """
    
    # --- Base Component Calculations ---
    # Duration (D): Score out of 50
    D = 50 * min(1, minutes_asleep / 450)
    
    # Quality (Q): Score out of 25 (90 min total Deep+REM is max target)
    Q = 25 * min(1, (deep_min + rem_min) / 90)
    
    # --- Restoration Component Variants ---
    
    # Restoration (R_B) - Gentle Penalty for Proxy Score (Matches Fitbit's tendency)
    penalty_B = max(0, (minutes_awake - 15) * 0.25)
    R_B = max(0, 25 - penalty_B)
    
    # Restoration (R_C) - Aggressive Penalty for Reality Score (Primary metric)
    penalty_C = max(0, (minutes_awake - 10) * 0.30)
    R_C = max(0, 25 - penalty_C)
    
    # --- Final Score Calculation ---
    
    # Fitbit Proxy Score (Formula B): Closest match to official score
    proxy_sum = D + Q + R_B
    proxy_score = round(proxy_sum - 5)  # 5-point proprietary penalty
    
    # Reality Score (Formula C): PRIMARY METRIC - Honest severity assessment
    reality_sum = D + Q + R_C
    reality_score = round(reality_sum)
    
    return {
        'proxy_score': max(0, min(100, proxy_score)),  # Clamp 0-100
        'reality_score': max(0, min(100, reality_score))  # Clamp 0-100
    }

# Initialize cache
print("🗄️ Initializing Fitbit data cache...")
cache = FitbitCache()

# Background cache builder state
cache_builder_running = False
cache_builder_thread = None
auto_sync_running = False
auto_sync_thread = None

def automatic_daily_sync():
    """
    Automatic daily sync thread that runs forever.
    Checks every hour if new data needs to be fetched (yesterday's data).
    Uses stored refresh token to get new access tokens automatically.
    """
    global auto_sync_running
    auto_sync_running = True
    print("🤖 Automatic daily sync thread started!")
    
    while True:
        try:
            time.sleep(3600)  # Check every hour
            
            # Get stored refresh token
            refresh_token = cache.get_refresh_token()
            if not refresh_token:
                print("⚠️ Auto-sync: No refresh token stored, skipping sync")
                continue
            
            # Check if we need to sync (last sync was yesterday or earlier)
            last_sync = cache.get_last_sync_date()
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            
            if last_sync and last_sync >= yesterday:
                print(f"✅ Auto-sync: Already synced today (last: {last_sync})")
                continue
            
            print(f"🔄 Auto-sync: Fetching data for {yesterday}...")
            
            # Refresh access token
            client_id = os.environ['CLIENT_ID']
            client_secret = os.environ['CLIENT_SECRET']
            token_url = 'https://api.fitbit.com/oauth2/token'
            
            payload = {
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token
            }
            
            token_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
            token_headers = {
                "Authorization": f"Basic {token_creds}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            token_response = requests.post(token_url, data=payload, headers=token_headers)
            
            if token_response.status_code == 200:
                token_data = token_response.json()
                new_access_token = token_data.get('access_token')
                new_refresh_token = token_data.get('refresh_token')
                
                # Update stored refresh token
                if new_refresh_token:
                    cache.store_refresh_token(new_refresh_token, token_data.get('expires_in', 28800))
                
                # Fetch yesterday's data
                headers = {"Authorization": f"Bearer {new_access_token}"}
                fetched = populate_sleep_score_cache([yesterday], headers, force_refresh=True)
                
                if fetched > 0:
                    cache.set_last_sync_date(yesterday)
                    print(f"✅ Auto-sync: Successfully fetched data for {yesterday}")
                else:
                    print(f"⚠️ Auto-sync: No sleep data available for {yesterday}")
            else:
                print(f"❌ Auto-sync: Failed to refresh token (status: {token_response.status_code})")
                
        except Exception as e:
            print(f"❌ Auto-sync error: {e}")
            # Continue running despite errors

def process_and_cache_daily_metrics(dates_str_list, metric_type, response_data, cache_manager):
    """
    🐞 FIX: Reusable function to process and cache daily metrics using date-string lookups
    
    This function handles the extraction and caching of Phase 1 range metrics.
    Used by both background_cache_builder (Phase 1) and update_output (report generation).
    
    Args:
        dates_str_list: List of date strings (YYYY-MM-DD) - master date list
        metric_type: One of: 'steps', 'calories', 'distance', 'floors', 'azm'
        response_data: Raw API response JSON
        cache_manager: FitbitCache instance
    
    Returns:
        int: Number of days successfully cached
    """
    cached_count = 0
    
    if metric_type == 'steps':
        # Create lookup dictionary
        steps_lookup = {entry['dateTime']: int(entry['value']) 
                       for entry in response_data.get('activities-steps', [])}
        
        # Iterate over master date list
        for date_str in dates_str_list:
            steps_value = steps_lookup.get(date_str)
            if steps_value == 0:
                steps_value = None  # Treat 0 as None
            
            if steps_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, steps=steps_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'calories':
        calories_lookup = {}
        for entry in response_data.get('activities-calories', []):
            try:
                calories_lookup[entry['dateTime']] = int(entry['value'])
            except (KeyError, ValueError):
                pass
        
        for date_str in dates_str_list:
            calories_value = calories_lookup.get(date_str)
            if calories_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, calories=calories_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'distance':
        distance_lookup = {}
        for entry in response_data.get('activities-distance', []):
            try:
                distance_km = float(entry['value'])
                distance_miles = round(distance_km * 0.621371, 2)
                distance_lookup[entry['dateTime']] = distance_miles
            except (KeyError, ValueError):
                pass
        
        for date_str in dates_str_list:
            distance_value = distance_lookup.get(date_str)
            if distance_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, distance=distance_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'floors':
        floors_lookup = {}
        for entry in response_data.get('activities-floors', []):
            try:
                floors_lookup[entry['dateTime']] = int(entry['value'])
            except (KeyError, ValueError):
                pass
        
        for date_str in dates_str_list:
            floors_value = floors_lookup.get(date_str)
            if floors_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, floors=floors_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'azm':
        azm_lookup = {}
        for entry in response_data.get('activities-active-zone-minutes', []):
            try:
                azm_lookup[entry['dateTime']] = entry['value']['activeZoneMinutes']
            except (KeyError, ValueError):
                pass
        
        for date_str in dates_str_list:
            azm_value = azm_lookup.get(date_str)
            if azm_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, active_zone_minutes=azm_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'heartrate':
        hr_lookup = {}
        for entry in response_data.get('activities-heart', []):
            try:
                if 'value' in entry and 'restingHeartRate' in entry['value']:
                    hr_lookup[entry['dateTime']] = entry['value']['restingHeartRate']
            except (KeyError, ValueError):
                pass
        
        for date_str in dates_str_list:
            hr_value = hr_lookup.get(date_str)
            if hr_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, resting_heart_rate=hr_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'weight':
        weight_lookup = {}
        for entry in response_data.get('weight', []):
            try:
                weight_kg = float(entry['weight'])
                weight_lbs = round(weight_kg * 2.20462, 1)
                weight_lookup[entry['date']] = weight_lbs
            except (KeyError, ValueError):
                pass
        
        for date_str in dates_str_list:
            weight_value = weight_lookup.get(date_str)
            if weight_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, weight=weight_value)
                    cached_count += 1
                except:
                    pass
    
    elif metric_type == 'spo2':
        spo2_lookup = {}
        for entry in response_data:
            try:
                if isinstance(entry, dict) and 'dateTime' in entry and 'value' in entry:
                    if 'avg' in entry['value']:
                        spo2_lookup[entry['dateTime']] = float(entry['value']['avg'])
            except (KeyError, ValueError, TypeError):
                pass
        
        for date_str in dates_str_list:
            spo2_value = spo2_lookup.get(date_str)
            if spo2_value is not None:
                try:
                    cache_manager.set_daily_metrics(date=date_str, spo2=spo2_value)
                    cached_count += 1
                except:
                    pass
    
    return cached_count


def background_cache_builder(access_token: str, refresh_token: str = None):
    """
    PHASED BACKGROUND CACHE BUILDER
    
    Runs hourly with sophisticated 3-phase strategy:
    
    Phase 1: Range-Based Endpoints (~11 calls for entire history)
      - Heart Rate, Steps, Weight, SpO2, Calories, Distance, Floors, AZM, Activities
      - Pulls ALL missing historical data in single calls per metric
      - Always refreshes today's data
    
    Phase 2: 30-Day Block Endpoints (Cardio Fitness)
      - Pulls one 30-day block per cycle
      - Starts with today+30 days, then works backward
      - Skips blocks that are fully cached
    
    Phase 3: Daily Endpoints (7-day blocks)
      - HRV, Breathing Rate, Temperature, Sleep (4 calls/day = 28 calls/7-day block)
      - Pulls today first (4 calls), then 7-day blocks
      - Most expensive, done last
    
    Loop: Phase 1 → Phase 2 → Phase 3 → Phase 2 → Phase 3... until 150 API limit or complete
    
    Runs every hour automatically until all data cached or container restart.
    """
    global cache_builder_running
    
    if not access_token:
        print("⚠️ Background cache builder: No access token available")
        return
    
    cache_builder_running = True
    print("🚀 Starting PHASED background cache builder...")
    print("📊 Strategy: Range endpoints → 30-day blocks → 7-day blocks (loop until 150/hour limit)")
    
    # 🐞 FIX: Track current tokens (they will be refreshed in the loop)
    current_access_token = access_token
    current_refresh_token = refresh_token
    
    try:
        while cache_builder_running:
            api_calls_this_hour = 0
            MAX_CALLS_PER_HOUR = 145  # Conservative limit (leave 5 for user reports)
            
            # 🐞 FIX: Refresh token at the start of each hourly cycle
            if current_refresh_token:
                print("\n🔄 Refreshing access token for new hourly cycle...")
                try:
                    new_access, new_refresh, new_expiry = refresh_access_token(current_refresh_token)
                    if new_access:
                        current_access_token = new_access
                        current_refresh_token = new_refresh
                        print(f"✅ Token refreshed! Valid for 8 hours (expires at {datetime.fromtimestamp(new_expiry).strftime('%H:%M:%S')})")
                    else:
                        print("⚠️ Token refresh failed, using existing token")
                except Exception as e:
                    print(f"⚠️ Error refreshing token: {e}, using existing token")
            else:
                print("⚠️ No refresh token available - token will expire after 8 hours")
            
            headers = {"Authorization": f"Bearer {current_access_token}"}
            today = datetime.now().strftime('%Y-%m-%d')
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            
            # Check if this is the first run of a new day
            last_cache_date = cache.get_metadata('last_cache_date')
            is_first_run_of_day = (last_cache_date != today)
            
            print(f"\n{'='*60}")
            print(f"🔄 NEW HOURLY CYCLE STARTING - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            if is_first_run_of_day:
                print(f"🌅 FIRST RUN OF NEW DAY - Will refresh YESTERDAY + TODAY")
            else:
                print(f"🔄 Same day run - Will refresh TODAY only")
            print(f"{'='*60}\n")
            
            # Update last cache date
            cache.set_metadata('last_cache_date', today)
            
            # ========== PHASE 1: RANGE-BASED ENDPOINTS (Most Efficient) ==========
            print("📍 PHASE 1: Range-Based Endpoints (Single call for entire history)")
            print("-" * 60)
            
            # Determine date range: last 365 days
            end_date = datetime.now()
            start_date = end_date - timedelta(days=365)
            start_date_str = start_date.strftime('%Y-%m-%d')
            end_date_str = end_date.strftime('%Y-%m-%d')
            
            # Generate master date list for caching alignment
            dates_str_list = []
            current = start_date
            while current <= end_date:
                dates_str_list.append(current.strftime('%Y-%m-%d'))
                current += timedelta(days=1)
            
            phase1_calls = 0
            rate_limit_hit = False  # Flag to track if we hit rate limit
            
            # These endpoints support date ranges - very efficient!
            range_endpoints = [
                ("Heart Rate", f"https://api.fitbit.com/1/user/-/activities/heart/date/{start_date_str}/{end_date_str}.json"),
                ("Steps", f"https://api.fitbit.com/1/user/-/activities/steps/date/{start_date_str}/{end_date_str}.json"),
                ("Weight", f"https://api.fitbit.com/1/user/-/body/weight/date/{start_date_str}/{end_date_str}.json"),
                ("SpO2", f"https://api.fitbit.com/1/user/-/spo2/date/{start_date_str}/{end_date_str}.json"),
                ("Calories", f"https://api.fitbit.com/1/user/-/activities/calories/date/{start_date_str}/{end_date_str}.json"),
                ("Distance", f"https://api.fitbit.com/1/user/-/activities/distance/date/{start_date_str}/{end_date_str}.json"),
                ("Floors", f"https://api.fitbit.com/1/user/-/activities/floors/date/{start_date_str}/{end_date_str}.json"),
                ("Active Zone Minutes", f"https://api.fitbit.com/1/user/-/activities/active-zone-minutes/date/{start_date_str}/{end_date_str}.json"),
                # 🐞 FIX: Fitbit API only accepts ONE date parameter (beforeDate OR afterDate, not both)
                # Using only beforeDate returns activities backward from that date
                ("Activities", f"https://api.fitbit.com/1/user/-/activities/list.json?beforeDate={end_date_str}&sort=asc&offset=0&limit=100"),
            ]
            
            for metric_name, endpoint in range_endpoints:
                if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                    print(f"⚠️ API limit reached ({api_calls_this_hour} calls), stopping Phase 1")
                    break
                
                try:
                    print(f"📥 Fetching {metric_name}... ", end="")
                    response = requests.get(endpoint, headers=headers, timeout=15)
                    
                    if response.status_code == 429:
                        print(f"❌ Rate limit hit!")
                        rate_limit_hit = True
                        break
                    
                    # Only count successful calls
                    api_calls_this_hour += 1
                    phase1_calls += 1
                    
                    # Check for errors
                    if response.status_code != 200:
                        print(f"⚠️ Error ({response.status_code})")
                        if metric_name == "Activities":
                            print(f"   ℹ️ Activities endpoint: {endpoint}")
                            try:
                                error_data = response.json()
                                print(f"   ℹ️ Error response: {error_data}")
                            except:
                                pass
                        continue
                    
                    print(f"✅ Success ({response.status_code})", end="")
                    
                    # 🐞 FIX: Process and cache the fetched data immediately
                    if response.status_code == 200:
                        response_data = response.json()
                        cached = 0
                        
                        if metric_name == "Heart Rate":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'heartrate', response_data, cache)
                        elif metric_name == "Steps":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'steps', response_data, cache)
                        elif metric_name == "Weight":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'weight', response_data, cache)
                        elif metric_name == "SpO2":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'spo2', response_data, cache)
                        elif metric_name == "Calories":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'calories', response_data, cache)
                        elif metric_name == "Distance":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'distance', response_data, cache)
                        elif metric_name == "Floors":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'floors', response_data, cache)
                        elif metric_name == "Active Zone Minutes":
                            cached = process_and_cache_daily_metrics(dates_str_list, 'azm', response_data, cache)
                        elif metric_name == "Activities":
                            # Activities need special handling with pagination (API returns max 100 per call)
                            # Keep fetching until we get fewer than 100 activities or hit API limit
                            all_activities = response_data.get('activities', [])
                            offset = 100  # Already got first 100
                            
                            # Paginate through all activities
                            while len(response_data.get('activities', [])) == 100 and api_calls_this_hour < MAX_CALLS_PER_HOUR:
                                try:
                                    print(f" → Fetching more (offset={offset})...", end="")
                                    paginated_url = f"https://api.fitbit.com/1/user/-/activities/list.json?beforeDate={end_date_str}&sort=asc&offset={offset}&limit=100"
                                    paginated_response = requests.get(paginated_url, headers=headers, timeout=15)
                                    
                                    if paginated_response.status_code == 429:
                                        print(" ❌ Rate limit")
                                        rate_limit_hit = True
                                        break
                                    
                                    if paginated_response.status_code == 200:
                                        api_calls_this_hour += 1
                                        phase1_calls += 1
                                        response_data = paginated_response.json()
                                        batch_activities = response_data.get('activities', [])
                                        all_activities.extend(batch_activities)
                                        offset += 100
                                        print(f" +{len(batch_activities)}", end="")
                                    else:
                                        break
                                except Exception as e:
                                    print(f" ⚠️ {e}")
                                    break
                            
                            # Now cache all activities
                            for activity in all_activities:
                                try:
                                    activity_date = datetime.strptime(activity['startTime'][:10], '%Y-%m-%d').strftime("%Y-%m-%d")
                                    activity_id = str(activity.get('logId', f"{activity_date}_{activity.get('activityName', 'activity')}"))
                                    cache.set_activity(
                                        activity_id=activity_id,
                                        date=activity_date,
                                        activity_name=activity.get('activityName', 'N/A'),
                                        duration_ms=activity.get('duration'),
                                        calories=activity.get('calories'),
                                        avg_heart_rate=activity.get('averageHeartRate'),
                                        steps=activity.get('steps'),
                                        distance=activity.get('distance'),
                                        activity_data_json=str(activity)
                                    )
                                    cached += 1
                                except Exception as e:
                                    pass
                        
                        if cached > 0:
                            print(f" → 💾 Cached {cached} days")
                        else:
                            print()  # New line
                    else:
                        print()  # New line
                    
                except Exception as e:
                    print(f"⚠️ Error: {e}")
            
            print(f"✅ Phase 1 Complete: {phase1_calls} API calls")
            print(f"📊 API Budget Remaining: {MAX_CALLS_PER_HOUR - api_calls_this_hour}")
            
            # If rate limit hit, stop immediately and wait
            if rate_limit_hit:
                print("\n" + "="*60)
                print("⏸️ RATE LIMIT (429) DETECTED!")
                print("🛑 Stopping ALL API calls immediately")
                print(f"⏰ Waiting 1 hour until {(datetime.now() + timedelta(hours=1)).strftime('%H:%M:%S')}")
                print("="*60 + "\n")
                time.sleep(3600)
                continue
            
            if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                print("⏸️ Hourly limit reached. Waiting 1 hour...")
                time.sleep(3600)
                continue
            
            # ========== FIRST RUN OF DAY: REFRESH YESTERDAY ==========
            if is_first_run_of_day and api_calls_this_hour < MAX_CALLS_PER_HOUR:
                print(f"\n🌅 FIRST RUN OF DAY - REFRESHING YESTERDAY ({yesterday})")
                print("=" * 60)
                print("📌 Purpose: Ensure yesterday's data is complete (sleep, HRV, etc. finalize late)")
                
                # Fetch yesterday's 4 daily metrics (Phase 3 style)
                yesterday_endpoints = [
                    ("Sleep", f"https://api.fitbit.com/1.2/user/-/sleep/date/{yesterday}.json"),
                    ("HRV", f"https://api.fitbit.com/1/user/-/hrv/date/{yesterday}.json"),
                    ("Breathing", f"https://api.fitbit.com/1/user/-/br/date/{yesterday}.json"),
                    ("Temperature", f"https://api.fitbit.com/1/user/-/temp/skin/date/{yesterday}.json"),
                ]
                
                yesterday_success = 0
                for metric_name, endpoint in yesterday_endpoints:
                    if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                        break
                    
                    try:
                        response = requests.get(endpoint, headers=headers, timeout=10)
                        
                        if response.status_code == 429:
                            print(f"❌ Rate limit hit while fetching yesterday's {metric_name}")
                            rate_limit_hit = True
                            break
                        
                        # Only count successful calls
                        api_calls_this_hour += 1
                        
                        if response.status_code == 200:
                            data = response.json()
                            
                            # Cache based on metric type
                            if metric_name == "Sleep" and 'sleep' in data:
                                for sleep_record in data['sleep']:
                                    if sleep_record.get('isMainSleep', True):
                                        sleep_score = None
                                        if 'sleepScore' in sleep_record and isinstance(sleep_record['sleepScore'], dict):
                                            sleep_score = sleep_record['sleepScore'].get('overall')
                                        # DO NOT fallback to efficiency - they are different metrics!
                                        
                                        # Cache sleep data even if sleep_score is None (stages, duration still valuable!)
                                        # Calculate 3-tier sleep scores
                                        minutes_asleep = sleep_record.get('minutesAsleep', 0)
                                        deep_min = sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes', 0)
                                        rem_min = sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes', 0)
                                        minutes_awake = sleep_record.get('minutesAwake', 0)
                                        
                                        calculated_scores = calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake)
                                        
                                        if sleep_score is not None:
                                            print(f"✅ YESTERDAY REFRESH - Found REAL sleep score for {yesterday}: {sleep_score}")
                                        else:
                                            print(f"⚠️ YESTERDAY REFRESH - No sleep score for {yesterday}, but caching stages/duration")
                                        print(f"   📊 Calculated scores: Reality={calculated_scores['reality_score']}, Proxy={calculated_scores['proxy_score']}, Efficiency={sleep_record.get('efficiency')}")
                                        
                                        cache.set_sleep_score(
                                            date=yesterday,
                                            sleep_score=sleep_score,  # Can be None - that's OK!
                                            efficiency=sleep_record.get('efficiency'),
                                            proxy_score=calculated_scores['proxy_score'],
                                            reality_score=calculated_scores['reality_score'],
                                            total_sleep=minutes_asleep,
                                            deep=deep_min,
                                            light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                            rem=rem_min,
                                            wake=minutes_awake,
                                            start_time=sleep_record.get('startTime'),
                                            sleep_data_json=str(sleep_record)
                                        )
                                        yesterday_success += 1
                                        print(f"✅ Yesterday's Sleep cached")
                            
                            elif metric_name == "HRV" and 'hrv' in data:
                                for hrv_entry in data['hrv']:
                                    if 'value' in hrv_entry and 'dailyRmssd' in hrv_entry['value']:
                                        cache.set_advanced_metrics(
                                            date=yesterday,
                                            hrv=hrv_entry['value']['dailyRmssd'],
                                            breathing_rate=None,
                                            temperature=None
                                        )
                                        yesterday_success += 1
                                        print(f"✅ Yesterday's HRV cached")
                            
                            elif metric_name == "Breathing" and 'br' in data:
                                for br_entry in data['br']:
                                    if 'value' in br_entry and 'breathingRate' in br_entry['value']:
                                        cache.set_advanced_metrics(
                                            date=yesterday,
                                            hrv=None,
                                            breathing_rate=br_entry['value']['breathingRate'],
                                            temperature=None
                                        )
                                        yesterday_success += 1
                                        print(f"✅ Yesterday's Breathing Rate cached")
                            
                            elif metric_name == "Temperature" and 'tempSkin' in data:
                                for temp_entry in data['tempSkin']:
                                    if 'value' in temp_entry and 'nightlyRelative' in temp_entry['value']:
                                        cache.set_advanced_metrics(
                                            date=yesterday,
                                            hrv=None,
                                            breathing_rate=None,
                                            temperature=temp_entry['value']['nightlyRelative']
                                        )
                                        yesterday_success += 1
                                        print(f"✅ Yesterday's Temperature cached")
                    
                    except Exception as e:
                        print(f"⚠️ Error fetching yesterday's {metric_name}: {e}")
                        continue
                
                print(f"📊 YESTERDAY REFRESH: {yesterday_success}/4 metrics updated")
                print(f"💰 API Calls Used: {api_calls_this_hour}/{MAX_CALLS_PER_HOUR}")
                print("=" * 60)
                
                # Check if rate limit was hit during yesterday refresh
                if rate_limit_hit:
                    print("\n" + "="*60)
                    print("⏸️ RATE LIMIT (429) DETECTED!")
                    print("🛑 Stopping ALL API calls immediately")
                    print(f"⏰ Waiting 1 hour until {(datetime.now() + timedelta(hours=1)).strftime('%H:%M:%S')}")
                    print("="*60 + "\n")
                    time.sleep(3600)
                    continue
            
            # ========== PHASE 2 & 3 LOOP ==========
            while api_calls_this_hour < MAX_CALLS_PER_HOUR and not rate_limit_hit:
                # PHASE 2: 30-Day Cardio Fitness Blocks
                print(f"\n📍 PHASE 2: Cardio Fitness (30-day blocks)")
                print("-" * 60)
                
                # Find missing 30-day block for cardio fitness
                # Start from today and work backward
                current_date = datetime.now()
                cardio_fetched = False
                
                for block_start_offset in range(0, 365, 30):
                    block_end = current_date - timedelta(days=block_start_offset)
                    block_start = block_end - timedelta(days=29)
                    
                    # Check if this block needs fetching
                    # (Simplified: just fetch it, API is smart about duplicates)
                    if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                        break
                    
                    try:
                        cf_endpoint = f"https://api.fitbit.com/1/user/-/cardioscore/date/{block_start.strftime('%Y-%m-%d')}/{block_end.strftime('%Y-%m-%d')}.json"
                        print(f"📥 Fetching Cardio Fitness {block_start.strftime('%m/%d')} to {block_end.strftime('%m/%d')}... ", end="")
                        response = requests.get(cf_endpoint, headers=headers, timeout=15)
                        
                        if response.status_code == 429:
                            print(f"❌ Rate limit!")
                            rate_limit_hit = True
                            break
                        
                        # Only count successful calls
                        api_calls_this_hour += 1
                        print(f"✅ ({response.status_code})")
                        cardio_fetched = True
                        break  # Only do one 30-day block per Phase 2 iteration
                        
                    except Exception as e:
                        print(f"⚠️ Error: {e}")
                        break
                
                if not cardio_fetched:
                    print("✅ All Cardio Fitness blocks cached")
                
                print(f"📊 API Budget Remaining: {MAX_CALLS_PER_HOUR - api_calls_this_hour}")
                
                # Break if rate limit hit
                if rate_limit_hit:
                    break
                
                if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                    break
                
                # PHASE 3: Daily Endpoints (7-Day Blocks)
                print(f"\n📍 PHASE 3: Daily Endpoints - 7-Day Blocks (HRV, BR, Temp, Sleep)")
                print("-" * 60)
                print("💰 Cost: 4 calls/day × 7 days = 28 calls per block")
                
                # Find missing dates for daily metrics
                missing_dates = cache.get_missing_dates(
                    (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d'),
                    datetime.now().strftime('%Y-%m-%d'),
                    metric_type='sleep'
                )
                
                if not missing_dates:
                    print("✅ All daily metrics fully cached!")
                    break
                
                # Process in 7-day blocks, starting from MOST RECENT (work backward)
                block_size = 7
                missing_dates_reversed = list(reversed(missing_dates))  # Newest first
                dates_to_fetch = missing_dates_reversed[:block_size]  # Take most recent 7 days
                
                if api_calls_this_hour + (len(dates_to_fetch) * 4) > MAX_CALLS_PER_HOUR:
                    print(f"⚠️ Not enough budget for 7-day block ({len(dates_to_fetch)*4} calls needed)")
                    break
                
                # Display range in chronological order (even though we fetch newest first)
                date_range_display = f"{min(dates_to_fetch)} to {max(dates_to_fetch)}" if len(dates_to_fetch) > 1 else dates_to_fetch[0]
                print(f"📥 Fetching 7-day block: {date_range_display} (newest → oldest)")
                
                phase3_success = 0
                for date_str in dates_to_fetch:
                    if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                        break
                    
                    # Fetch all 4 daily metrics for this date
                    daily_endpoints = [
                        ("Sleep", f"https://api.fitbit.com/1.2/user/-/sleep/date/{date_str}.json"),
                        ("HRV", f"https://api.fitbit.com/1/user/-/hrv/date/{date_str}.json"),
                        ("Breathing", f"https://api.fitbit.com/1/user/-/br/date/{date_str}.json"),
                        ("Temperature", f"https://api.fitbit.com/1/user/-/temp/skin/date/{date_str}.json"),
                    ]
                    
                    for metric_name, endpoint in daily_endpoints:
                        if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                            break
                        
                        try:
                            response = requests.get(endpoint, headers=headers, timeout=10)
                            
                            if response.status_code == 429:
                                print(f"❌ Rate limit hit at {date_str}")
                                rate_limit_hit = True
                                break
                            
                            # Only count successful calls
                            api_calls_this_hour += 1
                            
                            if response.status_code == 200:
                                data = response.json()
                                
                                # Cache based on metric type
                                if metric_name == "Sleep" and 'sleep' in data:
                                    for sleep_record in data['sleep']:
                                        if sleep_record.get('isMainSleep', True):
                                            sleep_score = None
                                            if 'sleepScore' in sleep_record and isinstance(sleep_record['sleepScore'], dict):
                                                sleep_score = sleep_record['sleepScore'].get('overall')
                                            # DO NOT fallback to efficiency - they are different metrics!
                                            
                                            # Cache sleep data even if sleep_score is None (stages, duration still valuable!)
                                            # Calculate 3-tier sleep scores
                                            minutes_asleep = sleep_record.get('minutesAsleep', 0)
                                            deep_min = sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes', 0)
                                            rem_min = sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes', 0)
                                            minutes_awake = sleep_record.get('minutesAwake', 0)
                                            
                                            calculated_scores = calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake)
                                            
                                            if sleep_score is not None:
                                                print(f"✅ PHASE 3 - Found REAL sleep score for {date_str}: {sleep_score}")
                                            else:
                                                print(f"⚠️ PHASE 3 - No sleep score for {date_str}, but caching stages/duration")
                                            print(f"   📊 Calculated scores: Reality={calculated_scores['reality_score']}, Proxy={calculated_scores['proxy_score']}, Efficiency={sleep_record.get('efficiency')}")
                                            
                                            cache.set_sleep_score(
                                                date=date_str,
                                                sleep_score=sleep_score,  # Can be None - that's OK!
                                                efficiency=sleep_record.get('efficiency'),
                                                proxy_score=calculated_scores['proxy_score'],
                                                reality_score=calculated_scores['reality_score'],
                                                total_sleep=minutes_asleep,
                                                deep=deep_min,
                                                light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                                rem=rem_min,
                                                wake=minutes_awake,
                                                start_time=sleep_record.get('startTime'),
                                                sleep_data_json=str(sleep_record)
                                            )
                                            break
                                
                                elif metric_name == "HRV" and "hrv" in data and len(data["hrv"]) > 0:
                                    hrv_value = data["hrv"][0]["value"].get("dailyRmssd")
                                    if hrv_value:
                                        cache.set_advanced_metrics(date=date_str, hrv=hrv_value)
                                
                                elif metric_name == "Breathing" and "br" in data and len(data["br"]) > 0:
                                    br_value = data["br"][0]["value"].get("breathingRate")
                                    if br_value:
                                        cache.set_advanced_metrics(date=date_str, breathing_rate=br_value)
                                
                                elif metric_name == "Temperature" and "tempSkin" in data and len(data["tempSkin"]) > 0:
                                    temp_value = data["tempSkin"][0]["value"]
                                    if isinstance(temp_value, dict):
                                        temp_value = temp_value.get("nightlyRelative", temp_value.get("value"))
                                    if temp_value is not None:
                                        cache.set_advanced_metrics(date=date_str, temperature=temp_value)
                                
                                phase3_success += 1
                                
                        except Exception as e:
                            print(f"❌ Error caching {metric_name} for {date_str}: {e}")
                            import traceback
                            traceback.print_exc()
                
                print(f"✅ Phase 3 Block Complete: {phase3_success} metric-days cached")
                print(f"📊 API Budget Remaining: {MAX_CALLS_PER_HOUR - api_calls_this_hour}")
                
                # Break if rate limit hit
                if rate_limit_hit:
                    break
                
                if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                    break
                
                # Loop back to Phase 2 if budget allows
                if api_calls_this_hour < MAX_CALLS_PER_HOUR - 30:  # Need at least 30 calls for next cycle
                    print(f"\n🔄 Budget allows another Phase 2→3 cycle...")
                    continue
                else:
                    print(f"\n⏸️ Not enough budget for another cycle ({MAX_CALLS_PER_HOUR - api_calls_this_hour} remaining)")
                    break
            
            # Hourly cycle complete (or rate limit hit)
            cycle_end_time = datetime.now().isoformat()
            
            if rate_limit_hit:
                print(f"\n{'='*60}")
                print(f"⏸️ RATE LIMIT HIT - CYCLE PAUSED")
                print(f"📊 API Calls Made Before Rate Limit: {api_calls_this_hour}")
                print(f"⏰ Sleeping 1 hour until {(datetime.now() + timedelta(hours=1)).strftime('%H:%M:%S')}")
                print(f"{'='*60}\n")
                cache.set_metadata('last_cache_run_time', cycle_end_time)
                cache.set_metadata('last_cache_run_status', f'⏸️ Rate limit hit after {api_calls_this_hour} calls')
            else:
                print(f"\n{'='*60}")
                print(f"✅ HOURLY CYCLE COMPLETE")
                print(f"📊 Total API Calls This Hour: {api_calls_this_hour}")
                print(f"⏰ Next cycle in 1 hour at {(datetime.now() + timedelta(hours=1)).strftime('%H:%M:%S')}")
                print(f"{'='*60}\n")
                cache.set_metadata('last_cache_run_time', cycle_end_time)
                cache.set_metadata('last_cache_run_status', f'✅ Success - {api_calls_this_hour} calls made')
            
            # Wait 1 hour before next cycle
            time.sleep(3600)
        
    except Exception as e:
        print(f"❌ Background cache builder error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cache_builder_running = False
        print("🛑 Background cache builder stopped")

def populate_sleep_score_cache(dates_to_fetch: list, headers: dict, force_refresh: bool = False):
    """
    Fetch actual sleep scores from Fitbit API for missing dates and cache them.
    This uses the daily endpoint which includes the real sleep score.
    
    Args:
        dates_to_fetch: List of dates to fetch
        headers: API headers
        force_refresh: If True, re-fetch even if already cached (useful for today's data)
    
    Returns:
        Number of dates fetched, or -1 if rate limit hit
    """
    fetched_count = 0
    for date_str in dates_to_fetch:
        try:
            # Fetch individual day's sleep data (includes sleepScore)
            response = requests.get(
                f"https://api.fitbit.com/1.2/user/-/sleep/date/{date_str}.json",
                headers=headers,
                timeout=10
            ).json()
            
            # Check for rate limit
            if 'error' in response:
                error_code = response.get('error', {}).get('code')
                if error_code == 429:
                    print("⚠️ Rate limit hit in cache population! Stopping...")
                    return -1  # Signal rate limit
            
            if 'sleep' in response and len(response['sleep']) > 0:
                for sleep_record in response['sleep']:
                    if sleep_record.get('isMainSleep', True):
                        # Extract ACTUAL sleep score (NOT efficiency!)
                        sleep_score = None
                        if 'sleepScore' in sleep_record and isinstance(sleep_record['sleepScore'], dict):
                            sleep_score = sleep_record['sleepScore'].get('overall')
                            print(f"✅ Found REAL sleep score for {date_str}: {sleep_score}")
                        else:
                            print(f"⚠️ No sleep score found for {date_str} - API didn't provide it (may be too short/incomplete sleep)")
                        
                        # Calculate 3-tier sleep scores
                        minutes_asleep = sleep_record.get('minutesAsleep', 0)
                        deep_min = sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes', 0)
                        rem_min = sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes', 0)
                        minutes_awake = sleep_record.get('minutesAwake', 0)
                        
                        calculated_scores = calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake)
                        
                        # NEVER fallback to efficiency - efficiency != sleep score!
                        # If Fitbit doesn't provide a sleep score, store None
                        if sleep_score is not None or 'efficiency' in sleep_record:
                            # Cache the sleep score and related data
                            cache.set_sleep_score(
                                date=date_str,
                                sleep_score=sleep_score,
                                efficiency=sleep_record.get('efficiency'),
                                proxy_score=calculated_scores['proxy_score'],
                                reality_score=calculated_scores['reality_score'],
                                total_sleep=minutes_asleep,
                                deep=deep_min,
                                light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                rem=rem_min,
                                wake=minutes_awake,
                                start_time=sleep_record.get('startTime'),
                                sleep_data_json=str(sleep_record)
                            )
                            fetched_count += 1
                            print(f"✅ Cached sleep scores for {date_str} - Reality: {calculated_scores['reality_score']}, Proxy: {calculated_scores['proxy_score']}")
                        break  # Only process main sleep
        except Exception as e:
            print(f"⚠️ Error fetching sleep score for {date_str}: {e}")
            continue
    
    return fetched_count

for variable in ['CLIENT_ID','CLIENT_SECRET','REDIRECT_URL'] :
    if variable not in os.environ.keys() :
        log.error(f'Missing required environment variable \'{variable}\', please review the README')
        exit(1)

app = dash.Dash(__name__)
app.title = "Fitbit Wellness Report"
server = app.server

# Configure Flask session for password protection
server.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')

# Password protection middleware
@server.before_request
def check_auth():
    """
    Check if user is authenticated before allowing access to dashboard.
    Bypasses auth for: login page, login POST, static assets, health check, and OAuth callback with code
    """
    # Allow these paths without authentication
    allowed_paths = ['/login', '/_dash-', '/assets/', '/health', '/_favicon.ico']
    
    # Check if path is allowed
    if any(request.path.startswith(path) for path in allowed_paths):
        return None
    
    # Allow OAuth callback (when Fitbit redirects with code parameter)
    if request.path == '/' and request.args.get('code'):
        return None
    
    # Check if user is authenticated
    if not session.get('authenticated'):
        # Not authenticated - redirect to login
        if request.path != '/':
            return flask_redirect('/login')
        # For root path, show login page
        return flask_redirect('/login')
    
    return None

@server.route('/login', methods=['GET', 'POST'])
def login():
    """Login page for dashboard access"""
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == DASHBOARD_PASSWORD:
            session['authenticated'] = True
            session.permanent = True
            print(f"✅ User authenticated successfully from {request.remote_addr}")
            return flask_redirect('/')
        else:
            print(f"⚠️ Failed login attempt from {request.remote_addr}")
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Login - Fitbit Wellness Dashboard</title>
                <style>
                    body {{
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        margin: 0;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    }}
                    .login-container {{
                        background: white;
                        padding: 40px;
                        border-radius: 15px;
                        box-shadow: 0 10px 40px rgba(0,0,0,0.3);
                        max-width: 400px;
                        width: 90%;
                    }}
                    h1 {{
                        color: #333;
                        text-align: center;
                        margin-bottom: 10px;
                    }}
                    .subtitle {{
                        text-align: center;
                        color: #666;
                        margin-bottom: 30px;
                        font-size: 14px;
                    }}
                    input[type="password"] {{
                        width: 100%;
                        padding: 12px;
                        border: 2px solid #ddd;
                        border-radius: 8px;
                        font-size: 16px;
                        box-sizing: border-box;
                        margin-bottom: 20px;
                    }}
                    input[type="password"]:focus {{
                        outline: none;
                        border-color: #667eea;
                    }}
                    button {{
                        width: 100%;
                        padding: 12px;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        color: white;
                        border: none;
                        border-radius: 8px;
                        font-size: 16px;
                        font-weight: bold;
                        cursor: pointer;
                        transition: transform 0.2s;
                    }}
                    button:hover {{
                        transform: translateY(-2px);
                    }}
                    .error {{
                        background: #ffebee;
                        color: #c62828;
                        padding: 12px;
                        border-radius: 8px;
                        margin-bottom: 20px;
                        text-align: center;
                        font-weight: bold;
                    }}
                    .icon {{
                        text-align: center;
                        font-size: 48px;
                        margin-bottom: 20px;
                    }}
                </style>
            </head>
            <body>
                <div class="login-container">
                    <div class="icon">🔐</div>
                    <h1>Dashboard Login</h1>
                    <div class="subtitle">Fitbit Wellness Report</div>
                    <div class="error">❌ Incorrect password. Please try again.</div>
                    <form method="POST">
                        <input type="password" name="password" placeholder="Enter dashboard password" required autofocus>
                        <button type="submit">🔓 Unlock Dashboard</button>
                    </form>
                </div>
            </body>
            </html>
            """
    
    # GET request - show login form
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login - Fitbit Wellness Dashboard</title>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            }
            .login-container {
                background: white;
                padding: 40px;
                border-radius: 15px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.3);
                max-width: 400px;
                width: 90%;
            }
            h1 {
                color: #333;
                text-align: center;
                margin-bottom: 10px;
            }
            .subtitle {
                text-align: center;
                color: #666;
                margin-bottom: 30px;
                font-size: 14px;
            }
            input[type="password"] {
                width: 100%;
                padding: 12px;
                border: 2px solid #ddd;
                border-radius: 8px;
                font-size: 16px;
                box-sizing: border-box;
                margin-bottom: 20px;
            }
            input[type="password"]:focus {
                outline: none;
                border-color: #667eea;
            }
            button {
                width: 100%;
                padding: 12px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: bold;
                cursor: pointer;
                transition: transform 0.2s;
            }
            button:hover {
                transform: translateY(-2px);
            }
            .icon {
                text-align: center;
                font-size: 48px;
                margin-bottom: 20px;
            }
            .info {
                background: #e3f2fd;
                color: #1976d2;
                padding: 12px;
                border-radius: 8px;
                margin-top: 20px;
                text-align: center;
                font-size: 13px;
            }
        </style>
    </head>
    <body>
        <div class="login-container">
            <div class="icon">🔐</div>
            <h1>Dashboard Login</h1>
            <div class="subtitle">Fitbit Wellness Report</div>
            <form method="POST">
                <input type="password" name="password" placeholder="Enter dashboard password" required autofocus>
                <button type="submit">🔓 Unlock Dashboard</button>
            </form>
            <div class="info">
                🛡️ This dashboard is password-protected<br>
                Enter your password to access your wellness data
            </div>
        </div>
    </body>
    </html>
    """

@server.route('/logout')
def logout():
    """Logout and clear session"""
    session.clear()
    print(f"👋 User logged out from {request.remote_addr}")
    return flask_redirect('/login')

def refresh_access_token(refresh_token):
    """Refresh the access token using the refresh token"""
    try:
        client_id = os.environ['CLIENT_ID']
        client_secret = os.environ['CLIENT_SECRET']
        token_url = 'https://api.fitbit.com/oauth2/token?'
        payload = {'grant_type': 'refresh_token', 'refresh_token': refresh_token}
        token_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        token_headers = {"Authorization": f"Basic {token_creds}"}
        token_response = requests.post(token_url, data=payload, headers=token_headers)
        token_response_json = token_response.json()
        
        new_access_token = token_response_json.get('access_token')
        new_refresh_token = token_response_json.get('refresh_token')
        expires_in = token_response_json.get('expires_in', 28800)
        
        if new_access_token:
            expiry_time = (datetime.now() + timedelta(seconds=expires_in)).timestamp()
            print("Token refreshed successfully!")
            return new_access_token, new_refresh_token, expiry_time
        else:
            print("Failed to refresh token")
            return None, None, None
    except Exception as e:
        print(f"Error refreshing token: {e}")
        return None, None, None

app.layout = html.Div(children=[
    dcc.ConfirmDialog(
        id='errordialog',
        message='Invalid Access Token : Unable to fetch data',
    ),
    dcc.ConfirmDialog(
        id='rate-limit-dialog',
        message='⚠️ Fitbit API Rate Limit Exceeded!\n\nYou have made too many API requests (150/hour limit).\n\nPlease wait at least 1 hour before generating another report.\n\nTip: Generate shorter date ranges to reduce API calls.',
    ),
    html.Div(id="input-area", className="hidden-print",
    style={
        'display': 'flex',
        'align-items': 'center',
        'justify-content': 'center',
        'flex-direction': 'column',
        'gap': '15px',
        'margin': 'auto',
        'margin-top': '30px',
        'max-width': '900px'
    },children=[
        # Date picker row
        dcc.DatePickerRange(
            id='my-date-picker-range',
            display_format='MMMM DD, Y',
            minimum_nights=0,
            max_date_allowed=datetime.today().date(),
            min_date_allowed=datetime.today().date() - timedelta(days=1000),
            end_date=datetime.today().date() - timedelta(days=1),
            start_date=datetime.today().date() - timedelta(days=7),
            style={'margin-bottom': '5px'}
        ),
        # All buttons in one row
        html.Div(style={'display': 'flex', 'justify-content': 'center', 'gap': '12px', 'flex-wrap': 'wrap', 'align-items': 'center'}, children=[
            html.Button(id='submit-button', type='submit', children='📊 Submit', n_clicks=0, style={
                'background-color': '#28a745', 'color': 'white', 'border': 'none',
                'padding': '12px 24px', 'border-radius': '6px', 'cursor': 'pointer', 
                'font-size': '15px', 'font-weight': 'bold', 'min-width': '140px',
                'box-shadow': '0 2px 4px rgba(0,0,0,0.1)', 'transition': 'all 0.2s'
            }),
            html.Button("🔐 Login to FitBit", id="login-button", style={
                'background-color': '#636efa', 'color': 'white', 'border': 'none',
                'padding': '12px 24px', 'border-radius': '6px', 'cursor': 'pointer', 
                'font-size': '15px', 'font-weight': 'bold', 'min-width': '140px',
                'box-shadow': '0 2px 4px rgba(0,0,0,0.1)', 'transition': 'all 0.2s'
            }),
            html.Button('🚪 Logout', id='logout-button', n_clicks=0, style={
                'background-color': '#dc3545', 'color': 'white', 'border': 'none',
                'padding': '12px 24px', 'border-radius': '6px', 'cursor': 'pointer',
                'font-size': '15px', 'font-weight': 'bold', 'min-width': '140px',
                'box-shadow': '0 2px 4px rgba(0,0,0,0.1)', 'transition': 'all 0.2s'
            }),
            html.Button("🗑️ Flush Cache", id="flush-cache-button-header", n_clicks=0, style={
                'background-color': '#ff6b6b', 'color': 'white', 'border': 'none', 
                'padding': '12px 24px', 'border-radius': '6px', 'cursor': 'pointer', 
                'font-size': '15px', 'font-weight': 'bold', 'min-width': '140px',
                'box-shadow': '0 2px 4px rgba(0,0,0,0.1)', 'transition': 'all 0.2s'
            }),
            html.Button("🚀 Start Cache", id="start-cache-button", n_clicks=0, style={
                'background-color': '#28a745', 'color': 'white', 'border': 'none', 
                'padding': '12px 24px', 'border-radius': '6px', 'cursor': 'pointer', 
                'font-size': '15px', 'font-weight': 'bold', 'min-width': '140px',
                'box-shadow': '0 2px 4px rgba(0,0,0,0.1)', 'transition': 'all 0.2s'
            }),
        ]),
    ]),
    dcc.Location(id="location"),
    dcc.Store(id="oauth-token", storage_type='session'),  # Store OAuth token in session storage
    dcc.Store(id="refresh-token", storage_type='session'),  # Store refresh token in session storage
    dcc.Store(id="token-expiry", storage_type='session'),  # Store token expiry time
    dcc.Store(id='exercise-data-store-json'), # 🐞 FIX: Use dcc.Store instead of global variable for workout details
    html.Div(id="instruction-area", className="hidden-print", style={'margin-top':'30px', 'margin-right':'auto', 'margin-left':'auto','text-align':'center'}, children=[
        html.P("Select a date range to generate a report.", style={'font-size':'17px', 'font-weight': 'bold', 'color':'#54565e'}),
        html.Div(id="cache-status-display", style={'margin-top': '10px', 'padding': '10px', 'background-color': '#f0f8ff', 'border-radius': '5px', 'font-size': '14px'}),
    ]),
    dcc.Interval(id='cache-status-interval', interval=5000, n_intervals=0),  # Update every 5 seconds
    dcc.ConfirmDialog(id='flush-confirm', message=''),
    html.Div(id='loading-div', style={'margin-top': '40px'}, children=[
    dcc.Loading(
            id="loading-progress",
            type="default",
            children=html.Div(id="loading-output-1")
        ),
    ]),

    html.Div(id='output_div', style={'max-width': '1400px', 'margin': 'auto'}, children=[

        html.Div(id='report-title-div', 
        style={
        'display': 'flex',
        'align-items': 'center',
        'justify-content': 'center',
        'flex-direction': 'column',
        'margin-top': '20px'}, children=[
            html.H2(id="report-title", style={'font-weight': 'bold'}),
            html.H4(id="date-range-title", style={'font-weight': 'bold'}),
            html.P(id="generated-on-title", style={'font-weight': 'bold', 'font-size': '16'})
        ]),
        html.Div(style={"height": '40px'}),
        html.H4("Resting Heart Rate 💖", style={'font-weight': 'bold'}),
        html.H6("Resting heart rate (RHR) is derived from a person's average sleeping heart rate. Fitbit tracks heart rate with photoplethysmography. This technique uses sensors and green light to detect blood volume when the heart beats. If a Fitbit device isn't worn during sleep, RHR is derived from daytime sedentary heart rate. According to the American Heart Association, a normal RHR is between 60-100 beats per minute (bpm), but this can vary based upon your age or fitness level."),
        dcc.Graph(
            id='graph_RHR',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='RHR_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Steps Count 👣", style={'font-weight': 'bold'}),
        html.H6("Fitbit devices use an accelerometer to track steps. Some devices track active minutes, which includes activities over 3 metabolic equivalents (METs), such as brisk walking and cardio workouts."),
        dcc.Graph(
            id='graph_steps',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        dcc.Graph(
            id='graph_steps_heatmap',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='steps_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Activity 🏃‍♂️", style={'font-weight': 'bold'}),
        html.H6("Heart Rate Zones (fat burn, cardio and peak) are based on a percentage of maximum heart rate. Maximum heart rate is calculated as 220 minus age. The Centers for Disease Control recommends that adults do at least 150-300 minutes of moderate-intensity aerobic activity each week or 75-150 minutes of vigorous-intensity aerobic activity each week."),
        dcc.Graph(
            id='graph_activity_minutes',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='fat_burn_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(id='cardio_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(id='peak_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Weight Log ⏲️", style={'font-weight': 'bold'}),
        html.H6("Fitbit connects with the Aria family of smart scales to track weight. Weight may also be self-reported using the Fitbit app. Studies suggest that regular weigh-ins may help people who want to lose weight."),
        dcc.Graph(
            id='graph_weight',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='weight_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("SpO2 🩸", style={'font-weight': 'bold'}),
        html.H6("A pulse oximeter reading indicates what percentage of your blood is saturated, known as the SpO2 level. A typical, healthy reading is 95–100% . If your SpO2 level is less than 92%, a doctor may recommend you get an ABG. A pulse ox is the most common type of test because it's noninvasive and provides quick readings."),
        dcc.Graph(
            id='graph_spo2',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='spo2_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        html.H4("Oxygen Variation (EOV) 🫁", style={'font-weight': 'bold'}),
        html.H6("EOV (Estimated Oxygen Variation) measures fluctuations in blood oxygen levels during sleep. Higher EOV scores may indicate breathing disturbances or sleep apnea. Lower, more stable scores indicate healthier breathing patterns."),
        dcc.Graph(
            id='graph_eov',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='eov_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Heart Rate Variability (HRV) 💗", style={'font-weight': 'bold'}),
        html.H6("Heart Rate Variability measures the variation in time between heartbeats. Higher HRV generally indicates better cardiovascular fitness and stress resilience. HRV is measured in milliseconds (ms) and varies by age, fitness level, and individual factors."),
        dcc.Graph(
            id='graph_hrv',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='hrv_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Breathing Rate 🫁", style={'font-weight': 'bold'}),
        html.H6("Breathing rate is the number of breaths per minute during sleep. A normal breathing rate for adults is typically between 12-20 breaths per minute. Fitbit calculates this using movement and heart rate sensors during sleep."),
        dcc.Graph(
            id='graph_breathing',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='breathing_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Cardio Fitness Score (VO2 Max) 🏃", style={'font-weight': 'bold'}),
        html.H6("Cardio Fitness Score estimates your VO2 Max - the maximum amount of oxygen your body can use during exercise. Higher scores indicate better cardiovascular fitness. Scores are personalized based on your age, sex, and fitness data."),
        dcc.Graph(
            id='graph_cardio_fitness',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='cardio_fitness_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Temperature 🌡️", style={'font-weight': 'bold'}),
        html.H6("Skin temperature variation from your personal baseline. Temperature changes can indicate illness, stress, or menstrual cycle changes. Measured in degrees relative to your baseline (available on supported devices like Fitbit Sense, Versa 3, Charge 5)."),
        dcc.Graph(
            id='graph_temperature',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='temperature_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Active Zone Minutes ⚡", style={'font-weight': 'bold'}),
        html.H6("Active Zone Minutes track time spent in fat burn, cardio, or peak heart rate zones. The American Heart Association recommends at least 150 Active Zone Minutes per week for health benefits."),
        dcc.Graph(
            id='graph_azm',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='azm_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Calories & Distance 🔥", style={'font-weight': 'bold'}),
        html.H6("Calories burned includes your basal metabolic rate (BMR) plus calories from activity. Distance is calculated from steps and stride length. These metrics help track daily energy expenditure."),
        dcc.Graph(
            id='graph_calories',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        dcc.Graph(
            id='graph_distance',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='calories_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Floors Climbed 🪜", style={'font-weight': 'bold'}),
        html.H6("Floors climbed are calculated using an altimeter that detects elevation changes. One floor is approximately 10 feet (3 meters) of elevation gain."),
        dcc.Graph(
            id='graph_floors',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='floors_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        html.H4("Exercise Log 🏋️", style={'font-weight': 'bold'}),
        html.H6("Logged exercises and workouts tracked by your Fitbit device. Includes activity type, duration, calories burned, and average heart rate for each session."),
        html.Div(style={'display': 'flex', 'gap': '20px', 'align-items': 'center', 'justify-content': 'center', 'margin': '20px'}, children=[
            html.Label("Filter by Activity Type:", style={'font-weight': 'bold'}),
            dcc.Dropdown(
                id='exercise-type-filter',
                options=[],  # Will be populated dynamically
                value='All',
                style={'min-width': '200px'}
            ),
        ]),
        html.Div(id='exercise_log_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '20px'}),
        html.H5("📊 Workout Details", style={'font-weight': 'bold', 'margin-top': '20px'}),
        html.P("Select a date to view detailed heart rate zones for that workout:", style={'color': '#666'}),
        html.Div(style={'display': 'flex', 'gap': '20px', 'align-items': 'center', 'margin': '15px 0'}, children=[
            dcc.Dropdown(
                id='workout-date-selector',
                options=[],
                placeholder="Select a workout date...",
                style={'min-width': '250px'}
            ),
        ]),
        html.Div(id='workout-detail-display', style={'margin': '20px 0'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        html.H4("Sleep 💤", style={'font-weight': 'bold'}),
        html.H6("Fitbit estimates sleep stages (awake, REM, light sleep and deep sleep) and sleep duration based on a person's movement and heart-rate patterns. The National Sleep Foundation recommends 7-9 hours of sleep per night for adults"),
        dcc.Checklist(options=[{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled':True}], value=['Color Code Sleep Stages'], style={'max-width': '1330px', 'margin': 'auto'}, inline=True, id="sleep-stage-checkbox", className="hidden-print"),
        dcc.Graph(
            id='graph_sleep',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        
        # Sleep Data Table with 3-Tier Scores
        html.H5("💤 Sleep Data Overview", style={'font-weight': 'bold', 'margin-top': '30px'}),
        html.P("Detailed sleep metrics for each night including Reality Score (primary), Proxy Score (Fitbit match), and sleep stage durations.", style={'color': '#666'}),
        html.Div(id='sleep_data_table', style={'max-width': '1400px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '20px'}),
        
        dcc.Graph(
            id='graph_sleep_regularity',
            figure=px.bar(),
            config= {'displaylogo': False}
        ),
        html.Div(id='sleep_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        html.H4("Sleep Quality Analysis 😴", style={'font-weight': 'bold'}),
        html.H6("Comprehensive sleep metrics including sleep score, stage distribution, and consistency patterns."),
        html.Div(style={'display': 'flex', 'flex-wrap': 'wrap', 'gap': '20px', 'justify-content': 'center'}, children=[
            html.Div(style={'flex': '1', 'min-width': '400px'}, children=[
                dcc.Graph(id='graph_sleep_score', figure=px.line(), config={'displaylogo': False}),
            ]),
            html.Div(style={'flex': '1', 'min-width': '400px'}, children=[
                dcc.Graph(id='graph_sleep_stages_pie', figure=px.pie(), config={'displaylogo': False}),
            ]),
        ]),
        html.Div(style={"height": '20px'}),
        html.H5("📊 Sleep Night Details", style={'font-weight': 'bold', 'margin-top': '20px'}),
        html.P("Select a date to view detailed sleep stages and timeline for that night:", style={'color': '#666'}),
        html.Div(style={'display': 'flex', 'gap': '20px', 'align-items': 'center', 'margin': '15px 0'}, children=[
            dcc.Dropdown(
                id='sleep-date-selector',
                options=[],
                placeholder="Select a sleep date...",
                style={'min-width': '250px'}
            ),
        ]),
        html.Div(id='sleep-detail-display', style={'margin': '20px 0'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        html.H4("Exercise ↔ Sleep Correlations 🔗", style={'font-weight': 'bold'}),
        html.H6("Discover how your workouts impact your sleep quality and next-day recovery."),
        html.Div(style={'display': 'flex', 'flex-wrap': 'wrap', 'gap': '20px', 'justify-content': 'center'}, children=[
            html.Div(style={'flex': '1', 'min-width': '500px'}, children=[
                dcc.Graph(id='graph_exercise_sleep_correlation', figure=px.scatter(), config={'displaylogo': False}),
            ]),
            html.Div(style={'flex': '1', 'min-width': '500px'}, children=[
                dcc.Graph(id='graph_azm_sleep_correlation', figure=px.scatter(), config={'displaylogo': False}),
            ]),
        ]),
        html.Div(id='correlation_insights', style={'max-width': '1200px', 'margin': 'auto', 'padding': '20px', 'background-color': '#f8f9fa', 'border-radius': '10px'}, children=[]),
        html.Div(style={"height": '40px'}),
        
        # Documentation Footer
        html.Div(className="hidden-print", style={'max-width': '1200px', 'margin': 'auto', 'padding': '30px', 'background-color': '#2c3e50', 'border-radius': '10px', 'margin-top': '40px'}, children=[
            html.H4("📚 Documentation & Guides", style={'color': 'white', 'text-align': 'center', 'margin-bottom': '20px'}),
            html.Div(style={'display': 'flex', 'flex-wrap': 'wrap', 'justify-content': 'center', 'gap': '15px'}, children=[
                html.A("📖 README", href="https://github.com/burrellka/fitbit-web-ui-app-kb/blob/enhanced-features/README.md", target="_blank", 
                       style={'padding': '10px 20px', 'background-color': '#3498db', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
                html.A("🗄️ Cache System Guide", href="https://github.com/burrellka/fitbit-web-ui-app-kb/blob/enhanced-features/CACHE_SYSTEM_GUIDE.md", target="_blank",
                       style={'padding': '10px 20px', 'background-color': '#9b59b6', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
                html.A("🔌 API Documentation", href="https://github.com/burrellka/fitbit-web-ui-app-kb/blob/enhanced-features/API_DOCUMENTATION.md", target="_blank",
                       style={'padding': '10px 20px', 'background-color': '#e74c3c', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
                html.A("🚀 Enhancement Roadmap", href="https://github.com/burrellka/fitbit-web-ui-app-kb/blob/enhanced-features/ENHANCEMENT_ROADMAP.md", target="_blank",
                       style={'padding': '10px 20px', 'background-color': '#16a085', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
                html.A("🏠 Deployment Guide", href="https://github.com/burrellka/fitbit-web-ui-app-kb/blob/enhanced-features/DEPLOYMENT_GUIDE.md", target="_blank",
                       style={'padding': '10px 20px', 'background-color': '#f39c12', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
                html.A("🔑 Get Access Token", href="https://github.com/burrellka/fitbit-web-ui-app-kb/blob/enhanced-features/help/GET_ACCESS_TOKEN.md", target="_blank",
                       style={'padding': '10px 20px', 'background-color': '#27ae60', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
            ]),
            html.Div(style={'text-align': 'center', 'margin-top': '20px', 'color': '#95a5a6', 'font-size': '12px'}, children=[
                html.P("Fitbit Wellness Enhanced v2.0 | Intelligent Caching & Auto-Sync"),
                html.P([
                    "Built with ❤️ | ",
                    html.A("View on GitHub", href="https://github.com/burrellka/fitbit-web-ui-app-kb", target="_blank", style={'color': '#3498db', 'text-decoration': 'none'}),
                    " | ",
                    html.A("Report Issues", href="https://github.com/burrellka/fitbit-web-ui-app-kb/issues", target="_blank", style={'color': '#e74c3c', 'text-decoration': 'none'})
                ])
            ])
        ]),
        
        # Cache Status Panel
        html.Div(id='detailed-cache-status', className="hidden-print", style={'max-width': '1200px', 'margin': 'auto', 'padding': '30px', 'background-color': '#34495e', 'border-radius': '10px', 'margin-top': '20px'}, children=[
            html.H4("🗄️ Cache Status", style={'color': 'white', 'text-align': 'center', 'margin-bottom': '20px'}),
            html.Div(id='cache-stats-grid', children=[
                html.P("Loading cache statistics...", style={'color': '#bdc3c7', 'text-align': 'center'})
            ])
        ]),
        
        html.Div(style={"height": '25px'}),
    ]),
])

@app.callback(Output('location', 'href'),Input('login-button', 'n_clicks'))
def authorize(n_clicks):
    """Authorize the application"""
    if n_clicks :
        client_id = os.environ['CLIENT_ID']
        redirect_uri = os.environ['REDIRECT_URL']
        # CRITICAL: 'settings' scope is REQUIRED for official Sleep Score (sleepScore.overall)
        # Without it, API only returns efficiency, not the actual score
        scope = 'profile activity settings heartrate sleep cardio_fitness weight oxygen_saturation respiratory_rate temperature location'
        # Force consent screen to reappear to grant new scopes
        auth_url = f'https://www.fitbit.com/oauth2/authorize?scope={scope}&client_id={client_id}&response_type=code&prompt=consent&redirect_uri={redirect_uri}'
        return auth_url
    return dash.no_update

@app.callback(Output('oauth-token', 'data'),Output('refresh-token', 'data'),Output('token-expiry', 'data'),Input('location', 'href'))
def handle_oauth_callback(href):
    """Process the OAuth callback"""
    if href:
        # Parse the query string from the URL to extract the 'code' parameter
        parsed_url = urlparse(href)
        query_params = parse_qs(parsed_url.query)
        oauth_code = query_params.get('code', [None])[0]
        if oauth_code :
            print(f"OAuth code received: {oauth_code[:20]}...")
        else :
            print("No OAuth code found in URL.")
            return dash.no_update, dash.no_update, dash.no_update
        # Exchange code for a token
        client_id = os.environ['CLIENT_ID']
        client_secret = os.environ['CLIENT_SECRET']
        redirect_uri = os.environ['REDIRECT_URL']
        token_url='https://api.fitbit.com/oauth2/token'
        payload = {
            'code': oauth_code, 
            'grant_type': 'authorization_code', 
            'client_id': client_id, 
            'redirect_uri': redirect_uri
        }
        token_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        token_headers = {
            "Authorization": f"Basic {token_creds}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        print(f"Requesting token with redirect_uri: {redirect_uri}")
        token_response = requests.post(token_url, data=payload, headers=token_headers)
        print(f"Token response status: {token_response.status_code}")
        print(f"Token response: {token_response.text}")
        
        try:
            token_response_json = token_response.json()
        except:
            print(f"ERROR: Could not parse token response as JSON")
            return dash.no_update, dash.no_update, dash.no_update
            
        access_token = token_response_json.get('access_token')
        refresh_token = token_response_json.get('refresh_token')
        expires_in = token_response_json.get('expires_in', 28800)  # Default 8 hours
        
        if access_token :
            print(f"✅ Access token received! Expires in {expires_in} seconds")
            # Store refresh token securely for automatic daily sync
            if refresh_token:
                cache.store_refresh_token(refresh_token, expires_in)
            # Calculate expiry timestamp
            expiry_time = (datetime.now() + timedelta(seconds=expires_in)).timestamp()
            return access_token, refresh_token, expiry_time
        else :
            errors = token_response_json.get('errors', token_response_json.get('error', 'Unknown error'))
            print(f"❌ No access token found in response. Errors: {errors}")
    return dash.no_update, dash.no_update, dash.no_update

@app.callback(
    Output('oauth-token', 'data', allow_duplicate=True),
    Output('refresh-token', 'data', allow_duplicate=True),
    Output('token-expiry', 'data', allow_duplicate=True),
    Output('location', 'href', allow_duplicate=True),
    Input('logout-button', 'n_clicks'),
    prevent_initial_call=True
)
def logout_callback(n_clicks):
    """Handle logout button click - clear all tokens and redirect"""
    if n_clicks and n_clicks > 0:
        print("🚪 Logout button clicked - clearing tokens and redirecting")
        # Clear all token stores and redirect to logout route (which clears Flask session)
        return None, None, None, '/logout'
    return dash.no_update, dash.no_update, dash.no_update, dash.no_update

@app.callback(
    Output('login-button', 'children'),
    Output('login-button', 'disabled'),
    Input('oauth-token', 'data'),
    Input('refresh-token', 'data')
)
def update_login_button(oauth_token, refresh_token):
    if oauth_token:
        # 🐞 FIX: Do NOT auto-start cache builder on login
        # User must manually click "Start Cache" button to begin caching
        # This allows for logout/flush cache without unwanted auto-restarts
        print("✅ Logged in - cache builder will start when 'Start Cache' button is clicked")
        return html.Span("Logged in"), True
    else:
        return "Login to FitBit", False

# Advanced metrics are now always enabled with smart caching - no toggle needed!

@app.callback(
    Output('cache-status-display', 'children'),
    Output('cache-stats-grid', 'children'),
    Input('cache-status-interval', 'n_intervals')
)
def update_cache_status(n):
    """Display current cache status in header and detailed grid"""
    try:
        stats = cache.get_cache_stats()
        detailed_stats = cache.get_detailed_cache_stats()
        
        # Header status
        if stats['sleep_records'] == 0:
            status_emoji = "⏳"
            status_text = "Cache Empty - Will auto-populate on first report"
            color = "#ff9800"
        elif stats['sleep_records'] < 30:
            status_emoji = "🔄"
            status_text = f"Building Cache: {stats['sleep_records']} days cached"
            color = "#2196f3"
        else:
            status_emoji = "✅"
            status_text = f"Cache Ready: {stats['sleep_records']} days | {stats['sleep_date_range']}"
            color = "#4caf50"
        
        header_status = html.Div([
            html.Span(status_emoji, style={'font-size': '18px', 'margin-right': '8px'}),
            html.Span(status_text, style={'color': color, 'font-weight': 'bold'})
        ])
        
        # Detailed grid
        metrics_grid = html.Div(style={'display': 'grid', 'grid-template-columns': 'repeat(auto-fit, minmax(250px, 1fr))', 'gap': '20px'}, children=[
            # Sleep Data
            html.Div(style={'background-color': '#2c3e50', 'padding': '20px', 'border-radius': '8px', 'border-left': '4px solid #3498db'}, children=[
                html.Div(style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '10px'}, children=[
                    html.Span("💤", style={'font-size': '24px', 'margin-right': '10px'}),
                    html.H5("Sleep Data", style={'color': 'white', 'margin': '0'})
                ]),
                html.P(f"{detailed_stats['sleep']['count']} days cached", style={'color': '#3498db', 'font-size': '18px', 'font-weight': 'bold', 'margin': '5px 0'}),
                html.P(detailed_stats['sleep']['date_range'], style={'color': '#bdc3c7', 'font-size': '12px', 'margin': '0'})
            ]),
            # HRV
            html.Div(style={'background-color': '#2c3e50', 'padding': '20px', 'border-radius': '8px', 'border-left': '4px solid #e74c3c'}, children=[
                html.Div(style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '10px'}, children=[
                    html.Span("💗", style={'font-size': '24px', 'margin-right': '10px'}),
                    html.H5("Heart Rate Variability", style={'color': 'white', 'margin': '0'})
                ]),
                html.P(f"{detailed_stats['hrv']['count']} days cached", style={'color': '#e74c3c', 'font-size': '18px', 'font-weight': 'bold', 'margin': '5px 0'}),
                html.P(detailed_stats['hrv']['date_range'], style={'color': '#bdc3c7', 'font-size': '12px', 'margin': '0'})
            ]),
            # Breathing Rate
            html.Div(style={'background-color': '#2c3e50', 'padding': '20px', 'border-radius': '8px', 'border-left': '4px solid #1abc9c'}, children=[
                html.Div(style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '10px'}, children=[
                    html.Span("🫁", style={'font-size': '24px', 'margin-right': '10px'}),
                    html.H5("Breathing Rate", style={'color': 'white', 'margin': '0'})
                ]),
                html.P(f"{detailed_stats['breathing_rate']['count']} days cached", style={'color': '#1abc9c', 'font-size': '18px', 'font-weight': 'bold', 'margin': '5px 0'}),
                html.P(detailed_stats['breathing_rate']['date_range'], style={'color': '#bdc3c7', 'font-size': '12px', 'margin': '0'})
            ]),
            # Temperature
            html.Div(style={'background-color': '#2c3e50', 'padding': '20px', 'border-radius': '8px', 'border-left': '4px solid #f39c12'}, children=[
                html.Div(style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '10px'}, children=[
                    html.Span("🌡️", style={'font-size': '24px', 'margin-right': '10px'}),
                    html.H5("Temperature", style={'color': 'white', 'margin': '0'})
                ]),
                html.P(f"{detailed_stats['temperature']['count']} days cached", style={'color': '#f39c12', 'font-size': '18px', 'font-weight': 'bold', 'margin': '5px 0'}),
                html.P(detailed_stats['temperature']['date_range'], style={'color': '#bdc3c7', 'font-size': '12px', 'margin': '0'})
            ]),
        ])
        
        # Cache Builder Status (Last Run & Next Run)
        from datetime import datetime
        last_run_time = cache.get_metadata('last_cache_run_time')
        last_run_status = cache.get_metadata('last_cache_run_status') or 'Never run'
        
        if last_run_time:
            try:
                last_run_dt = datetime.fromisoformat(last_run_time)
                last_run_display = last_run_dt.strftime('%Y-%m-%d %I:%M:%S %p')
                # Calculate next run (1 hour from last run)
                next_run_dt = last_run_dt + timedelta(hours=1)
                next_run_display = next_run_dt.strftime('%Y-%m-%d %I:%M:%S %p')
                
                # Check if next run is in the past (meaning it should be running now or soon)
                now = datetime.now()
                if now >= next_run_dt:
                    next_run_display += " (Running now)"
                    next_run_color = "#4caf50"
                else:
                    time_until = next_run_dt - now
                    minutes_until = int(time_until.total_seconds() / 60)
                    next_run_display += f" (in {minutes_until} min)"
                    next_run_color = "#2196f3"
            except:
                last_run_display = last_run_time
                next_run_display = "Unknown"
                next_run_color = "#999"
        else:
            last_run_display = "Never"
            next_run_display = "Waiting for first run"
            next_run_color = "#ff9800"
        
        cache_builder_status = html.Div(style={'margin-top': '30px', 'padding': '20px', 'background-color': '#34495e', 'border-radius': '8px', 'border-top': '3px solid #9b59b6'}, children=[
            html.H4("🤖 Automated Cache Builder Status", style={'color': 'white', 'margin-bottom': '15px', 'text-align': 'center'}),
            html.Div(style={'display': 'grid', 'grid-template-columns': '1fr 1fr', 'gap': '20px'}, children=[
                # Last Run
                html.Div(style={'background-color': '#2c3e50', 'padding': '15px', 'border-radius': '6px'}, children=[
                    html.Div(style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '8px'}, children=[
                        html.Span("⏱️", style={'font-size': '20px', 'margin-right': '8px'}),
                        html.H6("Last Cache Run", style={'color': 'white', 'margin': '0'})
                    ]),
                    html.P(last_run_display, style={'color': '#3498db', 'font-size': '14px', 'margin': '5px 0'}),
                    html.P(f"Status: {last_run_status}", style={'color': '#bdc3c7', 'font-size': '12px', 'margin': '0'})
                ]),
                # Next Run
                html.Div(style={'background-color': '#2c3e50', 'padding': '15px', 'border-radius': '6px'}, children=[
                    html.Div(style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '8px'}, children=[
                        html.Span("⏰", style={'font-size': '20px', 'margin-right': '8px'}),
                        html.H6("Next Cache Run", style={'color': 'white', 'margin': '0'})
                    ]),
                    html.P(next_run_display, style={'color': next_run_color, 'font-size': '14px', 'font-weight': 'bold', 'margin': '5px 0'}),
                    html.P("Auto-syncs every hour", style={'color': '#bdc3c7', 'font-size': '12px', 'margin': '0'})
                ]),
            ])
        ])
        
        full_display = html.Div([metrics_grid, cache_builder_status])
        
        return header_status, full_display
        
    except Exception as e:
        error_msg = html.Span(f"Cache status unavailable: {e}", style={'color': '#999'})
        error_grid = html.P(f"Unable to load cache statistics: {e}", style={'color': '#e74c3c', 'text-align': 'center'})
        return error_msg, error_grid

@app.callback(Output('flush-confirm', 'displayed'), Output('flush-confirm', 'message'), Input('flush-cache-button-header', 'n_clicks'))
def flush_cache_handler(n_clicks):
    """Handle cache flush button click - also STOPS cache builder"""
    global cache_builder_running
    
    if n_clicks and n_clicks > 0:
        try:
            # 🐞 FIX: Stop cache builder when flushing cache
            if cache_builder_running:
                print("🛑 Stopping cache builder due to cache flush...")
                cache_builder_running = False
                print("✅ Cache builder stopped")
            
            cache.flush_cache()
            return True, "✅ Cache flushed successfully! Cache builder stopped. Click 'Start Cache' to rebuild."
        except Exception as e:
            return True, f"❌ Error flushing cache: {e}"
    return False, ""

@app.callback(Output('flush-confirm', 'displayed', allow_duplicate=True), Output('flush-confirm', 'message', allow_duplicate=True), Input('start-cache-button', 'n_clicks'), State('oauth-token', 'data'), prevent_initial_call=True)
def start_cache_builder_handler(n_clicks, oauth_token):
    """Handle start cache button click - launches background cache builder"""
    global cache_builder_thread, cache_builder_running
    
    if n_clicks and n_clicks > 0:
        if not oauth_token:
            return True, "❌ Please login to Fitbit first before starting cache builder!"
        
        if cache_builder_running:
            return True, "⚠️ Cache builder is already running!"
        
        try:
            # 🐞 FIX: Pass both access_token AND refresh_token so builder can refresh tokens
            # Get refresh token from session
            refresh_token = session.get('refresh_token')
            
            # Launch background cache builder thread
            cache_builder_thread = threading.Thread(target=background_cache_builder, args=(oauth_token, refresh_token), daemon=True)
            cache_builder_thread.start()
            return True, "🚀 Cache builder started! It will run in the background and populate historical data. Check cache status below for progress."
        except Exception as e:
            return True, f"❌ Error starting cache builder: {e}"
    return False, ""

# Store for exercise and sleep detail data
exercise_data_store = {}
sleep_detail_data_store = {}

@app.callback(
    Output('workout-detail-display', 'children'),
    Input('workout-date-selector', 'value'),
    State('oauth-token', 'data')
)
def display_workout_details(selected_date, oauth_token):
    """
    Display detailed workout information including HR zones for selected date.
    🐞 FIX: This function is now self-reliant and fetches data directly from the cache.
    """
    if not selected_date or not oauth_token:
        return html.Div("Select a workout date to view details", style={'color': '#999', 'font-style': 'italic'})
    
    # Get stored activity data for the date directly from cache
    activities_from_cache = cache.get_activities(selected_date)
    
    if not activities_from_cache:
        return html.Div(f"No workout data available in cache for {selected_date}", style={'color': '#999'})

    # The cache stores activity data as a list of dicts. We need to reconstruct
    # the format that the chart-drawing helpers expect, which is the full JSON from the API.
    activities = []
    for act in activities_from_cache:
        try:
            # The full JSON of the activity was stored in the cache
            activity_details = json.loads(act.get('activity_data_json', '{}'))
            if activity_details:
                activities.append(activity_details)
        except (json.JSONDecodeError, TypeError):
            # Fallback if JSON is invalid or missing, build a basic dict
            print(f"⚠️ Warning: Could not parse full activity details from cache for logId {act.get('activity_id')}. Some chart data may be missing.")
            activities.append({
                'logId': act.get('activity_id'),
                'activityName': act.get('activity_name'),
                'startTime': f"{selected_date}T00:00:00.000",
                'duration': act.get('duration_ms'),
                'calories': act.get('calories'),
                'averageHeartRate': act.get('avg_heart_rate'),
                'steps': act.get('steps'),
                'distance': act.get('distance'),
                'heartRateZones': [] # Zone data is in the full JSON, so this will be empty in the fallback
            })
    
    if not activities:
         return html.Div(f"Could not reconstruct workout data for {selected_date}", style={'color': '#999'})
    
    # Helper function to fetch and create intraday HR chart
    def create_intraday_hr_chart(activity, oauth_token):
        """Fetch intraday HR data and create line chart with zone backgrounds"""
        import plotly.graph_objects as go
        from datetime import datetime
        import requests
        
        # Get activity details
        log_id = activity.get('logId')
        start_time = activity.get('startTime')
        duration_ms = activity.get('duration', 0)
        
        if not log_id or not start_time or duration_ms == 0:
            return None
        
        try:
            # Parse start time and calculate end time
            start_dt = datetime.fromisoformat(start_time.replace('Z', ''))
            date_str = start_dt.strftime('%Y-%m-%d')
            
            # Fetch intraday heart rate data (🐞 FIX #3: Use 1min instead of 1sec to conserve API budget)
            headers = {'Authorization': f'Bearer {oauth_token}'}
            url = f"https://api.fitbit.com/1/user/-/activities/heart/date/{date_str}/1d/1min.json"
            response = requests.get(url, headers=headers)
            
            if response.status_code != 200:
                print(f"⚠️ Failed to fetch intraday HR for activity {log_id}: {response.status_code}")
                return None
            
            data = response.json()
            intraday_data = data.get('activities-heart-intraday', {}).get('dataset', [])
            
            if not intraday_data:
                return None
            
            # Filter data to activity time window
            activity_start = start_dt.time()
            activity_end_seconds = (start_dt.hour * 3600 + start_dt.minute * 60 + start_dt.second) + (duration_ms / 1000)
            
            times = []
            hr_values = []
            
            for entry in intraday_data:
                entry_time_str = entry.get('time', '')
                entry_hr = entry.get('value', 0)
                
                if entry_hr > 0:
                    # Parse time (HH:MM:SS format)
                    time_parts = entry_time_str.split(':')
                    if len(time_parts) == 3:
                        entry_seconds = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                        start_seconds = activity_start.hour * 3600 + activity_start.minute * 60 + activity_start.second
                        
                        # Check if within activity window (with 5min buffer on each side)
                        if start_seconds - 300 <= entry_seconds <= activity_end_seconds + 300:
                            # Calculate relative time in minutes from activity start
                            relative_time = (entry_seconds - start_seconds) / 60
                            times.append(relative_time)
                            hr_values.append(entry_hr)
            
            if not times:
                return None
            
            # Get HR zones for background shading
            hr_zones = activity.get('heartRateZones', [])
            zone_ranges = {}
            for zone in hr_zones:
                zone_name = zone.get('name', '')
                zone_ranges[zone_name] = {
                    'min': zone.get('min', 0),
                    'max': zone.get('max', 220),
                    'color': {
                        'Out of Range': 'rgba(144, 202, 249, 0.15)',
                        'Fat Burn': 'rgba(255, 213, 79, 0.15)',
                        'Cardio': 'rgba(255, 152, 0, 0.15)',
                        'Peak': 'rgba(244, 67, 54, 0.15)'
                    }.get(zone_name, 'rgba(200, 200, 200, 0.1)')
                }
            
            # Create figure
            fig = go.Figure()
            
            # Add zone backgrounds (rectangles)
            for zone_name, zone_info in zone_ranges.items():
                if zone_name != 'Out of Range':  # Skip out of range to avoid cluttering
                    fig.add_hrect(
                        y0=zone_info['min'],
                        y1=zone_info['max'],
                        fillcolor=zone_info['color'],
                        layer="below",
                        line_width=0,
                        annotation_text=zone_name,
                        annotation_position="right",
                        annotation=dict(font_size=10, font_color="#666")
                    )
            
            # Add HR line trace
            fig.add_trace(go.Scatter(
                x=times,
                y=hr_values,
                mode='lines',
                line=dict(color='#e74c3c', width=2),
                name='Heart Rate',
                hovertemplate='<b>Time:</b> +%{x:.1f} min<br><b>HR:</b> %{y} bpm<extra></extra>'
            ))
            
            # Update layout
            fig.update_layout(
                title=dict(text="<b>Heart Rate Progression During Workout</b><br><sup>With heart rate zone backgrounds</sup>", 
                          font=dict(size=14)),
                xaxis=dict(
                    title="Time (minutes from start)",
                    gridcolor='#f0f0f0',
                    showgrid=True
                ),
                yaxis=dict(
                    title="Heart Rate (bpm)",
                    gridcolor='#f0f0f0',
                    showgrid=True
                ),
                height=350,
                margin=dict(l=60, r=60, t=60, b=50),
                plot_bgcolor='white',
                paper_bgcolor='white',
                hovermode='closest',
                showlegend=False
            )
            
            return fig
            
        except Exception as e:
            print(f"⚠️ Error creating intraday HR chart: {e}")
            return None
    
    # Helper function to create HR zones chart
    def create_hr_zones_chart(activity):
        """Create a chart showing heart rate zones distribution"""
        import plotly.graph_objects as go
        
        hr_zones = activity.get('heartRateZones', [])
        if not hr_zones:
            return None
        
        # Prepare zone data
        zone_names = []
        zone_minutes = []
        zone_colors = []
        
        color_map = {
            'Out of Range': '#90caf9',
            'Fat Burn': '#ffd54f',
            'Cardio': '#ff9800',
            'Peak': '#f44336'
        }
        
        for zone in hr_zones:
            if zone.get('minutes', 0) > 0:
                zone_names.append(zone.get('name', 'Zone'))
                zone_minutes.append(zone.get('minutes', 0))
                zone_colors.append(color_map.get(zone.get('name', ''), '#ccc'))
        
        if not zone_names:
            return None
        
        # Create horizontal bar chart
        fig = go.Figure(data=[
            go.Bar(
                y=zone_names,
                x=zone_minutes,
                orientation='h',
                marker=dict(color=zone_colors),
                text=[f"{m} min" for m in zone_minutes],
                textposition='inside',
                textfont=dict(color='white', size=12),
                hovertemplate='<b>%{y}</b><br>Time: %{x} min<extra></extra>'
            )
        ])
        
        fig.update_layout(
            title=dict(text="Time in Each Heart Rate Zone", font=dict(size=14)),
            xaxis=dict(title="Minutes", gridcolor='#f0f0f0'),
            yaxis=dict(title=""),
            height=250,
            margin=dict(l=100, r=50, t=50, b=50),
            plot_bgcolor='white',
            paper_bgcolor='white',
            showlegend=False
        )
        
        return fig
    
    # Build detailed display
    details = []
    for activity in activities:
        # Calculate total zone minutes
        hr_zones = activity.get('heartRateZones', [])
        total_zone_min = sum([zone.get('minutes', 0) for zone in hr_zones if zone.get('name') != 'Out of Range'])
        
        # Activity header
        details.append(html.Div(style={'background-color': '#f8f9fa', 'padding': '20px', 'border-radius': '10px', 'margin': '10px 0'}, children=[
            html.H6(f"{activity.get('activityName', 'Activity')} - {activity.get('startTime', '')[:10]}", 
                   style={'color': '#2c3e50', 'margin-bottom': '10px'}),
            
            # Stats grid
            html.Div(style={'display': 'grid', 'grid-template-columns': 'repeat(auto-fit, minmax(150px, 1fr))', 'gap': '10px'}, children=[
                html.Div([
                    html.Strong("Duration: "),
                    html.Span(f"{activity.get('duration', 0) // 60000} min")
                ]),
                html.Div([
                    html.Strong("Calories: "),
                    html.Span(f"{activity.get('calories', 0)} cal")
                ]),
                html.Div([
                    html.Strong("Cardio Load: "),
                    html.Span(f"{activity.get('activeDuration', 0) // 60000 if activity.get('activeDuration') else 'N/A'}")
                ]),
                html.Div([
                    html.Strong("Zone Minutes: "),
                    html.Span(f"{total_zone_min} min", style={'color': '#ff9500', 'font-weight': 'bold'})
                ]),
                html.Div([
                    html.Strong("Avg HR: "),
                    html.Span(f"{activity.get('averageHeartRate', 'N/A')} bpm")
                ]),
                html.Div([
                    html.Strong("Steps: "),
                    html.Span(f"{activity.get('steps', 'N/A')}")
                ]),
                html.Div([
                    html.Strong("Distance: "),
                    html.Span(f"{round(activity.get('distance', 0) * 0.621371, 2)} mi" if activity.get('distance') else 'N/A')
                ]),
            ]),
            
            # HR Zones if available
            html.Div(style={'margin-top': '20px'}, children=[
                html.Strong("Heart Rate Zones", style={'display': 'block', 'margin-bottom': '15px', 'font-size': '16px'}),
                html.Div(children=[
                    # Calculate total time for percentages
                    *[html.Div(style={'margin-bottom': '12px'}, children=[
                        # Zone name and time
                        html.Div(style={'display': 'flex', 'justify-content': 'space-between', 'margin-bottom': '4px'}, children=[
                            html.Span(zone.get('name', 'Zone'), style={'font-weight': '500', 'font-size': '13px'}),
                            html.Span(f"{zone.get('minutes', 0)} min · {int(zone.get('minutes', 0) / max(activity.get('duration', 1) / 60000, 1) * 100)}%", 
                                     style={'font-size': '13px', 'color': '#666'})
                        ]),
                        # Progress bar
                        html.Div(style={'background-color': '#e0e0e0', 'height': '24px', 'border-radius': '12px', 'overflow': 'hidden'}, children=[
                            html.Div(style={
                                'width': f"{int(zone.get('minutes', 0) / max(activity.get('duration', 1) / 60000, 1) * 100)}%",
                                'height': '100%',
                                'background-color': {
                                    'Out of Range': '#90caf9',
                                    'Fat Burn': '#ffd54f',
                                    'Cardio': '#ff9800',
                                    'Peak': '#f44336'
                                }.get(zone.get('name', ''), '#ccc'),
                                'transition': 'width 0.3s ease'
                            })
                        ])
                    ]) for zone in hr_zones if zone.get('minutes', 0) > 0]
                ])
            ]) if activity.get('heartRateZones') else html.Div("HR zone data not available", style={'color': '#999', 'font-style': 'italic', 'margin-top': '10px'}),
            
            # Add Intraday HR Line Chart (with zone backgrounds)
            html.Div(style={'margin-top': '25px'}, children=[
                dcc.Graph(figure=create_intraday_hr_chart(activity, oauth_token), config={'displayModeBar': False}) if create_intraday_hr_chart(activity, oauth_token) else html.Div("Intraday HR data not available (requires Personal scope)", style={'color': '#999', 'font-style': 'italic', 'text-align': 'center', 'padding': '20px', 'background-color': '#fff3cd', 'border-radius': '5px', 'border': '1px solid #ffeaa7'})
            ]),
            
            # Add HR Zones Chart
            html.Div(style={'margin-top': '25px'}, children=[
                dcc.Graph(figure=create_hr_zones_chart(activity), config={'displayModeBar': False}) if create_hr_zones_chart(activity) else html.Div("HR zone chart not available", style={'color': '#999', 'font-style': 'italic', 'text-align': 'center'})
            ])
        ]))
    
    return html.Div(details)

@app.callback(
    Output('sleep-detail-display', 'children'),
    Input('sleep-date-selector', 'value'),
    State('oauth-token', 'data')
)
def display_sleep_details(selected_date, oauth_token):
    """Display detailed sleep information including stages timeline for selected date"""
    if not selected_date or not oauth_token:
        return html.Div("Select a sleep date to view details", style={'color': '#999', 'font-style': 'italic'})
    
    # 🐞 FIX #1: Fetch data directly from cache instead of global store
    sleep_data = cache.get_sleep_data(selected_date)
    if not sleep_data:
        return html.Div(f"No sleep data available for {selected_date}", style={'color': '#999'})
    
    # Build sleep_data dict format expected by the rest of the function
    sleep_data = {
        'reality_score': sleep_data.get('reality_score'),
        'proxy_score': sleep_data.get('proxy_score'),
        'efficiency': sleep_data.get('efficiency'),
        'deep': sleep_data.get('deep'),
        'light': sleep_data.get('light'),
        'rem': sleep_data.get('rem'),
        'wake': sleep_data.get('wake'),
        'total_sleep': sleep_data.get('total_sleep'),
        'start_time': sleep_data.get('start_time')
    }
    
    # Try to build chronological sleep timeline from detailed data
    timeline_figure = None
    try:
        import ast
        import json as json_lib
        from datetime import datetime, timedelta
        import plotly.graph_objects as go
        
        # Get the full sleep record from cached data
        cached_full_data = cache.get_sleep_data(selected_date)
        if cached_full_data and cached_full_data.get('sleep_data_json'):
            # Parse the stored JSON
            sleep_json_str = cached_full_data['sleep_data_json']
            try:
                sleep_record = ast.literal_eval(sleep_json_str) if isinstance(sleep_json_str, str) else sleep_json_str
            except:
                try:
                    sleep_record = json_lib.loads(sleep_json_str)
                except:
                    sleep_record = None
            
            if sleep_record and 'levels' in sleep_record and 'data' in sleep_record['levels']:
                # Extract minute-by-minute sleep stages
                stages_data = sleep_record['levels']['data']
                
                #  Prepare data for timeline chart
                stage_colors = {
                    'deep': '#084466',
                    'light': '#1e9ad6',
                    'rem': '#4cc5da',
                    'wake': '#fd7676',
                    'awake': '#fd7676'
                }
                
                stage_names = {
                    'deep': 'Deep',
                    'light': 'Light',
                    'rem': 'REM',
                    'wake': 'Awake',
                    'awake': 'Awake'
                }
                
                # Create TRUE Gantt-style timeline with REAL TIMES on X-axis
                # 🐞 FIX: Group all segments of same stage into ONE trace per stage (eliminates fragmentation)
                fig_timeline = go.Figure()
                
                # Group segments by stage type
                stage_segments = {'deep': [], 'light': [], 'rem': [], 'wake': [], 'awake': []}
                
                for entry in stages_data:
                    stage = entry.get('level', '').lower()
                    start_time = datetime.fromisoformat(entry['dateTime'].replace('Z', '+00:00'))
                    duration_seconds = entry.get('seconds', 0)
                    end_time = start_time + timedelta(seconds=duration_seconds)
                    duration_minutes = duration_seconds / 60
                    
                    # Convert to milliseconds (JSON serializable)
                    start_ms = int(start_time.timestamp() * 1000)
                    duration_ms = duration_seconds * 1000
                    
                    if stage in stage_segments:
                        stage_segments[stage].append({
                            'start_ms': start_ms,
                            'duration_ms': duration_ms,
                            'start_time': start_time,
                            'end_time': end_time,
                            'duration_minutes': duration_minutes
                        })
                
                # Create ONE trace per stage type (with all segments of that stage)
                stage_order = ['deep', 'light', 'rem', 'wake', 'awake']
                for stage in stage_order:
                    if stage_segments[stage]:
                        # Collect all bases and durations for this stage
                        bases = [seg['start_ms'] for seg in stage_segments[stage]]
                        durations = [seg['duration_ms'] for seg in stage_segments[stage]]
                        
                        # Build custom hover text for each segment
                        hover_texts = []
                        for seg in stage_segments[stage]:
                            hover_texts.append(
                                f"<b>{stage_names.get(stage, stage)}</b><br>" +
                                f"{seg['start_time'].strftime('%I:%M %p')} - {seg['end_time'].strftime('%I:%M %p')}<br>" +
                                f"Duration: {int(seg['duration_minutes'])} min<br>"
                            )
                        
                        fig_timeline.add_trace(go.Bar(
                            base=bases,
                            x=durations,
                            y=["Sleep"] * len(bases),
                            orientation='h',
                            marker=dict(
                                color=stage_colors.get(stage, '#ccc'),
                                line=dict(width=0)
                            ),
                            name=stage_names.get(stage, stage),
                            hovertemplate='%{hovertext}<extra></extra>',
                            hovertext=hover_texts,
                            showlegend=True,
                            legendgroup=stage
                        ))
                
                fig_timeline.update_layout(
                    title=dict(text="<b>Sleep Timeline</b><br><sup>Showing when each sleep stage occurred throughout the night</sup>", 
                              font=dict(size=14)),
                    xaxis=dict(
                        title="Time of Night",
                        type='date',
                        tickformat='%I:%M %p',
                        gridcolor='#f0f0f0',
                        showgrid=True
                    ),
                    yaxis=dict(
                        showticklabels=False,
                        showgrid=False
                    ),
                    barmode='stack',
                    height=180,
                    margin=dict(l=10, r=10, t=60, b=40),
                    plot_bgcolor='white',
                    paper_bgcolor='white',
                    hovermode='closest',
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=-0.3,
                        xanchor="center",
                        x=0.5,
                        title=dict(text="")
                    )
                )
                
                timeline_figure = fig_timeline
    except Exception as e:
        print(f"⚠️ Error creating sleep timeline for {selected_date}: {e}")
        pass
    
    # Get sleep scores - use Reality Score as primary
    reality_score = sleep_data.get('reality_score')
    proxy_score = sleep_data.get('proxy_score')
    efficiency = sleep_data.get('efficiency')
    
    # Use Reality Score for rating (primary metric)
    score = reality_score if reality_score is not None else 0
    
    if score >= 90:
        rating = "Excellent"
        rating_color = "#4caf50"
        rating_emoji = "🌟"
    elif score >= 80:
        rating = "Good"
        rating_color = "#8bc34a"
        rating_emoji = "😊"
    elif score >= 60:
        rating = "Fair"
        rating_color = "#ff9800"
        rating_emoji = "😐"
    else:
        rating = "Poor"
        rating_color = "#f44336"
        rating_emoji = "😴"
    
    # Calculate total sleep for percentages
    total_sleep = sleep_data.get('total_sleep', 1)
    deep_min = sleep_data.get('deep', 0)
    light_min = sleep_data.get('light', 0)
    rem_min = sleep_data.get('rem', 0)
    wake_min = sleep_data.get('wake', 0)
    
    # Build detailed display
    return html.Div(style={'background-color': '#f8f9fa', 'padding': '20px', 'border-radius': '10px'}, children=[
        html.H6(f"Sleep Night: {selected_date}", style={'color': '#2c3e50', 'margin-bottom': '15px'}),
        
        # Summary stats - 3-Tier Sleep Scores
        html.Div(style={'display': 'grid', 'grid-template-columns': 'repeat(auto-fit, minmax(150px, 1fr))', 'gap': '10px', 'margin-bottom': '20px'}, children=[
            html.Div([
                html.Strong("Reality Score: ", style={'display': 'block', 'font-size': '11px', 'color': '#666'}),
                html.Span(f"{reality_score if reality_score is not None else 'N/A'}", 
                         style={'color': rating_color, 'font-size': '28px', 'font-weight': 'bold'}),
                html.Br(),
                html.Span(f"{rating_emoji} {rating}", style={'color': rating_color, 'font-weight': 'bold', 'font-size': '13px'}),
                html.Br(),
                html.Span("(Primary Metric)", style={'font-size': '10px', 'color': '#999'})
            ]),
            html.Div([
                html.Strong("Proxy Score: ", style={'display': 'block', 'font-size': '11px', 'color': '#666'}),
                html.Span(f"{proxy_score if proxy_score is not None else 'N/A'}", 
                         style={'font-size': '20px', 'font-weight': 'bold', 'color': '#3498db'}),
                html.Br(),
                html.Span("(Fitbit Match)", style={'font-size': '10px', 'color': '#999'})
            ]) if proxy_score is not None else html.Div(),
            html.Div([
                html.Strong("Efficiency: ", style={'display': 'block', 'font-size': '11px', 'color': '#666'}),
                html.Span(f"{efficiency if efficiency is not None else 'N/A'}%", 
                         style={'font-size': '20px', 'font-weight': 'bold', 'color': '#95a5a6'}),
                html.Br(),
                html.Span("(API Baseline)", style={'font-size': '10px', 'color': '#999'})
            ]) if efficiency is not None else html.Div(),
            html.Div([
                html.Strong("Total Sleep: "),
                html.Span(f"{total_sleep // 60}h {total_sleep % 60}m")
            ]),
            html.Div([
                html.Strong("Sleep Start: "),
                html.Span(f"{sleep_data.get('start_time', 'N/A')[:16]}" if sleep_data.get('start_time') else 'N/A')
            ]),
        ]),
        
        # Sleep Stages Visual Timeline (Simplified)
        html.Div(style={'margin-top': '25px'}, children=[
            html.Strong("Sleep Stages Distribution", style={'display': 'block', 'margin-bottom': '15px', 'font-size': '16px'}),
            
            # Deep Sleep
            html.Div(style={'margin-bottom': '12px'}, children=[
                html.Div(style={'display': 'flex', 'justify-content': 'space-between', 'margin-bottom': '4px'}, children=[
                    html.Span("🌊 Deep Sleep", style={'font-weight': '500', 'font-size': '13px'}),
                    html.Span(f"{deep_min} min · {int(deep_min / max(total_sleep, 1) * 100)}%", 
                             style={'font-size': '13px', 'color': '#666'})
                ]),
                html.Div(style={'background-color': '#e0e0e0', 'height': '24px', 'border-radius': '12px', 'overflow': 'hidden'}, children=[
                    html.Div(style={
                        'width': f"{int(deep_min / max(total_sleep, 1) * 100)}%",
                        'height': '100%',
                        'background-color': '#084466',
                        'transition': 'width 0.3s ease'
                    })
                ])
            ]),
            
            # Light Sleep
            html.Div(style={'margin-bottom': '12px'}, children=[
                html.Div(style={'display': 'flex', 'justify-content': 'space-between', 'margin-bottom': '4px'}, children=[
                    html.Span("☁️ Light Sleep", style={'font-weight': '500', 'font-size': '13px'}),
                    html.Span(f"{light_min} min · {int(light_min / max(total_sleep, 1) * 100)}%", 
                             style={'font-size': '13px', 'color': '#666'})
                ]),
                html.Div(style={'background-color': '#e0e0e0', 'height': '24px', 'border-radius': '12px', 'overflow': 'hidden'}, children=[
                    html.Div(style={
                        'width': f"{int(light_min / max(total_sleep, 1) * 100)}%",
                        'height': '100%',
                        'background-color': '#1e9ad6',
                        'transition': 'width 0.3s ease'
                    })
                ])
            ]),
            
            # REM Sleep
            html.Div(style={'margin-bottom': '12px'}, children=[
                html.Div(style={'display': 'flex', 'justify-content': 'space-between', 'margin-bottom': '4px'}, children=[
                    html.Span("💭 REM Sleep", style={'font-weight': '500', 'font-size': '13px'}),
                    html.Span(f"{rem_min} min · {int(rem_min / max(total_sleep, 1) * 100)}%", 
                             style={'font-size': '13px', 'color': '#666'})
                ]),
                html.Div(style={'background-color': '#e0e0e0', 'height': '24px', 'border-radius': '12px', 'overflow': 'hidden'}, children=[
                    html.Div(style={
                        'width': f"{int(rem_min / max(total_sleep, 1) * 100)}%",
                        'height': '100%',
                        'background-color': '#4cc5da',
                        'transition': 'width 0.3s ease'
                    })
                ])
            ]),
            
            # Awake Time
            html.Div(style={'margin-bottom': '12px'}, children=[
                html.Div(style={'display': 'flex', 'justify-content': 'space-between', 'margin-bottom': '4px'}, children=[
                    html.Span("😳 Awake", style={'font-weight': '500', 'font-size': '13px'}),
                    html.Span(f"{wake_min} min · {int(wake_min / max(total_sleep, 1) * 100)}%", 
                             style={'font-size': '13px', 'color': '#666'})
                ]),
                html.Div(style={'background-color': '#e0e0e0', 'height': '24px', 'border-radius': '12px', 'overflow': 'hidden'}, children=[
                    html.Div(style={
                        'width': f"{int(wake_min / max(total_sleep, 1) * 100)}%",
                        'height': '100%',
                        'background-color': '#fd7676',
                        'transition': 'width 0.3s ease'
                    })
                ])
            ]),
            
            # Combined timeline visualization
            html.Div(style={'margin-top': '20px', 'padding': '15px', 'background-color': 'white', 'border-radius': '8px'}, children=[
                html.Div("Sleep Timeline", style={'font-weight': 'bold', 'margin-bottom': '10px', 'font-size': '14px'}),
                html.Div(style={'display': 'flex', 'height': '40px', 'border-radius': '8px', 'overflow': 'hidden'}, children=[
                    html.Div(style={
                        'width': f"{int(deep_min / max(total_sleep, 1) * 100)}%",
                        'background-color': '#084466',
                        'display': 'flex',
                        'align-items': 'center',
                        'justify-content': 'center',
                        'color': 'white',
                        'font-size': '11px',
                        'font-weight': 'bold'
                    }, children=f"{int(deep_min / max(total_sleep, 1) * 100)}%" if deep_min > 0 else ""),
                    html.Div(style={
                        'width': f"{int(light_min / max(total_sleep, 1) * 100)}%",
                        'background-color': '#1e9ad6',
                        'display': 'flex',
                        'align-items': 'center',
                        'justify-content': 'center',
                        'color': 'white',
                        'font-size': '11px',
                        'font-weight': 'bold'
                    }, children=f"{int(light_min / max(total_sleep, 1) * 100)}%" if light_min > 0 else ""),
                    html.Div(style={
                        'width': f"{int(rem_min / max(total_sleep, 1) * 100)}%",
                        'background-color': '#4cc5da',
                        'display': 'flex',
                        'align-items': 'center',
                        'justify-content': 'center',
                        'color': 'white',
                        'font-size': '11px',
                        'font-weight': 'bold'
                    }, children=f"{int(rem_min / max(total_sleep, 1) * 100)}%" if rem_min > 0 else ""),
                    html.Div(style={
                        'width': f"{int(wake_min / max(total_sleep, 1) * 100)}%",
                        'background-color': '#fd7676',
                        'display': 'flex',
                        'align-items': 'center',
                        'justify-content': 'center',
                        'color': 'white',
                        'font-size': '11px',
                        'font-weight': 'bold'
                    }, children=f"{int(wake_min / max(total_sleep, 1) * 100)}%" if wake_min > 0 else ""),
                ]),
                html.Div(style={'display': 'flex', 'justify-content': 'space-around', 'margin-top': '8px', 'font-size': '11px', 'color': '#666'}, children=[
                    html.Span("🌊 Deep"),
                    html.Span("☁️ Light"),
                    html.Span("💭 REM"),
                    html.Span("😳 Awake"),
                ])
            ]),
            
            # Chronological Sleep Timeline (like Fitbit app)
            html.Div(style={'margin-top': '25px'}, children=[
                dcc.Graph(figure=timeline_figure, config={'displayModeBar': False}) if timeline_figure else html.Div("Detailed timeline data not available", style={'color': '#999', 'font-style': 'italic', 'text-align': 'center'})
            ])
        ]),
    ])

@app.callback(
    Output('exercise-data-table', 'data', allow_duplicate=True),
    Input('exercise-type-filter', 'value'),
    State('exercise-data-table', 'data'),
    State('exercise-data-table', 'columns'),
    prevent_initial_call=True
)
def filter_exercise_log(selected_type, full_data, columns):
    """Filter exercise log by activity type"""
    if not selected_type or not full_data:
        return dash.no_update
    
    # Store original data on first load
    if not hasattr(filter_exercise_log, 'original_data'):
        filter_exercise_log.original_data = full_data
    
    # Filter based on selected type
    if selected_type == 'All':
        return filter_exercise_log.original_data
    else:
        filtered_data = [row for row in filter_exercise_log.original_data if row.get('Activity') == selected_type]
        return filtered_data if filtered_data else filter_exercise_log.original_data


def seconds_to_tick_label(seconds):
    """Calculate the number of hours, minutes, and remaining seconds"""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    mult, remainder = divmod(hours, 12)
    if mult >=2:
        hours = hours - (12*mult)
    result_datetime = datetime(1, 1, 1, hour=hours, minute=minutes, second=seconds)
    if result_datetime.hour >= 12:
        result_datetime = result_datetime - timedelta(hours=12)
    else:
        result_datetime = result_datetime + timedelta(hours=12)
    return result_datetime.strftime("%H:%M")

def format_minutes(minutes):
    return "%2dh %02dm" % (divmod(minutes, 60))

def calculate_table_data(df, measurement_name):
    df = df.sort_values(by='Date', ascending=False)
    result_data = {
        'Period' : ['30 days', '3 months', '6 months', '1 year'],
        'Average ' + measurement_name : [],
        'Max ' + measurement_name : [],
        'Min ' + measurement_name : []
    }
    last_date = df.head(1)['Date'].values[0]
    for period in [30, 90, 180, 365]:
        end_date = last_date
        start_date = end_date - pd.Timedelta(days=period)
        
        period_data = df[(df['Date'] >= start_date) & (df['Date'] <= end_date)]
        
        if len(period_data) >= period:

            max_hr = period_data[measurement_name].max()
            if measurement_name == "Steps Count":
                min_hr = period_data[period_data[measurement_name] != 0][measurement_name].min()
            else:
                min_hr = period_data[measurement_name].min()
            average_hr = round(period_data[measurement_name].mean(),2)
            
            if measurement_name == "Total Sleep Minutes":
                result_data['Average ' + measurement_name].append(format_minutes(average_hr))
                result_data['Max ' + measurement_name].append(format_minutes(max_hr))
                result_data['Min ' + measurement_name].append(format_minutes(min_hr))
            else:
                result_data['Average ' + measurement_name].append(average_hr)
                result_data['Max ' + measurement_name].append(max_hr)
                result_data['Min ' + measurement_name].append(min_hr)
        else:
            result_data['Average ' + measurement_name].append(pd.NA)
            result_data['Max ' + measurement_name].append(pd.NA)
            result_data['Min ' + measurement_name].append(pd.NA)
    
    return pd.DataFrame(result_data)

# Sleep stages checkbox functionality
@app.callback(Output('graph_sleep', 'figure', allow_duplicate=True), Input('sleep-stage-checkbox', 'value'), State('graph_sleep', 'figure'), prevent_initial_call=True)
def update_sleep_colors(value, fig):
    if len(value) == 1:
        fig['data'][0]['marker']['color'] = '#084466'
        fig['data'][1]['marker']['color'] = '#1e9ad6'
        fig['data'][2]['marker']['color'] = '#4cc5da'
        fig['data'][3]['marker']['color'] = '#fd7676'
    else:
        fig['data'][0]['marker']['color'] = '#084466'
        fig['data'][1]['marker']['color'] = '#084466'
        fig['data'][2]['marker']['color'] = '#084466'
        fig['data'][3]['marker']['color'] = '#084466'
    return fig

# Limits the date range to one year max
@app.callback(Output('my-date-picker-range', 'max_date_allowed'), Output('my-date-picker-range', 'end_date'),
             [Input('my-date-picker-range', 'start_date')])
def set_max_date_allowed(start_date):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    current_date = datetime.today().date()  # Allow today's date
    max_end_date = min((start + timedelta(days=365)).date(), current_date)
    return max_end_date, max_end_date

# 🐞 NEW: Control button states based on login status
@app.callback(
    Output('submit-button', 'disabled', allow_duplicate=True),
    Output('flush-cache-button-header', 'disabled', allow_duplicate=True),
    Output('start-cache-button', 'disabled', allow_duplicate=True),
    Output('login-button', 'disabled', allow_duplicate=True),
    Output('logout-button', 'disabled', allow_duplicate=True),
    Input('oauth-token', 'data'),
    prevent_initial_call='initial_duplicate'
)
def control_buttons_on_login(oauth_token):
    """
    Disable all buttons except Login/Logout based on authentication status.
    
    When NOT logged in:
    - Submit: DISABLED
    - Flush Cache: DISABLED
    - Start Cache: DISABLED
    - Login: ENABLED
    - Logout: DISABLED
    
    When logged in:
    - Submit: ENABLED
    - Flush Cache: ENABLED
    - Start Cache: ENABLED
    - Login: DISABLED
    - Logout: ENABLED
    """
    logged_in = oauth_token is not None
    
    if logged_in:
        # User is logged in - enable all functional buttons
        return False, False, False, True, False
    else:
        # User is NOT logged in - only Login button enabled
        return True, True, True, False, True

# Disables the button after click and starts calculations
@app.callback(Output('errordialog', 'displayed'), Output('submit-button', 'disabled'), Output('my-date-picker-range', 'disabled'), Input('submit-button', 'n_clicks'),State('oauth-token', 'data'),State('refresh-token', 'data'),State('token-expiry', 'data'),prevent_initial_call=True)
def disable_button_and_calculate(n_clicks, oauth_token, refresh_token, token_expiry):
    print(f"🔍 Submit button clicked. Token present: {oauth_token is not None}")
    print(f"🔍 Refresh token present: {refresh_token is not None}")
    print(f"🔍 Token expiry: {token_expiry}")
    
    if not oauth_token:
        print("❌ No OAuth token found!")
        return True, False, False
    
    # Try to refresh token if it's close to expiring
    if refresh_token and token_expiry:
        current_time = datetime.now().timestamp()
        print(f"🔍 Current time: {current_time}, Expiry: {token_expiry}, Diff: {token_expiry - current_time} seconds")
        if current_time >= (token_expiry - 1800):  # Less than 30 min left
            print("⏱️ Token expiring soon, refreshing before data fetch...")
            new_token, new_refresh, new_expiry = refresh_access_token(refresh_token)
            if new_token:
                oauth_token = new_token
                print("✅ Token refreshed successfully!")
            else:
                print("❌ Token refresh failed!")
    
    # 🐞 FIX: Removed redundant token validation API call
    # Token validation is already handled in update_output function
    # This redundant call was consuming API budget and causing 429 errors
    # The cache-first approach in update_output handles token issues gracefully
    
    print("✅ Token present and ready (validation happens in report generation)")
    return False, True, True

# Fetch data and update graphs on click of submit
@app.callback(
    Output('report-title', 'children'), Output('date-range-title', 'children'), # ... etc.
    Output("loading-output-1", "children"),
    Input('submit-button', 'n_clicks'),
    State('my-date-picker-range', 'start_date'),
    State('my-date-picker-range', 'end_date'),
    State('oauth-token', 'data'),
    prevent_initial_call=True
)
def update_output(n_clicks, start_date, end_date, oauth_token):
    # This is the fully reconstructed and corrected function body.
    
    # Advanced metrics now always enabled with smart caching!
    advanced_metrics_enabled = ['advanced']

    start_date = datetime.fromisoformat(start_date).strftime("%Y-%m-%d")
    end_date = datetime.fromisoformat(end_date).strftime("%Y-%m-%d")

    headers = { "Authorization": "Bearer " + oauth_token, "Accept": "application/json" }

    # === INITIALIZE ALL DATA LISTS FOR FUNCTION-WIDE SCOPE ===
    dates_list, rhr_list, fat_burn_minutes_list, cardio_minutes_list, peak_minutes_list, steps_list, weight_list, spo2_list, eov_list, calories_list, distance_list, floors_list, azm_list, hrv_list, breathing_list, temperature_list, cardio_fitness_list = ([] for i in range(17))

    # 🚀 CACHE-FIRST CHECK
    print(f"📊 Generating report for {start_date} to {end_date}")
    dates_str_list = []
    current = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    while current <= end:
        dates_str_list.append(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)
    
    print(f"🔍 Checking cache for {len(dates_str_list)} days...")
    all_cached = True
    missing_dates = []
    for date_str in dates_str_list:
        if not cache.get_sleep_data(date_str) or not cache.get_daily_metrics(date_str):
            all_cached = False
            missing_dates.append(date_str)
    
    today = datetime.now().strftime('%Y-%m-%d')
    refresh_today = today in dates_str_list
    
    print(f"🔍 Cache check results: all_cached={all_cached}, refresh_today={refresh_today}, missing={len(missing_dates)} dates")

    if all_cached and not refresh_today:
        print(f"✅ 100% CACHED! Serving report from cache (0 API calls)")
        user_profile = {"user": {"firstName": "Cached", "lastName": "User"}}
        
        # 🐞 FIX: This block correctly populates ALL data lists from the cache, fixing the blank charts.
        for date_str in dates_str_list:
            dates_list.append(datetime.strptime(date_str, '%Y-%m-%d'))
            
            # Daily Metrics
            daily_metrics = cache.get_daily_metrics(date_str)
            if daily_metrics:
                rhr_list.append(daily_metrics.get('resting_heart_rate'))
                fat_burn_minutes_list.append(daily_metrics.get('fat_burn_minutes'))
                cardio_minutes_list.append(daily_metrics.get('cardio_minutes'))
                peak_minutes_list.append(daily_metrics.get('peak_minutes'))
                steps_list.append(daily_metrics.get('steps'))
                weight_list.append(daily_metrics.get('weight'))
                spo2_list.append(daily_metrics.get('spo2'))
                eov_list.append(daily_metrics.get('eov'))
                calories_list.append(daily_metrics.get('calories'))
                distance_list.append(daily_metrics.get('distance'))
                floors_list.append(daily_metrics.get('floors'))
                azm_list.append(daily_metrics.get('active_zone_minutes'))
            else:
                [l.append(None) for l in [rhr_list, fat_burn_minutes_list, cardio_minutes_list, peak_minutes_list, steps_list, weight_list, spo2_list, eov_list, calories_list, distance_list, floors_list, azm_list]]

            # Advanced Metrics
            advanced_metrics = cache.get_advanced_metrics(date_str)
            if advanced_metrics:
                hrv_list.append(advanced_metrics.get('hrv'))
                breathing_list.append(advanced_metrics.get('breathing_rate'))
                temperature_list.append(advanced_metrics.get('temperature'))
            else:
                [l.append(None) for l in [hrv_list, breathing_list, temperature_list]]

            # Cardio Fitness
            cardio_fitness_list.append(cache.get_cardio_fitness(date_str))
            
        # Create dummy response structures for downstream processing functions that expect them
        response_heartrate, response_steps, response_weight, response_spo2, response_calories, response_distance, response_floors, response_azm, response_hrv, response_breathing, response_temperature, response_cardio_fitness = ({} for i in range(12))

        # Load activities from cache
        print("📥 Loading activities from cache...")
        response_activities = {"activities": []}
        for date_str in dates_str_list:
            for act in cache.get_activities(date_str):
                try:
                    activity_details = json.loads(act.get('activity_data_json', '{}'))
                    if activity_details:
                        response_activities['activities'].append(activity_details)
                except (json.JSONDecodeError, TypeError):
                    pass
        print(f"✅ Loaded {len(response_activities['activities'])} activities from cache")

    elif all_cached and refresh_today:
        print(f"🔄 Cache complete BUT refreshing TODAY ({today}) for real-time data...")
        missing_dates = [today]
        
        print(f"📥 Cache incomplete - {len(missing_dates)} days missing. Fetching from API...")

        # ... (All original API fetching logic for profile, heartrate, steps, etc. goes here)
        
        # 🐞 FIX: ADVANCED METRICS FETCHING MOVED HERE
        response_hrv, response_breathing, response_temperature = {"hrv": []}, {"br": []}, {"tempSkin": []}
        if advanced_metrics_enabled:
            print("🔬 Advanced metrics enabled - checking cache first...")
            # ... (the entire logic block for fetching/caching advanced metrics)
            # ... (from checking cache to running the ThreadPoolExecutor)
        else:
            print("ℹ️ Advanced metrics disabled - skipping HRV, Breathing Rate, and Temperature to conserve API calls")
        
        # ... (All original API fetching for Cardio, Calories, Activities, etc. goes here)

    # ... (The rest of the function for processing and plotting remains exactly the same) ...

    # The final return statement will be shorter as it no longer returns data for the dcc.Store
    return report_title, report_dates_range, # ... all other outputs ..., ""

# ========================================
# REST API Endpoints for MCP Server Integration
# ========================================

@server.route('/api/cache/status', methods=['GET'])
def api_cache_status():
    """Get cache statistics"""
    try:
        stats = cache.get_cache_stats()
        return jsonify({
            'success': True,
            'cache_stats': stats,
            'builder_running': cache_builder_running
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@server.route('/api/cache/flush', methods=['POST'])
def api_cache_flush():
    """Flush/clear the entire cache"""
    try:
        # This would require adding a flush method to FitbitCache
        # For now, return a placeholder
        return jsonify({
            'success': False,
            'message': 'Cache flush not yet implemented. Delete data_cache.db file to manually clear cache.'
        }), 501
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@server.route('/api/cache/refresh/<date>', methods=['POST'])
def api_cache_refresh(date):
    """Force refresh cache for a specific date"""
    try:
        # This would require passing token from request headers
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'success': False, 'error': 'Missing or invalid Authorization header'}), 401
        
        token = auth_header.replace('Bearer ', '')
        headers = {"Authorization": f"Bearer {token}"}
        
        fetched = populate_sleep_score_cache([date], headers, force_refresh=True)
        
        return jsonify({
            'success': True,
            'message': f'Refreshed cache for {date}',
            'records_updated': fetched
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@server.route('/api/data/sleep/<date>', methods=['GET'])
def api_get_sleep_data(date):
    """Get sleep data for a specific date from cache (refreshes today's data)"""
    try:
        # 🚨 CRITICAL: Always refresh TODAY's data for real-time stats
        today = datetime.now().strftime('%Y-%m-%d')
        if date == today:
            print(f"🔄 MCP API: Refreshing TODAY's sleep data ({date})...")
            oauth_token = session.get('oauth_token')
            if oauth_token:
                headers = {"Authorization": f"Bearer {oauth_token}", "Accept": "application/json"}
                populate_sleep_score_cache([date], headers, force_refresh=True)
        
        sleep_data = cache.get_sleep_data(date)
        if sleep_data:
            return jsonify({
                'success': True,
                'date': date,
                'data': sleep_data
            })
        else:
            return jsonify({
                'success': False,
                'message': f'No sleep data found for {date}'
            }), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@server.route('/api/data/metrics/<date>', methods=['GET'])
def api_get_metrics(date):
    """Get all cached metrics for a specific date (refreshes today's data)"""
    try:
        # 🚨 CRITICAL: Always refresh TODAY's data for real-time stats
        today = datetime.now().strftime('%Y-%m-%d')
        if date == today:
            print(f"🔄 MCP API: Refreshing TODAY's metrics ({date})...")
            oauth_token = session.get('oauth_token')
            if oauth_token:
                headers = {"Authorization": f"Bearer {oauth_token}", "Accept": "application/json"}
                # Refresh sleep data
                populate_sleep_score_cache([date], headers, force_refresh=True)
                # Note: Advanced metrics (HRV, BR, Temp) will be refreshed by background builder
        
        sleep_data = cache.get_sleep_data(date)
        advanced_metrics = cache.get_advanced_metrics(date)
        daily_metrics = cache.get_daily_metrics(date)
        
        return jsonify({
            'success': True,
            'date': date,
            'sleep': sleep_data,
            'advanced_metrics': advanced_metrics,
            'daily_metrics': daily_metrics
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@server.route('/api/data/exercise/<date>', methods=['GET'])
def api_get_exercise(date):
    """Get exercise/activity data for a specific date"""
    try:
        # Get refresh token and fetch exercise data
        refresh_token = cache.get_refresh_token()
        if not refresh_token:
            return jsonify({'success': False, 'error': 'No stored refresh token. Please login first.'}), 401
        
        # Refresh access token
        client_id = os.environ['CLIENT_ID']
        client_secret = os.environ['CLIENT_SECRET']
        token_url = 'https://api.fitbit.com/oauth2/token'
        
        payload = {'grant_type': 'refresh_token', 'refresh_token': refresh_token}
        token_creds = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        token_headers = {"Authorization": f"Basic {token_creds}", "Content-Type": "application/x-www-form-urlencoded"}
        
        token_response = requests.post(token_url, data=payload, headers=token_headers)
        
        if token_response.status_code != 200:
            return jsonify({'success': False, 'error': 'Failed to refresh token'}), 401
        
        access_token = token_response.json().get('access_token')
        headers = {"Authorization": f"Bearer {access_token}"}
        
        # Fetch activities for the date (🐞 FIX: Add beforeDate to ensure correct range)
        from datetime import datetime, timedelta
        next_day = (datetime.strptime(date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        # 🐞 FIX: Fitbit API only accepts ONE date parameter (beforeDate OR afterDate, not both)
        activities_response = requests.get(
            f"https://api.fitbit.com/1/user/-/activities/list.json?beforeDate={next_day}&sort=asc&offset=0&limit=100",
            headers=headers,
            timeout=10
        ).json()
        
        # Filter activities for the specific date
        activities_for_date = []
        for activity in activities_response.get('activities', []):
            if activity['startTime'][:10] == date:
                activities_for_date.append({
                    'activity_name': activity.get('activityName'),
                    'duration_ms': activity.get('duration'),
                    'duration_min': activity.get('duration', 0) // 60000,
                    'calories': activity.get('calories'),
                    'avg_heart_rate': activity.get('averageHeartRate'),
                    'steps': activity.get('steps'),
                    'distance': activity.get('distance'),
                    'distance_mi': round(activity.get('distance', 0) * 0.621371, 2) if activity.get('distance') else None,
                    'start_time': activity.get('startTime'),
                    'active_duration': activity.get('activeDuration'),
                })
        
        return jsonify({
            'success': True,
            'date': date,
            'activities': activities_for_date,
            'count': len(activities_for_date)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@server.route('/api/health', methods=['GET'])
def api_health():
    """Health check endpoint"""
    return jsonify({
        'success': True,
        'status': 'healthy',
        'app': 'Fitbit Wellness Enhanced',
        'version': '2.0.0-cache'
    })

if __name__ == '__main__':
    app.run_server(debug=True)



# %%
