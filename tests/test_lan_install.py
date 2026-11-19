"""真实 Windows PowerShell 5.1 安装辅助流程回归；不安装 Python/QQ、不接管桌面。"""
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from inquiry.runtime import ROOT, TEMP_DIR


class LanInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix="lan-install-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.runtime = self.folder / "runtime"
        self.archive = self.folder / "application.zip"
        with zipfile.ZipFile(self.archive, "w") as z:
            z.writestr("main.py", "# fixture\n")
            z.writestr("requirements.lock.txt", "# fixture\n")
            z.writestr("lanhost/setup.py", "# fixture\n")
        self.helper = ROOT / "lanhost/install_helpers.ps1"

    def powershell(self, body):
        preamble = "$ErrorActionPreference='Stop'\n. $args[0]\n$root=$args[1]\n$archive=$args[2]\n$python=$args[3]\n"
        script = self.folder / "test.ps1"
        script.write_text(preamble + body + "\nWrite-Output 'PASS'\n", encoding="utf-8-sig")
        env = os.environ.copy()
        env["PSModuleAnalysisCachePath"] = str(self.folder / "ModuleAnalysisCache")
        command = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(self.helper),
                   str(self.runtime), str(self.archive), str(ROOT / ".venv/Scripts/python.exe")]
        result = subprocess.run(command, cwd=self.folder, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_legacy_partial_resume_preserves_business_data_and_restores_missing_source(self):
        self.powershell(r"""
New-Item -ItemType Directory -Path "$root\data\logs" -Force | Out-Null
[IO.File]::WriteAllText("$root\data\logs\setup.log", 'old failure')
$entries=@(Get-SetupApplication $archive)
$plan=Get-SetupPlan $root 'worker' 'device' 'office' $archive $entries
if(-not $plan.Resume){throw 'Expected early legacy resume'}
Copy-SetupApplication $archive $root $entries
[IO.File]::WriteAllText("$root\.env", 'customer-secret-fixture')
[IO.File]::WriteAllText("$root\data\inquiry.db", 'customer-db-fixture')
$plan=Get-SetupPlan $root 'worker' 'device' 'office' $archive $entries
Save-SetupState $root $plan.State 'dependencies' 'failed'
Remove-Item -LiteralPath "$root\lanhost\setup.py"
$plan=Get-SetupPlan $root 'worker' 'device' 'office' $archive $entries
Copy-SetupApplication $archive $root $entries
Copy-SetupInitialFile "$root\main.py" "$root\.env"
Copy-SetupInitialFile "$root\main.py" "$root\data\inquiry.db"
if((Get-Content -LiteralPath "$root\.env" -Raw) -ne 'customer-secret-fixture'){throw 'Secret changed'}
if((Get-Content -LiteralPath "$root\data\inquiry.db" -Raw) -ne 'customer-db-fixture'){throw 'Database changed'}
if(-not (Test-Path -LiteralPath "$root\lanhost\setup.py")){throw 'Missing source not restored'}
""")

    def test_conflicting_source_foreign_folder_or_device_is_preserved(self):
        self.powershell(r"""
$entries=@(Get-SetupApplication $archive)
New-Item -ItemType Directory -Path "$root\data" -Force | Out-Null
Copy-SetupApplication $archive $root $entries
$state=@{version=1;role='worker';device='device';controller_id='office';application_hash=(Get-SetupDigest $archive)}
Save-SetupState $root $state 'application'
$rejected=$false
try{Get-SetupPlan $root 'worker' 'other-device' 'office' $archive $entries | Out-Null}catch{$rejected=$true}
if(-not $rejected){throw 'Accepted wrong device'}
[IO.File]::WriteAllText("$root\main.py", 'custom-code')
$rejected=$false
try{Get-SetupPlan $root 'worker' 'device' 'office' $archive $entries | Out-Null}catch{$rejected=$true}
if(-not $rejected -or (Get-Content -LiteralPath "$root\main.py" -Raw) -ne 'custom-code'){throw 'Source conflict was overwritten'}
$foreign=$root+'-foreign'
New-Item -ItemType Directory -Path $foreign | Out-Null
[IO.File]::WriteAllText("$foreign\keep.txt", 'keep')
$rejected=$false
try{Get-SetupPlan $foreign 'worker' 'device' 'office' $archive $entries | Out-Null}catch{$rejected=$true}
if(-not $rejected -or -not (Test-Path -LiteralPath "$foreign\keep.txt")){throw 'Foreign folder was altered'}
""")

    def test_seed_replaces_only_unchanged_example_and_backs_it_up(self):
        self.powershell(r"""
New-Item -ItemType Directory -Path $root | Out-Null
[IO.File]::WriteAllText("$root\example", 'example')
[IO.File]::WriteAllText("$root\seed", 'private-fixture')
Copy-SetupInitialFile "$root\example" "$root\.env"
Copy-SetupInitialFile "$root\seed" "$root\.env" "$root\example" "$root\backups"
if((Get-Content -LiteralPath "$root\.env" -Raw) -ne 'private-fixture'){throw 'Seed not installed'}
if(@(Get-ChildItem -LiteralPath "$root\backups" -Force).Count -ne 1){throw 'Example backup missing'}
[IO.File]::WriteAllText("$root\.env", 'edited-by-customer')
Copy-SetupInitialFile "$root\seed" "$root\.env" "$root\example" "$root\backups"
if((Get-Content -LiteralPath "$root\.env" -Raw) -ne 'edited-by-customer'){throw 'Customer config replaced'}
if(@(Get-ChildItem -LiteralPath $root -Recurse -Force -Filter *.tmp).Count){throw 'Temporary files left over'}
""")

    def test_enrollment_is_reused_and_wrong_pairing_rejected(self):
        self.powershell(r"""
$entries=@(Get-SetupApplication $archive)
New-Item -ItemType Directory -Path "$root\data" -Force | Out-Null
Copy-SetupApplication $archive $root $entries
$cfg=@{role='worker';worker_device='device';controller_id='office';allow_send=$false;runtime=$root;secret='fixture'}
$cfg | ConvertTo-Json | Set-Content -LiteralPath "$root\data\worker.json" -Encoding UTF8
$plan=Get-SetupPlan $root 'worker' 'device' 'office' $archive $entries
if(-not $plan.Enrolled){throw 'Existing enrollment was not recognized'}
$rejected=$false
try{Get-SetupPlan $root 'worker' 'device' 'different-office' $archive $entries | Out-Null}catch{$rejected=$true}
if(-not $rejected){throw 'Accepted another controller'}
""")

    def test_module_initialization_uses_installed_directory_and_restores_cwd(self):
        self.runtime.mkdir()
        (self.runtime / "setup_location_fixture.py").write_text(
            "from pathlib import Path\nPath('module-ran.txt').write_text(str(Path.cwd()),encoding='utf-8')\n", encoding="utf-8")
        (self.runtime / "setup_failure_fixture.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        self.powershell(r"""
$before=(Get-Location).Path
Invoke-SetupModule $python $root 'setup_location_fixture'
if((Get-Location).Path -ne $before){throw 'Working directory not restored'}
if((Get-Content -LiteralPath "$root\module-ran.txt" -Raw -Encoding UTF8) -ne $root){throw 'Module ran in package folder'}
$rejected=$false
try{Invoke-SetupModule $python $root 'setup_failure_fixture'}catch{$rejected=$true}
if(-not $rejected -or (Get-Location).Path -ne $before){throw 'Module failure lost or directory not restored'}
""")

    def test_unsafe_application_paths_and_junctions_are_rejected(self):
        with zipfile.ZipFile(self.archive, "a") as z:
            z.writestr("lanhost/../../escape.py", "# unsafe")
        self.powershell(r"""
$rejected=$false
try{Get-SetupApplication $archive | Out-Null}catch{$rejected=$true}
if(-not $rejected){throw 'Archive traversal accepted'}
$rejected=$false
try{Assert-DSetupPath 'C:\temp\project-data' | Out-Null}catch{$rejected=$true}
if(-not $rejected){throw 'C drive accepted'}
New-Item -ItemType Directory -Path $root,($root+'-target') -Force | Out-Null
New-Item -ItemType Junction -Path "$root\data" -Target ($root+'-target') | Out-Null
$rejected=$false
try{Assert-DSetupPath "$root\data\log.txt" | Out-Null}catch{$rejected=$true}
if(-not $rejected){throw 'Directory junction accepted'}
""")

    def prepare_repair(self):
        """执行真实补丁脚本，使用只写测试标记的安装器替身。"""
        target = self.folder / "package"
        target.mkdir()
        import hashlib
        import shutil
        shutil.copy2(self.archive, target / "application.zip")
        (target / "install.ps1").write_text("# old installer\n", encoding="ascii")
        manifest = {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                    for name in ("application.zip", "install.ps1")}
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        tool = target / "repair-tools"
        tool.mkdir()
        shutil.copy2(self.helper, tool / "install_helpers.ps1")
        shutil.copy2(ROOT / "lanhost/apply-repair.ps1", tool / "apply-repair.ps1")
        (tool / "install.ps1").write_text("[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'resumed.txt'),'fixture')\n", encoding="ascii")
        patch = {"version": 1, "application_sha256": manifest["application.zip"],
                 "files": {name: hashlib.sha256((tool / name).read_bytes()).hexdigest()
                           for name in ("install.ps1", "install_helpers.ps1")}}
        (tool / "repair.json").write_text(json.dumps(patch), encoding="utf-8")
        return target, tool

    def test_small_repair_updates_manifest_backs_up_original_and_runs_resume(self):
        target, tool = self.prepare_repair()
        self.powershell("& " + "'" + str(tool / "apply-repair.ps1") + "'\n")
        self.assertEqual((target / "resumed.txt").read_text(), "fixture")
        backups = list((target / ".installer-backups").glob("*/install.ps1"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "# old installer\n")
        import hashlib
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8-sig"))
        for name, digest in manifest.items():
            self.assertEqual(hashlib.sha256((target / name).read_bytes()).hexdigest(), digest)

    def test_small_repair_rejects_another_package_before_replacing_files(self):
        target, tool = self.prepare_repair()
        with (target / "application.zip").open("ab") as f:
            f.write(b"changed-package")
        self.powershell("$rejected=$false; try { & '" + str(tool / "apply-repair.ps1") + "' } catch {$rejected=$true}; if(-not $rejected){throw 'Wrong package accepted'}")
        self.assertEqual((target / "install.ps1").read_text(), "# old installer\n")
        self.assertFalse((target / ".installer-backups").exists())

    def test_portable_package_pinning_resume_and_conflict_preservation(self):
        with zipfile.ZipFile(self.archive, "w") as z:
            for name in ("python.exe", "python312.dll", "Lib/venv/__init__.py", "Lib/ensurepip/__init__.py"):
                z.writestr("tools/" + name, "fixture-" + name)
        self.powershell(r"""
$hash=Get-SetupDigest $archive
$rejected=$false
try{Install-SetupPythonPackage $archive $root ('f'*64) | Out-Null}catch{$rejected=$true}
if(-not $rejected -or (Test-Path -LiteralPath $root)){throw 'Wrong runtime hash accepted or root modified'}
New-Item -ItemType Directory -Path "$root\python" -Force | Out-Null
[IO.File]::WriteAllText("$root\python\keep.txt", 'old-runtime')
$base=Install-SetupPythonPackage $archive $root $hash
$again=Install-SetupPythonPackage $archive $root $hash
if($base -ne $again -or -not (Test-Path -LiteralPath "$root\python\keep.txt")){throw 'Old runtime replaced'}
[IO.File]::WriteAllText("$base\python312.dll", 'modified')
$rejected=$false
try{Install-SetupPythonPackage $archive $root $hash | Out-Null}catch{$rejected=$true}
if(-not $rejected -or (Get-Content -LiteralPath "$base\python312.dll" -Raw) -ne 'modified'){throw 'Conflicting runtime file overwritten'}
""")

    def test_paired_environment_is_not_moved_to_a_different_python(self):
        self.powershell(r"""
New-Item -ItemType Directory -Path "$root\.venv" -Force | Out-Null
[IO.File]::WriteAllText("$root\.venv\pyvenv.cfg", 'home = C:\ExistingPython')
[IO.File]::WriteAllText("$root\.venv\keep.txt", 'paired-data')
$rejected=$false
try{Initialize-SetupVenv "$root\python-portable\3.12.10" $root $true}catch{$rejected=$true}
if(-not $rejected -or -not (Test-Path -LiteralPath "$root\.venv\keep.txt")){throw 'Paired environment moved'}
if(Test-Path -LiteralPath "$root\data\setup-backups"){throw 'Unexpected paired backup/move'}
""")

    def test_repair_can_add_portable_python_payload_to_original_package(self):
        import hashlib
        target, tool = self.prepare_repair()
        name = "python.3.12.10.nupkg"
        (tool / name).write_bytes(b"portable-payload-fixture")
        metadata = json.loads((tool / "repair.json").read_text(encoding="utf-8"))
        metadata["files"][name] = hashlib.sha256((tool / name).read_bytes()).hexdigest()
        (tool / "repair.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.powershell("& '" + str(tool / "apply-repair.ps1") + "'")
        self.assertEqual((target / name).read_bytes(), (tool / name).read_bytes())
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest[name], metadata["files"][name])
