import requests
import json

response = requests.get(
  url="https://openrouter.ai/api/v1/key",
  headers={
    "Authorization": f"Bearer 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"
  }
)

print(json.dumps(response.json(), indent=2))
