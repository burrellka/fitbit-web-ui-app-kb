#!/usr/bin/env python3
"""
Quick helper to add a debug endpoint to extract your access token
Add this code snippet to src/app.py temporarily
"""

TEMP_CODE_TO_ADD = """
# ============================================================
# TEMPORARY DEBUG ENDPOINT - Remove after testing!
# ============================================================
@server.route('/debug/token')
def debug_token():
    '''Debug endpoint to extract access token for testing'''
    import flask
    
    if 'access_token' in flask.session:
        token = flask.session['access_token']
        expiry = flask.session.get('token_expiry', 'Unknown')
        
        return {
            'status': 'success',
            'access_token': token,
            'expires_at': expiry,
            'refresh_token': flask.session.get('refresh_token', 'N/A')[:20] + '...',
            'note': 'Use this token in test_fitbit_sleep_api.py'
        }
    else:
        return {'status': 'error', 'message': 'Not logged in. Please authenticate first.'}

# ============================================================
"""

print("""
╔══════════════════════════════════════════════════════════════════════╗
║                 GET YOUR ACCESS TOKEN - 3 STEPS                      ║
╚══════════════════════════════════════════════════════════════════════╝

STEP 1: Add debug endpoint to src/app.py
─────────────────────────────────────────────────────────────────────────
Copy the code below and paste it at the END of src/app.py (before "if __name__"):

""")

print(TEMP_CODE_TO_ADD)

print("""
─────────────────────────────────────────────────────────────────────────

STEP 2: Restart your Docker container
─────────────────────────────────────────────────────────────────────────
docker restart fitbit-report-app-enhanced

STEP 3: Get your token
─────────────────────────────────────────────────────────────────────────
1. Make sure you're logged in to the Fitbit app
2. Visit: http://192.168.13.5:5033/debug/token
3. Copy the "access_token" value
4. Paste it into test_fitbit_sleep_api.py at line 12
5. Run: python3 test_fitbit_sleep_api.py

─────────────────────────────────────────────────────────────────────────

OR - Quick extraction from Docker logs:
─────────────────────────────────────────────────────────────────────────
After you login, run:
docker logs fitbit-report-app-enhanced 2>&1 | grep "access_token" | tail -1

This will show the most recent token from the OAuth flow.
Copy the value between "access_token":" and the next quote.

╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  SECURITY NOTE: Delete the debug endpoint after testing!        ║
╚══════════════════════════════════════════════════════════════════════╝
""")

