
import requests
import sys
import time

def test_mcp_connection(url="http://localhost:8000/sse"):
    print(f"🔌 Connecting to MCP Server at {url}...")
    
    try:
        # SSE requires stream=True
        with requests.get(url, stream=True, timeout=5) as response:
            if response.status_code == 200:
                print("✅ Connection Successful! (HTTP 200)")
                print("📥 Waiting for events (Press Ctrl+C to stop)...")
                
                # Read a few lines to verify stream
                for line in response.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        print(f"   Received: {decoded_line}")
                        if "endpoint" in decoded_line:
                            print("✅ Found 'endpoint' event! Server is ready.")
                            break
            else:
                print(f"❌ Connection Failed: HTTP {response.status_code}")
                print(response.text)
                
    except requests.exceptions.ConnectionError:
        print(f"❌ Could not connect to {url}. Is the server running?")
        print("   If running on Docker, make sure port 8000 is mapped.")
    except KeyboardInterrupt:
        print("\nTest stopped by user.")
    except Exception as e:
        print(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/sse"
    test_mcp_connection(target_url)
