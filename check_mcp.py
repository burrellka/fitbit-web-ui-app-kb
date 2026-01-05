import requests
import sys

def check_mcp_connection(url="http://localhost:5036/sse"):
    print(f"Testing connection to {url}...")
    try:
        # SSE endpoints are GET requests with stream=True
        headers = {'Accept': 'text/event-stream'}
        response = requests.get(url, headers=headers, stream=True, timeout=5)
        
        print(f"Response Status: {response.status_code}")
        
        if response.status_code == 200:
            print("Connection successful! Reading stream...")
            # Read a few chunks to verify stream is working
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    print(f"Received data: {chunk.decode()[:200]}...") # Print first 200 chars
                    break # Success, we connected
            return True
        else:
            print(f"Failed: Server returned status {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("Connection Refused: Is the server running? Check Docker logs.")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    success = check_mcp_connection()
    if not success:
        sys.exit(1)
