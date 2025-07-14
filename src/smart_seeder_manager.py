#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seederr: Smart Seeder Manager
Version: 12.0 (Library-Powered)

This script uses the official qbittorrent-api library for all client interactions,
ensuring robust and accurate data collection.
- A fast loop gathers precise I/O data.
- A slow loop analyzes data and performs torrent moves.
Database migrations are handled externally by Alembic.
"""

import os
import time
import psycopg2
import logging
import shutil
import threading
from pathlib import Path
from psycopg2.extras import RealDictCursor
from datetime import timedelta
import qbittorrentapi

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# --- Environment Variable Loading ---
DB_CONFIG = { "host": os.environ.get('DB_HOST'), "port": os.environ.get('DB_PORT'), "name": os.environ.get('DB_NAME'), "user": os.environ.get('DB_USER'), "pass": os.environ.get('DB_PASS') }
SSD_PATH = os.environ.get('SSD_PATH_IN_CONTAINER')
ARRAY_PATH = os.environ.get('ARRAY_PATH_IN_CONTAINER')

# Loop intervals
DATA_COLLECTION_INTERVAL = 15  # seconds
DECISION_MAKING_INTERVAL = int(os.environ.get('CHECK_INTERVAL_SECONDS', 3600))

# Logic Parameters
SSD_TARGET_CAPACITY_PERCENT = int(os.environ.get('SSD_TARGET_CAPACITY_PERCENT', 90))
MAX_MOVES_PER_CYCLE = int(os.environ.get('MAX_MOVES_PER_CYCLE', 1))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'
SSD_CACHE_TAG = 'ssdCache'
PEER_STATS_CLEANUP_HOURS = 24

def get_qbit_client():
    """Establishes a connection to qBittorrent and returns a client object."""
    try:
        client = qbittorrentapi.Client(
            host=os.environ.get('QBIT_HOST'),
            port=os.environ.get('QBIT_PORT'),
            username=os.environ.get('QBIT_USER'),
            password=os.environ.get('QBIT_PASS')
            # The invalid REQUESTS_TIMEOUT argument has been removed.
        )
        client.auth_log_in()
        logging.info(f"Successfully connected to qBittorrent v{client.app.version} at {client.host}.")
        return client
    except qbittorrentapi.LoginFailed as e:
        logging.error(f"qBittorrent login failed: {e}")
    except Exception as e:
        logging.error(f"Failed to connect to qBittorrent: {e}")
    return None

def db_connect():
    """Establishes a persistent connection to the database."""
    while True:
        try:
            conn = psycopg2.connect(dbname=DB_CONFIG['name'], user=DB_CONFIG['user'], password=DB_CONFIG['pass'], host=DB_CONFIG['host'], port=DB_CONFIG['port'])
            logging.info("Successfully connected to PostgreSQL database.")
            return conn
        except psycopg2.OperationalError as e:
            logging.error(f"Failed to connect to PostgreSQL, retrying in 30 seconds... Error: {e}")
            time.sleep(30)

def promote_torrent(qbit_client, db_conn, torrent):
    """Copies a torrent to the SSD, repoints qBit, and adds the cache tag."""
    source_path = Path(torrent['master_content_path'])
    try:
        relative_path = source_path.relative_to(Path(torrent['master_content_path']).parent)
    except ValueError:
        logging.error(f"Cannot calculate relative path for '{source_path}'. Skipping promotion.")
        return

    destination_content_path = Path(SSD_PATH) / relative_path
    destination_save_path = destination_content_path.parent

    if DRY_RUN:
        logging.info(f"[DRY RUN] PROMOTION: Would move '{torrent['name']}' to '{destination_content_path}'.")
        return

    try:
        logging.info(f"PROMOTING '{torrent['name']}' by copying to SSD cache...")
        destination_save_path.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, destination_content_path, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, destination_content_path)

        logging.info(f"Copy complete. Repointing qBittorrent...")
        qbit_client.torrents_set_location(torrent_hashes=torrent['hash'], location=str(destination_save_path))
        qbit_client.torrents_add_tags(tags=SSD_CACHE_TAG, torrent_hashes=torrent['hash'])

        with db_conn.cursor() as cursor:
            cursor.execute("UPDATE torrents SET location = 'ssd', content_path = %s, save_path = %s WHERE hash = %s",
                            (str(destination_content_path), str(destination_save_path), torrent['hash']))
        db_conn.commit()
        logging.info(f"PROMOTION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to promote torrent {torrent['hash']}: {e}", exc_info=True)

def relegate_torrent(qbit_client, db_conn, torrent):
    """Repoints qBit to the master save_path, removes cache tag, and deletes the SSD copy."""
    ssd_content_path = Path(torrent['content_path'])
    master_save_path = torrent['master_save_path']
    master_content_path = torrent['master_content_path']

    if DRY_RUN:
        logging.info(f"[DRY RUN] RELEGATION: Would re-point '{torrent['name']}' to '{master_save_path}' and delete from cache.")
        return

    try:
        logging.info(f"RELEGATING '{torrent['name']}'. Repointing to master save_path...")
        qbit_client.torrents_set_location(torrent_hashes=torrent['hash'], location=master_save_path)
        qbit_client.torrents_remove_tags(tags=SSD_CACHE_TAG, torrent_hashes=torrent['hash'])

        time.sleep(10)

        logging.info(f"Deleting cached version from SSD: '{ssd_content_path}'")
        if not str(ssd_content_path).startswith(SSD_PATH):
             logging.error(f"SAFETY CHECK FAILED: Path '{ssd_content_path}' is not on the SSD. Aborting delete.")
             return
        if ssd_content_path.is_dir():
            shutil.rmtree(ssd_content_path)
        elif ssd_content_path.is_file():
            ssd_content_path.unlink()

        with db_conn.cursor() as cursor:
            cursor.execute("UPDATE torrents SET location = 'array', content_path = %s, save_path = %s WHERE hash = %s",
                            (master_content_path, master_save_path, torrent['hash']))
        db_conn.commit()
        logging.info(f"RELEGATION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to relegate torrent {torrent['hash']}: {e}", exc_info=True)


def data_collector_loop():
    """Fast loop (every 15s). Connects and collects per-peer upload data."""
    qbit_client = get_qbit_client()
    db_conn = db_connect()
    logging.info("Data Collector thread started and connected.")

    while True:
        try:
            time.sleep(DATA_COLLECTION_INTERVAL)
            if not qbit_client: qbit_client = get_qbit_client()
            if not qbit_client: continue

            all_torrents = qbit_client.torrents_info()
            active_torrents = [t for t in all_torrents if t.upspeed > 0]

            if not active_torrents:
                logging.info("Data Collector: Cycle check. No torrents with active upload speed detected.")
                continue

            logging.info(f"Data Collector: Found {len(active_torrents)} torrent(s) with active upload. Processing...")

            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                for torrent in active_torrents:
                    cursor.execute("SELECT location, total_uploaded FROM torrents WHERE hash = %s", (torrent.hash,))
                    db_torrent = cursor.fetchone()
                    if not db_torrent: continue

                    # --- FIX ---
                    # Peer data must be fetched with a separate API call per torrent.
                    peers_data = qbit_client.sync.torrent_peers(torrent_hash=torrent.hash)
                    
                    if not peers_data or 'peers' not in peers_data:
                        continue
                    
                    # The actual list of peers is inside the 'peers' key
                    active_peers_count = sum(1 for peer in peers_data['peers'].values() if peer['up_speed'] > 0)

                    if active_peers_count > 0:
                        upload_delta = torrent.uploaded - db_torrent['total_uploaded']
                        if upload_delta > 0:
                            io_stress_score = upload_delta * active_peers_count
                            score_column = 'io_hit_score' if db_torrent['location'] == 'ssd' else 'io_miss_score'

                            cursor.execute(
                                f"UPDATE torrents SET {score_column} = {score_column} + %s, total_uploaded = %s WHERE hash = %s",
                                (io_stress_score, torrent.uploaded, torrent.hash)
                            )
                            logging.info(f"Data Collector: Logged {io_stress_score:,} to {score_column} for torrent {torrent.hash[:8]}...")
                db_conn.commit()

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Data Collector: Database connection lost: {e}. Attempting to reconnect...");
            if db_conn: db_conn.close()
            db_conn = db_connect()
        except qbittorrentapi.APIError as e:
            logging.error(f"Data Collector: qBittorrent API Error: {e}. Reconnecting...");
            qbit_client = None
        except Exception as e:
            logging.error(f"Critical error in Data Collector thread: {e}", exc_info=True)


def decision_maker_loop():
    """Slow loop. Connects and analyzes data to perform torrent moves."""
    qbit_client = get_qbit_client()
    db_conn = db_connect()
    logging.info("Decision Maker thread started and connected.")
    start_time = time.time()

    while True:
        try:
            if not qbit_client: qbit_client = get_qbit_client()
            if not qbit_client: time.sleep(DECISION_MAKING_INTERVAL); continue

            logging.info("--- Decision Maker: Starting new verification cycle ---")

            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                api_torrents = qbit_client.torrents_info()
                current_timestamp = int(time.time())

                api_hashes = {t.hash for t in api_torrents}
                if api_hashes:
                    cursor.execute("DELETE FROM torrents WHERE hash NOT IN %s", (tuple(api_hashes),))

                for t in api_torrents:
                    cursor.execute("SELECT hash FROM torrents WHERE hash = %s", (t.hash,))
                    if not cursor.fetchone():
                        location = 'ssd' if t.content_path.startswith(SSD_PATH) else 'array'
                        cursor.execute("""
                            INSERT INTO torrents (hash, name, size, save_path, content_path, master_content_path, master_save_path, location, added_on, last_checked, total_uploaded)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            t.hash, t.name, t.size, t.save_path, t.content_path, t.content_path, t.save_path,
                            location, t.added_on, current_timestamp, t.uploaded
                        ))
                    else:
                        cursor.execute("UPDATE torrents SET last_checked = %s, name = %s WHERE hash = %s",
                                       (current_timestamp, t.name, t.hash))
                db_conn.commit()
                logging.info("Decision Maker: Torrent list synchronized with database.")

                cursor.execute("SELECT * FROM torrents")
                all_db_torrents = cursor.fetchall()
                all_db_torrents.sort(key=lambda x: (x['io_miss_score'], x['io_hit_score']), reverse=True)

                try:
                    total_ssd_space, _, _ = shutil.disk_usage(SSD_PATH)
                    used_ssd_space = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file())
                except FileNotFoundError:
                    logging.error(f"SSD Path '{SSD_PATH}' not found. Skipping rebalancing cycle.")
                    time.sleep(DECISION_MAKING_INTERVAL); continue

                target_ssd_usage = total_ssd_space * (SSD_TARGET_CAPACITY_PERCENT / 100.0)
                logging.info(f"SSD Status: {(used_ssd_space / (1024**3)):.2f} GB used / {(total_ssd_space / (1024**3)):.2f} GB total. Target usage: {(target_ssd_usage / (1024**3)):.2f} GB.")

                ideal_ssd_hashes, temp_size = set(), 0
                for t in all_db_torrents:
                    if (t['io_miss_score'] > 0 or t['io_hit_score'] > 0) and temp_size + t['size'] <= target_ssd_usage:
                        ideal_ssd_hashes.add(t['hash'])
                        temp_size += t['size']

                current_ssd_hashes = {t['hash'] for t in all_db_torrents if t['location'] == 'ssd'}
                promotions_to_run = [t for t in all_db_torrents if t['hash'] in (ideal_ssd_hashes - current_ssd_hashes)]
                relegations_to_run = [t for t in all_db_torrents if t['hash'] in (current_ssd_hashes - ideal_ssd_hashes)]
                relegations_to_run.sort(key=lambda x: (x.get('io_miss_score', 0), x.get('io_hit_score', 0)))

                logging.info(f"Analysis complete: {len(promotions_to_run)} promotion(s) and {len(relegations_to_run)} relegation(s) identified.")

                moves_done = 0
                for torrent in relegations_to_run:
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    relegate_torrent(qbit_client, db_conn, torrent)
                    moves_done += 1

                current_used_space = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file())
                for torrent in promotions_to_run:
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    if current_used_space + torrent['size'] <= total_ssd_space:
                        promote_torrent(qbit_client, db_conn, torrent)
                        current_used_space += torrent['size']
                        moves_done += 1
                    else:
                        logging.warning(f"Skipping promotion of '{torrent['name']}': not enough free space on SSD.")

                cursor.execute("SELECT SUM(io_hit_score) as total_hits, SUM(io_miss_score) as total_misses FROM torrents")
                report_data = cursor.fetchone()
                total_hit_score = report_data['total_hits'] or 0
                total_miss_score = report_data['total_misses'] or 0
                uptime_seconds = time.time() - start_time
                uptime_str = str(timedelta(seconds=int(uptime_seconds)))

                logging.info("="*80)
                logging.info(f"I/O STRESS & STATUS REPORT (Uptime: {uptime_str})")
                logging.info("-"*80)
                logging.info(f"  ✅ Cumulative Cache Hit Score: {int(total_hit_score):,}")
                logging.info(f"  ❌ Cumulative Cache Miss Score: {int(total_miss_score):,}")
                total_score = total_hit_score + total_miss_score
                if total_score > 0:
                    logging.info(f"  => Cache Efficiency (Cycle): {(total_hit_score / total_score) * 100:.2f}%")
                else:
                    logging.info("  => Cache Efficiency (Cycle): N/A (no I/O score recorded)")
                logging.info("="*80)

                logging.info("Resetting I/O scores for the next decision cycle.")
                cursor.execute("UPDATE torrents SET io_hit_score = 0, io_miss_score = 0")
                db_conn.commit()

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Decision Maker: Database connection lost: {e}. Attempting to reconnect...");
            if db_conn: db_conn.close()
            db_conn = db_connect()
        except qbittorrentapi.APIError as e:
            logging.error(f"Decision Maker: qBittorrent API Error: {e}. Reconnecting...");
            qbit_client = None
        except Exception as e:
            logging.critical(f"An unhandled critical error occurred in Decision Maker: {e}", exc_info=True)

        logging.info(f"Decision cycle complete. Next check in {DECISION_MAKING_INTERVAL / 3600:.1f} hour(s).")
        time.sleep(DECISION_MAKING_INTERVAL)


if __name__ == "__main__":
    if DRY_RUN:
        logging.warning("="*50); logging.warning("=== SCRIPT IS RUNNING IN DRY RUN MODE ==="); logging.warning("="*50)

    logging.info("Starting Seederr (v12.0 - Library-Powered)")

    collector_thread = threading.Thread(target=data_collector_loop, daemon=True)
    decision_thread = threading.Thread(target=decision_maker_loop, daemon=True)

    collector_thread.start()
    decision_thread.start()

    while True:
        time.sleep(1)