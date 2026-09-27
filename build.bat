@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --windowed --name FileRoom --distpath dist --workpath build --add-data "public;public" file_room.py
if errorlevel 1 exit /b 1
copy /Y dist\FileRoom.exe FileRoom.exe
echo Built FileRoom.exe
