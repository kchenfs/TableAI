import requests
import json

response = requests.get(
  url="https://openrouter.ai/api/v1/key",
  headers={
    "Authorization": f"Bearer sk-or-7"
  }
)

print(json.dumps(response.json(), indent=2))
