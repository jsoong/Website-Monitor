"""``packaging/installer.iss`` and ``packaging/build.py``'s idea of it.

Inno Setup's compiler only runs on Windows, so these tests read the script. They pin what cannot
change without breaking users (the product GUID, never deleting the data folder) and what has to
agree with the rest of the repository (program names, the Task Scheduler task, the CLI flags).
What actually runs the installer is ``packaging/verify-install.ps1`` on a Windows machine.
"""

from __future__ import annotations

import re
import sys

import pytest

from tests.packaging.helpers import PACKAGING, ROOT, load_packaging_module

bundle = load_packaging_module("bundle")
build = load_packaging_module("build")

ISS = (PACKAGING / "installer.iss").read_text(encoding="utf-8")

#: Released installers carry this product id. Changing it makes Windows treat the next version as
#: a different product: two installs side by side, two uninstall entries, no upgrade.
PINNED_APP_GUID = "413146B5-7D13-4194-82D8-B6CCFBEF6A15"


def section(name: str) -> str:
    m = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[\w+\]\s*$|\Z)", ISS, re.MULTILINE | re.DOTALL)
    assert m, f"no [{name}] section"
    return m.group(1)


def setup_directives() -> dict[str, str]:
    found = {}
    for line in section("Setup").splitlines():
        if line.strip() and not line.lstrip().startswith(";"):
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip()
    return found


def code_without_comments_and_strings() -> str:
    code = section("Code")
    code = re.sub(r"\{[^}]*\}", " ", code)  # { Pascal comments }
    code = re.sub(r"'(?:[^']|'')*'", "''", code)  # strings
    return code


# -- identity and upgrades ----------------------------------------------------------------


def test_the_product_guid_never_changes() -> None:
    assert f'#define AppGuid "{PINNED_APP_GUID}"' in ISS
    assert setup_directives()["AppId"] == "{{{#AppGuid}}", "AppId must come from AppGuid"
    # the same GUID builds the registry key the downgrade check reads
    assert r"Uninstall\{{#AppGuid}}_is1" in ISS


def test_the_version_comes_from_the_build_never_from_the_script() -> None:
    assert re.search(r"#ifndef AppVersion\s+#error", ISS), "no default: a forgotten /D must fail"
    assert setup_directives()["AppVersion"] == "{#AppVersion}"
    assert not re.search(r"\d+\.\d+\.\d+", re.sub(r";.*", "", ISS)), "a literal version number"


def test_the_installer_file_name_is_what_the_build_script_expects() -> None:
    expected = build.installer_name("{#AppVersion}")
    assert setup_directives()["OutputBaseFilename"] + ".exe" == expected


def test_it_installs_where_the_build_puts_the_files() -> None:
    assert '#define SourceDir "..\\dist\\PageWatch"' in ISS
    assert (PACKAGING / ".." / "dist" / "PageWatch").resolve() == (
        ROOT / "dist" / bundle.BUNDLE_NAME
    )
    assert "{#SourceDir}\\*" in section("Files")
    assert {"recursesubdirs", "createallsubdirs"} <= set(re.findall(r"\w+", section("Files")))


def test_the_installer_icon_exists() -> None:
    assert setup_directives()["SetupIconFile"] == "{#SourcePath}assets\\pagewatch.ico"
    assert (PACKAGING / "assets" / "pagewatch.ico").is_file()


# -- per-user, 64-bit, Windows 10+ ----------------------------------------------------------


def test_the_default_install_needs_no_administrator() -> None:
    s = setup_directives()
    assert s["PrivilegesRequired"] == "lowest"
    assert s["PrivilegesRequiredOverridesAllowed"] == "dialog"  # "for all users" stays possible
    assert s["DefaultDirName"] == "{autopf}\\{#AppName}"
    assert '#define AppName "PageWatch"' in ISS


def test_it_targets_64_bit_windows_10_and_later() -> None:
    s = setup_directives()
    assert s["ArchitecturesAllowed"] == "x64compatible"
    assert s["ArchitecturesInstallIn64BitMode"] == "x64compatible"
    assert s["MinVersion"] == "10.0"


