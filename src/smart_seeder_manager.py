#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seederr: A Smart Seeder Manager
Version: 8.0 (Production)

This script intelligently manages seeding torrents by moving them between a fast SSD cache
and a large storage array based on performance. It aims to maximize seeding efficiency
by keeping the most active torrents on the fastest storage.

Key Features:
- Performance-based scoring using a weighted long-term and short-term upload rate.
- Category-aware moves to preserve directory structures (e.g., .../movies/).
- Dry run mode for safe testing of logic.
- Support for manually specifying disk sizes for remote testing.
- Detailed logging for all actions.
"""

import os
import time
import requests
import psycopg2
import logging
import shutil
from psycopg2.extras import RealDictCursor

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# --- Environment Variable Loading ---
QBIT_CONFIG = {
    "host": os.environ.get('QBIT_HOST'),
    "port": os.environ.get('QBIT_PORT'),
    "user": os.environ.get('QBIT_USER'),
    "pass": os.environ.get('QBIT_PASS')
}
DB_CONFIG = {
    "host": os.environ.get('DB_HOST'),
    "port": os.environ.get('DB_PORT'),
    "name": os.environ.get('DB_NAME'),
    "user": os.environ.get('DB_USER'),
    "pass": os.environ.get('DB_PASS')
}
SSD_PATH = os.environ.get('SSD_PATH_IN_CONTAINER')
ARRAY_PATH = os.environ.get('ARRAY_PATH_IN_CONTAINER')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL_SECONDS', 3600))
EMA_ALPHA = float(os.environ.get('EMA_ALPHA', 0.012))
WEIGHT_LONG_TERM = float(os.environ.get('WEIGHT_LONG_TERM', 0.8))
WEIGHT_SHORT_TERM = float(os.environ.get('WEIGHT_SHORT_TERM', 0.2))
SSD_TARGET_CAPACITY_PERCENT = int(os.environ.get('SSD_TARGET_CAPACITY_PERCENT', 90))
MAX_MOVES_PER_CYCLE = int(os.environ.get('MAX_MOVES_PER_CYCLE', 1))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'
MANUAL_SSD_TOTAL_SPACE_GB = os.environ.get('MANUAL_SSD_TOTAL_SPACE_GB')


class QBittorrentClient:
    """Client to interact with the qBittorrent WebUI API."""
    def __init__(self, config):
        self.base_url = f"http://{config['host']}:{config['port']}"
        self.user = config['user']
        self.password = config['pass']
        self.session = requests.Session()
        self.session.headers.update({'Referer': self.base_url})
        self._login()

    def _login(self):
        login_url = f"{self.base_url}/api/v2/auth/login"
        login_data = {'username': self.user, 'password': self.password}
        try:
            r = self.session.post(login_url, data=login_data)
            r.raise_for_status()
            if r.text == "Ok.":
                logging.info("Successfully connected to qBittorrent API.")
            else:
                logging.error("Failed to connect to qBittorrent: Invalid credentials.")
                raise ConnectionError("qBittorrent login failed")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error connecting to qBittorrent API: {e}")
            raise

    def get_torrents(self):
        torrents_url = f"{self.base_url}/api/v2/torrents/info?filter=all&sort=name"
        try:
            r = self.session.get(torrents_url)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Could not retrieve torrent list: {e}")
            return []

    def pause_torrent(self, torrent_hash):
        url = f"{self.base_url}/api/v2/torrents/pause"
        self.session.post(url, data={'hashes': torrent_hash})

    def resume_torrent(self, torrent_hash):
        url = f"{self.base_url}/api/v2/torrents/resume"
        self.session.post(url, data={'hashes': torrent_hash})

    def set_location(self, torrent_hash, new_location):
        url = f"{self.base_url}/api/v2/torrents/setLocation"
        self.session.post(url, data={'hashes': torrent_hash, 'location': new_location})


def db_connect():
    """Establishes a connection to the PostgreSQL database."""
    while True:
        try:
            conn = psycopg2.connect(
                dbname=DB_CONFIG['name'],
                user=DB_CONFIG['user'],
                password=DB_CONFIG['pass'],
                host=DB_CONFIG['host'],
                port=DB_CONFIG['port']
            )
            logging.info("Successfully connected to PostgreSQL database.")
            return conn
        except psycopg2.OperationalError as e:
            logging.error(f"Failed to connect to PostgreSQL, retrying in 30 seconds... Error: {e}")
            time.sleep(30)


def setup_database(cursor):
    """Creates and updates the torrents table if necessary."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS torrents (
            hash VARCHAR(40) PRIMARY KEY,
            name TEXT,
            size BIGINT,
            save_path TEXT,
            content_path TEXT,
            location VARCHAR(10),
            added_on BIGINT,
            last_checked BIGINT,
            last_uploaded BIGINT,
            rate_gb_day REAL DEFAULT 0.0,
            smoothed_rate_gb_day REAL DEFAULT 0.0
        );
    """)
    # Add columns if they are missing for backward compatibility
    columns_to_check = {
        'size': 'BIGINT',
        'smoothed_rate_gb_day': 'REAL DEFAULT 0.0'
    }
    for col, col_type in columns_to_check.items():
        query = f"""
            DO $$
            BEGIN
                IF NOT EXISTS(SELECT * FROM information_schema.columns WHERE table_name='torrents' and column_name=%s)
                THEN ALTER TABLE torrents ADD COLUMN {col} {col_type};
                END IF;
            END $$;
        """
        cursor.execute(query, (col,))
    logging.info("Database schema verification complete.")


def get_ssd_space_info(cursor):
    """
    Returns the total and used space for the SSD.
    Uses the manual environment variable for total space if defined.
    Otherwise, detects space automatically.
    Used space is always calculated from the database for accuracy.
    """
    total_space = 0
    used_space = 0

    cursor.execute("SELECT SUM(size) FROM torrents WHERE location = 'ssd';")
    result = cursor.fetchone()
    used_space = result['sum'] if result and result['sum'] is not None else 0

    if MANUAL_SSD_TOTAL_SPACE_GB:
        logging.info(f"Using manual SSD total size: {MANUAL_SSD_TOTAL_SPACE_GB} GB")
        total_space = int(MANUAL_SSD_TOTAL_SPACE_GB) * (1024**3)
    else:
        try:
            total, _ = shutil.disk_usage(SSD_PATH)
            total_space = total
        except FileNotFoundError:
            logging.error(f"Path '{SSD_PATH}' not found. Cannot determine disk size automatically.")
            total_space = 0

    return total_space, used_space


def execute_move(qbit_client, cursor, torrent, destination_path, new_location_label):
    """Executes the physical move and updates qBit/DB, preserving the category subdirectory."""
    score = torrent.get('weighted_score', 'N/A')
    score_text = f"(Score: {score:.4f})" if isinstance(score, float) else ""
    
    # Determine current base path (ssd or array) to extract the relative path
    current_base_path = SSD_PATH if torrent['location'] == 'ssd' else ARRAY_PATH
    
    try:
        # Get the path of the torrent relative to its current base path
        # e.g., if content_path is /downloads/hot/movies/Tenet (2020)
        # and current_base_path is /downloads/hot, relative_path will be "movies/Tenet (2020)"
        relative_path = os.path.relpath(torrent['content_path'], current_base_path)
    except ValueError:
        # This can happen if the torrent's path doesn't start with the expected base path
        logging.error(f"Could not determine relative path for '{torrent['name']}'. Its path '{torrent['content_path']}' does not seem to be under '{current_base_path}'. Skipping move.")
        return

    # Construct the full destination path including the category subdirectory
    # e.g., /downloads/cold/movies/Tenet (2020)
    full_destination_path = os.path.join(destination_path, relative_path)
    
    if DRY_RUN:
        logging.info(f"[DRY RUN] ACTION: Would move '{torrent['name']}' {score_text} from '{torrent['content_path']}' to '{full_destination_path}'.")
        # In Dry Run, we still update the DB to simulate the move for the next cycle's calculations.
        cursor.execute("UPDATE torrents SET location = %s, content_path = %s, save_path = %s WHERE hash = %s", 
                       (new_location_label, full_destination_path, destination_path, torrent['hash']))
        return

    try:
        logging.info(f"Initiating move of '{torrent['name']}' {score_text} to {new_location_label} location.")
        logging.info(f"Source: {torrent['content_path']}")
        logging.info(f"Destination: {full_destination_path}")

        # 1. Pause Torrent
        qbit_client.pause_torrent(torrent['hash'])
        logging.info(f"Torrent {torrent['hash']} paused.")
        time.sleep(5) # Allow time for file handles to be released

        # 2. Ensure the destination category directory exists
        # e.g., ensure /downloads/cold/movies exists
        destination_parent_dir = os.path.dirname(full_destination_path)
        os.makedirs(destination_parent_dir, exist_ok=True)
        logging.info(f"Ensured destination directory '{destination_parent_dir}' exists.")

        # 3. Physical Move
        shutil.move(torrent['content_path'], full_destination_path)
        logging.info("Physical file move complete.")

        # 4. Update location in qBittorrent.
        # The location given to qBit is the new *base path*, not the full content path.
        qbit_client.set_location(torrent['hash'], destination_path)
        logging.info(f"New location '{destination_path}' set in qBittorrent.")

        # 5. Update our DB with the new location and paths
        cursor.execute("UPDATE torrents SET location = %s, content_path = %s, save_path = %s WHERE hash = %s", 
                       (new_location_label, full_destination_path, destination_path, torrent['hash']))
        
        # 6. Resume Torrent
        qbit_client.resume_torrent(torrent['hash'])
        logging.info(f"Torrent {torrent['hash']} resumed.")

    except Exception as e:
        logging.error(f"An error occurred while moving torrent {torrent['hash']}: {e}", exc_info=True)
        # In case of error, always try to resume the torrent to not leave it paused
        qbit_client.resume_torrent(torrent['hash'])


def main():
    """Main execution loop."""
    if DRY_RUN:
        logging.warning("="*50)
        logging.warning("=== SCRIPT IS RUNNING IN DRY RUN MODE ===")
        if MAX_MOVES_PER_CYCLE == -1:
             logging.warning("=== Move limit is disabled (-1), ALL potential moves will be listed ===")
        logging.warning("="*50)
    
    logging.info(f"Starting Seederr (v8.0 - Production)")
    
    qbit_client = QBittorrentClient(QBIT_CONFIG)
    db_conn = db_connect()
    with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
        setup_database(cursor)
    db_conn.commit()

    while True:
        try:
            logging.info("--- Starting new verification cycle ---")
            api_torrents = qbit_client.get_torrents()
            if not api_torrents:
                logging.info("No torrents found in qBittorrent. Waiting for next cycle.")
                time.sleep(CHECK_INTERVAL)
                continue
            
            logging.info(f"Retrieved {len(api_torrents)} torrents from qBittorrent for processing.")
            current_timestamp = int(time.time())
            
            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Step 1: Update performance stats for all torrents
                for t in api_torrents:
                    cursor.execute("SELECT * FROM torrents WHERE hash = %s", (t['hash'],))
                    db_entry = cursor.fetchone()
                    
                    instant_rate_gb_day = 0.0
                    
                    if not db_entry:
                        location = 'ssd' if t['save_path'].startswith(SSD_PATH) else 'array'
                        cursor.execute("""
                            INSERT INTO torrents (hash, name, size, save_path, content_path, location, added_on, last_checked, last_uploaded, rate_gb_day, smoothed_rate_gb_day)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (t['hash'], t['name'], t['size'], t['save_path'], t['content_path'], location, t['added_on'], current_timestamp, t['uploaded'], 0.0, 0.0))
                    else:
                        delta_time = current_timestamp - db_entry['last_checked']
                        if delta_time > 0:
                            delta_upload = t['uploaded'] - db_entry['last_uploaded']
                            instant_rate_gb_day = (delta_upload / delta_time) * 86400 / (1024**3)

                        old_smoothed_rate = db_entry.get('smoothed_rate_gb_day') or 0.0
                        new_smoothed_rate = (instant_rate_gb_day * EMA_ALPHA) + (old_smoothed_rate * (1 - EMA_ALPHA))
                        
                        location = 'ssd' if t['save_path'].startswith(SSD_PATH) else 'array'
                        cursor.execute("""
                            UPDATE torrents SET
                                last_checked = %s, last_uploaded = %s, rate_gb_day = %s, smoothed_rate_gb_day = %s,
                                location = %s, size = %s, save_path = %s, content_path = %s, name = %s
                            WHERE hash = %s
                        """, (current_timestamp, t['uploaded'], instant_rate_gb_day, new_smoothed_rate, location, t['size'], t['save_path'], t['content_path'], t['name'], t['hash']))
                
                db_conn.commit()
                logging.info("Performance stats updated for all torrents.")

                # Step 2: "Top-K" rebalancing logic
                ssd_total_space, ssd_used_space_from_db = get_ssd_space_info(cursor)
                if ssd_total_space == 0:
                    logging.error("Cannot determine SSD total size. Skipping rebalancing for this cycle.")
                    time.sleep(CHECK_INTERVAL)
                    continue

                target_ssd_usage = ssd_total_space * (SSD_TARGET_CAPACITY_PERCENT / 100.0)
                logging.info(f"SSD Capacity: {ssd_total_space / (1024**3):.2f} GB. Target Usage: {target_ssd_usage / (1024**3):.2f} GB. Current DB-calculated Usage: {ssd_used_space_from_db / (1024**3):.2f} GB")

                query = """
                    SELECT hash, name, size, location, content_path,
                           (smoothed_rate_gb_day::double precision * %s + rate_gb_day::double precision * %s) as weighted_score
                    FROM torrents
                    ORDER BY weighted_score DESC;
                """
                cursor.execute(query, (WEIGHT_LONG_TERM, WEIGHT_SHORT_TERM))
                all_ranked_torrents = cursor.fetchall()
                
                ideal_ssd_torrents_hashes, current_size = set(), 0
                for torrent in all_ranked_torrents:
                    if current_size + torrent['size'] <= target_ssd_usage:
                        ideal_ssd_torrents_hashes.add(torrent['hash'])
                        current_size += torrent['size']
                    else:
                        break
                
                current_ssd_torrents_hashes = {t['hash'] for t in all_ranked_torrents if t['location'] == 'ssd'}
                promotions_needed = ideal_ssd_torrents_hashes - current_ssd_torrents_hashes
                relegations_needed = current_ssd_torrents_hashes - ideal_ssd_torrents_hashes
                
                logging.info(f"Analysis complete: {len(promotions_needed)} promotion(s) and {len(relegations_needed)} relegation(s) required.")

                moves_done_this_cycle = 0
                
                # 1. Relegate to make space
                relegation_candidates = sorted([t for t in all_ranked_torrents if t['hash'] in relegations_needed], key=lambda x: x['weighted_score'])
                for torrent_to_relegate in relegation_candidates:
                    if MAX_MOVES_PER_CYCLE != -1 and moves_done_this_cycle >= MAX_MOVES_PER_CYCLE: break
                    execute_move(qbit_client, cursor, torrent_to_relegate, ARRAY_PATH, 'array')
                    moves_done_this_cycle += 1
                
                # 2. Promote if space is available
                promotion_candidates = [t for t in all_ranked_torrents if t['hash'] in promotions_needed]
                # Recalculate used space after relegations for accuracy
                _, current_ssd_used = get_ssd_space_info(cursor)
                for torrent_to_promote in promotion_candidates:
                    if MAX_MOVES_PER_CYCLE != -1 and moves_done_this_cycle >= MAX_MOVES_PER_CYCLE: break
                    if current_ssd_used + torrent_to_promote['size'] <= ssd_total_space:
                        execute_move(qbit_client, cursor, torrent_to_promote, SSD_PATH, 'ssd')
                        moves_done_this_cycle += 1
                        current_ssd_used += torrent_to_promote['size']
                    else:
                        logging.warning(f"Skipping promotion of '{torrent_to_promote['name']}': not enough free space on SSD.")
                
                db_conn.commit()

            logging.info(f"Cycle complete. Next check in {CHECK_INTERVAL / 3600:.1f} hour(s).")
            time.sleep(CHECK_INTERVAL)

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Database connection lost: {e}. Attempting to reconnect...")
            db_conn.close()
            db_conn = db_connect()
        except requests.exceptions.ConnectionError as e:
            logging.error(f"Connection to qBittorrent lost: {e}. Attempting to reconnect...")
            qbit_client = QBittorrentClient(QBIT_CONFIG)
        except Exception as e:
            logging.critical(f"An unhandled critical error occurred: {e}", exc_info=True)
            logging.info("Waiting 5 minutes before continuing to prevent rapid error loops.")
            time.sleep(300)


if __name__ == "__main__":
    # Check for presence of critical environment variables before starting
    required_vars = ['QBIT_HOST', 'QBIT_PORT', 'QBIT_USER', 'QBIT_PASS', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASS']
    if not MANUAL_SSD_TOTAL_SPACE_GB: # Path variables are only required if not in manual mode
        required_vars.extend(['SSD_PATH_IN_CONTAINER', 'ARRAY_PATH_IN_CONTAINER'])
    
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        logging.critical(f"Critical environment variables are missing: {', '.join(missing_vars)}. Exiting.")
    else:
        main()