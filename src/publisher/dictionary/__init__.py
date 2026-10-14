"""The artist dictionary: every artist Beatport has carried in the pool's
families, crawled nightly into a local SQLite file, exported as fold-sorted
shards and uploaded to the multi-tenant API's dictionary ingest routes
(tunefinder-multi-tenant docs/superpowers/plans/2026-10-14-artist-dictionary.md).

Three phases, each its own module, one entry point:

  crawl.py   — walk /v4/catalog/tracks by genre and date slice into crawl.db
               (backfill until every slice is done, then a top-up of the current
               and previous month);
  export.py  — aggregate per artist, fold-sort, shard, write the shard files;
  upload.py  — post the shards then the commit through PoolApiClient.

  python -m src.publisher.dictionary nightly --until 05:15 --env dev

runs all three (from the checkout, so .env and data/ resolve as they do for
publish-pool). `crawl`, `status`, `export` and `upload` run one phase each.
"""
import os

# Where the standalone crawler has kept its database since the first night; the
# schema is unchanged, so nothing crawled before the move is lost.
DEFAULT_DB_PATH = os.path.expanduser("~/OpenClaw/Automations/artist-crawl/crawl.db")

LOG = "[dictionary]"


def default_export_dir(db_path: str) -> str:
    """The shard files live beside the database they were exported from."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "export")
