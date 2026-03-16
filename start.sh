#!/bin/bash
cd /Users/titougranier/Desktop/jobhunter-ai
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir /Users/titougranier/Desktop/jobhunter-ai
