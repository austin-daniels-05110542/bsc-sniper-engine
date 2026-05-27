# Start BSC Sniper web dashboard (PairCreated lab UI)
Set-Location $PSScriptRoot
Write-Host "Starting dashboard at http://127.0.0.1:8765"
python api_server.py
