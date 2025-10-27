"""
Fitbit Data Cache Manager
Caches sleep scores, HRV, breathing rate, temperature, and other metrics
to avoid redundant API calls and provide accurate historical data.
Also manages secure refresh token storage for automatic daily sync.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import threading
import base64
import os

class FitbitCache:
    def __init__(self, db_path='/app/data_cache.db'):
        # Ensure the directory exists
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_database()
    
    def _init_database(self):
        """Initialize the cache database with required tables"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Sleep metrics table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sleep_cache (
                    date TEXT PRIMARY KEY,
                    sleep_score INTEGER,
                    efficiency INTEGER,
                    proxy_score INTEGER,
                    reality_score INTEGER,
                    total_sleep INTEGER,
                    deep_minutes INTEGER,
                    light_minutes INTEGER,
                    rem_minutes INTEGER,
                    wake_minutes INTEGER,
                    start_time TEXT,
                    sleep_data_json TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Add proxy_score and reality_score columns if they don't exist (migration)
            try:
                cursor.execute('ALTER TABLE sleep_cache ADD COLUMN proxy_score INTEGER')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            try:
                cursor.execute('ALTER TABLE sleep_cache ADD COLUMN reality_score INTEGER')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            # Add EOV column to daily_metrics_cache (migration for existing databases)
            try:
                cursor.execute('ALTER TABLE daily_metrics_cache ADD COLUMN eov REAL')
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            # Advanced metrics table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS advanced_metrics_cache (
                    date TEXT PRIMARY KEY,
                    hrv REAL,
                    breathing_rate REAL,
                    temperature REAL,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Daily metrics table (RHR, steps, weight, spo2, zones, etc.)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_metrics_cache (
                    date TEXT PRIMARY KEY,
                    resting_heart_rate INTEGER,
                    steps INTEGER,
                    weight REAL,
                    spo2 REAL,
                    eov REAL,
                    calories INTEGER,
                    distance REAL,
                    floors INTEGER,
                    active_zone_minutes INTEGER,
                    fat_burn_minutes INTEGER,
                    cardio_minutes INTEGER,
                    peak_minutes INTEGER,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Cardio fitness table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cardio_fitness_cache (
                    date TEXT PRIMARY KEY,
                    vo2_max REAL,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Activities/Exercise table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS activities_cache (
                    activity_id TEXT PRIMARY KEY,
                    date TEXT,
                    activity_name TEXT,
                    duration_ms INTEGER,
                    calories INTEGER,
                    avg_heart_rate INTEGER,
                    steps INTEGER,
                    distance REAL,
                    activity_data_json TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Cache metadata table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cache_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
            conn.close()
            print("✅ Cache database initialized")
    
    def get_sleep_score(self, date: str) -> Optional[int]:
        """Get cached sleep score for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT sleep_score FROM sleep_cache WHERE date = ?', (date,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result and result[0] is not None else None
    
    def set_sleep_score(self, date: str, sleep_score: int, efficiency: int = None,
                       proxy_score: int = None, reality_score: int = None,
                       total_sleep: int = None, deep: int = None, light: int = None,
                       rem: int = None, wake: int = None, start_time: str = None,
                       sleep_data_json: str = None):
        """Cache sleep score and related data for a specific date"""
        try:
            with self.lock:
                print(f"🔍 [CACHE DEBUG] Attempting to cache {date} - Reality={reality_score}, Proxy={proxy_score}")
                conn = sqlite3.connect(self.db_path)
                print(f"🔍 [CACHE DEBUG] Connected to {self.db_path}")
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO sleep_cache 
                    (date, sleep_score, efficiency, proxy_score, reality_score, total_sleep, deep_minutes, light_minutes, 
                     rem_minutes, wake_minutes, start_time, sleep_data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (date, sleep_score, efficiency, proxy_score, reality_score, total_sleep, deep, light, rem, wake, 
                      start_time, sleep_data_json))
                print(f"🔍 [CACHE DEBUG] Execute completed, committing...")
                conn.commit()
                print(f"🔍 [CACHE DEBUG] Commit completed")
                
                # VERIFY the write
                cursor.execute("SELECT reality_score FROM sleep_cache WHERE date = ?", (date,))
                verify = cursor.fetchone()
                if verify:
                    print(f"✅ [CACHE DEBUG] VERIFIED: {date} exists with reality_score={verify[0]}")
                else:
                    print(f"❌ [CACHE DEBUG] VERIFICATION FAILED: {date} NOT FOUND after commit!")
                
                conn.close()
                print(f"💾 Cached sleep scores for {date}: Reality={reality_score}, Proxy={proxy_score}, Efficiency={efficiency}")
        except Exception as e:
            print(f"❌ [CACHE ERROR] Failed to cache {date}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    
    def get_sleep_data(self, date: str) -> Optional[Dict]:
        """Get all cached sleep data for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sleep_score, efficiency, proxy_score, reality_score, total_sleep, deep_minutes, light_minutes,
                       rem_minutes, wake_minutes, start_time, sleep_data_json
                FROM sleep_cache WHERE date = ?
            ''', (date,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'sleep_score': result[0],
                    'efficiency': result[1],
                    'proxy_score': result[2],
                    'reality_score': result[3],
                    'total_sleep': result[4],
                    'deep': result[5],
                    'light': result[6],
                    'rem': result[7],
                    'wake': result[8],
                    'start_time': result[9],
                    'sleep_data_json': result[10]
                }
            return None
    
    def get_advanced_metrics(self, date: str) -> Optional[Dict]:
        """Get cached advanced metrics for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT hrv, breathing_rate, temperature
                FROM advanced_metrics_cache WHERE date = ?
            ''', (date,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'hrv': result[0],
                    'breathing_rate': result[1],
                    'temperature': result[2]
                }
            return None
    
    def set_advanced_metrics(self, date: str, hrv: float = None, 
                           breathing_rate: float = None, temperature: float = None):
        """
        Sets (UPSERTS) advanced metrics for a specific date, preserving other data.
        """
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Using COALESCE on the UPDATE ensures we don't overwrite existing data with NULLs
            sql = f"""
                INSERT INTO advanced_metrics_cache (date, hrv, breathing_rate, temperature)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    hrv = COALESCE(excluded.hrv, advanced_metrics_cache.hrv),
                    breathing_rate = COALESCE(excluded.breathing_rate, advanced_metrics_cache.breathing_rate),
                    temperature = COALESCE(excluded.temperature, advanced_metrics_cache.temperature);
            """
            
            cursor.execute(sql, (date, hrv, breathing_rate, temperature))
            conn.commit()
            conn.close()
    
    def get_missing_dates(self, start_date: str, end_date: str, metric_type: str = 'sleep') -> List[str]:
        """Get list of dates that are NOT in cache for given date range"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Generate all dates in range
            start = datetime.strptime(start_date, '%Y-%m-%d')
            end = datetime.strptime(end_date, '%Y-%m-%d')
            all_dates = []
            current = start
            while current <= end:
                all_dates.append(current.strftime('%Y-%m-%d'))
                current += timedelta(days=1)
            
            # Get dates already in cache
            if metric_type == 'sleep':
                # Check for reality_score instead of sleep_score (since API doesn't provide sleep_score for Personal apps)
                cursor.execute('''
                    SELECT date FROM sleep_cache 
                    WHERE date >= ? AND date <= ? AND reality_score IS NOT NULL
                ''', (start_date, end_date))
            else:  # advanced_metrics
                cursor.execute('''
                    SELECT date FROM advanced_metrics_cache 
                    WHERE date >= ? AND date <= ?
                ''', (start_date, end_date))
            
            cached_dates = set(row[0] for row in cursor.fetchall())
            conn.close()
            
            # Return missing dates
            missing = [date for date in all_dates if date not in cached_dates]
            return missing
    
    def get_metadata(self, key: str) -> Optional[str]:
        """Get metadata value"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM cache_metadata WHERE key = ?', (key,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else None
    
    def set_metadata(self, key: str, value: str):
        """Set metadata value"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO cache_metadata (key, value, last_updated)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, value))
            conn.commit()
            conn.close()
    
    def get_cache_stats(self) -> Dict:
        """Get statistics about the cache"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Check for reality_score instead of sleep_score (since API doesn't provide sleep_score for Personal apps)
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM sleep_cache WHERE reality_score IS NOT NULL')
            sleep_stats = cursor.fetchone()
            
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM advanced_metrics_cache')
            advanced_stats = cursor.fetchone()
            
            conn.close()
            
            return {
                'sleep_records': sleep_stats[0] or 0,
                'sleep_date_range': f"{sleep_stats[1]} to {sleep_stats[2]}" if sleep_stats[1] else "No data",
                'advanced_records': advanced_stats[0] or 0,
                'advanced_date_range': f"{advanced_stats[1]} to {advanced_stats[2]}" if advanced_stats[1] else "No data"
            }
    
    def get_detailed_cache_stats(self) -> Dict:
        """Get detailed per-metric statistics about the cache"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Sleep data - Check for reality_score instead of sleep_score (since API doesn't provide sleep_score for Personal apps)
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM sleep_cache WHERE reality_score IS NOT NULL')
            sleep_stats = cursor.fetchone()
            
            # HRV
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM advanced_metrics_cache WHERE hrv IS NOT NULL')
            hrv_stats = cursor.fetchone()
            
            # Breathing Rate
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM advanced_metrics_cache WHERE breathing_rate IS NOT NULL')
            breathing_stats = cursor.fetchone()
            
            # Temperature
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM advanced_metrics_cache WHERE temperature IS NOT NULL')
            temp_stats = cursor.fetchone()
            
            conn.close()
            
            return {
                'sleep': {
                    'count': sleep_stats[0] or 0,
                    'date_range': f"{sleep_stats[1]} to {sleep_stats[2]}" if sleep_stats[1] else "No data"
                },
                'hrv': {
                    'count': hrv_stats[0] or 0,
                    'date_range': f"{hrv_stats[1]} to {hrv_stats[2]}" if hrv_stats[1] else "No data"
                },
                'breathing_rate': {
                    'count': breathing_stats[0] or 0,
                    'date_range': f"{breathing_stats[1]} to {breathing_stats[2]}" if breathing_stats[1] else "No data"
                },
                'temperature': {
                    'count': temp_stats[0] or 0,
                    'date_range': f"{temp_stats[1]} to {temp_stats[2]}" if temp_stats[1] else "No data"
                }
            }
    
    def store_refresh_token(self, refresh_token: str, expires_in: int = 28800):
        """Store encrypted refresh token for automatic sync"""
        # Simple base64 encoding (not cryptographically secure, but adds obfuscation)
        # For production, use proper encryption like Fernet
        encoded_token = base64.b64encode(refresh_token.encode()).decode()
        expiry_timestamp = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
        
        self.set_metadata('refresh_token', encoded_token)
        self.set_metadata('token_expiry', expiry_timestamp)
        self.set_metadata('last_token_update', datetime.now().isoformat())
        print(f"🔒 Refresh token stored securely (expires: {expiry_timestamp})")
    
    def get_refresh_token(self) -> Optional[str]:
        """Retrieve and decode stored refresh token"""
        encoded_token = self.get_metadata('refresh_token')
        if encoded_token:
            try:
                return base64.b64decode(encoded_token.encode()).decode()
            except:
                return None
        return None
    
    def get_last_sync_date(self) -> Optional[str]:
        """Get the date of last successful sync"""
        return self.get_metadata('last_sync_date')
    
    def set_last_sync_date(self, date_str: str):
        """Update last successful sync date"""
        self.set_metadata('last_sync_date', date_str)
        print(f"📅 Last sync date updated: {date_str}")
    
    def get_daily_metrics(self, date: str) -> Optional[Dict]:
        """Get cached daily metrics for a specific date (🐞 FIX: Added EOV support)"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT resting_heart_rate, steps, weight, spo2, eov, calories, distance, 
                       floors, active_zone_minutes, fat_burn_minutes, cardio_minutes, peak_minutes
                FROM daily_metrics_cache WHERE date = ?
            ''', (date,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'resting_heart_rate': result[0],
                    'steps': result[1],
                    'weight': result[2],
                    'spo2': result[3],
                    'eov': result[4],
                    'calories': result[5],
                    'distance': result[6],
                    'floors': result[7],
                    'active_zone_minutes': result[8],
                    'fat_burn_minutes': result[9],
                    'cardio_minutes': result[10],
                    'peak_minutes': result[11]
                }
            return None
    
    def set_daily_metrics(self, date: str, resting_heart_rate: int = None, steps: int = None,
                         weight: float = None, spo2: float = None, eov: float = None, calories: int = None,
                         distance: float = None, floors: int = None, active_zone_minutes: int = None,
                         fat_burn_minutes: int = None, cardio_minutes: int = None, peak_minutes: int = None):
        """
        Sets (UPSERTS) daily metrics for a specific date, preserving other data.
        """
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Dynamically build the SET clauses for the UPDATE part of the UPSERT
            set_clauses = []
            params = {'date': date}
            if resting_heart_rate is not None: set_clauses.append("resting_heart_rate = excluded.resting_heart_rate")
            if fat_burn_minutes is not None: set_clauses.append("fat_burn_minutes = excluded.fat_burn_minutes")
            if cardio_minutes is not None: set_clauses.append("cardio_minutes = excluded.cardio_minutes")
            if peak_minutes is not None: set_clauses.append("peak_minutes = excluded.peak_minutes")
            if steps is not None: set_clauses.append("steps = excluded.steps")
            if weight is not None: set_clauses.append("weight = excluded.weight")
            if spo2 is not None: set_clauses.append("spo2 = excluded.spo2")
            if eov is not None: set_clauses.append("eov = excluded.eov")
            if calories is not None: set_clauses.append("calories = excluded.calories")
            if distance is not None: set_clauses.append("distance = excluded.distance")
            if floors is not None: set_clauses.append("floors = excluded.floors")
            if active_zone_minutes is not None: set_clauses.append("active_zone_minutes = excluded.active_zone_minutes")

            if not set_clauses: # Nothing to update
                return
            
            # Using COALESCE on the UPDATE ensures we don't overwrite existing data with NULLs
            # For example, if we only provide 'steps', 'rhr' will be preserved if it already exists.
            sql = f"""
                INSERT INTO daily_metrics_cache (date, resting_heart_rate, fat_burn_minutes, cardio_minutes, peak_minutes, steps, weight, spo2, eov, calories, distance, floors, active_zone_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    resting_heart_rate = COALESCE(excluded.resting_heart_rate, daily_metrics_cache.resting_heart_rate),
                    fat_burn_minutes = COALESCE(excluded.fat_burn_minutes, daily_metrics_cache.fat_burn_minutes),
                    cardio_minutes = COALESCE(excluded.cardio_minutes, daily_metrics_cache.cardio_minutes),
                    peak_minutes = COALESCE(excluded.peak_minutes, daily_metrics_cache.peak_minutes),
                    steps = COALESCE(excluded.steps, daily_metrics_cache.steps),
                    weight = COALESCE(excluded.weight, daily_metrics_cache.weight),
                    spo2 = COALESCE(excluded.spo2, daily_metrics_cache.spo2),
                    eov = COALESCE(excluded.eov, daily_metrics_cache.eov),
                    calories = COALESCE(excluded.calories, daily_metrics_cache.calories),
                    distance = COALESCE(excluded.distance, daily_metrics_cache.distance),
                    floors = COALESCE(excluded.floors, daily_metrics_cache.floors),
                    active_zone_minutes = COALESCE(excluded.active_zone_minutes, daily_metrics_cache.active_zone_minutes);
            """
            
            cursor.execute(sql, (date, resting_heart_rate, fat_burn_minutes, cardio_minutes, peak_minutes, steps, weight, spo2, eov, calories, distance, floors, active_zone_minutes))
            conn.commit()
            conn.close()
    
    def get_cardio_fitness(self, date: str) -> Optional[float]:
        """Get cached cardio fitness (VO2 Max) for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT vo2_max FROM cardio_fitness_cache WHERE date = ?', (date,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result and result[0] is not None else None
    
    def set_cardio_fitness(self, date: str, vo2_max: float):
        """Cache cardio fitness (VO2 Max) for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO cardio_fitness_cache (date, vo2_max, last_updated)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (date, vo2_max))
            conn.commit()
            conn.close()
    
    def get_activities(self, date: str) -> List[Dict]:
        """Get cached activities for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT activity_id, activity_name, duration_ms, calories, avg_heart_rate,
                       steps, distance, activity_data_json
                FROM activities_cache WHERE date = ?
            ''', (date,))
            results = cursor.fetchall()
            conn.close()
            
            activities = []
            for row in results:
                activities.append({
                    'activity_id': row[0],
                    'activity_name': row[1],
                    'duration_ms': row[2],
                    'calories': row[3],
                    'avg_heart_rate': row[4],
                    'steps': row[5],
                    'distance': row[6],
                    'activity_data_json': row[7]
                })
            return activities
    
    def set_activity(self, activity_id: str, date: str, activity_name: str,
                    duration_ms: int = None, calories: int = None, avg_heart_rate: int = None,
                    steps: int = None, distance: float = None, activity_data_json: str = None):
        """Cache an activity"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO activities_cache 
                (activity_id, date, activity_name, duration_ms, calories, avg_heart_rate,
                 steps, distance, activity_data_json, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (activity_id, date, activity_name, duration_ms, calories, avg_heart_rate,
                  steps, distance, activity_data_json))
            conn.commit()
            conn.close()
    
    def flush_cache(self):
        """Clear all cached data (sleep, advanced metrics, daily metrics, activities, but NOT tokens)"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM sleep_cache')
            cursor.execute('DELETE FROM advanced_metrics_cache')
            cursor.execute('DELETE FROM daily_metrics_cache')
            cursor.execute('DELETE FROM cardio_fitness_cache')
            cursor.execute('DELETE FROM activities_cache')
            
            conn.commit()
            conn.close()
            print("🗑️ Cache flushed successfully! (Tokens preserved)")
    
    def flush_all(self):
        """Clear EVERYTHING including tokens (requires re-login)"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM sleep_cache')
            cursor.execute('DELETE FROM advanced_metrics_cache')
            cursor.execute('DELETE FROM daily_metrics_cache')
            cursor.execute('DELETE FROM cardio_fitness_cache')
            cursor.execute('DELETE FROM activities_cache')
            cursor.execute('DELETE FROM cache_metadata')
            
            conn.commit()
            conn.close()
            print("🗑️ ALL cache data flushed! (Including tokens - re-login required)")
    
    def get_metadata(self, key: str) -> Optional[str]:
        """Get a metadata value by key"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('SELECT value FROM cache_metadata WHERE key = ?', (key,))
            result = cursor.fetchone()
            
            conn.close()
            return result[0] if result else None
    
    def set_metadata(self, key: str, value: str):
        """Set a metadata key-value pair"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO cache_metadata (key, value, last_updated)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, value))
            
            conn.commit()
            conn.close()

