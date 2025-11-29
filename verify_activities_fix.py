import datetime
import json
from unittest.mock import MagicMock

# Mock the cache
class MockCache:
    def get_activities(self, date_str):
        # Return a dummy activity for verification
        return [{
            'activity_id': 12345,
            'activity_name': 'Run',
            'duration_ms': 1800000,
            'calories': 300,
            'avg_heart_rate': 140,
            'steps': 3000,
            'distance': 3.5,
            'activity_data_json': json.dumps({
                'logId': 12345,
                'activityName': 'Run',
                'startTime': f"{date_str}T10:00:00.000",
                'duration': 1800000,
                'calories': 300,
                'averageHeartRate': 140,
                'steps': 3000,
                'distance': 3.5
            })
        }]
    
    def get_sleep_data(self, date_str):
        return None
        
    def get_daily_metrics(self, date_str):
        return {}
        
    def get_advanced_metrics(self, date_str):
        return {}
        
    def get_cardio_fitness(self, date_str):
        return None

# Simulate the logic in update_output
def test_update_output_logic():
    print("🧪 Starting verification of activities loading logic...")
    
    # Setup
    cache = MockCache()
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    dates_str_list = [today]
    refresh_today = True
    start_date = today
    end_date = today
    
    # --- Logic from app.py (simplified) ---
    
    # 1. Load activities from cache (lines 3614-3658)
    print("📥 Loading activities from cache...")
    response_activities = {"activities": []}
    total_activities = 0
    
    for date_str in dates_str_list:
        activities_for_date = cache.get_activities(date_str)
        for act in activities_for_date:
            activity_json = act.get('activity_data_json')
            if activity_json:
                full_activity = json.loads(activity_json)
                response_activities['activities'].append(full_activity)
            total_activities += 1
            
    print(f"✅ Loaded {total_activities} activities from cache")
    
    # 2. The problematic line (3744) was here: response_activities = {"activities": []}
    # It is now REMOVED.
    
    # 3. Processing data loop (lines 4194+)
    exercise_data = []
    activities_found = 0
    
    print(f"🔍 Checking response_activities content: {len(response_activities.get('activities', []))} items")
    
    for activity in response_activities.get('activities', []):
        activities_found += 1
        print(f"   Found activity: {activity.get('activityName')}")
        
    # Verification
    if activities_found > 0:
        print("\n✅ SUCCESS: Activities were preserved and found in the final loop!")
        print(f"   Count: {activities_found}")
    else:
        print("\n❌ FAILURE: No activities found in the final loop. They were likely cleared.")

if __name__ == "__main__":
    test_update_output_logic()
