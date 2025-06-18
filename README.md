# Seederr: A Smart Seeder Manager

![Docker Build Status](https://img.shields.io/docker/build/kheopsian/seederr.svg)
![Docker Image Size](https://img.shields.io/docker/image-size/kheopsian/seederr)

Seederr is a Python-based utility, designed to run in a Docker container, that intelligently manages your seeding torrents. It operates on a promotion/relegation model, treating your slow, mass storage as a permanent "master" library and your fast SSD as a "seeding cache".

The goal is to maximize your seed ratio by copying the most active torrents to your fast storage for optimal seeding. When performance drops, the cached copy is deleted, and seeding continues from the master location, ensuring that hardlinks for your media library (Sonarr, Radarr, Plex) are never broken.

## Key Features

-   **Performance-Based Scoring**: Ranks torrents using a weighted score combining long-term and short-term upload rates.
-   **SSD Cache Management**: Intelligently copies popular torrents from a "master" array to a "cache" SSD.
-   **Non-Destructive Relegation**: Safely removes torrents from the cache by repointing qBittorrent back to the master file and deleting the temporary copy, preserving hardlinks.
-   **Permissions Handling**: Uses PUID/PGID for proper file ownership.
-   **Persistent Stats**: Uses a PostgreSQL database to track performance metrics.
-   **Dry Run Mode**: Safely test the script's logic without moving any files.

## Prerequisites & Path Structure

Seederr is designed to integrate seamlessly with the popular Unraid application ecosystem (Sonarr, Radarr, qBittorrent). This requires a specific path structure.

1.  **Data Volume**: A main data share for all your media-related applications (e.g., `/mnt/user/data/`). This path should be mapped as `/data` inside Seederr, qBittorrent, Sonarr, and Radarr. This volume typically contains:
    * `/downloads`: For qBittorrent's completed downloads. This will be Seederr's "master" path.
    * `/media`: For your Sonarr/Radarr library, which contains hardlinks pointing to files in `/downloads`.
2.  **Cache Volume**: A dedicated folder on a fast SSD for caching popular torrents (e.g., `/mnt/disks/your_ssd/cache/`). This path must be mapped as `/cache` inside **both Seederr and qBittorrent**.

## Installation on Unraid

*(The installation steps remain the same, by cloning the repository into the CA plugin folder. Omitted for brevity.)*

## Configuration

Configuration is handled via environment variables. **Pay close attention to the path mappings.**

| Variable                        | Description                                                                                                                              | Default Value           | Required |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | :------: |
| **User / Group IDs** |                                                                                                                                          |                         |          |
| `PUID`                          | User ID for file permissions. Set to your Unraid user's ID.                                                                             | `99` (nobody)           |   Yes    |
| `PGID`                          | Group ID for file permissions. Set to your Unraid user's group ID.                                                                        | `100` (users)           |   Yes    |
| **qBittorrent Settings** |                                                                                                                                          |                         |          |
| `QBIT_HOST`                     | The IP address of your qBittorrent instance.                                                                                             | `192.168.1.100`         |   Yes    |
| `QBIT_PORT`                     | The WebUI port for qBittorrent.                                                                                                          | `8080`                  |   Yes    |
| `QBIT_USER`                     | qBittorrent username.                                                                                                                    | `admin`                 |   Yes    |
| `QBIT_PASS`                     | qBittorrent password.                                                                                                                    |                         |   Yes    |
| **PostgreSQL Settings** |                                                                                                                                          |                         |          |
| `DB_HOST`                       | The IP address of your PostgreSQL database.                                                                                              | `192.168.1.100`         |   Yes    |
| `DB_PORT`                       | The port for your PostgreSQL database.                                                                                                   | `5432`                  |   Yes    |
| `DB_NAME`                       | The name of the database for Seederr to use.                                                                                             | `torrents_stats`        |   Yes    |
| `DB_USER`                       | The username for the database.                                                                                                           |                         |   Yes    |
| `DB_PASS`                       | The password for the database user.                                                                                                      |                         |   Yes    |
| **Path Settings** | **These paths are inside the container.** They are derived from your volume mappings.                                                                |                         |          |
| `SSD_PATH_IN_CONTAINER`         | The internal container path to the SSD seeding cache.                                                                                    | `/cache`                |   Yes    |
| `ARRAY_PATH_IN_CONTAINER`       | The internal container path to the master downloads folder.                                                                              | `/data/downloads`       |   Yes    |
| **Logic Parameters** |                                                                                                                                          |                         |          |
| `DRY_RUN`                       | `true`: Log actions without copying/deleting files. `false`: Enable real file operations.                                                 | `true`                  |   Yes    |
| `CHECK_INTERVAL_SECONDS`        | How often the script should run, in seconds.                                                                                             | `3600` (1 hour)         |   Yes    |
| `SSD_TARGET_CAPACITY_PERCENT`   | The target fill percentage for the SSD cache.                                                                                             | `90`                    |   Yes    |
| `MAX_MOVES_PER_CYCLE`           | The maximum number of promotions/relegations to perform in a single run.                                                                  | `1`                     |   Yes    |


## Crucial Setup Steps

1.  **Configure qBittorrent Paths**:
    * Set qBittorrent's default save path to `/data/downloads`.
    * **CRITICAL**: Add a volume mapping to your qBittorrent container that maps your host cache folder to `/cache` inside the container. If you don't do this, qBittorrent won't be able to find the files that Seederr promotes, and seeding will fail.

2.  **Initial Run (Dry Run)**: After installing, leave `DRY_RUN` set to `true`. Check the container logs to see what decisions the script is making.
    ```bash
    docker logs -f seederr
    ```
    You will see messages like `[DRY RUN] PROMOTION: Would copy...` or `[DRY RUN] RELEGATION: Would repoint qBit and delete...`. Monitor these to ensure the logic is behaving as you expect.

3.  **Going Live**: Once confident, set `DRY_RUN` to `false`.