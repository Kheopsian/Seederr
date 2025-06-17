# Use an official lightweight Python image
FROM python:3.9-slim

# Define the working directory in the container
WORKDIR /app

# Copy the requirements file first and install dependencies
# The advantage of copying it alone first is that Docker will cache this layer
# if the file doesn't change, speeding up future builds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the source code into the container
COPY . .

# The command to execute when the container starts
CMD ["python", "smart_seeder_manager.py"]