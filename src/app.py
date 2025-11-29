# %%
import os
import base64
import logging
from logging.handlers import RotatingFileHandler
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
import sqlite3


# %%

# Configure file logging with rotation (50MB x 3 files = 150MB max)
log_dir = '/app/logs'
os.makedirs(log_dir, exist_ok=True)

# Get log level from environment variable (default: INFO)
log_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
log_level_map = {
    'CRITICAL': logging.CRITICAL,  # 50 - Fatal errors, app will crash
    'FATAL': logging.CRITICAL,     # Alias for CRITICAL
    'ERROR': logging.ERROR,         # 40 - Errors that don't crash the app
    'WARN': logging.WARNING,        # 30 - Warnings, potential issues
    'WARNING': logging.WARNING,     # Alias for WARN
    'INFO': logging.INFO,           # 20 - Normal operational messages
    'DEBUG': logging.DEBUG,         # 10 - Detailed diagnostic info
    'TRACE': 5                      # 5 - Most verbose, step-by-step execution
}

# Add TRACE level if not exists
if not hasattr(logging, 'TRACE'):
    logging.TRACE = 5
    logging.addLevelName(5, 'TRACE')
    def trace(self, message, *args, **kwargs):
        if self.isEnabledFor(5):
            self._log(5, message, args, **kwargs)
    logging.Logger.trace = trace

log_level = log_level_map.get(log_level_str, logging.INFO)

# Create rotating file handler
file_handler = RotatingFileHandler(
    os.path.join(log_dir, 'fitbit-app.log'),
    maxBytes=50 * 1024 * 1024,  # 50MB
    backupCount=3  # Keep 3 backup files
)
file_handler.setLevel(log_level)

# Create console handler (still show in Docker logs too)
console_handler = logging.StreamHandler()
console_handler.setLevel(log_level)

# Create formatter
formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(log_level)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Log the configured level on startup
print(f"🔧 Log level set to: {log_level_str} ({log_level})")

log = logging.getLogger(__name__)

# Intercept print() to also write to log files
import sys
class LoggerWriter:
    def __init__(self, level):
        self.level = level
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        
    def write(self, message):
        # Write to original stdout/stderr (for Docker logs)
        if self.level == 'stdout':
            self.original_stdout.write(message)
        else:
            self.original_stderr.write(message)
        
        # Also write to log file
        if message.strip():  # Avoid logging empty lines
            if self.level == 'stdout':
                root_logger.info(message.strip())
            else:
                root_logger.error(message.strip())
    
    def flush(self):
        if self.level == 'stdout':
            self.original_stdout.flush()
        else:
            self.original_stderr.flush()

