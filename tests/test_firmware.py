"""Tests for ppk2.firmware — query, check, upgrade."""

import subprocess
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

from ppk2 import firmware

FIXTURES = Path(__file__).parent / "fixtures"


def _nrfutil_stdout() -> str:
    return (FIXTURES / "nrfutil_fw_info.json").read_text()


class TestModuleScaffold:
    def test_current_version_constants_exist(self):
        assert isinstance(firmware.CURRENT_APPLICATION_VERSION, int)
        assert firmware.CURRENT_APPLICATION_VERSION > 0
        assert isinstance(firmware.CURRENT_SEMVER, str)
        assert isinstance(firmware.CURRENT_COMMIT, str)

    def test_dataclasses_importable(self):
        assert firmware.FirmwareInfo is not None
        assert firmware.UpstreamFirmware is not None

    def test_exceptions_importable(self):
        assert issubclass(firmware.FirmwareToolMissing, RuntimeError)
        assert issubclass(firmware.FirmwareQueryError, RuntimeError)
        assert issubclass(firmware.UpstreamFetchError, RuntimeError)
        assert issubclass(firmware.FirmwareUpgradeError, RuntimeError)


class TestQuery:
    def test_happy_path(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_nrfutil_stdout(), stderr=""
        )
        with patch("ppk2.firmware.subprocess.run", return_value=completed) as mock_run:
            info = firmware.query(serial_number="E2794420999B")

        assert info.serial_number == "E2794420999B"
        assert info.bootloader_type == "NRFDL_BOOTLOADER_TYPE_SDFU"
        assert info.bootloader_version == 3
        assert info.application_version == 20300
        assert "imageInfoList" in info.raw

        args = mock_run.call_args.args[0]
        assert args[:3] == ["nrfutil", "device", "fw-info"]
        assert "--serial-number" in args
        assert "E2794420999B" in args
        assert "--traits" in args
        assert "nordicUsb" in args
        assert "--json" in args

    def test_tool_missing_raises(self):
        with patch(
            "ppk2.firmware.subprocess.run",
            side_effect=FileNotFoundError("nrfutil"),
        ):
            try:
                firmware.query(serial_number="SN")
            except firmware.FirmwareToolMissing as e:
                assert "nrfutil" in str(e)
                assert "install device" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareToolMissing")

    def test_device_subcommand_missing_raises_tool_missing(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="Error: nrfutil command `device` not found.",
        )
        with patch("ppk2.firmware.subprocess.run", return_value=completed):
            try:
                firmware.query(serial_number="SN")
            except firmware.FirmwareToolMissing:
                pass
            else:
                raise AssertionError("expected FirmwareToolMissing")

    def test_nrfutil_nonzero_raises_query_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="",
            stderr="no matching device",
        )
        with patch("ppk2.firmware.subprocess.run", return_value=completed):
            try:
                firmware.query(serial_number="SN")
            except firmware.FirmwareQueryError as e:
                assert "no matching device" in str(e)
            else:
                raise AssertionError("expected FirmwareQueryError")

    def test_malformed_json_raises_query_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not valid json\n", stderr="",
        )
        with patch("ppk2.firmware.subprocess.run", return_value=completed):
            try:
                firmware.query(serial_number="SN")
            except firmware.FirmwareQueryError as e:
                assert "unparseable" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareQueryError")

    def test_missing_info_event_raises_query_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"type":"task_begin","data":{}}\n', stderr="",
        )
        with patch("ppk2.firmware.subprocess.run", return_value=completed):
            try:
                firmware.query(serial_number="SN")
            except firmware.FirmwareQueryError as e:
                assert "info" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareQueryError")

    def test_missing_image_info_raises_query_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                '{"type":"info","data":{"devices":[{"bootloaderType":"X",'
                '"imageInfoList":[],"serialNumber":"SN"}]}}\n'
            ),
            stderr="",
        )
        with patch("ppk2.firmware.subprocess.run", return_value=completed):
            try:
                firmware.query(serial_number="SN")
            except firmware.FirmwareQueryError as e:
                assert "bootloader" in str(e).lower() or "application" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareQueryError")


