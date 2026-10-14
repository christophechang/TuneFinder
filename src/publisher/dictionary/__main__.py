"""`python -m src.publisher.dictionary <command>`, run from the TuneFinder checkout.

  nightly --env dev [--env prod] [--until 05:30]
                     crawl (backfill, then top-up), export, upload; one summary
                     line per phase; exit 1 if the crawl errored, the export
                     failed or any upload did not commit
  crawl [--until HH:MM] [--top-up]
  status
  export
  upload --env dev
"""
import argparse
import datetime as dt
import sys

from src.logger import get_logger, setup_logging
from src.publisher.dictionary import DEFAULT_DB_PATH, LOG, default_export_dir
from src.publisher.dictionary import crawl as crawl_mod
from src.publisher.dictionary import export as export_mod
from src.publisher.dictionary import upload as upload_mod

logger = get_logger(__name__)


def _crawl(args, db, top_up: bool) -> crawl_mod.CrawlOutcome:
    deadline = crawl_mod.deadline_for(args.until, dt.datetime.now())
    outcome = crawl_mod.run_crawl(
        db,
        lambda: crawl_mod.Client(args.interval, args.max_requests),
        deadline=deadline,
        top_up_when_done=top_up,
    )
    logger.info("%s %s", LOG, outcome.summary_line(db))
    return outcome


def _export(db, export_dir: str) -> export_mod.Export:
    export = export_mod.build_export(db)
    export_mod.write_export(export, export_dir)
    logger.info("%s %s", LOG, export.summary_line())
    return export


def _upload(envs: list[str], export_dir: str) -> bool:
    from src.config import load_settings

    settings = load_settings()
    ok = True
    for env in envs:
        outcome = upload_mod.upload_to_env(settings, env, export_dir)
        logger.info("%s %s", LOG, outcome.summary_line())
        ok = ok and outcome.ok
    return ok


def nightly(args, db, export_dir: str) -> int:
    crawled = _crawl(args, db, top_up=True)
    try:
        _export(db, export_dir)
    except Exception as exc:
        logger.error("%s export failed: %s: %s", LOG, type(exc).__name__, exc)
        return 1
    uploaded = _upload(args.env, export_dir)
    return 0 if uploaded and not crawled.error else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.publisher.dictionary")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="crawl database (default: %(default)s)")
    parser.add_argument("--export-dir", help="shard files (default: export/ beside the database)")
    sub = parser.add_subparsers(dest="command", required=True)

    def crawl_args(p):
        p.add_argument("--until", help="stop at this local time, HH:MM")
        p.add_argument("--interval", type=float, default=2.0, help="seconds between requests")
        p.add_argument("--max-requests", type=int, default=0)

    def env_arg(p):
        p.add_argument("--env", action="append", choices=["dev", "prod"], required=True,
                       help="target; repeat for both")

    nightly_p = sub.add_parser("nightly", help="crawl, export and upload")
    crawl_args(nightly_p)
    env_arg(nightly_p)
    crawl_p = sub.add_parser("crawl", help="crawl Beatport into the database")
    crawl_args(crawl_p)
    crawl_p.add_argument("--top-up", action="store_true",
                         help="when no backfill slice is left, re-walk the current and previous month")
    sub.add_parser("status", help="crawl progress")
    sub.add_parser("export", help="write the shard files")
    upload_p = sub.add_parser("upload", help="post the shard files, then the commit")
    env_arg(upload_p)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    db = crawl_mod.connect(args.db)
    export_dir = args.export_dir or default_export_dir(args.db)
    try:
        if args.command == "nightly":
            return nightly(args, db, export_dir)
        if args.command == "crawl":
            return 1 if _crawl(args, db, top_up=args.top_up).error else 0
        if args.command == "status":
            crawl_mod.status(db)
            return 0
        if args.command == "export":
            _export(db, export_dir)
            return 0
        if args.command == "upload":
            return 0 if _upload(args.env, export_dir) else 1
    finally:
        db.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
