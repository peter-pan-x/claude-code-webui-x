@echo off
cd /d "D:\claudecode\claude-code-desktop"
pip install -q fastapi uvicorn pydantic websockets pywinpty
taskkill /F /IM python.exe 2>nul
start "Server" /MIN python app.py
ping -n 3 127.0.0.1 >nul
start http://127.0.0.1:9020
echo Ready: http://127.0.0.1:9020
pause
