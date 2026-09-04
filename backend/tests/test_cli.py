from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import teco_quant.__main__ as cli


class BackendCliTests(unittest.TestCase):
    def test_explicit_render_binding_overrides_validated_loopback_settings(self) -> None:
        environment = {
            "TECO_EXECUTION_MODE": "DATA_ONLY",
            "TECO_SERVER_HOST": "127.0.0.1",
            "TECO_SERVER_PORT": "8000",
        }
        with (
            patch.object(cli, "environment_with_dotenv", return_value=environment),
            patch.object(cli, "serve_backend", return_value=0) as serve_backend,
        ):
            result = cli.main(
                ["serve", "--host", "0.0.0.0", "--port", "10000"]
            )

        self.assertEqual(result, 0)
        settings = serve_backend.call_args.args[0]
        self.assertEqual(settings.server_host, "0.0.0.0")
        self.assertEqual(settings.server_port, 10000)
        self.assertEqual(settings.execution_mode, "DATA_ONLY")

    def test_environment_is_validated_before_cli_overrides_are_applied(self) -> None:
        environment = {"TECO_SERVER_HOST": "0.0.0.0"}
        with (
            patch.object(cli, "environment_with_dotenv", return_value=environment),
            patch.object(cli, "serve_backend") as serve_backend,
            redirect_stderr(io.StringIO()),
        ):
            result = cli.main(["serve", "--host", "127.0.0.1"])

        self.assertEqual(result, 1)
        serve_backend.assert_not_called()

    def test_cli_rejects_whitespace_host_and_out_of_range_port(self) -> None:
        for arguments in (
            ["serve", "--host", " 0.0.0.0 "],
            ["serve", "--port", "0"],
            ["serve", "--port", "65536"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(cli, "environment_with_dotenv", return_value={}),
                patch.object(cli, "serve_backend") as serve_backend,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(cli.main(arguments), 1)
                serve_backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
