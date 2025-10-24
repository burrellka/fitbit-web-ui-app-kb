"""
Minimal OAuth Callback Handler
This is a lightweight app that ONLY handles Fitbit OAuth callbacks.
No health data is displayed on this endpoint - it's purely for authentication.
Runs on port 5032 (public-facing for Fitbit OAuth)
"""

import os
from flask import Flask, request, redirect
from urllib.parse import urlencode

app = Flask(__name__)

# Get environment variables
CLIENT_ID = os.environ.get('CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('CLIENT_SECRET', '')
REDIRECT_URL = os.environ.get('REDIRECT_URL', 'https://fitbitkb.burrellstribedns.org/')
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://192.168.13.5:5033/')

@app.route('/')
def oauth_handler():
    """
    OAuth callback handler - receives code from Fitbit and redirects to dashboard
    This is the ONLY page on this port - no health data is exposed here
    """
    # Check if this is an OAuth callback
    code = request.args.get('code')
    
    if code:
        # Got OAuth code from Fitbit - redirect to dashboard with the code
        print(f"✅ OAuth code received: {code[:20]}...")
        
        # Build redirect URL to dashboard with the code
        dashboard_redirect = f"{DASHBOARD_URL}?code={code}"
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Fitbit OAuth - Redirecting...</title>
            <meta http-equiv="refresh" content="0;url={dashboard_redirect}">
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                }}
                .container {{
                    text-align: center;
                    padding: 40px;
                    background: rgba(255, 255, 255, 0.1);
                    border-radius: 15px;
                    backdrop-filter: blur(10px);
                }}
                .spinner {{
                    border: 4px solid rgba(255, 255, 255, 0.3);
                    border-radius: 50%;
                    border-top: 4px solid white;
                    width: 40px;
                    height: 40px;
                    animation: spin 1s linear infinite;
                    margin: 20px auto;
                }}
                @keyframes spin {{
                    0% {{ transform: rotate(0deg); }}
                    100% {{ transform: rotate(360deg); }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🔐 Authentication Successful!</h1>
                <div class="spinner"></div>
                <p>Redirecting to your dashboard...</p>
                <p style="font-size: 12px; margin-top: 20px;">If you are not redirected automatically, <a href="{dashboard_redirect}" style="color: #fff;">click here</a>.</p>
            </div>
        </body>
        </html>
        """
    else:
        # No code - show minimal info page
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Fitbit OAuth Callback Endpoint</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                }
                .container {
                    text-align: center;
                    padding: 40px;
                    background: rgba(255, 255, 255, 0.1);
                    border-radius: 15px;
                    backdrop-filter: blur(10px);
                    max-width: 500px;
                }
                .icon {
                    font-size: 64px;
                    margin-bottom: 20px;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="icon">🔐</div>
                <h1>Fitbit OAuth Callback Endpoint</h1>
                <p>This endpoint is used for Fitbit authentication only.</p>
                <p><strong>⚠️ No health data is accessible here.</strong></p>
                <p style="font-size: 12px; margin-top: 30px; opacity: 0.8;">
                    To access your dashboard, please use your internal URL.
                </p>
            </div>
        </body>
        </html>
        """

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    return {"status": "ok", "service": "oauth-callback"}, 200

if __name__ == '__main__':
    print("🔐 Starting OAuth Callback Handler on port 5032...")
    print(f"📍 Redirect URL: {REDIRECT_URL}")
    print(f"🏠 Dashboard URL: {DASHBOARD_URL}")
    print("⚠️  This endpoint handles OAuth only - no health data exposed!")
    app.run(host='0.0.0.0', port=5032, debug=False)

