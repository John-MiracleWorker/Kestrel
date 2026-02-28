import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

from server import get_service

print("Starting Gmail authentication...")
try:
    service = get_service()
    print("Authentication successful! token.json has been created.")
except Exception as e:
    print(f"Error during authentication: {e}")