# Redirect stdout/stderr to logger (while keeping original output)
sys.stdout = LoggerWriter('stdout')
sys.stderr = LoggerWriter('stderr')

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
        dates_str_list: List of date strings (YYYY-MM-DD) - master date list, or None to extract from response
        metric_type: One of: 'steps', 'calories', 'distance', 'floors', 'azm', 'heartrate', 'weight', 'spo2'
        response_data: Raw API response JSON
        cache_manager: FitbitCache instance
    
    Returns:
        int: Number of days successfully cached
    """
    cached_count = 0
    
    # 🐞 CRITICAL FIX: If dates_str_list is None, extract dates from API response
    # This prevents iterating over dates that don't have data, which would skip caching
    # and cause fragmented data in the database
    
    if metric_type == 'steps':
        # Create lookup dictionary
        steps_lookup = {entry['dateTime']: int(entry['value']) 
                       for entry in response_data.get('activities-steps', [])}
        
        # Use dates from API response if no master list provided
        if dates_str_list is None:
            dates_str_list = list(steps_lookup.keys())
        
        # Iterate over date list
        for date_str in dates_str_list:
            steps_value = steps_lookup.get(date_str)
            if steps_value == 0:
                steps_value = None  # Treat 0 as None
            
            if steps_value is not None:
                try:
                    # print(f"  [CACHE_DEBUG] Caching Steps for {date_str}: Value={steps_value} (Type: {type(steps_value).__name__})")
                    cache_manager.set_daily_metrics(date=date_str, steps=steps_value)
                    cached_count += 1
                    # Verify immediately (disabled - too verbose)
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and verify_val.get('steps') == steps_value:
                    #     print(f"  ✅ [CACHE_VERIFY] Steps cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] Steps verification FAILED for {date_str}: Expected {steps_value}, Got {verify_val.get('steps') if verify_val else 'NULL'}")
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching Steps for {date_str}: Value={steps_value}, Error={e}")
                    import traceback
                    traceback.print_exc()
            else:
                if date_str in dates_str_list[:3]:  # Only log first 3 to avoid spam
                    print(f"⚠️ No steps data for {date_str}")
    
    elif metric_type == 'calories':
        calories_lookup = {}
        for entry in response_data.get('activities-calories', []):
            try:
                calories_lookup[entry['dateTime']] = int(entry['value'])
            except (KeyError, ValueError):
                pass
        
        # Use dates from API response if no master list provided
        if dates_str_list is None:
            dates_str_list = list(calories_lookup.keys())
        
        for date_str in dates_str_list:
            calories_value = calories_lookup.get(date_str)
            if calories_value is not None:
                try:
                    # print(f"  [CACHE_DEBUG] Caching Calories for {date_str}: Value={calories_value} (Type: {type(calories_value).__name__})")
                    cache_manager.set_daily_metrics(date=date_str, calories=int(calories_value))
                    cached_count += 1
                    # Verify immediately (disabled - too verbose)
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and verify_val.get('calories') == int(calories_value):
                    #     print(f"  ✅ [CACHE_VERIFY] Calories cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] Calories verification FAILED for {date_str}: Expected {calories_value}, Got {verify_val.get('calories') if verify_val else 'NULL'}")
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching Calories for {date_str}: Value={calories_value}, Error={e}")
                    import traceback
                    traceback.print_exc()
    
    elif metric_type == 'distance':
        distance_lookup = {}
        for entry in response_data.get('activities-distance', []):
            try:
                distance_km = float(entry['value'])
                distance_miles = round(distance_km * 0.621371, 2)
                distance_lookup[entry['dateTime']] = distance_miles
            except (KeyError, ValueError):
                pass
        
        # Use dates from API response if no master list provided
        if dates_str_list is None:
            dates_str_list = list(distance_lookup.keys())
        
        for date_str in dates_str_list:
            distance_value = distance_lookup.get(date_str)
            if distance_value is not None:
                try:
                    # print(f"  [CACHE_DEBUG] Caching Distance for {date_str}: Value={distance_value} (Type: {type(distance_value).__name__})")
                    cache_manager.set_daily_metrics(date=date_str, distance=float(distance_value))
                    cached_count += 1
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and abs(verify_val.get('distance', 0) - float(distance_value)) < 0.01:
                    #     print(f"  ✅ [CACHE_VERIFY] Distance cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] Distance verification FAILED for {date_str}: Expected {distance_value}, Got {verify_val.get('distance') if verify_val else 'NULL'}")
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching Distance for {date_str}: Value={distance_value}, Error={e}")
                    import traceback
                    traceback.print_exc()
    
    elif metric_type == 'floors':
        floors_lookup = {}
        for entry in response_data.get('activities-floors', []):
            try:
                floors_lookup[entry['dateTime']] = int(entry['value'])
            except (KeyError, ValueError):
                pass
        
        # Use dates from API response if no master list provided
        if dates_str_list is None:
            dates_str_list = list(floors_lookup.keys())
        
        for date_str in dates_str_list:
            floors_value = floors_lookup.get(date_str)
            if floors_value is not None:
                try:
                    # print(f"  [CACHE_DEBUG] Caching Floors for {date_str}: Value={floors_value} (Type: {type(floors_value).__name__})")
                    cache_manager.set_daily_metrics(date=date_str, floors=int(floors_value))
                    cached_count += 1
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and verify_val.get('floors') == int(floors_value):
                    #     print(f"  ✅ [CACHE_VERIFY] Floors cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] Floors verification FAILED for {date_str}: Expected {floors_value}, Got {verify_val.get('floors') if verify_val else 'NULL'}")
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching Floors for {date_str}: Value={floors_value}, Error={e}")
                    import traceback
                    traceback.print_exc()
    
    elif metric_type == 'azm':
        azm_lookup = {}
        for entry in response_data.get('activities-active-zone-minutes', []):
            try:
                azm_lookup[entry['dateTime']] = entry['value']['activeZoneMinutes']
            except (KeyError, ValueError):
                pass
        
        # Use dates from API response if no master list provided
        if dates_str_list is None:
            dates_str_list = list(azm_lookup.keys())
        
        for date_str in dates_str_list:
            azm_value = azm_lookup.get(date_str)
            if azm_value is not None:
                try:
                    # print(f"  [CACHE_DEBUG] Caching AZM for {date_str}: Value={azm_value} (Type: {type(azm_value).__name__})")
                    cache_manager.set_daily_metrics(date=date_str, active_zone_minutes=int(azm_value))
                    cached_count += 1
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and verify_val.get('active_zone_minutes') == int(azm_value):
                    #     print(f"  ✅ [CACHE_VERIFY] AZM cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] AZM verification FAILED for {date_str}: Expected {azm_value}, Got {verify_val.get('active_zone_minutes') if verify_val else 'NULL'}")
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching AZM for {date_str}: Value={azm_value}, Error={e}")
                    import traceback
                    traceback.print_exc()
    
    elif metric_type == 'heartrate':
        # 🐞 FIX: Cache both RHR AND HR zones (fat burn, cardio, peak)
        hr_lookup = {}
        for entry in response_data.get('activities-heart', []):
            try:
                date_str = entry['dateTime']
                hr_data = {}
                
                # Resting Heart Rate
                if 'value' in entry and 'restingHeartRate' in entry['value']:
                    hr_data['rhr'] = entry['value']['restingHeartRate']
                
                # Heart Rate Zones (fat burn, cardio, peak)
                if 'value' in entry and 'heartRateZones' in entry['value']:
                    zones = entry['value']['heartRateZones']
                    if len(zones) >= 4:
                        hr_data['fat_burn'] = zones[1].get('minutes', 0)  # Index 1 = Fat Burn
                        hr_data['cardio'] = zones[2].get('minutes', 0)     # Index 2 = Cardio
                        hr_data['peak'] = zones[3].get('minutes', 0)       # Index 3 = Peak
                
                if hr_data:  # Only add if we have at least some data
                    hr_lookup[date_str] = hr_data
            except (KeyError, ValueError, TypeError):
                pass
        
        # Use dates from API response if no master list provided
        if dates_str_list is None:
            dates_str_list = list(hr_lookup.keys())
        
        for date_str in dates_str_list:
            hr_data = hr_lookup.get(date_str)
            if hr_data:
                try:
                    rhr = hr_data.get('rhr')
                    fat_burn = hr_data.get('fat_burn')
                    cardio = hr_data.get('cardio')
                    peak = hr_data.get('peak')
                    # print(f"  [CACHE_DEBUG] Caching HR for {date_str}: RHR={rhr}, FatBurn={fat_burn}, Cardio={cardio}, Peak={peak}")
                    cache_manager.set_daily_metrics(
                        date=date_str, 
                        resting_heart_rate=rhr,
                        fat_burn_minutes=fat_burn,
                        cardio_minutes=cardio,
                        peak_minutes=peak
                    )
                    cached_count += 1
                    # Verify immediately (disabled - too verbose)
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val:
                    #     v_rhr = verify_val.get('resting_heart_rate')
                    #     v_fb = verify_val.get('fat_burn_minutes')
                    #     v_cardio = verify_val.get('cardio_minutes')
                    #     v_peak = verify_val.get('peak_minutes')
                    #     if v_rhr == rhr and v_fb == fat_burn and v_cardio == cardio and v_peak == peak:
                    #         print(f"  ✅ [CACHE_VERIFY] HR cached successfully for {date_str}")
                    #     else:
                    #         print(f"  ❌ [CACHE_VERIFY] HR verification FAILED for {date_str}:")
                    #         print(f"     Expected: RHR={rhr}, FB={fat_burn}, Cardio={cardio}, Peak={peak}")
                    #         print(f"     Got:      RHR={v_rhr}, FB={v_fb}, Cardio={v_cardio}, Peak={v_peak}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] HR verification FAILED for {date_str}: No data returned")
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching HR for {date_str}: {e}")
                    import traceback
                    traceback.print_exc()
    
    elif metric_type == 'weight':
        weight_lookup = {}
        # 1. Build the lookup dictionary FIRST
        
        # CORRECT KEYS per actual API testing: 'weight' and 'date'
        for entry in response_data.get('weight', []):
            try:
                date_str = entry['date']  # API uses 'date' not 'dateTime'
                weight_kg = float(entry['weight'])
                weight_lbs = round(weight_kg * 2.20462, 1)
                body_fat_pct = entry.get('fat')  # 'fat' key is correct
                
                weight_lookup[date_str] = {'weight': weight_lbs, 'body_fat': body_fat_pct}
            except (KeyError, ValueError, TypeError) as e:
                print(f"  [CACHE_DEBUG] Error parsing weight entry: {entry}, Error: {e}")
                pass
        
        # 2. Use the lookup's keys as the dates to iterate over
        for date_str, weight_data in weight_lookup.items():
            if weight_data is not None:
                try:
                    # === START FIX: Define variables correctly ===
                    weight_value = weight_data.get('weight')
                    body_fat_value = weight_data.get('body_fat')
                    # === END FIX ===
                    
                    if weight_value is None: # Skip if no weight value
                        continue
                    # print(f"  [CACHE_DEBUG] Caching Weight for {date_str}: Weight={weight_value}, Body Fat={body_fat_value}%")
                    
                    # 3. Call the cache function with BOTH weight and body_fat
                    cache_manager.set_daily_metrics(date=date_str, weight=float(weight_value), body_fat=float(body_fat_value) if body_fat_value is not None else None)
                    cached_count += 1
                    
                    # 4. Verify the write (disabled - too verbose)
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and verify_val.get('weight') is not None and abs(verify_val.get('weight', 0) - float(weight_value)) < 0.1:
                    #     print(f"  ✅ [CACHE_VERIFY] Weight/Fat cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] Weight verification FAILED for {date_str}: Expected {weight_value}, Got {verify_val.get('weight') if verify_val else 'NULL'}")
                
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching Weight for {date_str}: Value={weight_data}, Error={e}")
                    import traceback
                    traceback.print_exc()
    
    elif metric_type == 'spo2':
        spo2_lookup = {}
        eov_lookup = {}
        # 1. Build lookup dictionaries FIRST
        for entry in response_data:
            try:
                if isinstance(entry, dict) and 'dateTime' in entry and 'value' in entry:
                    date_str = entry['dateTime']
                    if 'avg' in entry['value']:
                        spo2_lookup[date_str] = float(entry['value']['avg'])
                    # EOV is in the same entry
                    eov_val = entry['value'].get("eov") or entry['value'].get("variationScore")
                    if eov_val is not None:
                        eov_lookup[date_str] = float(eov_val)
            except (KeyError, ValueError, TypeError) as e:
                print(f"  [CACHE_DEBUG] Error parsing SpO2 entry: {entry}, Error: {e}")
                pass
        
        # 2. Use the spo2_lookup's keys as the dates to iterate over
        all_spo2_dates = set(spo2_lookup.keys()) | set(eov_lookup.keys())
        
        for date_str in all_spo2_dates:
            spo2_value = spo2_lookup.get(date_str)  # Get from lookup
            eov_value = eov_lookup.get(date_str)     # Get from lookup

            if spo2_value is not None or eov_value is not None:
                try:
                    # print(f"  [CACHE_DEBUG] Caching SpO2/EOV for {date_str}: SpO2={spo2_value}, EOV={eov_value}")
                    
                    # 3. Call the cache function
                    cache_manager.set_daily_metrics(date=date_str, spo2=spo2_value, eov=eov_value)
                    cached_count += 1
                    
                    # 4. Verify the write (disabled - too verbose)
                    # verify_val = cache_manager.get_daily_metrics(date_str)
                    # if verify_val and (verify_val.get('spo2') == spo2_value or verify_val.get('eov') == eov_value):
                    #     print(f"  ✅ [CACHE_VERIFY] SpO2/EOV cached successfully for {date_str}")
                    # else:
                    #     print(f"  ❌ [CACHE_VERIFY] SpO2/EOV verification FAILED for {date_str}")
                
                except Exception as e:
                    print(f"❌ [CACHE_ERROR] Failed caching SpO2/EOV for {date_str}: Error={e}")
                    import traceback
                    traceback.print_exc()
    
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
            MAX_CALLS_PER_HOUR = 130  # Leave 20 calls free for user interaction (report generation + workout details)
            
            # === START CRITICAL FIX #1: ALWAYS GET LATEST REFRESH TOKEN ===
            # This logic must run INSIDE the hourly loop
            
            current_refresh_token = cache.get_refresh_token()  # GET LATEST TOKEN FROM DB
            if not current_refresh_token:
                print("❌ Background builder stopping: No refresh token found in cache. Please log in again.")
                cache_builder_running = False  # Stop the thread
                break  # Exit the while loop

            print("\n🔄 Refreshing access token for new hourly cycle...")
            try:
                # Use the existing refresh_access_token function
                new_access, new_refresh, new_expiry = refresh_access_token(current_refresh_token)
                
                if new_access:
                    current_access_token = new_access
                    headers = {"Authorization": f"Bearer {current_access_token}"}
                    print(f"✅ Token refreshed successfully! Valid for 8 hours.")

                    # IMPORTANT: Update the refresh token *back* into the cache if it changed
                    if new_refresh and new_refresh != current_refresh_token:
                        print("✨ New refresh token received, updating cache...")
                        cache.store_refresh_token(new_refresh, 28800)
                        current_refresh_token = new_refresh  # Use the newest one going forward
                
                else:
                    print("❌ Token refresh failed! Background builder pausing for 1 hour.")
                    time.sleep(3600)  # Wait an hour before retrying
                    continue  # Skip to the next hourly cycle
            
            except Exception as e:
                print(f"❌ CRITICAL Error refreshing token: {e}. Background builder pausing for 1 hour.")
                import traceback
                traceback.print_exc()
                time.sleep(3600)  # Wait an hour before retrying
                continue  # Skip to the next hourly cycle
            # === END CRITICAL FIX #1 ===
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
            print(f"📅 Fetching range: {start_date_str} to {end_date_str} (365 days)")
            
            # Generate master date list for caching alignment
            dates_str_list = []
            current = start_date
            while current <= end_date:
                dates_str_list.append(current.strftime('%Y-%m-%d'))
                current += timedelta(days=1)
            
            phase1_calls = 0
            rate_limit_hit = False  # Flag to track if we hit rate limit
            
            # These endpoints support date ranges - very efficient!
            # NOTE: Weight endpoint removed - it only supports 31-day max, moved to Phase 3
            range_endpoints = [
                ("Heart Rate", f"https://api.fitbit.com/1/user/-/activities/heart/date/{start_date_str}/{end_date_str}.json"),
                ("Steps", f"https://api.fitbit.com/1/user/-/activities/steps/date/{start_date_str}/{end_date_str}.json"),
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
                        if metric_name in ["Activities", "Weight"]:
                            print(f"   ℹ️ {metric_name} endpoint: {endpoint}")
                            try:
                                error_data = response.json()
                                print(f"   ℹ️ Error response: {error_data}")
                            except:
                                print(f"   ℹ️ Response text: {response.text[:200]}")
                        continue
                    
                    print(f"✅ Success ({response.status_code})", end="")
                    
                    # 🐞 FIX: Process and cache the fetched data immediately
                    if response.status_code == 200:
                        response_data = response.json()
                        cached = 0
                        
                        # 🐞 CRITICAL FIX: Don't pass full 365-day list - only cache dates that API returned data for
                        # This prevents NULL overwrites when API doesn't return data for older dates
                        if metric_name == "Heart Rate":
                            cached = process_and_cache_daily_metrics(None, 'heartrate', response_data, cache)
                        elif metric_name == "Steps":
                            cached = process_and_cache_daily_metrics(None, 'steps', response_data, cache)
                        elif metric_name == "Weight":
                            cached = process_and_cache_daily_metrics(None, 'weight', response_data, cache)
                        elif metric_name == "SpO2":
                            cached = process_and_cache_daily_metrics(None, 'spo2', response_data, cache)
                        elif metric_name == "Calories":
                            cached = process_and_cache_daily_metrics(None, 'calories', response_data, cache)
                        elif metric_name == "Distance":
                            cached = process_and_cache_daily_metrics(None, 'distance', response_data, cache)
                        elif metric_name == "Floors":
                            cached = process_and_cache_daily_metrics(None, 'floors', response_data, cache)
                        elif metric_name == "Active Zone Minutes":
                            cached = process_and_cache_daily_metrics(None, 'azm', response_data, cache)
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
                                        activity_data_json=json.dumps(activity)
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
            
            # 🐞 CRITICAL FIX: Phase 1 Per-Metric Retry (fills gaps from failed/incomplete fetches)
            if not rate_limit_hit and api_calls_this_hour < MAX_CALLS_PER_HOUR:
                print("\n📍 PHASE 1 RETRY: Checking for missing Phase 1 metrics...")
                print("-" * 60)
                
                # NOTE: Weight removed - moved to Phase 3 due to 31-day API limit
                phase1_retry_metrics = [
                    ('steps', 'Steps', f"https://api.fitbit.com/1/user/-/activities/steps/date/{start_date_str}/{end_date_str}.json"),
                    ('calories', 'Calories', f"https://api.fitbit.com/1/user/-/activities/calories/date/{start_date_str}/{end_date_str}.json"),
                    ('distance', 'Distance', f"https://api.fitbit.com/1/user/-/activities/distance/date/{start_date_str}/{end_date_str}.json"),
                    ('floors', 'Floors', f"https://api.fitbit.com/1/user/-/activities/floors/date/{start_date_str}/{end_date_str}.json"),
                    ('azm', 'Active Zone Minutes', f"https://api.fitbit.com/1/user/-/activities/active-zone-minutes/date/{start_date_str}/{end_date_str}.json"),
                    ('heartrate', 'Heart Rate', f"https://api.fitbit.com/1/user/-/activities/heart/date/{start_date_str}/{end_date_str}.json"),
                    ('spo2', 'SpO2', f"https://api.fitbit.com/1/user/-/spo2/date/{start_date_str}/{end_date_str}.json"),
                ]
                
                for metric_key, metric_name, endpoint in phase1_retry_metrics:
                    if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                        break
                    
                    # Check if this metric has missing dates
                    missing_dates = cache.get_missing_dates(start_date_str, end_date_str, metric_type=metric_key)
                    if not missing_dates:
                        print(f"✅ '{metric_name}' is 100% cached.")
                        continue
                    
                    print(f"📥 '{metric_name}' missing {len(missing_dates)} days. Re-fetching...")
                    try:
                        response = requests.get(endpoint, headers=headers, timeout=15)
                        api_calls_this_hour += 1
                        
                        if response.status_code == 429:
                            print(f"❌ Rate limit hit on '{metric_name}' retry")
                            rate_limit_hit = True
                            break
                        
                        if response.status_code == 200:
                            response_data = response.json()
                            cached = process_and_cache_daily_metrics(None, metric_key, response_data, cache)
                            print(f"  → 💾 Cached {cached} days for '{metric_name}'")
                        else:
                            print(f"  ⚠️ Error ({response.status_code})")
                    except Exception as e:
                        print(f"❌ Error on '{metric_name}' retry: {e}")
                
                print(f"✅ Phase 1 Retry Complete")
                print(f"📊 API Budget Remaining: {MAX_CALLS_PER_HOUR - api_calls_this_hour}\n")
            
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
                                        # Note: Fitbit's sleep score doesn't work, so we only use our calculated scores
                                        # Calculate our custom 3-tier sleep scores from stages
                                        minutes_asleep = sleep_record.get('minutesAsleep', 0)
                                        deep_min = sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes', 0)
                                        rem_min = sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes', 0)
                                        minutes_awake = sleep_record.get('minutesAwake', 0)
                                        
                                        calculated_scores = calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake)
                                        
                                        print(f"⚠️ YESTERDAY REFRESH - No sleep score for {yesterday}, but caching stages/duration")
                                        print(f"   📊 Calculated scores: Reality={calculated_scores['reality_score']}, Proxy={calculated_scores['proxy_score']}, Efficiency={sleep_record.get('efficiency')}")
                                        
                                        cache.set_sleep_score(
                                            date=yesterday,
                                            sleep_score=None,  # Fitbit sleep score doesn't work
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
                
                # 🐞 CRITICAL FIX: PHASE 3 - Per-Metric Fetching (prevents fragmented cache)
                # Each metric is now checked and fetched INDEPENDENTLY
                print(f"\n📍 PHASE 3: Daily Endpoints (Per-Metric)")
                print("-" * 60)
                
                date_range_start = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
                date_range_end = datetime.now().strftime('%Y-%m-%d')
                
                phase3_metrics_processed = {'weight': 0, 'sleep': 0, 'hrv': 0, 'br': 0, 'temp': 0}
                
                # --- 3A: FETCH MISSING WEIGHT DATA FIRST (1 call = ~30 days) ---
                if api_calls_this_hour < MAX_CALLS_PER_HOUR and not rate_limit_hit:
                    missing_weight = cache.get_missing_dates(date_range_start, date_range_end, metric_type='weight')
                    if missing_weight:
                        newest_date = max(missing_weight)
                        newest_dt = datetime.strptime(newest_date, '%Y-%m-%d')
                        print(f"📥 [3A: Weight] Fetching {newest_dt.strftime('%B %Y')} (1 month) ending {newest_date} (1 API call)...")
                        
                        try:
                            endpoint = f"https://api.fitbit.com/1/user/-/body/log/weight/date/{newest_date}/1m.json"
                            response = requests.get(endpoint, headers=headers, timeout=10)
                            api_calls_this_hour += 1
                            
                            if response.status_code == 429:
                                rate_limit_hit = True
                                print(f"⚠️ [3A: Weight] Rate limit hit")
                            elif response.status_code == 200:
                                data = response.json()
                                print(f"📥 [3A: Weight] API Response: {len(data.get('weight', []))} weight entries")
                                if 'weight' in data and len(data['weight']) > 0:
                                    # Show first entry for debugging
                                    first_entry = data['weight'][0]
                                    print(f"   First entry: date={first_entry.get('date')}, weight={first_entry.get('weight')}kg, fat={first_entry.get('fat')}%")
                                    cached = process_and_cache_daily_metrics(None, 'weight', data, cache)
                                    phase3_metrics_processed['weight'] = cached
                                    print(f"✅ [3A: Weight] Cached {cached} dates")
                                else:
                                    print(f"⚠️ [3A: Weight] No weight data in response: {data}")
                            else:
                                print(f"⚠️ [3A: Weight] Error {response.status_code}: {response.text[:200]}")
                        except Exception as e:
                            print(f"❌ [3A: Weight] Error: {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print("✅ [3A: Weight] 100% cached")
                
                # --- 3B: FETCH MISSING SLEEP DATA (RANGE ENDPOINT - 1 CALL = 1 MONTH) ---
                if api_calls_this_hour < MAX_CALLS_PER_HOUR and not rate_limit_hit:
                    missing_sleep = cache.get_missing_dates(date_range_start, date_range_end, metric_type='sleep')
                    if missing_sleep:
                        # Use range endpoint: 1 API call fetches one calendar month
                        # Get the newest missing date and fetch its entire month
                        newest_missing = max(missing_sleep)
                        newest_dt = datetime.strptime(newest_missing, '%Y-%m-%d')
                        
                        # Calculate first and last day of that month
                        first_day_of_month = newest_dt.replace(day=1)
                        if newest_dt.month == 12:
                            last_day_of_month = newest_dt.replace(day=31)
                        else:
                            next_month = newest_dt.replace(month=newest_dt.month + 1, day=1)
                            last_day_of_month = next_month - timedelta(days=1)
                        
                        oldest_date = first_day_of_month.strftime('%Y-%m-%d')
                        newest_date = last_day_of_month.strftime('%Y-%m-%d')
                        days_in_month = (last_day_of_month - first_day_of_month).days + 1
                        
                        print(f"📥 [3B: Sleep] Fetching {newest_dt.strftime('%B %Y')} ({days_in_month} days) from {oldest_date} to {newest_date} (1 API call)...")
                        
                        try:
                            endpoint = f"https://api.fitbit.com/1.2/user/-/sleep/date/{oldest_date}/{newest_date}.json"
                            response = requests.get(endpoint, headers=headers, timeout=10)
                            api_calls_this_hour += 1
                            
                            if response.status_code == 429:
                                rate_limit_hit = True
                                print(f"⚠️ [3B: Sleep] Rate limit hit")
                            elif response.status_code == 200:
                                data = response.json()
                                sleep_records = data.get('sleep', [])
                                print(f"📥 [3B: Sleep] API Response: {len(sleep_records)} sleep records")
                                
                                for sleep_record in sleep_records:
                                    if sleep_record.get('isMainSleep', True):
                                        date_str = sleep_record.get('dateOfSleep')
                                        if not date_str:
                                            continue
                                        
                                        try:
                                            minutes_asleep = sleep_record.get('minutesAsleep', 0)
                                            deep_min = sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes', 0)
                                            rem_min = sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes', 0)
                                            minutes_awake = sleep_record.get('minutesAwake', 0)
                                            calculated_scores = calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake)
                                            
                                            # Note: Fitbit's sleep score doesn't work, so we only use our calculated scores
                                            cache.set_sleep_score(
                                                date=date_str,
                                                sleep_score=None,  # Fitbit's sleep score doesn't work
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
                                            phase3_metrics_processed['sleep'] += 1
                                        except Exception as e:
                                            print(f"❌ Error caching sleep for {date_str}: {e}")
                                
                                print(f"✅ [3B: Sleep] Cached {phase3_metrics_processed['sleep']} dates")
                            else:
                                print(f"⚠️ [3B: Sleep] Error {response.status_code}: {response.text[:200]}")
                        except Exception as e:
                            print(f"❌ [3B: Sleep] Error: {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print("✅ [3B: Sleep] 100% cached (365 days)")
                
                # --- 3C: FETCH MISSING HRV DATA ---
                if api_calls_this_hour < MAX_CALLS_PER_HOUR and not rate_limit_hit:
                    missing_hrv = cache.get_missing_dates(date_range_start, date_range_end, metric_type='hrv')
                    if missing_hrv:
                        remaining_budget = MAX_CALLS_PER_HOUR - api_calls_this_hour
                        dates_to_fetch = list(reversed(missing_hrv))[:remaining_budget]
                        print(f"📥 [3C: HRV] Fetching {len(dates_to_fetch)} missing dates (budget: {remaining_budget})...")
                        for date_str in dates_to_fetch:
                            if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                                break
                            try:
                                response = requests.get(f"https://api.fitbit.com/1/user/-/hrv/date/{date_str}.json", headers=headers, timeout=10)
                                api_calls_this_hour += 1
                                if response.status_code == 429:
                                    rate_limit_hit = True
                                    break
                                if response.status_code == 200:
                                    data = response.json()
                                    if "hrv" in data and len(data["hrv"]) > 0:
                                        hrv_value = data["hrv"][0]["value"].get("dailyRmssd")
                                        if hrv_value is not None:
                                            cache.set_advanced_metrics(date=date_str, hrv=hrv_value)
                                            phase3_metrics_processed['hrv'] += 1
                            except Exception as e:
                                print(f"❌ Error caching HRV for {date_str}: {e}")
                        print(f"✅ [3C: HRV] Cached {phase3_metrics_processed['hrv']} dates")
                    else:
                        print("✅ [3C: HRV] 100% cached")
                
                # --- 3D: FETCH MISSING BREATHING RATE DATA ---
                if api_calls_this_hour < MAX_CALLS_PER_HOUR and not rate_limit_hit:
                    missing_br = cache.get_missing_dates(date_range_start, date_range_end, metric_type='breathing_rate')
                    if missing_br:
                        remaining_budget = MAX_CALLS_PER_HOUR - api_calls_this_hour
                        dates_to_fetch = list(reversed(missing_br))[:remaining_budget]
                        print(f"📥 [3D: Breathing Rate] Fetching {len(dates_to_fetch)} missing dates (budget: {remaining_budget})...")
                        for date_str in dates_to_fetch:
                            if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                                break
                            try:
                                response = requests.get(f"https://api.fitbit.com/1/user/-/br/date/{date_str}.json", headers=headers, timeout=10)
                                api_calls_this_hour += 1
                                if response.status_code == 429:
                                    rate_limit_hit = True
                                    break
                                if response.status_code == 200:
                                    data = response.json()
                                    if "br" in data and len(data["br"]) > 0:
                                        br_value = data["br"][0]["value"].get("breathingRate")
                                        if br_value is not None:
                                            cache.set_advanced_metrics(date=date_str, breathing_rate=br_value)
                                            phase3_metrics_processed['br'] += 1
                            except Exception as e:
                                print(f"❌ Error caching BR for {date_str}: {e}")
                        print(f"✅ [3D: Breathing Rate] Cached {phase3_metrics_processed['br']} dates")
                    else:
                        print("✅ [3D: Breathing Rate] 100% cached")
                
                # --- 3E: FETCH MISSING TEMPERATURE DATA ---
                if api_calls_this_hour < MAX_CALLS_PER_HOUR and not rate_limit_hit:
                    missing_temp = cache.get_missing_dates(date_range_start, date_range_end, metric_type='temperature')
                    if missing_temp:
                        remaining_budget = MAX_CALLS_PER_HOUR - api_calls_this_hour
                        dates_to_fetch = list(reversed(missing_temp))[:remaining_budget]
                        print(f"📥 [3E: Temperature] Fetching {len(dates_to_fetch)} missing dates (budget: {remaining_budget})...")
                        for date_str in dates_to_fetch:
                            if api_calls_this_hour >= MAX_CALLS_PER_HOUR:
                                break
                            try:
                                response = requests.get(f"https://api.fitbit.com/1/user/-/temp/skin/date/{date_str}.json", headers=headers, timeout=10)
                                api_calls_this_hour += 1
                                if response.status_code == 429:
                                    rate_limit_hit = True
                                    break
                                if response.status_code == 200:
                                    data = response.json()
                                    if "tempSkin" in data and len(data["tempSkin"]) > 0:
                                        temp_value = data["tempSkin"][0]["value"]
                                        if isinstance(temp_value, dict):
                                            temp_value = temp_value.get("nightlyRelative", temp_value.get("value"))
                                        if temp_value is not None:
                                            cache.set_advanced_metrics(date=date_str, temperature=temp_value)
                                            phase3_metrics_processed['temp'] += 1
                            except Exception as e:
                                print(f"❌ Error caching Temp for {date_str}: {e}")
                        print(f"✅ [3E: Temperature] Cached {phase3_metrics_processed['temp']} dates")
                    else:
                        print("✅ [3E: Temperature] 100% cached")
                
                total_phase3 = sum(phase3_metrics_processed.values())
                print(f"✅ Phase 3 Complete: {total_phase3} metric-days cached (Weight={phase3_metrics_processed.get('weight', 0)}, Sleep={phase3_metrics_processed['sleep']}, HRV={phase3_metrics_processed['hrv']}, BR={phase3_metrics_processed['br']}, Temp={phase3_metrics_processed['temp']})")
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
    Fetch sleep data from Fitbit API for missing dates and cache them.
    Note: Fitbit's sleep score doesn't work, so we only use our custom calculated scores.
    
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
            # Fetch individual day's sleep data
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
                        # Note: Fitbit's sleep score doesn't work, so we only use our calculated scores
                        # Calculate our custom 3-tier sleep scores from stages
                        minutes_asleep = sleep_record.get('minutesAsleep', 0)
                        deep_min = sleep_record.get('levels', {}).get('summary', {}).get('deep', {}).get('minutes', 0)
                        rem_min = sleep_record.get('levels', {}).get('summary', {}).get('rem', {}).get('minutes', 0)
                        minutes_awake = sleep_record.get('minutesAwake', 0)
                        
                        calculated_scores = calculate_sleep_scores(minutes_asleep, deep_min, rem_min, minutes_awake)
                        
                        # Cache sleep data with our calculated scores
                        cache.set_sleep_score(
                            date=date_str,
                            sleep_score=None,  # Fitbit sleep score doesn't work
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

def fetch_todays_stats(date_str, access_token):
    """
    Fetches real-time stats for a specific date (usually today) and updates the cache.
    Returns a dictionary of fetched data or None if failed.
    """
    print(f"🔄 Fetching TODAY's real-time stats ({date_str})...")
    headers = {"Authorization": f"Bearer {access_token}"}
    
    # We need to fetch multiple endpoints to get a complete picture
    # 1. Heart Rate (1d)
    # 2. Steps, Calories, Distance, Floors, AZM (1d)
    # 3. Weight (1d)
    # 4. SpO2, HRV, Breathing Rate, Temp (may not be available for today yet, but we try)
    # 5. Sleep (today's sleep)
    
    fetched_data = {}
    
    try:
        # 1. Heart Rate
        url = f"https://api.fitbit.com/1/user/-/activities/heart/date/{date_str}/1d.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            # Process and cache HR
            if 'activities-heart' in data and data['activities-heart']:
                entry = data['activities-heart'][0]
                rhr = entry['value'].get('restingHeartRate')
                zones = entry['value'].get('heartRateZones', [])
                fat_burn = next((z['minutes'] for z in zones if z['name'] == 'Fat Burn'), 0)
                cardio = next((z['minutes'] for z in zones if z['name'] == 'Cardio'), 0)
                peak = next((z['minutes'] for z in zones if z['name'] == 'Peak'), 0)
                
                cache.set_daily_metrics(
                    date=date_str,
                    resting_heart_rate=rhr,
                    fat_burn_minutes=fat_burn,
                    cardio_minutes=cardio,
                    peak_minutes=peak
                )
                fetched_data['heart_rate'] = True
                print("   ✅ Fetched heart_rate")

        # 2. Activity Metrics (Steps, Calories, Distance, Floors, AZM)
        metrics = {
            'steps': 'activities/steps',
            'calories': 'activities/calories',
            'distance': 'activities/distance',
            'floors': 'activities/floors',
            'active_zone_minutes': 'activities/active-zone-minutes'
        }
        
        activity_updates = {}
        
        for metric_name, endpoint in metrics.items():
            url = f"https://api.fitbit.com/1/user/-/{endpoint}/date/{date_str}/1d.json"
            try:
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    key = f"activities-{metric_name.replace('_', '-')}"
                    if key in data and data[key]:
                        val = data[key][0]['value']
                        
                        # Handle specific formats
                        if metric_name == 'active_zone_minutes':
                            if isinstance(val, dict):
                                val = val.get('activeZoneMinutes', 0)
                            else:
                                val = 0 # Fallback if unexpected format
                        elif metric_name == 'distance':
                            # Convert km to miles
                            val = float(val) * 0.621371
                        else:
                            val = float(val)
                            
                        activity_updates[metric_name] = val
                        fetched_data[metric_name] = True
                        print(f"   ✅ Fetched {metric_name}: {val}")
                else:
                    print(f"   ⚠️ Failed to fetch {metric_name}: Status {response.status_code} - {response.text[:100]}")
            except Exception as e:
                print(f"   ⚠️ Exception fetching {metric_name}: {e}")

        # Batch update cache for activity metrics
        if activity_updates:
            print(f"   💾 Saving activity metrics to cache: {list(activity_updates.keys())}")
            cache.set_daily_metrics(date=date_str, **activity_updates)

        # 3. Weight
        url = f"https://api.fitbit.com/1/user/-/body/log/weight/date/{date_str}/1d.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'weight' in data and data['weight']:
                entry = data['weight'][0]
                weight_kg = entry.get('weight')
                fat = entry.get('fat')
                if weight_kg:
                    weight_lbs = weight_kg * 2.20462
                    cache.set_daily_metrics(date=date_str, weight=weight_lbs, body_fat=fat)
                    fetched_data['weight'] = True
                    print("   ✅ Fetched weight")

        # 4. Advanced Metrics (SpO2, HRV, etc - often only available after sleep sync)
        # SpO2
        url = f"https://api.fitbit.com/1/user/-/spo2/date/{date_str}.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'value' in data:
                avg = data['value'].get('avg')
                eov = data['value'].get('eov') or data['value'].get('variationScore')
                if avg:
                    cache.set_daily_metrics(date=date_str, spo2=avg, eov=eov)
                    fetched_data['spo2'] = True
                    print("   ✅ Fetched spo2")

        # HRV
        url = f"https://api.fitbit.com/1/user/-/hrv/date/{date_str}.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'hrv' in data and data['hrv']:
                val = data['hrv'][0]['value']['dailyRmssd']
                cache.set_advanced_metrics(date=date_str, hrv=val)
                fetched_data['hrv'] = True
                print("   ✅ Fetched hrv")
        
        # Breathing Rate
        url = f"https://api.fitbit.com/1/user/-/br/date/{date_str}.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'br' in data and data['br']:
                val = data['br'][0]['value']['breathingRate']
                cache.set_advanced_metrics(date=date_str, breathing_rate=val)
                fetched_data['breathing_rate'] = True
                print("   ✅ Fetched breathing_rate")
                
        # Temperature
        url = f"https://api.fitbit.com/1/user/-/temp/skin/date/{date_str}.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'tempSkin' in data and data['tempSkin']:
                val = data['tempSkin'][0]['value']
                if isinstance(val, dict):
                    val = val.get('nightlyRelative')
                cache.set_advanced_metrics(date=date_str, temperature=val)
                fetched_data['temperature'] = True
                print("   ✅ Fetched temperature")
                
        # Cardio Fitness (VO2 Max)
        url = f"https://api.fitbit.com/1/user/-/cardioscore/date/{date_str}.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'cardioScore' in data and data['cardioScore']:
                val = data['cardioScore'][0]['value']['vo2Max']
                # Handle ranges
                if isinstance(val, str) and '-' in val:
                    parts = val.split('-')
                    val = (float(parts[0]) + float(parts[1])) / 2
                cache.set_cardio_fitness(date=date_str, vo2_max=float(val))
                fetched_data['cardio_fitness'] = True
                print("   ✅ Fetched cardio_fitness")

        # 5. Sleep
        # Note: Sleep is handled by populate_sleep_score_cache, but we can call it here for completeness
        # or let the main loop handle it. For now, let's just ensure we have the data.
        
        # 6. Activities List
        url = f"https://api.fitbit.com/1/user/-/activities/date/{date_str}.json"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'activities' in data:
                for act in data['activities']:
                    activity_id = str(act.get('logId'))
                    cache.set_activity(
                        activity_id=activity_id,
                        date=date_str,
                        activity_name=act.get('activityName'),
                        duration_ms=act.get('duration'),
                        calories=act.get('calories'),
                        avg_heart_rate=act.get('averageHeartRate'),
                        steps=act.get('steps'),
                        distance=act.get('distance'),
                        activity_data_json=json.dumps(act)
                    )
                fetched_data['activities'] = True
                print(f"   ✅ Fetched {len(data['activities'])} activities")

        return fetched_data

    except Exception as e:
        print(f"⚠️ Error fetching today's stats: {e}")
        return None


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
API_KEY = os.environ.get('API_KEY', '')  # API key for MCP/external access

# API Key authentication decorator
def require_api_key(f):
    """Decorator to require API key for endpoint access"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for API key in header
        provided_key = request.headers.get('X-API-Key')
        
        # If no API key is configured, allow access (backward compatibility)
        if not API_KEY:
            print("⚠️ Warning: API_KEY not configured - API endpoints are unprotected!")
            return f(*args, **kwargs)
        
        # Validate API key
        if provided_key != API_KEY:
            return jsonify({
                'success': False,
                'error': 'Unauthorized - Invalid or missing API key'
            }), 401
        
        return f(*args, **kwargs)
    return decorated_function

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
            print(f"❌ Failed to refresh token. Status: {token_response.status_code}, Response: {token_response_json}")
            return None, None, None
    except Exception as e:
        print(f"❌ Error refreshing token: {e}")
        import traceback
        traceback.print_exc()
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
        
        html.H4("Body Fat % 💪", style={'font-weight': 'bold'}),
        html.H6("Body fat percentage is tracked by Fitbit smart scales (Aria family) and provides insight into body composition beyond just weight. Monitoring body fat % can help track fitness progress and overall health."),
        dcc.Graph(
            id='graph_body_fat',
            figure=px.line(),
            config= {'displaylogo': False}
        ),
        html.Div(id='body_fat_table', style={'max-width': '1200px', 'margin': 'auto', 'font-weight': 'bold'}, children=[]),
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
        html.Div(style={'display': 'flex', 'flex-direction': 'column', 'gap': '20px'}, children=[
            html.Div(style={'width': '100%'}, children=[
                dcc.Graph(id='graph_sleep_score', figure=px.line(), config={'displaylogo': False}),
            ]),
            html.Div(style={'width': '100%'}, children=[
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
        
        # New Exercise Timeline Graph
        dcc.Graph(id='graph_exercise_timeline', figure=px.bar(), config={'displaylogo': False}),
        html.Div(style={"height": '20px'}),
        
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
                html.A("📊 Cache Log Viewer", href="/cache-log", 
                       style={'padding': '10px 20px', 'background-color': '#8e44ad', 'color': 'white', 'text-decoration': 'none', 'border-radius': '5px', 'font-weight': 'bold'}),
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
    Output('oauth-token', 'data', allow_duplicate=True),
    Output('refresh-token', 'data', allow_duplicate=True),
    Output('token-expiry', 'data', allow_duplicate=True),
    Input('workout-date-selector', 'value'),
    State('oauth-token', 'data'),
    State('refresh-token', 'data'),
    State('token-expiry', 'data'),
    prevent_initial_call=True
)
def display_workout_details(selected_date, oauth_token, refresh_token, token_expiry):
    """
    Display detailed workout information including HR zones for selected date.
    🐞 FIX: This function now fetches data directly from the cache.
    🐞 CRITICAL FIX #2: Add token refresh logic before making API calls
    """
    # Default return values (no token update)
    no_update = dash.no_update
    
    if not selected_date or not oauth_token:
        return html.Div("Select a workout date to view details", style={'color': '#999', 'font-style': 'italic'}), no_update, no_update, no_update
    
    # === START CRITICAL FIX #2: REFRESH TOKEN IF NEEDED ===
    current_time = time.time()
    if refresh_token and token_expiry and current_time >= (token_expiry - 300):  # 5 min buffer
        print(f"🔄 [WORKOUT_DETAILS] Token expiring soon, refreshing...")
        try:
            new_access, new_refresh, new_expiry = refresh_access_token(refresh_token)
            if new_access:
                oauth_token = new_access
                # Update session store for main app
                session['access_token'] = new_access
                session['refresh_token'] = new_refresh
                session['token_expiry'] = new_expiry
                cache.store_refresh_token(new_refresh, 28800)  # Store it persistently
                print(f"✅ [WORKOUT_DETAILS] Token refreshed successfully")
                
                # Return new tokens to update client-side stores
                return generate_workout_detail_view(selected_date), new_access, new_refresh, new_expiry
            else:
                print(f"⚠️ [WORKOUT_DETAILS] Token refresh failed")
                return html.Div("Error: Token refresh failed. Please log in again.", style={'color': 'red'}), no_update, no_update, no_update
        except Exception as e:
            print(f"❌ [WORKOUT_DETAILS] Error refreshing token: {e}")
            import traceback
            traceback.print_exc()
            return html.Div(f"Error refreshing token: {e}", style={'color': 'red'}), no_update, no_update, no_update
    # === END CRITICAL FIX #2 ===
    
    return generate_workout_detail_view(selected_date), no_update, no_update, no_update

def generate_workout_detail_view(selected_date):
    """Helper to generate the workout detail view content"""
    # Get stored activity data for the date directly from cache
    activities_from_cache = cache.get_activities(selected_date)
    
    if not activities_from_cache:
        return html.Div(f"No workout data available in cache for {selected_date}", style={'color': '#999'})


    # Reconstruct activities from cache
    activities = []
    for act in activities_from_cache:
        try:
            activity_details = json.loads(act.get('activity_data_json', '{}'))
            if activity_details:
                activities.append(activity_details)
        except (json.JSONDecodeError, TypeError):
            print(f"⚠️ Warning: Could not parse activity data for {selected_date}")
    
    if not activities:
         return html.Div(f"Could not load workout data for {selected_date}", style={'color': '#999'})
    
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
                    html.Strong("Active Duration: "),
                    html.Span(f"{activity.get('activeDuration', 0) // 60000 if activity.get('activeDuration') else 'N/A'} min")
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
    
    # Safety check: return empty table if DataFrame is empty
    if len(df) == 0 or df.empty:
        # Return a DataFrame with the same structure but no data rows
        return pd.DataFrame({
            'Period': [],
            'Average ' + measurement_name: [],
            'Max ' + measurement_name: [],
            'Min ' + measurement_name: []
        })
    
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
    # 🐞 FIX: DO NOT disable submit button - it prevents the update_output callback from firing!
    # The loading spinner in update_output provides visual feedback instead
    return False, False, False

# Fetch data and update graphs on click of submit
def format_duration(minutes):
    """Format minutes into 'Xh Ym' string"""
    if not minutes:
        return "0m"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"

@app.callback(Output('report-title', 'children'), Output('date-range-title', 'children'), Output('generated-on-title', 'children'), Output('graph_RHR', 'figure'), Output('RHR_table', 'children'), Output('graph_steps', 'figure'), Output('graph_steps_heatmap', 'figure'), Output('steps_table', 'children'), Output('graph_activity_minutes', 'figure'), Output('fat_burn_table', 'children'), Output('cardio_table', 'children'), Output('peak_table', 'children'), Output('graph_weight', 'figure'), Output('weight_table', 'children'), Output('graph_body_fat', 'figure'), Output('body_fat_table', 'children'), Output('graph_spo2', 'figure'), Output('spo2_table', 'children'), Output('graph_eov', 'figure'), Output('eov_table', 'children'), Output('graph_sleep', 'figure'), Output('sleep_data_table', 'children'), Output('graph_sleep_regularity', 'figure'), Output('sleep_table', 'children'), Output('sleep-stage-checkbox', 'options'), Output('graph_hrv', 'figure'), Output('hrv_table', 'children'), Output('graph_breathing', 'figure'), Output('breathing_table', 'children'), Output('graph_cardio_fitness', 'figure'), Output('cardio_fitness_table', 'children'), Output('graph_temperature', 'figure'), Output('temperature_table', 'children'), Output('graph_azm', 'figure'), Output('azm_table', 'children'), Output('graph_calories', 'figure'), Output('graph_distance', 'figure'), Output('calories_table', 'children'), Output('graph_floors', 'figure'), Output('floors_table', 'children'), Output('exercise_log_table', 'children'), Output('workout-date-selector', 'options'), Output('graph_sleep_score', 'figure'), Output('graph_sleep_stages_pie', 'figure'), Output('sleep-date-selector', 'options'), Output('graph_exercise_sleep_correlation', 'figure'), Output('graph_azm_sleep_correlation', 'figure'), Output('graph_exercise_timeline', 'figure'), Output('correlation_insights', 'children'), Output("loading-output-1", "children"),
Input('submit-button', 'n_clicks'),
State('my-date-picker-range', 'start_date'), State('my-date-picker-range', 'end_date'), State('oauth-token', 'data'),
prevent_initial_call=True)
def update_output(n_clicks, start_date, end_date, oauth_token):
    print(f"🎯 UPDATE_OUTPUT CALLBACK FIRED! n_clicks={n_clicks}")
    print(f"🎯 start_date={start_date}, end_date={end_date}, oauth_token present={oauth_token is not None}")
    
    # 🐞 FIX: Removed fragile global variables and clear() calls.
    # global exercise_data_store, sleep_detail_data_store
    # exercise_data_store.clear()
    # sleep_detail_data_store.clear()
    
    # Advanced metrics now always enabled with smart caching!
    advanced_metrics_enabled = ['advanced']  # Always enabled

    try:
        start_date = datetime.fromisoformat(start_date).strftime("%Y-%m-%d")
        end_date = datetime.fromisoformat(end_date).strftime("%Y-%m-%d")
    except Exception as e:
        print(f"❌ Error parsing dates: {e}")
        print(f"   start_date type: {type(start_date)}, value: {start_date}")
        print(f"   end_date type: {type(end_date)}, value: {end_date}")
        raise

    if not oauth_token:
        print("❌ No oauth_token in update_output!")
        raise ValueError("No OAuth token provided")

    headers = {
        "Authorization": "Bearer " + oauth_token,
        "Accept": "application/json"
    }

    # === 🚨 FIX: INITIALIZE ALL DATA LISTS FOR FUNCTION-WIDE SCOPE ===
    # These lists must be initialized at the function level to be accessible
    # in ALL code paths (cached, API-fetching, etc.) - prevents UnboundLocalError
    dates_list = []
    rhr_list = []
    fat_burn_minutes_list = []
    cardio_minutes_list = []
    peak_minutes_list = []
    steps_list = []
    weight_list = []
    body_fat_list = []
    spo2_list = []
    eov_list = []
    calories_list = []
    distance_list = []
    floors_list = []
    azm_list = []
    hrv_list = []
    breathing_list = []
    temperature_list = []
    cardio_fitness_list = []
    
    # Sleep data structures
    sleep_record_dict = {}
    sleep_detail_data_store = {}
    deep_sleep_list = []
    light_sleep_list = []
    rem_sleep_list = []
    awake_list = []
    total_sleep_list = []
    sleep_start_times_list = []
    # =================================================================

    # 🚀 CACHE-FIRST CHECK: Verify if ALL data is cached before making ANY API calls
    print(f"📊 Generating report for START: {start_date} to END: {end_date}")
    
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
        daily_data = cache.get_daily_metrics(date_str)
        
        # 🐞 FIX: Advanced metrics are optional - don't fail cache check if missing
        if not sleep_data or not daily_data:
            all_cached = False
            missing_dates.append(date_str)
    

    # 🚨 CRITICAL: Always refresh TODAY's data if it's in the range
    today = datetime.now().strftime('%Y-%m-%d')
    refresh_today = today in dates_str_list
    
    if refresh_today:
        print(f"🔄 TODAY ({today}) in range - fetching real-time stats...")
        # Fetch and cache today's data
        todays_data = fetch_todays_stats(today, oauth_token)
        if todays_data:
            print(f"✅ Today's stats fetched and cached: {list(todays_data.keys())}")
        else:
            print(f"⚠️ Failed to fetch today's stats")
            
    # 🚀 UNIFIED DATA LOADING: Always load from cache!
    # We just refreshed today's data, so now the cache is as up-to-date as possible.
    # This avoids the split logic where "not all_cached" leads to broken API processing.
    
    print(f"✅ Loading all {len(dates_str_list)} days from cache...")
    
    # Populate ALL data from cache
    user_profile = {"user": {"displayName": "Cached User", "firstName": "Cached", "lastName": "User"}}
    
    # Read all daily metrics from cache
    # (Lists already initialized at function level)
    print(f"📖 Reading {len(dates_str_list)} days from cache...")
    for date_str in dates_str_list:
        daily_metrics = cache.get_daily_metrics(date_str)
        
        if daily_metrics:
            # Heart rate data
            rhr_list.append(daily_metrics.get('resting_heart_rate'))
            fat_burn_minutes_list.append(daily_metrics.get('fat_burn_minutes'))
            cardio_minutes_list.append(daily_metrics.get('cardio_minutes'))
            peak_minutes_list.append(daily_metrics.get('peak_minutes'))
            
            # Steps, weight, SpO2
            steps_list.append(daily_metrics.get('steps'))
            weight_list.append(daily_metrics.get('weight'))
            body_fat_list.append(daily_metrics.get('body_fat'))
            spo2_list.append(daily_metrics.get('spo2'))
            
            # Calories, distance, floors, AZM
            calories_list.append(daily_metrics.get('calories'))
            distance_list.append(daily_metrics.get('distance'))
            floors_list.append(daily_metrics.get('floors'))
            azm_list.append(daily_metrics.get('active_zone_minutes'))
        else:
            # Append None if no cache data
            rhr_list.append(None)
            fat_burn_minutes_list.append(None)
            cardio_minutes_list.append(None)
            peak_minutes_list.append(None)
            steps_list.append(None)
            weight_list.append(None)
            body_fat_list.append(None)
            spo2_list.append(None)
            calories_list.append(None)
            distance_list.append(None)
            floors_list.append(None)
            azm_list.append(None)
        
        # Advanced metrics (🐞 FIX #3: Added EOV to cache reading)
        advanced_metrics = cache.get_advanced_metrics(date_str)
        if advanced_metrics:
            hrv_list.append(advanced_metrics.get('hrv'))
            breathing_list.append(advanced_metrics.get('breathing_rate'))
            temperature_list.append(advanced_metrics.get('temperature'))
        else:
            hrv_list.append(None)
            breathing_list.append(None)
            temperature_list.append(None)
        
        # EOV from daily metrics (SpO2 related)
        if daily_metrics:
            eov_list.append(daily_metrics.get('eov'))
        else:
            eov_list.append(None)
        
        # Cardio fitness
        cardio_data = cache.get_cardio_fitness(date_str)
        cardio_fitness_list.append(cardio_data)
        
        # Dates (already in dates_str_list, just append to dates_list)
        dates_list.append(datetime.strptime(date_str, '%Y-%m-%d'))
    
    # Create dummy response structures (won't be used in processing)
    response_heartrate = {"activities-heart": []}
    response_steps = {"activities-steps": []}
    response_weight = {"weight": []}
    response_spo2 = []
    response_calories = {"activities-calories": []}
    response_distance = {"activities-distance": []}
    response_floors = {"activities-floors": []}
    response_azm = {"activities-active-zone-minutes": []}
    response_hrv = {"hrv": []}
    response_breathing = {"br": []}
    response_temperature = {"tempSkin": []}
    response_cardio_fitness = {"cardioScore": []}
    
    # 🐞 FIX: Load activities from cache (CRITICAL - was missing!)
    print("📥 Loading activities from cache...")
    response_activities = {"activities": []}
    total_activities = 0
    
    for date_str in dates_str_list:
        activities_for_date = cache.get_activities(date_str)
        for act in activities_for_date:
            # Try to parse the full activity JSON if available
            try:
                activity_json = act.get('activity_data_json')
                if activity_json:
                    full_activity = json.loads(activity_json)
                    # Use the full activity data from cache
                    response_activities['activities'].append(full_activity)
                else:
                    # Fallback: Reconstruct from basic fields
                    activity_dict = {
                        'logId': act.get('activity_id'),
                        'activityName': act.get('activity_name'),
                        'startTime': f"{date_str}T00:00:00.000",
                        'duration': act.get('duration_ms'),
                        'calories': act.get('calories'),
                        'averageHeartRate': act.get('avg_heart_rate'),
                        'steps': act.get('steps'),
                        'distance': act.get('distance')
                    }
                    response_activities['activities'].append(activity_dict)
                total_activities += 1
            except (json.JSONDecodeError, TypeError) as e:
                print(f"⚠️ Warning: Could not parse activity JSON for {date_str}: {e}")
                # Fallback to basic reconstruction
                activity_dict = {
                    'logId': act.get('activity_id'),
                    'activityName': act.get('activity_name'),
                    'startTime': f"{date_str}T00:00:00.000",
                    'duration': act.get('duration_ms'),
                    'calories': act.get('calories'),
                    'averageHeartRate': act.get('avg_heart_rate'),
                    'steps': act.get('steps'),
                    'distance': act.get('distance')
                }
                response_activities['activities'].append(activity_dict)
                total_activities += 1
    
    print(f"✅ Loaded {total_activities} activities from cache")
    
    # Force all_cached = True to skip the legacy API processing block
    all_cached = True

    
    # 🚨 CRITICAL FIX #4: Always use dates_str_list (the requested date range)
    # Don't rely on API responses since we're not making foreground API calls anymore
    temp_dates_list = dates_str_list
    
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
        
        # 🚨 CRITICAL FIX #4: STOP ALL FOREGROUND API CALLS FOR ADVANCED METRICS
        # The background cache builder will fill in missing data automatically
        total_missing = len(set(missing_hrv + missing_br + missing_temp))
        if total_missing > 0:
            print(f"⚠️ {total_missing} advanced metrics missing from cache (will show as 'No Data').")
            print(f"💡 The background cache builder will populate these automatically.")
            # 🚨 API CALLS REMOVED - Data will show as "No Data" until cache builder populates it
        else:
            print("✅ All advanced metrics loaded from cache - 0 API calls!")
        
        print(f"📊 Total: HRV={len(response_hrv['hrv'])}, BR={len(response_breathing['br'])}, Temp={len(response_temperature['tempSkin'])}")
    else:
        print("ℹ️ Advanced metrics disabled - skipping HRV, Breathing Rate, and Temperature to conserve API calls")
    
    # 🚨 CRITICAL FIX #4: STOP ALL FOREGROUND API CALLS FOR BASIC METRICS
    # Create empty response structures - data will come from cache only
    response_cardio_fitness = {"cardioScore": []}
    response_calories = {"activities-calories": []}
    response_distance = {"activities-distance": []}
    response_floors = {"activities-floors": []}
    response_azm = {"activities-active-zone-minutes": []}
    response_azm = {"activities-active-zone-minutes": []}
    # response_activities = {"activities": []}  <-- REMOVED to preserve cached activities

    # Processing data-----------------------------------------------------------------------------------------------------------------------
    days_name_list = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday','Sunday')
    report_title = "Wellness Report - " + user_profile["user"]["firstName"] + " " + user_profile["user"]["lastName"]
    report_dates_range = datetime.fromisoformat(start_date).strftime("%d %B, %Y") + " – " + datetime.fromisoformat(end_date).strftime("%d %B, %Y")
    generated_on_date = "Report Generated : " + datetime.today().date().strftime("%d %B, %Y")
    
    # 🚀 UNIFIED DATA LOADING: Logic is now fully cache-based above.
    # The legacy API processing block has been removed to prevent list length mismatches.
    # All lists (rhr_list, steps_list, etc.) are populated in the "Read all daily metrics from cache" loop.
    
    print(f"✅ Data processing complete. Using {len(dates_str_list)} days from cache.")


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
                
                # 🐞 FIX: Use reality_score instead of deprecated sleep_score field
                print(f"📊 Using CACHED sleep scores for {date_str}: Reality={cached_data.get('reality_score')}, Proxy={cached_data.get('proxy_score')}, Efficiency={cached_data.get('efficiency', 'N/A')}")
                
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
    
    # 🚨 CRITICAL FIX #4: STOP ALL FOREGROUND API CALLS FOR SLEEP DATA
    # Missing sleep data will show as "No Data" until cache builder populates it
    if missing_dates:
        print(f"⚠️ {len(missing_dates)} sleep dates missing from cache (will show as 'No Data').")
        print(f"💡 The background cache builder will populate these automatically.")

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
    "body_fat": body_fat_list,
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
    # Create weekly steps heatmap (only if we have at least 7 days)
    if len(df_merged) >= 7:
        weekly_steps_array = np.array([0]*days_name_list.index(datetime.fromisoformat(start_date).strftime('%A')) + df_merged["Steps Count"].to_list() + [0]*(6 - days_name_list.index(datetime.fromisoformat(end_date).strftime('%A'))))
        weekly_steps_array = np.transpose(weekly_steps_array.reshape((int(len(weekly_steps_array)/7), 7)))
        weekly_steps_array = pd.DataFrame(weekly_steps_array, index=days_name_list)
    else:
        # If less than 7 days, create a simple single-column DataFrame
        weekly_steps_array = pd.DataFrame([[0]], index=['Monday'])

    # Plotting data-----------------------------------------------------------------------------------------------------------------------

    fig_rhr = px.line(df_merged, x="Date", y="Resting Heart Rate", line_shape="spline", color_discrete_sequence=["#d30f1c"], title=f"<b>Daily Resting Heart Rate<br><br><sup>Overall average : {rhr_avg['overall']} bpm | Last 30d average : {rhr_avg['30d']} bpm</sup></b><br><br><br>")
    if df_merged["Resting Heart Rate"].dtype != object and df_merged["Resting Heart Rate"].notna().any():
        fig_rhr.add_annotation(x=df_merged.iloc[df_merged["Resting Heart Rate"].idxmax()]["Date"], y=df_merged["Resting Heart Rate"].max(), text=str(df_merged["Resting Heart Rate"].max()), showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
        fig_rhr.add_annotation(x=df_merged.iloc[df_merged["Resting Heart Rate"].idxmin()]["Date"], y=df_merged["Resting Heart Rate"].min(), text=str(df_merged["Resting Heart Rate"].min()), showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    fig_rhr.add_hline(y=df_merged["Resting Heart Rate"].mean(), line_dash="dot",annotation_text="Average : " + str(round(df_merged["Resting Heart Rate"].mean(), 1)) + " BPM", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    fig_rhr.add_hrect(y0=62, y1=68, fillcolor="green", opacity=0.15, line_width=0)
    rhr_summary_df = calculate_table_data(df_merged, "Resting Heart Rate")
    rhr_summary_table = dash_table.DataTable(rhr_summary_df.to_dict('records'), [{"name": i, "id": i} for i in rhr_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#5f040a','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    fig_steps = px.bar(df_merged, x="Date", y="Steps Count", color_discrete_sequence=["#2fb376"], title=f"<b>Daily Steps Count<br><br><sup>Overall average : {steps_avg['overall']} steps | Last 30d average : {steps_avg['30d']} steps</sup></b><br><br><br>")
    if df_merged["Steps Count"].dtype != object and df_merged["Steps Count"].notna().any():
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
    # Get most recent and earliest weight for summary header
    weight_with_data = df_merged[df_merged["weight"].notna()].sort_values("Date", ascending=False)
    if not weight_with_data.empty:
        most_recent_weight = weight_with_data.iloc[0]["weight"]
        most_recent_date = weight_with_data.iloc[0]["Date"].strftime("%m/%d")
        earliest_weight = weight_with_data.iloc[-1]["weight"]
        earliest_date = weight_with_data.iloc[-1]["Date"].strftime("%m/%d")
        weight_change = round(most_recent_weight - earliest_weight, 1)
        change_symbol = "📉" if weight_change < 0 else "📈" if weight_change > 0 else "➡️"
        weight_header_text = f"<b>Weight<br><br><sup>📊 Most Recent: {most_recent_weight} lbs ({most_recent_date}) | Earliest: {earliest_weight} lbs ({earliest_date}) | Change: {change_symbol} {weight_change:+.1f} lbs<br>Overall avg: {weight_avg['overall']} lbs | Last 30d avg: {weight_avg['30d']} lbs</sup></b><br><br>"
    else:
        weight_header_text = f"<b>Weight<br><br><sup>Overall average : {weight_avg['overall']} lbs | Last 30d average : {weight_avg['30d']} lbs</sup></b><br><br><br>"
    
    fig_weight = px.line(df_merged, x="Date", y="weight", line_shape="spline", color_discrete_sequence=["#6b3908"], title=weight_header_text, labels={"weight": "Weight (lbs)"})
    # Safety check: Only add annotations if we have valid weight data
    if df_merged["weight"].dtype != object and df_merged["weight"].notna().any() and len(df_merged[df_merged["weight"].notna()]) > 0:
        valid_weight = df_merged[df_merged["weight"].notna()]
        if len(valid_weight) > 0:
            fig_weight.add_annotation(x=valid_weight.loc[valid_weight["weight"].idxmax(), "Date"], y=valid_weight["weight"].max(), text=str(valid_weight["weight"].max()) + " lbs", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
            fig_weight.add_annotation(x=valid_weight.loc[valid_weight["weight"].idxmin(), "Date"], y=valid_weight["weight"].min(), text=str(valid_weight["weight"].min()) + " lbs", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    if df_merged["weight"].notna().any() and len(df_merged[df_merged["weight"].notna()]) > 0:
        fig_weight.add_hline(y=round(df_merged["weight"].mean(),1), line_dash="dot",annotation_text="Average : " + str(round(df_merged["weight"].mean(), 1)) + " lbs", annotation_position="bottom right", annotation_bgcolor="#6b3908", annotation_opacity=0.6, annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
    weight_summary_df = calculate_table_data(df_merged, "weight")
    weight_summary_table = dash_table.DataTable(weight_summary_df.to_dict('records'), [{"name": i, "id": i} for i in weight_summary_df.columns], style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], style_header={'backgroundColor': '#4c3b7d','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, style_cell={'textAlign': 'center'})
    
    # Body Fat % Chart
    body_fat_avg = {'overall': safe_avg(df_merged["body_fat"].mean(), 1), '30d': safe_avg(df_merged["body_fat"].tail(30).mean(), 1)}
    if df_merged["body_fat"].notna().any() and df_merged["body_fat"].sum() > 0:
        # Get most recent and earliest body fat for summary header
        body_fat_with_data = df_merged[df_merged["body_fat"].notna()].sort_values("Date", ascending=False)
        if not body_fat_with_data.empty:
            most_recent_bf = body_fat_with_data.iloc[0]["body_fat"]
            most_recent_bf_date = body_fat_with_data.iloc[0]["Date"].strftime("%m/%d")
            earliest_bf = body_fat_with_data.iloc[-1]["body_fat"]
            earliest_bf_date = body_fat_with_data.iloc[-1]["Date"].strftime("%m/%d")
            bf_change = round(most_recent_bf - earliest_bf, 1)
            bf_change_symbol = "📉" if bf_change < 0 else "📈" if bf_change > 0 else "➡️"
            body_fat_header_text = f"<b>Body Fat %<br><br><sup>💪 Most Recent: {most_recent_bf}% ({most_recent_bf_date}) | Earliest: {earliest_bf}% ({earliest_bf_date}) | Change: {bf_change_symbol} {bf_change:+.1f}%<br>Overall avg: {body_fat_avg['overall']}% | Last 30d avg: {body_fat_avg['30d']}%</sup></b><br><br>"
        else:
            body_fat_header_text = f"<b>Body Fat %<br><br><sup>Overall average : {body_fat_avg['overall']}% | Last 30d average : {body_fat_avg['30d']}%</sup></b><br><br><br>"
        
        fig_body_fat = px.line(df_merged, x="Date", y="body_fat", line_shape="spline", color_discrete_sequence=["#2c3e50"], 
                              title=body_fat_header_text, 
                              labels={"body_fat": "Body Fat (%)"})
        if df_merged["body_fat"].dtype != object and df_merged["body_fat"].notna().any():
            valid_body_fat = df_merged[df_merged["body_fat"].notna()]
            # Only add annotations if we have at least one valid data point
            if len(valid_body_fat) > 0:
                # Get the max/min indices within the valid_body_fat dataframe
                max_idx = valid_body_fat["body_fat"].idxmax()
                min_idx = valid_body_fat["body_fat"].idxmin()
                
                # Use .loc[] to safely access the row
                fig_body_fat.add_annotation(x=valid_body_fat.loc[max_idx, "Date"], 
                                           y=df_merged["body_fat"].max(), text=str(round(df_merged["body_fat"].max(), 1)) + "%", 
                                           showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, 
                                           font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
                fig_body_fat.add_annotation(x=valid_body_fat.loc[min_idx, "Date"], 
                                           y=df_merged["body_fat"].min(), text=str(round(df_merged["body_fat"].min(), 1)) + "%", 
                                           showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, 
                                           font=dict(family="Helvetica, monospace", size=12, color="#ffffff"))
                fig_body_fat.add_hline(y=df_merged["body_fat"].mean(), line_dash="dot",
                                      annotation_text="Average : " + str(safe_avg(df_merged["body_fat"].mean(), 1)) + "%", 
                                      annotation_position="bottom right", annotation_bgcolor="#2c3e50", annotation_opacity=0.6, 
                                      annotation_borderpad=5, annotation_font=dict(family="Helvetica, monospace", size=14, color="#ffffff"))
        body_fat_summary_df = calculate_table_data(df_merged, "body_fat")
        body_fat_summary_table = dash_table.DataTable(body_fat_summary_df.to_dict('records'), [{"name": i, "id": i} for i in body_fat_summary_df.columns], 
                                                     style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], 
                                                     style_header={'backgroundColor': '#34495e','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, 
                                                     style_cell={'textAlign': 'center'})
    else:
        fig_body_fat = {}
        body_fat_summary_table = html.P("No body fat % data available", style={'text-align': 'center', 'color': '#888'})
    fig_spo2 = px.scatter(df_merged, x="Date", y="SPO2", color_discrete_sequence=["#983faa"], title=f"<b>SPO2 Percentage<br><br><sup>Overall average : {spo2_avg['overall']}% | Last 30d average : {spo2_avg['30d']}% </sup></b><br><br><br>", range_y=(90,100), labels={'SPO2':"SpO2(%)"})
    # Safety check: Only add annotations if we have valid SpO2 data
    if df_merged["SPO2"].dtype != object and df_merged["SPO2"].notna().any() and len(df_merged[df_merged["SPO2"].notna()]) > 0:
        valid_spo2 = df_merged[df_merged["SPO2"].notna()]
        if len(valid_spo2) > 0:
            fig_spo2.add_annotation(x=valid_spo2.loc[valid_spo2["SPO2"].idxmax(), "Date"], y=valid_spo2["SPO2"].max(), text=str(valid_spo2["SPO2"].max())+"%", showarrow=False, arrowhead=0, bgcolor="#5f040a", opacity=0.80, yshift=15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
            fig_spo2.add_annotation(x=valid_spo2.loc[valid_spo2["SPO2"].idxmin(), "Date"], y=valid_spo2["SPO2"].min(), text=str(valid_spo2["SPO2"].min())+"%", showarrow=False, arrowhead=0, bgcolor="#0b2d51", opacity=0.80, yshift=-15, borderpad=5, font=dict(family="Helvetica, monospace", size=12, color="#ffffff"), )
    if df_merged["SPO2"].notna().any() and len(df_merged[df_merged["SPO2"].notna()]) > 0:
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
    if len(dates_str_list) > 0:
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
    
    # 🐞 FIX: Load activities from cache if in cache-only mode
    if all_cached and not refresh_today:
        print("📦 Loading activities from cache...")
        cached_activities = cache.get_activities_in_range(start_date, end_date)
        if cached_activities:
            response_activities = {"activities": cached_activities}
            print(f"✅ Loaded {len(cached_activities)} activities from cache")
        else:
            print("⚠️ No cached activities found for this date range")
    
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
                    'Active Duration (min)': activity.get('activeDuration', 0) // 60000 if activity.get('activeDuration') else 'N/A',
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
                        activity_data_json=json.dumps(activity)
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
            data=exercise_df.to_dict('records'), 
            columns=[{"name": i, "id": i} for i in exercise_df.columns], 
            style_data_conditional=[{'if': {'row_index': 'odd'},'backgroundColor': 'rgb(248, 248, 248)'}], 
            style_header={'backgroundColor': '#336699','fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'}, 
            style_cell={'textAlign': 'center'},
            page_size=20,
            filter_action='native',  # Enable built-in filtering
            sort_action='native',     # Enable built-in sorting
            export_format='csv',  # Enable CSV export
            export_headers='display'
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
        if cached_sleep:
            # Use Reality Score as primary metric (most accurate calculated score)
            reality_score = cached_sleep.get('reality_score')
            proxy_score = cached_sleep.get('proxy_score')
            efficiency = cached_sleep.get('efficiency')
            
            if reality_score is not None:
                print(f"📊 Using CACHED sleep scores for {date_str}: Reality={reality_score}, Proxy={proxy_score}, Efficiency={efficiency}")
                sleep_scores.append({
                    'Date': date_str, 
                    'Score': reality_score,  # PRIMARY: Reality Score
                    'Proxy_Score': proxy_score,
                    'Efficiency': efficiency
                })
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
    
    # Sleep Score Chart - 3-Tier System
    if sleep_scores:
        import plotly.graph_objects as go
        sleep_score_df = pd.DataFrame(sleep_scores)
        
        fig_sleep_score = go.Figure()
        
        # Reality Score (PRIMARY - Bold line)
        fig_sleep_score.add_trace(go.Scatter(
            x=sleep_score_df['Date'], 
            y=sleep_score_df['Score'],
            mode='lines+markers',
            name='Reality Score (Primary)',
            line=dict(color='#e74c3c', width=3),
            marker=dict(size=8),
            hovertemplate='%{x}<br>Reality Score: %{y}<extra></extra>'
        ))
        
        # Proxy Score (Fitbit Approximation)
        fig_sleep_score.add_trace(go.Scatter(
            x=sleep_score_df['Date'], 
            y=sleep_score_df['Proxy_Score'],
            mode='lines+markers',
            name='Proxy Score (Fitbit Match)',
            line=dict(color='#3498db', width=2, dash='dash'),
            marker=dict(size=6),
            hovertemplate='%{x}<br>Proxy Score: %{y}<extra></extra>'
        ))
        
        # Efficiency (API Raw)
        fig_sleep_score.add_trace(go.Scatter(
            x=sleep_score_df['Date'], 
            y=sleep_score_df['Efficiency'],
            mode='lines+markers',
            name='Efficiency % (API)',
            line=dict(color='#95a5a6', width=1.5, dash='dot'),
            marker=dict(size=5),
            hovertemplate='%{x}<br>Efficiency: %{y}%<extra></extra>'
        ))
        
        fig_sleep_score.update_layout(
            title='Sleep Quality Score - 3-Tier System (0-100)<br><sub>Reality Score (Red) is the primary metric - most accurate assessment</sub>',
            yaxis_range=[0, 100],
            yaxis_title='Score',
            xaxis_title='Date',
            hovermode='x unified',
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        
        # Reference lines
        fig_sleep_score.add_hline(y=90, line_dash="dot", line_color="green", 
                                   annotation_text="Excellent (90+)", annotation_position="right")
        fig_sleep_score.add_hline(y=80, line_dash="dot", line_color="lightgreen", 
                                   annotation_text="Good (80+)", annotation_position="right")
        fig_sleep_score.add_hline(y=60, line_dash="dot", line_color="orange", 
                                   annotation_text="Fair (60+)", annotation_position="right")
    else:
        fig_sleep_score = px.line(title='Sleep Quality Score (No Data)')
    
    # Sleep Stages Pie Chart
    if sum(sleep_stages_totals.values()) > 0:
        stages_df = pd.DataFrame([{'Stage': k, 'Minutes': v} for k, v in sleep_stages_totals.items() if v > 0])
        
        # Add formatted duration column for hover
        stages_df['Duration'] = stages_df['Minutes'].apply(lambda x: f"{x // 60}h {x % 60}m")
        
        fig_sleep_stages_pie = px.pie(stages_df, values='Minutes', names='Stage',
                                       title='Average Sleep Stage Distribution',
                                       color='Stage',
                                       color_discrete_map={'Deep': '#084466', 'Light': '#1e9ad6', 
                                                          'REM': '#4cc5da', 'Wake': '#fd7676'})
        
        # Custom hover template with formatted duration
        fig_sleep_stages_pie.update_traces(
            customdata=stages_df[['Duration']].values,
            hovertemplate='<b>%{label}</b><br>Duration: %{customdata[0]}<br>%{percent}<extra></extra>'
        )
    else:
        fig_sleep_stages_pie = px.pie(title='Sleep Stages (No Data)')
    
    # Create Sleep Data Table with 3-Tier Scores
    sleep_data_rows = []
    for date_str in dates_str_list:
        cached_sleep = cache.get_sleep_data(date_str)
        if cached_sleep and cached_sleep.get('reality_score') is not None:
            reality_score = cached_sleep.get('reality_score', 'N/A')
            proxy_score = cached_sleep.get('proxy_score', 'N/A')
            efficiency = cached_sleep.get('efficiency', 'N/A')
            
            # Determine rating based on Reality Score
            if reality_score >= 90:
                rating = "Excellent"
            elif reality_score >= 80:
                rating = "Good"
            elif reality_score >= 60:
                rating = "Fair"
            else:
                rating = "Poor"
            
            sleep_data_rows.append({
                'Date': date_str,
                'Reality Score': reality_score,
                'Rating': rating,
                'Proxy Score': proxy_score,
                'Efficiency %': efficiency,
                'Deep Sleep (min)': cached_sleep.get('deep', 0),
                'REM Sleep (min)': cached_sleep.get('rem', 0),
                'Light Sleep (min)': cached_sleep.get('light', 0),
                'Awake (min)': cached_sleep.get('wake', 0)
            })
    
    if sleep_data_rows:
        sleep_data_df = pd.DataFrame(sleep_data_rows)
        sleep_data_table_output = dash_table.DataTable(
            data=sleep_data_df.to_dict('records'),
            columns=[{"name": i, "id": i} for i in sleep_data_df.columns],
            style_data_conditional=[
                {'if': {'row_index': 'odd'}, 'backgroundColor': 'rgb(248, 248, 248)'},
                # Color code ratings
                {'if': {'filter_query': '{Rating} = "Excellent"', 'column_id': 'Rating'}, 
                 'backgroundColor': '#4caf50', 'color': 'white', 'fontWeight': 'bold'},
                {'if': {'filter_query': '{Rating} = "Good"', 'column_id': 'Rating'}, 
                 'backgroundColor': '#8bc34a', 'color': 'white', 'fontWeight': 'bold'},
                {'if': {'filter_query': '{Rating} = "Fair"', 'column_id': 'Rating'}, 
                 'backgroundColor': '#ff9800', 'color': 'white', 'fontWeight': 'bold'},
                {'if': {'filter_query': '{Rating} = "Poor"', 'column_id': 'Rating'}, 
                 'backgroundColor': '#f44336', 'color': 'white', 'fontWeight': 'bold'},
            ],
            style_header={'backgroundColor': '#336699', 'fontWeight': 'bold', 'color': 'white', 'fontSize': '14px'},
            style_cell={'textAlign': 'center'},
            page_size=10,
            export_format='csv',
            export_headers='display',
            sort_action='native',
            filter_action='native'
        )
    else:
        sleep_data_table_output = html.P("No sleep data available for this period.", style={'text-align': 'center', 'color': '#888'})
    
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
                    html.P(f"💪 Average sleep after workout days: {format_duration(avg_exercise_sleep)}" if not pd.isna(avg_exercise_sleep) else ""),
                    html.P(f"😴 Average sleep on rest days: {format_duration(avg_no_exercise_sleep)}" if not pd.isna(avg_no_exercise_sleep) else ""),
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
        sleep_score = None
        
        if cached_sleep:
            # Try Reality Score first, then Proxy, then Efficiency
            sleep_score = cached_sleep.get('reality_score')
            if sleep_score is None:
                sleep_score = cached_sleep.get('proxy_score')
            if sleep_score is None:
                sleep_score = cached_sleep.get('efficiency')
                
        elif date_str in sleep_record_dict:
             sleep_score = sleep_record_dict[date_str].get('sleep_score')
             
        if sleep_score is not None:
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

    # Populate activities_by_date for the timeline graph
    activities_by_date = {}
    if response_activities and 'activities' in response_activities:
        for act in response_activities['activities']:
            try:
                start_time = act.get('startTime')
                if start_time:
                    # Parse date from startTime (e.g., "2023-10-27T14:30:00.000")
                    date_str = start_time.split('T')[0]
                    if date_str not in activities_by_date:
                        activities_by_date[date_str] = []
                    
                    # Wrap in expected format for Phase 6 logic
                    activities_by_date[date_str].append({
                        'activity_data_json': json.dumps(act)
                    })
            except Exception as e:
                print(f"Error grouping activity for timeline: {e}")

    # Phase 6: Exercise Timeline (Calories vs Sleep Score)
    exercise_timeline_data = []
    for date_str in dates_str_list:
        # Get activities
        activities = activities_by_date.get(date_str, [])
        total_cals = 0
        has_exercise = False
        
        for act in activities:
            try:
                act_data = json.loads(act.get('activity_data_json', '{}'))
                if act_data:
                    total_cals += act_data.get('calories', 0)
                    has_exercise = True
            except:
                pass
        
        # Get Next Day Sleep Score
        try:
            next_day = (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
            cached_sleep = cache.get_sleep_data(next_day)
            sleep_score = None
            if cached_sleep:
                 sleep_score = cached_sleep.get('reality_score')
            
            if has_exercise or sleep_score is not None:
                exercise_timeline_data.append({
                    'Date': date_str,
                    'Exercise Calories': total_cals,
                    'Next Day Sleep Score': sleep_score if sleep_score else 0
                })
        except Exception as e:
            print(f"Error processing timeline for {date_str}: {e}")
            
    if exercise_timeline_data:
        et_df = pd.DataFrame(exercise_timeline_data)
        fig_exercise_timeline = go.Figure()
        
        # Bar for Calories
        fig_exercise_timeline.add_trace(go.Bar(
            x=et_df['Date'],
            y=et_df['Exercise Calories'],
            name='Exercise Calories',
            marker_color='#e67e22'
        ))
        
        # Line for Sleep Score
        fig_exercise_timeline.add_trace(go.Scatter(
            x=et_df['Date'],
            y=et_df['Next Day Sleep Score'],
            name='Next Day Sleep Score',
            yaxis='y2',
            mode='lines+markers',
            line=dict(color='#2c3e50', width=3)
        ))
        
        fig_exercise_timeline.update_layout(
            title='Exercise Intensity vs Next Day Sleep Quality',
            yaxis=dict(title='Calories Burned'),
            yaxis2=dict(title='Sleep Score', overlaying='y', side='right', range=[0, 100]),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode='x unified'
        )
    else:
        fig_exercise_timeline = px.bar(title='No Exercise/Sleep Data for Timeline')

    # 🐞 FIX: Serialize the collected activity data to JSON for the dcc.Store
    exercise_data_json = json.dumps(activities_by_date)

    return report_title, report_dates_range, generated_on_date, fig_rhr, rhr_summary_table, fig_steps, fig_steps_heatmap, steps_summary_table, fig_activity_minutes, fat_burn_summary_table, cardio_summary_table, peak_summary_table, fig_weight, weight_summary_table, fig_body_fat, body_fat_summary_table, fig_spo2, spo2_summary_table, fig_eov, eov_summary_table, fig_sleep_minutes, sleep_data_table_output, fig_sleep_regularity, sleep_summary_table, [{'label': 'Color Code Sleep Stages', 'value': 'Color Code Sleep Stages','disabled': False}], fig_hrv, hrv_summary_table, fig_breathing, breathing_summary_table, fig_cardio_fitness, cardio_fitness_summary_table, fig_temperature, temperature_summary_table, fig_azm, azm_summary_table, fig_calories, fig_distance, calories_summary_table, fig_floors, floors_summary_table, exercise_log_table, workout_dates_for_dropdown, fig_sleep_score, fig_sleep_stages_pie, sleep_dates_for_dropdown, fig_correlation, fig_azm_sleep_correlation, fig_exercise_timeline, correlation_insights, ""

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
@require_api_key
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
@require_api_key
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
@require_api_key
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

@server.route('/api/data/range', methods=['GET'])
@require_api_key
def api_get_data_range():
    """Get cached data for a date range - optimized for MCP server
    
    Query Parameters:
        start (required): Start date (YYYY-MM-DD)
        end (required): End date (YYYY-MM-DD)
        metrics (optional): Comma-separated list of metric types
                           Options: daily, sleep, advanced, cardio, activities
                           Default: all metrics
    
    Example: /api/data/range?start=2025-10-01&end=2025-10-31&metrics=daily,sleep
    """
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    metrics_str = request.args.get('metrics', 'daily,sleep,advanced,cardio,activities')
    
    if not start_date or not end_date:
        return jsonify({
            'success': False,
            'error': 'Missing required parameters: start and end dates'
        }), 400
    
    # Validate date format
    try:
        from datetime import datetime, timedelta
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        if start > end:
            return jsonify({
                'success': False,
                'error': 'Start date must be before or equal to end date'
            }), 400
        
        # Limit to 365 days to prevent abuse
        if (end - start).days > 365:
            return jsonify({
                'success': False,
                'error': 'Date range cannot exceed 365 days'
            }), 400
            
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': 'Invalid date format. Use YYYY-MM-DD'
        }), 400
    
    selected_metrics = set(metrics_str.split(','))
    
    try:
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        # Generate date range
        dates = [(start + timedelta(days=x)).strftime('%Y-%m-%d') 
                 for x in range((end - start).days + 1)]
        
        data = []
        
        for date in dates:
            day_data = {'date': date}
            
            # Daily Metrics
            if 'daily' in selected_metrics:
                cursor.execute('''
                    SELECT steps, calories, distance, floors, active_zone_minutes,
                           resting_heart_rate, fat_burn_minutes, cardio_minutes, peak_minutes,
                           weight, body_fat, spo2, eov
                    FROM daily_metrics_cache WHERE date = ?
                ''', (date,))
                daily = cursor.fetchone()
                
                if daily:
                    day_data['daily'] = {
                        'steps': daily[0],
                        'calories': daily[1],
                        'distance_km': daily[2],
                        'distance_mi': round(daily[2] * 0.621371, 2) if daily[2] else None,
                        'floors': daily[3],
                        'active_zone_minutes': daily[4],
                        'resting_heart_rate': daily[5],
                        'fat_burn_minutes': daily[6],
                        'cardio_minutes': daily[7],
                        'peak_minutes': daily[8],
                        'weight_lbs': daily[9],
                        'body_fat_pct': daily[10],
                        'spo2': daily[11],
                        'eov': daily[12]
                    }
            
            # Sleep Metrics
            if 'sleep' in selected_metrics:
                cursor.execute('''
                    SELECT sleep_score, efficiency, proxy_score, reality_score,
                           total_sleep, deep_minutes, light_minutes, rem_minutes, wake_minutes,
                           start_time
                    FROM sleep_cache WHERE date = ?
                ''', (date,))
                sleep = cursor.fetchone()
                
                if sleep:
                    day_data['sleep'] = {
                        'fitbit_score': sleep[0],
                        'efficiency': sleep[1],
                        'proxy_score': sleep[2],
                        'reality_score': sleep[3],
                        'total_minutes': sleep[4],
                        'deep_minutes': sleep[5],
                        'light_minutes': sleep[6],
                        'rem_minutes': sleep[7],
                        'wake_minutes': sleep[8],
                        'start_time': sleep[9]
                    }
            
            # Advanced Metrics
            if 'advanced' in selected_metrics:
                cursor.execute('''
                    SELECT hrv, breathing_rate, temperature
                    FROM advanced_metrics_cache WHERE date = ?
                ''', (date,))
                advanced = cursor.fetchone()
                
                if advanced:
                    day_data['advanced'] = {
                        'hrv_ms': advanced[0],
                        'breathing_rate_bpm': advanced[1],
                        'temperature_f': advanced[2]
                    }
            
            # Cardio Fitness
            if 'cardio' in selected_metrics:
                cursor.execute('''
                    SELECT vo2_max FROM cardio_fitness_cache WHERE date = ?
                ''', (date,))
                cardio = cursor.fetchone()
                
                if cardio and cardio[0]:
                    day_data['cardio'] = {
                        'vo2_max': cardio[0]
                    }
            
            # Activities
            if 'activities' in selected_metrics:
                cursor.execute('''
                    SELECT activity_id, activity_name, duration_ms, calories, 
                           avg_heart_rate, steps, distance, activity_data_json
                    FROM activities_cache WHERE date = ?
                ''', (date,))
                activities = cursor.fetchall()
                
                if activities:
                    day_data['activities'] = []
                    for act in activities:
                        activity = {
                            'activity_id': act[0],
                            'name': act[1],
                            'duration_minutes': act[2] // 60000 if act[2] else None,
                            'calories': act[3],
                            'avg_heart_rate': act[4],
                            'steps': act[5],
                            'distance_km': act[6],
                            'distance_mi': round(act[6] * 0.621371, 2) if act[6] else None
                        }
                        
                        # Extract active duration from JSON if available
                        try:
                            import json
                            activity_json = json.loads(act[7]) if act[7] else {}
                            activity['active_duration_minutes'] = activity_json.get('activeDuration', 0) // 60000 if activity_json.get('activeDuration') else None
                        except:
                            pass
                        
                        day_data['activities'].append(activity)
            
            data.append(day_data)
        
        conn.close()
        
        return jsonify({
            'success': True,
            'start_date': start_date,
            'end_date': end_date,
            'total_days': len(data),
            'metrics_included': list(selected_metrics),
            'data': data
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@server.route('/api/health', methods=['GET'])
def api_health():
    """Health check endpoint"""
    return jsonify({
        'success': True,
        'status': 'healthy',
        'app': 'Fitbit Wellness Enhanced',
        'version': '2.0.0-cache'
    })

# ========================================
# Cache Log Viewer Page
# ========================================
@server.route('/cache-log')
def cache_log_page():
    """Cache Log Viewer - Interactive page to view cached data"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cache Log Viewer - Fitbit Wellness</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container {
                max-width: 1400px;
                margin: 0 auto;
                background: white;
                border-radius: 15px;
                padding: 30px;
                box-shadow: 0 10px 50px rgba(0,0,0,0.2);
            }
            h1 {
                color: #667eea;
                text-align: center;
                margin-bottom: 10px;
                font-size: 2.5em;
            }
            .subtitle {
                text-align: center;
                color: #666;
                margin-bottom: 30px;
                font-size: 1.1em;
            }
            .back-link {
                display: inline-block;
                margin-bottom: 20px;
                color: #667eea;
                text-decoration: none;
                font-weight: bold;
                padding: 10px 20px;
                border: 2px solid #667eea;
                border-radius: 8px;
                transition: all 0.3s;
            }
            .back-link:hover {
                background: #667eea;
                color: white;
            }
            .controls {
                background: #f8f9fa;
                padding: 25px;
                border-radius: 10px;
                margin-bottom: 25px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                font-weight: bold;
                margin-bottom: 8px;
                color: #333;
            }
            input[type="date"] {
                padding: 10px;
                border: 2px solid #ddd;
                border-radius: 6px;
                font-size: 16px;
                width: 200px;
                margin-right: 15px;
            }
            input[type="date"]:focus {
                outline: none;
                border-color: #667eea;
            }
            .metrics-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-bottom: 20px;
            }
            .metric-checkbox {
                background: white;
                padding: 12px;
                border: 2px solid #ddd;
                border-radius: 8px;
                cursor: pointer;
                transition: all 0.2s;
            }
            .metric-checkbox:hover {
                border-color: #667eea;
                background: #f0f4ff;
            }
            .metric-checkbox input {
                margin-right: 8px;
                cursor: pointer;
            }
            .metric-checkbox label {
                cursor: pointer;
                margin: 0;
                font-weight: normal;
            }
            button {
                background: #667eea;
                color: white;
                border: none;
                padding: 12px 30px;
                border-radius: 8px;
                font-size: 16px;
                font-weight: bold;
                cursor: pointer;
                margin-right: 10px;
                transition: all 0.3s;
            }
            button:hover {
                background: #5568d3;
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.3);
            }
            button.secondary {
                background: #28a745;
            }
            button.secondary:hover {
                background: #218838;
            }
            button.tertiary {
                background: #dc3545;
            }
            button.tertiary:hover {
                background: #c82333;
            }
            #output {
                background: #f8f9fa;
                border: 2px solid #ddd;
                border-radius: 10px;
                padding: 20px;
                min-height: 400px;
                font-family: 'Courier New', monospace;
                font-size: 14px;
                white-space: pre-wrap;
                overflow-x: auto;
                line-height: 1.6;
            }
            #output:empty::before {
                content: 'Select date range and metrics, then click "Generate Report" to view cache data...';
                color: #999;
                font-style: italic;
            }
            .loading {
                text-align: center;
                color: #667eea;
                font-size: 18px;
                padding: 40px;
            }
            .error {
                color: #dc3545;
                font-weight: bold;
            }
            .success {
                color: #28a745;
                font-weight: bold;
            }
            .select-all-container {
                margin-bottom: 15px;
                padding: 12px;
                background: #e3f2fd;
                border-radius: 8px;
            }
            .select-all-container input {
                margin-right: 8px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back-link">← Back to Dashboard</a>
            
            <h1>📊 Cache Log Viewer</h1>
            <p class="subtitle">View and download cached Fitbit data for any date range</p>
            
            <div class="controls">
                <div class="form-group">
                    <label>📅 Select Date Range:</label>
                    <input type="date" id="startDate" value="2025-10-20">
                    <input type="date" id="endDate" value="2025-10-25">
                </div>
                
                <div class="form-group">
                    <label>🎯 Select Metrics to Display:</label>
                    
                    <div class="select-all-container">
                        <input type="checkbox" id="selectAll" checked>
                        <label for="selectAll" style="display: inline; font-weight: bold; color: #667eea;">Select All</label>
                    </div>
                    
                    <div class="metrics-grid">
                        <div class="metric-checkbox">
                            <input type="checkbox" id="metric_daily" value="daily" checked>
                            <label for="metric_daily">📊 Daily Metrics (Steps, Calories, etc.)</label>
                        </div>
                        <div class="metric-checkbox">
                            <input type="checkbox" id="metric_sleep" value="sleep" checked>
                            <label for="metric_sleep">😴 Sleep Data</label>
                        </div>
                        <div class="metric-checkbox">
                            <input type="checkbox" id="metric_advanced" value="advanced" checked>
                            <label for="metric_advanced">💚 Advanced Metrics (HRV, BR, Temp)</label>
                        </div>
                        <div class="metric-checkbox">
                            <input type="checkbox" id="metric_cardio" value="cardio" checked>
                            <label for="metric_cardio">🏃 Cardio Fitness (VO2 Max)</label>
                        </div>
                        <div class="metric-checkbox">
                            <input type="checkbox" id="metric_activities" value="activities" checked>
                            <label for="metric_activities">🏋️ Activities/Exercises</label>
                        </div>
                    </div>
                </div>
                
                <div style="margin-top: 20px;">
                    <button onclick="generateReport()">🔍 Generate Report</button>
                    <button class="secondary" onclick="downloadReport()">💾 Download as Text</button>
                    <button class="secondary" onclick="downloadCSV()" style="background: #17a2b8;">📊 Export CSV</button>
                    <button class="tertiary" onclick="clearOutput()">🗑️ Clear Output</button>
                </div>
            </div>
            
            <div id="output"></div>
        </div>
        
        <script>
            // Select All functionality
            document.getElementById('selectAll').addEventListener('change', function() {
                const checkboxes = document.querySelectorAll('.metric-checkbox input[type="checkbox"]');
                checkboxes.forEach(cb => cb.checked = this.checked);
            });
            
            // Update Select All when individual checkboxes change
            document.querySelectorAll('.metric-checkbox input[type="checkbox"]').forEach(cb => {
                cb.addEventListener('change', function() {
                    const allCheckboxes = document.querySelectorAll('.metric-checkbox input[type="checkbox"]');
                    const allChecked = Array.from(allCheckboxes).every(checkbox => checkbox.checked);
                    document.getElementById('selectAll').checked = allChecked;
                });
            });
            
            async function generateReport() {
                const startDate = document.getElementById('startDate').value;
                const endDate = document.getElementById('endDate').value;
                const output = document.getElementById('output');
                
                if (!startDate || !endDate) {
                    output.innerHTML = '<span class="error">❌ Please select both start and end dates</span>';
                    return;
                }
                
                // Get selected metrics
                const metrics = [];
                document.querySelectorAll('.metric-checkbox input[type="checkbox"]:checked').forEach(cb => {
                    metrics.push(cb.value);
                });
                
                if (metrics.length === 0) {
                    output.innerHTML = '<span class="error">❌ Please select at least one metric</span>';
                    return;
                }
                
                output.innerHTML = '<div class="loading">⏳ Loading cache data...</div>';
                
                try {
                    const response = await fetch(`/api/cache-log?start=${startDate}&end=${endDate}&metrics=${metrics.join(',')}`);
                    const data = await response.json();
                    
                    if (data.success) {
                        output.innerHTML = data.report;
                    } else {
                        output.innerHTML = `<span class="error">❌ Error: ${data.error}</span>`;
                    }
                } catch (error) {
                    output.innerHTML = `<span class="error">❌ Error: ${error.message}</span>`;
                }
            }
            
            function downloadReport() {
                const output = document.getElementById('output');
                if (!output.textContent || output.textContent.includes('Select date range')) {
                    alert('Please generate a report first!');
                    return;
                }
                
                const blob = new Blob([output.textContent], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `fitbit-cache-log-${new Date().toISOString().split('T')[0]}.txt`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }
            
            async function downloadCSV() {
                const startDate = document.getElementById('startDate').value;
                const endDate = document.getElementById('endDate').value;
                
                if (!startDate || !endDate) {
                    alert('❌ Please select both start and end dates');
                    return;
                }
                
                // Get selected metrics
                const metrics = [];
                document.querySelectorAll('.metric-checkbox input[type="checkbox"]:checked').forEach(cb => {
                    metrics.push(cb.value);
                });
                
                if (metrics.length === 0) {
                    alert('❌ Please select at least one metric');
                    return;
                }
                
                try {
                    const response = await fetch(`/api/cache-csv?start=${startDate}&end=${endDate}&metrics=${metrics.join(',')}`);
                    
                    if (!response.ok) {
                        throw new Error('Failed to generate CSV');
                    }
                    
                    const blob = await response.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `fitbit-cache-export-${new Date().toISOString().split('T')[0]}.csv`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                } catch (error) {
                    alert(`❌ Error: ${error.message}`);
                }
            }
            
            function clearOutput() {
                document.getElementById('output').innerHTML = '';
            }
        </script>
    </body>
    </html>
    '''

@server.route('/api/cache-log')
def api_cache_log():
    """API endpoint to generate cache log report"""
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    metrics_str = request.args.get('metrics', 'daily,sleep,advanced,cardio,activities')
    
    if not start_date or not end_date:
        return jsonify({'success': False, 'error': 'Missing date parameters'})
    
    selected_metrics = set(metrics_str.split(','))
    
    try:
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        # Generate date range
        from datetime import datetime, timedelta
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        dates = [(start + timedelta(days=x)).strftime('%Y-%m-%d') 
                 for x in range((end - start).days + 1)]
        
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append(f"FITBIT CACHE REPORT: {start_date} to {end_date}")
        report_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append("=" * 80)
        report_lines.append("")
        
        for date in dates:
            report_lines.append(f"\n📅 {date}")
            report_lines.append("-" * 80)
            
            # Daily Metrics
            if 'daily' in selected_metrics:
                cursor.execute('''
                    SELECT steps, calories, distance, floors, active_zone_minutes,
                           resting_heart_rate, fat_burn_minutes, cardio_minutes, peak_minutes,
                           weight, body_fat, spo2, eov
                    FROM daily_metrics_cache WHERE date = ?
                ''', (date,))
                daily = cursor.fetchone()
                
                if daily:
                    report_lines.append("  📊 Daily Metrics:")
                    report_lines.append(f"    Steps: {daily[0]}")
                    report_lines.append(f"    Calories: {daily[1]}")
                    report_lines.append(f"    Distance: {daily[2]}")
                    report_lines.append(f"    Floors: {daily[3]}")
                    report_lines.append(f"    Active Zone Minutes: {daily[4]}")
                    report_lines.append(f"    Resting Heart Rate: {daily[5]}")
                    report_lines.append(f"    Fat Burn Minutes: {daily[6]}")
                    report_lines.append(f"    Cardio Minutes: {daily[7]}")
                    report_lines.append(f"    Peak Minutes: {daily[8]}")
                    report_lines.append(f"    Weight: {daily[9]} lbs")
                    report_lines.append(f"    Body Fat: {daily[10]}%")
                    report_lines.append(f"    SpO2: {daily[11]}")
                    report_lines.append(f"    EOV: {daily[12]}")
                else:
                    report_lines.append("  ❌ No daily metrics found")
            
            # Sleep Data
            if 'sleep' in selected_metrics:
                cursor.execute('''
                    SELECT reality_score, proxy_score, efficiency, 
                           deep_minutes, light_minutes, rem_minutes, wake_minutes, total_sleep
                    FROM sleep_cache WHERE date = ?
                ''', (date,))
                sleep = cursor.fetchone()
                
                if sleep:
                    report_lines.append("  😴 Sleep Data:")
                    report_lines.append(f"    Reality Score: {sleep[0]}")
                    report_lines.append(f"    Proxy Score: {sleep[1]}")
                    report_lines.append(f"    Efficiency: {sleep[2]}%")
                    report_lines.append(f"    Deep: {sleep[3]} min")
                    report_lines.append(f"    Light: {sleep[4]} min")
                    report_lines.append(f"    REM: {sleep[5]} min")
                    report_lines.append(f"    Wake: {sleep[6]} min")
                    report_lines.append(f"    Total: {sleep[7]} min")
                else:
                    report_lines.append("  ❌ No sleep data found")
            
            # Advanced Metrics
            if 'advanced' in selected_metrics:
                cursor.execute('''
                    SELECT hrv, breathing_rate, temperature
                    FROM advanced_metrics_cache WHERE date = ?
                ''', (date,))
                advanced = cursor.fetchone()
                
                if advanced:
                    report_lines.append("  💚 Advanced Metrics:")
                    report_lines.append(f"    HRV: {advanced[0]} ms")
                    report_lines.append(f"    Breathing Rate: {advanced[1]} bpm")
                    report_lines.append(f"    Temperature: {advanced[2]}°F")
                else:
                    report_lines.append("  ❌ No advanced metrics found")
            
            # Cardio Fitness
            if 'cardio' in selected_metrics:
                cursor.execute('''
                    SELECT vo2_max FROM cardio_fitness_cache WHERE date = ?
                ''', (date,))
                cardio = cursor.fetchone()
                
                if cardio and cardio[0]:
                    report_lines.append("  🏃 Cardio Fitness:")
                    report_lines.append(f"    VO2 Max: {cardio[0]}")
                else:
                    report_lines.append("  ❌ No cardio fitness data found")
            
            # Activities
            if 'activities' in selected_metrics:
                cursor.execute('''
                    SELECT activity_name, duration_ms, calories, avg_heart_rate
                    FROM activities_cache WHERE date = ?
                ''', (date,))
                activities = cursor.fetchall()
                
                if activities:
                    report_lines.append(f"  🏋️ Activities ({len(activities)}):")
                    for act in activities:
                        duration_min = act[1] // 60000 if act[1] else 0
                        report_lines.append(f"    - {act[0]}: {duration_min} min, {act[2]} cal, HR: {act[3]}")
                else:
                    report_lines.append("  ❌ No activities found")
        
        conn.close()
        
        report_lines.append("")
        report_lines.append("=" * 80)
        report_lines.append("Cache report complete!")
        report_lines.append("=" * 80)
        
        return jsonify({
            'success': True,
            'report': '\n'.join(report_lines)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@server.route('/api/cache-csv')
def api_cache_csv():
    """API endpoint to export cache data as CSV"""
    from flask import Response
    import io
    import csv
    
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    metrics_str = request.args.get('metrics', 'daily,sleep,advanced,cardio,activities')
    
    if not start_date or not end_date:
        return Response('Missing date parameters', status=400)
    
    selected_metrics = set(metrics_str.split(','))
    
    try:
        conn = sqlite3.connect(cache.db_path)
        cursor = conn.cursor()
        
        # Generate date range
        from datetime import datetime, timedelta
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        dates = [(start + timedelta(days=x)).strftime('%Y-%m-%d') 
                 for x in range((end - start).days + 1)]
        
        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Build header row based on selected metrics
        header = ['Date']
        
        if 'daily' in selected_metrics:
            header.extend(['Steps', 'Calories', 'Distance (mi)', 'Floors', 'Active Zone Min', 
                          'Resting HR', 'Fat Burn Min', 'Cardio Min', 'Peak Min',
                          'Weight (lbs)', 'Body Fat %', 'SpO2', 'EOV'])
        
        if 'sleep' in selected_metrics:
            header.extend(['Sleep Reality Score', 'Sleep Proxy Score', 'Sleep Efficiency %',
                          'Deep Sleep (min)', 'Light Sleep (min)', 'REM Sleep (min)', 
                          'Wake Time (min)', 'Total Sleep (min)'])
        
        if 'advanced' in selected_metrics:
            header.extend(['HRV (ms)', 'Breathing Rate (bpm)', 'Temperature (°F)'])
        
        if 'cardio' in selected_metrics:
            header.extend(['VO2 Max'])
        
        if 'activities' in selected_metrics:
            header.extend(['Activities Count', 'Activities Summary'])
        
        writer.writerow(header)
        
        # Write data rows
        for date in dates:
            row = [date]
            
            # Daily Metrics
            if 'daily' in selected_metrics:
                cursor.execute('''
                    SELECT steps, calories, distance, floors, active_zone_minutes,
                           resting_heart_rate, fat_burn_minutes, cardio_minutes, peak_minutes,
                           weight, body_fat, spo2, eov
                    FROM daily_metrics_cache WHERE date = ?
                ''', (date,))
                daily = cursor.fetchone()
                
                if daily:
                    row.extend([daily[0] or '', daily[1] or '', daily[2] or '', daily[3] or '', daily[4] or '',
                               daily[5] or '', daily[6] or '', daily[7] or '', daily[8] or '',
                               daily[9] or '', daily[10] or '', daily[11] or '', daily[12] or ''])
                else:
                    row.extend([''] * 13)
            
            # Sleep Data
            if 'sleep' in selected_metrics:
                cursor.execute('''
                    SELECT reality_score, proxy_score, efficiency, 
                           deep_minutes, light_minutes, rem_minutes, wake_minutes, total_sleep
                    FROM sleep_cache WHERE date = ?
                ''', (date,))
                sleep = cursor.fetchone()
                
                if sleep:
                    row.extend([sleep[0] or '', sleep[1] or '', sleep[2] or '',
                               sleep[3] or '', sleep[4] or '', sleep[5] or '', 
                               sleep[6] or '', sleep[7] or ''])
                else:
                    row.extend([''] * 8)
            
            # Advanced Metrics
            if 'advanced' in selected_metrics:
                cursor.execute('''
                    SELECT hrv, breathing_rate, temperature
                    FROM advanced_metrics_cache WHERE date = ?
                ''', (date,))
                advanced = cursor.fetchone()
                
                if advanced:
                    row.extend([advanced[0] or '', advanced[1] or '', advanced[2] or ''])
                else:
                    row.extend([''] * 3)
            
            # Cardio Fitness
            if 'cardio' in selected_metrics:
                cursor.execute('''
                    SELECT vo2_max FROM cardio_fitness_cache WHERE date = ?
                ''', (date,))
                cardio = cursor.fetchone()
                
                if cardio and cardio[0]:
                    row.extend([cardio[0]])
                else:
                    row.extend([''])
            
            # Activities
            if 'activities' in selected_metrics:
                cursor.execute('''
                    SELECT activity_name, duration_ms, calories, avg_heart_rate, steps, distance, activity_data_json
                    FROM activities_cache WHERE date = ?
                ''', (date,))
                activities = cursor.fetchall()
                
                if activities:
                    count = len(activities)
                    summaries = []
                    for act in activities:
                        # Extract basic fields
                        name = act[0]
                        duration_min = act[1] // 60000 if act[1] else 0
                        calories = act[2]
                        hr = act[3]
                        steps = act[4] or 'N/A'
                        distance_mi = round(act[5] * 0.621371, 2) if act[5] else 'N/A'
                        
                        # Parse JSON to get active duration
                        active_duration = 'N/A'
                        try:
                            import json
                            activity_json = json.loads(act[6]) if act[6] else {}
                            active_duration = activity_json.get('activeDuration', 0) // 60000 if activity_json.get('activeDuration') else 'N/A'
                        except:
                            pass
                        
                        summaries.append(f"{name} ({duration_min}min, Active:{active_duration}min, {calories}cal, HR:{hr}, {steps} steps, {distance_mi}mi)")
                    
                    row.extend([count, '; '.join(summaries)])
                else:
                    row.extend(['', ''])
            
            writer.writerow(row)
        
        conn.close()
        
        # Create response with CSV content
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=fitbit-cache-export-{datetime.now().strftime("%Y-%m-%d")}.csv'}
        )
        
    except Exception as e:
        return Response(f'Error: {str(e)}', status=500)

if __name__ == '__main__':
    app.run_server(debug=True)



# %%
