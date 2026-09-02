"""Command-line diagnostics and local backend server."""

from __future__ import annotations

import argparse
import json
import os
import sys

from teco_quant.brokers.dhan import DhanConfigurationError, DhanCredentials
from teco_quant.config import RuntimeSettings, environment_with_dotenv
from teco_quant.persistence.sqlite import SCHEMA_VERSION, SQLiteRepository
from teco_quant.server import serve as serve_backend
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG, STRATEGY_VERSION


def doctor() -> int:
    try:
        environment = environment_with_dotenv(os.environ)
        settings = RuntimeSettings.from_environment(environment)
    except ValueError as exc:
        print(json.dumps({"status": "error", "runtime_configuration": str(exc)}, indent=2))
        return 1
    repository = SQLiteRepository(":memory:")
    try:
        repository.publish_strategy_config(DEFAULT_STRATEGY_CONFIG)
    finally:
        repository.close()
    try:
        DhanCredentials.from_environment(environment)
        credentials = "configured"
    except DhanConfigurationError:
        credentials = "not configured (offline mode is ready)"
    print(
        json.dumps(
            {
                "status": "ok",
                "strategy_version": STRATEGY_VERSION,
                "schema_version": SCHEMA_VERSION,
                "dhan_credentials": credentials,
                "dhan_market_data_requested": settings.dhan_live_enabled,
                "http_server": "available with: python -m teco_quant serve",
                "default_execution_mode": "DATA_ONLY",
                "paper_execution": "available only through explicit PAPER_TRADING mode",
                "live_broker_execution": "locked; no provider order implementation",
            },
            indent=2,
        )
    )
    return 0


def serve(*, host: str | None = None, port: int | None = None) -> int:
    try:
        environment = environment_with_dotenv(os.environ)
        if host is not None:
            environment["TECO_SERVER_HOST"] = host
        if port is not None:
            environment["TECO_SERVER_PORT"] = str(port)
        settings = RuntimeSettings.from_environment(environment)
        credentials: DhanCredentials | None = None
        if settings.dhan_live_enabled:
            try:
                credentials = DhanCredentials.from_environment(environment)
            except DhanConfigurationError:
                # Starting without secrets is supported so the frontend can display an
                # explicit CONFIG_REQUIRED state instead of losing the entire backend.
                credentials = None
        return serve_backend(settings, dhan_credentials=credentials)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "server": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m teco_quant")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="validate the offline runtime")
    server = commands.add_parser("serve", help="start the local JSON HTTP API")
    server.add_argument(
        "--host",
        help="listener host (overrides TECO_SERVER_HOST)",
    )
    server.add_argument(
        "--port",
        type=int,
        help="listener port (overrides TECO_SERVER_PORT)",
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments if arguments is not None else sys.argv[1:])
    if parsed.command == "doctor":
        return doctor()
    if parsed.command == "serve":
        return serve(host=parsed.host, port=parsed.port)
    raise AssertionError("argparse returned an unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
