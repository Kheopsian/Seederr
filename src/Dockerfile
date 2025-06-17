# Use an official lightweight Python image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file from the src directory and install dependencies
# The advantage of copying it alone first is that Docker will cache this layer
# if the file doesn't change, speeding up future builds.
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's source code from the src directory
COPY src/ .

# The command to run when the container starts
CMD ["python", "smart_seeder_manager.py"]