class TestFetchUpstream:
    def _urlopen_with(self, body: bytes):
        mock = MagicMock()
        mock.__enter__.return_value = BytesIO(body)
        mock.__exit__.return_value = False
        return mock

    def test_happy_path(self):
        body = (FIXTURES / "github_contents_firmware.json").read_bytes()
        with patch(
            "ppk2.firmware.urllib.request.urlopen",
            return_value=self._urlopen_with(body),
        ) as mock_urlopen:
            upstream = firmware.fetch_upstream()

        assert upstream.semver == "1.2.4"
        assert upstream.commit == "db16a94"
        assert upstream.filename == "pca63100_ppk2_1.2.4_db16a94.hex"
        assert upstream.url.startswith("https://raw.githubusercontent.com/")
        req = mock_urlopen.call_args.args[0]
        url_str = req.full_url if hasattr(req, "full_url") else req
        assert (
            "api.github.com/repos/NordicSemiconductor/pc-nrfconnect-ppk/contents/firmware"
            in url_str
        )

    def test_http_error_raises_fetch_error(self):
        err = HTTPError(firmware.GITHUB_CONTENTS_URL,
                        403, "rate limit", {}, None)
        with patch("ppk2.firmware.urllib.request.urlopen", side_effect=err):
            try:
                firmware.fetch_upstream()
            except firmware.UpstreamFetchError as e:
                assert "403" in str(e)
            else:
                raise AssertionError("expected UpstreamFetchError")

    def test_url_error_raises_fetch_error(self):
        with patch(
            "ppk2.firmware.urllib.request.urlopen",
            side_effect=URLError("name resolution failed"),
        ):
            try:
                firmware.fetch_upstream()
            except firmware.UpstreamFetchError as e:
                assert "network" in str(e).lower()
            else:
                raise AssertionError("expected UpstreamFetchError")

    def test_malformed_json_raises_fetch_error(self):
        with patch(
            "ppk2.firmware.urllib.request.urlopen",
            return_value=self._urlopen_with(b"not json"),
        ):
            try:
                firmware.fetch_upstream()
            except firmware.UpstreamFetchError as e:
                assert "unparseable" in str(e).lower()
            else:
                raise AssertionError("expected UpstreamFetchError")

    def test_no_hex_file_raises_fetch_error(self):
        with patch(
            "ppk2.firmware.urllib.request.urlopen",
            return_value=self._urlopen_with(b"[]"),
        ):
            try:
                firmware.fetch_upstream()
            except firmware.UpstreamFetchError as e:
                assert "no .hex" in str(e).lower()
            else:
                raise AssertionError("expected UpstreamFetchError")

    def test_bad_filename_raises_fetch_error(self):
        body = (
            b'[{"name":"unexpected_name.hex","type":"file",'
            b'"download_url":"https://example.com/x.hex"}]'
        )
        with patch(
            "ppk2.firmware.urllib.request.urlopen",
            return_value=self._urlopen_with(body),
        ):
            try:
                firmware.fetch_upstream()
            except firmware.UpstreamFetchError as e:
                assert "pattern" in str(e).lower()
            else:
                raise AssertionError("expected UpstreamFetchError")


class TestDownloadUpstream:
    def test_downloads_to_dest_dir(self, tmp_path):
        fake_upstream = firmware.UpstreamFirmware(
            semver="1.2.4", commit="db16a94",
            filename="pca63100_ppk2_1.2.4_db16a94.hex",
            url="https://example.com/hex",
        )
        hex_bytes = b":10000000FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00\n"

        urlopen_mock = MagicMock()
        urlopen_mock.__enter__.return_value = BytesIO(hex_bytes)
        urlopen_mock.__exit__.return_value = False

        with patch(
            "ppk2.firmware.fetch_upstream", return_value=fake_upstream
        ), patch(
            "ppk2.firmware.urllib.request.urlopen", return_value=urlopen_mock
        ):
            path, upstream = firmware.download_upstream(dest_dir=tmp_path)

        assert upstream == fake_upstream
        assert path.parent == tmp_path
        assert path.name == fake_upstream.filename
        assert path.read_bytes() == hex_bytes


