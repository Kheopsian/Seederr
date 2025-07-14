#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# Go to the application directory
cd /app/src

echo "Waiting for PostgreSQL to be ready..."
# A simple loop to wait for the DB to be available.
# This avoids race conditions on startup.
# Note: This is a basic check. For production, a more robust tool like wait-for-it.sh could be used.
# However, for a home server, this is generally sufficient.
# We'll try for 30 seconds.
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
# Use alembic to upgrade the database to the latest version
# The --x-arg is used to pass environment variables to alembic.ini
alembic -x DB_USER=$DB_USER -x DB_PASS=$DB_PASS -x DB_HOST=$DB_HOST -x DB_PORT=$DB_PORT -x DB_NAME=$DB_NAME upgrade head
echo "Database migrations complete."


echo "Starting Seederr application..."
# Execute the main application
python smart_seeder_manager.py