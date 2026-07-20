import json, importlib, os, sys, types

# Stub heavy deps so app.py imports without real AWS/AI setup.
os.environ.setdefault("MENU_TABLE_NAME", "m"); os.environ.setdefault("ORDERS_TABLE_NAME", "o")
for mod in ["openai", "google.generativeai", "numpy", "faiss", "boto3", "stripe"]:
    sys.modules.setdefault(mod.split(".")[0], types.ModuleType(mod.split(".")[0]))

# `import google.generativeai as genai` needs the submodule registered under
# sys.modules and set as an attribute of the parent package.
_google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
_generativeai_stub = sys.modules.setdefault("google.generativeai", types.ModuleType("google.generativeai"))
_google_pkg.generativeai = _generativeai_stub

# app.py needs `OpenAI` (a class it instantiates) from the openai stub, and
# `boto3.resource`/`boto3.client` (called at import time) to be callable.
sys.modules["openai"].OpenAI = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(
    Table=lambda *a, **k: types.SimpleNamespace())
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()

def test_no_items_returns_failed(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, {}, None))
    event = {"sessionState": {"intent": {"name": "OrderFood", "state": "ReadyForFulfillment"},
             "sessionAttributes": {"parsedOrder": json.dumps({"order_items": []}), "orderMode": "dine-in"}}}
    resp = app.fulfill_order(event)
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
    assert "what would you like" in resp["messages"][0]["content"].lower()
