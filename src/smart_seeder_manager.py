#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seederr: Smart Seeder Manager
Version: 11.0 (Dual-Loop Architecture)

This script uses a dual-loop architecture.
- A fast loop (data collector) runs every 15s to gather precise I/O data per peer.
- A slow loop (decision maker) runs every 30m to analyze the collected data and perform torrent moves.
Database migrations are handled externally by Alembic.
"""

import os
import time
import requests
import psycopg2
import logging
import shutil
import threading
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

# Loop intervals
DATA_COLLECTION_INTERVAL = 15  # seconds
DECISION_MAKING_INTERVAL = int(os.environ.get('CHECK_INTERVAL_SECONDS', 3600))

# Logic Parameters
SSD_TARGET_CAPACITY_PERCENT = int(os.environ.get('SSD_TARGET_CAPACITY_PERCENT', 90))
MAX_MOVES_PER_CYCLE = int(os.environ.get('MAX_MOVES_PER_CYCLE', 1))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'
SSD_CACHE_TAG = 'ssdCache'
PEER_STATS_CLEANUP_HOURS = 24 # Remove peer stats if not seen for this many hours

class QBittorrentClient:
    """
    Client to interact with the qBittorrent WebUI API, with auto-relogin.
    This version uses the /sync/maindata endpoint for real-time data.
    """
    def __init__(self, config):
        self.base_url = f"http://{config['host']}:{config['port']}"
        self.user = config['user']
        self.password = config['pass']
        self.session = requests.Session()
        self.session.headers.update({'Referer': self.base_url})
        self.rid = 0  # Response ID for sync requests
        self._login()

    def _login(self):
        login_url = f"{self.base_url}/api/v2/auth/login"
        login_data = {'username': self.user, 'password': self.password}
        try:
            r = self.session.post(login_url, data=login_data, timeout=10)
            r.raise_for_status()
            if r.text.strip() != "Ok.":
                raise ConnectionError("qBittorrent login failed: Invalid credentials or unexpected response.")
            self.rid = 0 # Reset response ID on re-login
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
            return None

    def get_torrents(self):
        """
        Gets torrent data using the /sync/maindata endpoint for real-time accuracy.
        """
        url = f"{self.base_url}/api/v2/sync/maindata?rid={self.rid}"
        response = self._request_wrapper('get', url, timeout=30)
        
        if not response:
            return []

        data = response.json()
        self.rid = data.get('rid', self.rid) # Update the response ID for the next call

        # The 'torrents' key contains a dictionary of torrents, keyed by their hash.
        # We return a list of the torrent objects, similar to the old method.
        torrents_dict = data.get('torrents', {})
        return list(torrents_dict.values())

    def get_torrent_peers(self, torrent_hash):
        url = f"{self.base_url}/api/v2/sync/torrentPeers?hash={torrent_hash}"
        response = self._request_wrapper('get', url, timeout=20)
        return response.json().get('peers', {}) if response else {}

    # set_location, add_tags, and remove_tags methods remain unchanged
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

        logging.info(f"Copy complete. Repointing qBittorrent to new save_path: {destination_save_path}")
        qbit_client.set_location(torrent['hash'], str(destination_save_path))
        qbit_client.add_tags(torrent['hash'], SSD_CACHE_TAG)

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
        
        with db_conn.cursor() as cursor:
            cursor.execute("UPDATE torrents SET location = 'array', content_path = %s, save_path = %s WHERE hash = %s", 
                            (master_content_path, master_save_path, torrent['hash']))
        db_conn.commit()
        logging.info(f"RELEGATION successful for '{torrent['name']}'.")
    except Exception as e:
        logging.error(f"Failed to relegate torrent {torrent['hash']}: {e}", exc_info=True)


def data_collector_loop(qbit_client, db_conn):
    """
    Fast loop (every 15s). Its only job is to collect per-peer upload data for active torrents
    and update the I/O scores in the database.
    """
    logging.info("Data Collector thread started.")
    while True:
        try:
            time.sleep(DATA_COLLECTION_INTERVAL)
            all_torrents = qbit_client.get_torrents()
            active_torrents = [t for t in all_torrents if t.get('up_speed', 0) > 0]
            
            if not active_torrents:
                continue

            logging.info(f"Data Collector: Found {len(active_torrents)} torrent(s) with active upload.")
            
            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                for torrent in active_torrents:
                    thash = torrent['hash']
                    
                    # Get torrent location from our DB
                    cursor.execute("SELECT location FROM torrents WHERE hash = %s", (thash,))
                    db_torrent = cursor.fetchone()
                    if not db_torrent:
                        continue # Decision-maker will handle new torrents
                    
                    torrent_location = db_torrent['location']
                    
                    # Get per-peer data from qBit
                    peers_data = qbit_client.get_torrent_peers(thash)
                    if not peers_data:
                        continue
                        
                    active_peers_count = 0
                    for peer_ip, peer in peers_data.items():
                        if peer.get('up_speed', 0) > 0:
                            active_peers_count += 1

                    if active_peers_count > 0:
                        # Fetch previous total upload for this torrent
                        cursor.execute("SELECT total_uploaded FROM torrents WHERE hash = %s", (thash,))
                        prev_total_uploaded = cursor.fetchone()['total_uploaded']
                        
                        current_total_uploaded = torrent['uploaded']
                        upload_delta = current_total_uploaded - prev_total_uploaded
                        
                        if upload_delta > 0:
                            # Calculate score: total data transferred * number of concurrent reads
                            io_stress_score = upload_delta * active_peers_count
                            
                            score_column = 'io_hit_score' if torrent_location == 'ssd' else 'io_miss_score'
                            
                            # Atomically increment the score
                            cursor.execute(
                                f"UPDATE torrents SET {score_column} = {score_column} + %s, total_uploaded = %s WHERE hash = %s",
                                (io_stress_score, current_total_uploaded, thash)
                            )
                            logging.info(f"Logged {io_stress_score:,} to {score_column} for torrent {thash[:8]}...")

                db_conn.commit()

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Data Collector: Database connection lost: {e}. Attempting to reconnect..."); 
            db_conn.close(); 
            db_conn = db_connect()
        except Exception as e:
            logging.error(f"Critical error in Data Collector thread: {e}", exc_info=True)


def decision_maker_loop(qbit_client, db_conn):
    """
    Slow loop (every 30-60min). It analyzes the data collected by the fast loop,
    updates torrent states, and performs promotions/relegations.
    """
    logging.info("Decision Maker thread started.")
    start_time = time.time()
    
    while True:
        try:
            logging.info("--- Decision Maker: Starting new verification cycle ---")
            
            with db_conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Sync torrent list from qBit with our DB
                api_torrents = qbit_client.get_torrents()
                current_timestamp = int(time.time())

                # Prune deleted torrents
                api_hashes = {t['hash'] for t in api_torrents}
                cursor.execute("DELETE FROM torrents WHERE hash NOT IN %s", (tuple(api_hashes) if api_hashes else ('',),))
                
                for t in api_torrents:
                    cursor.execute("SELECT * FROM torrents WHERE hash = %s", (t['hash'],))
                    db_entry = cursor.fetchone()
                    
                    if not db_entry:
                        location = 'ssd' if t['content_path'].startswith(SSD_PATH) else 'array'
                        cursor.execute("""
                            INSERT INTO torrents (hash, name, size, save_path, content_path, master_content_path, master_save_path, location, added_on, last_checked, total_uploaded) 
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            t['hash'], t['name'], t['size'], t['save_path'], t['content_path'], t['content_path'], t['save_path'],
                            location, t['added_on'], current_timestamp, t.get('uploaded', 0)
                        ))
                    else:
                        cursor.execute("UPDATE torrents SET last_checked = %s, name = %s WHERE hash = %s", 
                                       (current_timestamp, t['name'], t['hash']))
                
                db_conn.commit()
                logging.info("Decision Maker: Torrent list synchronized with database.")

                # --- Rebalancing Logic ---
                cursor.execute("SELECT *, (io_hit_score + io_miss_score) as total_io_score FROM torrents ORDER BY total_io_score DESC")
                all_db_torrents = cursor.fetchall()

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
                    if t['total_io_score'] > 0 and temp_size + t['size'] <= target_ssd_usage:
                        ideal_ssd_hashes.add(t['hash'])
                        temp_size += t['size']
                
                current_ssd_hashes = {t['hash'] for t in all_db_torrents if t['location'] == 'ssd'}
                
                promotions_to_run = [t for t in all_db_torrents if t['hash'] in (ideal_ssd_hashes - current_ssd_hashes)]
                relegations_to_run = [t for t in all_db_torrents if t['hash'] in (current_ssd_hashes - ideal_ssd_hashes)]
                
                # Relegate lowest score torrents first
                relegations_to_run.sort(key=lambda x: x['total_io_score'])

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
                
                # --- Reporting and Cleanup ---
                cursor.execute("SELECT SUM(io_hit_score) as total_hits, SUM(io_miss_score) as total_misses FROM torrents")
                report_data = cursor.fetchone()
                total_hit_score = report_data['total_hits'] or 0
                total_miss_score = report_data['total_misses'] or 0
                
                uptime_seconds = time.time() - start_time
                uptime_str = str(timedelta(seconds=int(uptime_seconds)))

                logging.info("="*80)
                logging.info("I/O STRESS & STATUS REPORT")
                logging.info(f"Total Uptime: {uptime_str}")
                logging.info("-"*80)
                logging.info(f"  ✅ Cumulative Cache Hit Score: {int(total_hit_score):,}")
                logging.info(f"  ❌ Cumulative Cache Miss Score: {int(total_miss_score):,}")
                
                total_score = total_hit_score + total_miss_score
                if total_score > 0:
                    cache_hit_rate = (total_hit_score / total_score) * 100
                    logging.info(f"  => Cache Efficiency (Cycle): {cache_hit_rate:.2f}%")
                else:
                    logging.info("  => Cache Efficiency (Cycle): N/A (no I/O score recorded)")
                logging.info("="*80)
                
                # Reset scores for the next cycle
                logging.info("Resetting I/O scores for the next decision cycle.")
                cursor.execute("UPDATE torrents SET io_hit_score = 0, io_miss_score = 0")
                
                # Cleanup old peer stats
                cleanup_threshold = current_timestamp - (PEER_STATS_CLEANUP_HOURS * 3600)
                cursor.execute("DELETE FROM peer_stats WHERE last_seen < %s", (cleanup_threshold,))
                logging.info(f"Cleaned up {cursor.rowcount} stale peer entries.")

                db_conn.commit()

        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            logging.error(f"Decision Maker: Database connection lost: {e}. Attempting to reconnect..."); 
            db_conn.close(); 
            db_conn = db_connect()
        except Exception as e:
            logging.critical(f"An unhandled critical error occurred in Decision Maker: {e}", exc_info=True)
        
        logging.info(f"Decision cycle complete. Next check in {DECISION_MAKING_INTERVAL / 3600:.1f} hour(s).")
        time.sleep(DECISION_MAKING_INTERVAL)

if __name__ == "__main__":
    required_vars = ['QBIT_HOST', 'QBIT_PORT', 'QBIT_USER', 'QBIT_PASS', 'DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASS', 'SSD_PATH_IN_CONTAINER', 'ARRAY_PATH_IN_CONTAINER']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        logging.critical(f"Critical environment variables are missing: {', '.join(missing_vars)}. Exiting.")
        exit(1)
        
    if DRY_RUN:
        logging.warning("="*50); logging.warning("=== SCRIPT IS RUNNING IN DRY RUN MODE ==="); logging.warning("="*50)
    
    logging.info("Starting Seederr (v11.0 - Dual-Loop Architecture)")

    qbit_client = QBittorrentClient(QBIT_CONFIG)
    db_connection = db_connect()
    
    # Start the two main loops in separate threads
    collector_thread = threading.Thread(target=data_collector_loop, args=(qbit_client, db_connection), daemon=True)
    decision_thread = threading.Thread(target=decision_maker_loop, args=(qbit_client, db_connection), daemon=True)
    
    collector_thread.start()
    decision_thread.start()
    
    # Keep the main thread alive
    while True:
        time.sleep(1)