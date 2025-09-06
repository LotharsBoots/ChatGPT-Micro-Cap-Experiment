#!/bin/bash

echo "========================================"
echo "   Automated Trading Dashboard Setup"
echo "========================================"
echo

echo "Installing Python dependencies..."
pip install -r requirements.txt

echo
echo "Starting the trading dashboard..."
echo
echo "The dashboard will open in your browser at:"
echo "http://localhost:5000"
echo
echo "Press Ctrl+C to stop the application"
echo

python app.py
