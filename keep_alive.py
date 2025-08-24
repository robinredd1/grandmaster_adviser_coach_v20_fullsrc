from flask import Flask, jsonify, send_from_directory
import os, json
from config import *

app = Flask(__name__, static_folder="public")

@app.get("/")
def home(): return send_from_directory("public","index.html")

@app.get("/dashboard")
def dash(): return send_from_directory("public","index.html")

@app.get("/health")
def health(): return jsonify(ok=True), 200

@app.get("/account")
def account():
    p=os.path.join("public","account.json")
    if not os.path.exists(p): return jsonify(ok=False, error="no account yet"), 200
    try:
        with open(p,"r") as f: data=json.load(f)
        data["ok"]=True; return jsonify(data), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200

@app.get("/picks")
def picks():
    p=os.path.join("public","picks.json")
    if not os.path.exists(p): return jsonify(ok=False, error="no picks yet"), 200
    try:
        with open(p,"r") as f: data=json.load(f)
        data["ok"]=True; return jsonify(data), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200

def run():
    port=int(os.environ.get("PORT",8000))
    app.run(host="0.0.0.0", port=port)

if __name__=="__main__":
    run()
