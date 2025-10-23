"""
Fitbit Data Cache Manager
Caches sleep scores, HRV, breathing rate, temperature, and other metrics
to avoid redundant API calls and provide accurate historical data.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import threading

class FitbitCache:
    def __init__(self, db_path='data_cache.db'):
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
                       total_sleep: int = None, deep: int = None, light: int = None,
                       rem: int = None, wake: int = None, start_time: str = None,
                       sleep_data_json: str = None):
        """Cache sleep score and related data for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO sleep_cache 
                (date, sleep_score, efficiency, total_sleep, deep_minutes, light_minutes, 
                 rem_minutes, wake_minutes, start_time, sleep_data_json, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (date, sleep_score, efficiency, total_sleep, deep, light, rem, wake, 
                  start_time, sleep_data_json))
            conn.commit()
            conn.close()
    
    def get_sleep_data(self, date: str) -> Optional[Dict]:
        """Get all cached sleep data for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sleep_score, efficiency, total_sleep, deep_minutes, light_minutes,
                       rem_minutes, wake_minutes, start_time, sleep_data_json
                FROM sleep_cache WHERE date = ?
            ''', (date,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'sleep_score': result[0],
                    'efficiency': result[1],
                    'total_sleep': result[2],
                    'deep': result[3],
                    'light': result[4],
                    'rem': result[5],
                    'wake': result[6],
                    'start_time': result[7],
                    'sleep_data_json': result[8]
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
        """Cache advanced metrics for a specific date"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO advanced_metrics_cache 
                (date, hrv, breathing_rate, temperature, last_updated)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (date, hrv, breathing_rate, temperature))
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
                cursor.execute('''
                    SELECT date FROM sleep_cache 
                    WHERE date >= ? AND date <= ? AND sleep_score IS NOT NULL
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
            
            cursor.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM sleep_cache WHERE sleep_score IS NOT NULL')
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

