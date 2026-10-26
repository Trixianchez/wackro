"""Tests for the packaging side of step 7: version, resource_path, Wackro.spec, build.ps1,
requirements-build.txt, .gitignore.

The spec is a Python file, so it is executed here against fake PyInstaller classes (Analysis, PYZ,
EXE). That checks what the real build would be told to do without needing PyInstaller installed.
"""

import contextlib
import io
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

SPEC_PATH = ROOT / "Wackro.spec"


class VersionTests(unittest.TestCase):
    def test_version_looks_like_semver(self):
        self.assertRegex(config.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_changelog_has_a_release_entry_for_this_version(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^## \[" + re.escape(config.APP_VERSION) + r"\] - \d{4}-\d{2}-\d{2}$")

    def test_readme_explains_the_build(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("build.ps1", text)
        self.assertIn("Wackro-Admin.exe", text)


class ResourcePathTests(unittest.TestCase):
    def test_icon_path_is_inside_assets(self):
        self.assertEqual(config.ICON_RELATIVE_PATH, Path("assets") / "Wackro.ico")

    def test_from_source_it_is_next_to_config_py(self):
        had, saved = hasattr(sys, "_MEIPASS"), getattr(sys, "_MEIPASS", None)
        if had:
            del sys._MEIPASS  # only set inside a PyInstaller bundle
        try:
            self.assertEqual(
                config.resource_path("assets/x.ico"), Path(config.__file__).resolve().parent / "assets/x.ico"
            )
        finally:
            if had:
                sys._MEIPASS = saved

    def test_when_frozen_it_is_inside_the_bundle_folder(self):
        with mock.patch.object(sys, "_MEIPASS", "/tmp/_MEI12345", create=True):
            self.assertEqual(config.resource_path("assets/x.ico"), Path("/tmp/_MEI12345") / "assets/x.ico")

    def test_accepts_path_objects(self):
        with mock.patch.object(sys, "_MEIPASS", "/tmp/_MEI1", create=True):
            self.assertEqual(config.resource_path(config.ICON_RELATIVE_PATH), Path("/tmp/_MEI1/assets/Wackro.ico"))

    def test_returns_a_path(self):
        self.assertIsInstance(config.resource_path("x"), Path)


def _run_spec(env_admin=None, platform="linux", with_icon=False, with_tray=False):
    """Execute Wackro.spec with fakes. Returns (captured, printed_output)."""
    captured = {}

    class Analysis:
        def __init__(self, scripts, **kwargs):
            captured["scripts"] = scripts
            captured["analysis"] = kwargs
            self.pure, self.scripts, self.binaries, self.datas = "pure", "scripts", "binaries", "datas"

    class PYZ:
        def __init__(self, pure):
            captured["pyz"] = pure

    class EXE:
        def __init__(self, *args, **kwargs):
            captured["exe_args"] = args
            captured["exe"] = kwargs

    def collect_all(name):
        captured.setdefault("collect_all", []).append(name)
        return [("cust_data", "customtkinter")], [], ["customtkinter.extra"]

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = collect_all
    fake_modules = {
        "PyInstaller": types.ModuleType("PyInstaller"),
        "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
        "PyInstaller.utils.hooks": hooks,
    }

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "assets").mkdir()
        if with_icon:
            (Path(tmp) / "assets" / "Wackro.ico").write_bytes(b"ico")
        if with_tray:
            for state in ("idle", "recording", "playing"):
                (Path(tmp) / "assets" / f"tray_{state}.png").write_bytes(b"png")
        env = {"WACKRO_ADMIN": env_admin} if env_admin is not None else {}
        namespace = {"SPECPATH": tmp, "Analysis": Analysis, "PYZ": PYZ, "EXE": EXE, "__name__": "spec"}
        out = io.StringIO()
        with mock.patch.dict(sys.modules, fake_modules), mock.patch.dict("os.environ", env, clear=False):
            if env_admin is None:
                import os
                os.environ.pop("WACKRO_ADMIN", None)
            with mock.patch.object(sys, "platform", platform), contextlib.redirect_stdout(out):
                exec(compile(SPEC_PATH.read_text(encoding="utf-8"), str(SPEC_PATH), "exec"), namespace)
        captured["tmp"] = tmp
    return captured, out.getvalue()


class SpecTests(unittest.TestCase):
    def test_spec_file_exists_and_compiles(self):
        compile(SPEC_PATH.read_text(encoding="utf-8"), str(SPEC_PATH), "exec")

    def test_normal_build(self):
        captured, _ = _run_spec()
        self.assertEqual(captured["exe"]["name"], "Wackro")
        self.assertIs(captured["exe"]["uac_admin"], False)

    def test_admin_build_when_env_is_1(self):
        captured, _ = _run_spec(env_admin="1")
        self.assertEqual(captured["exe"]["name"], "Wackro-Admin")
        self.assertIs(captured["exe"]["uac_admin"], True)

    def test_other_env_values_mean_normal(self):
        for value in ("0", "", "true", "yes"):
            with self.subTest(value=value):
                captured, _ = _run_spec(env_admin=value)
                self.assertEqual(captured["exe"]["name"], "Wackro")
                self.assertIs(captured["exe"]["uac_admin"], False)

    def test_windowed_no_upx(self):
        captured, _ = _run_spec()
        self.assertIs(captured["exe"]["console"], False)
        self.assertIs(captured["exe"]["upx"], False)
        self.assertIs(captured["exe"]["debug"], False)

    def test_entry_point_is_main_py(self):
        captured, _ = _run_spec()
        self.assertEqual([Path(p).name for p in captured["scripts"]], ["main.py"])
        self.assertEqual(Path(captured["scripts"][0]).parent, Path(captured["tmp"]))
        self.assertEqual([Path(p) for p in captured["analysis"]["pathex"]], [Path(captured["tmp"])])

    def test_customtkinter_is_collected_with_its_data(self):
        captured, _ = _run_spec()
        self.assertEqual(captured["collect_all"], ["customtkinter"])
        self.assertIn(("cust_data", "customtkinter"), captured["analysis"]["datas"])
        self.assertIn("customtkinter.extra", captured["analysis"]["hiddenimports"])

    def test_pynput_backend_matches_the_platform(self):
        expected = {
            "win32": ("pynput.keyboard._win32", "pynput.mouse._win32", "pynput._util.win32", "pystray._win32"),
            "darwin": ("pynput.keyboard._darwin", "pynput.mouse._darwin", "pynput._util.darwin", "pystray._darwin"),
            "linux": ("pynput.keyboard._xorg", "pynput.mouse._xorg", "pynput._util.xorg", "pystray._xorg"),
        }
        for platform, modules in expected.items():
            with self.subTest(platform=platform):
                hidden = _run_spec(platform=platform)[0]["analysis"]["hiddenimports"]
                for module in modules:
                    self.assertIn(module, hidden)
                others = {m for p, ms in expected.items() if p != platform for m in ms}
                self.assertFalse(others & set(hidden))

    def test_icon_is_used_and_bundled_when_present(self):
        captured, output = _run_spec(with_icon=True, with_tray=True)
        icon = Path(captured["tmp"]) / "assets" / "Wackro.ico"
        self.assertEqual(Path(captured["exe"]["icon"]), icon)
        self.assertIn((str(icon), "assets"), captured["analysis"]["datas"])
        self.assertNotIn("WARNING", output)

    def test_missing_icon_warns_but_still_builds(self):
        captured, output = _run_spec(with_icon=False, with_tray=False)
        self.assertIsNone(captured["exe"]["icon"])
        self.assertNotIn("assets", [dest for _, dest in captured["analysis"]["datas"]])
        self.assertIn("WARNING", output)
        self.assertIn("Wackro.ico", output)
        self.assertIn("tray_idle.png", output)

    def test_tray_icons_are_bundled_where_config_looks(self):
        captured, _ = _run_spec(with_icon=True, with_tray=True)
        bundled = {Path("assets") / Path(src).name for src, dest in captured["analysis"]["datas"] if dest == "assets"}
        for path in config.TRAY_ICON_PATHS.values():
            with self.subTest(path=path):
                self.assertIn(path, bundled)

    def test_the_real_assets_exist(self):
        self.assertTrue((ROOT / config.ICON_RELATIVE_PATH).is_file())
        for path in config.TRAY_ICON_PATHS.values():
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())

    def test_runtime_requirements_include_the_tray_library(self):
        text = (ROOT / "requirements.txt").read_text().lower()
        self.assertIn("pystray", text)
        self.assertIn("pillow", text)

    def test_bundled_icon_location_matches_resource_path(self):
        """The spec puts the icon at <bundle>/assets/, and config.resource_path looks there."""
        captured, _ = _run_spec(with_icon=True)
        source, destination = [d for d in captured["analysis"]["datas"] if d[1] == "assets"][0]
        self.assertEqual(Path(destination) / Path(source).name, config.ICON_RELATIVE_PATH)


class BuildFilesTests(unittest.TestCase):
    def test_build_script_has_what_the_docs_promise(self):
        text = (ROOT / "build.ps1").read_text(encoding="utf-8")
        for needle in ("[switch]$SkipTests", '[ValidateSet("Both", "Normal", "Admin")]', "[string]$Only", "WACKRO_ADMIN", "Wackro.spec",
                       "$LASTEXITCODE", "requirements-build.txt", "VIRTUAL_ENV", "Get-FileHash",
                       "Wackro-Admin"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_build_script_never_touches_git(self):
        text = (ROOT / "build.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^\s*git\s")

    def test_build_requirements(self):
        lines = [l.strip() for l in (ROOT / "requirements-build.txt").read_text().splitlines() if l.strip()]
        self.assertIn("-r requirements.txt", lines)
        self.assertTrue(any(l.lower().startswith("pyinstaller") for l in lines))

    def test_runtime_requirements_do_not_pull_in_pyinstaller(self):
        self.assertNotIn("pyinstaller", (ROOT / "requirements.txt").read_text().lower())

    def test_gitignore_keeps_the_spec_but_ignores_build_output(self):
        lines = [l.strip() for l in (ROOT / ".gitignore").read_text().splitlines()]
        self.assertNotIn("*.spec", lines)
        self.assertNotIn("Wackro.spec", lines)
        self.assertIn("build/", lines)
        self.assertIn("dist/", lines)

    def test_assets_folder_has_instructions_for_the_icon(self):
        self.assertIn("Wackro.ico", (ROOT / "assets" / "README.md").read_text(encoding="utf-8"))

    def test_checklist_covers_the_exe(self):
        text = (ROOT / "WINDOWS_CHECKLIST.md").read_text(encoding="utf-8")
        self.assertIn("Wackro-Admin.exe", text)
        self.assertIn("SmartScreen", text)


class InstallerTests(unittest.TestCase):
    """installer/Wackro.iss has to agree with the code it installs."""

    def setUp(self):
        self.iss = (ROOT / "installer" / "Wackro.iss").read_text(encoding="utf-8")

    def test_it_installs_the_folder_build_and_both_shortcuts(self):
        self.assertIn(r'Source: "..\dist\app\Wackro\*"', self.iss)
        self.assertIn('Parameters: "--admin"', self.iss)
        import main
        self.assertEqual(main.ADMIN_FLAG, "--admin")
        self.assertIn("{autodesktop}", self.iss)
        self.assertIn("PrivilegesRequiredOverridesAllowed=dialog", self.iss)

    def test_it_waits_for_the_running_app(self):
        import single_instance
        self.assertIn("AppMutex=Local\\" + config.APP_NAME, self.iss)
        self.assertIn('f"Local\\\\{self._name}"', (ROOT / "single_instance.py").read_text(encoding="utf-8"))
        self.assertEqual(single_instance.SingleInstance.__init__.__defaults__[0], config.APP_NAME)

    def test_the_recordings_folder_file_matches_folders_py(self):
        import folders
        self.assertIn("{app}\\" + folders.INSTALL_INFO_NAME, self.iss)
        self.assertIn('{"recordings_dir": "', self.iss)
        self.assertIn("{userdocs}\\" + folders.FOLDER_NAME, self.iss)

    def test_uninstall_asks_and_keeps_recordings(self):
        self.assertIn("{localappdata}\\" + config.APP_NAME, self.iss)
        self.assertIn("MB_DEFBUTTON2", self.iss)          # "No" is the default answer
        self.assertIn("recordings are kept", self.iss)

    def test_the_build_and_the_release_make_the_setup(self):
        build = (ROOT / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("WACKRO_ONEDIR", build)
        self.assertIn("installer\\Wackro.iss", build)
        self.assertIn("/DAppVersion=", build)
        self.assertIn("ONEDIR", (ROOT / "Wackro.spec").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("build.ps1 -Installer", workflow)
        self.assertIn("Wackro-Setup-", workflow)


if __name__ == "__main__":
    unittest.main()
