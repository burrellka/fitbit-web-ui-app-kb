#!/usr/bin/env python3
"""Quick check - dump RAW JSON from Fitbit API"""

import requests
import json

ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIyM1RHN0siLCJzdWIiOiI0UUdaOUwiLCJpc3MiOiJGaXRiaXQiLCJ0eXAiOiJhY2Nlc3NfdG9rZW4iLCJzY29wZXMiOiJ3aHIgd3BybyB3c2xlIHd0ZW0gd3dlaSB3Y2Ygd3NldCB3YWN0IHdsb2Mgd3JlcyB3b3h5IiwiZXhwIjoxNzYxNDY1Mjc4LCJpYXQiOjE3NjE0MzY0Nzh9.1smBcbU5W8xSHVXo9jkk9WA5-lKjFVQbpxJnHmdt5cs"

# Test multiple dates - maybe older dates have sleep scores?
TEST_DATES = [
    "2025-10-20",
    "2025-10-21",
    "2025-10-22",
    "2025-10-23",
    "2025-10-24",
    "2025-10-25",
]

headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

for test_date in TEST_DATES:
    print(f"\n{'='*70}")
    print(f"Testing: {test_date}")
    print(f"{'='*70}")
    
    url = f"https://api.fitbit.com/1.2/user/-/sleep/date/{test_date}.json"
    response = requests.get(url, headers=headers)
    
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        if 'sleep' in data and len(data['sleep']) > 0:
            record = data['sleep'][0]
            
            if 'sleepScore' in record:
                print(f"✅ SLEEP SCORE FOUND: {record['sleepScore'].get('overall')}")
                print(f"   Full score object: {json.dumps(record['sleepScore'], indent=2)}")
            else:
                print(f"❌ NO SLEEP SCORE - Only efficiency: {record.get('efficiency')}")
        else:
            print(f"⚠️ No sleep data for this date")
    else:
        print(f"❌ API Error: {response.status_code}")

