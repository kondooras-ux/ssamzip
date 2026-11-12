@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem 파이썬 찾기: py 런처 -> python 순서
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY (
  python --version >nul 2>nul && set PY=python
)
if not defined PY goto nopython
echo 사용할 파이썬:
%PY% --version

echo [1/3] 필요한 라이브러리 설치 중... (인터넷 필요)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

echo [2/3] 프로그램 만드는 중... (1~3분 걸립니다)
%PY% build.py
if errorlevel 1 goto fail

echo [3/3] 완료!
echo   dist\ssamzip\ssamzip.exe 를 실행하세요. (dist\ssamzip 폴더 전체를 옮겨야 합니다)
echo   처음 실행 후 [설정] 탭에서 우클릭 메뉴를 등록하세요.
echo   exe 한 개로 만들고 싶으면: %PY% build.py --onefile
pause
exit /b 0

:nopython
echo 파이썬을 찾을 수 없습니다. https://www.python.org/downloads/ 에서 설치하세요.
echo 설치 첫 화면에서 "Add python.exe to PATH"를 꼭 체크하세요.
pause
exit /b 1

:fail
echo.
echo 오류가 발생했습니다. 바로 위의 빨간 글씨/ERROR 줄을 확인하세요.
echo (파이썬이 너무 새 버전이면 일부 라이브러리가 아직 준비되지 않았을 수 있습니다. 3.12 권장)
pause
exit /b 1
