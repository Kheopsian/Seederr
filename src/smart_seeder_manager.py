#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seederr: Smart Seeder Manager
Version: 10.1 (I/O Stress Metric)

This script manages seeding torrents by copying popular torrents from a "master"
storage array to a fast SSD cache. It uses a PostgreSQL database for state and
calculates cache performance based on an I/O stress score (upload delta * peers).
"""

import os
import time
import requests
import psycopg2
import logging
import shutil
from pathlib import Path
from psycopg2.extras import RealDictCursor
from datetime import timedelta


# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# --- Environment Variable Loading ---
QBIT_CONFIG = { "host": os.environ.get('QBIT_HOST'), "port": os.environ.get('QBIT_PORT'), "user": os.environ.get('QBIT_USER'), "pass": os.environ.get('QBIT_PASS') }
DB_CONFIG = { "host": os.environ.get('DB_HOST'), "port": os.environ.get('DB_PORT'), "name": os.environ.get('DB_NAME'), "user": os.environ.get('DB_USER'), "pass": os.environ.get('DB_PASS') }
SSD_PATH = os.environ.get('SSD_PATH_IN_CONTAINER')
ARRAY_PATH = os.environ.get('ARRAY_PATH_IN_CONTAINER')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL_SECONDS', 3600))
WEIGHT_LEECHERS = float(os.environ.get('WEIGHT_LEECHERS', 1000.0))
WEIGHT_SL_RATIO = float(os.environ.get('WEIGHT_SL_RATIO', 200.0))
SSD_CACHE_TAG = 'ssdCache'

SSD_TARGET_CAPACITY_PERCENT = int(os.environ.get('SSD_TARGET_CAPACITY_PERCENT', 90))
MAX_MOVES_PER_CYCLE = int(os.environ.get('MAX_MOVES_PER_CYCLE', 1))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'

class QBittorrentClient:
    """Client to interact with the qBittorrent WebUI API, with auto-relogin."""

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
            if r.text.strip() != "Ok.":
                raise ConnectionError("qBittorrent login failed: Invalid credentials or unexpected response.")
            logging.info("Successfully (re)connected to qBittorrent API.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error connecting to qBittorrent API during login: {e}")
            raise

    def _request_wrapper(self, method, url, **kwargs):
        try:
            r = self.session.request(method, url, **kwargs)
            if r.status_code == 403:
                logging.warning("Received 403 Forbidden. Session may have expired. Attempting to re-login.")
                self._login()
                r = self.session.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            logging.error(f"qBittorrent API request failed for {method} {url}: {e}")
            raise

    def get_torrents(self):
        torrents_url = f"{self.base_url}/api/v2/torrents/info?filter=all&sort=name"
        response = self._request_wrapper('get', torrents_url, timeout=30)
        return response.json()

    def set_location(self, torrent_hash, new_save_path):
        url = f"{self.base_url}/api/v2/torrents/setLocation"
        data = {'hashes': torrent_hash, 'location': str(new_save_path)}
        self._request_wrapper('post', url, data=data, timeout=60)
        logging.info(f"Set new save_path for torrent {torrent_hash} to '{new_save_path}'.")

    def add_tags(self, torrent_hash, tags):
        url = f"{self.base_url}/api/v2/torrents/addTags"
        data = {'hashes': torrent_hash, 'tags': tags}
        self._request_wrapper('post', url, data=data, timeout=15)
        logging.info(f"Added tags '{tags}' to torrent {torrent_hash}.")

    def remove_tags(self, torrent_hash, tags):
        url = f"{self.base_url}/api/v2/torrents/removeTags"
        data = {'hashes': torrent_hash, 'tags': tags}
        self._request_wrapper('post', url, data=data, timeout=15)
        logging.info(f"Removed tags '{tags}' from torrent {torrent_hash}.")

def connect_to_qbit(config, interval):
    while True:
        try:
            logging.info("Attempting to connect to the qBittorrent API...")
            client = QBittorrentClient(config)
            return client
        except (requests.exceptions.RequestException, ConnectionError) as e:
            logging.error(f"Failed to connect to qBittorrent: {e}. Retrying in {interval / 3600:.1f} hour(s).")
            time.sleep(interval)

def db_connect():
    while True:
        try:
            conn = psycopg2.connect(dbname=DB_CONFIG['name'], user=DB_CONFIG['user'], password=DB_CONFIG['pass'], host=DB_CONFIG['host'], port=DB_CONFIG['port'])
            logging.info("Successfully connected to PostgreSQL database.")
            return conn
        except psycopg2.OperationalError as e:
            logging.error(f"Failed to connect to PostgreSQL, retrying in 30 seconds... Error: {e}")
            time.sleep(30)

def setup_database(cursor):
    """Ensures the database schema is up to date."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS torrents (
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
            total_uploaded BIGINT DEFAULT 0
        );
    """)
    # Add the total_uploaded column to existing tables for seamless upgrades.
    cursor.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='torrents' AND column_name='total_uploaded') THEN
                ALTER TABLE torrents ADD COLUMN total_uploaded BIGINT DEFAULT 0;
            END IF;
        END $$;
    """)

