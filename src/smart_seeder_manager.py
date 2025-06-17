#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seederr: Smart Seeder Manager
Version: 9.0 (Copy-based Promotion/Relegation)

This script manages seeding torrents by copying popular torrents from a "master"
storage array to a fast SSD cache for optimal seeding. When a torrent is no longer
popular, the cached copy is deleted, and qBittorrent is repointed to the master file.
This workflow prioritizes immediate media availability via hardlinks from the array,
at the cost of temporary space duplication for popular torrents.
"""

import os
import time
import requests
import psycopg2
import logging
import shutil
from pathlib import Path
from psycopg2.extras import RealDictCursor

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# --- Environment Variable Loading ---
QBIT_CONFIG = { "host": os.environ.get('QBIT_HOST'), "port": os.environ.get('QBIT_PORT'), "user": os.environ.get('QBIT_USER'), "pass": os.environ.get('QBIT_PASS') }
DB_CONFIG = { "host": os.environ.get('DB_HOST'), "port": os.environ.get('DB_PORT'), "name": os.environ.get('DB_NAME'), "user": os.environ.get('DB_USER'), "pass": os.environ.get('DB_PASS') }
SSD_PATH = os.environ.get('SSD_PATH_IN_CONTAINER')
ARRAY_PATH = os.environ.get('ARRAY_PATH_IN_CONTAINER')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL_SECONDS', 3600))
EMA_ALPHA = float(os.environ.get('EMA_ALPHA', 0.012))
WEIGHT_LONG_TERM = float(os.environ.get('WEIGHT_LONG_TERM', 0.8))
WEIGHT_SHORT_TERM = float(os.environ.get('WEIGHT_SHORT_TERM', 0.2))
SSD_TARGET_CAPACITY_PERCENT = int(os.environ.get('SSD_TARGET_CAPACITY_PERCENT', 90))
MAX_MOVES_PER_CYCLE = int(os.environ.get('MAX_MOVES_PER_CYCLE', 1))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'

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
            r = self.session.post(login_url, data=login_data, timeout=10)
            r.raise_for_status()
            if r.text != "Ok.":
                raise ConnectionError("qBittorrent login failed: Invalid credentials.")
            logging.info("Successfully connected to qBittorrent API.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error connecting to qBittorrent API: {e}")
            raise

    def get_torrents(self):
        torrents_url = f"{self.base_url}/api/v2/torrents/info?filter=all&sort=name"
        try:
            r = self.session.get(torrents_url, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Could not retrieve torrent list: {e}")
            return []

    def set_location(self, torrent_hash, new_location):
        url = f"{self.base_url}/api/v2/torrents/setLocation"
        # The location for qBit is the directory containing the torrent data.
        new_save_path = str(Path(new_location).parent)
        self.session.post(url, data={'hashes': torrent_hash, 'location': new_save_path}, timeout=60)
        logging.info(f"Set new save_path for torrent {torrent_hash} to '{new_save_path}'.")

def db_connect():
    """Establishes a connection to the PostgreSQL database."""
    while True:
        try:
            conn = psycopg2.connect(dbname=DB_CONFIG['name'], user=DB_CONFIG['user'], password=DB_CONFIG['pass'], host=DB_CONFIG['host'], port=DB_CONFIG['port'])
            logging.info("Successfully connected to PostgreSQL database.")
            return conn
        except psycopg2.OperationalError as e:
            logging.error(f"Failed to connect to PostgreSQL, retrying in 30 seconds... Error: {e}")
            time.sleep(30)

def setup_database(cursor):
    """Creates and updates the torrents table, adding master_content_path if needed."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS torrents (
            hash VARCHAR(40) PRIMARY KEY, name TEXT, size BIGINT, save_path TEXT,
            content_path TEXT, master_content_path TEXT, location VARCHAR(10),
            added_on BIGINT, last_checked BIGINT, last_uploaded BIGINT,
            rate_gb_day REAL DEFAULT 0.0, smoothed_rate_gb_day REAL DEFAULT 0.0
        );
    """)
    cursor.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='torrents' and column_name='master_content_path') THEN
                ALTER TABLE torrents ADD COLUMN master_content_path TEXT;
            END IF;
        END $$;
    """)
    logging.info("Database schema verification complete.")

