"""Create initial schema for torrents and peer_stats

Revision ID: 0001
Revises: 
Create Date: 2025-07-15 12:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE torrents (
            hash VARCHAR(40) PRIMARY KEY,
            name TEXT,
            size BIGINT,
            save_path TEXT,
            content_path TEXT,
            master_content_path TEXT,
            master_save_path TEXT,
            location VARCHAR(10),
            added_on BIGINT,
            last_checked BIGINT,
            current_leechers INT DEFAULT 0,
            current_seeders INT DEFAULT 0,
            total_uploaded BIGINT DEFAULT 0,
            io_hit_score BIGINT DEFAULT 0,
            io_miss_score BIGINT DEFAULT 0
        );
    """)

    op.execute("""
        CREATE TABLE peer_stats (
            peer_id TEXT PRIMARY KEY,
            torrent_hash VARCHAR(40) NOT NULL,
            total_uploaded BIGINT DEFAULT 0,
            last_seen BIGINT NOT NULL
        );
    """)

    op.execute("""
        CREATE INDEX idx_peer_stats_torrent_hash ON peer_stats (torrent_hash);
    """)
    op.execute("""
        CREATE INDEX idx_peer_stats_last_seen ON peer_stats (last_seen);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE peer_stats;")
    op.execute("DROP TABLE torrents;")