"""真实便携 Python/venv/离线依赖及旧版办公模块初始化；不启动 QQ 或工作机。"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inquiry.runtime import ROOT, DATA_DIR


def main():
    output = DATA_DIR / "qa" / ("portable-install-20261022-" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    old = DATA_DIR / "lan/packages/worker-20261022-115143-26e648"
    package = DATA_DIR / "lan/installers/python.3.12.10.nupkg"
    if not package.is_file() or not (old / "wheels").is_dir():
        raise RuntimeError("先准备官方 Python 便携包及原交付包离线依赖；本测试不自动下载")
    target = output / "runtime"
    # 模拟一个因系统 Python 路径不同而中断的残留环境，验证先备份再创建。
    (target / ".venv").mkdir(parents=True)
    (target / ".venv/pyvenv.cfg").write_text("home = C:\\MissingPython\n", encoding="utf-8")
    (target / ".venv/keep-fixture.txt").write_text("preserve", encoding="utf-8")
    probe = output / "probe.py"
    probe.write_text('''import json, sys
from pathlib import Path
from inquiry.runtime import ROOT
from lanhost import controller, config
assert "inquiry.qq" not in sys.modules
assert "ui.server" not in sys.modules
assert config.controller()["office_device"]
with controller.app.test_client() as client:
    assert client.get("/api/health").json["app_id"] == "tianxin-lan-controller"
    assert client.get("/api/lan/status").json["paired"] is False
# 真实加载所有关键扩展，不调用 QQ 控件、模型或网络。
import flask, cryptography, openai, playwright.sync_api, PIL.Image
import win32gui, win32process, uiautomation
from lanhost import worker
assert Path(sys.executable).resolve().is_relative_to(ROOT)
assert Path(sys.base_prefix).resolve().is_relative_to(ROOT)
print(json.dumps({"executable":sys.executable,"base_prefix":sys.base_prefix,
    "runtime_root":str(ROOT),"dependencies_imported":True,"office_initialized":True,
    "physical_second_pc_tested":False,"qq_operations":0}, ensure_ascii=False))
''', encoding="utf-8")
    script = output / "verify.ps1"
    script.write_text(r'''$ErrorActionPreference='Stop'
$helper=$args[0]; $package=$args[1]; $root=$args[2]; $original=$args[3]; $probe=$args[4]
. $helper
New-Item -ItemType Directory -Path "$root\.cache\tmp","$root\.cache\powershell","$root\data" -Force | Out-Null
$env:TEMP="$root\.cache\tmp"; $env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP
$env:PYTHONPYCACHEPREFIX="$root\.cache\pycache"; $env:PIP_CACHE_DIR="$root\.cache\pip"
$env:PSModuleAnalysisCachePath="$root\.cache\powershell\ModuleAnalysisCache"
$env:PYTHONIOENCODING='utf-8'
Remove-Item -LiteralPath Env:PYTHONHOME,Env:PYTHONPATH -ErrorAction SilentlyContinue
$entries=@(Get-SetupApplication (Join-Path $original 'application.zip'))
Copy-SetupApplication (Join-Path $original 'application.zip') $root $entries
$base=Install-SetupPythonPackage $package $root (Get-SetupPythonPackageHash)
foreach($name in @('python.exe','python312.dll')) {
    $signature=Get-AuthenticodeSignature -LiteralPath (Join-Path $base $name)
    if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation'){throw 'Publisher verification failed'}
}
Initialize-SetupVenv $base $root $false
if(@(Get-ChildItem -Path "$root\data\setup-backups\venv-*\keep-fixture.txt").Count -ne 1){throw 'Old venv not preserved'}
$py=Join-Path $root '.venv\Scripts\python.exe'
Invoke-SetupModule $py $root 'pip' @('install','--no-index','--no-compile','--disable-pip-version-check','--find-links',"$original\wheels",'-r',"$root\requirements.lock.txt")
Invoke-SetupModule $py $root 'pip' @('check','--disable-pip-version-check')
Copy-SetupInitialFile "$root\.env.example" "$root\.env"
Copy-SetupInitialFile "$root\settings.example.toml" "$root\settings.toml"
Invoke-SetupModule $py $root 'lanhost.setup' @('office')
Copy-Item -LiteralPath $probe -Destination "$root\portable_probe.py"
Invoke-SetupModule $py $root 'portable_probe'
$before=Get-SetupDigest "$root\.venv\pyvenv.cfg"
$again=Install-SetupPythonPackage $package $root (Get-SetupPythonPackageHash)
Initialize-SetupVenv $again $root $false
if($before -ne (Get-SetupDigest "$root\.venv\pyvenv.cfg")){throw 'Repeat setup replaced venv'}
Write-Output 'PORTABLE_RUNTIME_SMOKE_PASSED'
''', encoding="utf-8-sig")
    env = os.environ.copy()
    env["PSModuleAnalysisCachePath"] = str(output / "ModuleAnalysisCache")
    command = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
               str(ROOT / "lanhost/install_helpers.ps1"), str(package), str(target), str(old), str(probe)]
    with (output / "install.log").open("w", encoding="utf-8") as log:
        process = subprocess.run(command, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 text=True, timeout=180)
    content = (output / "install.log").read_text(encoding="utf-8", errors="replace")
    if process.returncode or "PORTABLE_RUNTIME_SMOKE_PASSED" not in content:
        print(content[-7000:])
        raise RuntimeError("真实便携环境验收未通过，日志：" + str(output / "install.log"))
    facts = next(json.loads(line) for line in content.splitlines() if line.startswith('{"executable"'))
    facts.update(passed=True, existing_global_python_not_uninstalled=True, old_venv_backed_up=True,
                 repeated_setup_preserved_venv=True, log=str(output / "install.log"))
    (DATA_DIR / "qa/portable-install-result.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(facts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
