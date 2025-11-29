
import json
from datetime import datetime, timedelta

# Mock data
response_activities = {
    "activities": [
        {
            "logId": 123,
            "activityName": "Run",
            "startTime": "2023-10-27T14:30:00.000",
            "duration": 1800000,
            "calories": 300,
            "averageHeartRate": 140,
            "steps": 3000,
            "distance": 3.5
        },
        {
            "logId": 124,
            "activityName": "Walk",
            "startTime": "2023-10-27T18:00:00.000",
            "duration": 1200000,
            "calories": 100,
            "averageHeartRate": 100,
            "steps": 1500,
            "distance": 1.0
        },
        {
            "logId": 125,
            "activityName": "Swim",
            "startTime": "2023-10-28T07:00:00.000",
            "duration": 2400000,
            "calories": 400,
            "averageHeartRate": 130,
            "steps": 0,
            "distance": 1.2
        }
    ]
}

dates_str_list = ["2023-10-27", "2023-10-28", "2023-10-29"]

# --- Logic to verify ---

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

print(f"Activities by date: {json.dumps(activities_by_date, indent=2)}")

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
    
    # Mock sleep score
    sleep_score = 85 # Mock value
    
    if has_exercise or sleep_score is not None:
        exercise_timeline_data.append({
            'Date': date_str,
            'Exercise Calories': total_cals,
            'Next Day Sleep Score': sleep_score if sleep_score else 0
        })

print(f"Exercise Timeline Data: {json.dumps(exercise_timeline_data, indent=2)}")

# Verification
expected_cals_27 = 400 # 300 + 100
expected_cals_28 = 400 # 400
expected_cals_29 = 0

data_27 = next((d for d in exercise_timeline_data if d['Date'] == '2023-10-27'), None)
data_28 = next((d for d in exercise_timeline_data if d['Date'] == '2023-10-28'), None)
data_29 = next((d for d in exercise_timeline_data if d['Date'] == '2023-10-29'), None)

if data_27 and data_27['Exercise Calories'] == expected_cals_27:
    print("✅ 2023-10-27 Calories Correct")
else:
    print(f"❌ 2023-10-27 Calories Incorrect: Expected {expected_cals_27}, Got {data_27['Exercise Calories'] if data_27 else 'None'}")

if data_28 and data_28['Exercise Calories'] == expected_cals_28:
    print("✅ 2023-10-28 Calories Correct")
else:
    print(f"❌ 2023-10-28 Calories Incorrect: Expected {expected_cals_28}, Got {data_28['Exercise Calories'] if data_28 else 'None'}")

if data_29 and data_29['Exercise Calories'] == expected_cals_29:
    print("✅ 2023-10-29 Calories Correct")
else:
    print(f"❌ 2023-10-29 Calories Incorrect: Expected {expected_cals_29}, Got {data_29['Exercise Calories'] if data_29 else 'None'}")