def promote_torrent(qbit_client, cursor, torrent):
    """Copies a torrent from the array to the SSD cache and repoints qBittorrent."""
    source_path = Path(torrent['master_content_path'])
    try:
        # Calculate the path of the torrent relative to the base array path
        relative_path = source_path.relative_to(ARRAY_PATH)
    except ValueError:
        logging.error(f"Cannot calculate relative path for '{source_path}'. It does not appear to be inside '{ARRAY_PATH}'. Skipping promotion.")
        return
        
    destination_path = Path(SSD_PATH) / relative_path

    if DRY_RUN:
        logging.info(f"[DRY RUN] PROMOTION: Would copy '{source_path}' to '{destination_path}' and repoint qBit.")
        return

    try:
        logging.info(f"PROMOTING '{torrent['name']}' by copying to SSD cache...")
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        # Use copytree for directories, copy2 for single files
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path, dirs_exist_ok=True)
        elif source_path.is_file():
            shutil.copy2(source_path, destination_path)
        else:
            logging.warning(f"Source path '{source_path}' is not a file or directory. Cannot promote.")
            return

        logging.info(f"Copy complete for '{torrent['name']}'. Repointing qBittorrent...")
        qbit_client.set_location(torrent['hash'], str(destination_path))
        
        cursor.execute("UPDATE torrents SET location = 'ssd', content_path = %s, save_path = %s WHERE hash = %s", 
                       (str(destination_path), str(destination_path.parent), torrent['hash']))
        logging.info(f"PROMOTION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to promote torrent {torrent['hash']}: {e}", exc_info=True)

def relegate_torrent(qbit_client, cursor, torrent):
    """Repoints qBittorrent to the master file on the array and deletes the cached SSD copy."""
    ssd_path_to_delete = Path(torrent['content_path'])
    master_path = Path(torrent['master_content_path'])
    
    if DRY_RUN:
        logging.info(f"[DRY RUN] RELEGATION: Would repoint qBit to '{master_path}' and delete '{ssd_path_to_delete}'.")
        return

    try:
        logging.info(f"RELEGATING '{torrent['name']}'. Repointing to master file on array.")
        # Repoint qBit first, this is the most important step.
        qbit_client.set_location(torrent['hash'], str(master_path))
        
        # Give qBit a moment to release file handles before deleting
        time.sleep(10) 
        
        logging.info(f"Deleting cached version from SSD: '{ssd_path_to_delete}'")
        # SAFETY CHECK: Ensure we are only deleting from the configured SSD path.
        if not str(ssd_path_to_delete).startswith(SSD_PATH):
             logging.error(f"SAFETY CHECK FAILED: Path '{ssd_path_to_delete}' is not on the SSD. Aborting delete.")
             return

        if ssd_path_to_delete.is_dir():
            shutil.rmtree(ssd_path_to_delete)
        elif ssd_path_to_delete.is_file():
            ssd_path_to_delete.unlink()
        
        cursor.execute("UPDATE torrents SET location = 'array', content_path = %s, save_path = %s WHERE hash = %s", 
                       (str(master_path), str(master_path.parent), torrent['hash']))
        logging.info(f"RELEGATION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to relegate torrent {torrent['hash']}: {e}", exc_info=True)


