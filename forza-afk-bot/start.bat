@echo off
chcp 65001 >NUL
cd /d "%~dp0"
where python >NUL 2>NUL || (
    echo Python не найден. Установите Python 3.11+ с python.org и отметьте "Add Python to PATH".
    pause
    exit /b 1
)
python -c "import vgamepad" >NUL 2>NUL || (
    echo Устанавливаю vgamepad. Если появится установщик драйвера ViGEmBus, согласитесь...
    python -m pip install -r requirements.txt
)
python afk_bot.py %*
pause
