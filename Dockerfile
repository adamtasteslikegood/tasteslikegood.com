# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
# PYTHONDONTWRITEBYTECODE: Prevents Python from writing pyc files to disc
# PYTHONUNBUFFERED: Prevents Python from buffering stdout and stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
# gcc and other tools might be needed for some python packages
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt /app/

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . /app/

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Run app.py when the container launches
# Using gunicorn for production is better than the flask dev server
# But for now, adhering to the "mimic existing structure" and simpler setup if gunicorn isn't in requirements.
# However, "production grade" usually implies a WSGI server.
# Checking requirements.txt again... gunicorn is NOT there.
# I will use the flask run command for now but add a comment about gunicorn.
# Or better, I will use python app.py since that's how it's currently run.
CMD ["python", "app.py"]
