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


# %%

log = logging.getLogger(__name__)

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

def background_cache_builder(access_token: str):
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
    
    try:
        while cache_builder_running:
            api_calls_this_hour = 0
            MAX_CALLS_PER_HOUR = 145  # Conservative limit (leave 5 for user reports)
            
            headers = {"Authorization": f"Bearer {access_token}"}
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
                ("Activities", f"https://api.fitbit.com/1/user/-/activities/list.json?afterDate={start_date_str}&sort=asc&offset=0&limit=100"),
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
                    print(f"✅ Success ({response.status_code})")
                    # Data is cached automatically by report generation logic
                    
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
                                        
                                        if sleep_score is not None:
                                            print(f"✅ YESTERDAY REFRESH - Found REAL sleep score for {yesterday}: {sleep_score}")
                                            cache.set_sleep_score(
                                                date=yesterday,
                                                sleep_score=sleep_score,
                                                efficiency=sleep_record.get('efficiency'),
                                                total_sleep=sleep_record.get('minutesAsleep'),
                                                deep=sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes'),
                                                light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                                rem=sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes'),
                                                wake=sleep_record.get('levels', {}).get('summary', {}).get('wake', {}).get('minutes'),
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
                                            
                                            if sleep_score is not None:
                                                print(f"✅ PHASE 3 - Found REAL sleep score for {date_str}: {sleep_score}")
                                                cache.set_sleep_score(
                                                    date=date_str,
                                                    sleep_score=sleep_score,
                                                    efficiency=sleep_record.get('efficiency'),
                                                    total_sleep=sleep_record.get('minutesAsleep'),
                                                    deep=sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes'),
                                                    light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                                    rem=sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes'),
                                                    wake=sleep_record.get('levels', {}).get('summary', {}).get('wake', {}).get('minutes'),
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
                            pass
                
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
                        
                        # NEVER fallback to efficiency - efficiency != sleep score!
                        # If Fitbit doesn't provide a sleep score, store None
                        if sleep_score is not None or 'efficiency' in sleep_record:
                            # Cache the sleep score and related data
                            cache.set_sleep_score(
                                date=date_str,
                                sleep_score=sleep_score,
                                efficiency=sleep_record.get('efficiency'),
                                total_sleep=sleep_record.get('minutesAsleep'),
                                deep=sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes'),
                                light=sleep_record.get('levels', {}).get('summary', {}).get('light', {}).get('minutes'),
                                rem=sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes'),
                                wake=sleep_record.get('levels', {}).get('summary', {}).get('wake', {}).get('minutes'),
                                start_time=sleep_record.get('startTime'),
                                sleep_data_json=str(sleep_record)
                            )
                            fetched_count += 1
                            print(f"✅ Cached sleep score for {date_str}: {sleep_score}")
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
        scope = 'profile activity cardio_fitness heartrate sleep weight oxygen_saturation respiratory_rate temperature location'
        auth_url = f'https://www.fitbit.com/oauth2/authorize?scope={scope}&client_id={client_id}&response_type=code&prompt=none&redirect_uri={redirect_uri}'
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

@app.callback(Output('login-button', 'children'),Output('login-button', 'disabled'),Input('oauth-token', 'data'))
def update_login_button(oauth_token):
    if oauth_token:
        # Start background cache builder if not already running
        global cache_builder_thread, cache_builder_running, auto_sync_thread, auto_sync_running
        if not cache_builder_running and (cache_builder_thread is None or not cache_builder_thread.is_alive()):
            print("🚀 Launching background cache builder...")
            cache_builder_thread = threading.Thread(
                target=background_cache_builder, 
                args=(oauth_token,),
                daemon=True
            )
            cache_builder_thread.start()
        
        # Start automatic daily sync if not already running
        if not auto_sync_running and (auto_sync_thread is None or not auto_sync_thread.is_alive()):
            print("🤖 Launching automatic daily sync...")
            auto_sync_thread = threading.Thread(
                target=automatic_daily_sync,
                daemon=True
            )
            auto_sync_thread.start()
        
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
    """Handle cache flush button click"""
    if n_clicks and n_clicks > 0:
        try:
            cache.flush_cache()
            return True, "✅ Cache flushed successfully! Your login is preserved. Generate a new report to rebuild cache."
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
            # Launch background cache builder thread
            cache_builder_thread = threading.Thread(target=background_cache_builder, args=(oauth_token,), daemon=True)
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
    """Display detailed workout information including HR zones for selected date"""
    if not selected_date or not oauth_token:
        return html.Div("Select a workout date to view details", style={'color': '#999', 'font-style': 'italic'})
    
    # Get stored activity data for the date
    if selected_date not in exercise_data_store:
        return html.Div(f"No workout data available for {selected_date}", style={'color': '#999'})
    
    activities = exercise_data_store[selected_date]
    
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
            
            # Fetch intraday heart rate data
            headers = {'Authorization': f'Bearer {oauth_token}'}
            url = f"https://api.fitbit.com/1/user/-/activities/heart/date/{date_str}/1d/1sec.json"
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
    
    # Get stored sleep data for the date
    if selected_date not in sleep_detail_data_store:
        return html.Div(f"No sleep data available for {selected_date}", style={'color': '#999'})
    
    sleep_data = sleep_detail_data_store[selected_date]
    
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
                fig_timeline = go.Figure()
                
                # Build timeline segments with actual start/end times
                for idx, entry in enumerate(stages_data):
                    stage = entry.get('level', '').lower()
                    start_time = datetime.fromisoformat(entry['dateTime'].replace('Z', '+00:00'))
                    duration_seconds = entry.get('seconds', 0)
                    end_time = start_time + timedelta(seconds=duration_seconds)
                    duration_minutes = duration_seconds / 60
                    
                    # Convert to milliseconds (JSON serializable)
                    start_ms = int(start_time.timestamp() * 1000)
                    duration_ms = duration_seconds * 1000
                    
                    # Create a horizontal bar segment for this stage with REAL times
                    fig_timeline.add_trace(go.Bar(
                        base=[start_ms],  # Start time in milliseconds
                        x=[duration_ms],  # Duration in milliseconds
                        y=["Sleep"],
                        orientation='h',
                        marker=dict(
                            color=stage_colors.get(stage, '#ccc'),
                            line=dict(width=0)
                        ),
                        name=stage_names.get(stage, stage),
                        hovertemplate=f"<b>{stage_names.get(stage, stage)}</b><br>" +
                                      f"{start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}<br>" +
                                      f"Duration: {int(duration_minutes)} min<br>" +
                                      "<extra></extra>",
                        showlegend=(idx == 0 or stage != stages_data[idx-1].get('level', '').lower()),
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
    
    # Get sleep score rating
    score = sleep_data.get('sleep_score', 0)
    if score >= 80:
        rating = "Excellent"
        rating_color = "#4caf50"
        rating_emoji = "🌟"
    elif score >= 60:
        rating = "Good"
        rating_color = "#8bc34a"
        rating_emoji = "😊"
    else:
        rating = "Fair"
        rating_color = "#ff9800"
        rating_emoji = "😐"
    
    # Calculate total sleep for percentages
    total_sleep = sleep_data.get('total_sleep', 1)
    deep_min = sleep_data.get('deep', 0)
    light_min = sleep_data.get('light', 0)
    rem_min = sleep_data.get('rem', 0)
    wake_min = sleep_data.get('wake', 0)
    
    # Build detailed display
    return html.Div(style={'background-color': '#f8f9fa', 'padding': '20px', 'border-radius': '10px'}, children=[
        html.H6(f"Sleep Night: {selected_date}", style={'color': '#2c3e50', 'margin-bottom': '15px'}),
        
        # Summary stats
        html.Div(style={'display': 'grid', 'grid-template-columns': 'repeat(auto-fit, minmax(150px, 1fr))', 'gap': '10px', 'margin-bottom': '20px'}, children=[
            html.Div([
                html.Strong("Sleep Score: "),
                html.Span(f"{sleep_data.get('sleep_score', 'N/A')}", style={'color': rating_color, 'font-size': '24px', 'font-weight': 'bold'}),
                html.Br(),
                html.Span(f"{rating_emoji} {rating}", style={'color': rating_color, 'font-weight': 'bold', 'font-size': '14px'})
            ]),
            html.Div([
                html.Strong("Total Sleep: "),
                html.Span(f"{total_sleep // 60}h {total_sleep % 60}m")
            ]),
            html.Div([
                html.Strong("Sleep Start: "),
                html.Span(f"{sleep_data.get('start_time', 'N/A')[:16]}" if sleep_data.get('start_time') else 'N/A')
            ]),
            html.Div([
                html.Strong("Efficiency: "),
                html.Span(f"{sleep_data.get('efficiency', 'N/A')}%", style={'color': '#4caf50', 'font-weight': 'bold'})
            ]) if sleep_data.get('efficiency') else html.Div(),
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
    Output('exercise_log_table', 'children', allow_duplicate=True),
    Input('exercise-type-filter', 'value'),
    State('exercise_log_table', 'children'),
    prevent_initial_call=True
)
def filter_exercise_log(selected_type, current_table):
    """Filter exercise log by activity type"""
    if not selected_type or not isinstance(current_table, dash_table.DataTable):
        return dash.no_update
    
    # Get the full data from the table
    try:
        full_data = current_table.data
        
        # Filter based on selected type
        if selected_type == 'All':
            filtered_data = full_data
        else:
            filtered_data = [row for row in full_data if row.get('Activity') == selected_type]
        
        if filtered_data and len(filtered_data) > 0:
            return dash_table.DataTable(
                filtered_data,
                [{"name": i, "id": i} for i in full_data[0].keys()],
                style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}],
                style_header={'backgroundColor': '#336699','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'},
                style_cell={'textAlign': 'center'},
                page_size=20
            )
        else:
            return html.P(f"No {selected_type} activities in this period.", style={'text-align': 'center', 'color': '#888'})
    except Exception as e:
        print(f"Error filtering exercise log: {e}")
        return dash.no_update


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
    
    headers = {
        "Authorization": "Bearer " + oauth_token,
        "Accept": "application/json"
    }
    try:
        print("🔍 Validating token with profile API...")
        token_response = requests.get("https://api.fitbit.com/1/user/-/profile.json", headers=headers, timeout=10)
        print(f"🔍 Validation response status: {token_response.status_code}")
        if token_response.status_code != 200:
            print(f"❌ Validation failed! Response: {token_response.text[:200]}")
        token_response.raise_for_status()
        print("✅ Token validation successful!")
    except Exception as e:
        print(f"❌ Token validation exception: {type(e).__name__}: {str(e)}")
        return True, False, False
    return False, True, True

# Fetch data and update graphs on click of submit
@app.callback(Output('report-title', 'children'), Output('date-range-title', 'children'), Output('generated-on-title', 'children'), Output('graph_RHR', 'figure'), Output('RHR_table', 'children'), Output('graph_steps', 'figure'), Output('graph_steps_heatmap', 'figure'), Output('steps_table', 'children'), Output('graph_activity_minutes', 'figure'), Output('fat_burn_table', 'children'), Output('cardio_table', 'children'), Output('peak_table', 'children'), Output('graph_weight', 'figure'), Output('weight_table', 'children'), Output('graph_spo2', 'figure'), Output('spo2_table', 'children'), Output('graph_eov', 'figure'), Output('eov_table', 'children'), Output('graph_sleep', 'figure'), Output('graph_sleep_regularity', 'figure'), Output('sleep_table', 'children'), Output('sleep-stage-checkbox', 'options'), Output('graph_hrv', 'figure'), Output('hrv_table', 'children'), Output('graph_breathing', 'figure'), Output('breathing_table', 'children'), Output('graph_cardio_fitness', 'figure'), Output('cardio_fitness_table', 'children'), Output('graph_temperature', 'figure'), Output('temperature_table', 'children'), Output('graph_azm', 'figure'), Output('azm_table', 'children'), Output('graph_calories', 'figure'), Output('graph_distance', 'figure'), Output('calories_table', 'children'), Output('graph_floors', 'figure'), Output('floors_table', 'children'), Output('exercise-type-filter', 'options'), Output('exercise_log_table', 'children'), Output('workout-date-selector', 'options'), Output('graph_sleep_score', 'figure'), Output('graph_sleep_stages_pie', 'figure'), Output('sleep-date-selector', 'options'), Output('graph_exercise_sleep_correlation', 'figure'), Output('graph_azm_sleep_correlation', 'figure'), Output('correlation_insights', 'children'), Output("loading-output-1", "children"),
Input('submit-button', 'disabled'),
State('my-date-picker-range', 'start_date'), State('my-date-picker-range', 'end_date'), State('oauth-token', 'data'),
prevent_initial_call=True)
def update_output(n_clicks, start_date, end_date, oauth_token):
    # Advanced metrics now always enabled with smart caching!
    advanced_metrics_enabled = ['advanced']  # Always enabled

    start_date = datetime.fromisoformat(start_date).strftime("%Y-%m-%d")
    end_date = datetime.fromisoformat(end_date).strftime("%Y-%m-%d")

    headers = {
        "Authorization": "Bearer " + oauth_token,
        "Accept": "application/json"
    }

    # 🚀 CACHE-FIRST CHECK: Verify if ALL data is cached before making ANY API calls
    print(f"📊 Generating report for {start_date} to {end_date}")
    
    # Generate list of dates in range
    dates_str_list = []
    current = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    while current <= end:
        dates_str_list.append(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)
    
    print(f"🔍 Checking cache for {len(dates_str_list)} days...")
    
    # Check if ALL required data is in cache
    all_cached = True
    missing_dates = []
    
    for date_str in dates_str_list:
        sleep_data = cache.get_sleep_data(date_str)
        advanced_data = cache.get_advanced_metrics(date_str)
        daily_data = cache.get_daily_metrics(date_str)
        
        if not sleep_data or not advanced_data or not daily_data:
            all_cached = False
            missing_dates.append(date_str)
    
    # 🚨 CRITICAL: Always refresh TODAY's data if it's in the range
    today = datetime.now().strftime('%Y-%m-%d')
    refresh_today = today in dates_str_list
    
    if all_cached and not refresh_today:
        print(f"✅ 100% CACHED! Serving report from cache (0 API calls)")
        # Skip ALL API calls - serve directly from cache
        # We'll still need to populate the response structures from cache below
        user_profile = {"user": {"displayName": "Cached User"}}  # Dummy profile
        response_heartrate = {"activities-heart": [{"dateTime": d} for d in dates_str_list]}
        response_steps = {"activities-steps": []}
        response_weight = {"weight": []}
        response_spo2 = []
    elif all_cached and refresh_today:
        print(f"🔄 Cache complete BUT refreshing TODAY ({today}) for real-time data...")
        # Refresh today's data, but serve the rest from cache
        missing_dates = [today]
        all_cached = False  # Force API calls for today only
    
    if not all_cached:
        print(f"📥 Cache incomplete - {len(missing_dates)} days missing. Fetching from API...")
        print(f"Missing dates: {missing_dates[:5]}{'...' if len(missing_dates) > 5 else ''}")
        
        # Collecting data-----------------------------------------------------------------------------------------------------------------------
        
        try:
            user_profile = requests.get("https://api.fitbit.com/1/user/-/profile.json", headers=headers).json()
            
            # Check for rate limiting or errors
            if 'error' in user_profile:
                error_code = user_profile['error'].get('code')
                if error_code == 429:
                    print("⚠️ RATE LIMIT EXCEEDED! Fitbit API limit: 150 requests/hour")
                    print("Please wait at least 1 hour before generating another report.")
                    # Return with error message (44 outputs total)
                    empty_fig = px.line(title="Rate Limit Exceeded - Please wait 1 hour")
                    empty_heatmap = px.imshow([[0]], title="Rate Limit Exceeded")
                    return "⚠️ Rate Limit Exceeded", "Please wait at least 1 hour before trying again", "", empty_fig, [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.line(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("Rate limit exceeded"), [], px.line(), px.pie(), [], px.scatter(), px.scatter(), html.P("Rate limit exceeded"), ""
                else:
                    print(f"API Error: {user_profile['error']}")
                    
            response_heartrate = requests.get("https://api.fitbit.com/1/user/-/activities/heart/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
            
            # Check for rate limiting in heart rate response
            if 'error' in response_heartrate:
                error_code = response_heartrate['error'].get('code')
                if error_code == 429:
                    print("⚠️ RATE LIMIT EXCEEDED! Fitbit API limit: 150 requests/hour")
                    print("Please wait at least 1 hour before generating another report.")
                    empty_fig = px.line(title="Rate Limit Exceeded - Please wait 1 hour")
                    empty_heatmap = px.imshow([[0]], title="Rate Limit Exceeded")
                    return "⚠️ Rate Limit Exceeded", "Please wait at least 1 hour before trying again", "", empty_fig, [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.line(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("Rate limit exceeded"), [], px.line(), px.pie(), [], px.scatter(), px.scatter(), html.P("Rate limit exceeded"), ""
                    
            response_steps = requests.get("https://api.fitbit.com/1/user/-/activities/steps/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
            response_weight = requests.get("https://api.fitbit.com/1/user/-/body/weight/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
            response_spo2 = requests.get("https://api.fitbit.com/1/user/-/spo2/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
        except Exception as e:
            print(f"ERROR fetching initial data: {e}")
            # Return empty results if API calls fail with valid empty plots
            empty_fig = px.line(title="Error Fetching Data")
            empty_heatmap = px.imshow([[0]], title="No Data Available")
            return dash.no_update, dash.no_update, dash.no_update, empty_fig, [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("Error fetching data"), [], px.line(), px.pie(), [], px.scatter(), px.scatter(), html.P("Error fetching data"), ""
    
    # Build dates list for processing (use our pre-generated list if all_cached)
    if all_cached:
        temp_dates_list = dates_str_list
    else:
        temp_dates_list = []
        if 'activities-heart' in response_heartrate:
            for entry in response_heartrate['activities-heart']:
                temp_dates_list.append(entry['dateTime'])
        else:
            print(f"ERROR: No heart rate data in response: {response_heartrate}")
            empty_heatmap = px.imshow([[0]], title="No Data Available")
            return dash.no_update, dash.no_update, dash.no_update, px.line(), [], px.bar(), empty_heatmap, [], px.bar(), [], [], [], px.line(), [], px.scatter(), [], px.bar(), px.bar(), [], [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': True}], px.line(), [], px.line(), [], px.line(), [], px.line(), [], px.bar(), [], px.bar(), px.bar(), [], px.bar(), [], [{'label': 'All', 'value': 'All'}], html.P("No heart rate data"), [], px.line(), px.pie(), [], px.scatter(), px.scatter(), html.P("No heart rate data"), ""
    
    # 🚀 CACHE-FIRST: Check cache for advanced metrics before fetching from API
    response_hrv = {"hrv": []}
    response_breathing = {"br": []}
    response_temperature = {"tempSkin": []}
    
    if advanced_metrics_enabled and 'advanced' in advanced_metrics_enabled:
        print("🔬 Advanced metrics enabled - checking cache first...")
        
        # Check cache for each date
        missing_hrv = []
        missing_br = []
        missing_temp = []
        cached_count = {'hrv': 0, 'br': 0, 'temp': 0}
        
        for date_str in temp_dates_list:
            cached_advanced = cache.get_advanced_metrics(date_str)
            
            if cached_advanced:
                # HRV from cache
                if cached_advanced.get('hrv') is not None:
                    response_hrv["hrv"].append({
                        "dateTime": date_str,
                        "value": {"dailyRmssd": cached_advanced['hrv']}
                    })
                    cached_count['hrv'] += 1
                else:
                    missing_hrv.append(date_str)
                
                # Breathing Rate from cache
                if cached_advanced.get('breathing_rate') is not None:
                    response_breathing["br"].append({
                        "dateTime": date_str,
                        "value": {"breathingRate": cached_advanced['breathing_rate']}
                    })
                    cached_count['br'] += 1
                else:
                    missing_br.append(date_str)
                
                # Temperature from cache
                if cached_advanced.get('temperature') is not None:
                    response_temperature["tempSkin"].append({
                        "dateTime": date_str,
                        "value": cached_advanced['temperature']
                    })
                    cached_count['temp'] += 1
                else:
                    missing_temp.append(date_str)
            else:
                # No cache entry for this date - need to fetch all three
                missing_hrv.append(date_str)
                missing_br.append(date_str)
                missing_temp.append(date_str)
        
        print(f"✅ Loaded from cache: HRV={cached_count['hrv']}, BR={cached_count['br']}, Temp={cached_count['temp']}")
        
        # Only fetch missing data
        total_missing = len(set(missing_hrv + missing_br + missing_temp))
        if total_missing > 0:
            print(f"📥 Fetching {total_missing} missing advanced metrics from API...")
            
            def fetch_hrv_day(date_str):
                try:
                    hrv_day = requests.get(f"https://api.fitbit.com/1/user/-/hrv/date/{date_str}.json", headers=headers, timeout=10).json()
                    if "hrv" in hrv_day and len(hrv_day["hrv"]) > 0:
                        return {"dateTime": date_str, "value": hrv_day["hrv"][0]["value"]}
                except:
                    pass
                return None
            
            def fetch_breathing_day(date_str):
                try:
                    br_day = requests.get(f"https://api.fitbit.com/1/user/-/br/date/{date_str}.json", headers=headers, timeout=10).json()
                    if "br" in br_day and len(br_day["br"]) > 0:
                        return {"dateTime": date_str, "value": br_day["br"][0]["value"]}
                except:
                    pass
                return None
            
            def fetch_temperature_day(date_str):
                try:
                    temp_day = requests.get(f"https://api.fitbit.com/1/user/-/temp/skin/date/{date_str}.json", headers=headers, timeout=10).json()
                    if "tempSkin" in temp_day and len(temp_day["tempSkin"]) > 0:
                        return {"dateTime": date_str, "value": temp_day["tempSkin"][0]["value"]}
                except:
                    pass
                return None
            
            # Fetch only missing data in parallel
            with ThreadPoolExecutor(max_workers=20) as executor:
                # Submit only missing dates
                hrv_futures = {executor.submit(fetch_hrv_day, date): date for date in missing_hrv}
                br_futures = {executor.submit(fetch_breathing_day, date): date for date in missing_br}
                temp_futures = {executor.submit(fetch_temperature_day, date): date for date in missing_temp}
                
                # Collect HRV results
                for future in as_completed(hrv_futures):
                    result = future.result()
                    if result:
                        response_hrv["hrv"].append(result)
                        # Cache immediately
                        try:
                            cache.set_advanced_metrics(
                                date=result["dateTime"],
                                hrv=result["value"]["dailyRmssd"]
                            )
                        except:
                            pass
                
                # Collect Breathing Rate results
                for future in as_completed(br_futures):
                    result = future.result()
                    if result:
                        response_breathing["br"].append(result)
                        # Cache immediately
                        try:
                            cache.set_advanced_metrics(
                                date=result["dateTime"],
                                breathing_rate=result["value"]["breathingRate"]
                            )
                        except:
                            pass
                
                # Collect Temperature results
                for future in as_completed(temp_futures):
                    result = future.result()
                    if result:
                        response_temperature["tempSkin"].append(result)
                        # Cache immediately
                        try:
                            temp_value = result["value"]
                            if isinstance(temp_value, dict):
                                temp_value = temp_value.get("nightlyRelative", temp_value.get("value"))
                            cache.set_advanced_metrics(
                                date=result["dateTime"],
                                temperature=temp_value
                            )
                        except:
                            pass
            
            print(f"✅ Fetched and cached: HRV={len([r for r in response_hrv['hrv'] if r['dateTime'] in missing_hrv])}, BR={len([r for r in response_breathing['br'] if r['dateTime'] in missing_br])}, Temp={len([r for r in response_temperature['tempSkin'] if r['dateTime'] in missing_temp])}")
        else:
            print("✅ All advanced metrics loaded from cache - 0 API calls!")
        
        print(f"📊 Total: HRV={len(response_hrv['hrv'])}, BR={len(response_breathing['br'])}, Temp={len(response_temperature['tempSkin'])}")
    else:
        print("ℹ️ Advanced metrics disabled - skipping HRV, Breathing Rate, and Temperature to conserve API calls")
    
    # Cardio Fitness - Fetch in 30-day chunks (API limitation)
    response_cardio_fitness = {"cardioScore": []}
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    current_dt = start_dt
    while current_dt <= end_dt:
        chunk_end = min(current_dt + timedelta(days=29), end_dt)
        try:
            cf_chunk = requests.get(f"https://api.fitbit.com/1/user/-/cardioscore/date/{current_dt.strftime('%Y-%m-%d')}/{chunk_end.strftime('%Y-%m-%d')}.json", headers=headers).json()
            if "cardioScore" in cf_chunk:
                response_cardio_fitness["cardioScore"].extend(cf_chunk["cardioScore"])
        except:
            pass
        current_dt = chunk_end + timedelta(days=1)
    print(f"Cardio Fitness API Response: Fetched {len(response_cardio_fitness.get('cardioScore', []))} days of data")
    try:
        response_calories = requests.get("https://api.fitbit.com/1/user/-/activities/calories/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_calories = {}
    try:
        response_distance = requests.get("https://api.fitbit.com/1/user/-/activities/distance/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_distance = {}
    try:
        response_floors = requests.get("https://api.fitbit.com/1/user/-/activities/floors/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_floors = {}
    try:
        response_azm = requests.get("https://api.fitbit.com/1/user/-/activities/active-zone-minutes/date/"+ start_date +"/"+ end_date +".json", headers=headers).json()
    except:
        response_azm = {}
    try:
        response_activities = requests.get("https://api.fitbit.com/1/user/-/activities/list.json?afterDate="+ start_date +"&sort=asc&offset=0&limit=100", headers=headers).json()
    except:
        response_activities = {}

    # Processing data-----------------------------------------------------------------------------------------------------------------------
    days_name_list = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday','Sunday')
    report_title = "Wellness Report - " + user_profile["user"]["firstName"] + " " + user_profile["user"]["lastName"]
    report_dates_range = datetime.fromisoformat(start_date).strftime("%d %B, %Y") + " – " + datetime.fromisoformat(end_date).strftime("%d %B, %Y")
    generated_on_date = "Report Generated : " + datetime.today().date().strftime("%d %B, %Y")
    dates_list = []
    dates_str_list = []
    rhr_list = []
    steps_list = []
    weight_list = []
    spo2_list = []
    sleep_record_dict = {}
    deep_sleep_list, light_sleep_list, rem_sleep_list, awake_list, total_sleep_list, sleep_start_times_list = [],[],[],[],[],[]
    fat_burn_minutes_list, cardio_minutes_list, peak_minutes_list = [], [], []
    
    # New data lists
    hrv_list = []
    breathing_list = []
    cardio_fitness_list = []
    temperature_list = []
    calories_list = []
    distance_list = []
    floors_list = []
    azm_list = []
    eov_list = []  # Estimated Oxygen Variation for sleep apnea monitoring

    # 🚀 CACHE-FIRST: Check cache before processing API responses
    print(f"📊 Processing data for {len(temp_dates_list)} dates...")
    cached_daily_count = 0
    
    for entry in response_heartrate['activities-heart']:
        date_str = entry['dateTime']
        dates_str_list.append(date_str)
        dates_list.append(datetime.strptime(date_str, '%Y-%m-%d'))
        
        # Extract values
        try:
            fat_burn = entry["value"]["heartRateZones"][1]["minutes"]
            cardio = entry["value"]["heartRateZones"][2]["minutes"]
            peak = entry["value"]["heartRateZones"][3]["minutes"]
            fat_burn_minutes_list.append(fat_burn)
            cardio_minutes_list.append(cardio)
            peak_minutes_list.append(peak)
        except KeyError as E:
            fat_burn, cardio, peak = None, None, None
            fat_burn_minutes_list.append(None)
            cardio_minutes_list.append(None)
            peak_minutes_list.append(None)
        
        if 'restingHeartRate' in entry['value']:
            rhr = entry['value']['restingHeartRate']
            rhr_list.append(rhr)
        else:
            rhr = None
            rhr_list.append(None)
        
        # Cache daily metrics immediately
        try:
            cache.set_daily_metrics(
                date=date_str,
                resting_heart_rate=rhr,
                fat_burn_minutes=fat_burn,
                cardio_minutes=cardio,
                peak_minutes=peak
            )
            cached_daily_count += 1
        except:
            pass
    
    print(f"✅ Cached {cached_daily_count} days of heart rate data")
    
    # Process and cache steps
    steps_cached = 0
    for i, entry in enumerate(response_steps['activities-steps']):
        date_str = dates_str_list[i]
        if int(entry['value']) == 0:
            steps = None
            steps_list.append(None)
        else:
            steps = int(entry['value'])
            steps_list.append(steps)
        
        # Update cache with steps
        try:
            cache.set_daily_metrics(date=date_str, steps=steps)
            steps_cached += 1
        except:
            pass
    
    print(f"✅ Cached {steps_cached} days of steps data")

    # Process and cache weight
    weight_cached = 0
    for entry in response_weight["body-weight"]:
        date_str = entry['dateTime']
        # Convert kg to lbs (1 kg = 2.20462 lbs)
        weight_list += [None]*(dates_str_list.index(date_str)-len(weight_list))
        weight_kg = float(entry['value'])
        weight_lbs = round(weight_kg * 2.20462, 1)
        weight_list.append(weight_lbs)
        
        # Cache weight
        try:
            cache.set_daily_metrics(date=date_str, weight=weight_lbs)
            weight_cached += 1
        except:
            pass
    weight_list += [None]*(len(dates_str_list)-len(weight_list))
    print(f"✅ Cached {weight_cached} days of weight data")
    
    # Process and cache SpO2
    spo2_cached = 0
    for entry in response_spo2:
        date_str = entry["dateTime"]
        spo2_list += [None]*(dates_str_list.index(date_str)-len(spo2_list))
        eov_list += [None]*(dates_str_list.index(date_str)-len(eov_list))
        
        spo2_value = entry["value"]["avg"]
        spo2_list.append(spo2_value)
        
        # Extract EOV (Estimated Oxygen Variation) if available
        eov_value = None
        if "value" in entry and isinstance(entry["value"], dict):
            # EOV can be in different keys depending on API version
            eov_value = entry["value"].get("eov") or entry["value"].get("variationScore")
        eov_list.append(eov_value)
        
        # Cache SpO2
        try:
            cache.set_daily_metrics(date=date_str, spo2=spo2_value)
            spo2_cached += 1
        except:
            pass
    spo2_list += [None]*(len(dates_str_list)-len(spo2_list))
    eov_list += [None]*(len(dates_str_list)-len(eov_list))
    print(f"✅ Cached {spo2_cached} days of SpO2 data")
    
    # Process HRV data - only include dates in our range
    for entry in response_hrv.get("hrv", []):
        try:
            if entry["dateTime"] in dates_str_list:  # Only process if in our date range
                hrv_list += [None]*(dates_str_list.index(entry["dateTime"])-len(hrv_list))
                hrv_list.append(entry["value"]["dailyRmssd"])
        except (KeyError, ValueError):
            pass
    hrv_list += [None]*(len(dates_str_list)-len(hrv_list))
    
    # Process Breathing Rate data - only include dates in our range
    for entry in response_breathing.get("br", []):
        try:
            if entry["dateTime"] in dates_str_list:  # Only process if in our date range
                breathing_list += [None]*(dates_str_list.index(entry["dateTime"])-len(breathing_list))
                breathing_list.append(entry["value"]["breathingRate"])
        except (KeyError, ValueError):
            pass
    breathing_list += [None]*(len(dates_str_list)-len(breathing_list))
    
    # Process and cache Cardio Fitness Score data
    cardio_cached = 0
    for entry in response_cardio_fitness.get("cardioScore", []):
        try:
            date_str = entry["dateTime"]
            if date_str in dates_str_list:  # Only process if in our date range
                cardio_fitness_list += [None]*(dates_str_list.index(date_str)-len(cardio_fitness_list))
                vo2max_value = entry["value"]["vo2Max"]
                
                # Handle range values (e.g., "42-46") by taking the midpoint
                if isinstance(vo2max_value, str) and '-' in vo2max_value:
                    parts = vo2max_value.split('-')
                    if len(parts) == 2:
                        try:
                            vo2max_value = (float(parts[0]) + float(parts[1])) / 2
                        except:
                            vo2max_value = float(parts[0])  # Use first value if conversion fails
                
                vo2max_final = float(vo2max_value) if vo2max_value else None
                cardio_fitness_list.append(vo2max_final)
                
                # Cache cardio fitness
                try:
                    if vo2max_final is not None:
                        cache.set_cardio_fitness(date=date_str, vo2_max=vo2max_final)
                        cardio_cached += 1
                except:
                    pass
        except (KeyError, ValueError, TypeError):
            pass
    cardio_fitness_list += [None]*(len(dates_str_list)-len(cardio_fitness_list))
    print(f"✅ Cached {cardio_cached} days of cardio fitness data")
    
    # Process Temperature data - only include dates in our range
    for entry in response_temperature.get("tempSkin", []):
        try:
            if entry["dateTime"] in dates_str_list:  # Only process if in our date range
                temperature_list += [None]*(dates_str_list.index(entry["dateTime"])-len(temperature_list))
                # Temperature value might be nested or direct
                if isinstance(entry["value"], dict):
                    temperature_list.append(entry["value"].get("nightlyRelative", entry["value"].get("value")))
                else:
                    temperature_list.append(entry["value"])
        except (KeyError, ValueError):
            pass
    temperature_list += [None]*(len(dates_str_list)-len(temperature_list))
    
    # Process and cache Calories data
    calories_cached = 0
    for i, entry in enumerate(response_calories.get('activities-calories', [])):
        try:
            date_str = dates_str_list[i] if i < len(dates_str_list) else None
            calories_value = int(entry['value'])
            calories_list.append(calories_value)
            
            # Cache calories
            if date_str:
                try:
                    cache.set_daily_metrics(date=date_str, calories=calories_value)
                    calories_cached += 1
                except:
                    pass
        except (KeyError, ValueError):
            calories_list.append(None)
    # Ensure same length as dates
    while len(calories_list) < len(dates_str_list):
        calories_list.append(None)
    print(f"✅ Cached {calories_cached} days of calories data")
    
    # Process and cache Distance data
    distance_cached = 0
    for i, entry in enumerate(response_distance.get('activities-distance', [])):
        try:
            date_str = dates_str_list[i] if i < len(dates_str_list) else None
            # Convert km to miles (1 km = 0.621371 miles)
            distance_km = float(entry['value'])
            distance_miles = round(distance_km * 0.621371, 2)
            distance_list.append(distance_miles)
            
            # Cache distance
            if date_str:
                try:
                    cache.set_daily_metrics(date=date_str, distance=distance_miles)
                    distance_cached += 1
                except:
                    pass
        except (KeyError, ValueError):
            distance_list.append(None)
    # Ensure same length as dates
    while len(distance_list) < len(dates_str_list):
        distance_list.append(None)
    print(f"✅ Cached {distance_cached} days of distance data")
    
    # Process and cache Floors data
    floors_cached = 0
    for i, entry in enumerate(response_floors.get('activities-floors', [])):
        try:
            date_str = dates_str_list[i] if i < len(dates_str_list) else None
            floors_value = int(entry['value'])
            floors_list.append(floors_value)
            
            # Cache floors
            if date_str:
                try:
                    cache.set_daily_metrics(date=date_str, floors=floors_value)
                    floors_cached += 1
                except:
                    pass
        except (KeyError, ValueError):
            floors_list.append(None)
    # Ensure same length as dates
    while len(floors_list) < len(dates_str_list):
        floors_list.append(None)
    print(f"✅ Cached {floors_cached} days of floors data")
    
    # Process and cache Active Zone Minutes data
    azm_cached = 0
    for i, entry in enumerate(response_azm.get('activities-active-zone-minutes', [])):
        try:
            date_str = dates_str_list[i] if i < len(dates_str_list) else None
            azm_value = entry['value']['activeZoneMinutes']
            azm_list.append(azm_value)
            
            # Cache AZM
            if date_str:
                try:
                    cache.set_daily_metrics(date=date_str, active_zone_minutes=azm_value)
                    azm_cached += 1
                except:
                    pass
        except (KeyError, ValueError):
            azm_list.append(None)
    # Ensure same length as dates
    while len(azm_list) < len(dates_str_list):
        azm_list.append(None)
    print(f"✅ Cached {azm_cached} days of AZM data")

    # 🚀 USE CACHE FOR SLEEP DATA - Only fetch missing dates!
    print(f"📊 Fetching sleep data for {len(dates_str_list)} dates...")
    
    # First, check which dates are in cache
    cached_count = 0
    missing_dates = []
    for date_str in dates_str_list:
        cached_data = cache.get_sleep_data(date_str)
        if cached_data:
            # Use cached data!
            cached_count += 1
            try:
                # Parse start_time to calculate sleep_time_of_day
                sleep_start_time = datetime.strptime(cached_data['start_time'], "%Y-%m-%dT%H:%M:%S.%f")
                if sleep_start_time.hour < 12:
                    sleep_start_time = sleep_start_time + timedelta(hours=12)
                else:
                    sleep_start_time = sleep_start_time + timedelta(hours=-12)
                sleep_time_of_day = sleep_start_time.time()
                
                # DEBUG: Log what we're pulling from cache
                print(f"📊 Using CACHED sleep score for {date_str}: {cached_data['sleep_score']} (efficiency: {cached_data.get('efficiency', 'N/A')})")
                
                sleep_record_dict[date_str] = {
                    'deep': cached_data['deep'],
                    'light': cached_data['light'],
                    'rem': cached_data['rem'],
                    'wake': cached_data['wake'],
                    'total_sleep': cached_data['total_sleep'],
                    'start_time_seconds': (sleep_time_of_day.hour * 3600) + (sleep_time_of_day.minute * 60) + sleep_time_of_day.second,
                    'sleep_score': cached_data['sleep_score']
                }
                
                # Store in global dict for drill-down
                sleep_detail_data_store[date_str] = {
                    'deep': cached_data['deep'],
                    'light': cached_data['light'],
                    'rem': cached_data['rem'],
                    'wake': cached_data['wake'],
                    'total_sleep': cached_data['total_sleep'],
                    'start_time': cached_data['start_time'],
                    'sleep_score': cached_data['sleep_score'],
                    'efficiency': cached_data['efficiency']
                }
            except Exception as e:
                print(f"⚠️ Error processing cached data for {date_str}: {e}")
        else:
            # Need to fetch this date
            missing_dates.append(date_str)
    
    print(f"✅ Loaded {cached_count} dates from cache")
    print(f"🔄 Need to fetch {len(missing_dates)} dates from API")
    
    # Fetch missing dates from API (in batches of 30 to avoid rate limits)
    if missing_dates:
        for i in range(0, len(missing_dates), 30):
            batch = missing_dates[i:i+30]
            print(f"📥 Fetching sleep batch {i//30 + 1} ({len(batch)} dates)...")
            
            # Use the populate_sleep_score_cache function which handles caching
            fetched_count = populate_sleep_score_cache(batch, headers, force_refresh=False)
            
            # Now load the newly cached data into our dict
            for date_str in batch:
                cached_data = cache.get_sleep_data(date_str)
                if cached_data:
                    try:
                        sleep_start_time = datetime.strptime(cached_data['start_time'], "%Y-%m-%dT%H:%M:%S.%f")
                        if sleep_start_time.hour < 12:
                            sleep_start_time = sleep_start_time + timedelta(hours=12)
                        else:
                            sleep_start_time = sleep_start_time + timedelta(hours=-12)
                        sleep_time_of_day = sleep_start_time.time()
                        
                        sleep_record_dict[date_str] = {
                            'deep': cached_data['deep'],
                            'light': cached_data['light'],
                            'rem': cached_data['rem'],
                            'wake': cached_data['wake'],
                            'total_sleep': cached_data['total_sleep'],
                            'start_time_seconds': (sleep_time_of_day.hour * 3600) + (sleep_time_of_day.minute * 60) + sleep_time_of_day.second,
                            'sleep_score': cached_data['sleep_score']
                        }
                        
                        sleep_detail_data_store[date_str] = {
                            'deep': cached_data['deep'],
                            'light': cached_data['light'],
                            'rem': cached_data['rem'],
                            'wake': cached_data['wake'],
                            'total_sleep': cached_data['total_sleep'],
                            'start_time': cached_data['start_time'],
                            'sleep_score': cached_data['sleep_score'],
                            'efficiency': cached_data['efficiency']
                        }
                    except Exception as e:
                        print(f"⚠️ Error processing fetched data for {date_str}: {e}")

    for day in dates_str_list:
        if day in sleep_record_dict:
            deep_sleep_list.append(sleep_record_dict[day]['deep'])
            light_sleep_list.append(sleep_record_dict[day]['light'])
            rem_sleep_list.append(sleep_record_dict[day]['rem'])
            awake_list.append(sleep_record_dict[day]['wake'])
            total_sleep_list.append(sleep_record_dict[day]['total_sleep'])
            sleep_start_times_list.append(sleep_record_dict[day]['start_time_seconds'])
        else:
            deep_sleep_list.append(None)
            light_sleep_list.append(None)
            rem_sleep_list.append(None)
            awake_list.append(None)
            total_sleep_list.append(None)
            sleep_start_times_list.append(None)

    # Final safety check: Ensure all arrays are the same length as dates_list
    expected_length = len(dates_list)
    arrays_to_check = {
        'dates_str_list': dates_str_list,
        'rhr_list': rhr_list,
        'steps_list': steps_list,
        'weight_list': weight_list,
        'spo2_list': spo2_list,
        'deep_sleep_list': deep_sleep_list,
        'light_sleep_list': light_sleep_list,
        'rem_sleep_list': rem_sleep_list,
        'awake_list': awake_list,
        'total_sleep_list': total_sleep_list,
        'sleep_start_times_list': sleep_start_times_list,
        'fat_burn_minutes_list': fat_burn_minutes_list,
        'cardio_minutes_list': cardio_minutes_list,
        'peak_minutes_list': peak_minutes_list,
        'hrv_list': hrv_list,
        'breathing_list': breathing_list,
        'cardio_fitness_list': cardio_fitness_list,
        'temperature_list': temperature_list,
        'calories_list': calories_list,
        'distance_list': distance_list,
        'floors_list': floors_list,
        'azm_list': azm_list
    }
    
    for name, arr in arrays_to_check.items():
        if len(arr) != expected_length:
            if len(arr) > expected_length:
                # Array too long - truncate
                print(f"⚠️ Array length mismatch: {name} has {len(arr)} items, expected {expected_length}. Truncating...")
                del arr[expected_length:]
            else:
                # Array too short - pad
                print(f"⚠️ Array length mismatch: {name} has {len(arr)} items, expected {expected_length}. Padding...")
                while len(arr) < expected_length:
                    arr.append(None)

    df_merged = pd.DataFrame({
    "Date": dates_list,
    "Resting Heart Rate": rhr_list,
    "Steps Count": steps_list,
    "Fat Burn Minutes": fat_burn_minutes_list,
    "Cardio Minutes": cardio_minutes_list,
    "Peak Minutes": peak_minutes_list,
    "weight": weight_list,
    "SPO2": spo2_list,
    "EOV": eov_list,
    "Deep Sleep Minutes": deep_sleep_list,
    "Light Sleep Minutes": light_sleep_list,
    "REM Sleep Minutes": rem_sleep_list,
    "Awake Minutes": awake_list,
    "Total Sleep Minutes": total_sleep_list,
    "Sleep Start Time Seconds": sleep_start_times_list,
    "HRV": hrv_list,
    "Breathing Rate": breathing_list,
    "Cardio Fitness Score": cardio_fitness_list,
    "Temperature": temperature_list,
    "Calories": calories_list,
    "Distance": distance_list,
    "Floors": floors_list,
    "Active Zone Minutes": azm_list
    })
    
    df_merged['Total Sleep Seconds'] = df_merged['Total Sleep Minutes']*60
    df_merged["Sleep End Time Seconds"] = df_merged["Sleep Start Time Seconds"] + df_merged['Total Sleep Seconds']
    # Helper function to safely handle NaN values
    def safe_avg(value, decimals=1, as_int=False):
        """Convert value to number, handling NaN. Returns 0 if NaN."""
        if pd.isna(value) or value is None or (isinstance(value, float) and np.isnan(value)):
            return 0
        if as_int:
            return int(value)
        return round(value, decimals)
    
    df_merged["Total Active Minutes"] = df_merged["Fat Burn Minutes"] + df_merged["Cardio Minutes"] + df_merged["Peak Minutes"]
    rhr_avg = {'overall': safe_avg(df_merged["Resting Heart Rate"].mean(),1), '30d': safe_avg(df_merged["Resting Heart Rate"].tail(30).mean(),1)}
    steps_avg = {'overall': safe_avg(df_merged["Steps Count"].mean(), as_int=True), '30d': safe_avg(df_merged["Steps Count"].tail(31).mean(), as_int=True)}
    weight_avg = {'overall': safe_avg(df_merged["weight"].mean(),1), '30d': safe_avg(df_merged["weight"].tail(30).mean(),1)}
    spo2_avg = {'overall': safe_avg(df_merged["SPO2"].mean(),1), '30d': safe_avg(df_merged["SPO2"].tail(30).mean(),1)}
    sleep_avg = {'overall': safe_avg(df_merged["Total Sleep Minutes"].mean(),1), '30d': safe_avg(df_merged["Total Sleep Minutes"].tail(30).mean(),1)}
    active_mins_avg = {'overall': safe_avg(df_merged["Total Active Minutes"].mean(),2), '30d': safe_avg(df_merged["Total Active Minutes"].tail(30).mean(),2)}
    weekly_steps_array = np.array([0]*days_name_list.index(datetime.fromisoformat(start_date).strftime('%A')) + df_merged["Steps Count"].to_list() + [0]*(6 - days_name_list.index(datetime.fromisoformat(end_date).strftime('%A'))))
    weekly_steps_array = np.transpose(weekly_steps_array.reshape((int(len(weekly_steps_array)/7), 7)))
    weekly_steps_array = pd.DataFrame(weekly_steps_array, index=days_name_list)

    # Plotting data-----------------------------------------------------------------------------------------------------------------------

    fig_rhr = px.line(df_merged, x="Date", y="Resting Heart Rate", line_shape="spline", color_discrete_sequence=["#d30f1c"], title=f"<b>Daily Resting Heart Rate<br><br><sup>Overall average : {rhr_avg['overall']} bpm | Last 30d average : {rhr_avg['30d']} bpm</sup></b><br><br><br>")
    if df_merged["Resting Heart Rate"].dtype != object:
        fig_rhr.add_annotation(x=df_merged.iloc[df_merged["Resting Heart Rate"].idxmax()]["Date"], y=df_merged["Resting Heart Rate"].max(), text=str(df_merged["Resting Heart Rate"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_rhr.add_annotation(x=df_merged.iloc[df_merged["Resting Heart Rate"].idxmin()]["Date"], y=df_merged["Resting Heart Rate"].min(), text=str(df_merged["Resting Heart Rate"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_rhr.add_hline(y=df_merged["Resting Heart Rate"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Resting Heart Rate"].mean(), 1)) + " BPM", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_rhr.add_hrect(y0=62, y1=68, fillcolor="green", opacity=0.15, line_width=0)
    rhr_summary_df = calculate_table_data(df_merged, "Resting Heart Rate")
    rhr_summary_table = dash_table.DataTable(rhr_summary_df.to_dict('records'), [{"name": i, "id": i} for i in rhr_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#5f040a','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_steps = px.bar(df_merged, x="Date", y="Steps Count", color_discrete_sequence=["#2fb376"], title=f"<b>Daily Steps Count<br><br><sup>Overall average : {steps_avg['overall']} steps | Last 30d average : {steps_avg['30d']} steps</sup></b><br><br><br>")
    if df_merged["Steps Count"].dtype != object:
        fig_steps.add_annotation(x=df_merged.iloc[df_merged["Steps Count"].idxmax()]["Date"], y=df_merged["Steps Count"].max(), text=str(df_merged["Steps Count"].max())+" steps", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_steps.add_annotation(x=df_merged.iloc[df_merged["Steps Count"].idxmin()]["Date"], y=df_merged["Steps Count"].min(), text=str(df_merged["Steps Count"].min())+" steps", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_steps.add_hline(y=df_merged["Steps Count"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Steps Count"].mean(), 1)) + " Steps", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_steps_heatmap = px.imshow(weekly_steps_array, color_continuous_scale='YLGn', origin='lower', title="<b>Weekly Steps Heatmap</b>", labels={'x':"Week Number", 'y': "Day of the Week"}, height=350, aspect='equal')
    fig_steps_heatmap.update_traces(colorbar_orientation='h', selector=dict(type='heatmap'))
    steps_summary_df = calculate_table_data(df_merged, "Steps Count")
    steps_summary_table = dash_table.DataTable(steps_summary_df.to_dict('records'), [{"name": i, "id": i} for i in steps_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#072f1c','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_activity_minutes = px.bar(df_merged, x="Date", y=["Fat Burn Minutes", "Cardio Minutes", "Peak Minutes"], title=f"<b>Activity Minutes<br><br><sup>Overall total active minutes average : {active_mins_avg['overall']} minutes | Last 30d total active minutes average : {active_mins_avg['30d']} minutes</sup></b><br><br><br>")
    fig_activity_minutes.update_layout(yaxis_title='Active Minutes', legend=dict(orientation="h",yanchor="bottom", y=1.02, xanchor="right", x=1, title_text=''))
    fat_burn_summary_df = calculate_table_data(df_merged, "Fat Burn Minutes")
    fat_burn_summary_table = dash_table.DataTable(fat_burn_summary_df.to_dict('records'), [{"name": i, "id": i} for i in fat_burn_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#636efa','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    cardio_summary_df = calculate_table_data(df_merged, "Cardio Minutes")
    cardio_summary_table = dash_table.DataTable(cardio_summary_df.to_dict('records'), [{"name": i, "id": i} for i in cardio_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#ef553b','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    peak_summary_df = calculate_table_data(df_merged, "Peak Minutes")
    peak_summary_table = dash_table.DataTable(peak_summary_df.to_dict('records'), [{"name": i, "id": i} for i in peak_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#00cc96','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_weight = px.line(df_merged, x="Date", y="weight", line_shape="spline", color_discrete_sequence=["#6b3908"], title=f"<b>Weight<br><br><sup>Overall average : {weight_avg['overall']} lbs | Last 30d average : {weight_avg['30d']} lbs</sup></b><br><br><br>", labels={"weight": "Weight (lbs)"})
    if df_merged["weight"].dtype != object:
        fig_weight.add_annotation(x=df_merged.iloc[df_merged["weight"].idxmax()]["Date"], y=df_merged["weight"].max(), text=str(df_merged["weight"].max()) + " lbs", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_weight.add_annotation(x=df_merged.iloc[df_merged["weight"].idxmin()]["Date"], y=df_merged["weight"].min(), text=str(df_merged["weight"].min()) + " lbs", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_weight.add_hline(y=round(df_merged["weight"].mean(),1), line_dash="dot",annotation_text="Average : " + str(round(df_merged["weight"].mean(), 1)) + " lbs", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    weight_summary_df = calculate_table_data(df_merged, "weight")
    weight_summary_table = dash_table.DataTable(weight_summary_df.to_dict('records'), [{"name": i, "id": i} for i in weight_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#4c3b7d','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_spo2 = px.scatter(df_merged, x="Date", y="SPO2", color_discrete_sequence=["#983faa"], title=f"<b>SPO2 Percentage<br><br><sup>Overall average : {spo2_avg['overall']}% | Last 30d average : {spo2_avg['30d']}% </sup></b><br><br><br>", range_y=(90,100), labels={'SPO2':"SpO2(%)"})
    if df_merged["SPO2"].dtype != object:
        fig_spo2.add_annotation(x=df_merged.iloc[df_merged["SPO2"].idxmax()]["Date"], y=df_merged["SPO2"].max(), text=str(df_merged["SPO2"].max())+"%", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_spo2.add_annotation(x=df_merged.iloc[df_merged["SPO2"].idxmin()]["Date"], y=df_merged["SPO2"].min(), text=str(df_merged["SPO2"].min())+"%", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_spo2.add_hline(y=df_merged["SPO2"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["SPO2"].mean(), 1)) + "%", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_spo2.update_traces(marker_size=6)
    spo2_summary_df = calculate_table_data(df_merged, "SPO2")
    spo2_summary_table = dash_table.DataTable(spo2_summary_df.to_dict('records'), [{"name": i, "id": i} for i in spo2_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#8d3a18','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # EOV (Estimated Oxygen Variation) Chart
    eov_avg = {'overall': safe_avg(df_merged["EOV"].mean(), 1), '30d': safe_avg(df_merged["EOV"].tail(30).mean(), 1)}
    if df_merged["EOV"].notna().any() and df_merged["EOV"].sum() > 0:
        fig_eov = px.line(df_merged, x="Date", y="EOV", line_shape="spline", color_discrete_sequence=["#e74c3c"], 
                          title=f"<b>Oxygen Variation (EOV) - Sleep Apnea Indicator<br><br><sup>Overall average : {eov_avg['overall']} | Last 30d average : {eov_avg['30d']}</sup></b><br><br><br>", 
                          labels={"EOV": "EOV Score"})
        if df_merged["EOV"].dtype != object and df_merged["EOV"].notna().any():
            fig_eov.add_annotation(x=df_merged[df_merged["EOV"].notna()].iloc[df_merged[df_merged["EOV"].notna()]["EOV"].idxmax()]["Date"], 
                                  y=df_merged["EOV"].max(), text=str(round(df_merged["EOV"].max(), 1)), 
                                  showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, 
                                  font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
            fig_eov.add_annotation(x=df_merged[df_merged["EOV"].notna()].iloc[df_merged[df_merged["EOV"].notna()]["EOV"].idxmin()]["Date"], 
                                  y=df_merged["EOV"].min(), text=str(round(df_merged["EOV"].min(), 1)), 
                                  showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, 
                                  font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
            fig_eov.add_hline(y=df_merged["EOV"].mean(), line_dash="dot",
                            annotation_text="Average : " + str(safe_avg(df_merged["EOV"].mean(), 1)), 
                            annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, 
                            annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
        eov_summary_df = calculate_table_data(df_merged, "EOV")
        eov_summary_table = dash_table.DataTable(eov_summary_df.to_dict('records'), [{"name": i, "id": i} for i in eov_summary_df.columns], 
                                                 style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], 
                                                 style_header={'backgroundColor': '#c0392b','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, 
                                                 style_cell={'textAlign': 'center'})
    else:
        fig_eov = px.line(title="Oxygen Variation (EOV) - No Data Available")
        eov_summary_table = html.P("No EOV data available for this period", style={'text-align': 'center', 'color': '#999'})
    
    fig_sleep_minutes = px.bar(df_merged, x="Date", y=["Deep Sleep Minutes", "Light Sleep Minutes", "REM Sleep Minutes", "Awake Minutes"], title=f"<b>Sleep Stages<br><br><sup>Overall average : {format_minutes(sleep_avg['overall'])} | Last 30d average : {format_minutes(sleep_avg['30d'])}</sup></b><br><br>", color_discrete_map={"Deep Sleep Minutes": '#084466', "Light Sleep Minutes": '#1e9ad6', "REM Sleep Minutes": '#4cc5da', "Awake Minutes": '#fd7676',}, height=500)
    fig_sleep_minutes.update_layout(yaxis_title='Sleep Minutes', legend=dict(orientation="h",yanchor="bottom", y=1.02, xanchor="right", x=1, title_text=''), yaxis=dict(tickvals=[1,120,240,360,480,600,720], ticktext=[f"{m // 60}h" for m in [1,120,240,360,480,600,720]], title="Sleep Time (hours)"))
    # Fix tooltip to show "Xh Ym" format instead of large numbers
    fig_sleep_minutes.update_traces(hovertemplate='<b>%{x}</b><br>%{fullData.name}: %{customdata}<extra></extra>')
    for trace in fig_sleep_minutes.data:
        stage_column = trace.name
        formatted_values = df_merged[stage_column].apply(lambda x: format_minutes(int(x)) if pd.notna(x) else "N/A")
        trace.customdata = formatted_values
    if df_merged["Total Sleep Minutes"].dtype != object and df_merged["Total Sleep Minutes"].notna().any():
        fig_sleep_minutes.add_annotation(x=df_merged.iloc[df_merged["Total Sleep Minutes"].idxmax()]["Date"], y=df_merged["Total Sleep Minutes"].max(), text=str(format_minutes(df_merged["Total Sleep Minutes"].max())), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_sleep_minutes.add_annotation(x=df_merged.iloc[df_merged["Total Sleep Minutes"].idxmin()]["Date"], y=df_merged["Total Sleep Minutes"].min(), text=str(format_minutes(df_merged["Total Sleep Minutes"].min())), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_sleep_minutes.add_hline(y=df_merged["Total Sleep Minutes"].mean(), line_dash="dot",annotation_text="Average : " + str(format_minutes(safe_avg(df_merged["Total Sleep Minutes"].mean()))), annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    # Set range slider - handle short date ranges
    range_start = dates_str_list[max(-30, -len(dates_str_list))]
    fig_sleep_minutes.update_xaxes(rangeslider_visible=True,range=[range_start, dates_str_list[-1]],rangeslider_range=[dates_str_list[0], dates_str_list[-1]])
    sleep_summary_df = calculate_table_data(df_merged, "Total Sleep Minutes")
    sleep_summary_table = dash_table.DataTable(sleep_summary_df.to_dict('records'), [{"name": i, "id": i} for i in sleep_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#636efa','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_sleep_regularity = px.bar(df_merged, x="Date", y="Total Sleep Seconds", base="Sleep Start Time Seconds", title="<b>Sleep Regularity<br><br><sup>The chart time here is always in local time ( Independent of timezone changes )</sup></b>", labels={"Total Sleep Seconds":"Time of Day ( HH:MM )"})
    fig_sleep_regularity.update_layout(yaxis = dict(tickmode = 'array',tickvals = list(range(0, 120000, 10000)),ticktext = list(map(seconds_to_tick_label, list(range(0, 120000, 10000))))))
    # Fix tooltip to show time in HH:MM format instead of numbers
    sleep_start_formatted = df_merged["Sleep Start Time Seconds"].apply(lambda x: seconds_to_tick_label(int(x)) if pd.notna(x) else "N/A")
    sleep_end_formatted = df_merged["Sleep End Time Seconds"].apply(lambda x: seconds_to_tick_label(int(x)) if pd.notna(x) else "N/A")
    sleep_duration_formatted = df_merged["Total Sleep Minutes"].apply(lambda x: format_minutes(int(x)) if pd.notna(x) else "N/A")
    fig_sleep_regularity.update_traces(
        hovertemplate='<b>%{x}</b><br>Sleep Start: %{customdata[0]}<br>Sleep End: %{customdata[1]}<br>Duration: %{customdata[2]}<extra></extra>', 
        customdata=list(zip(sleep_start_formatted, sleep_end_formatted, sleep_duration_formatted))
    )
    if df_merged["Sleep Start Time Seconds"].notna().any():
        fig_sleep_regularity.add_hline(y=df_merged["Sleep Start Time Seconds"].mean(), line_dash="dot",annotation_text="Sleep Start Time Trend : "+ str(seconds_to_tick_label(safe_avg(df_merged["Sleep Start Time Seconds"].mean(), as_int=True))), annotation_position="bottom right", annotation_bgcolor="#0a3024", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
        fig_sleep_regularity.add_hline(y=df_merged["Sleep End Time Seconds"].mean(), line_dash="dot",annotation_text="Sleep End Time Trend : " + str(seconds_to_tick_label(safe_avg(df_merged["Sleep End Time Seconds"].mean(), as_int=True))), annotation_position="top left", annotation_bgcolor="#5e060d", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    
    # New visualizations
    # HRV
    hrv_avg = {'overall': safe_avg(df_merged["HRV"].mean(),1), '30d': safe_avg(df_merged["HRV"].tail(30).mean(),1)}
    fig_hrv = px.line(df_merged, x="Date", y="HRV", line_shape="spline", color_discrete_sequence=["#ff6692"], title=f"<b>Heart Rate Variability (HRV)<br><br><sup>Overall average : {hrv_avg['overall']} ms | Last 30d average : {hrv_avg['30d']} ms</sup></b><br><br><br>", labels={"HRV": "HRV (ms)"})
    if df_merged["HRV"].dtype != object and df_merged["HRV"].notna().any():
        fig_hrv.add_annotation(x=df_merged.iloc[df_merged["HRV"].idxmax()]["Date"], y=df_merged["HRV"].max(), text=str(df_merged["HRV"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_hrv.add_annotation(x=df_merged.iloc[df_merged["HRV"].idxmin()]["Date"], y=df_merged["HRV"].min(), text=str(df_merged["HRV"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_hrv.add_hline(y=df_merged["HRV"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["HRV"].mean(), 1)) + " ms", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    hrv_summary_df = calculate_table_data(df_merged, "HRV")
    hrv_summary_table = dash_table.DataTable(hrv_summary_df.to_dict('records'), [{"name": i, "id": i} for i in hrv_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#a8326b','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Breathing Rate
    breathing_avg = {'overall': safe_avg(df_merged["Breathing Rate"].mean(),1), '30d': safe_avg(df_merged["Breathing Rate"].tail(30).mean(),1)}
    fig_breathing = px.line(df_merged, x="Date", y="Breathing Rate", line_shape="spline", color_discrete_sequence=["#00d4ff"], title=f"<b>Breathing Rate<br><br><sup>Overall average : {breathing_avg['overall']} bpm | Last 30d average : {breathing_avg['30d']} bpm</sup></b><br><br><br>", labels={"Breathing Rate": "Breaths per Minute"})
    if df_merged["Breathing Rate"].dtype != object and df_merged["Breathing Rate"].notna().any():
        fig_breathing.add_annotation(x=df_merged.iloc[df_merged["Breathing Rate"].idxmax()]["Date"], y=df_merged["Breathing Rate"].max(), text=str(df_merged["Breathing Rate"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_breathing.add_annotation(x=df_merged.iloc[df_merged["Breathing Rate"].idxmin()]["Date"], y=df_merged["Breathing Rate"].min(), text=str(df_merged["Breathing Rate"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_breathing.add_hline(y=df_merged["Breathing Rate"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["Breathing Rate"].mean(), 1)) + " bpm", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    breathing_summary_df = calculate_table_data(df_merged, "Breathing Rate")
    breathing_summary_table = dash_table.DataTable(breathing_summary_df.to_dict('records'), [{"name": i, "id": i} for i in breathing_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#007a8c','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Cardio Fitness Score with error handling
    try:
        # Convert to numeric, coercing errors to NaN
        df_merged["Cardio Fitness Score"] = pd.to_numeric(df_merged["Cardio Fitness Score"], errors='coerce')
        cardio_fitness_avg = {'overall': safe_avg(df_merged["Cardio Fitness Score"].mean(),1), '30d': safe_avg(df_merged["Cardio Fitness Score"].tail(30).mean(),1)}
        fig_cardio_fitness = px.line(df_merged, x="Date", y="Cardio Fitness Score", line_shape="spline", color_discrete_sequence=["#ff9500"], title=f"<b>Cardio Fitness Score (VO2 Max)<br><br><sup>Overall average : {cardio_fitness_avg['overall']} | Last 30d average : {cardio_fitness_avg['30d']}</sup></b><br><br><br>")
        if df_merged["Cardio Fitness Score"].notna().any():
            fig_cardio_fitness.add_annotation(x=df_merged.iloc[df_merged["Cardio Fitness Score"].idxmax()]["Date"], y=df_merged["Cardio Fitness Score"].max(), text=str(round(df_merged["Cardio Fitness Score"].max(), 1)), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
            fig_cardio_fitness.add_annotation(x=df_merged.iloc[df_merged["Cardio Fitness Score"].idxmin()]["Date"], y=df_merged["Cardio Fitness Score"].min(), text=str(round(df_merged["Cardio Fitness Score"].min(), 1)), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
            fig_cardio_fitness.add_hline(y=df_merged["Cardio Fitness Score"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Cardio Fitness Score"].mean(), 1)), annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
        cardio_fitness_summary_df = calculate_table_data(df_merged, "Cardio Fitness Score")
        cardio_fitness_summary_table = dash_table.DataTable(cardio_fitness_summary_df.to_dict('records'), [{"name": i, "id": i} for i in cardio_fitness_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#995500','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    except Exception as e:
        print(f"Error processing Cardio Fitness Score: {e}")
        fig_cardio_fitness = px.line(title="Cardio Fitness Score (No Data)")
        cardio_fitness_summary_table = html.P("No cardio fitness data available", style={'text-align': 'center', 'color': '#888'})
    
    # Temperature
    temperature_avg = {'overall': safe_avg(df_merged["Temperature"].mean(),2), '30d': safe_avg(df_merged["Temperature"].tail(30).mean(),2)}
    fig_temperature = px.line(df_merged, x="Date", y="Temperature", line_shape="spline", color_discrete_sequence=["#ff5733"], title=f"<b>Temperature Variation<br><br><sup>Overall average : {temperature_avg['overall']}°F | Last 30d average : {temperature_avg['30d']}°F</sup></b><br><br><br>")
    if df_merged["Temperature"].dtype != object and df_merged["Temperature"].notna().any():
        fig_temperature.add_annotation(x=df_merged.iloc[df_merged["Temperature"].idxmax()]["Date"], y=df_merged["Temperature"].max(), text=str(df_merged["Temperature"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_temperature.add_annotation(x=df_merged.iloc[df_merged["Temperature"].idxmin()]["Date"], y=df_merged["Temperature"].min(), text=str(df_merged["Temperature"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
        fig_temperature.add_hline(y=df_merged["Temperature"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["Temperature"].mean(), 2)) + "°F", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    temperature_summary_df = calculate_table_data(df_merged, "Temperature")
    temperature_summary_table = dash_table.DataTable(temperature_summary_df.to_dict('records'), [{"name": i, "id": i} for i in temperature_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#992211','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Active Zone Minutes
    azm_avg = {'overall': safe_avg(df_merged["Active Zone Minutes"].mean(),1), '30d': safe_avg(df_merged["Active Zone Minutes"].tail(30).mean(),1)}
    fig_azm = px.bar(df_merged, x="Date", y="Active Zone Minutes", color_discrete_sequence=["#ffcc00"], title=f"<b>Active Zone Minutes<br><br><sup>Overall average : {azm_avg['overall']} minutes | Last 30d average : {azm_avg['30d']} minutes</sup></b><br><br><br>")
    if df_merged["Active Zone Minutes"].dtype != object and df_merged["Active Zone Minutes"].notna().any():
        fig_azm.add_hline(y=df_merged["Active Zone Minutes"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["Active Zone Minutes"].mean(), 1)) + " minutes", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    azm_summary_df = calculate_table_data(df_merged, "Active Zone Minutes")
    azm_summary_table = dash_table.DataTable(azm_summary_df.to_dict('records'), [{"name": i, "id": i} for i in azm_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#997700','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Calories and Distance
    calories_avg = {'overall': safe_avg(df_merged["Calories"].mean(), as_int=True), '30d': safe_avg(df_merged["Calories"].tail(30).mean(), as_int=True)}
    fig_calories = px.bar(df_merged, x="Date", y="Calories", color_discrete_sequence=["#ff3366"], title=f"<b>Daily Calories Burned<br><br><sup>Overall average : {calories_avg['overall']} cal | Last 30d average : {calories_avg['30d']} cal</sup></b><br><br><br>")
    if df_merged["Calories"].dtype != object and df_merged["Calories"].notna().any():
        fig_calories.add_hline(y=df_merged["Calories"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["Calories"].mean(), as_int=True)) + " cal", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    
    distance_avg = {'overall': safe_avg(df_merged["Distance"].mean(),2), '30d': safe_avg(df_merged["Distance"].tail(30).mean(),2)}
    fig_distance = px.bar(df_merged, x="Date", y="Distance", color_discrete_sequence=["#33ccff"], title=f"<b>Daily Distance<br><br><sup>Overall average : {distance_avg['overall']} miles | Last 30d average : {distance_avg['30d']} miles</sup></b><br><br><br>", labels={"Distance": "Distance (miles)"})
    if df_merged["Distance"].dtype != object and df_merged["Distance"].notna().any():
        fig_distance.add_hline(y=df_merged["Distance"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["Distance"].mean(), 2)) + " miles", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    
    calories_summary_df = calculate_table_data(df_merged, "Calories")
    calories_summary_table = dash_table.DataTable(calories_summary_df.to_dict('records'), [{"name": i, "id": i} for i in calories_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#991133','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Floors
    floors_avg = {'overall': safe_avg(df_merged["Floors"].mean(), as_int=True), '30d': safe_avg(df_merged["Floors"].tail(30).mean(), as_int=True)}
    fig_floors = px.bar(df_merged, x="Date", y="Floors", color_discrete_sequence=["#9966ff"], title=f"<b>Daily Floors Climbed<br><br><sup>Overall average : {floors_avg['overall']} floors | Last 30d average : {floors_avg['30d']} floors</sup></b><br><br><br>")
    if df_merged["Floors"].dtype != object and df_merged["Floors"].notna().any():
        fig_floors.add_hline(y=df_merged["Floors"].mean(), line_dash="dot",annotation_text="Average : " + str(safe_avg(df_merged["Floors"].mean(), as_int=True)) + " floors", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.8, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    floors_summary_df = calculate_table_data(df_merged, "Floors")
    floors_summary_table = dash_table.DataTable(floors_summary_df.to_dict('records'), [{"name": i, "id": i} for i in floors_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#663399','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Exercise Log with Enhanced Data - with caching
    exercise_data = []
    activity_types = set(['All'])
    workout_dates_for_dropdown = []  # For drill-down selector
    activities_by_date = {}  # Store activities by date for drill-down
    activities_cached = 0
    
    for activity in response_activities.get('activities', []):
        try:
            activity_date = datetime.strptime(activity['startTime'][:10], '%Y-%m-%d').strftime("%Y-%m-%d")
            if activity_date >= start_date and activity_date <= end_date:
                activity_name = activity.get('activityName', 'N/A')
                activity_types.add(activity_name)
                exercise_data.append({
                    'Date': activity_date,
                    'Activity': activity_name,
                    'Duration (min)': activity.get('duration', 0) // 60000,
                    'Calories': activity.get('calories', 0),
                    'Avg HR': activity.get('averageHeartRate', 'N/A'),
                    'Steps': activity.get('steps', 'N/A'),
                    'Distance (mi)': round(activity.get('distance', 0) * 0.621371, 2) if activity.get('distance') else 'N/A'
                })
                
                # Store for drill-down
                if activity_date not in activities_by_date:
                    activities_by_date[activity_date] = []
                    workout_dates_for_dropdown.append({'label': f"{activity_date} - {activity_name}", 'value': activity_date})
                activities_by_date[activity_date].append(activity)
                
                # Store in global dict for callback access
                if activity_date not in exercise_data_store:
                    exercise_data_store[activity_date] = []
                exercise_data_store[activity_date].append(activity)
                
                # Cache activity
                try:
                    activity_id = str(activity.get('logId', f"{activity_date}_{activity_name}"))
                    cache.set_activity(
                        activity_id=activity_id,
                        date=activity_date,
                        activity_name=activity_name,
                        duration_ms=activity.get('duration'),
                        calories=activity.get('calories'),
                        avg_heart_rate=activity.get('averageHeartRate'),
                        steps=activity.get('steps'),
                        distance=activity.get('distance'),
                        activity_data_json=str(activity)
                    )
                    activities_cached += 1
                except:
                    pass
        except:
            pass
    
    print(f"✅ Cached {activities_cached} activities")
    
    # Exercise type filter options
    exercise_filter_options = [{'label': activity_type, 'value': activity_type} for activity_type in sorted(activity_types)]
    
    if exercise_data:
        exercise_df = pd.DataFrame(exercise_data)
        exercise_log_table = dash_table.DataTable(
            exercise_df.to_dict('records'), 
            [{"name": i, "id": i} for i in exercise_df.columns], 
            style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], 
            style_header={'backgroundColor': '#336699','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, 
            style_cell={'textAlign': 'center'},
            page_size=20
        )
    else:
        exercise_df = pd.DataFrame()
        exercise_log_table = html.P("No exercise activities logged in this period.", style={'text-align': 'center', 'color': '#888'})
    
    # Phase 3B: Sleep Quality Analysis - Use cached Fitbit sleep scores
    print("🗄️ Checking cache for sleep scores...")
    
    # Always refresh today's data (most recent)
    today = datetime.now().strftime('%Y-%m-%d')
    if today in dates_str_list:
        print(f"🔄 Refreshing today's data ({today})...")
        populate_sleep_score_cache([today], headers, force_refresh=True)
    
    # Check which dates are missing from cache
    missing_dates = cache.get_missing_dates(start_date, end_date, metric_type='sleep')
    
    # Remove today from missing dates since we already refreshed it
    missing_dates = [d for d in missing_dates if d != today]
    
    if missing_dates:
        # Limit to 30 dates at a time to avoid rate limits
        dates_to_fetch = missing_dates[:30]  # Start with last 30 days
        print(f"📥 Fetching {len(dates_to_fetch)} missing sleep scores from API...")
        fetched = populate_sleep_score_cache(dates_to_fetch, headers)
        print(f"✅ Successfully cached {fetched} new sleep scores")
        
        if len(missing_dates) > 30:
            print(f"ℹ️ {len(missing_dates) - 30} older dates will be fetched in future reports")
    else:
        print("✅ All historical sleep scores already cached!")
    
    # Now build sleep scores from cache
    sleep_scores = []
    sleep_stages_totals = {'Deep': 0, 'Light': 0, 'REM': 0, 'Wake': 0}
    sleep_dates_for_dropdown = []  # For drill-down selector
    
    for date_str in dates_str_list:
        # Try cache first
        cached_sleep = cache.get_sleep_data(date_str)
        if cached_sleep and cached_sleep['sleep_score'] is not None:
            print(f"📊 Using CACHED sleep score for {date_str}: {cached_sleep['sleep_score']}")
            sleep_scores.append({'Date': date_str, 'Score': cached_sleep['sleep_score']})
            sleep_dates_for_dropdown.append({'label': date_str, 'value': date_str})
            
            # Use cached sleep stage data if available
            if cached_sleep['deep']:
                sleep_stages_totals['Deep'] += cached_sleep['deep']
            if cached_sleep['light']:
                sleep_stages_totals['Light'] += cached_sleep['light']
            if cached_sleep['rem']:
                sleep_stages_totals['REM'] += cached_sleep['rem']
            if cached_sleep['wake']:
                sleep_stages_totals['Wake'] += cached_sleep['wake']
        elif date_str in sleep_record_dict:
            # Fallback to sleep_record_dict if not in cache yet
            sleep_data = sleep_record_dict[date_str]
            fitbit_score = sleep_data.get('sleep_score')
            if fitbit_score is not None:
                print(f"⚠️ Using FALLBACK sleep score for {date_str}: {fitbit_score} (not in cache)")
                sleep_scores.append({'Date': date_str, 'Score': fitbit_score})
                sleep_dates_for_dropdown.append({'label': date_str, 'value': date_str})
            
            # Accumulate stage totals for pie chart
            sleep_stages_totals['Deep'] += sleep_data.get('deep', 0)
            sleep_stages_totals['Light'] += sleep_data.get('light', 0)
            sleep_stages_totals['REM'] += sleep_data.get('rem', 0)
            sleep_stages_totals['Wake'] += sleep_data.get('wake', 0)
        else:
            print(f"⚠️ No sleep data found for {date_str} (not in cache or sleep_record_dict)")
    
    # Sleep Score Chart
    if sleep_scores:
        sleep_score_df = pd.DataFrame(sleep_scores)
        fig_sleep_score = px.line(sleep_score_df, x='Date', y='Score', 
                                   title='Sleep Quality Score (0-100)',
                                   markers=True)
        fig_sleep_score.update_layout(yaxis_range=[0, 100])
        fig_sleep_score.add_hline(y=75, line_dash="dot", line_color="green", 
                                   annotation_text="Good Sleep", annotation_position="right")
    else:
        fig_sleep_score = px.line(title='Sleep Quality Score (No Data)')
    
    # Sleep Stages Pie Chart
    if sum(sleep_stages_totals.values()) > 0:
        stages_df = pd.DataFrame([{'Stage': k, 'Minutes': v} for k, v in sleep_stages_totals.items() if v > 0])
        fig_sleep_stages_pie = px.pie(stages_df, values='Minutes', names='Stage',
                                       title='Average Sleep Stage Distribution',
                                       color='Stage',
                                       color_discrete_map={'Deep': '#084466', 'Light': '#1e9ad6', 
                                                          'REM': '#4cc5da', 'Wake': '#fd7676'})
    else:
        fig_sleep_stages_pie = px.pie(title='Sleep Stages (No Data)')
    
    # Phase 4: Exercise-Sleep Correlation
    correlation_data = []
    for i, date_str in enumerate(dates_str_list[:-1]):  # Skip last day
        # Check if there was exercise on this day
        exercise_calories = sum([ex['Calories'] for ex in exercise_data if ex['Date'] == date_str])
        exercise_duration = sum([ex['Duration (min)'] for ex in exercise_data if ex['Date'] == date_str])
        
        # Get next day's sleep
        next_date = dates_str_list[i + 1]
        if next_date in sleep_record_dict:
            sleep_data = sleep_record_dict[next_date]
            correlation_data.append({
                'Date': date_str,
                'Exercise Calories': exercise_calories,
                'Exercise Duration (min)': exercise_duration,
                'Next Day Sleep (min)': sleep_data.get('total_sleep', 0),
                'Deep Sleep %': (sleep_data.get('deep', 0) / sleep_data.get('total_sleep', 1) * 100) if sleep_data.get('total_sleep', 0) > 0 else 0
            })
    
    if correlation_data and len(correlation_data) > 3:
        corr_df = pd.DataFrame(correlation_data)
        corr_df = corr_df[corr_df['Exercise Calories'] > 0]  # Only days with exercise
        
        if len(corr_df) > 0:
            fig_correlation = px.scatter(corr_df, x='Exercise Calories', y='Next Day Sleep (min)',
                                        size='Exercise Duration (min)', hover_data=['Date'],
                                        title='Exercise Impact on Next Day Sleep',
                                        trendline="ols")
            fig_correlation.update_layout(xaxis_title="Exercise Calories Burned",
                                         yaxis_title="Next Day Sleep Duration (min)")
            
            # Calculate correlation coefficient
            if len(corr_df) >= 3:
                corr_coef = corr_df['Exercise Calories'].corr(corr_df['Next Day Sleep (min)'])
                avg_exercise_sleep = corr_df[corr_df['Exercise Calories'] > 100]['Next Day Sleep (min)'].mean()
                avg_no_exercise_sleep = corr_df[corr_df['Exercise Calories'] <= 100]['Next Day Sleep (min)'].mean()
                
                correlation_insights = html.Div([
                    html.H5("🔍 Insights:", style={'margin-bottom': '15px'}),
                    html.P(f"📊 Correlation between exercise and next-day sleep: {corr_coef:.2f}" + 
                          (" (Positive - More exercise correlates with better sleep!)" if corr_coef > 0.3 else 
                           " (Negative - Heavy exercise may be affecting sleep)" if corr_coef < -0.3 else 
                           " (Weak correlation)")),
                    html.P(f"💪 Average sleep after workout days: {avg_exercise_sleep:.0f} minutes" if not pd.isna(avg_exercise_sleep) else ""),
                    html.P(f"😴 Average sleep on rest days: {avg_no_exercise_sleep:.0f} minutes" if not pd.isna(avg_no_exercise_sleep) else ""),
                    html.P(f"✨ Best practice: Your data suggests exercising in the {'morning/afternoon' if corr_coef > 0 else 'earlier hours'} for optimal sleep quality.")
                ])
            else:
                correlation_insights = html.P("Need more exercise data for meaningful insights (minimum 3 workout days).")
        else:
            fig_correlation = px.scatter(title='Exercise-Sleep Correlation (No Exercise Data)')
            correlation_insights = html.P("No exercise activities found in this period.")
    else:
        fig_correlation = px.scatter(title='Exercise-Sleep Correlation (Insufficient Data)')
        correlation_insights = html.P("Need more data points for correlation analysis. Try a longer date range or log more workouts!")
    
    # Phase 5: AZM vs Sleep Score Correlation (same day)
    azm_sleep_data = []
    for date_str in dates_str_list:
        # Get AZM for this date from df_merged
        azm_value = df_merged[df_merged['Date'] == date_str]['Active Zone Minutes'].values
        azm = azm_value[0] if len(azm_value) > 0 and not pd.isna(azm_value[0]) else 0
        
        # Get Sleep Score for this date
        cached_sleep = cache.get_sleep_data(date_str)
        if cached_sleep and cached_sleep['sleep_score'] is not None:
            sleep_score = cached_sleep['sleep_score']
            azm_sleep_data.append({
                'Date': date_str,
                'Active Zone Minutes': azm,
                'Sleep Score': sleep_score
            })
        elif date_str in sleep_record_dict and sleep_record_dict[date_str].get('sleep_score'):
            sleep_score = sleep_record_dict[date_str]['sleep_score']
            azm_sleep_data.append({
                'Date': date_str,
                'Active Zone Minutes': azm,
                'Sleep Score': sleep_score
            })
    
    if azm_sleep_data and len(azm_sleep_data) >= 3:
        azm_sleep_df = pd.DataFrame(azm_sleep_data)
        azm_sleep_df = azm_sleep_df[azm_sleep_df['Sleep Score'] > 0]  # Filter valid scores
        
        if len(azm_sleep_df) >= 3:
            fig_azm_sleep_correlation = px.scatter(azm_sleep_df, x='Active Zone Minutes', y='Sleep Score',
                                                   hover_data=['Date'],
                                                   title='Active Zone Minutes vs Sleep Quality',
                                                   trendline="ols")
            fig_azm_sleep_correlation.update_layout(xaxis_title="Active Zone Minutes (Daily)",
                                                   yaxis_title="Sleep Score (0-100)",
                                                   yaxis_range=[0, 100])
            fig_azm_sleep_correlation.add_hline(y=75, line_dash="dot", line_color="green",
                                                annotation_text="Good Sleep", annotation_position="right")
            
            # Calculate correlation coefficient
            azm_corr_coef = azm_sleep_df['Active Zone Minutes'].corr(azm_sleep_df['Sleep Score'])
            print(f"📊 AZM vs Sleep Score Correlation: {azm_corr_coef:.3f}")
        else:
            fig_azm_sleep_correlation = px.scatter(title='AZM-Sleep Correlation (Insufficient Data)')
    else:
        fig_azm_sleep_correlation = px.scatter(title='AZM-Sleep Correlation (Insufficient Data)')
    
    return report_title, report_dates_range, generated_on_date, fig_rhr, rhr_summary_table, fig_steps, fig_steps_heatmap, steps_summary_table, fig_activity_minutes, fat_burn_summary_table, cardio_summary_table, peak_summary_table, fig_weight, weight_summary_table, fig_spo2, spo2_summary_table, fig_eov, eov_summary_table, fig_sleep_minutes, fig_sleep_regularity, sleep_summary_table, [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': False}], fig_hrv, hrv_summary_table, fig_breathing, breathing_summary_table, fig_cardio_fitness, cardio_fitness_summary_table, fig_temperature, temperature_summary_table, fig_azm, azm_summary_table, fig_calories, fig_distance, calories_summary_table, fig_floors, floors_summary_table, exercise_filter_options, exercise_log_table, workout_dates_for_dropdown, fig_sleep_score, fig_sleep_stages_pie, sleep_dates_for_dropdown, fig_correlation, fig_azm_sleep_correlation, correlation_insights, ""

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
        
        # Fetch activities for the date
        activities_response = requests.get(
            f"https://api.fitbit.com/1/user/-/activities/list.json?afterDate={date}&sort=asc&offset=0&limit=100",
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
