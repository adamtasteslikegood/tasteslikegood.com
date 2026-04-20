import logging
import threading
import sys
import os

# Add parent directory to path so imports work correctly
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from workers.recipe_worker import start_recipe_worker
from workers.image_worker import start_image_worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    """
    Entry point to run both Pub/Sub workers in parallel.
    This script listens to both the recipe-generation and image-generation topics.
    """
    logger.info("Starting Pub/Sub workers...")
    
    recipe_thread = threading.Thread(target=start_recipe_worker, daemon=True)
    image_thread = threading.Thread(target=start_image_worker, daemon=True)
    
    recipe_thread.start()
    image_thread.start()
    
    try:
        # Keep the main thread alive
        while recipe_thread.is_alive() and image_thread.is_alive():
            recipe_thread.join(timeout=1.0)
            image_thread.join(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("Shutting down workers...")

if __name__ == "__main__":
    main()