def promote_torrent(qbit_client, cursor, torrent):
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
        logging.info(f"[DRY RUN] PROMOTION: Would copy to '{destination_content_path}', set save_path to '{destination_save_path}', and add tag.")
        return

    try:
        logging.info(f"PROMOTING '{torrent['name']}' by copying to SSD cache...")
        destination_save_path.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, destination_content_path, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, destination_content_path)

        logging.info(f"Copy complete. Repointing qBittorrent to new save_path: {destination_save_path}")
        qbit_client.set_location(torrent['hash'], str(destination_save_path))
        qbit_client.add_tags(torrent['hash'], SSD_CACHE_TAG)

        cursor.execute("UPDATE torrents SET location = 'ssd', content_path = %s, save_path = %s WHERE hash = %s", 
                        (str(destination_content_path), str(destination_save_path), torrent['hash']))
        logging.info(f"PROMOTION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to promote torrent {torrent['hash']}: {e}", exc_info=True)

def relegate_torrent(qbit_client, cursor, torrent):
    """Repoints qBit to the master save_path, removes cache tag, and deletes the SSD copy."""
    ssd_content_path = Path(torrent['content_path'])
    master_save_path = torrent['master_save_path']
    master_content_path = torrent['master_content_path']
    
    if DRY_RUN:
        logging.info(f"[DRY RUN] RELEGATION: Would set save_path to '{master_save_path}', remove tag, and delete '{ssd_content_path}'.")
        return

    try:
        logging.info(f"RELEGATING '{torrent['name']}'. Repointing to master save_path: {master_save_path}")
        qbit_client.set_location(torrent['hash'], master_save_path)
        qbit_client.remove_tags(torrent['hash'], SSD_CACHE_TAG)

        time.sleep(10) 
        
        logging.info(f"Deleting cached version from SSD: '{ssd_content_path}'")
        if not str(ssd_content_path).startswith(SSD_PATH):
             logging.error(f"SAFETY CHECK FAILED: Path '{ssd_content_path}' is not on the SSD. Aborting delete.")
             return
        if ssd_content_path.is_dir():
            shutil.rmtree(ssd_content_path)
        elif ssd_content_path.is_file():
            ssd_content_path.unlink()
        
        cursor.execute("UPDATE torrents SET location = 'array', content_path = %s, save_path = %s WHERE hash = %s", 
                        (master_content_path, master_save_path, torrent['hash']))
        logging.info(f"RELEGATION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to relegate torrent {torrent['hash']}: {e}", exc_info=True)

