
import sqlite3
import json
import os
from src.cache_manager import FitbitCache

# Setup a temporary test database
TEST_DB = 'test_data_cache.db'
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

print(f"Creating test cache at {TEST_DB}...")
cache = FitbitCache(TEST_DB)

# Test Data
activity_id = "12345"
intraday_data = [
    {"time": "12:00:00", "value": 80},
    {"time": "12:01:00", "value": 85},
    {"time": "12:02:00", "value": 90}
]

# 1. Verify Table Creation
print("\n1. Verifying table creation...")
conn = sqlite3.connect(TEST_DB)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activity_intraday_cache'")
if cursor.fetchone():
    print("✅ Table 'activity_intraday_cache' exists.")
else:
    print("❌ Table 'activity_intraday_cache' does NOT exist.")
conn.close()

# 2. Verify Set/Get
print("\n2. Verifying set_activity_intraday and get_activity_intraday...")
cache.set_activity_intraday(activity_id, intraday_data)
print("Data set.")

fetched_data = cache.get_activity_intraday(activity_id)
print(f"Fetched data: {fetched_data}")

if fetched_data == intraday_data:
    print("✅ Data verification SUCCESSFUL: Fetched data matches original.")
else:
    print("❌ Data verification FAILED: Fetched data does not match.")

# 3. Verify Persistence (New instance)
print("\n3. Verifying persistence...")
cache2 = FitbitCache(TEST_DB)
fetched_data_2 = cache2.get_activity_intraday(activity_id)
if fetched_data_2 == intraday_data:
    print("✅ Persistence verification SUCCESSFUL.")
else:
    print("❌ Persistence verification FAILED.")

# Cleanup
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
    print("\nTest database removed.")
