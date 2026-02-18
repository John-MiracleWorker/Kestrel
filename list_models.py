
import requests
import os

API_KEY = os.getenv("GOOGLE_API_KEY", "YOUR_API_KEY")
ENDPOINTS = [
    f"https://generativelanguage.googleapis.com/v1/models?key={API_KEY}",
    f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
]

for URL in ENDPOINTS:
    print(f"\nQuerying {URL}...")
    resp = requests.get(URL)
    if resp.status_code == 200:
        models = resp.json().get("models", [])
        print(f"Available Models ({len(models)}):")
        for m in models:
            print(f" - {m['name']} (supported_methods: {m.get('supportedGenerationMethods')})")
    else:
        print(f"Error: {resp.status_code}")
        print(resp.text)