class TestUpgrade:
    def _info(self, app: int = 20300) -> firmware.FirmwareInfo:
        return firmware.FirmwareInfo(
            serial_number="SN",
            bootloader_type="NRFDL_BOOTLOADER_TYPE_SDFU",
            bootloader_version=3,
            application_version=app,
            raw={},
        )

    def test_happy_path_packages_hex_then_programs_zip(self, tmp_path):
        """Upgrade must wrap the .hex into a SDFU zip (pkg generate) before
        calling `nrfutil device program`, because nordicUsb devices reject
        raw hex with 'invalid Zip archive: Could not find EOCD'."""
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")

        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"type":"info","data":{}}\n', stderr="",
        )
        with patch(
            "ppk2.firmware._pid_path_exists_and_alive", return_value=False
        ), patch(
            "ppk2.firmware.subprocess.run", return_value=completed
        ) as mock_run, patch(
            "ppk2.firmware.query", return_value=self._info(app=20300)
        ) as mock_query:
            info = firmware.upgrade("SN", hex_path)

        assert info.application_version == 20300

        # Two subprocess calls in order: pkg generate, then device program.
        assert mock_run.call_count == 2
        first_args = mock_run.call_args_list[0].args[0]
        second_args = mock_run.call_args_list[1].args[0]

        # Call 1: nrfutil nrf5sdk-tools pkg generate ... --application <hex>
        assert first_args[:4] == ["nrfutil", "nrf5sdk-tools", "pkg", "generate"]
        assert "--debug-mode" in first_args
        assert "--application" in first_args
        assert str(hex_path) in first_args
        assert "--hw-version" in first_args
        assert "52" in first_args
        assert "--sd-req" in first_args

        # Call 2: nrfutil device program --firmware <zip> ...
        assert second_args[:3] == ["nrfutil", "device", "program"]
        firmware_idx = second_args.index("--firmware")
        zip_path = second_args[firmware_idx + 1]
        assert zip_path.endswith(".zip"), f"expected zip path, got {zip_path}"
        assert str(hex_path) not in second_args  # the hex itself must not be passed here
        assert "--serial-number" in second_args and "SN" in second_args
        assert "--traits" in second_args and "nordicUsb" in second_args

        mock_query.assert_called_once_with(serial_number="SN")

    def test_daemon_running_refuses(self, tmp_path):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        with patch(
            "ppk2.firmware._pid_path_exists_and_alive", return_value=True
        ):
            try:
                firmware.upgrade("SN", hex_path)
            except firmware.FirmwareUpgradeError as e:
                assert "daemon" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareUpgradeError")

    def test_hex_not_found_raises(self):
        with patch(
            "ppk2.firmware._pid_path_exists_and_alive", return_value=False
        ):
            try:
                firmware.upgrade("SN", Path("/nonexistent/fw.hex"))
            except firmware.FirmwareUpgradeError as e:
                assert "hex file" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareUpgradeError")

    def test_nrfutil_nonzero_raises(self, tmp_path):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        completed = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="", stderr="flash failed: something",
        )
        with patch(
            "ppk2.firmware._pid_path_exists_and_alive", return_value=False
        ), patch(
            "ppk2.firmware.subprocess.run", return_value=completed
        ):
            try:
                firmware.upgrade("SN", hex_path)
            except firmware.FirmwareUpgradeError as e:
                assert "3" in str(e) and "flash failed" in str(e)
            else:
                raise AssertionError("expected FirmwareUpgradeError")

    def test_abort_dfu_writes_slip_sequence(self):
        """abort_dfu opens the DFU port for the target serial and writes the
        3-byte SLIP ABORT sequence (0xC0 0x0C 0xC0), then closes.
        """
        written = bytearray()

        class _FakeSerial:
            def __init__(self, *args, **kwargs):
                self._opened_args = (args, kwargs)

            def write(self, data):
                written.extend(data)

            def flush(self):
                pass

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()
                return False

        fake_port = "/dev/tty.usbmodemXXXXXX1"
        with patch(
            "ppk2.firmware._resolve_dfu_port", return_value=fake_port
        ) as mock_resolve, patch(
            "ppk2.firmware.serial.Serial", _FakeSerial
        ):
            firmware.abort_dfu("SN")

        mock_resolve.assert_called_once_with("SN")
        assert bytes(written) == b"\xc0\x0c\xc0", (
            f"expected SLIP ABORT bytes, got {bytes(written)!r}"
        )

    def test_abort_dfu_raises_when_no_dfu_port(self):
        """If the serial has no DFU-mode port (device is in app mode or
        not connected), abort_dfu raises FirmwareUpgradeError.
        """
        with patch(
            "ppk2.firmware._resolve_dfu_port", return_value=None
        ):
            try:
                firmware.abort_dfu("SN")
            except firmware.FirmwareUpgradeError as e:
                assert "dfu" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareUpgradeError")

    def test_post_flash_verify_fails_raises(self, tmp_path):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch(
            "ppk2.firmware._pid_path_exists_and_alive", return_value=False
        ), patch(
            "ppk2.firmware.subprocess.run", return_value=completed
        ), patch(
            "ppk2.firmware.query",
            side_effect=firmware.FirmwareQueryError("device gone"),
        ):
            try:
                firmware.upgrade("SN", hex_path)
            except firmware.FirmwareUpgradeError as e:
                assert "verification failed" in str(e).lower()
            else:
                raise AssertionError("expected FirmwareUpgradeError")
