#!/usr/bin/env python3
"""
Migration script to add body_fat column to existing database.
Run this once to upgrade the database schema.
"""

import sqlite3
import os

DB_PATH = os.environ.get('CACHE_DB_PATH', '/app/data_cache.db')

print(f"🔧 Migrating database at: {DB_PATH}")

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if column exists
    cursor.execute("PRAGMA table_info(daily_metrics_cache)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'body_fat' in columns:
        print("✅ body_fat column already exists, no migration needed")
    else:
        print("📊 Adding body_fat column to daily_metrics_cache...")
        cursor.execute('ALTER TABLE daily_metrics_cache ADD COLUMN body_fat REAL')
        conn.commit()
        print("✅ Successfully added body_fat column!")
    
    conn.close()
    print("✅ Migration complete!")
    
except Exception as e:
    print(f"❌ Migration failed: {e}")
    raise


