$ErrorActionPreference = "Stop"

python -m pip install -r requirements.txt
python -m PyInstaller `
  --noconfirm `
  --windowed `
  --name "ClinicaAgape" `
  --icon "agape_app\assets\app_icon.ico" `
  --add-data ".env.example;." `
  --add-data "agape_app\assets;agape_app\assets" `
  main.py

Write-Host "Executavel gerado em dist\\ClinicaAgape\\ClinicaAgape.exe"