def test_setup_directives_are_ones_inno_setup_knows() -> None:
    known = {
        "AppId", "AppName", "AppVersion", "AppVerName", "AppPublisher", "DefaultDirName",
        "DefaultGroupName", "DisableProgramGroupPage", "PrivilegesRequired",
        "PrivilegesRequiredOverridesAllowed", "ArchitecturesAllowed",
        "ArchitecturesInstallIn64BitMode", "MinVersion", "OutputBaseFilename", "SetupIconFile",
        "UninstallDisplayIcon", "Compression", "SolidCompression", "WizardStyle",
        "CloseApplications", "RestartApplications", "OutputDir",
    }  # fmt: skip
    assert set(setup_directives()) <= known


# -- autostart ------------------------------------------------------------------------------


def test_autostart_is_a_task_that_is_on_by_default() -> None:
    line = next(ln for ln in section("Tasks").splitlines() if ln.startswith('Name: "autostart"'))
    assert "unchecked" not in line


def test_it_registers_the_engine_through_the_cli() -> None:
    assert (
        "RunCli('service install --exe \"' + ExpandConstant('{app}\\pagewatch-engine.exe')" in ISS
    )
    assert "RunCli('service uninstall')" in ISS


def test_the_task_is_registered_for_the_user_who_ran_setup_not_the_elevated_account() -> None:
    assert "ExecAsOriginalUser(ExpandConstant('{app}\\pagewatch-cli.exe')" in ISS
    assert "runasoriginaluser" in section("Run")


def test_the_task_name_is_the_one_the_cli_creates() -> None:
    assert '#define TaskName "PageWatch Engine"' in ISS
    service = ROOT / "src" / "pagewatch" / "cli" / "service.py"
    if not service.exists():
        pytest.skip("pagewatch.cli.service arrives with M5; this check comes alive after the merge")
    assert 'TASK_NAME = "PageWatch Engine"' in service.read_text(encoding="utf-8")


def test_the_flags_the_installer_passes_exist_on_the_cli() -> None:
    from pagewatch.cli.main import build_parser

    parser = build_parser()
    try:
        install = parser.parse_args(["service", "install", "--exe", "C:\\x\\pagewatch-engine.exe"])
        parser.parse_args(["service", "uninstall"])
    except SystemExit:
        pytest.skip(
            "`pagewatch-cli service` arrives with M5; this check comes alive after the merge"
        )
    assert install.exe == "C:\\x\\pagewatch-engine.exe"


def test_every_program_the_installer_names_is_one_the_bundle_builds() -> None:
    named = set(re.findall(r"pagewatch-(?:engine|ui|cli)\b", ISS))
    assert named == {c.exe for c in bundle.COMPONENTS}


# -- upgrades and uninstalling --------------------------------------------------------------


def test_the_engine_is_stopped_before_files_are_replaced_and_restarted_after() -> None:
    assert re.search(r"function PrepareToInstall.*?StopEngine\(\)", ISS, re.DOTALL)
    assert "/End /TN" in ISS and "/F /T /IM pagewatch-engine.exe" in ISS
    post = re.search(r"procedure CurStepChanged.*?\nend;", ISS, re.DOTALL)
    assert post and "ssPostInstall" in post.group(0) and "/Run /TN" in post.group(0)


def test_the_engine_is_asked_to_quit_before_anything_is_killed() -> None:
    """``pagewatch-cli stop --wait 30`` first; the two hard stops stay as the fallback (the CLI of
    a release from before ``stop`` existed, or none yet on a first install, cannot do it)."""
    stop = re.search(r"procedure StopEngine.*?\nend;", ISS, re.DOTALL)
    assert stop
    body = stop.group(0)
    graceful = body.index("RunCli('stop --wait 30')")
    assert graceful < body.index("/End /TN") < body.index("/F /T /IM pagewatch-engine.exe")
    assert "EngineWasRunning := EngineIsRunning();" in body
    # Pascal Script reads top to bottom: RunCli has to exist before StopEngine uses it
    assert ISS.index("function RunCli") < ISS.index("procedure StopEngine")


