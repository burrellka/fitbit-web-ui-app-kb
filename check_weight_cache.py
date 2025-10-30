#!/usr/bin/env python3
"""
Simple diagnostic script to check if weight data is in the cache database.
Run this on your homelab server where the Docker container is running.
"""

import sqlite3
import os

# Path to the database (adjust if needed based on your Docker volume mount)
DB_PATH = "./cache/fitbit_cache.db"

if not os.path.exists(DB_PATH):
    print(f"❌ Database not found at {DB_PATH}")
    print("   Make sure you're running this in the directory with the 'cache' folder")
    print("   or adjust the DB_PATH variable")
    exit(1)

print("=" * 80)
print("WEIGHT DATA DIAGNOSTIC")
print("=" * 80)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Check if body_fat column exists
print("\n1️⃣ Checking if 'body_fat' column exists in daily_metrics_cache...")
cursor.execute("PRAGMA table_info(daily_metrics_cache)")
columns = cursor.fetchall()
column_names = [col[1] for col in columns]

if 'weight' in column_names:
    print("   ✅ 'weight' column EXISTS")
else:
    print("   ❌ 'weight' column MISSING")

if 'body_fat' in column_names:
    print("   ✅ 'body_fat' column EXISTS")
else:
    print("   ❌ 'body_fat' column MISSING - DATABASE NEEDS MIGRATION!")
    print("      Solution: Delete cache/fitbit_cache.db and restart container")

# Count total records
print("\n2️⃣ Checking total records in daily_metrics_cache...")
cursor.execute("SELECT COUNT(*) FROM daily_metrics_cache")
total_count = cursor.fetchone()[0]
print(f"   Total records: {total_count}")

# Count records with weight data
print("\n3️⃣ Checking records WITH weight data...")
cursor.execute("SELECT COUNT(*) FROM daily_metrics_cache WHERE weight IS NOT NULL")
weight_count = cursor.fetchone()[0]
print(f"   Records with weight: {weight_count}")

if weight_count == 0:
    print("   ⚠️  NO WEIGHT DATA FOUND IN CACHE!")
    print("      This means the background builder hasn't fetched weight data yet.")
else:
    print(f"   ✅ Found {weight_count} days with weight data")

# Count records with body_fat data
if 'body_fat' in column_names:
    print("\n4️⃣ Checking records WITH body_fat data...")
    cursor.execute("SELECT COUNT(*) FROM daily_metrics_cache WHERE body_fat IS NOT NULL")
    body_fat_count = cursor.fetchone()[0]
    print(f"   Records with body fat: {body_fat_count}")
    
    if body_fat_count == 0:
        print("   ⚠️  NO BODY FAT DATA FOUND IN CACHE!")
    else:
        print(f"   ✅ Found {body_fat_count} days with body fat data")

# Show sample data (last 5 days with ANY data)
print("\n5️⃣ Sample data (last 5 days):")
print("-" * 80)

if 'body_fat' in column_names:
    cursor.execute("""
        SELECT date, weight, body_fat, steps, calories 
        FROM daily_metrics_cache 
        ORDER BY date DESC 
        LIMIT 5
    """)
    rows = cursor.fetchall()
    
    if rows:
        print(f"{'Date':<12} {'Weight (lbs)':<15} {'Body Fat (%)':<15} {'Steps':<10} {'Calories':<10}")
        print("-" * 80)
        for row in rows:
            date, weight, body_fat, steps, cals = row
            weight_str = f"{weight:.1f}" if weight else "None"
            body_fat_str = f"{body_fat:.1f}" if body_fat else "None"
            print(f"{date:<12} {weight_str:<15} {body_fat_str:<15} {steps or 'None':<10} {cals or 'None':<10}")
    else:
        print("   ❌ No data found in database!")
else:
    print("   ❌ Cannot show sample - body_fat column missing!")

print("\n" + "=" * 80)
print("DIAGNOSIS COMPLETE")
print("=" * 80)

# Diagnosis summary
print("\n🔍 SUMMARY:")
if 'body_fat' not in column_names:
    print("   ❌ PROBLEM: Database schema is outdated (missing body_fat column)")
    print("   💡 SOLUTION: Delete cache/fitbit_cache.db and restart container to recreate")
elif weight_count == 0:
    print("   ❌ PROBLEM: Weight endpoint is not being called by background builder")
    print("   💡 SOLUTION: Check container logs for API errors or rate limiting")
    print("   📝 Command: docker logs fitbit-report-app-enhanced | grep -i weight")
else:
    print("   ✅ Weight data is present in cache!")
    print(f"   📊 {weight_count} days cached")

conn.close()

