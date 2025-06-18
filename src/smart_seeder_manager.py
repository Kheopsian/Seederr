#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seederr: Smart Seeder Manager
Version: 9.1 (Copy-based Promotion/Relegation with enhanced popularity and cache tracking)

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
WEIGHT_LEECHERS = float(os.environ.get('WEIGHT_LEECHERS', 500.0))
WEIGHT_COMPLETED_PER_HOUR = float(os.environ.get('WEIGHT_COMPLETED_PER_HOUR', 1000.0))
WEIGHT_SMOOTHED_UPLOAD = float(os.environ.get('WEIGHT_SMOOTHED_UPLOAD', 0.1))

SSD_TARGET_CAPACITY_PERCENT = int(os.environ.get('SSD_TARGET_CAPACITY_PERCENT', 90))
MAX_MOVES_PER_CYCLE = int(os.environ.get('MAX_MOVES_PER_CYCLE', 1))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'

class QBittorrentClient:
    """Client to interact with the qBittorrent WebUI API, with auto-relogin."""

    def __init__(self, config):
        """
        Initializes the client and performs the first login.
        """
        self.base_url = f"http://{config['host']}:{config['port']}"
        self.user = config['user']
        self.password = config['pass']
        self.session = requests.Session()
        self.session.headers.update({'Referer': self.base_url})
        self._login()

    def _login(self):
        """
        Performs authentication against the qBittorrent API.
        This will be called on init and whenever a session expires.
        """
        login_url = f"{self.base_url}/api/v2/auth/login"
        login_data = {'username': self.user, 'password': self.password}
        try:
            # We start with a fresh session state for login, but keep the session object
            # to preserve any other settings (like proxies, if any were set).
            # The 'post' will update the session's cookies automatically.
            r = self.session.post(login_url, data=login_data, timeout=10)
            r.raise_for_status()
            if r.text.strip() != "Ok.":
                raise ConnectionError("qBittorrent login failed: Invalid credentials or unexpected response.")
            logging.info("Successfully (re)connected to qBittorrent API.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error connecting to qBittorrent API during login: {e}")
            raise # Propagate the error if login itself fails

    def _request_wrapper(self, method, url, **kwargs):
        """
        A wrapper for all API requests that handles session expiration.
        It tries the request, and if it fails with a 403 (Forbidden),
        it attempts to log in again and retries the request once.
        """
        try:
            r = self.session.request(method, url, **kwargs)
            # A 403 status is a common sign of an expired session cookie
            if r.status_code == 403:
                logging.warning("Received 403 Forbidden. Session may have expired. Attempting to re-login.")
                self._login()
                # Retry the original request after successful re-login
                r = self.session.request(method, url, **kwargs)
            
            r.raise_for_status() # Raise an exception for any other error (4xx, 5xx)
            return r
        except requests.exceptions.RequestException as e:
            logging.error(f"qBittorrent API request failed for {method} {url}: {e}")
            # Instead of returning a default value, we raise the exception
            # to let the main loop handle the connection loss if it persists.
            raise

    def get_torrents(self):
        """
        Retrieves the list of all torrents from qBittorrent.
        """
        # Ensure we request all necessary fields from qBittorrent API
        # 'uploaded', 'completed', 'num_leechs', 'num_seeds' are crucial
        torrents_url = f"{self.base_url}/api/v2/torrents/info?filter=all&sort=name"
        response = self._request_wrapper('get', torrents_url, timeout=30)
        return response.json()

    def set_location(self, torrent_hash, new_location):
        """
        Sets a new location for a specific torrent.
        """
        url = f"{self.base_url}/api/v2/torrents/setLocation"
        # The location for qBit is the directory containing the torrent data.
        new_save_path = str(Path(new_location).parent)
        data = {'hashes': torrent_hash, 'location': new_save_path}
        
        # This call is now wrapped, handling potential session issues.
        self._request_wrapper('post', url, data=data, timeout=60)
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
    """
    Creates and updates the torrents table.
    - Adds 'master_content_path' if needed (existing logic).
    - Adds 'last_completed', 'completed_per_hour', 'smoothed_completed_per_hour',
      'current_leechers', 'current_seeders' for enhanced popularity tracking.
    - Adds 'cycles_in_cache' to track how many cycles a torrent has been in the SSD cache.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS torrents (
            hash VARCHAR(40) PRIMARY KEY,
            name TEXT,
            size BIGINT,
            save_path TEXT,
            content_path TEXT,
            master_content_path TEXT, -- The original path on the array
            location VARCHAR(10),     -- 'ssd' or 'array'
            added_on BIGINT,          -- Timestamp when added to qBittorrent
            last_checked BIGINT,      -- Last timestamp this script checked
            last_uploaded BIGINT,     -- Total uploaded bytes from last check
            rate_gb_day REAL DEFAULT 0.0,            -- Instantaneous upload rate GB/day
            smoothed_rate_gb_day REAL DEFAULT 0.0,   -- EMA of upload rate GB/day

            -- New columns for enhanced popularity
            last_completed BIGINT DEFAULT 0,         -- Total completed count from last check
            completed_per_hour REAL DEFAULT 0.0,     -- Instantaneous completed rate per hour
            smoothed_completed_per_hour REAL DEFAULT 0.0, -- EMA of completed rate per hour
            current_leechers INT DEFAULT 0,          -- Current number of leechers (from qBittorrent API)
            current_seeders INT DEFAULT 0,           -- Current number of seeders (from qBittorrent API)

            -- New column for tracking cache residency
            cycles_in_cache INT DEFAULT 0            -- Number of consecutive cycles in SSD cache
        );
    """)
    
    columns_to_add = {
        'master_content_path': 'TEXT',
        'last_completed': 'BIGINT DEFAULT 0',
        'completed_per_hour': 'REAL DEFAULT 0.0',
        'smoothed_completed_per_hour': 'REAL DEFAULT 0.0',
        'current_leechers': 'INT DEFAULT 0',
        'current_seeders': 'INT DEFAULT 0',
        'cycles_in_cache': 'INT DEFAULT 0'
    }

    for column, definition in columns_to_add.items():
        cursor.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='torrents' AND column_name='{column}') THEN
                    ALTER TABLE torrents ADD COLUMN {column} {definition};
                END IF;
            END $$;
        """)
    logging.info("Database schema verification complete. All necessary columns are ensured.")

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
    
    logging.info("Starting Seederr (v9.1 - Copy-based Promotion with enhanced popularity)")
    
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
                    
                    # Get new values from qBittorrent API
                    current_uploaded = t['uploaded']
                    current_completed = t['completed']
                    current_leechers = t['num_leechs']
                    current_seeders = t['num_seeds']

                    if not db_entry:
                        # First time seeing this torrent. Its current location IS the master location.
                        location = 'ssd' if t['content_path'].startswith(SSD_PATH) else 'array'
                        logging.info(f"New torrent '{t['name']}' found. Setting master path to '{t['content_path']}' and location to '{location}'.")
                        cursor.execute("""
                            INSERT INTO torrents (
                                hash, name, size, save_path, content_path, master_content_path, location, added_on, 
                                last_checked, last_uploaded, rate_gb_day, smoothed_rate_gb_day, 
                                last_completed, completed_per_hour, smoothed_completed_per_hour, 
                                current_leechers, current_seeders, cycles_in_cache
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            t['hash'], t['name'], t['size'], t['save_path'], t['content_path'], t['content_path'], 
                            location, t['added_on'], current_timestamp, current_uploaded, 0.0, 0.0, 
                            current_completed, 0.0, 0.0, current_leechers, current_seeders, 0 if location == 'array' else 1
                        ))
                    else:
                        delta_time = current_timestamp - db_entry['last_checked']
                        
                        # Calculate instant upload rate (existing)
                        instant_rate_gb_day = db_entry['rate_gb_day']
                        if delta_time > 0:
                            delta_upload = current_uploaded - db_entry['last_uploaded']
                            instant_rate_gb_day = (delta_upload / delta_time) * 86400 / (1024**3)
                        
                        old_smoothed_upload_rate = db_entry.get('smoothed_rate_gb_day') or 0.0
                        new_smoothed_upload_rate = (instant_rate_gb_day * EMA_ALPHA) + (old_smoothed_upload_rate * (1 - EMA_ALPHA))

                        # Calculate instant completed rate
                        instant_completed_per_hour = db_entry['completed_per_hour']
                        if delta_time > 0:
                            delta_completed = current_completed - db_entry['last_completed']
                            # Convert to completed per hour
                            instant_completed_per_hour = delta_completed / (delta_time / 3600.0) 
                        
                        old_smoothed_completed_per_hour = db_entry.get('smoothed_completed_per_hour') or 0.0
                        new_smoothed_completed_per_hour = (instant_completed_per_hour * EMA_ALPHA) + (old_smoothed_completed_per_hour * (1 - EMA_ALPHA))
                        
                        # Update DB
                        cursor.execute("""
                            UPDATE torrents SET 
                                last_checked = %s, last_uploaded = %s, rate_gb_day = %s, smoothed_rate_gb_day = %s, 
                                last_completed = %s, completed_per_hour = %s, smoothed_completed_per_hour = %s, 
                                current_leechers = %s, current_seeders = %s, name = %s 
                            WHERE hash = %s
                        """, (
                            current_timestamp, current_uploaded, instant_rate_gb_day, new_smoothed_upload_rate, 
                            current_completed, instant_completed_per_hour, new_smoothed_completed_per_hour,
                            current_leechers, current_seeders, t['name'], t['hash']
                        ))
                
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

                cursor.execute("""
                    SELECT *, 
                           (current_leechers::double precision * %s + 
                            smoothed_completed_per_hour::double precision * %s +
                            smoothed_rate_gb_day::double precision * %s
                           ) as weighted_score 
                    FROM torrents 
                    ORDER BY weighted_score DESC
                """, (WEIGHT_LEECHERS, WEIGHT_COMPLETED_PER_HOUR, WEIGHT_SMOOTHED_UPLOAD))

                all_db_torrents = cursor.fetchall()
                
                ideal_ssd_hashes, temp_size = set(), 0
                for t in all_db_torrents:
                    if temp_size + t['size'] <= target_ssd_usage:
                        ideal_ssd_hashes.add(t['hash'])
                        temp_size += t['size']
                    else:
                        break
                
                current_ssd_hashes = {t['hash'] for t in all_db_torrents if t['location'] == 'ssd'}
                
                promotions_to_run = [t for t in all_db_torrents if t['hash'] in (ideal_ssd_hashes - current_ssd_hashes)]
                relegations_to_run = [t for t in all_db_torrents if t['hash'] in (current_ssd_hashes - ideal_ssd_hashes)]

                logging.info(f"Analysis complete: {len(promotions_to_run)} promotion(s) and {len(relegations_to_run)} relegation(s) identified.")

                # 1. Increment for torrents that are currently on SSD and remain there (ideal or not yet relegated)
                for t in all_db_torrents:
                    if t['location'] == 'ssd' and t['hash'] in ideal_ssd_hashes:
                        # Torrent is on SSD and is ideally on SSD -> increment cycles_in_cache
                        cursor.execute("UPDATE torrents SET cycles_in_cache = cycles_in_cache + 1 WHERE hash = %s", (t['hash'],))
                    elif t['location'] == 'ssd' and t['hash'] not in ideal_ssd_hashes and t['hash'] not in {r['hash'] for r in relegations_to_run}:
                        # Torrent is on SSD, is NOT ideally on SSD, but also NOT marked for relegation in this cycle.
                        # It's still effectively "in cache" for this cycle.
                        cursor.execute("UPDATE torrents SET cycles_in_cache = cycles_in_cache + 1 WHERE hash = %s", (t['hash'],))
                    elif t['location'] == 'array' and t['hash'] not in ideal_ssd_hashes:
                        # Torrent is on array and is ideally on array -> ensure cycles_in_cache is 0
                        # This handles cases where it might have been relegated manually or missed by a previous cycle
                        if t['cycles_in_cache'] != 0:
                            cursor.execute("UPDATE torrents SET cycles_in_cache = 0 WHERE hash = %s", (t['hash'],))
                    # Torrents being promoted will have their cycles_in_cache set to 1 in promote_torrent
                    # Torrents being relegated will have their cycles_in_cache set to 0 in relegate_torrent
                db_conn.commit() # Commit the cycles_in_cache increments before actual moves


                moves_done = 0
                for torrent in sorted(relegations_to_run, key=lambda x: x['weighted_score']):
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    relegate_torrent(qbit_client, cursor, torrent)
                    # Set cycles_in_cache to 0 when a torrent is relegated
                    cursor.execute("UPDATE torrents SET cycles_in_cache = 0 WHERE hash = %s", (torrent['hash'],))
                    moves_done += 1
                
                current_used_space = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file()) # Recalculate after relegations
                for torrent in sorted(promotions_to_run, key=lambda x: x['weighted_score'], reverse=True):
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    if current_used_space + torrent['size'] <= total_ssd_space:
                        promote_torrent(qbit_client, cursor, torrent)
                        # Set cycles_in_cache to 1 when a torrent is promoted for the first time or re-promoted
                        cursor.execute("UPDATE torrents SET cycles_in_cache = 1 WHERE hash = %s", (torrent['hash'],))
                        current_used_space += torrent['size']
                        moves_done += 1
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
    required_vars.extend(['WEIGHT_LEECHERS', 'WEIGHT_COMPLETED_PER_HOUR', 'WEIGHT_SMOOTHED_UPLOAD']) 
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        logging.critical(f"Critical environment variables are missing: {', '.join(missing_vars)}. Exiting.")
    else:
        main()                                                                                                                                  