def test_the_cli_stop_command_the_installer_calls_exists_with_that_flag() -> None:
    from pagewatch.cli.main import build_parser

    args = build_parser().parse_args(["stop", "--wait", "30"])
    assert args.cmd == "stop" and args.wait == 30


def test_it_refuses_to_install_an_older_version_over_a_newer_one() -> None:
    m = re.search(r"function InitializeSetup.*?\nend;", ISS, re.DOTALL)
    assert m
    body = m.group(0)
    assert "ComparePackedVersion(InstalledPacked, MinePacked) > 0" in body
    assert "Result := False" in body


def test_the_data_folder_is_only_ever_deleted_when_the_user_says_yes() -> None:
    assert "[UninstallDelete]" not in ISS, "files listed there are deleted without asking"
    assert ISS.count("DelTree(") == 1
    m = re.search(r"usPostUninstall(.*?)\nend;", ISS, re.DOTALL)
    assert m
    body = m.group(1)
    assert "not UninstallSilent()" in body, "a silent uninstall must keep the data"
    assert "MB_YESNO or MB_DEFBUTTON2" in body, "the default answer is No"
    assert "{localappdata}\\PageWatch" in body
    assert body.index("MsgBox(") < body.index("DelTree(")


def test_uninstall_takes_the_task_away_while_the_program_still_exists() -> None:
    m = re.search(r"usUninstall(.*?)usPostUninstall", ISS, re.DOTALL)
    assert m and "RunCli('service uninstall')" in m.group(1)


def test_the_user_guide_is_installed_and_linked() -> None:
    assert '"{app}\\USER_GUIDE.md"' in section("Icons")
    assert build.USER_GUIDE == ROOT / "docs" / "USER_GUIDE.md"


# -- structure -----------------------------------------------------------------------------


def test_begin_and_end_balance_in_the_pascal_code() -> None:
    code = code_without_comments_and_strings()
    begins = len(re.findall(r"\bbegin\b", code, re.IGNORECASE))
    ends = len(re.findall(r"\bend\b", code, re.IGNORECASE))
    assert begins == ends and begins > 5


def test_every_function_the_code_calls_is_defined_or_built_in() -> None:
    code = code_without_comments_and_strings()
    defined = {m.lower() for m in re.findall(r"^\s*(?:function|procedure)\s+(\w+)", code, re.M)}
    built_in = {
        "exec", "execasoriginaluser", "expandconstant", "log", "sleep", "regquerystringvalue",
        "strtoversion", "comparepackedversion", "suppressiblemsgbox", "wizardistaskselected",
        "direxists", "uninstallsilent", "msgbox", "deltree", "inttostr",
        "initializesetup", "preparetoinstall", "curstepchanged", "curuninstallstepchanged",
    }  # fmt: skip
    called = {m.lower() for m in re.findall(r"\b([A-Za-z_]\w*)\s*\(", code)}
    keywords = {"if", "then", "and", "or", "not", "begin", "end", "while", "do", "else"}
    unknown = called - defined - built_in - keywords
    assert not unknown, (
        f"not defined here and not a function this test knows is built in: {unknown}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the compiler is what checks it there")
def test_the_installer_cannot_be_built_off_windows() -> None:
    import argparse

    args = argparse.Namespace(
        out=ROOT / "dist", work=ROOT / "build" / "x", no_ui=True, clean=False, smoke=False,
        portable=False, installer=True,
    )  # fmt: skip
    with pytest.raises(build.BuildError, match="needs Windows"):
        build.build(args)


def test_the_launch_page_of_the_installer_runs_the_ui_as_the_original_user() -> None:
    line = next(ln for ln in section("Run").splitlines() if "pagewatch-ui.exe" in ln)
    assert {"postinstall", "skipifsilent", "nowait", "runasoriginaluser"} <= set(
        re.findall(r"\w+", line)
    )


def test_the_script_is_pure_ascii_so_no_compiler_codepage_surprises() -> None:
    ISS.encode("ascii")
