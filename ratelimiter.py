import requests
import json

response = requests.get(
  url="https://openrouter.ai/api/v1/key",
  headers={
<<<<<<< HEAD
    "Authorization": f"Bearer sk-or-"
  }
)

print(json.dumps(response.json(), indent=2))
