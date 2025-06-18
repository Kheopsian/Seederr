# Seederr: A Smart Torrent Manager

Seederr is a Python-based utility, designed to run in a Docker container, that intelligently manages your seeding torrents. It operates on a promotion/relegation model, treating your slow, mass storage as a permanent "master" library and your fast SSD as a "seeding cache".

The goal is to maximize your seed ratio by copying the most active torrents to your fast storage for optimal seeding. Popularity is determined by a sophisticated weighted score that prioritizes **active demand (leechers)** and **swarm health (seeder/leecher ratio)**. When performance drops, the cached copy is deleted, and seeding continues from the master location, ensuring that hardlinks for your media library (Sonarr, Radarr, Plex) are never broken.

## Key Features

-   **Weighted Popularity Scoring**: Ranks torrents using a tunable score based on active leechers, seeder/leecher ratio, and upload rates.
-   **SSD Cache Management**: Intelligently copies popular torrents from a "master" array to a "cache" SSD.
-   **Non-Destructive Relegation**: Safely removes torrents from the cache by repointing qBittorrent back to the master file and deleting the temporary copy, preserving hardlinks.
-   **Permissions Handling**: Uses PUID/PGID for proper file ownership.
-   **Persistent Stats**: Uses a PostgreSQL database to track performance metrics.
-   **Dry Run Mode**: Safely test the script's logic without moving any files.

## Prerequisites & Path Structure

Seederr is designed to integrate seamlessly with the popular Unraid application ecosystem (Sonarr, Radarr, qBittorrent). This requires a specific path structure.

1.  **Data Volume**: A main data share for all your media-related applications (e.g., `/mnt/user/data/`). This path should be mapped as `/data` inside Seederr, qBittorrent, Sonarr, and Radarr.
2.  **Cache Volume**: A dedicated folder on a fast SSD for caching popular torrents (e.g., `/mnt/disks/your_ssd/cache/`). This path must be mapped as `/cache` inside **both Seederr and qBittorrent**.

## Configuration

Configuration is handled via environment variables.

| Variable                      | Description                                                                                                                                                                                                                                                            | Default Value           |
| :---------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------------- |
| **User / Group IDs** |                                                                                                                                                                                                                                                                        |                         |
| `PUID`                        | User ID for file permissions.                                                                                                                                                                                                            | `99`           |
| `PGID`                        | Group ID for file permissions.                                                                                                                                                                                                   | `100`           |
| **qBittorrent Settings** |                                                                                                                                                                                                                                                                        |                         |
| `QBIT_HOST`                   | The IP address of your qBittorrent instance.                                                                                                                                                                                                                         | `192.168.1.100`         |
| `QBIT_PORT`                   | The WebUI port for qBittorrent.                                                                                                                                                                                                                                      | `8080`                  |
| `QBIT_USER`                   | qBittorrent username.                                                                                                                                                                                                                                                | `admin`                 |
| `QBIT_PASS`                   | qBittorrent password.                                                                                                                                                                                                                                                |                         |
| **PostgreSQL Settings** |                                                                                                                                                                                                                                                                        |                         |
| `DB_HOST`                     | The IP address of your PostgreSQL database.                                                                                                                                                                                                                          | `192.168.1.100`         |
| `DB_PORT`                     | The port for your PostgreSQL database.                                                                                                                                                                                                                               | `5432`                  |
| `DB_NAME`                     | The name of the database for Seederr to use.                                                                                                                                                                                                                         | `torrents_stats`        |
| `DB_USER`                     | The username for the database.                                                                                                                                                                                                                                       |                         |
| `DB_PASS`                     | The password for the database user.                                                                                                                                                                                                                                  |                         |
| **Path Settings** | **These paths are inside the container.** |                         |
| `SSD_PATH_IN_CONTAINER`       | The internal container path to the SSD seeding cache.                                                                                                                                                                                                               | `/cache`                |
| `ARRAY_PATH_IN_CONTAINER`     | The internal container path to the master downloads folder.                                                                                                                                                                                                          | `/data/downloads`       |
| **Logic Parameters** |                                                                                                                                                                                                                                                                        |                         |
| `DRY_RUN`                     | `true`: Log actions without copying/deleting files. `false`: Enable real file operations.                                                                                                                                                                            | `true`                  |
| `CHECK_INTERVAL_SECONDS`      | How often the script should run, in seconds.                                                                                                                                                                                                                         | `3600` (1 hour)         |
| `SSD_TARGET_CAPACITY_PERCENT` | The target fill percentage for the SSD cache.                                                                                                                                                                                                                        | `90`                    |
| `MAX_MOVES_PER_CYCLE`         | The maximum number of promotions/relegations to perform in a single run.                                                                                                                                                                                             | `1`                     |
| `WEIGHT_LEECHERS`             | Weight for the number of leechers. Prioritizes active demand.                                                                                                                             | `1000.0`                |
| `WEIGHT_SL_RATIO`   | Weight for the Seeder/Leecher ratio bonus. Favors torrents in need of seeders.                                                                                                               | `200.0`                |
