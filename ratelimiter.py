import requests
import json

response = requests.get(
  url="https://openrouter.ai/api/v1/key",
  headers={
<<<<<<< HEAD
    "Authorization": f"Bearer sk-or-7"
=======
    "Authorization": f"Bearer sk-or-v"
>>>>>>> 51d14d439a0f1305bd8ade9e8b1508d08e1d1635
  }
)

print(json.dumps(response.json(), indent=2))