def print_performance_report(hit_score, miss_score, start_time):
    """Displays a performance and uptime report using the logging module."""
    uptime_seconds = time.time() - start_time
    uptime_str = str(timedelta(seconds=int(uptime_seconds)))

    # Use logging.info instead of print to ensure output goes to the log stream
    logging.info("="*80)
    logging.info("I/O STRESS & STATUS REPORT")
    logging.info(f"Total Uptime: {uptime_str}")
    logging.info("-"*80)
    
    logging.info(f"  ✅ Cache Hit Score: {int(hit_score):,}")
    logging.info(f"  ❌ Cache Miss Score: {int(miss_score):,}")
    
    total_score = hit_score + miss_score
    if total_score > 0:
        cache_hit_rate = (hit_score / total_score) * 100
        logging.info(f"  => Cache Hit Rate (by I/O Score): {cache_hit_rate:.2f}%")
    else:
        logging.info("  => Cache Hit Rate: N/A (no upload activity recorded yet)")
    logging.info("="*80)

def main():
    if DRY_RUN:
        logging.warning("="*50); logging.warning("=== SCRIPT IS RUNNING IN DRY RUN MODE ==="); logging.warning("="*50)
    
    logging.info("Starting Seederr (v10.1 - I/O Stress Metric)")
    
    qbit_client = connect_to_qbit(QBIT_CONFIG, CHECK_INTERVAL)
    db_conn = db_connect()

    with db_conn.cursor() as cursor:
        setup_database(cursor)
    db_conn.commit()
    
    total_cache_hit_score = 0.0
    total_cache_miss_score = 0.0
    start_time = time.time()

    while True:
        try:
            logging.info("--- Starting new verification cycle ---")
            
            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                api_torrents = qbit_client.get_torrents()
                logging.info(f"Retrieved {len(api_torrents)} torrents from qBittorrent for processing.")
                current_timestamp = int(time.time())

                all_db_torrents = []

                for t in api_torrents:
                    cursor.execute("SELECT * FROM torrents WHERE hash = %s", (t['hash'],))
                    db_entry = cursor.fetchone()
                    
                    current_leechers = t.get('num_incomplete', 0)
                    current_seeders = t.get('num_complete', 0)
                    current_uploaded_bytes = t.get('uploaded', 0)

                    if not db_entry:
                        location = 'ssd' if t['content_path'].startswith(SSD_PATH) else 'array'
                        cursor.execute("""
                            INSERT INTO torrents (
                                hash, name, size, save_path, content_path, master_content_path, master_save_path,
                                location, added_on, last_checked, current_leechers, current_seeders, total_uploaded
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            t['hash'], t['name'], t['size'], t['save_path'], t['content_path'], t['content_path'], t['save_path'],
                            location, t['added_on'], current_timestamp, current_leechers, current_seeders, current_uploaded_bytes
                        ))
                        db_conn.commit()
                        cursor.execute("SELECT * FROM torrents WHERE hash = %s", (t['hash'],))
                        db_entry = cursor.fetchone()

                    # Calculate I/O stress score based on upload delta and peer count.
                    previous_uploaded_bytes = db_entry.get('total_uploaded', 0)
                    upload_delta = current_uploaded_bytes - previous_uploaded_bytes

                    if upload_delta > 0:
                        io_stress_score = upload_delta * (current_leechers + 1)
                        if db_entry['location'] == 'ssd':
                            total_cache_hit_score += io_stress_score
                        else:
                            total_cache_miss_score += io_stress_score
                    
                    cursor.execute("""
                        UPDATE torrents 
                        SET last_checked = %s, current_leechers = %s, current_seeders = %s, name = %s, total_uploaded = %s
                        WHERE hash = %s
                    """, (current_timestamp, current_leechers, current_seeders, t['name'], current_uploaded_bytes, t['hash']))
                    
                    # Calculate the placement score used for rebalancing decisions.
                    if current_leechers > 0:
                        sl_ratio_bonus = (current_leechers / (current_seeders + 1)) * WEIGHT_SL_RATIO
                        leechers_score = current_leechers * WEIGHT_LEECHERS
                    else:
                        sl_ratio_bonus = 0
                        leechers_score = 0
                    
                    weighted_score = leechers_score + sl_ratio_bonus

                    db_entry.update({'weighted_score': weighted_score})
                    all_db_torrents.append(db_entry)

                db_conn.commit()
                logging.info("Stats updated and demand scores calculated for all torrents.")
                
                # --- Rebalancing Logic ---
                all_db_torrents.sort(key=lambda x: x['weighted_score'], reverse=True)
                
                try:
                    total, _, _ = shutil.disk_usage(SSD_PATH)
                    used = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file())
                except FileNotFoundError:
                    logging.error(f"SSD Path '{SSD_PATH}' not found. Skipping rebalancing cycle.")
                    time.sleep(CHECK_INTERVAL); continue

                target_ssd_usage = total * (SSD_TARGET_CAPACITY_PERCENT / 100.0)
                logging.info(f"SSD Status: {(used / (1024**3)):.2f} GB used / {(total / (1024**3)):.2f} GB total. Target usage: {(target_ssd_usage / (1024**3)):.2f} GB.")

                ideal_ssd_hashes, temp_size = set(), 0
                for t in all_db_torrents:
                    if t['weighted_score'] > 0 and temp_size + t['size'] <= target_ssd_usage:
                        ideal_ssd_hashes.add(t['hash'])
                        temp_size += t['size']
                    else:
                        if t['weighted_score'] == 0: continue
                        break
                
                current_ssd_hashes = {t['hash'] for t in all_db_torrents if t['location'] == 'ssd'}
                
                promotions_to_run = [t for t in all_db_torrents if t['hash'] in (ideal_ssd_hashes - current_ssd_hashes)]
                relegations_to_run = [t for t in all_db_torrents if t['hash'] in (current_ssd_hashes - ideal_ssd_hashes)]

                relegations_to_run.sort(key=lambda x: x['weighted_score'])
                logging.info(f"Analysis complete: {len(promotions_to_run)} promotion(s) and {len(relegations_to_run)} relegation(s) identified.")

                moves_done = 0
                for torrent in relegations_to_run:
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    relegate_torrent(qbit_client, cursor, torrent)
                    moves_done += 1
                
                current_used_space = sum(f.stat().st_size for f in Path(SSD_PATH).glob('**/*') if f.is_file())
                for torrent in promotions_to_run:
                    if moves_done >= MAX_MOVES_PER_CYCLE: break
                    if current_used_space + torrent['size'] <= total:
                        promote_torrent(qbit_client, cursor, torrent)
                        current_used_space += torrent['size']
                        moves_done += 1
                    else:
                        logging.warning(f"Skipping promotion of '{torrent['name']}': not enough free space on SSD.")
                
                db_conn.commit()

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Database connection lost: {e}. Attempting to reconnect..."); 
            db_conn.close(); 
            db_conn = db_connect()
        
        except requests.exceptions.RequestException as e:
            logging.error(f"Connection to qBittorrent lost during cycle: {e}. Attempting to create a new connection for the next cycle.")
            try:
                qbit_client = QBittorrentClient(QBIT_CONFIG)
                logging.info("New qBittorrent client created successfully.")
            except (requests.exceptions.RequestException, ConnectionError) as e_recon:
                logging.error(f"Failed to create new qBittorrent client immediately: {e_recon}. Will retry in the next cycle.")
        
        except Exception as e:
            logging.critical(f"An unhandled critical error occurred: {e}", exc_info=True); time.sleep(300)
        
        print_performance_report(total_cache_hit_score, total_cache_miss_score, start_time)
        
        logging.info(f"Cycle complete. Next check in {CHECK_INTERVAL / 3600:.1f} hour(s).")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    required_vars = ['QBIT_HOST', 'QBIT_PORT', 'QBIT_USER', 'QBIT_PASS', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASS', 'SSD_PATH_IN_CONTAINER', 'ARRAY_PATH_IN_CONTAINER']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        logging.critical(f"Critical environment variables are missing: {', '.join(missing_vars)}. Exiting.")
    else:
        main()