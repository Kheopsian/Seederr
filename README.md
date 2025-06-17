# Seederr: A Smart Seeder Manager

![Docker Build Status](https://img.shields.io/docker/build/kheopsian/seederr.svg)
![Docker Image Size](https://img.shields.io/docker/image-size/kheopsian/seederr)

Seederr is a Python-based utility, designed to run in a Docker container on Unraid, that intelligently manages your seeding torrents. It automatically moves torrents between a fast SSD cache and a large, slower storage array based on their real-time and long-term seeding performance.

The goal is to maximize your seed ratio and network contribution by ensuring the most active and valuable torrents always reside on the fastest storage, while less popular ones are relegated to the mass storage array without interrupting seeding.

## Key Features

-   **Performance-Based Scoring**: Ranks torrents using a weighted score combining a long-term Exponential Moving Average (EMA) of their upload rate and their immediate upload rate.
-   **"Top-K" Rebalancing**: Keeps the fast SSD filled to a user-defined target capacity with the best-performing torrents.
-   **Category-Aware Moves**: Preserves qBittorrent category subdirectories (e.g., `.../movies/` or `.../tv/`) when moving files, ensuring compatibility with Sonarr, Radarr, and your organizational structure.
-   **Persistent Stats**: Uses a PostgreSQL database to track performance metrics over time.
-   **Dry Run Mode**: Allows you to safely test the script's logic by logging intended actions without moving any files.
-   **Unraid Integration**: Easily installable and configurable through Unraid's Community Applications plugin using the provided template.

## Prerequisites

-   An Unraid server with the Community Applications plugin installed.
-   A running instance of qBittorrent, accessible over the network.
-   A running instance of PostgreSQL, accessible over the network, with a dedicated database created.
-   A "hot" download location on a fast drive (SSD) and a "cold" download location on a slower drive/array.

## Installation on Unraid

The recommended installation method is using Git to clone the template repository directly onto your Unraid boot drive. This makes future updates trivial.

### Step 1: Install Git on Unraid (if you haven't already)

1.  In the Unraid WebUI, go to the **"Apps"** tab.
2.  Search for and install `Nerd Tools`.
3.  Go to **"Settings"** -> **"Nerd Tools"**.
4.  Find `git` in the list and toggle it **ON**. Click **Apply**.

### Step 2: Install the Template

1.  Connect to your Unraid server via SSH or use the built-in WebUI Terminal.
2.  Navigate to the Community Applications plugin directory:
    ```bash
    cd /boot/config/plugins/community.applications/
    ```
3.  Clone this repository into a folder named `private`. This specific name is required by the CA plugin.
    ```bash
    # Replace YOUR_GITHUB_USERNAME with your actual username
    git clone [https://github.com/YOUR_GITHUB_USERNAME/seederr.git](https://github.com/YOUR_GITHUB_USERNAME/seederr.git) private
    ```

### Step 3: Install the Application

1.  In the Unraid WebUI, go to the **"Apps"** tab.
2.  Click "Check for Updates" to force a rescan of the templates.
3.  Search for `Seederr`. Your private application should now appear.
4.  Click **Install** and fill out the configuration variables as detailed below.

## Configuration

All configuration is handled via environment variables in the Unraid template.

| Variable                        | Description                                                                                              | Default Value           | Required |
| ------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------- | :------: |
| **qBittorrent Settings** |                                                                                                          |                         |          |
| `QBIT_HOST`                     | The IP address of your qBittorrent instance.                                                             | `192.168.1.100`         |   Yes    |
| `QBIT_PORT`                     | The WebUI port for qBittorrent.                                                                          | `8080`                  |   Yes    |
| `QBIT_USER`                     | qBittorrent username.                                                                                    | `admin`                 |   Yes    |
| `QBIT_PASS`                     | qBittorrent password.                                                                                    |                         |   Yes    |
| **PostgreSQL Settings** |                                                                                                          |                         |          |
| `DB_HOST`                       | The IP address of your PostgreSQL database.                                                              | `192.168.1.100`         |   Yes    |
| `DB_PORT`                       | The port for your PostgreSQL database.                                                                   | `5432`                  |   Yes    |
| `DB_NAME`                       | The name of the database for Seederr to use.                                                             | `torrents_stats`        |   Yes    |
| `DB_USER`                       | The username for the database.                                                                           |                         |   Yes    |
| `DB_PASS`                       | The password for the database user.                                                                      |                         |   Yes    |
| **Path Settings** | **These paths are inside the container.** They must match the container-side paths in your volume mappings. |                         |          |
| `SSD_PATH_IN_CONTAINER`         | The internal path to the "hot" SSD storage.                                                              | `/downloads/hot`        |   Yes    |
| `ARRAY_PATH_IN_CONTAINER`       | The internal path to the "cold" array storage.                                                           | `/downloads/cold`       |   Yes    |
| **Logic Parameters** |                                                                                                          |                         |          |
| `DRY_RUN`                       | `true`: Log actions without moving files. `false`: Enable real file moves.                               | `true`                  |   Yes    |
| `CHECK_INTERVAL_SECONDS`        | How often the script should run, in seconds.                                                             | `3600` (1 hour)         |   Yes    |
| `SSD_TARGET_CAPACITY_PERCENT`   | The target fill percentage for the SSD. The script will try to fill it to this level with top torrents.  | `90`                    |   Yes    |
| `MAX_MOVES_PER_CYCLE`           | The maximum number of files to move in a single run. Prevents I/O storms. Set to `-1` for unlimited moves in Dry Run. | `1`                     |   Yes    |
| `WEIGHT_LONG_TERM`              | The weight (0.0-1.0) to give the long-term EMA score.                                                    | `0.8`                   |    No    |
| `WEIGHT_SHORT_TERM`             | The weight (0.0-1.0) to give the short-term upload rate score.                                           | `0.2`                   |    No    |
| `EMA_ALPHA`                     | The smoothing factor for the long-term EMA calculation. A smaller value means a longer-term average.   | `0.012`                 |    No    |


## Usage

1.  **Initial Run (Dry Run)**: After installing, leave `DRY_RUN` set to `true`. This is a critical safety step. Check the container logs to see what decisions the script is making.
    ```bash
    docker logs -f seederr
    ```
    You will see `[DRY RUN] ACTION: Would move...` messages. Monitor these for a few cycles to ensure the logic is behaving as you expect.

2.  **Going Live**: Once you are confident in the script's decisions, edit the container in the Unraid Docker tab, change the `DRY_RUN` variable to `false`, and apply the changes. The script will now perform actual file moves on its next run.

## License

This project is licensed under the MIT License.