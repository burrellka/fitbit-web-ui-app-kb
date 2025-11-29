import sys
import os

# Mock environment variables to avoid errors during import
os.environ['CLIENT_ID'] = 'mock_id'
os.environ['CLIENT_SECRET'] = 'mock_secret'
os.environ['REDIRECT_URL'] = 'http://localhost:8080/callback'
os.environ['DASHBOARD_PASSWORD'] = 'mock_pass'

try:
    print("Verifying app.py syntax...")
    from src import app
    print("✅ app.py imported successfully. Syntax is correct.")
    
    # Verify callback outputs count matches function return
    # This is hard to do dynamically without running the server, but we can check if the function is defined
    if hasattr(app, 'update_output'):
        print("✅ update_output function exists.")
    
    if hasattr(app, 'display_workout_details'):
        print("✅ display_workout_details function exists.")
        
except ImportError as e:
    print(f"❌ ImportError: {e}")
except SyntaxError as e:
    print(f"❌ SyntaxError: {e}")
except Exception as e:
    print(f"❌ Error: {e}")