def main():
    """Main execution loop."""
    if DRY_RUN:
        logging.warning("="*50); logging.warning("=== SCRIPT IS RUNNING IN DRY RUN MODE ==="); logging.warning("="*50)
    
    logging.info("Starting Seederr (v9.0 - Copy-based Promotion)")
    
    qbit_client = QBittorrentClient(QBIT_CONFIG)
    db_conn = db_connect()

    with db_conn.cursor() as cursor:
        setup_database(cursor)
    db_conn.commit()

    while True:
        try:
            logging.info("--- Starting new verification cycle ---")
            
            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                api_torrents = qbit_client.get_torrents()
                logging.info(f"Retrieved {len(api_torrents)} torrents from qBittorrent for processing.")
                current_timestamp = int(time.time())

                # Update DB with latest info from qBittorrent
                for t in api_torrents:
                    cursor.execute("SELECT * FROM torrents WHERE hash = %s", (t['hash'],))
                    db_entry = cursor.fetchone()
                    
                    if not db_entry:
                        # First time seeing this torrent. Its current location IS the master location.
                        location = 'ssd' if t['content_path'].startswith(SSD_PATH) else 'array'
                        logging.info(f"New torrent '{t['name']}' found. Setting master path to '{t['content_path']}' and location to '{location}'.")
                        cursor.execute("""
                            INSERT INTO torrents (hash, name, size, save_path, content_path, master_content_path, location, added_on, last_checked, last_uploaded, rate_gb_day, smoothed_rate_gb_day)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (t['hash'], t['name'], t['size'], t['save_path'], t['content_path'], t['content_path'], location, t['added_on'], current_timestamp, t['uploaded'], 0.0, 0.0))
                    else:
                        delta_time = current_timestamp - db_entry['last_checked']
                        instant_rate_gb_day = db_entry['rate_gb_day']
                        if delta_time > 0:
                            delta_upload = t['uploaded'] - db_entry['last_uploaded']
                            instant_rate_gb_day = (delta_upload / delta_time) * 86400 / (1024**3)
                        
                        old_smoothed_rate = db_entry.get('smoothed_rate_gb_day') or 0.0
                        new_smoothed_rate = (instant_rate_gb_day * EMA_ALPHA) + (old_smoothed_rate * (1 - EMA_ALPHA))
                        
                        cursor.execute("UPDATE torrents SET last_checked = %s, last_uploaded = %s, rate_gb_day = %s, smoothed_rate_gb_day = %s, name = %s WHERE hash = %s",
                                       (current_timestamp, t['uploaded'], instant_rate_gb_day, new_smoothed_rate, t['name'], t['hash']))
                
                db_conn.commit()
                logging.info("Performance stats updated for all torrents.")

                # Rebalancing Logic
                try:
                    total_ssd_space, _, _ = shutil.disk_usage(SSD_PATH)
                    used_ssd_space = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file())
                except FileNotFoundError:
                    logging.error(f"SSD Path '{SSD_PATH}' not found. Skipping rebalancing cycle.")
                    time.sleep(CHECK_INTERVAL); continue

                target_ssd_usage = total_ssd_space * (SSD_TARGET_CAPACITY_PERCENT / 100.0)
                logging.info(f"SSD Status: {(used_ssd_space / (1024**3)):.2f} GB used / {(total_ssd_space / (1024**3)):.2f} GB total. Target usage: {(target_ssd_usage / (1024**3)):.2f} GB.")

                cursor.execute("SELECT *, (smoothed_rate_gb_day::double precision * %s + rate_gb_day::double precision * %s) as weighted_score FROM torrents ORDER BY weighted_score DESC", (WEIGHT_LONG_TERM, WEIGHT_SHORT_TERM))
                all_db_torrents = cursor.fetchall()
                
                ideal_ssd_hashes, temp_size = set(), 0
                for t in all_db_torrents:
                    if temp_size + t['size'] <= target_ssd_usage:
                        ideal_ssd_hashes.add(t['hash']); temp_size += t['size']
                    else: break
                
                current_ssd_hashes = {t['hash'] for t in all_db_torrents if t['location'] == 'ssd'}
                
                promotions_to_run = [t for t in all_db_torrents if t['hash'] in (ideal_ssd_hashes - current_ssd_hashes)]
                relegations_to_run = [t for t in all_db_torrents if t['hash'] in (current_ssd_hashes - ideal_ssd_hashes)]
                
                logging.info(f"Analysis complete: {len(promotions_to_run)} promotion(s) and {len(relegations_to_run)} relegation(s) identified.")

                moves_done = 0
                for torrent in sorted(relegations_to_run, key=lambda x: x['weighted_score']):
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    relegate_torrent(qbit_client, cursor, torrent); moves_done += 1
                
                current_used_space = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file())
                for torrent in sorted(promotions_to_run, key=lambda x: x['weighted_score'], reverse=True):
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    if current_used_space + torrent['size'] <= total_ssd_space:
                        promote_torrent(qbit_client, cursor, torrent); moves_done += 1
                        current_used_space += torrent['size']
                    else:
                        logging.warning(f"Skipping promotion of '{torrent['name']}': not enough free space on SSD.")
                
                db_conn.commit()

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Database connection lost: {e}. Attempting to reconnect..."); db_conn.close(); db_conn = db_connect()
        except requests.exceptions.RequestException as e:
            logging.error(f"Connection to qBittorrent lost: {e}. Attempting to reconnect..."); qbit_client = QBittorrentClient(QBIT_CONFIG)
        except Exception as e:
            logging.critical(f"An unhandled critical error occurred: {e}", exc_info=True); time.sleep(300)

        logging.info(f"Cycle complete. Next check in {CHECK_INTERVAL / 3600:.1f} hour(s).")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    required_vars = ['QBIT_HOST', 'QBIT_PORT', 'QBIT_USER', 'QBIT_PASS', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASS', 'SSD_PATH_IN_CONTAINER', 'ARRAY_PATH_IN_CONTAINER']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        logging.critical(f"Critical environment variables are missing: {', '.join(missing_vars)}. Exiting.")
    else:
        main()