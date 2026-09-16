@echo off
title GitHub Quick Deploy

echo ===================================
echo   Pushing Bot Updates to GitHub
echo ===================================
echo.

:: Stage all changed files
git add .

:: Nothing staged? Bail out instead of making an empty commit.
git diff --cached --quiet
if not errorlevel 1 (
    echo Nothing to commit. Working tree is clean.
    echo.
    pause
    exit /b 0
)

:: Prompt for commit message
set "msg="
set /p "msg=Enter commit message (or press ENTER for default): "
if "%msg%"=="" set "msg=Routine bot update"

:: Commit first, so the pull below has a clean index to rebase onto.
git commit -m "%msg%"
if errorlevel 1 (
    echo.
    echo [X] Commit failed. Nothing was pushed.
    echo.
    pause
    exit /b 1
)

:: Pick up anything that landed on the remote before we push.
git pull --rebase origin main
if errorlevel 1 (
    echo.
    echo [X] Pull failed. Your commit is saved locally - resolve the conflicts,
    echo     then run "git rebase --continue" and push again.
    echo.
    pause
    exit /b 1
)

git push origin main
if errorlevel 1 (
    echo.
    echo [X] Push failed. Your commit is saved locally; retry when ready.
    echo.
    pause
    exit /b 1
)

echo.
echo ===================================
echo   Updates sent to GitHub.
echo ===================================
echo.
pause