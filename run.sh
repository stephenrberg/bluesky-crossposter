#!/bin/bash

# Run once per hour if nothing else has been specified in environment variables
while :; do
  python run.py
  sleep 900
done