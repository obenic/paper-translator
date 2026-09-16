import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf_to_docx


SID = "S-1-5-21-10-20-30-1001"
PRIVILEGED = SID + r"\SOFTWARE\Adobe\Adobe Acrobat\DC\Privileged"
PROTECTED_MODE = (PRIVILEGED, "bProtectedMode")


class RegistryService:
    def __init__(self):
        self.values = {PROTECTED_MODE: 1}
        self.keys = {PRIVILEGED}
        self.denied = set()
        self.Methods_ = Mock()
        self.Methods_.Item.return_value.InParameters.SpawnInstance_.side_effect = (
            SimpleNamespace
        )

    def ExecMethod_(self, method, parameters):
        if (parameters.hDefKey != 0x80000003
                or not parameters.sSubKeyName.startswith(SID + "\\")):
            return SimpleNamespace(ReturnValue=87)
        if method in self.denied:
            return SimpleNamespace(ReturnValue=5)
        if method == "CreateKey":
            self.keys.add(parameters.sSubKeyName)
            return SimpleNamespace(ReturnValue=0)
        key = (parameters.sSubKeyName, parameters.sValueName)
        if method == "GetDWORDValue":
            return SimpleNamespace(ReturnValue=0 if key in self.values else 2,
                                   uValue=self.values.get(key))
        if method == "SetDWORDValue":
            if parameters.sSubKeyName not in self.keys:
                return SimpleNamespace(ReturnValue=2)
            self.values[key] = parameters.uValue
            return SimpleNamespace(ReturnValue=0)
        raise AssertionError(f"Unexpected registry method: {method}")


class AcrobatRegistryTests(unittest.TestCase):
    def setUp(self):
        self.service = RegistryService()
        self.shadow = MagicMock()
        self.shadow.HKEY_USERS = 0xFFFFFFFF80000003
        self.shadow.QueryValueEx.return_value = (0, 4)
        self.com = Mock()
        self.com.GetObject.return_value = self.service
        security = SimpleNamespace(
            OpenProcessToken=Mock(return_value=Mock()),
            GetTokenInformation=Mock(return_value=("token-user", 0)),
            ConvertSidToStringSid=Mock(return_value=SID),
            TokenUser=1,
        )
        patches = [
            patch.object(pdf_to_docx, "winreg", self.shadow),
            patch.object(pdf_to_docx, "win32", self.com),
            patch.dict(sys.modules, {
                "win32api": SimpleNamespace(GetCurrentProcess=lambda: 1),
                "win32con": SimpleNamespace(TOKEN_QUERY=8),
                "win32security": security,
            }),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reads_real_user_value_instead_of_host_shadow(self):
        self.assertEqual(pdf_to_docx.reg_get(
            pdf_to_docx.PRIV_KEY, "bProtectedMode"), 1)

    def test_changes_and_restores_the_value_acrobat_actually_reads(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                pdf_to_docx, "PM_STASH", str(Path(tmp) / "restore")):
            with pdf_to_docx.protected_mode_off():
                self.assertEqual(self.service.values[PROTECTED_MODE], 0)
            self.assertEqual(self.service.values[PROTECTED_MODE], 1)
            self.assertFalse(Path(pdf_to_docx.PM_STASH).exists())

    def test_missing_real_value_does_not_fall_back_to_shadow(self):
        self.service.values.clear()
        self.assertIsNone(pdf_to_docx.reg_get(
            pdf_to_docx.PRIV_KEY, "bProtectedMode"))

    def test_denied_read_is_not_reported_as_disabled_protection(self):
        self.service.denied.add("GetDWORDValue")
        with self.assertRaises(OSError) as error:
            pdf_to_docx.reg_get(pdf_to_docx.PRIV_KEY, "bProtectedMode")
        self.assertEqual(error.exception.errno, 5)

    def test_denied_write_does_not_report_success(self):
        self.service.denied.add("SetDWORDValue")
        with self.assertRaises(OSError) as error:
            pdf_to_docx.reg_set(pdf_to_docx.PRIV_KEY, "bProtectedMode", 0)
        self.assertEqual(error.exception.errno, 5)
        self.assertEqual(self.service.values[PROTECTED_MODE], 1)

    def test_docx_settings_are_created_in_the_same_real_user_hive(self):
        pdf_to_docx.reg_set(pdf_to_docx.DOCX_SETTINGS, "iLayoutMode", 0)
        key = (SID + r"\SOFTWARE\Adobe\Adobe Acrobat\DC"
               r"\AVConversionFromPDF\cSettings\c1\cSettings", "iLayoutMode")
        self.assertEqual(self.service.values.get(key), 0)

    def test_unavailable_service_does_not_fall_back_to_host_registry(self):
        self.com.GetObject.side_effect = RuntimeError("registry service unavailable")
        with self.assertRaises(OSError):
            pdf_to_docx.reg_get(pdf_to_docx.PRIV_KEY, "bProtectedMode")

    def test_failed_crash_recovery_preserves_the_original_value_record(self):
        self.service.values[PROTECTED_MODE] = 0
        self.service.denied.add("SetDWORDValue")
        with tempfile.TemporaryDirectory() as tmp:
            stash = Path(tmp) / "restore"
            stash.write_text("1", encoding="ascii")
            with patch.object(pdf_to_docx, "PM_STASH", str(stash)), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                pdf_to_docx.restore_stashed_protected_mode()
            self.assertTrue(stash.exists(), "failed restoration must keep its record")
            self.assertEqual(stash.read_text(encoding="ascii"), "1")
        self.assertEqual(self.service.values[PROTECTED_MODE], 0)


if __name__ == "__main__":
    unittest.main()
