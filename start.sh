#!/usr/bin/env bash

sudo pkill python
source venv/bin/activate
sudo pigpiod
python3 main.py
