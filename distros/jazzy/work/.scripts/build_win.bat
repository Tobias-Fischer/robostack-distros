
setlocal EnableExtensions EnableDelayedExpansion

set CONDA_BLD_PATH=C:\bld
echo "PATH is %PATH%"

rmdir /Q/S C:\Strawberry\
rmdir /Q/S "C:\Program Files (x86)\Windows Kits\10\Include\10.0.17763.0\"

:: ROBOSTACK_DISTRO is set by the generated build workflow (tools/robostack.py gha).
cd distros\%ROBOSTACK_DISTRO%\work
set "FEEDSTOCK_ROOT=%cd%"

mkdir %CONDA_BLD_PATH%

:: Enable long path names on Windows
reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f
:: ... and in git, for sources that vendor packages clone during the build
git config --global core.longpaths true

for %%X in (%CURRENT_RECIPES%) do (
    echo "BUILDING RECIPE %%X"
    cd %FEEDSTOCK_ROOT%\recipes\%%X\
    rem build-ci (tools/robostack.py) adds the variant config and the distro's channels.
    pixi run -v rs %ROBOSTACK_DISTRO% build-ci --recipe %FEEDSTOCK_ROOT%\recipes\%%X\ ^
        --output-dir %CONDA_BLD_PATH%

    if errorlevel 1 exit 1
    rem -m %FEEDSTOCK_ROOT%\.ci_support\conda_forge_pinnings.yaml
)

:: Check if .conda files exist in the win-64 directory
if exist "%CONDA_BLD_PATH%\win-64\*.conda" (
    echo Found .conda files, starting upload...
    rem Upload packages one-by-one; the upload task (tools/robostack.py) skips or overwrites
    rem packages that already exist, depending on the upload target.
    for %%F in ("%CONDA_BLD_PATH%\win-64\*.conda") do (
        echo Uploading %%~fF
        pixi run rs %ROBOSTACK_DISTRO% upload "%%~fF"
        if errorlevel 1 exit 1
    )
) else (
    echo Warning: No .conda files found in %CONDA_BLD_PATH%\win-64
    echo This might be due to all the packages being skipped
)
