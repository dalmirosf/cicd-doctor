"""Command line entry point: ``cicd-doctor api`` and ``cicd-doctor worker``."""

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cicd-doctor", description="CICD Doctor")
    commands = parser.add_subparsers(dest="command", required=True)

    api = commands.add_parser("api", help="run the HTTP API with uvicorn")
    api.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    api.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    api.add_argument("--reload", action="store_true", help="restart on code changes (dev only)")

    commands.add_parser(
        "worker",
        help="run the job worker (settings from the environment and .env: DATABASE_URL, "
        "WORKER_CONCURRENCY, JOB_LEASE_S)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "api":
        import uvicorn

        uvicorn.run("cicd_doctor.api.main:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "worker":
        from cicd_doctor.app.container import serve_worker

        serve_worker()
    return 0
