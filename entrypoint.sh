#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# The WORKDIR is /app, so all paths are relative to it.

echo "Waiting for PostgreSQL to be ready..."
# A simple loop to wait for the DB to be available.
n=0
while ! pg_isready -h $DB_HOST -p $DB_PORT -U $DB_USER > /dev/null 2>&1; do
  n=$((n+1))
  if [ $n -gt 30 ]; then
    echo "Database connection failed after 30 attempts. Exiting."
    exit 1
  fi
  echo "Attempt $n: DB not ready, waiting 1 second..."
  sleep 1
done
echo "PostgreSQL is ready."


echo "Running database migrations..."
# Use the -c flag to specify the path to the alembic.ini config file.
# We also pass the DB credentials as arguments to the config.
alembic -c /app/alembic.ini -x DB_USER=$DB_USER -x DB_PASS=$DB_PASS -x DB_HOST=$DB_HOST -x DB_PORT=$DB_PORT -x DB_NAME=$DB_NAME upgrade head
echo "Database migrations complete."


echo "Starting Seederr application..."
# Execute the main application from its source directory.
python src/smart_seeder_manager.py