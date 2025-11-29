import sys

file_path = r'c:\dev\fitbit-web-ui-app-kb\src\app.py'

try:
    with open(file_path, 'r', encoding='utf-8') as f:
        source = f.read()
    
    compile(source, file_path, 'exec')
    print("✅ Syntax check passed!")
except SyntaxError as e:
    print(f"❌ SyntaxError: {e}")
    print(f"   Line {e.lineno}: {e.text}")
except Exception as e:
    print(f"❌ Error: {e}")
