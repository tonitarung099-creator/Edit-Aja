@echo off
setlocal
py -3.11 -m pip install --upgrade pip
py -3.11 -m pip install -r requirements-dev.txt
py -3.11 -m compileall -q minicut_agent main.py mcp_server.py
py -3.11 -m PyInstaller --noconfirm --clean --windowed --name "MiniCut Studio Agent" --collect-all PySide6 main.py
py -3.11 -m PyInstaller --noconfirm --clean --console --name "MiniCut MCP" --collect-all mcp mcp_server.py
copy "dist\MiniCut MCP\MiniCut MCP.exe" "dist\MiniCut Studio Agent\MiniCut MCP.exe"
copy README.md "dist\MiniCut Studio Agent\README_AGENT.md"
echo Build selesai di dist\MiniCut Studio Agent
pause
