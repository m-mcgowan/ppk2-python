"""Tests for `ppk2 firmware` CLI subcommands."""

import json
import sys
from unittest.mock import patch

import pytest

from ppk2 import cli, firmware


def _info(app: int = 20300, serial: str = "E2794420999B") -> firmware.FirmwareInfo:
    return firmware.FirmwareInfo(
        serial_number=serial,
        bootloader_type="NRFDL_BOOTLOADER_TYPE_SDFU",
        bootloader_version=3,
        application_version=app,
        raw={"bootloaderType": "NRFDL_BOOTLOADER_TYPE_SDFU"},
    )


def _upstream() -> firmware.UpstreamFirmware:
    return firmware.UpstreamFirmware(
        semver="1.2.4",
        commit="db16a94",
        filename="pca63100_ppk2_1.2.4_db16a94.hex",
        url="https://raw.githubusercontent.com/.../pca63100_ppk2_1.2.4_db16a94.hex",
    )


class TestFirmwareInfoCli:
    def test_prints_human_output(self, capsys):
        with patch("ppk2.cli.firmware.query", return_value=_info()), \
             patch.object(sys, "argv", ["ppk2", "firmware"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "E2794420999B" in out
        assert "20300" in out
        assert "3" in out

    def test_prints_json_output(self, capsys):
        with patch("ppk2.cli.firmware.query", return_value=_info()), \
             patch.object(sys, "argv", ["ppk2", "firmware", "--json"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 0
        doc = json.loads(out)
        assert doc["serial_number"] == "E2794420999B"
        assert doc["application_version"] == 20300
        assert doc["bootloader_version"] == 3

    def test_tool_missing_exits_2(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            side_effect=firmware.FirmwareToolMissing("install nrfutil"),
        ), patch.object(sys, "argv", ["ppk2", "firmware"]):
            rc = cli.main()
        err = capsys.readouterr().err
        assert rc == 2
        assert "nrfutil" in err.lower()

    def test_query_error_exits_2(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            side_effect=firmware.FirmwareQueryError("no device"),
        ), patch.object(sys, "argv", ["ppk2", "firmware"]):
            rc = cli.main()
        err = capsys.readouterr().err
        assert rc == 2
        assert "no device" in err.lower()


class TestFirmwareCheckCli:
    def test_up_to_date_exits_0(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ), patch(
            "ppk2.cli.firmware.fetch_upstream", return_value=_upstream()
        ), patch.object(sys, "argv", ["ppk2", "firmware", "check"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "up to date" in out.lower()

    def test_mismatch_exits_1(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION - 100),
        ), patch(
            "ppk2.cli.firmware.fetch_upstream", return_value=_upstream()
        ), patch.object(sys, "argv", ["ppk2", "firmware", "check"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "outdated" in out.lower() or "mismatch" in out.lower()

    def test_upstream_unavailable_exits_1_but_prints_device(self, capsys):
        with patch(
            "ppk2.cli.firmware.query", return_value=_info()
        ), patch(
            "ppk2.cli.firmware.fetch_upstream",
            side_effect=firmware.UpstreamFetchError("rate limited"),
        ), patch.object(sys, "argv", ["ppk2", "firmware", "check"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "upstream" in out.lower() and "unavailable" in out.lower()
        assert "E2794420999B" in out

    def test_json_output(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ), patch(
            "ppk2.cli.firmware.fetch_upstream", return_value=_upstream()
        ), patch.object(sys, "argv", ["ppk2", "firmware", "check", "--json"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 0
        doc = json.loads(out)
        assert doc["up_to_date"] is True
        assert doc["device"]["application_version"] == firmware.CURRENT_APPLICATION_VERSION
        assert doc["upstream"]["semver"] == "1.2.4"
        assert doc["upstream_error"] is None

    def test_serial_before_action_is_forwarded_to_query(self):
        """Regression: `ppk2 firmware --serial SN check` must pass SN to query.

        Initially the subparser's own `--serial` default (None) stomped the
        parent's value when the flag appeared before the action verb.
        """
        with patch(
            "ppk2.cli.firmware.query",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ) as mock_query, patch(
            "ppk2.cli.firmware.fetch_upstream", return_value=_upstream()
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "--serial", "E2794420999B", "check"],
        ):
            rc = cli.main()
        assert rc == 0
        mock_query.assert_called_once_with(serial_number="E2794420999B")


class TestFirmwareUpgradeCli:
    def test_upgrade_with_yes_and_hex_path(self, tmp_path, capsys):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        with patch(
            "ppk2.cli.firmware.upgrade",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ) as mock_up, patch(
            "ppk2.cli.firmware.query", return_value=_info(app=20100)
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "upgrade",
             "--serial", "SN",
             "--hex", str(hex_path),
             "--yes"],
        ):
            rc = cli.main()
        assert rc == 0
        mock_up.assert_called_once()
        out = capsys.readouterr().out
        assert "Flashing" in out or "complete" in out.lower()

    def test_upgrade_default_downloads_upstream(self, tmp_path, capsys):
        up = _upstream()
        fake_hex = tmp_path / up.filename
        fake_hex.write_bytes(b":00000001FF\n")
        with patch(
            "ppk2.cli.firmware.download_upstream",
            return_value=(fake_hex, up),
        ) as mock_dl, patch(
            "ppk2.cli.firmware.query", return_value=_info(app=20100)
        ), patch(
            "ppk2.cli.firmware.upgrade",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "upgrade", "--serial", "SN", "--yes"],
        ):
            rc = cli.main()
        assert rc == 0
        mock_dl.assert_called_once()

    def test_mutually_exclusive_hex_and_download(self, tmp_path, capsys):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        with patch.object(
            sys, "argv",
            ["ppk2", "firmware", "upgrade",
             "--serial", "SN",
             "--hex", str(hex_path),
             "--download",
             "--yes"],
        ):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_non_tty_without_yes_refuses(self, tmp_path, capsys):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        with patch(
            "ppk2.cli.firmware.query", return_value=_info(app=20100)
        ), patch(
            "ppk2.cli.sys.stdin.isatty", return_value=False
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "upgrade",
             "--serial", "SN",
             "--hex", str(hex_path)],
        ):
            rc = cli.main()
        err = capsys.readouterr().err
        assert rc == 1
        assert "aborted" in err.lower() or "confirm" in err.lower()

    def test_user_declines_confirm(self, tmp_path, capsys, monkeypatch):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        with patch(
            "ppk2.cli.firmware.query", return_value=_info(app=20100)
        ), patch(
            "ppk2.cli.sys.stdin.isatty", return_value=True
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "upgrade",
             "--serial", "SN",
             "--hex", str(hex_path)],
        ):
            rc = cli.main()
        err = capsys.readouterr().err
        assert rc == 1
        assert "aborted" in err.lower()

    def test_daemon_running_error(self, tmp_path, capsys):
        hex_path = tmp_path / "fw.hex"
        hex_path.write_bytes(b":00000001FF\n")
        with patch(
            "ppk2.cli.firmware.query", return_value=_info(app=20100)
        ), patch(
            "ppk2.cli.firmware.upgrade",
            side_effect=firmware.FirmwareUpgradeError(
                "a ppk2 daemon is running for serial SN; stop it"
            ),
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "upgrade",
             "--serial", "SN",
             "--hex", str(hex_path),
             "--yes"],
        ):
            rc = cli.main()
        err = capsys.readouterr().err
        assert rc == 2
        assert "daemon" in err.lower()


class TestFirmwareAbortDfuCli:
    def test_success(self, capsys):
        with patch(
            "ppk2.cli.firmware.abort_dfu", return_value=None
        ) as mock_abort, patch.object(
            sys, "argv",
            ["ppk2", "firmware", "abort-dfu", "--serial", "SN"],
        ):
            rc = cli.main()
        assert rc == 0
        mock_abort.assert_called_once_with("SN")
        out = capsys.readouterr().out
        assert "SN" in out
        assert "app" in out.lower() or "abort" in out.lower()

    def test_not_in_dfu(self, capsys):
        with patch(
            "ppk2.cli.firmware.abort_dfu",
            side_effect=firmware.FirmwareUpgradeError(
                "no DFU-mode PPK2 found for serial SN — already in app mode"
            ),
        ), patch.object(
            sys, "argv",
            ["ppk2", "firmware", "abort-dfu", "--serial", "SN"],
        ):
            rc = cli.main()
        err = capsys.readouterr().err
        assert rc == 2
        assert "dfu" in err.lower() or "app mode" in err.lower()


class TestFirmwareCheckDfuDetection:
    """check should warn when the device is in DFU mode so the user sees a
    remediation hint (run `ppk2 firmware abort-dfu`).
    """

    def test_check_warns_on_dfu_mode(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ), patch(
            "ppk2.cli.firmware.fetch_upstream", return_value=_upstream()
        ), patch(
            "ppk2.cli.firmware.is_in_dfu_mode", return_value=True
        ), patch.object(sys, "argv", ["ppk2", "firmware", "check",
                                      "--serial", "E2794420999B"]):
            rc = cli.main()
        out = capsys.readouterr().out
        # Still exits 0 (firmware version matches), but surfaces the DFU hint.
        assert rc == 0
        assert "dfu" in out.lower()
        assert "abort-dfu" in out.lower()

    def test_check_no_dfu_warning_when_app_mode(self, capsys):
        with patch(
            "ppk2.cli.firmware.query",
            return_value=_info(app=firmware.CURRENT_APPLICATION_VERSION),
        ), patch(
            "ppk2.cli.firmware.fetch_upstream", return_value=_upstream()
        ), patch(
            "ppk2.cli.firmware.is_in_dfu_mode", return_value=False
        ), patch.object(sys, "argv", ["ppk2", "firmware", "check",
                                      "--serial", "E2794420999B"]):
            rc = cli.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "dfu" not in out.lower()
